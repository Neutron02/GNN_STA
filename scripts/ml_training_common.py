#!/usr/bin/env python3
"""Shared utilities for PyTorch timing-graph training and evaluation."""

from __future__ import annotations

import csv
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import torch
except Exception:  # pragma: no cover - handled by require_torch
    torch = None


NODE_FEATURE_COLS = [
    "is_sequential_cell",
    "is_buffer_cell",
    "is_inverter_cell",
    "is_clock_pin",
    "is_endpoint",
    "x_um",
    "y_um",
    "inst_x_um",
    "inst_y_um",
    "cell_area_um2",
    "port_cap_max_f",
    "port_cap_min_f",
]

EDGE_NUMERIC_COLS = [
    "net_fanout",
    "net_routed_length_um",
    "net_cap_max_f",
    "net_cap_min_f",
    "net_wire_res_ohm_rcx",
    "net_wire_cap_f_rcx",
]


@dataclass
class TimingGraph:
    run_id: str
    design: str
    x: "torch.Tensor"
    edge_index: "torch.Tensor"
    edge_attr: "torch.Tensor"
    cell_type_tokens: List[str]
    cell_type_idx: Optional["torch.Tensor"]
    y_sec: "torch.Tensor"
    y_norm: "torch.Tensor"
    finite_idx: "torch.Tensor"
    wns_sec: Optional[float]


@dataclass
class NormalizationStats:
    node_mean: List[float]
    node_std: List[float]
    edge_mean: List[float]
    edge_std: List[float]
    target_scale: float
    target_mean: float
    target_std: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "node_mean": self.node_mean,
            "node_std": self.node_std,
            "edge_mean": self.edge_mean,
            "edge_std": self.edge_std,
            "target_scale": self.target_scale,
            "target_mean": self.target_mean,
            "target_std": self.target_std,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "NormalizationStats":
        return cls(
            node_mean=[float(v) for v in d["node_mean"]],
            node_std=[float(v) for v in d["node_std"]],
            edge_mean=[float(v) for v in d["edge_mean"]],
            edge_std=[float(v) for v in d["edge_std"]],
            target_scale=float(d["target_scale"]),
            target_mean=float(d["target_mean"]),
            target_std=float(d["target_std"]),
        )


@dataclass
class GraphBatch:
    x: "torch.Tensor"
    edge_index: "torch.Tensor"
    edge_attr: "torch.Tensor"
    cell_type_idx: Optional["torch.Tensor"]
    y_sec: "torch.Tensor"
    y_norm: "torch.Tensor"
    loss_idx: "torch.Tensor"
    graph_ptr: List[int]
    graph_wns_sec: List[Optional[float]]
    graph_run_ids: List[str]


def require_torch() -> None:
    if torch is None:
        raise RuntimeError(
            "PyTorch is required for train/eval scripts. Install torch, then rerun."
        )


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


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_dataset_index(path: Path) -> Dict[str, Dict[str, str]]:
    rows = _read_csv(path)
    return {row["run_id"]: row for row in rows}


def load_splits(path: Path) -> Dict[str, List[str]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "train": list(data.get("train", [])),
        "val": list(data.get("val", [])),
        "test": list(data.get("test", [])),
    }


def _row_ok(row: Dict[str, str], design: str) -> bool:
    if row.get("design") != design:
        return False
    if row.get("status") != "success":
        return False
    if str(row.get("validation_passed", "")).lower() not in {"1", "true"}:
        return False
    return True


def _limit_rows(rows: List[Dict[str, str]], max_runs: int) -> List[Dict[str, str]]:
    if max_runs <= 0:
        return rows
    return rows[:max_runs]


