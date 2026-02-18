#!/usr/bin/env python3
"""Run scenario-ranker holdout battery and aggregate verification metrics."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _read_designs(manifest: Path) -> List[str]:
    with manifest.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return sorted({str(r.get("design", "")).strip() for r in rows if str(r.get("design", "")).strip()})


def _run(cmd: List[str], cwd: Path) -> None:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run holdout battery for train_scenario_ranker.py")
    ap.add_argument("--manifest", default="data/manifests/scenario_dominance_replay.csv")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--designs", default="", help="Comma-separated holdout designs (default: all in manifest)")
    ap.add_argument("--mode", choices=["scenario_only", "graph", "both"], default="scenario_only")
    ap.add_argument("--top-k", type=int, default=2)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--eval-every", type=int, default=1)
    ap.add_argument("--early-stop-patience", type=int, default=10)
    ap.add_argument("--max-nodes-per-graph", type=int, default=2048)
    ap.add_argument("--max-edges-per-graph", type=int, default=20000)
    ap.add_argument("--graph-cache-size", type=int, default=256)
    ap.add_argument("--out-dir", default="results/scenario_ranker_runs")
    ap.add_argument("--summary-json", default="")
    args = ap.parse_args()

    repo = Path(__file__).resolve().parents[1]
    manifest = (repo / args.manifest).resolve()
    dataset_index = (repo / args.dataset_index).resolve()
    out_dir = (repo / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.designs.strip():
        designs = [d.strip() for d in args.designs.split(",") if d.strip()]
    else:
        designs = _read_designs(manifest)
    if not designs:
        raise SystemExit("No holdout designs found")

    modes: List[str]
    if args.mode == "both":
        modes = ["scenario_only", "graph"]
    else:
        modes = [args.mode]

    stamp = _utc_stamp()
    results: List[Dict[str, object]] = []
    for mode in modes:
        for design in designs:
            run_name = f"scenario_ranker_{mode}_{design}_{stamp}"
            cmd = [
                sys.executable,
                "scripts/train_scenario_ranker.py",
                "--manifest",
                str(manifest),
                "--dataset-index",
                str(dataset_index),
                "--holdout-design",
                design,
                "--top-k",
                str(args.top_k),
                "--epochs",
                str(args.epochs),
                "--eval-every",
                str(args.eval_every),
                "--early-stop-patience",
                str(args.early_stop_patience),
                "--run-name",
                run_name,
            ]
            if mode == "scenario_only":
                cmd.append("--disable-graph")
            else:
                cmd.extend(
                    [
                        "--max-nodes-per-graph",
                        str(args.max_nodes_per_graph),
                        "--max-edges-per-graph",
                        str(args.max_edges_per_graph),
                        "--graph-cache-size",
                        str(args.graph_cache_size),
                    ]
                )
            print("running:", " ".join(cmd))
            _run(cmd, repo)

            summary_path = out_dir / run_name / "summary.json"
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
            metrics = payload.get("best_test_metrics", {})
            results.append(
                {
                    "mode": mode,
                    "holdout_design": design,
                    "run_name": run_name,
                    "best_epoch": payload.get("best_epoch"),
                    "best_val_top1": payload.get("best_val_top1"),
                    "best_val_mrr": payload.get("best_val_mrr"),
                    "test_top1": metrics.get("top1"),
                    "test_topk": metrics.get("topk"),
                    "test_mrr": metrics.get("mrr"),
                    "test_missed_violation_rate": metrics.get("missed_violation_rate"),
                    "test_worst_regret_ps_p95": metrics.get("worst_regret_ps_p95"),
                    "test_worst_regret_ps_max": metrics.get("worst_regret_ps_max"),
                    "summary_json": str(summary_path),
                }
            )

    by_mode: Dict[str, Dict[str, float]] = {}
    for mode in modes:
        rows = [r for r in results if r["mode"] == mode]
        if not rows:
            continue
        n = float(len(rows))
        by_mode[mode] = {
            "num_designs": len(rows),
            "avg_test_top1": sum(float(r["test_top1"]) for r in rows) / n,
            "avg_test_topk": sum(float(r["test_topk"]) for r in rows) / n,
            "avg_test_mrr": sum(float(r["test_mrr"]) for r in rows) / n,
            "avg_test_missed_violation_rate": sum(float(r["test_missed_violation_rate"]) for r in rows) / n,
            "avg_test_worst_regret_ps_p95": sum(float(r["test_worst_regret_ps_p95"]) for r in rows) / n,
            "max_test_worst_regret_ps_max": max(float(r["test_worst_regret_ps_max"]) for r in rows),
        }

    summary = {
        "manifest": str(manifest),
        "dataset_index": str(dataset_index),
        "modes": modes,
        "designs": designs,
        "runs": results,
        "aggregates": by_mode,
    }

    if args.summary_json.strip():
        out = Path(args.summary_json)
        if not out.is_absolute():
            out = repo / out
    else:
        out = out_dir / f"verification_battery_{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
