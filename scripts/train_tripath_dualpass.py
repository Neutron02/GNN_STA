#!/usr/bin/env python3
"""Train a tripartite dual-pass timing model with path-aware supervision.

Architecture highlights:
- Tripartite graph: pin + cell + net nodes.
- Relation-specific message passing over directed typed edges.
- Dual-pass latent states (forward/backward timing semantics).
- Global-context conditioning (clock, density, placement/size features).
- Path auxiliary head trained from path endpoints in paths_summary.csv.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ml_training_common import (
    EDGE_NUMERIC_COLS,
    NODE_FEATURE_COLS,
    load_dataset_index,
    load_splits,
    require_torch,
    select_rows,
)

try:
    import torch
    import torch.nn as nn
except Exception:  # pragma: no cover
    torch = None
    nn = None


RELATIONS = (
    "cell_arc",
    "pin_to_cell",
    "cell_to_pin",
    "drv_to_net",
    "net_to_sink",
    "net_pair",
)

# Keep only sweep/runtime knobs that should transfer across designs.
GLOBAL_FEATURE_KEYS = (
    "clock_period_ns",
    "abc_area",
    "place_density",
    "routing_layer_adjustment",
)


@dataclass
class TripGraph:
    run_id: str
    design: str
    pin_x: "torch.Tensor"
    cell_x: "torch.Tensor"
    net_x: "torch.Tensor"
    global_x: "torch.Tensor"
    cell_type_tokens: List[str]
    cell_type_idx: Optional["torch.Tensor"]
    edge_rel_local: Dict[str, "torch.Tensor"]
    y_sec: "torch.Tensor"  # [P, T]
    y_norm: "torch.Tensor"  # [P, T]
    finite_idx: "torch.Tensor"
    wns_sec: Optional[float]
    path_pairs_pin: "torch.Tensor"  # [M, 2] pin indices
    path_targets_sec: "torch.Tensor"  # [M, 2] (arrival, slack)
    path_targets_norm: "torch.Tensor"  # [M, 2]

    @property
    def num_pins(self) -> int:
        return int(self.pin_x.size(0))

    @property
    def num_cells(self) -> int:
        return int(self.cell_x.size(0))

    @property
    def num_nets(self) -> int:
        return int(self.net_x.size(0))


@dataclass
class TripNormStats:
    pin_mean: List[float]
    pin_std: List[float]
    cell_mean: List[float]
    cell_std: List[float]
    net_mean: List[float]
    net_std: List[float]
    global_mean: List[float]
    global_std: List[float]
    target_scale: float
    target_mean: List[float]
    target_std: List[float]

    def to_dict(self) -> Dict[str, object]:
        return {
            "pin_mean": self.pin_mean,
            "pin_std": self.pin_std,
            "cell_mean": self.cell_mean,
            "cell_std": self.cell_std,
            "net_mean": self.net_mean,
            "net_std": self.net_std,
            "global_mean": self.global_mean,
            "global_std": self.global_std,
            "target_scale": self.target_scale,
            "target_mean": self.target_mean,
            "target_std": self.target_std,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "TripNormStats":
        return cls(
            pin_mean=[float(v) for v in d["pin_mean"]],
            pin_std=[float(v) for v in d["pin_std"]],
            cell_mean=[float(v) for v in d["cell_mean"]],
            cell_std=[float(v) for v in d["cell_std"]],
            net_mean=[float(v) for v in d["net_mean"]],
            net_std=[float(v) for v in d["net_std"]],
            global_mean=[float(v) for v in d["global_mean"]],
            global_std=[float(v) for v in d["global_std"]],
            target_scale=float(d["target_scale"]),
            target_mean=[float(v) for v in d["target_mean"]],
            target_std=[float(v) for v in d["target_std"]],
        )


@dataclass
class TripBatch:
    pin_x: "torch.Tensor"
    cell_x: "torch.Tensor"
    net_x: "torch.Tensor"
    global_x: "torch.Tensor"  # [B, G]
    cell_type_idx: Optional["torch.Tensor"]
    edge_rel_global: Dict[str, "torch.Tensor"]
    node_graph_idx: "torch.Tensor"  # [total_nodes]
    y_sec: "torch.Tensor"  # [P, T]
    y_norm: "torch.Tensor"  # [P, T]
    loss_idx: "torch.Tensor"  # pin indices in global pin space
    path_pairs_global: "torch.Tensor"  # [M, 2] pin indices
    path_targets_sec: "torch.Tensor"  # [M, 2]
    path_targets_norm: "torch.Tensor"  # [M, 2]
    graph_pin_ptr: List[int]
    graph_wns_sec: List[Optional[float]]
    graph_run_ids: List[str]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _resolve_device(name: str):
    if name == "cpu":
        return torch.device("cpu")
    if name == "cuda":
        return torch.device("cuda")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _to_float(value: object) -> float:
    if value is None:
        return 0.0
    s = str(value).strip()
    if s == "":
        return 0.0
    try:
        v = float(s)
    except Exception:
        return 0.0
    if not math.isfinite(v):
        return 0.0
    return v


def _parse_wns_sec(global_json_path: Path) -> Optional[float]:
    if not global_json_path.exists():
        return None
    try:
        payload = json.loads(global_json_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    wns_ns = payload.get("wns_ns")
    if wns_ns is None:
        return None
    try:
        return float(wns_ns) * 1e-9
    except Exception:
        return None


def _parse_csv_list(s: str) -> List[str]:
    return [v.strip() for v in str(s).split(",") if v.strip()]


def _parse_float_list(s: str, expected_len: int) -> List[float]:
    vals = [float(v.strip()) for v in str(s).split(",") if v.strip()]
    if len(vals) == 1 and expected_len > 1:
        vals = vals * expected_len
    if len(vals) != expected_len:
        raise ValueError(f"Expected {expected_len} weights, got {len(vals)} from '{s}'")
    return vals


def _target_idx(target_cols: Sequence[str], col: str) -> int:
    if col not in target_cols:
        raise ValueError(f"Target column '{col}' not found in target list: {target_cols}")
    return int(target_cols.index(col))


def _cell_type_token(nrow: Dict[str, str]) -> str:
    cell = str(nrow.get("cell_name", "")).strip()
    if cell:
        return cell
    node_kind = str(nrow.get("node_kind", "")).strip().lower()
    io_type = str(nrow.get("io_type", "")).strip().upper() or "UNK"
    if node_kind == "bterm":
        return f"BTERM_{io_type}"
    return "UNK_CELL"


def _pin_name_aliases(name: str) -> List[str]:
    s = str(name).strip()
    if not s:
        return []

    # Normalize escaped pin/index text from CSV exports (e.g. "\[" -> "[").
    s = s.replace("\\[", "[").replace("\\]", "]").replace("\\", "")
    out = [s]

    # Add hierarchy-stripped aliases to match path reports that may omit top module names.
    if "." in s:
        parts = s.split(".")
        for i in range(1, len(parts)):
            out.append(".".join(parts[i:]))
    out.append(s.split(".")[-1])

    uniq: List[str] = []
    seen = set()
    for v in out:
        vv = v.strip()
        if not vv or vv in seen:
            continue
        seen.add(vv)
        uniq.append(vv)
    return uniq


def _write_json(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _write_epoch_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "epoch",
        "train_loss",
        "train_norm_mse",
        "train_rmse_ps",
        "val_norm_mse",
        "val_rmse_ps",
        "test_norm_mse",
        "test_rmse_ps",
        "val_wns_mae_ps",
        "test_wns_mae_ps",
        "val_path_slack_rmse_ps",
        "test_path_slack_rmse_ps",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def _pairwise_rank_loss(
    pred: "torch.Tensor",
    truth: "torch.Tensor",
    pairs: int,
    margin: float,
) -> "torch.Tensor":
    n = int(truth.numel())
    if n < 2 or pairs <= 0:
        return pred.new_tensor(0.0)
    ii = torch.randint(0, n, (pairs,), device=pred.device)
    jj = torch.randint(0, n, (pairs,), device=pred.device)
    neq = ii != jj
    if int(neq.sum().item()) == 0:
        return pred.new_tensor(0.0)
    ii = ii[neq]
    jj = jj[neq]
    d_true = truth[jj] - truth[ii]
    sign = torch.sign(d_true)
    valid = sign != 0
    if int(valid.sum().item()) == 0:
        return pred.new_tensor(0.0)
    sign = sign[valid]
    ii = ii[valid]
    jj = jj[valid]
    d_pred = pred[jj] - pred[ii]
    return torch.relu(float(margin) - sign * d_pred).mean()


def _safe_json(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_global_features(
    global_payload: Dict[str, object],
) -> List[float]:
    vals = []
    for k in GLOBAL_FEATURE_KEYS:
        v = global_payload.get(k)
        if v is None:
            vals.append(0.0)
        else:
            vals.append(_to_float(v))
    return vals


def load_trip_graph(row: Dict[str, str], target_cols: Sequence[str], max_paths: int) -> TripGraph:
    run_id = row["run_id"]
    design = row["design"]

    nodes_rows = _read_csv(Path(row["nodes_csv"]))
    edges_rows = _read_csv(Path(row["edges_csv"]))
    labels_rows = _read_csv(Path(row["labels_csv"]))
    paths_rows = _read_csv(Path(row["paths_csv"])) if Path(row["paths_csv"]).exists() else []
    global_json = Path(row["global_json"])

    node_id_to_pin_idx: Dict[str, int] = {}
    node_name_to_pin_idx: Dict[str, int] = {}
    pin_feats: List[List[float]] = []
    cell_type_tokens: List[str] = []

    xs: List[float] = []
    ys: List[float] = []

    # Per-cell aggregation from pin rows.
    cell_name_to_idx: Dict[str, int] = {}
    cell_acc: List[Dict[str, float]] = []
    pin_to_cell_idx: List[Optional[int]] = []

    for i, nrow in enumerate(nodes_rows):
        node_id_to_pin_idx[nrow["node_id"]] = i
        node_name = str(nrow.get("node_name", "")).strip()
        inst_name = str(nrow.get("inst_name", "")).strip()
        port_name = str(nrow.get("port_name", "")).strip()
        name_candidates = [node_name]
        if inst_name and port_name:
            name_candidates.append(f"{inst_name}/{port_name}")
        for cand in name_candidates:
            for alias in _pin_name_aliases(cand):
                node_name_to_pin_idx.setdefault(alias, i)
        pin_feats.append([_to_float(nrow.get(col, "0")) for col in NODE_FEATURE_COLS])
        cell_type_tokens.append(_cell_type_token(nrow))

        x = _to_float(nrow.get("x_um", "0"))
        y = _to_float(nrow.get("y_um", "0"))
        xs.append(x)
        ys.append(y)

        if inst_name:
            cidx = cell_name_to_idx.get(inst_name)
            if cidx is None:
                cidx = len(cell_name_to_idx)
                cell_name_to_idx[inst_name] = cidx
                cell_acc.append(
                    {
                        "seq": 0.0,
                        "buf": 0.0,
                        "inv": 0.0,
                        "x_sum": 0.0,
                        "y_sum": 0.0,
                        "area_max": 0.0,
                        "pins": 0.0,
                        "in_pins": 0.0,
                        "out_pins": 0.0,
                    }
                )
            acc = cell_acc[cidx]
            acc["seq"] = max(acc["seq"], _to_float(nrow.get("is_sequential_cell", "0")))
            acc["buf"] = max(acc["buf"], _to_float(nrow.get("is_buffer_cell", "0")))
            acc["inv"] = max(acc["inv"], _to_float(nrow.get("is_inverter_cell", "0")))
            acc["x_sum"] += _to_float(nrow.get("inst_x_um", nrow.get("x_um", "0")))
            acc["y_sum"] += _to_float(nrow.get("inst_y_um", nrow.get("y_um", "0")))
            acc["area_max"] = max(acc["area_max"], _to_float(nrow.get("cell_area_um2", "0")))
            acc["pins"] += 1.0
            io_type = str(nrow.get("io_type", "")).strip().upper()
            if io_type == "INPUT":
                acc["in_pins"] += 1.0
            elif io_type == "OUTPUT":
                acc["out_pins"] += 1.0
            pin_to_cell_idx.append(cidx)
        else:
            pin_to_cell_idx.append(None)

    num_pins = len(pin_feats)
    x_min = min(xs) if xs else 0.0
    x_max = max(xs) if xs else 0.0
    y_min = min(ys) if ys else 0.0
    y_max = max(ys) if ys else 0.0
    bbox_w = max(0.0, x_max - x_min)
    bbox_h = max(0.0, y_max - y_min)

    # Build cell nodes.
    cell_feats: List[List[float]] = []
    for cidx in range(len(cell_acc)):
        acc = cell_acc[cidx]
        denom = max(1.0, acc["pins"])
        cell_feats.append(
            [
                acc["seq"],
                acc["buf"],
                acc["inv"],
                acc["x_sum"] / denom,
                acc["y_sum"] / denom,
                acc["area_max"],
                acc["pins"],
                acc["in_pins"],
                acc["out_pins"],
            ]
        )

    # Labels on pin nodes.
    y_sec = [[0.0] * len(target_cols) for _ in range(num_pins)]
    finite_mask = [False] * num_pins
    for lrow in labels_rows:
        idx = node_id_to_pin_idx.get(lrow.get("node_id", ""))
        if idx is None:
            continue
        for j, col in enumerate(target_cols):
            y_sec[idx][j] = _to_float(lrow.get(col, "0"))
        finite_mask[idx] = (str(lrow.get("is_slack_inf", "0")).strip() != "1")
    finite_idx = [i for i, ok in enumerate(finite_mask) if ok]
    if not finite_idx:
        raise RuntimeError(f"{run_id}: no finite target nodes found")

    # Edge relations on local indices where local ordering is:
    # pins [0, P), cells [P, P+C), nets [P+C, P+C+N)
    rel_src: Dict[str, List[int]] = {r: [] for r in RELATIONS}
    rel_dst: Dict[str, List[int]] = {r: [] for r in RELATIONS}

    # pin <-> cell relations
    num_cells = len(cell_feats)
    for pidx, cidx in enumerate(pin_to_cell_idx):
        if cidx is None:
            continue
        c_local = num_pins + cidx
        rel_src["pin_to_cell"].append(pidx)
        rel_dst["pin_to_cell"].append(c_local)
        rel_src["cell_to_pin"].append(c_local)
        rel_dst["cell_to_pin"].append(pidx)

    # net lifting and routed RC accumulation.
    net_name_to_idx: Dict[str, int] = {}
    net_acc_sum: List[List[float]] = []
    net_acc_cnt: List[int] = []

    for erow in edges_rows:
        src = node_id_to_pin_idx.get(erow.get("src_node_id", ""))
        dst = node_id_to_pin_idx.get(erow.get("dst_node_id", ""))
        if src is None or dst is None:
            continue
        edge_type = str(erow.get("edge_type", "")).strip()

        if edge_type == "cell_arc":
            rel_src["cell_arc"].append(src)
            rel_dst["cell_arc"].append(dst)
            continue

        if edge_type != "net":
            continue

        net_name = str(erow.get("net_name", "")).strip()
        numf = [_to_float(erow.get(col, "0")) for col in EDGE_NUMERIC_COLS]

        if net_name:
            nidx = net_name_to_idx.get(net_name)
            if nidx is None:
                nidx = len(net_name_to_idx)
                net_name_to_idx[net_name] = nidx
                net_acc_sum.append([0.0] * len(EDGE_NUMERIC_COLS))
                net_acc_cnt.append(0)
            for j, v in enumerate(numf):
                net_acc_sum[nidx][j] += v
            net_acc_cnt[nidx] += 1

            n_local = num_pins + num_cells + nidx
            rel_src["drv_to_net"].append(src)
            rel_dst["drv_to_net"].append(n_local)
            rel_src["net_to_sink"].append(n_local)
            rel_dst["net_to_sink"].append(dst)
        else:
            rel_src["net_pair"].append(src)
            rel_dst["net_pair"].append(dst)

    net_feats: List[List[float]] = []
    for nidx in range(len(net_acc_sum)):
        cnt = max(1, net_acc_cnt[nidx])
        net_feats.append([v / cnt for v in net_acc_sum[nidx]])
    num_nets = len(net_feats)

    edge_rel_local: Dict[str, torch.Tensor] = {}
    for rel in RELATIONS:
        if rel_src[rel]:
            edge_rel_local[rel] = torch.tensor([rel_src[rel], rel_dst[rel]], dtype=torch.long)
        else:
            edge_rel_local[rel] = torch.zeros((2, 0), dtype=torch.long)

    # Path supervision: endpoint pair + (arrival, slack).
    path_pairs: List[List[int]] = []
    path_tgts: List[List[float]] = []
    if paths_rows:
        def _rank(row: Dict[str, str]) -> int:
            try:
                return int(str(row.get("rank", "0") or "0"))
            except Exception:
                return 10**9

        sorted_rows = sorted(paths_rows, key=_rank)
        for prow in sorted_rows:
            sname = str(prow.get("startpoint", "")).strip()
            ename = str(prow.get("endpoint", "")).strip()
            sidx = None
            for alias in _pin_name_aliases(sname):
                sidx = node_name_to_pin_idx.get(alias)
                if sidx is not None:
                    break
            eidx = None
            for alias in _pin_name_aliases(ename):
                eidx = node_name_to_pin_idx.get(alias)
                if eidx is not None:
                    break
            if sidx is None or eidx is None:
                continue
            path_pairs.append([sidx, eidx])
            path_tgts.append([
                _to_float(prow.get("data_arrival_time_s", "0")),
                _to_float(prow.get("slack_s", "0")),
            ])
            if len(path_pairs) >= max_paths:
                break

    if path_pairs:
        path_pairs_t = torch.tensor(path_pairs, dtype=torch.long)
        path_tgts_sec = torch.tensor(path_tgts, dtype=torch.float32)
    else:
        path_pairs_t = torch.zeros((0, 2), dtype=torch.long)
        path_tgts_sec = torch.zeros((0, 2), dtype=torch.float32)

    g_payload = _safe_json(global_json)
    global_vals = _build_global_features(global_payload=g_payload)

    return TripGraph(
        run_id=run_id,
        design=design,
        pin_x=torch.tensor(pin_feats, dtype=torch.float32),
        cell_x=torch.tensor(cell_feats, dtype=torch.float32)
        if cell_feats
        else torch.zeros((0, 9), dtype=torch.float32),
        net_x=torch.tensor(net_feats, dtype=torch.float32)
        if net_feats
        else torch.zeros((0, len(EDGE_NUMERIC_COLS)), dtype=torch.float32),
        global_x=torch.tensor(global_vals, dtype=torch.float32),
        cell_type_tokens=cell_type_tokens,
        cell_type_idx=None,
        edge_rel_local=edge_rel_local,
        y_sec=torch.tensor(y_sec, dtype=torch.float32),
        y_norm=torch.zeros((num_pins, len(target_cols)), dtype=torch.float32),
        finite_idx=torch.tensor(finite_idx, dtype=torch.long),
        wns_sec=_parse_wns_sec(global_json),
        path_pairs_pin=path_pairs_t,
        path_targets_sec=path_tgts_sec,
        path_targets_norm=torch.zeros_like(path_tgts_sec),
    )


def build_cell_type_vocab(train_graphs: Sequence[TripGraph], min_count: int = 1) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for g in train_graphs:
        for tok in g.cell_type_tokens:
            counts[tok] = counts.get(tok, 0) + 1
    vocab: Dict[str, int] = {"__UNK__": 0}
    for tok in sorted(counts.keys()):
        if counts[tok] >= min_count:
            vocab[tok] = len(vocab)
    return vocab


def apply_cell_type_encoding(graphs: Sequence[TripGraph], vocab: Dict[str, int]) -> None:
    unk = int(vocab.get("__UNK__", 0))
    for g in graphs:
        ids = [int(vocab.get(tok, unk)) for tok in g.cell_type_tokens]
        g.cell_type_idx = torch.tensor(ids, dtype=torch.long)


def compute_norm_stats(train_graphs: Sequence[TripGraph], target_scale: float) -> TripNormStats:
    pin_all = torch.cat([g.pin_x for g in train_graphs], dim=0)
    pin_mean = pin_all.mean(dim=0)
    pin_std = pin_all.std(dim=0, unbiased=False)
    pin_std = torch.where(pin_std < 1e-12, torch.ones_like(pin_std), pin_std)

    cell_rows = [g.cell_x for g in train_graphs if int(g.cell_x.size(0)) > 0]
    if cell_rows:
        cell_all = torch.cat(cell_rows, dim=0)
        cell_mean = cell_all.mean(dim=0)
        cell_std = cell_all.std(dim=0, unbiased=False)
        cell_std = torch.where(cell_std < 1e-12, torch.ones_like(cell_std), cell_std)
    else:
        cell_mean = torch.zeros((9,), dtype=torch.float32)
        cell_std = torch.ones((9,), dtype=torch.float32)

    net_rows = [g.net_x for g in train_graphs if int(g.net_x.size(0)) > 0]
    if net_rows:
        net_all = torch.cat(net_rows, dim=0)
        net_mean = net_all.mean(dim=0)
        net_std = net_all.std(dim=0, unbiased=False)
        net_std = torch.where(net_std < 1e-12, torch.ones_like(net_std), net_std)
    else:
        net_mean = torch.zeros((len(EDGE_NUMERIC_COLS),), dtype=torch.float32)
        net_std = torch.ones((len(EDGE_NUMERIC_COLS),), dtype=torch.float32)

    g_all = torch.stack([g.global_x for g in train_graphs], dim=0)
    g_mean = g_all.mean(dim=0)
    g_std = g_all.std(dim=0, unbiased=False)
    g_std = torch.where(g_std < 1e-12, torch.ones_like(g_std), g_std)

    y_rows = [g.y_sec[g.finite_idx] * float(target_scale) for g in train_graphs]
    y_all = torch.cat(y_rows, dim=0)
    t_mean = y_all.mean(dim=0)
    t_std = y_all.std(dim=0, unbiased=False)
    t_std = torch.where(t_std < 1e-12, torch.ones_like(t_std), t_std)

    return TripNormStats(
        pin_mean=pin_mean.tolist(),
        pin_std=pin_std.tolist(),
        cell_mean=cell_mean.tolist(),
        cell_std=cell_std.tolist(),
        net_mean=net_mean.tolist(),
        net_std=net_std.tolist(),
        global_mean=g_mean.tolist(),
        global_std=g_std.tolist(),
        target_scale=float(target_scale),
        target_mean=t_mean.tolist(),
        target_std=t_std.tolist(),
    )


def apply_norm(
    graphs: Sequence[TripGraph],
    stats: TripNormStats,
    arrival_idx: int,
    slack_idx: int,
) -> None:
    pin_mean = torch.tensor(stats.pin_mean, dtype=torch.float32)
    pin_std = torch.tensor(stats.pin_std, dtype=torch.float32)
    cell_mean = torch.tensor(stats.cell_mean, dtype=torch.float32)
    cell_std = torch.tensor(stats.cell_std, dtype=torch.float32)
    net_mean = torch.tensor(stats.net_mean, dtype=torch.float32)
    net_std = torch.tensor(stats.net_std, dtype=torch.float32)
    g_mean = torch.tensor(stats.global_mean, dtype=torch.float32)
    g_std = torch.tensor(stats.global_std, dtype=torch.float32)

    t_scale = float(stats.target_scale)
    t_mean = torch.tensor(stats.target_mean, dtype=torch.float32)
    t_std = torch.tensor(stats.target_std, dtype=torch.float32)

    for g in graphs:
        g.pin_x = (g.pin_x - pin_mean) / pin_std
        if int(g.cell_x.size(0)) > 0:
            g.cell_x = (g.cell_x - cell_mean) / cell_std
        if int(g.net_x.size(0)) > 0:
            g.net_x = (g.net_x - net_mean) / net_std
        g.global_x = (g.global_x - g_mean) / g_std

        g.y_norm = (g.y_sec * t_scale - t_mean) / t_std

        if int(g.path_targets_sec.size(0)) > 0:
            arr = g.path_targets_sec[:, 0] * t_scale
            slk = g.path_targets_sec[:, 1] * t_scale
            arr_n = (arr - t_mean[arrival_idx]) / t_std[arrival_idx]
            slk_n = (slk - t_mean[slack_idx]) / t_std[slack_idx]
            g.path_targets_norm = torch.stack([arr_n, slk_n], dim=1)
        else:
            g.path_targets_norm = torch.zeros((0, 2), dtype=torch.float32)


def _sample_idx(idx: "torch.Tensor", max_count: int, rng: random.Random) -> "torch.Tensor":
    if max_count <= 0 or int(idx.numel()) <= max_count:
        return idx
    data = idx.tolist()
    sampled = rng.sample(data, max_count)
    sampled.sort()
    return torch.tensor(sampled, dtype=torch.long)


def make_batch(graphs: Sequence[TripGraph], loss_nodes_per_graph: int, rng: random.Random) -> TripBatch:
    total_pins = sum(g.num_pins for g in graphs)
    total_cells = sum(g.num_cells for g in graphs)
    total_nets = sum(g.num_nets for g in graphs)

    pin_x = torch.cat([g.pin_x for g in graphs], dim=0)
    if total_cells > 0:
        cell_x = torch.cat([g.cell_x for g in graphs], dim=0)
    else:
        cell_x = torch.zeros((0, 9), dtype=torch.float32)
    if total_nets > 0:
        net_x = torch.cat([g.net_x for g in graphs], dim=0)
    else:
        net_x = torch.zeros((0, len(EDGE_NUMERIC_COLS)), dtype=torch.float32)

    global_x = torch.stack([g.global_x for g in graphs], dim=0)

    y_sec = torch.cat([g.y_sec for g in graphs], dim=0)
    y_norm = torch.cat([g.y_norm for g in graphs], dim=0)

    cell_type_idx = None
    if any(g.cell_type_idx is not None for g in graphs):
        parts = []
        for g in graphs:
            if g.cell_type_idx is None:
                raise RuntimeError(f"{g.run_id}: missing cell_type_idx")
            parts.append(g.cell_type_idx)
        cell_type_idx = torch.cat(parts, dim=0)

    rel_src_parts: Dict[str, List[torch.Tensor]] = {r: [] for r in RELATIONS}
    rel_dst_parts: Dict[str, List[torch.Tensor]] = {r: [] for r in RELATIONS}
    loss_parts: List[torch.Tensor] = []

    path_pair_parts: List[torch.Tensor] = []
    path_tgt_sec_parts: List[torch.Tensor] = []
    path_tgt_norm_parts: List[torch.Tensor] = []

    graph_pin_ptr = [0]
    graph_wns_sec: List[Optional[float]] = []
    graph_run_ids: List[str] = []

    node_graph_idx_parts: List[torch.Tensor] = []

    pin_off = 0
    cell_off = 0
    net_off = 0

    for gidx, g in enumerate(graphs):
        p = g.num_pins
        c = g.num_cells
        n = g.num_nets

        sampled = _sample_idx(g.finite_idx, loss_nodes_per_graph, rng) + pin_off
        loss_parts.append(sampled)

        # node_graph_idx aligns with concatenated all-node tensor ordering:
        # [all pins][all cells][all nets]
        node_graph_idx_parts.append(torch.full((p,), gidx, dtype=torch.long))

        for rel in RELATIONS:
            e = g.edge_rel_local[rel]
            if int(e.size(1)) == 0:
                continue
            src = e[0].clone()
            dst = e[1].clone()

            def _map(idx_t: "torch.Tensor") -> "torch.Tensor":
                pin_mask = idx_t < p
                cell_mask = (idx_t >= p) & (idx_t < (p + c))
                net_mask = idx_t >= (p + c)

                out = torch.empty_like(idx_t)
                out[pin_mask] = idx_t[pin_mask] + pin_off
                out[cell_mask] = (idx_t[cell_mask] - p) + (total_pins + cell_off)
                out[net_mask] = (idx_t[net_mask] - (p + c)) + (total_pins + total_cells + net_off)
                return out

            src_g = _map(src)
            dst_g = _map(dst)
            rel_src_parts[rel].append(src_g)
            rel_dst_parts[rel].append(dst_g)

        if int(g.path_pairs_pin.size(0)) > 0:
            path_pair_parts.append(g.path_pairs_pin + pin_off)
            path_tgt_sec_parts.append(g.path_targets_sec)
            path_tgt_norm_parts.append(g.path_targets_norm)

        pin_off += p
        cell_off += c
        net_off += n

        graph_pin_ptr.append(pin_off)
        graph_wns_sec.append(g.wns_sec)
        graph_run_ids.append(g.run_id)

    # append graph idx for cell and net regions after all pins.
    for gidx, g in enumerate(graphs):
        if g.num_cells > 0:
            node_graph_idx_parts.append(torch.full((g.num_cells,), gidx, dtype=torch.long))
    for gidx, g in enumerate(graphs):
        if g.num_nets > 0:
            node_graph_idx_parts.append(torch.full((g.num_nets,), gidx, dtype=torch.long))

    edge_rel_global: Dict[str, torch.Tensor] = {}
    for rel in RELATIONS:
        if rel_src_parts[rel]:
            edge_rel_global[rel] = torch.stack(
                [torch.cat(rel_src_parts[rel], dim=0), torch.cat(rel_dst_parts[rel], dim=0)],
                dim=0,
            )
        else:
            edge_rel_global[rel] = torch.zeros((2, 0), dtype=torch.long)

    if path_pair_parts:
        path_pairs_global = torch.cat(path_pair_parts, dim=0)
        path_targets_sec = torch.cat(path_tgt_sec_parts, dim=0)
        path_targets_norm = torch.cat(path_tgt_norm_parts, dim=0)
    else:
        path_pairs_global = torch.zeros((0, 2), dtype=torch.long)
        path_targets_sec = torch.zeros((0, 2), dtype=torch.float32)
        path_targets_norm = torch.zeros((0, 2), dtype=torch.float32)

    return TripBatch(
        pin_x=pin_x,
        cell_x=cell_x,
        net_x=net_x,
        global_x=global_x,
        cell_type_idx=cell_type_idx,
        edge_rel_global=edge_rel_global,
        node_graph_idx=torch.cat(node_graph_idx_parts, dim=0),
        y_sec=y_sec,
        y_norm=y_norm,
        loss_idx=torch.cat(loss_parts, dim=0),
        path_pairs_global=path_pairs_global,
        path_targets_sec=path_targets_sec,
        path_targets_norm=path_targets_norm,
        graph_pin_ptr=graph_pin_ptr,
        graph_wns_sec=graph_wns_sec,
        graph_run_ids=graph_run_ids,
    )


def iter_batches(
    graphs: Sequence[TripGraph],
    batch_graphs: int,
    loss_nodes_per_graph: int,
    shuffle: bool,
    rng: random.Random,
) -> Iterable[TripBatch]:
    if batch_graphs <= 0:
        batch_graphs = 1
    order = list(range(len(graphs)))
    if shuffle:
        rng.shuffle(order)
    for i in range(0, len(order), batch_graphs):
        chunk = [graphs[j] for j in order[i : i + batch_graphs]]
        yield make_batch(chunk, loss_nodes_per_graph, rng)


def denorm_to_sec(pred_norm: "torch.Tensor", stats: TripNormStats) -> "torch.Tensor":
    t_mean = torch.tensor(stats.target_mean, dtype=torch.float32, device=pred_norm.device)
    t_std = torch.tensor(stats.target_std, dtype=torch.float32, device=pred_norm.device)
    return (pred_norm * t_std + t_mean) / float(stats.target_scale)


class TripathDualPassNet(nn.Module):
    def __init__(
        self,
        pin_dim: int,
        cell_dim: int,
        net_dim: int,
        global_dim: int,
        hidden_dim: int = 192,
        message_steps: int = 5,
        dropout: float = 0.1,
        out_dim: int = 3,
        cell_type_vocab_size: int = 0,
        cell_emb_dim: int = 0,
    ) -> None:
        super().__init__()
        self.hidden_dim = hidden_dim
        self.message_steps = message_steps
        self.out_dim = out_dim
        self.cell_emb_dim = max(0, int(cell_emb_dim))
        self.cell_type_vocab_size = max(0, int(cell_type_vocab_size))

        self.cell_embedding = None
        if self.cell_emb_dim > 0 and self.cell_type_vocab_size > 0:
            self.cell_embedding = nn.Embedding(self.cell_type_vocab_size, self.cell_emb_dim)

        pin_in = pin_dim + (self.cell_emb_dim if self.cell_embedding is not None else 0)
        self.pin_encoder = nn.Sequential(
            nn.Linear(pin_in, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
        )
        self.cell_encoder = nn.Sequential(
            nn.Linear(cell_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
        )
        self.net_encoder = nn.Sequential(
            nn.Linear(net_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
        )

        self.global_encoder = nn.Sequential(
            nn.Linear(global_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
        )
        self.film_f = nn.Linear(hidden_dim, hidden_dim * 2)
        self.film_b = nn.Linear(hidden_dim, hidden_dim * 2)

        self.msg_f = nn.ModuleDict()
        self.msg_b = nn.ModuleDict()
        for rel in RELATIONS:
            self.msg_f[rel] = nn.Sequential(
                nn.Linear(hidden_dim * 2, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
            )
            self.msg_b[rel] = nn.Sequential(
                nn.Linear(hidden_dim * 2, hidden_dim),
                nn.ReLU(),
                nn.Linear(hidden_dim, hidden_dim),
            )

        self.upd_f = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )
        self.upd_b = nn.Sequential(
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )
        self.norm_f = nn.LayerNorm(hidden_dim)
        self.norm_b = nn.LayerNorm(hidden_dim)
        self.dropout = nn.Dropout(dropout)

        self.arr_head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1),
        )
        self.req_head = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1),
        )
        self.slk_head = nn.Sequential(
            nn.Linear(hidden_dim * 4, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1),
        )

        self.path_head = nn.Sequential(
            nn.Linear(hidden_dim * 10, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 2),  # [arrival_norm, slack_norm]
        )

    def forward(
        self,
        pin_x: "torch.Tensor",
        cell_x: "torch.Tensor",
        net_x: "torch.Tensor",
        global_x: "torch.Tensor",
        edge_rel: Dict[str, "torch.Tensor"],
        node_graph_idx: "torch.Tensor",
        cell_type_idx: Optional["torch.Tensor"] = None,
        path_pairs_global: Optional["torch.Tensor"] = None,
    ) -> Tuple["torch.Tensor", Optional["torch.Tensor"]]:
        if self.cell_embedding is not None:
            if cell_type_idx is None:
                raise RuntimeError("cell_type_idx required when cell embedding enabled")
            pin_x = torch.cat([pin_x, self.cell_embedding(cell_type_idx)], dim=1)

        pin_h = self.pin_encoder(pin_x)
        if int(cell_x.size(0)) > 0:
            cell_h = self.cell_encoder(cell_x)
        else:
            cell_h = torch.zeros((0, self.hidden_dim), dtype=pin_h.dtype, device=pin_h.device)
        if int(net_x.size(0)) > 0:
            net_h = self.net_encoder(net_x)
        else:
            net_h = torch.zeros((0, self.hidden_dim), dtype=pin_h.dtype, device=pin_h.device)

        h0 = torch.cat([pin_h, cell_h, net_h], dim=0)
        h_f = h0
        h_b = h0

        g = self.global_encoder(global_x)
        g_node = g[node_graph_idx]

        num_nodes = int(h0.size(0))
        for _ in range(self.message_steps):
            agg_f = torch.zeros_like(h_f)
            agg_b = torch.zeros_like(h_b)
            deg_f = torch.zeros((num_nodes,), dtype=h_f.dtype, device=h_f.device)
            deg_b = torch.zeros((num_nodes,), dtype=h_b.dtype, device=h_b.device)

            for rel in RELATIONS:
                e = edge_rel[rel]
                if int(e.size(1)) == 0:
                    continue
                src = e[0]
                dst = e[1]

                msg_in_f = torch.cat([h_f[src], h_b[src]], dim=1)
                msg_f = self.msg_f[rel](msg_in_f)
                agg_f.index_add_(0, dst, msg_f)
                deg_f.index_add_(0, dst, torch.ones((dst.size(0),), dtype=h_f.dtype, device=h_f.device))

                src_b = dst
                dst_b = src
                msg_in_b = torch.cat([h_b[src_b], h_f[src_b]], dim=1)
                msg_b = self.msg_b[rel](msg_in_b)
                agg_b.index_add_(0, dst_b, msg_b)
                deg_b.index_add_(0, dst_b, torch.ones((dst_b.size(0),), dtype=h_b.dtype, device=h_b.device))

            agg_f = agg_f / deg_f.clamp(min=1.0).unsqueeze(1)
            agg_b = agg_b / deg_b.clamp(min=1.0).unsqueeze(1)

            upd_f = self.upd_f(torch.cat([h_f, agg_f], dim=1))
            upd_b = self.upd_b(torch.cat([h_b, agg_b], dim=1))

            h_f = self.norm_f(h_f + self.dropout(torch.relu(upd_f)))
            h_b = self.norm_b(h_b + self.dropout(torch.relu(upd_b)))

            # Global-context FiLM conditioning per node (light strength for stability).
            gf, bf = self.film_f(g_node).chunk(2, dim=1)
            gb, bb = self.film_b(g_node).chunk(2, dim=1)
            h_f = h_f * (1.0 + 0.1 * torch.tanh(gf)) + 0.1 * bf
            h_b = h_b * (1.0 + 0.1 * torch.tanh(gb)) + 0.1 * bb

        num_pins = int(pin_x.size(0))
        pin_f = h_f[:num_pins]
        pin_b = h_b[:num_pins]
        g_pin = g_node[:num_pins]

        arr = self.arr_head(pin_f).squeeze(1)
        req = self.req_head(pin_b).squeeze(1)
        slk = self.slk_head(torch.cat([pin_f, pin_b, pin_f - pin_b, g_pin], dim=1)).squeeze(1)
        node_pred = torch.stack([arr, slk, req], dim=1)

        path_pred = None
        if path_pairs_global is not None and int(path_pairs_global.size(0)) > 0:
            s = path_pairs_global[:, 0]
            e = path_pairs_global[:, 1]
            pf_s = pin_f[s]
            pf_e = pin_f[e]
            pb_s = pin_b[s]
            pb_e = pin_b[e]
            pg_s = g_pin[s]
            pg_e = g_pin[e]
            p_in = torch.cat(
                [
                    pf_s,
                    pf_e,
                    pb_s,
                    pb_e,
                    pf_e - pf_s,
                    pb_e - pb_s,
                    pg_s,
                    pg_e,
                    pf_e * pf_s,
                    pb_e * pb_s,
                ],
                dim=1,
            )
            path_pred = self.path_head(p_in)

        return node_pred, path_pred


def evaluate_model(
    model: TripathDualPassNet,
    graphs: Sequence[TripGraph],
    stats: TripNormStats,
    device: "torch.device",
    batch_graphs: int,
    loss_nodes_per_graph: int,
    primary_idx: int,
    arrival_idx: int,
    slack_idx: int,
    seed: int,
) -> Dict[str, float]:
    model.eval()
    rng = random.Random(seed)

    t = len(stats.target_mean)
    sq_norm = [0.0] * t
    sq_sec = [0.0] * t
    abs_sec = [0.0] * t
    total_nodes = 0

    path_sq_slack_sec = 0.0
    path_abs_slack_sec = 0.0
    path_count = 0

    wns_diffs_ps: List[float] = []

    with torch.no_grad():
        for batch in iter_batches(
            graphs=graphs,
            batch_graphs=batch_graphs,
            loss_nodes_per_graph=loss_nodes_per_graph,
            shuffle=False,
            rng=rng,
        ):
            pin_x = batch.pin_x.to(device)
            cell_x = batch.cell_x.to(device)
            net_x = batch.net_x.to(device)
            global_x = batch.global_x.to(device)
            rel = {k: v.to(device) for k, v in batch.edge_rel_global.items()}
            node_graph_idx = batch.node_graph_idx.to(device)
            cidx = batch.cell_type_idx.to(device) if batch.cell_type_idx is not None else None
            y_norm = batch.y_norm.to(device)
            y_sec = batch.y_sec.to(device)
            loss_idx = batch.loss_idx.to(device)

            path_pairs = batch.path_pairs_global.to(device)
            path_sec = batch.path_targets_sec.to(device)

            pred_norm, path_pred_norm = model(
                pin_x=pin_x,
                cell_x=cell_x,
                net_x=net_x,
                global_x=global_x,
                edge_rel=rel,
                node_graph_idx=node_graph_idx,
                cell_type_idx=cidx,
                path_pairs_global=path_pairs,
            )

            pred_sec = denorm_to_sec(pred_norm, stats)
            diff_norm = pred_norm[loss_idx] - y_norm[loss_idx]
            diff_sec = pred_sec[loss_idx] - y_sec[loss_idx]

            for j in range(t):
                dn = diff_norm[:, j]
                ds = diff_sec[:, j]
                sq_norm[j] += float((dn * dn).sum().item())
                sq_sec[j] += float((ds * ds).sum().item())
                abs_sec[j] += float(ds.abs().sum().item())
            total_nodes += int(loss_idx.numel())

            if path_pred_norm is not None and int(path_pred_norm.size(0)) > 0:
                t_mean = torch.tensor(stats.target_mean, dtype=torch.float32, device=device)
                t_std = torch.tensor(stats.target_std, dtype=torch.float32, device=device)
                scale = float(stats.target_scale)

                path_arr_sec = (path_pred_norm[:, 0] * t_std[arrival_idx] + t_mean[arrival_idx]) / scale
                path_slk_sec = (path_pred_norm[:, 1] * t_std[slack_idx] + t_mean[slack_idx]) / scale

                d_slk = path_slk_sec - path_sec[:, 1]
                path_sq_slack_sec += float((d_slk * d_slk).sum().item())
                path_abs_slack_sec += float(d_slk.abs().sum().item())
                path_count += int(d_slk.numel())

            for gidx, wns_sec in enumerate(batch.graph_wns_sec):
                if wns_sec is None:
                    continue
                s = batch.graph_pin_ptr[gidx]
                e = batch.graph_pin_ptr[gidx + 1]
                local_loss = loss_idx[(loss_idx >= s) & (loss_idx < e)] - s
                if int(local_loss.numel()) == 0:
                    continue
                pred_wns = float(pred_sec[s:e, primary_idx][local_loss].min().item())
                wns_diffs_ps.append(abs(pred_wns - float(wns_sec)) * 1e12)

    if total_nodes == 0:
        return {
            "norm_mse": 0.0,
            "rmse_ps": 0.0,
            "mae_ps": 0.0,
            "wns_mae_ps": 0.0,
            "wns_rmse_ps": 0.0,
            "norm_mse_avg": 0.0,
            "path_slack_rmse_ps": 0.0,
            "path_slack_mae_ps": 0.0,
        }

    norm_mse_all = [v / total_nodes for v in sq_norm]
    rmse_ps_all = [math.sqrt(v / total_nodes) * 1e12 for v in sq_sec]
    mae_ps_all = [(v / total_nodes) * 1e12 for v in abs_sec]

    if wns_diffs_ps:
        wns_mae_ps = sum(wns_diffs_ps) / len(wns_diffs_ps)
        wns_rmse_ps = math.sqrt(sum(v * v for v in wns_diffs_ps) / len(wns_diffs_ps))
    else:
        wns_mae_ps = 0.0
        wns_rmse_ps = 0.0

    if path_count > 0:
        path_slack_rmse_ps = math.sqrt(path_sq_slack_sec / path_count) * 1e12
        path_slack_mae_ps = (path_abs_slack_sec / path_count) * 1e12
    else:
        path_slack_rmse_ps = 0.0
        path_slack_mae_ps = 0.0

    out = {
        "norm_mse": norm_mse_all[primary_idx],
        "rmse_ps": rmse_ps_all[primary_idx],
        "mae_ps": mae_ps_all[primary_idx],
        "wns_mae_ps": wns_mae_ps,
        "wns_rmse_ps": wns_rmse_ps,
        "norm_mse_avg": sum(norm_mse_all) / len(norm_mse_all),
        "path_slack_rmse_ps": path_slack_rmse_ps,
        "path_slack_mae_ps": path_slack_mae_ps,
    }
    for j in range(t):
        out[f"norm_mse_t{j}"] = norm_mse_all[j]
        out[f"rmse_ps_t{j}"] = rmse_ps_all[j]
        out[f"mae_ps_t{j}"] = mae_ps_all[j]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Train tripartite dual-pass timing model")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--splits", default="data/manifests/splits.json")
    ap.add_argument("--eval-mode", choices=["within_design", "holdout_design"], default="holdout_design")
    ap.add_argument("--design", default="gcd")
    ap.add_argument("--train-design", default="gcd")
    ap.add_argument("--eval-design", default="aes")

    ap.add_argument(
        "--target-cols",
        default="arrival_setup_scalar_s,slack_setup_scalar_s,required_setup_scalar_s",
    )
    ap.add_argument("--primary-target-col", default="slack_setup_scalar_s")
    ap.add_argument("--arrival-col", default="arrival_setup_scalar_s")
    ap.add_argument("--slack-col", default="slack_setup_scalar_s")
    ap.add_argument("--required-col", default="required_setup_scalar_s")
    ap.add_argument("--target-weights", default="0.7,1.0,0.7")
    ap.add_argument("--target-scale", type=float, default=1e12)

    ap.add_argument("--max-train-runs", type=int, default=0)
    ap.add_argument("--max-val-runs", type=int, default=0)
    ap.add_argument("--max-test-runs", type=int, default=0)
    ap.add_argument("--batch-graphs", type=int, default=2)
    ap.add_argument("--loss-nodes-per-graph-train", type=int, default=2048)
    ap.add_argument("--loss-nodes-per-graph-eval", type=int, default=0)
    ap.add_argument("--max-paths-per-graph", type=int, default=64)

    ap.add_argument("--hidden-dim", type=int, default=192)
    ap.add_argument("--message-steps", type=int, default=5)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--cell-emb-dim", type=int, default=24)
    ap.add_argument("--cell-vocab-min-count", type=int, default=1)
    ap.add_argument("--lr", type=float, default=8e-4)
    ap.add_argument("--weight-decay", type=float, default=1e-5)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--early-stop-patience", type=int, default=10)

    ap.add_argument("--consistency-weight", type=float, default=5e-4)
    ap.add_argument("--rank-loss-weight", type=float, default=0.03)
    ap.add_argument("--rank-pairs", type=int, default=1024)
    ap.add_argument("--rank-margin-norm", type=float, default=0.05)
    ap.add_argument("--critical-loss-weight", type=float, default=1.0)
    ap.add_argument("--critical-threshold-ps", type=float, default=0.0)
    ap.add_argument("--path-aux-weight", type=float, default=0.2)

    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")

    ap.add_argument("--out-dir", default="results/train_runs")
    ap.add_argument("--run-name", default="")
    args = ap.parse_args()

    require_torch()
    if torch is None or nn is None:
        raise RuntimeError("PyTorch is unavailable in this environment.")

    target_cols = _parse_csv_list(args.target_cols)
    if not target_cols:
        raise SystemExit("No target columns parsed from --target-cols")
    target_weights = _parse_float_list(args.target_weights, len(target_cols))
    primary_idx = _target_idx(target_cols, args.primary_target_col)
    arrival_idx = _target_idx(target_cols, args.arrival_col)
    slack_idx = _target_idx(target_cols, args.slack_col)
    required_idx = _target_idx(target_cols, args.required_col)

    _set_seed(args.seed)
    device = _resolve_device(args.device)

    dataset_index = load_dataset_index(Path(args.dataset_index))
    splits = load_splits(Path(args.splits))
    train_rows, val_rows, test_rows, eval_tag = select_rows(
        dataset_index=dataset_index,
        splits=splits,
        eval_mode=args.eval_mode,
        design=args.design,
        train_design=args.train_design,
        eval_design=args.eval_design,
        max_train_runs=args.max_train_runs,
        max_val_runs=args.max_val_runs,
        max_test_runs=args.max_test_runs,
    )
    if not train_rows or not val_rows or not test_rows:
        raise SystemExit("Row selection returned empty split(s).")

    print(f"eval={eval_tag}")
    print(
        f"runs train={len(train_rows)} val={len(val_rows)} test={len(test_rows)} "
        f"targets={','.join(target_cols)} primary={args.primary_target_col}"
    )

    train_graphs = [load_trip_graph(r, target_cols, max_paths=args.max_paths_per_graph) for r in train_rows]
    val_graphs = [load_trip_graph(r, target_cols, max_paths=args.max_paths_per_graph) for r in val_rows]
    test_graphs = [load_trip_graph(r, target_cols, max_paths=args.max_paths_per_graph) for r in test_rows]

    cell_vocab: Dict[str, int] = {}
    if args.cell_emb_dim > 0:
        cell_vocab = build_cell_type_vocab(train_graphs, min_count=args.cell_vocab_min_count)
        apply_cell_type_encoding(train_graphs, cell_vocab)
        apply_cell_type_encoding(val_graphs, cell_vocab)
        apply_cell_type_encoding(test_graphs, cell_vocab)
        print(f"cell_embedding enabled dim={args.cell_emb_dim} vocab={len(cell_vocab)}")
    else:
        print("cell_embedding disabled")

    stats = compute_norm_stats(train_graphs, args.target_scale)
    apply_norm(train_graphs, stats, arrival_idx=arrival_idx, slack_idx=slack_idx)
    apply_norm(val_graphs, stats, arrival_idx=arrival_idx, slack_idx=slack_idx)
    apply_norm(test_graphs, stats, arrival_idx=arrival_idx, slack_idx=slack_idx)

    model = TripathDualPassNet(
        pin_dim=int(train_graphs[0].pin_x.size(1)),
        cell_dim=int(train_graphs[0].cell_x.size(1)) if int(train_graphs[0].cell_x.size(0)) > 0 else 9,
        net_dim=int(train_graphs[0].net_x.size(1)) if int(train_graphs[0].net_x.size(0)) > 0 else len(EDGE_NUMERIC_COLS),
        global_dim=int(train_graphs[0].global_x.numel()),
        hidden_dim=args.hidden_dim,
        message_steps=args.message_steps,
        dropout=args.dropout,
        out_dim=len(target_cols),
        cell_type_vocab_size=len(cell_vocab),
        cell_emb_dim=(args.cell_emb_dim if args.cell_emb_dim > 0 else 0),
    ).to(device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    target_w_t = torch.tensor(target_weights, dtype=torch.float32, device=device)

    run_name = args.run_name.strip() or f"timing_tripath_{_utc_stamp()}"
    run_dir = Path(args.out_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    config_payload = {
        "run_name": run_name,
        "eval_tag": eval_tag,
        "args": vars(args),
        "target_cols": target_cols,
        "target_weights": target_weights,
        "primary_target_idx": primary_idx,
        "arrival_idx": arrival_idx,
        "slack_idx": slack_idx,
        "required_idx": required_idx,
        "train_ids": [r["run_id"] for r in train_rows],
        "val_ids": [r["run_id"] for r in val_rows],
        "test_ids": [r["run_id"] for r in test_rows],
        "model": {
            "pin_dim": int(train_graphs[0].pin_x.size(1)),
            "cell_dim": int(train_graphs[0].cell_x.size(1)) if int(train_graphs[0].cell_x.size(0)) > 0 else 9,
            "net_dim": int(train_graphs[0].net_x.size(1)) if int(train_graphs[0].net_x.size(0)) > 0 else len(EDGE_NUMERIC_COLS),
            "global_dim": int(train_graphs[0].global_x.numel()),
            "hidden_dim": args.hidden_dim,
            "message_steps": args.message_steps,
            "dropout": args.dropout,
            "out_dim": len(target_cols),
            "cell_emb_dim": (args.cell_emb_dim if args.cell_emb_dim > 0 else 0),
            "cell_type_vocab_size": len(cell_vocab),
        },
        "cell_type_vocab": cell_vocab,
        "normalization": stats.to_dict(),
    }
    _write_json(run_dir / "config.json", config_payload)

    best_val = float("inf")
    best_epoch = 0
    bad_epochs = 0
    rows: List[Dict[str, object]] = []
    train_rng = random.Random(args.seed + 101)

    for epoch in range(1, args.epochs + 1):
        model.train()
        total_loss = 0.0
        total_nodes = 0

        for batch in iter_batches(
            graphs=train_graphs,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_train,
            shuffle=True,
            rng=train_rng,
        ):
            pin_x = batch.pin_x.to(device)
            cell_x = batch.cell_x.to(device)
            net_x = batch.net_x.to(device)
            global_x = batch.global_x.to(device)
            rel = {k: v.to(device) for k, v in batch.edge_rel_global.items()}
            node_graph_idx = batch.node_graph_idx.to(device)
            cidx = batch.cell_type_idx.to(device) if batch.cell_type_idx is not None else None
            y_norm = batch.y_norm.to(device)
            y_sec = batch.y_sec.to(device)
            loss_idx = batch.loss_idx.to(device)
            path_pairs = batch.path_pairs_global.to(device)
            path_targets_norm = batch.path_targets_norm.to(device)

            optimizer.zero_grad(set_to_none=True)
            pred_norm, path_pred_norm = model(
                pin_x=pin_x,
                cell_x=cell_x,
                net_x=net_x,
                global_x=global_x,
                edge_rel=rel,
                node_graph_idx=node_graph_idx,
                cell_type_idx=cidx,
                path_pairs_global=path_pairs,
            )

            pred_loss = pred_norm[loss_idx]
            y_loss = y_norm[loss_idx]
            diff = pred_loss - y_loss

            t_losses = []
            for j in range(len(target_cols)):
                if j == primary_idx and args.critical_loss_weight > 1.0:
                    thr_sec = float(args.critical_threshold_ps) * 1e-12
                    crit = (y_sec[loss_idx, primary_idx] <= thr_sec).float()
                    weights = 1.0 + (float(args.critical_loss_weight) - 1.0) * crit
                    sq = diff[:, j].pow(2)
                    tl = (weights * sq).sum() / weights.sum().clamp(min=1.0)
                else:
                    tl = diff[:, j].pow(2).mean()
                t_losses.append(tl)
            t_loss_vec = torch.stack(t_losses, dim=0)
            loss = (t_loss_vec * target_w_t).sum() / target_w_t.sum().clamp(min=1e-12)

            if args.consistency_weight > 0.0:
                pred_sec = denorm_to_sec(pred_norm, stats)
                cons = pred_sec[:, required_idx] - (pred_sec[:, arrival_idx] + pred_sec[:, slack_idx])
                cons_ps = cons[loss_idx] * 1e12
                loss = loss + float(args.consistency_weight) * cons_ps.pow(2).mean()

            if args.rank_loss_weight > 0.0:
                rloss = _pairwise_rank_loss(
                    pred=pred_loss[:, primary_idx],
                    truth=y_loss[:, primary_idx],
                    pairs=int(args.rank_pairs),
                    margin=float(args.rank_margin_norm),
                )
                loss = loss + float(args.rank_loss_weight) * rloss

            if path_pred_norm is not None and int(path_pred_norm.size(0)) > 0 and float(args.path_aux_weight) > 0.0:
                path_loss = (path_pred_norm - path_targets_norm).pow(2).mean()
                loss = loss + float(args.path_aux_weight) * path_loss

            loss.backward()
            optimizer.step()

            n = int(loss_idx.numel())
            total_loss += float(loss.item()) * n
            total_nodes += n

        train_loss = total_loss / max(1, total_nodes)

        train_metrics = evaluate_model(
            model=model,
            graphs=train_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
            primary_idx=primary_idx,
            arrival_idx=arrival_idx,
            slack_idx=slack_idx,
            seed=args.seed + epoch * 11 + 1,
        )
        val_metrics = evaluate_model(
            model=model,
            graphs=val_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
            primary_idx=primary_idx,
            arrival_idx=arrival_idx,
            slack_idx=slack_idx,
            seed=args.seed + epoch * 11 + 3,
        )
        test_metrics = evaluate_model(
            model=model,
            graphs=test_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
            primary_idx=primary_idx,
            arrival_idx=arrival_idx,
            slack_idx=slack_idx,
            seed=args.seed + epoch * 11 + 5,
        )

        row = {
            "epoch": epoch,
            "train_loss": train_loss,
            "train_norm_mse": train_metrics["norm_mse"],
            "train_rmse_ps": train_metrics["rmse_ps"],
            "val_norm_mse": val_metrics["norm_mse"],
            "val_rmse_ps": val_metrics["rmse_ps"],
            "test_norm_mse": test_metrics["norm_mse"],
            "test_rmse_ps": test_metrics["rmse_ps"],
            "val_wns_mae_ps": val_metrics["wns_mae_ps"],
            "test_wns_mae_ps": test_metrics["wns_mae_ps"],
            "val_path_slack_rmse_ps": val_metrics.get("path_slack_rmse_ps", 0.0),
            "test_path_slack_rmse_ps": test_metrics.get("path_slack_rmse_ps", 0.0),
        }
        rows.append(row)

        print(
            f"epoch={epoch:03d} train_loss={train_loss:.6e} "
            f"val_norm_mse={val_metrics['norm_mse']:.6e} "
            f"val_rmse_ps={val_metrics['rmse_ps']:.3f} "
            f"test_rmse_ps={test_metrics['rmse_ps']:.3f} "
            f"test_path_slack_rmse_ps={test_metrics.get('path_slack_rmse_ps', 0.0):.3f}"
        )

        ckpt = {
            "epoch": epoch,
            "model_state": model.state_dict(),
            "model_cfg": config_payload["model"],
            "cell_type_vocab": cell_vocab,
            "normalization": stats.to_dict(),
            "target_cols": target_cols,
            "target_weights": target_weights,
            "primary_target_idx": primary_idx,
            "arrival_idx": arrival_idx,
            "slack_idx": slack_idx,
            "required_idx": required_idx,
            "args": vars(args),
            "eval_tag": eval_tag,
            "train_ids": config_payload["train_ids"],
            "val_ids": config_payload["val_ids"],
            "test_ids": config_payload["test_ids"],
            "val_metrics": val_metrics,
            "test_metrics": test_metrics,
        }
        torch.save(ckpt, run_dir / "last.pt")

        cur = float(val_metrics["norm_mse"])
        if cur < best_val:
            best_val = cur
            best_epoch = epoch
            bad_epochs = 0
            torch.save(ckpt, run_dir / "best.pt")
        else:
            bad_epochs += 1

        if bad_epochs >= args.early_stop_patience:
            print(f"early_stop epoch={epoch} patience={args.early_stop_patience}")
            break

    _write_epoch_csv(run_dir / "epoch_metrics.csv", rows)

    best_ckpt = torch.load(run_dir / "best.pt", map_location=device)
    model.load_state_dict(best_ckpt["model_state"])
    best_test = evaluate_model(
        model=model,
        graphs=test_graphs,
        stats=stats,
        device=device,
        batch_graphs=args.batch_graphs,
        loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
        primary_idx=primary_idx,
        arrival_idx=arrival_idx,
        slack_idx=slack_idx,
        seed=args.seed + 999,
    )

    summary = {
        "run_name": run_name,
        "eval_tag": eval_tag,
        "best_epoch": best_epoch,
        "best_val_norm_mse": best_val,
        "best_test_metrics": best_test,
        "artifacts": {
            "run_dir": str(run_dir.resolve()),
            "best_ckpt": str((run_dir / "best.pt").resolve()),
            "last_ckpt": str((run_dir / "last.pt").resolve()),
            "epoch_csv": str((run_dir / "epoch_metrics.csv").resolve()),
            "config_json": str((run_dir / "config.json").resolve()),
        },
    }
    _write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
