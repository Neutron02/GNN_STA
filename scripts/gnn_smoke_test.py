#!/usr/bin/env python3
"""Pure-Python GNN smoke test on extracted timing graphs.

This is intentionally lightweight and dependency-free (stdlib only).
It trains a tiny message-passing regressor using finite-difference SGD.
Purpose: sanity-check that the graph + labels support learning dynamics.

Supports:
- within-design eval (train/val/test from split manifests)
- unseen-design holdout eval (train on one design, test on another)
- target scaling + z-score normalization (important for tiny second-scale values)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Sequence, Tuple


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
class GraphData:
    run_id: str
    design: str
    node_features: List[List[float]]
    edge_features: List[List[float]]
    edge_src: List[int]
    edge_dst: List[int]
    target_raw_s: List[float]
    target_scaled: List[float]
    target_norm: List[float]
    loss_node_indices: List[int]


def to_float(value: str) -> float:
    if value is None:
        return 0.0
    s = str(value).strip()
    if s == "":
        return 0.0
    try:
        v = float(s)
    except Exception:
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return v


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_dataset_index(path: Path) -> Dict[str, Dict[str, str]]:
    rows = read_csv(path)
    by_run = {}
    for row in rows:
        by_run[row["run_id"]] = row
    return by_run


def load_split_ids(path: Path) -> Dict[str, List[str]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {
        "train": list(data.get("train", [])),
        "val": list(data.get("val", [])),
        "test": list(data.get("test", [])),
    }


def build_graph(row: Dict[str, str], target_col: str, loss_nodes_per_graph: int, rng: random.Random) -> GraphData:
    run_id = row["run_id"]
    design = row["design"]
    nodes_rows = read_csv(Path(row["nodes_csv"]))
    edges_rows = read_csv(Path(row["edges_csv"]))
    labels_rows = read_csv(Path(row["labels_csv"]))

    node_id_to_idx: Dict[str, int] = {}
    node_features: List[List[float]] = []
    for i, nrow in enumerate(nodes_rows):
        node_id = nrow["node_id"]
        node_id_to_idx[node_id] = i
        feats = [to_float(nrow.get(col, "0")) for col in NODE_FEATURE_COLS]
        node_features.append(feats)

    target_raw_s = [0.0] * len(nodes_rows)
    finite_mask = [False] * len(nodes_rows)
    for lrow in labels_rows:
        node_id = lrow["node_id"]
        idx = node_id_to_idx.get(node_id)
        if idx is None:
            continue
        t = to_float(lrow.get(target_col, "0"))
        target_raw_s[idx] = t
        # Keep only rows explicitly marked finite when available.
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
        numeric = [to_float(erow.get(col, "0")) for col in EDGE_NUMERIC_COLS]
        edge_feats = [is_net, is_cell_arc] + numeric
        edge_src.append(src)
        edge_dst.append(dst)
        edge_features.append(edge_feats)

    valid_idxs = [i for i, ok in enumerate(finite_mask) if ok]
    if not valid_idxs:
        raise RuntimeError(f"{run_id}: no finite target nodes found")
    if loss_nodes_per_graph > 0 and len(valid_idxs) > loss_nodes_per_graph:
        valid_idxs = rng.sample(valid_idxs, loss_nodes_per_graph)

    return GraphData(
        run_id=run_id,
        design=design,
        node_features=node_features,
        edge_features=edge_features,
        edge_src=edge_src,
        edge_dst=edge_dst,
        target_raw_s=target_raw_s,
        target_scaled=[0.0] * len(target_raw_s),
        target_norm=[0.0] * len(target_raw_s),
        loss_node_indices=valid_idxs,
    )


def compute_stats(graphs: Sequence[GraphData]) -> Tuple[List[float], List[float], List[float], List[float]]:
    nf = len(graphs[0].node_features[0])
    ef = len(graphs[0].edge_features[0])
    n_sum = [0.0] * nf
    n_sq = [0.0] * nf
    n_cnt = 0
    e_sum = [0.0] * ef
    e_sq = [0.0] * ef
    e_cnt = 0

    for g in graphs:
        for row in g.node_features:
            for j, v in enumerate(row):
                n_sum[j] += v
                n_sq[j] += v * v
            n_cnt += 1
        for row in g.edge_features:
            for j, v in enumerate(row):
                e_sum[j] += v
                e_sq[j] += v * v
            e_cnt += 1

    n_mean = [n_sum[j] / max(1, n_cnt) for j in range(nf)]
    e_mean = [e_sum[j] / max(1, e_cnt) for j in range(ef)]
    n_std = []
    e_std = []
    for j in range(nf):
        var = n_sq[j] / max(1, n_cnt) - n_mean[j] * n_mean[j]
        n_std.append(math.sqrt(var) if var > 1e-20 else 1.0)
    for j in range(ef):
        var = e_sq[j] / max(1, e_cnt) - e_mean[j] * e_mean[j]
        e_std.append(math.sqrt(var) if var > 1e-20 else 1.0)
    return n_mean, n_std, e_mean, e_std


def normalize_graphs(
    graphs: Sequence[GraphData],
    n_mean: Sequence[float],
    n_std: Sequence[float],
    e_mean: Sequence[float],
    e_std: Sequence[float],
) -> None:
    for g in graphs:
        for i, row in enumerate(g.node_features):
            g.node_features[i] = [(row[j] - n_mean[j]) / n_std[j] for j in range(len(row))]
        for i, row in enumerate(g.edge_features):
            g.edge_features[i] = [(row[j] - e_mean[j]) / e_std[j] for j in range(len(row))]


def compute_target_stats(train_graphs: Sequence[GraphData], target_scale: float) -> Tuple[float, float]:
    vals: List[float] = []
    for g in train_graphs:
        for i in g.loss_node_indices:
            vals.append(g.target_raw_s[i] * target_scale)
    if not vals:
        return 0.0, 1.0
    mean = sum(vals) / len(vals)
    var = 0.0
    for v in vals:
        d = v - mean
        var += d * d
    var /= len(vals)
    std = math.sqrt(var) if var > 1e-20 else 1.0
    return mean, std


def normalize_targets(graphs: Sequence[GraphData], target_scale: float, t_mean: float, t_std: float) -> None:
    for g in graphs:
        n = len(g.target_raw_s)
        g.target_scaled = [0.0] * n
        g.target_norm = [0.0] * n
        for i in range(n):
            sv = g.target_raw_s[i] * target_scale
            g.target_scaled[i] = sv
            g.target_norm[i] = (sv - t_mean) / t_std


def unpack_params(
    params: Sequence[float], node_dim: int, edge_dim: int
) -> Tuple[List[float], float, List[float], float, float, float, float, float, float]:
    idx = 0
    w_node = list(params[idx : idx + node_dim])
    idx += node_dim
    w_src = params[idx]
    idx += 1
    w_edge = list(params[idx : idx + edge_dim])
    idx += edge_dim
    b_msg = params[idx]
    idx += 1
    w_self = params[idx]
    idx += 1
    w_agg = params[idx]
    idx += 1
    b_upd = params[idx]
    idx += 1
    w_out = params[idx]
    idx += 1
    b_out = params[idx]
    return w_node, w_src, w_edge, b_msg, w_self, w_agg, b_upd, w_out, b_out


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s


def forward_graph(graph: GraphData, params: Sequence[float], steps: int) -> List[float]:
    node_dim = len(graph.node_features[0])
    edge_dim = len(graph.edge_features[0])
    w_node, w_src, w_edge, b_msg, w_self, w_agg, b_upd, w_out, b_out = unpack_params(
        params, node_dim, edge_dim
    )

    n = len(graph.node_features)
    h = [dot(w_node, feats) for feats in graph.node_features]

    for _ in range(steps):
        agg_sum = [0.0] * n
        agg_count = [0] * n
        for eidx in range(len(graph.edge_src)):
            src = graph.edge_src[eidx]
            dst = graph.edge_dst[eidx]
            msg = w_src * h[src] + dot(w_edge, graph.edge_features[eidx]) + b_msg
            agg_sum[dst] += msg
            agg_count[dst] += 1
        h_new = [0.0] * n
        for i in range(n):
            mean_msg = (agg_sum[i] / agg_count[i]) if agg_count[i] else 0.0
            h_new[i] = w_self * h[i] + w_agg * mean_msg + b_upd
        h = h_new

    return [w_out * v + b_out for v in h]


def mse_for_graph(graph: GraphData, preds: Sequence[float]) -> float:
    idxs = graph.loss_node_indices
    if not idxs:
        return 0.0
    err = 0.0
    for i in idxs:
        d = preds[i] - graph.target_norm[i]
        err += d * d
    return err / len(idxs)


def dataset_loss(graphs: Sequence[GraphData], params: Sequence[float], steps: int) -> float:
    if not graphs:
        return 0.0
    loss = 0.0
    for g in graphs:
        pred = forward_graph(g, params, steps)
        loss += mse_for_graph(g, pred)
    return loss / len(graphs)


def finite_diff_grad(
    graphs: Sequence[GraphData], params: List[float], steps: int, eps: float
) -> List[float]:
    grad = [0.0] * len(params)
    for i in range(len(params)):
        orig = params[i]
        params[i] = orig + eps
        lp = dataset_loss(graphs, params, steps)
        params[i] = orig - eps
        lm = dataset_loss(graphs, params, steps)
        params[i] = orig
        grad[i] = (lp - lm) / (2.0 * eps)
    return grad


def evaluate_dataset(
    graphs: Sequence[GraphData],
    params: Sequence[float],
    steps: int,
    t_mean: float,
    t_std: float,
    target_scale: float,
) -> Dict[str, float]:
    if not graphs:
        return {"norm_mse": 0.0, "rmse_ps": 0.0, "mae_ps": 0.0}
    norm_loss = dataset_loss(graphs, params, steps)
    se = 0.0
    ae = 0.0
    cnt = 0
    for g in graphs:
        pred_norm = forward_graph(g, params, steps)
        for i in g.loss_node_indices:
            pred_scaled = pred_norm[i] * t_std + t_mean
            true_scaled = g.target_scaled[i]
            pred_s = pred_scaled / target_scale
            true_s = true_scaled / target_scale
            d = pred_s - true_s
            se += d * d
            ae += abs(d)
            cnt += 1
    if cnt == 0:
        return {"norm_mse": norm_loss, "rmse_ps": 0.0, "mae_ps": 0.0}
    mse_s = se / cnt
    rmse_ps = math.sqrt(mse_s) * 1e12
    mae_ps = (ae / cnt) * 1e12
    return {"norm_mse": norm_loss, "rmse_ps": rmse_ps, "mae_ps": mae_ps}


def _row_ok(row: Dict[str, str], design: str) -> bool:
    if row.get("design") != design:
        return False
    if row.get("status") != "success":
        return False
    if str(row.get("validation_passed", "")).lower() not in {"true", "1"}:
        return False
    return True


def _pick_rows_from_ids(
    dataset_index: Dict[str, Dict[str, str]], ids: Sequence[str], design: str, max_runs: int
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for rid in ids:
        row = dataset_index.get(rid)
        if row is None or not _row_ok(row, design):
            continue
        out.append(row)
        if max_runs > 0 and len(out) >= max_runs:
            break
    return out


def _pick_rows_all(dataset_index: Dict[str, Dict[str, str]], design: str, max_runs: int) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for rid in sorted(dataset_index.keys()):
        row = dataset_index[rid]
        if not _row_ok(row, design):
            continue
        out.append(row)
        if max_runs > 0 and len(out) >= max_runs:
            break
    return out


def select_runs(
    dataset_index: Dict[str, Dict[str, str]],
    split_ids: Dict[str, List[str]],
    eval_mode: str,
    design: str,
    train_design: str,
    eval_design: str,
    max_train_runs: int,
    max_val_runs: int,
    max_test_runs: int,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], List[Dict[str, str]], str]:
    if eval_mode == "within_design":
        train_rows = _pick_rows_from_ids(dataset_index, split_ids["train"], design, max_train_runs)
        val_rows = _pick_rows_from_ids(dataset_index, split_ids["val"], design, max_val_runs)
        test_rows = _pick_rows_from_ids(dataset_index, split_ids["test"], design, max_test_runs)
        tag = f"within_design:{design}"
        return train_rows, val_rows, test_rows, tag

    if eval_mode == "holdout_design":
        train_rows = _pick_rows_from_ids(dataset_index, split_ids["train"], train_design, max_train_runs)
        val_rows = _pick_rows_from_ids(dataset_index, split_ids["val"], train_design, max_val_runs)
        test_rows = _pick_rows_all(dataset_index, eval_design, max_test_runs)
        tag = f"holdout_design:{train_design}-> {eval_design}"
        return train_rows, val_rows, test_rows, tag

    raise ValueError(f"Unknown eval mode: {eval_mode}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Dependency-free GNN smoke test")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--splits", default="data/manifests/splits.json")
    ap.add_argument("--eval-mode", choices=["within_design", "holdout_design"], default="within_design")
    ap.add_argument("--design", default="gcd")
    ap.add_argument("--train-design", default="gcd")
    ap.add_argument("--eval-design", default="aes")
    ap.add_argument("--target-col", default="slack_setup_scalar_s")
    ap.add_argument("--max-train-runs", type=int, default=4)
    ap.add_argument("--max-val-runs", type=int, default=2)
    ap.add_argument("--max-test-runs", type=int, default=2)
    ap.add_argument("--loss-nodes-per-graph", type=int, default=512)
    ap.add_argument("--message-steps", type=int, default=2)
    ap.add_argument("--epochs", type=int, default=8)
    ap.add_argument("--lr", type=float, default=0.05)
    ap.add_argument("--fd-eps", type=float, default=1e-3)
    ap.add_argument(
        "--target-scale",
        type=float,
        default=1e12,
        help="Scale factor applied before target normalization (1e12 => seconds to picoseconds).",
    )
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    dataset_index = load_dataset_index(Path(args.dataset_index))
    split_ids = load_split_ids(Path(args.splits))
    train_rows, val_rows, test_rows, eval_tag = select_runs(
        dataset_index,
        split_ids,
        args.eval_mode,
        args.design,
        args.train_design,
        args.eval_design,
        args.max_train_runs,
        args.max_val_runs,
        args.max_test_runs,
    )

    if not train_rows:
        raise SystemExit("No train runs selected. Check eval mode, design args, and split manifests.")
    if not val_rows:
        raise SystemExit("No val runs selected. Increase --max-val-runs or check splits.")
    if not test_rows:
        raise SystemExit("No test runs selected. Increase --max-test-runs or check selection.")

    print(f"eval_mode={eval_tag}")
    print(
        f"train_runs={len(train_rows)} val_runs={len(val_rows)} test_runs={len(test_rows)}"
    )
    print(
        f"target_col={args.target_col} target_scale={args.target_scale:.3e} (seconds * scale before z-score)"
    )
    print("train_ids=", ",".join(r["run_id"] for r in train_rows))
    print("val_ids=", ",".join(r["run_id"] for r in val_rows))
    print("test_ids=", ",".join(r["run_id"] for r in test_rows))

    train_graphs = [
        build_graph(row, args.target_col, args.loss_nodes_per_graph, rng) for row in train_rows
    ]
    val_graphs = [
        build_graph(row, args.target_col, args.loss_nodes_per_graph, rng) for row in val_rows
    ]
    test_graphs = [
        build_graph(row, args.target_col, args.loss_nodes_per_graph, rng) for row in test_rows
    ]

    n_mean, n_std, e_mean, e_std = compute_stats(train_graphs)
    normalize_graphs(train_graphs, n_mean, n_std, e_mean, e_std)
    normalize_graphs(val_graphs, n_mean, n_std, e_mean, e_std)
    normalize_graphs(test_graphs, n_mean, n_std, e_mean, e_std)

    t_mean, t_std = compute_target_stats(train_graphs, args.target_scale)
    normalize_targets(train_graphs, args.target_scale, t_mean, t_std)
    normalize_targets(val_graphs, args.target_scale, t_mean, t_std)
    normalize_targets(test_graphs, args.target_scale, t_mean, t_std)

    node_dim = len(train_graphs[0].node_features[0])
    edge_dim = len(train_graphs[0].edge_features[0])
    n_params = node_dim + 1 + edge_dim + 1 + 1 + 1 + 1 + 1 + 1
    params = [rng.uniform(-0.1, 0.1) for _ in range(n_params)]

    tr0 = evaluate_dataset(train_graphs, params, args.message_steps, t_mean, t_std, args.target_scale)
    va0 = evaluate_dataset(val_graphs, params, args.message_steps, t_mean, t_std, args.target_scale)
    te0 = evaluate_dataset(test_graphs, params, args.message_steps, t_mean, t_std, args.target_scale)
    print(
        "epoch=0 "
        f"train_norm_mse={tr0['norm_mse']:.6e} val_norm_mse={va0['norm_mse']:.6e} test_norm_mse={te0['norm_mse']:.6e} "
        f"val_rmse_ps={va0['rmse_ps']:.4f} test_rmse_ps={te0['rmse_ps']:.4f}"
    )

    for ep in range(1, args.epochs + 1):
        grad = finite_diff_grad(train_graphs, params, args.message_steps, args.fd_eps)
        for i in range(len(params)):
            params[i] -= args.lr * grad[i]

        tr = evaluate_dataset(train_graphs, params, args.message_steps, t_mean, t_std, args.target_scale)
        va = evaluate_dataset(val_graphs, params, args.message_steps, t_mean, t_std, args.target_scale)
        te = evaluate_dataset(test_graphs, params, args.message_steps, t_mean, t_std, args.target_scale)
        print(
            f"epoch={ep} "
            f"train_norm_mse={tr['norm_mse']:.6e} val_norm_mse={va['norm_mse']:.6e} test_norm_mse={te['norm_mse']:.6e} "
            f"val_rmse_ps={va['rmse_ps']:.4f} test_rmse_ps={te['rmse_ps']:.4f}"
        )

    final = evaluate_dataset(test_graphs, params, args.message_steps, t_mean, t_std, args.target_scale)
    print(
        "final_test_metrics "
        f"norm_mse={final['norm_mse']:.6e} rmse_ps={final['rmse_ps']:.4f} mae_ps={final['mae_ps']:.4f}"
    )

    print("smoke_test_complete")


if __name__ == "__main__":
    main()