def _pick_rows_from_ids(
    dataset_index: Dict[str, Dict[str, str]],
    run_ids: Sequence[str],
    design: str,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for rid in run_ids:
        row = dataset_index.get(rid)
        if row is None:
            continue
        if _row_ok(row, design):
            out.append(row)
    return out


def _pick_rows_all(dataset_index: Dict[str, Dict[str, str]], design: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for rid in sorted(dataset_index.keys()):
        row = dataset_index[rid]
        if _row_ok(row, design):
            out.append(row)
    return out


def select_rows(
    dataset_index: Dict[str, Dict[str, str]],
    splits: Dict[str, List[str]],
    eval_mode: str,
    design: str,
    train_design: str,
    eval_design: str,
    max_train_runs: int,
    max_val_runs: int,
    max_test_runs: int,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]], str]:
    if eval_mode == "within_design":
        train_rows = _limit_rows(_pick_rows_from_ids(dataset_index, splits["train"], design), max_train_runs)
        val_rows = _limit_rows(_pick_rows_from_ids(dataset_index, splits["val"], design), max_val_runs)
        test_rows = _limit_rows(_pick_rows_from_ids(dataset_index, splits["test"], design), max_test_runs)
        return train_rows, val_rows, test_rows, f"within_design:{design}"

    if eval_mode == "holdout_design":
        train_rows = _limit_rows(
            _pick_rows_from_ids(dataset_index, splits["train"], train_design), max_train_runs
        )
        val_rows = _limit_rows(
            _pick_rows_from_ids(dataset_index, splits["val"], train_design), max_val_runs
        )
        test_rows = _limit_rows(_pick_rows_all(dataset_index, eval_design), max_test_runs)
        return train_rows, val_rows, test_rows, f"holdout_design:{train_design}->{eval_design}"

    raise ValueError(f"Unsupported eval mode: {eval_mode}")


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


def _cell_type_token(nrow: Dict[str, str]) -> str:
    cell = str(nrow.get("cell_name", "")).strip()
    if cell:
        return cell
    node_kind = str(nrow.get("node_kind", "")).strip().lower()
    io_type = str(nrow.get("io_type", "")).strip().upper() or "UNK"
    if node_kind == "bterm":
        return f"BTERM_{io_type}"
    return "UNK_CELL"


def load_graph(row: Dict[str, str], target_col: str) -> TimingGraph:
    require_torch()
    run_id = row["run_id"]
    design = row["design"]

    nodes_rows = _read_csv(Path(row["nodes_csv"]))
    edges_rows = _read_csv(Path(row["edges_csv"]))
    labels_rows = _read_csv(Path(row["labels_csv"]))
    global_json = Path(row["global_json"])

    node_id_to_idx: Dict[str, int] = {}
    node_features: List[List[float]] = []
    cell_type_tokens: List[str] = []
    for i, nrow in enumerate(nodes_rows):
        node_id_to_idx[nrow["node_id"]] = i
        node_features.append([_to_float(nrow.get(col, "0")) for col in NODE_FEATURE_COLS])
        cell_type_tokens.append(_cell_type_token(nrow))

    y_sec = [0.0] * len(nodes_rows)
    finite_mask = [False] * len(nodes_rows)
    for lrow in labels_rows:
        node_id = lrow.get("node_id", "")
        idx = node_id_to_idx.get(node_id)
        if idx is None:
            continue
        yv = _to_float(lrow.get(target_col, "0"))
        y_sec[idx] = yv
        is_inf = str(lrow.get("is_slack_inf", "0")).strip()
        finite_mask[idx] = (is_inf != "1")

    edge_src: List[int] = []
    edge_dst: List[int] = []
    edge_features: List[List[float]] = []
    for erow in edges_rows:
        src = node_id_to_idx.get(erow.get("src_node_id", ""))
        dst = node_id_to_idx.get(erow.get("dst_node_id", ""))
        if src is None or dst is None:
            continue
        edge_type = erow.get("edge_type", "")
        is_net = 1.0 if edge_type == "net" else 0.0
        is_cell_arc = 1.0 if edge_type == "cell_arc" else 0.0
        numeric = [_to_float(erow.get(col, "0")) for col in EDGE_NUMERIC_COLS]
        edge_src.append(src)
        edge_dst.append(dst)
        edge_features.append([is_net, is_cell_arc] + numeric)

    finite_idx = [i for i, ok in enumerate(finite_mask) if ok]
    if not finite_idx:
        raise RuntimeError(f"{run_id}: no finite target nodes found")

    x = torch.tensor(node_features, dtype=torch.float32)
    edge_index = torch.tensor([edge_src, edge_dst], dtype=torch.long)
    edge_attr = torch.tensor(edge_features, dtype=torch.float32)
    y_sec_t = torch.tensor(y_sec, dtype=torch.float32)
    y_norm_t = torch.zeros_like(y_sec_t)
    finite_idx_t = torch.tensor(finite_idx, dtype=torch.long)

    return TimingGraph(
        run_id=run_id,
        design=design,
        x=x,
        edge_index=edge_index,
        edge_attr=edge_attr,
        cell_type_tokens=cell_type_tokens,
        cell_type_idx=None,
        y_sec=y_sec_t,
        y_norm=y_norm_t,
        finite_idx=finite_idx_t,
        wns_sec=_parse_wns_sec(global_json),
    )


