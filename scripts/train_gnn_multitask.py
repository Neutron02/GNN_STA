#!/usr/bin/env python3
"""Train a multi-task timing GNN (arrival, slack, required)."""

from __future__ import annotations

import argparse
import csv
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Sequence

from ml_models import TimingMPNN, require_torch as require_torch_models
from ml_training_common import (
    MultiNormalizationStats,
    apply_cell_type_encoding_multi,
    apply_normalization_multi,
    build_cell_type_vocab_multi,
    compute_normalization_stats_multi,
    denorm_predictions_to_sec_multi,
    evaluate_model_multi,
    iter_batches_multi,
    load_dataset_index,
    load_graph_multi,
    load_splits,
    require_torch,
    select_rows,
)

try:
    import torch
except Exception:  # pragma: no cover
    torch = None


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Train multi-task timing GNN")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--splits", default="data/manifests/splits.json")
    ap.add_argument("--eval-mode", choices=["within_design", "holdout_design"], default="within_design")
    ap.add_argument("--design", default="gcd")
    ap.add_argument("--train-design", default="gcd")
    ap.add_argument("--eval-design", default="aes")

    ap.add_argument(
        "--target-cols",
        default="arrival_setup_scalar_s,slack_setup_scalar_s,required_setup_scalar_s",
    )
    ap.add_argument("--primary-target-col", default="slack_setup_scalar_s")
    ap.add_argument("--target-weights", default="1.0,1.0,1.0")
    ap.add_argument("--arrival-col", default="arrival_setup_scalar_s")
    ap.add_argument("--slack-col", default="slack_setup_scalar_s")
    ap.add_argument("--required-col", default="required_setup_scalar_s")
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
    ap.add_argument("--cell-emb-dim", type=int, default=16)
    ap.add_argument("--cell-vocab-min-count", type=int, default=1)
    ap.add_argument("--disable-message-gate", action="store_true")
    ap.add_argument("--disable-layer-norm", action="store_true")
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-5)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--early-stop-patience", type=int, default=10)
    ap.add_argument("--consistency-weight", type=float, default=1e-4)
    ap.add_argument("--rank-loss-weight", type=float, default=0.02)
    ap.add_argument("--rank-pairs", type=int, default=1024)
    ap.add_argument("--rank-margin-norm", type=float, default=0.05)
    ap.add_argument("--critical-loss-weight", type=float, default=1.0)
    ap.add_argument("--critical-threshold-ps", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")

    ap.add_argument("--out-dir", default="results/train_runs")
    ap.add_argument("--run-name", default="")
    args = ap.parse_args()

    require_torch()
    require_torch_models()
    if torch is None:
        raise RuntimeError("PyTorch is unavailable in this Python environment.")

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

    train_graphs = [load_graph_multi(r, target_cols) for r in train_rows]
    val_graphs = [load_graph_multi(r, target_cols) for r in val_rows]
    test_graphs = [load_graph_multi(r, target_cols) for r in test_rows]

    cell_vocab: Dict[str, int] = {}
    if args.cell_emb_dim > 0:
        cell_vocab = build_cell_type_vocab_multi(train_graphs, min_count=args.cell_vocab_min_count)
        apply_cell_type_encoding_multi(train_graphs, cell_vocab)
        apply_cell_type_encoding_multi(val_graphs, cell_vocab)
        apply_cell_type_encoding_multi(test_graphs, cell_vocab)
        print(f"cell_embedding enabled dim={args.cell_emb_dim} vocab={len(cell_vocab)}")
    else:
        print("cell_embedding disabled")

    stats = compute_normalization_stats_multi(train_graphs, args.target_scale)
    apply_normalization_multi(train_graphs, stats)
    apply_normalization_multi(val_graphs, stats)
    apply_normalization_multi(test_graphs, stats)

    node_dim = int(train_graphs[0].x.size(1))
    edge_dim = int(train_graphs[0].edge_attr.size(1))
    out_dim = len(target_cols)
    model = TimingMPNN(
        node_dim=node_dim,
        edge_dim=edge_dim,
        hidden_dim=args.hidden_dim,
        message_steps=args.message_steps,
        dropout=args.dropout,
        cell_type_vocab_size=len(cell_vocab),
        cell_emb_dim=(args.cell_emb_dim if args.cell_emb_dim > 0 else 0),
        use_message_gate=(not args.disable_message_gate),
        use_layer_norm=(not args.disable_layer_norm),
        out_dim=out_dim,
    ).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    target_w_t = torch.tensor(target_weights, dtype=torch.float32, device=device)

    run_name = args.run_name.strip() or f"timing_mpnn_mt_{_utc_stamp()}"
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
        "node_dim": node_dim,
        "edge_dim": edge_dim,
        "model": {
            "hidden_dim": args.hidden_dim,
            "message_steps": args.message_steps,
            "dropout": args.dropout,
            "cell_emb_dim": (args.cell_emb_dim if args.cell_emb_dim > 0 else 0),
            "cell_type_vocab_size": len(cell_vocab),
            "use_message_gate": (not args.disable_message_gate),
            "use_layer_norm": (not args.disable_layer_norm),
            "out_dim": out_dim,
        },
        "cell_type_vocab": cell_vocab,
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

        for batch in iter_batches_multi(
            graphs=train_graphs,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_train,
            shuffle=True,
            rng=train_rng,
        ):
            x = batch.x.to(device)
            edge_index = batch.edge_index.to(device)
            edge_attr = batch.edge_attr.to(device)
            cell_type_idx = None
            if batch.cell_type_idx is not None:
                cell_type_idx = batch.cell_type_idx.to(device)
            y_sec = batch.y_sec.to(device)
            y_norm = batch.y_norm.to(device)
            loss_idx = batch.loss_idx.to(device)

            optimizer.zero_grad(set_to_none=True)
            pred_norm = model(x, edge_index, edge_attr, cell_type_idx=cell_type_idx)
            pred_loss = pred_norm[loss_idx]
            y_loss = y_norm[loss_idx]
            diff = pred_loss - y_loss

            per_target_losses: List[torch.Tensor] = []
            for j in range(out_dim):
                if j == primary_idx and args.critical_loss_weight > 1.0:
                    crit_thr_sec = float(args.critical_threshold_ps) * 1e-12
                    crit = (y_sec[loss_idx, primary_idx] <= crit_thr_sec).float()
                    weights = 1.0 + (float(args.critical_loss_weight) - 1.0) * crit
                    sq = diff[:, j].pow(2)
                    tloss = (weights * sq).sum() / weights.sum().clamp(min=1.0)
                else:
                    tloss = diff[:, j].pow(2).mean()
                per_target_losses.append(tloss)
            per_target_t = torch.stack(per_target_losses, dim=0)
            loss = (per_target_t * target_w_t).sum() / target_w_t.sum().clamp(min=1e-12)

            if args.consistency_weight > 0.0:
                pred_sec = denorm_predictions_to_sec_multi(pred_norm, stats)
                cons = pred_sec[:, required_idx] - (
                    pred_sec[:, arrival_idx] + pred_sec[:, slack_idx]
                )
                cons_ps = cons[loss_idx] * 1e12
                loss = loss + float(args.consistency_weight) * (cons_ps.pow(2).mean())

            if args.rank_loss_weight > 0.0:
                rank_loss = _pairwise_rank_loss(
                    pred=pred_loss[:, primary_idx],
                    truth=y_loss[:, primary_idx],
                    pairs=int(args.rank_pairs),
                    margin=float(args.rank_margin_norm),
                )
                loss = loss + float(args.rank_loss_weight) * rank_loss

            loss.backward()
            optimizer.step()

            n = int(loss_idx.numel())
            total_loss += float(loss.item()) * n
            total_nodes += n

        train_loss = total_loss / max(1, total_nodes)

        train_metrics = evaluate_model_multi(
            model=model,
            graphs=train_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
            primary_idx=primary_idx,
            seed=args.seed + epoch * 11 + 1,
        )
        val_metrics = evaluate_model_multi(
            model=model,
            graphs=val_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
            primary_idx=primary_idx,
            seed=args.seed + epoch * 11 + 3,
        )
        test_metrics = evaluate_model_multi(
            model=model,
            graphs=test_graphs,
            stats=stats,
            device=device,
            batch_graphs=args.batch_graphs,
            loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
            primary_idx=primary_idx,
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
    best_test = evaluate_model_multi(
        model=model,
        graphs=test_graphs,
        stats=stats,
        device=device,
        batch_graphs=args.batch_graphs,
        loss_nodes_per_graph=args.loss_nodes_per_graph_eval,
        primary_idx=primary_idx,
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
