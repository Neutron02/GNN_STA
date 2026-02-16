#!/usr/bin/env python3
"""Evaluate a trained tripartite dual-pass timing model checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List

from ml_training_common import EDGE_NUMERIC_COLS, load_dataset_index, load_splits, require_torch, select_rows
from train_tripath_dualpass import (
    TripNormStats,
    TripathDualPassNet,
    apply_cell_type_encoding,
    apply_norm,
    evaluate_model,
    load_trip_graph,
)

try:
    import torch
except Exception:  # pragma: no cover
    torch = None


def _resolve_device(name: str):
    if name == "cpu":
        return torch.device("cpu")
    if name == "cuda":
        return torch.device("cuda")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _print_block(name: str, m: Dict[str, float]) -> None:
    print(
        f"{name}: "
        f"norm_mse={m['norm_mse']:.6e} "
        f"rmse_ps={m['rmse_ps']:.3f} "
        f"mae_ps={m['mae_ps']:.3f} "
        f"wns_mae_ps={m['wns_mae_ps']:.3f} "
        f"path_slack_rmse_ps={m.get('path_slack_rmse_ps', 0.0):.3f}"
    )


def _target_idx(target_cols: List[str], col: str, default_idx: int) -> int:
    if col in target_cols:
        return int(target_cols.index(col))
    return default_idx


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate trained tripartite dual-pass checkpoint")
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--splits", default="data/manifests/splits.json")
    ap.add_argument("--eval-mode", choices=["within_design", "holdout_design"], default=None)
    ap.add_argument("--design", default=None)
    ap.add_argument("--train-design", default=None)
    ap.add_argument("--eval-design", default=None)
    ap.add_argument("--max-train-runs", type=int, default=0)
    ap.add_argument("--max-val-runs", type=int, default=0)
    ap.add_argument("--max-test-runs", type=int, default=0)
    ap.add_argument("--max-paths-per-graph", type=int, default=None)
    ap.add_argument("--batch-graphs", type=int, default=None)
    ap.add_argument("--loss-nodes-per-graph-eval", type=int, default=None)
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--save-json", default="")
    args = ap.parse_args()

    require_torch()
    if torch is None:
        raise RuntimeError("PyTorch is unavailable in this environment.")

    ckpt_path = Path(args.checkpoint)
    ckpt = torch.load(ckpt_path, map_location="cpu")
    train_args = ckpt.get("args", {})

    eval_mode = args.eval_mode or train_args.get("eval_mode", "holdout_design")
    design = args.design or train_args.get("design", "gcd")
    train_design = args.train_design or train_args.get("train_design", "gcd")
    eval_design = args.eval_design or train_args.get("eval_design", "aes")
    batch_graphs = args.batch_graphs or int(train_args.get("batch_graphs", 2))
    loss_nodes_per_graph_eval = (
        args.loss_nodes_per_graph_eval
        if args.loss_nodes_per_graph_eval is not None
        else int(train_args.get("loss_nodes_per_graph_eval", 0))
    )
    max_paths = (
        args.max_paths_per_graph
        if args.max_paths_per_graph is not None
        else int(train_args.get("max_paths_per_graph", 64))
    )

    target_cols = list(ckpt.get("target_cols", []))
    if not target_cols:
        raise RuntimeError("Checkpoint missing target_cols.")
    primary_idx = int(ckpt.get("primary_target_idx", 1))
    arrival_idx = int(ckpt.get("arrival_idx", _target_idx(target_cols, "arrival_setup_scalar_s", 0)))
    slack_idx = int(ckpt.get("slack_idx", _target_idx(target_cols, "slack_setup_scalar_s", 1)))

    dataset_index = load_dataset_index(Path(args.dataset_index))
    splits = load_splits(Path(args.splits))
    train_rows, val_rows, test_rows, eval_tag = select_rows(
        dataset_index=dataset_index,
        splits=splits,
        eval_mode=eval_mode,
        design=design,
        train_design=train_design,
        eval_design=eval_design,
        max_train_runs=args.max_train_runs,
        max_val_runs=args.max_val_runs,
        max_test_runs=args.max_test_runs,
    )
    if not train_rows or not val_rows or not test_rows:
        raise SystemExit("Row selection returned empty split(s).")

    model_cfg = ckpt["model_cfg"]
    cell_vocab = ckpt.get("cell_type_vocab", {})
    if not isinstance(cell_vocab, dict):
        cell_vocab = {}
    stats = TripNormStats.from_dict(ckpt["normalization"])

    device = _resolve_device(args.device)
    model = TripathDualPassNet(
        pin_dim=int(model_cfg["pin_dim"]),
        cell_dim=int(model_cfg["cell_dim"]),
        net_dim=int(model_cfg["net_dim"]),
        global_dim=int(model_cfg["global_dim"]),
        hidden_dim=int(model_cfg["hidden_dim"]),
        message_steps=int(model_cfg["message_steps"]),
        dropout=float(model_cfg["dropout"]),
        out_dim=int(model_cfg["out_dim"]),
        cell_type_vocab_size=int(model_cfg.get("cell_type_vocab_size", len(cell_vocab))),
        cell_emb_dim=int(model_cfg.get("cell_emb_dim", 0)),
        edge_attr_dim=int(model_cfg.get("edge_attr_dim", len(EDGE_NUMERIC_COLS) + 2)),
    ).to(device)
    model.load_state_dict(ckpt["model_state"])

    train_graphs = [load_trip_graph(r, target_cols, max_paths=max_paths) for r in train_rows]
    val_graphs = [load_trip_graph(r, target_cols, max_paths=max_paths) for r in val_rows]
    test_graphs = [load_trip_graph(r, target_cols, max_paths=max_paths) for r in test_rows]

    if int(model_cfg.get("cell_emb_dim", 0)) > 0:
        if not cell_vocab:
            raise RuntimeError("Checkpoint requires cell embeddings, but no cell_type_vocab found.")
        apply_cell_type_encoding(train_graphs, cell_vocab)
        apply_cell_type_encoding(val_graphs, cell_vocab)
        apply_cell_type_encoding(test_graphs, cell_vocab)

    apply_norm(train_graphs, stats, arrival_idx=arrival_idx, slack_idx=slack_idx)
    apply_norm(val_graphs, stats, arrival_idx=arrival_idx, slack_idx=slack_idx)
    apply_norm(test_graphs, stats, arrival_idx=arrival_idx, slack_idx=slack_idx)

    train_metrics = evaluate_model(
        model=model,
        graphs=train_graphs,
        stats=stats,
        device=device,
        batch_graphs=batch_graphs,
        loss_nodes_per_graph=loss_nodes_per_graph_eval,
        primary_idx=primary_idx,
        arrival_idx=arrival_idx,
        slack_idx=slack_idx,
        seed=args.seed + 1,
    )
    val_metrics = evaluate_model(
        model=model,
        graphs=val_graphs,
        stats=stats,
        device=device,
        batch_graphs=batch_graphs,
        loss_nodes_per_graph=loss_nodes_per_graph_eval,
        primary_idx=primary_idx,
        arrival_idx=arrival_idx,
        slack_idx=slack_idx,
        seed=args.seed + 3,
    )
    test_metrics = evaluate_model(
        model=model,
        graphs=test_graphs,
        stats=stats,
        device=device,
        batch_graphs=batch_graphs,
        loss_nodes_per_graph=loss_nodes_per_graph_eval,
        primary_idx=primary_idx,
        arrival_idx=arrival_idx,
        slack_idx=slack_idx,
        seed=args.seed + 5,
    )

    print(f"eval={eval_tag}")
    print(f"checkpoint={ckpt_path}")
    _print_block("train", train_metrics)
    _print_block("val", val_metrics)
    _print_block("test", test_metrics)

    payload = {
        "eval_tag": eval_tag,
        "checkpoint": str(ckpt_path.resolve()),
        "target_cols": target_cols,
        "primary_target_idx": primary_idx,
        "arrival_idx": arrival_idx,
        "slack_idx": slack_idx,
        "train_metrics": train_metrics,
        "val_metrics": val_metrics,
        "test_metrics": test_metrics,
        "selected": {
            "train_ids": [r["run_id"] for r in train_rows],
            "val_ids": [r["run_id"] for r in val_rows],
            "test_ids": [r["run_id"] for r in test_rows],
        },
    }

    if args.save_json:
        out = Path(args.save_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