def compute_normalization_stats(train_graphs: Sequence[TimingGraph], target_scale: float) -> NormalizationStats:
    require_torch()
    if not train_graphs:
        raise ValueError("No training graphs provided")

    x_all = torch.cat([g.x for g in train_graphs], dim=0)
    e_all = torch.cat([g.edge_attr for g in train_graphs], dim=0)

    node_mean = x_all.mean(dim=0)
    node_std = x_all.std(dim=0, unbiased=False)
    node_std = torch.where(node_std < 1e-12, torch.ones_like(node_std), node_std)

    edge_mean = e_all.mean(dim=0)
    edge_std = e_all.std(dim=0, unbiased=False)
    edge_std = torch.where(edge_std < 1e-12, torch.ones_like(edge_std), edge_std)

    y_scaled_values = []
    for g in train_graphs:
        y_scaled_values.append(g.y_sec[g.finite_idx] * float(target_scale))
    y_all = torch.cat(y_scaled_values, dim=0)
    target_mean = y_all.mean()
    target_std = y_all.std(unbiased=False)
    if float(target_std) < 1e-12:
        target_std = torch.tensor(1.0, dtype=torch.float32)

    return NormalizationStats(
        node_mean=node_mean.tolist(),
        node_std=node_std.tolist(),
        edge_mean=edge_mean.tolist(),
        edge_std=edge_std.tolist(),
        target_scale=float(target_scale),
        target_mean=float(target_mean),
        target_std=float(target_std),
    )


def apply_normalization(graphs: Sequence[TimingGraph], stats: NormalizationStats) -> None:
    require_torch()
    n_mean = torch.tensor(stats.node_mean, dtype=torch.float32)
    n_std = torch.tensor(stats.node_std, dtype=torch.float32)
    e_mean = torch.tensor(stats.edge_mean, dtype=torch.float32)
    e_std = torch.tensor(stats.edge_std, dtype=torch.float32)
    t_scale = float(stats.target_scale)
    t_mean = float(stats.target_mean)
    t_std = float(stats.target_std)

    for g in graphs:
        g.x = (g.x - n_mean) / n_std
        g.edge_attr = (g.edge_attr - e_mean) / e_std
        g.y_norm = (g.y_sec * t_scale - t_mean) / t_std


def build_cell_type_vocab(train_graphs: Sequence[TimingGraph], min_count: int = 1) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for g in train_graphs:
        for tok in g.cell_type_tokens:
            counts[tok] = counts.get(tok, 0) + 1

    vocab: Dict[str, int] = {"__UNK__": 0}
    for tok in sorted(counts.keys()):
        if counts[tok] >= min_count:
            vocab[tok] = len(vocab)
    return vocab


def apply_cell_type_encoding(graphs: Sequence[TimingGraph], vocab: Dict[str, int]) -> None:
    require_torch()
    unk = int(vocab.get("__UNK__", 0))
    for g in graphs:
        ids = [int(vocab.get(tok, unk)) for tok in g.cell_type_tokens]
        g.cell_type_idx = torch.tensor(ids, dtype=torch.long)


def _sample_indices(
    idx: "torch.Tensor",
    max_count: int,
    rng: random.Random,
) -> "torch.Tensor":
    if max_count <= 0 or idx.numel() <= max_count:
        return idx
    data = idx.tolist()
    sampled = rng.sample(data, max_count)
    sampled.sort()
    return torch.tensor(sampled, dtype=torch.long)


