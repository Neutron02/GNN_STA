#!/usr/bin/env python3
"""Evaluate a trained timing GNN checkpoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ml_models import TimingMPNN, require_torch as require_torch_models
from ml_training_common import (
    NormalizationStats,
    apply_normalization,
    evaluate_model,
    load_dataset_index,
    load_graph,
    load_splits,
    require_torch,
    select_rows,
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


def _print_block(name: str, m: dict) -> None:
    print(
        f"{name}: "
        f"norm_mse={m['norm_mse']:.6e} "
        f"rmse_ps={m['rmse_ps']:.3f} "
        f"mae_ps={m['mae_ps']:.3f} "
        f"wns_mae_ps={m['wns_mae_ps']:.3f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Evaluate trained timing GNN checkpoint")
    ap.add_argument("--checkpoint", required=True, help="Path to best.pt or last.pt")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--splits", default="data/manifests/splits.json")
    ap.add_argument("--eval-mode", choices=["within_design", "holdout_design"], default=None)
    ap.add_argument("--design", default=None)
    ap.add_argument("--train-design", default=None)
    ap.add_argument("--eval-design", default=None)
    ap.add_argument("--target-col", default=None)
    ap.add_argument("--max-train-runs", type=int, default=0)
    ap.add_argument("--max-val-runs", type=int, default=0)
    ap.add_argument("--max-test-runs", type=int, default=0)
    ap.add_argument("--batch-graphs", type=int, default=None)
    ap.add_argument("--loss-nodes-per-graph-eval", type=int, default=None)
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--save-json", default="")
    args = ap.parse_args()

    require_torch()
    require_torch_models()

    if torch is None:
        raise RuntimeError("PyTorch is unavailable in this Python environment.")

    ckpt_path = Path(args.checkpoint)
    ckpt = torch.load(ckpt_path, map_location="cpu")

    train_args = ckpt.get("args", {})
    eval_mode = args.eval_mode or train_args.get("eval_mode", "within_design")
    design = args.design or train_args.get("design", "gcd")
    train_design = args.train_design or train_args.get("train_design", "gcd")
    eval_design = args.eval_design or train_args.get("eval_design", "aes")
    target_col = args.target_col or train_args.get("target_col", "slack_setup_scalar_s")
    batch_graphs = args.batch_graphs or int(train_args.get("batch_graphs", 2))
    loss_nodes_per_graph_eval = (
        args.loss_nodes_per_graph_eval
        if args.loss_nodes_per_graph_eval is not None
        else int(train_args.get("loss_nodes_per_graph_eval", 0))
    )

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

    stats = NormalizationStats.from_dict(ckpt["normalization"])
    model_cfg = ckpt["model_cfg"]

    device = _resolve_device(args.device)
    model = TimingMPNN(
        node_dim=int(model_cfg["node_dim"]),
        edge_dim=int(model_cfg["edge_dim"]),
        hidden_dim=int(model_cfg["hidden_dim"]),
        message_steps=int(model_cfg["message_steps"]),
        dropout=float(model_cfg["dropout"]),
    ).to(device)
    model.load_state_dict(ckpt["model_state"])

    train_graphs = [load_graph(r, target_col) for r in train_rows]
    val_graphs = [load_graph(r, target_col) for r in val_rows]
    test_graphs = [load_graph(r, target_col) for r in test_rows]
    apply_normalization(train_graphs, stats)
    apply_normalization(val_graphs, stats)
    apply_normalization(test_graphs, stats)

    train_metrics = evaluate_model(
        model=model,
        graphs=train_graphs,
        stats=stats,
        device=device,
        batch_graphs=batch_graphs,
        loss_nodes_per_graph=loss_nodes_per_graph_eval,
        seed=args.seed + 1,
    )
    val_metrics = evaluate_model(
        model=model,
        graphs=val_graphs,
        stats=stats,
        device=device,
        batch_graphs=batch_graphs,
        loss_nodes_per_graph=loss_nodes_per_graph_eval,
        seed=args.seed + 3,
    )
    test_metrics = evaluate_model(
        model=model,
        graphs=test_graphs,
        stats=stats,
        device=device,
        batch_graphs=batch_graphs,
        loss_nodes_per_graph=loss_nodes_per_graph_eval,
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
