#!/usr/bin/env python3
"""Train a PyTorch message-passing model for timing prediction."""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from ml_models import TimingMPNN, require_torch as require_torch_models
from ml_training_common import (
    apply_normalization,
    compute_normalization_stats,
    evaluate_model,
    iter_batches,
    load_dataset_index,
    load_graph,
    load_splits,
    require_torch,
    select_rows,
)

try:
    import torch
    import torch.nn.functional as F
except Exception:  # pragma: no cover
    torch = None
    F = None


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
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def main() -> None:
    ap = argparse.ArgumentParser(description="Train timing GNN")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--splits", default="data/manifests/splits.json")
    ap.add_argument("--eval-mode", choices=["within_design", "holdout_design"], default="within_design")
    ap.add_argument("--design", default="gcd")
    ap.add_argument("--train-design", default="gcd")
    ap.add_argument("--eval-design", default="aes")
    ap.add_argument("--target-col", default="slack_setup_scalar_s")
    ap.add_argument("--target-scale", type=float, default=1e12)

    ap.add_argument("--max-train-runs", type=int, default=0)
    ap.add_argument("--max-val-runs", type=int, default=0)
    ap.add_argument("--max-test-runs", type=int, default=0)
    ap.add_argument("--batch-graphs", type=int, default=2)
    ap.add_argument("--loss-nodes-per-graph-train", type=int, default=2048)
    ap.add_argument("--loss-nodes-per-graph-eval", type=int, default=0)

    ap.add_argument("--hidden-dim", type=int, default=128)
    ap.add_argument("--message-steps", type=int, default=3)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-5)
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--early-stop-patience", type=int, default=10)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")

    ap.add_argument("--out-dir", default="results/train_runs")
    ap.add_argument("--run-name", default="")
    args = ap.parse_args()

    require_torch()
    require_torch_models()

    if torch is None or F is None:
        raise RuntimeError("PyTorch is unavailable in this Python environment.")

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

    if not train_rows:
        raise SystemExit("No train runs selected.")
    if not val_rows:
        raise SystemExit("No val runs selected.")
    if not test_rows:
        raise SystemExit("No test runs selected.")

    print(f"eval={eval_tag}")
    print(
        f"runs train={len(train_rows)} val={len(val_rows)} test={len(test_rows)} "
        f"target={args.target_col}"
    )

    train_graphs = [load_graph(r, args.target_col) for r in train_rows]
    val_graphs = [load_graph(r, args.target_col) for r in val_rows]
    test_graphs = [load_graph(r, args.target_col) for r in test_rows]

    stats = compute_normalization_stats(train_graphs, args.target_scale)
    apply_normalization(train_graphs, stats)
    apply_normalization(val_graphs, stats)
    apply_normalization(test_graphs, stats)

    node_dim = int(train_graphs[0].x.size(1))
    edge_dim = int(train_graphs[0].edge_attr.size(1))
    model = TimingMPNN(
        node_dim=node_dim,
        edge_dim=edge_dim,
        hidden_dim=args.hidden_dim,
        message_steps=args.message_steps,
        dropout=args.dropout,
    ).to(device)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    run_name = args.run_name.strip() or f"timing_mpnn_{_utc_stamp()}"
    run_dir = Path(args.out_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    config_payload = {
        "run_name": run_name,
        "eval_tag": eval_tag,
        "args": vars(args),
        "train_ids": [r["run_id"] for r in train_rows],
        "val_ids": [r["run_id"] for r in val_rows],
        "test_ids": [r["run_id"] for r in test_rows],
        "node_dim": node_dim,
        "edge_dim": edge_dim,
        "model": {
            "hidden_dim": args.hidden_dim,
            "message_steps": args.message_steps,
            "dropout": args.dropout,
        },
        "normalization": stats.to_dict(),
    }
    _write_json(run_dir / "config.json", config_payload)

    best_val = float("inf")
    best_epoch = 0
    bad_epochs = 0
    epoch_rows: List[Dict[str, object]] = []

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
            x = batch.x.to(device)
            edge_index = batch.edge_index.to(device)
            edge_attr = batch.edge_attr.to(device)
            y_norm = batch.y_norm.to(device)
            loss_idx = batch.loss_idx.to(device)

            optimizer.zero_grad(set_to_none=True)
            pred = model(x, edge_index, edge_attr)
            loss = F.mse_loss(pred[loss_idx], y_norm[loss_idx])
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
            seed=args.seed + epoch * 11 + 1,
        )
        val_metrics = evaluate_model(
            model=model,
            graphs=val_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
            seed=args.seed + epoch * 11 + 3,
        )
        test_metrics = evaluate_model(
            model=model,
            graphs=test_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
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
        }
        epoch_rows.append(row)

        print(
            f"epoch={epoch:03d} "
            f"train_loss={train_loss:.6e} "
            f"val_norm_mse={val_metrics['norm_mse']:.6e} "
            f"val_rmse_ps={val_metrics['rmse_ps']:.3f} "
            f"test_rmse_ps={test_metrics['rmse_ps']:.3f}"
        )

        ckpt_payload = {
            "epoch": epoch,
            "model_state": model.state_dict(),
            "model_cfg": config_payload["model"] | {"node_dim": node_dim, "edge_dim": edge_dim},
            "normalization": stats.to_dict(),
            "args": vars(args),
            "eval_tag": eval_tag,
            "train_ids": config_payload["train_ids"],
            "val_ids": config_payload["val_ids"],
            "test_ids": config_payload["test_ids"],
            "val_metrics": val_metrics,
            "test_metrics": test_metrics,
        }
        torch.save(ckpt_payload, run_dir / "last.pt")

        cur_val = float(val_metrics["norm_mse"])
        if cur_val < best_val:
            best_val = cur_val
            best_epoch = epoch
            bad_epochs = 0
            torch.save(ckpt_payload, run_dir / "best.pt")
        else:
            bad_epochs += 1

        if bad_epochs >= args.early_stop_patience:
            print(f"early_stop epoch={epoch} patience={args.early_stop_patience}")
            break

    _write_epoch_csv(run_dir / "epoch_metrics.csv", epoch_rows)

    best_ckpt = torch.load(run_dir / "best.pt", map_location=device)
    model.load_state_dict(best_ckpt["model_state"])
    best_test = evaluate_model(
        model=model,
        graphs=test_graphs,
        stats=stats,
        device=device,
        batch_graphs=args.batch_graphs,
        loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
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