def make_batch(
    graphs: Sequence[TimingGraph],
    loss_nodes_per_graph: int,
    rng: random.Random,
) -> GraphBatch:
    require_torch()
    x_parts: List["torch.Tensor"] = []
    eidx_parts: List["torch.Tensor"] = []
    eattr_parts: List["torch.Tensor"] = []
    y_sec_parts: List["torch.Tensor"] = []
    y_norm_parts: List["torch.Tensor"] = []
    loss_idx_parts: List["torch.Tensor"] = []
    cell_type_parts: List["torch.Tensor"] = []
    graph_ptr = [0]
    graph_wns_sec: List[Optional[float]] = []
    graph_run_ids: List[str] = []
    has_cell_types = any(g.cell_type_idx is not None for g in graphs)

    offset = 0
    for g in graphs:
        x_parts.append(g.x)
        y_sec_parts.append(g.y_sec)
        y_norm_parts.append(g.y_norm)

        eidx_parts.append(g.edge_index + offset)
        eattr_parts.append(g.edge_attr)
        if has_cell_types:
            if g.cell_type_idx is None:
                raise RuntimeError(f"{g.run_id}: missing cell_type_idx for encoded batch")
            cell_type_parts.append(g.cell_type_idx)

        sampled = _sample_indices(g.finite_idx, loss_nodes_per_graph, rng) + offset
        loss_idx_parts.append(sampled)

        offset += g.x.size(0)
        graph_ptr.append(offset)
        graph_wns_sec.append(g.wns_sec)
        graph_run_ids.append(g.run_id)

    return GraphBatch(
        x=torch.cat(x_parts, dim=0),
        edge_index=torch.cat(eidx_parts, dim=1),
        edge_attr=torch.cat(eattr_parts, dim=0),
        cell_type_idx=(torch.cat(cell_type_parts, dim=0) if has_cell_types else None),
        y_sec=torch.cat(y_sec_parts, dim=0),
        y_norm=torch.cat(y_norm_parts, dim=0),
        loss_idx=torch.cat(loss_idx_parts, dim=0),
        graph_ptr=graph_ptr,
        graph_wns_sec=graph_wns_sec,
        graph_run_ids=graph_run_ids,
    )


def iter_batches(
    graphs: Sequence[TimingGraph],
    batch_graphs: int,
    loss_nodes_per_graph: int,
    shuffle: bool,
    rng: random.Random,
) -> Iterable[GraphBatch]:
    if batch_graphs <= 0:
        batch_graphs = 1
    order = list(range(len(graphs)))
    if shuffle:
        rng.shuffle(order)
    for i in range(0, len(order), batch_graphs):
        chunk = [graphs[j] for j in order[i : i + batch_graphs]]
        yield make_batch(chunk, loss_nodes_per_graph, rng)


def denorm_predictions_to_sec(pred_norm: "torch.Tensor", stats: NormalizationStats) -> "torch.Tensor":
    require_torch()
    return (pred_norm * float(stats.target_std) + float(stats.target_mean)) / float(stats.target_scale)


def evaluate_model(
    model,
    graphs: Sequence[TimingGraph],
    stats: NormalizationStats,
    device: "torch.device",
    batch_graphs: int,
    loss_nodes_per_graph: int,
    seed: int,
) -> Dict[str, float]:
    require_torch()
    model.eval()

    rng = random.Random(seed)
    total_sq_norm = 0.0
    total_sq_sec = 0.0
    total_abs_sec = 0.0
    total_nodes = 0

    wns_diffs_ps: List[float] = []

    with torch.no_grad():
        for batch in iter_batches(
            graphs=graphs,
            batch_graphs=batch_graphs,
            loss_nodes_per_graph=loss_nodes_per_graph,
            shuffle=False,
            rng=rng,
        ):
            x = batch.x.to(device)
            edge_index = batch.edge_index.to(device)
            edge_attr = batch.edge_attr.to(device)
            cell_type_idx = None
            if batch.cell_type_idx is not None:
                cell_type_idx = batch.cell_type_idx.to(device)
            y_norm = batch.y_norm.to(device)
            y_sec = batch.y_sec.to(device)
            loss_idx = batch.loss_idx.to(device)

            pred_norm = model(x, edge_index, edge_attr, cell_type_idx=cell_type_idx)
            pred_sec = denorm_predictions_to_sec(pred_norm, stats)

            diff_norm = pred_norm[loss_idx] - y_norm[loss_idx]
            diff_sec = pred_sec[loss_idx] - y_sec[loss_idx]

            total_sq_norm += float((diff_norm * diff_norm).sum().item())
            total_sq_sec += float((diff_sec * diff_sec).sum().item())
            total_abs_sec += float(diff_sec.abs().sum().item())
            total_nodes += int(loss_idx.numel())

            for gidx, wns_sec in enumerate(batch.graph_wns_sec):
                if wns_sec is None:
                    continue
                start = batch.graph_ptr[gidx]
                end = batch.graph_ptr[gidx + 1]
                local_pred = pred_sec[start:end]
                local_loss = loss_idx[(loss_idx >= start) & (loss_idx < end)] - start
                if local_loss.numel() == 0:
                    continue
                pred_wns = float(local_pred[local_loss].min().item())
                wns_diffs_ps.append(abs(pred_wns - float(wns_sec)) * 1e12)

    if total_nodes == 0:
        return {
            "norm_mse": 0.0,
            "rmse_ps": 0.0,
            "mae_ps": 0.0,
            "wns_mae_ps": 0.0,
            "wns_rmse_ps": 0.0,
        }

    norm_mse = total_sq_norm / total_nodes
    mse_sec = total_sq_sec / total_nodes
    rmse_ps = math.sqrt(mse_sec) * 1e12
    mae_ps = (total_abs_sec / total_nodes) * 1e12

    if wns_diffs_ps:
        wns_mae_ps = sum(wns_diffs_ps) / len(wns_diffs_ps)
        wns_rmse_ps = math.sqrt(sum(v * v for v in wns_diffs_ps) / len(wns_diffs_ps))
    else:
        wns_mae_ps = 0.0
        wns_rmse_ps = 0.0

    return {
        "norm_mse": norm_mse,
        "rmse_ps": rmse_ps,
        "mae_ps": mae_ps,
        "wns_mae_ps": wns_mae_ps,
        "wns_rmse_ps": wns_rmse_ps,
    }


@dataclass
class MultiTimingGraph:
    run_id: str
    design: str
    x: "torch.Tensor"
    edge_index: "torch.Tensor"
    edge_attr: "torch.Tensor"
    cell_type_tokens: List[str]
    cell_type_idx: Optional["torch.Tensor"]
    y_sec: "torch.Tensor"  # [N, T]
    y_norm: "torch.Tensor"  # [N, T]
    finite_idx: "torch.Tensor"
    wns_sec: Optional[float]


@dataclass
class MultiNormalizationStats:
    node_mean: List[float]
    node_std: List[float]
    edge_mean: List[float]
    edge_std: List[float]
    target_scale: float
    target_mean: List[float]
    target_std: List[float]

    def to_dict(self) -> Dict[str, object]:
        return {
            "node_mean": self.node_mean,
            "node_std": self.node_std,
            "edge_mean": self.edge_mean,
            "edge_std": self.edge_std,
            "target_scale": self.target_scale,
            "target_mean": self.target_mean,
            "target_std": self.target_std,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "MultiNormalizationStats":
        return cls(
            node_mean=[float(v) for v in d["node_mean"]],
            node_std=[float(v) for v in d["node_std"]],
            edge_mean=[float(v) for v in d["edge_mean"]],
            edge_std=[float(v) for v in d["edge_std"]],
            target_scale=float(d["target_scale"]),
            target_mean=[float(v) for v in d["target_mean"]],
            target_std=[float(v) for v in d["target_std"]],
        )


@dataclass
class MultiGraphBatch:
    x: "torch.Tensor"
    edge_index: "torch.Tensor"
    edge_attr: "torch.Tensor"
    cell_type_idx: Optional["torch.Tensor"]
    y_sec: "torch.Tensor"  # [N, T]
    y_norm: "torch.Tensor"  # [N, T]
    loss_idx: "torch.Tensor"
    graph_ptr: List[int]
    graph_wns_sec: List[Optional[float]]
    graph_run_ids: List[str]


def load_graph_multi(
    row: Dict[str, str],
    target_cols: Sequence[str],
    finite_flag_col: str = "is_slack_inf",
) -> MultiTimingGraph:
    require_torch()
    run_id = row["run_id"]
    design = row["design"]

    nodes_rows = _read_csv(Path(row["nodes_csv"]))
    edges_rows = _read_csv(Path(row["edges_csv"]))
    labels_rows = _read_csv(Path(row["labels_csv"]))
    global_json = Path(row["global_json"])

    node_id_to_idx: Dict[str, int] = {}
    node_features: List[List[float]] = []
    cell_type_tokens: List[str] = []
    for i, nrow in enumerate(nodes_rows):
        node_id_to_idx[nrow["node_id"]] = i
        node_features.append([_to_float(nrow.get(col, "0")) for col in NODE_FEATURE_COLS])
        cell_type_tokens.append(_cell_type_token(nrow))

    t = len(target_cols)
    y_sec = [[0.0] * t for _ in range(len(nodes_rows))]
    finite_mask = [False] * len(nodes_rows)
    for lrow in labels_rows:
        node_id = lrow.get("node_id", "")
        idx = node_id_to_idx.get(node_id)
        if idx is None:
            continue
        for j, col in enumerate(target_cols):
            y_sec[idx][j] = _to_float(lrow.get(col, "0"))
        is_inf = str(lrow.get(finite_flag_col, "0")).strip()
        finite_mask[idx] = (is_inf != "1")

    edge_src: List[int] = []
    edge_dst: List[int] = []
    edge_features: List[List[float]] = []
    for erow in edges_rows:
        src = node_id_to_idx.get(erow.get("src_node_id", ""))
        dst = node_id_to_idx.get(erow.get("dst_node_id", ""))
        if src is None or dst is None:
            continue
        edge_type = erow.get("edge_type", "")
        is_net = 1.0 if edge_type == "net" else 0.0
        is_cell_arc = 1.0 if edge_type == "cell_arc" else 0.0
        numeric = [_to_float(erow.get(col, "0")) for col in EDGE_NUMERIC_COLS]
        edge_src.append(src)
        edge_dst.append(dst)
        edge_features.append([is_net, is_cell_arc] + numeric)

    finite_idx = [i for i, ok in enumerate(finite_mask) if ok]
    if not finite_idx:
        raise RuntimeError(f"{run_id}: no finite target nodes found")

    x = torch.tensor(node_features, dtype=torch.float32)
    edge_index = torch.tensor([edge_src, edge_dst], dtype=torch.long)
    edge_attr = torch.tensor(edge_features, dtype=torch.float32)
    y_sec_t = torch.tensor(y_sec, dtype=torch.float32)
    y_norm_t = torch.zeros_like(y_sec_t)
    finite_idx_t = torch.tensor(finite_idx, dtype=torch.long)

    return MultiTimingGraph(
        run_id=run_id,
        design=design,
        x=x,
        edge_index=edge_index,
        edge_attr=edge_attr,
        cell_type_tokens=cell_type_tokens,
        cell_type_idx=None,
        y_sec=y_sec_t,
        y_norm=y_norm_t,
        finite_idx=finite_idx_t,
        wns_sec=_parse_wns_sec(global_json),
    )


def build_cell_type_vocab_multi(train_graphs: Sequence[MultiTimingGraph], min_count: int = 1) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for g in train_graphs:
        for tok in g.cell_type_tokens:
            counts[tok] = counts.get(tok, 0) + 1

    vocab: Dict[str, int] = {"__UNK__": 0}
    for tok in sorted(counts.keys()):
        if counts[tok] >= min_count:
            vocab[tok] = len(vocab)
    return vocab


def apply_cell_type_encoding_multi(graphs: Sequence[MultiTimingGraph], vocab: Dict[str, int]) -> None:
    require_torch()
    unk = int(vocab.get("__UNK__", 0))
    for g in graphs:
        ids = [int(vocab.get(tok, unk)) for tok in g.cell_type_tokens]
        g.cell_type_idx = torch.tensor(ids, dtype=torch.long)


def compute_normalization_stats_multi(
    train_graphs: Sequence[MultiTimingGraph],
    target_scale: float,
) -> MultiNormalizationStats:
    require_torch()
    if not train_graphs:
        raise ValueError("No training graphs provided")

    x_all = torch.cat([g.x for g in train_graphs], dim=0)
    e_all = torch.cat([g.edge_attr for g in train_graphs], dim=0)

    node_mean = x_all.mean(dim=0)
    node_std = x_all.std(dim=0, unbiased=False)
    node_std = torch.where(node_std < 1e-12, torch.ones_like(node_std), node_std)

    edge_mean = e_all.mean(dim=0)
    edge_std = e_all.std(dim=0, unbiased=False)
    edge_std = torch.where(edge_std < 1e-12, torch.ones_like(edge_std), edge_std)

    y_scaled_values = []
    for g in train_graphs:
        y_scaled_values.append(g.y_sec[g.finite_idx] * float(target_scale))
    y_all = torch.cat(y_scaled_values, dim=0)
    target_mean = y_all.mean(dim=0)
    target_std = y_all.std(dim=0, unbiased=False)
    target_std = torch.where(target_std < 1e-12, torch.ones_like(target_std), target_std)

    return MultiNormalizationStats(
        node_mean=node_mean.tolist(),
        node_std=node_std.tolist(),
        edge_mean=edge_mean.tolist(),
        edge_std=edge_std.tolist(),
        target_scale=float(target_scale),
        target_mean=target_mean.tolist(),
        target_std=target_std.tolist(),
    )


def apply_normalization_multi(graphs: Sequence[MultiTimingGraph], stats: MultiNormalizationStats) -> None:
    require_torch()
    n_mean = torch.tensor(stats.node_mean, dtype=torch.float32)
    n_std = torch.tensor(stats.node_std, dtype=torch.float32)
    e_mean = torch.tensor(stats.edge_mean, dtype=torch.float32)
    e_std = torch.tensor(stats.edge_std, dtype=torch.float32)
    t_scale = float(stats.target_scale)
    t_mean = torch.tensor(stats.target_mean, dtype=torch.float32)
    t_std = torch.tensor(stats.target_std, dtype=torch.float32)

    for g in graphs:
        g.x = (g.x - n_mean) / n_std
        g.edge_attr = (g.edge_attr - e_mean) / e_std
        g.y_norm = (g.y_sec * t_scale - t_mean) / t_std


def make_batch_multi(
    graphs: Sequence[MultiTimingGraph],
    loss_nodes_per_graph: int,
    rng: random.Random,
) -> MultiGraphBatch:
    require_torch()
    x_parts: List["torch.Tensor"] = []
    eidx_parts: List["torch.Tensor"] = []
    eattr_parts: List["torch.Tensor"] = []
    y_sec_parts: List["torch.Tensor"] = []
    y_norm_parts: List["torch.Tensor"] = []
    loss_idx_parts: List["torch.Tensor"] = []
    cell_type_parts: List["torch.Tensor"] = []
    graph_ptr = [0]
    graph_wns_sec: List[Optional[float]] = []
    graph_run_ids: List[str] = []
    has_cell_types = any(g.cell_type_idx is not None for g in graphs)

    offset = 0
    for g in graphs:
        x_parts.append(g.x)
        y_sec_parts.append(g.y_sec)
        y_norm_parts.append(g.y_norm)

        eidx_parts.append(g.edge_index + offset)
        eattr_parts.append(g.edge_attr)
        if has_cell_types:
            if g.cell_type_idx is None:
                raise RuntimeError(f"{g.run_id}: missing cell_type_idx for encoded batch")
            cell_type_parts.append(g.cell_type_idx)

        sampled = _sample_indices(g.finite_idx, loss_nodes_per_graph, rng) + offset
        loss_idx_parts.append(sampled)

        offset += g.x.size(0)
        graph_ptr.append(offset)
        graph_wns_sec.append(g.wns_sec)
        graph_run_ids.append(g.run_id)

    return MultiGraphBatch(
        x=torch.cat(x_parts, dim=0),
        edge_index=torch.cat(eidx_parts, dim=1),
        edge_attr=torch.cat(eattr_parts, dim=0),
        cell_type_idx=(torch.cat(cell_type_parts, dim=0) if has_cell_types else None),
        y_sec=torch.cat(y_sec_parts, dim=0),
        y_norm=torch.cat(y_norm_parts, dim=0),
        loss_idx=torch.cat(loss_idx_parts, dim=0),
        graph_ptr=graph_ptr,
        graph_wns_sec=graph_wns_sec,
        graph_run_ids=graph_run_ids,
    )


def iter_batches_multi(
    graphs: Sequence[MultiTimingGraph],
    batch_graphs: int,
    loss_nodes_per_graph: int,
    shuffle: bool,
    rng: random.Random,
) -> Iterable[MultiGraphBatch]:
    if batch_graphs <= 0:
        batch_graphs = 1
    order = list(range(len(graphs)))
    if shuffle:
        rng.shuffle(order)
    for i in range(0, len(order), batch_graphs):
        chunk = [graphs[j] for j in order[i : i + batch_graphs]]
        yield make_batch_multi(chunk, loss_nodes_per_graph, rng)


def denorm_predictions_to_sec_multi(
    pred_norm: "torch.Tensor",
    stats: MultiNormalizationStats,
) -> "torch.Tensor":
    require_torch()
    t_mean = torch.tensor(stats.target_mean, dtype=torch.float32, device=pred_norm.device)
    t_std = torch.tensor(stats.target_std, dtype=torch.float32, device=pred_norm.device)
    return (pred_norm * t_std + t_mean) / float(stats.target_scale)


def evaluate_model_multi(
    model,
    graphs: Sequence[MultiTimingGraph],
    stats: MultiNormalizationStats,
    device: "torch.device",
    batch_graphs: int,
    loss_nodes_per_graph: int,
    primary_idx: int,
    seed: int,
) -> Dict[str, float]:
    require_torch()
    model.eval()
    rng = random.Random(seed)

    target_count = len(stats.target_mean)
    sq_norm = [0.0] * target_count
    sq_sec = [0.0] * target_count
    abs_sec = [0.0] * target_count
    total_nodes = 0
    wns_diffs_ps: List[float] = []

    with torch.no_grad():
        for batch in iter_batches_multi(
            graphs=graphs,
            batch_graphs=batch_graphs,
            loss_nodes_per_graph=loss_nodes_per_graph,
            shuffle=False,
            rng=rng,
        ):
            x = batch.x.to(device)
            edge_index = batch.edge_index.to(device)
            edge_attr = batch.edge_attr.to(device)
            cell_type_idx = None
            if batch.cell_type_idx is not None:
                cell_type_idx = batch.cell_type_idx.to(device)
            y_norm = batch.y_norm.to(device)
            y_sec = batch.y_sec.to(device)
            loss_idx = batch.loss_idx.to(device)

            pred_norm = model(x, edge_index, edge_attr, cell_type_idx=cell_type_idx)
            pred_sec = denorm_predictions_to_sec_multi(pred_norm, stats)

            diff_norm = pred_norm[loss_idx] - y_norm[loss_idx]
            diff_sec = pred_sec[loss_idx] - y_sec[loss_idx]

            for j in range(target_count):
                dnorm = diff_norm[:, j]
                dsec = diff_sec[:, j]
                sq_norm[j] += float((dnorm * dnorm).sum().item())
                sq_sec[j] += float((dsec * dsec).sum().item())
                abs_sec[j] += float(dsec.abs().sum().item())
            total_nodes += int(loss_idx.numel())

            for gidx, wns_sec in enumerate(batch.graph_wns_sec):
                if wns_sec is None:
                    continue
                start = batch.graph_ptr[gidx]
                end = batch.graph_ptr[gidx + 1]
                local_pred = pred_sec[start:end, primary_idx]
                local_loss = loss_idx[(loss_idx >= start) & (loss_idx < end)] - start
                if local_loss.numel() == 0:
                    continue
                pred_wns = float(local_pred[local_loss].min().item())
                wns_diffs_ps.append(abs(pred_wns - float(wns_sec)) * 1e12)

    if total_nodes == 0:
        return {
            "norm_mse": 0.0,
            "rmse_ps": 0.0,
            "mae_ps": 0.0,
            "wns_mae_ps": 0.0,
            "wns_rmse_ps": 0.0,
            "norm_mse_avg": 0.0,
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

    out = {
        "norm_mse": norm_mse_all[primary_idx],
        "rmse_ps": rmse_ps_all[primary_idx],
        "mae_ps": mae_ps_all[primary_idx],
        "wns_mae_ps": wns_mae_ps,
        "wns_rmse_ps": wns_rmse_ps,
        "norm_mse_avg": sum(norm_mse_all) / len(norm_mse_all),
    }
    for j in range(target_count):
        out[f"norm_mse_t{j}"] = norm_mse_all[j]
        out[f"rmse_ps_t{j}"] = rmse_ps_all[j]
        out[f"mae_ps_t{j}"] = mae_ps_all[j]
    return out
