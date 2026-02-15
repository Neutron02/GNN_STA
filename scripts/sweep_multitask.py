#!/usr/bin/env python3
"""Run multi-task training sweeps and generate a leaderboard."""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from pipeline_common import ensure_dir, load_json, repo_root


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _cli_kv(key: str, value) -> List[str]:
    flag = f"--{key.replace('_', '-')}"
    if value is None:
        return []
    if isinstance(value, bool):
        return [flag] if value else []
    if isinstance(value, (list, tuple)):
        return [flag, ",".join(str(v) for v in value)]
    return [flag, str(value)]


def _flatten_trial_args(base_args: Dict[str, object], trial_args: Dict[str, object]) -> Dict[str, object]:
    out = dict(base_args)
    out.update(trial_args)
    return out


def _summary_metrics(summary_path: Path) -> Dict[str, float]:
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    m = payload.get("best_test_metrics", {})
    out = {}
    for key in ("norm_mse", "rmse_ps", "mae_ps", "wns_mae_ps", "wns_rmse_ps"):
        if key in m:
            out[f"test_{key}"] = float(m[key])
    out["best_epoch"] = int(payload.get("best_epoch", 0))
    out["best_val_norm_mse"] = float(payload.get("best_val_norm_mse", 0.0))
    return out


def _read_existing_runs(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {row["trial_name"]: row for row in rows}


@dataclass
class TrialSpec:
    trial_name: str
    run_name: str
    args: Dict[str, object]


@dataclass
class TrialResult:
    trial_name: str
    run_name: str
    status: str
    return_code: int
    started_utc: str
    ended_utc: str
    duration_sec: float
    summary_path: str
    log_path: str
    error: str
    metrics: Dict[str, float]


def _run_trial(
    train_script: Path,
    out_dir: Path,
    logs_dir: Path,
    ts: TrialSpec,
    dry_run: bool,
) -> TrialResult:
    ensure_dir(logs_dir)
    summary_path = out_dir / ts.run_name / "summary.json"
    log_path = logs_dir / f"{ts.run_name}.log"
    started = _utc_now()
    t0 = time.time()

    cmd = [sys.executable, str(train_script)]
    for key, value in ts.args.items():
        cmd.extend(_cli_kv(key, value))
    cmd.extend(["--run-name", ts.run_name])

    if dry_run:
        ended = _utc_now()
        return TrialResult(
            trial_name=ts.trial_name,
            run_name=ts.run_name,
            status="dry_run",
            return_code=0,
            started_utc=started,
            ended_utc=ended,
            duration_sec=0.0,
            summary_path=str(summary_path),
            log_path=str(log_path),
            error="",
            metrics={},
        )

    with log_path.open("w", encoding="utf-8") as lf:
        lf.write(" ".join(cmd) + "\n\n")
        lf.flush()
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root()),
            stdout=lf,
            stderr=subprocess.STDOUT,
            check=False,
        )

    ended = _utc_now()
    duration = time.time() - t0
    if proc.returncode != 0:
        return TrialResult(
            trial_name=ts.trial_name,
            run_name=ts.run_name,
            status="failed",
            return_code=int(proc.returncode),
            started_utc=started,
            ended_utc=ended,
            duration_sec=duration,
            summary_path=str(summary_path),
            log_path=str(log_path),
            error=f"train exited with code {proc.returncode}",
            metrics={},
        )

    if not summary_path.exists():
        return TrialResult(
            trial_name=ts.trial_name,
            run_name=ts.run_name,
            status="failed",
            return_code=0,
            started_utc=started,
            ended_utc=ended,
            duration_sec=duration,
            summary_path=str(summary_path),
            log_path=str(log_path),
            error="summary.json missing",
            metrics={},
        )

    metrics = _summary_metrics(summary_path)
    return TrialResult(
        trial_name=ts.trial_name,
        run_name=ts.run_name,
        status="success",
        return_code=0,
        started_utc=started,
        ended_utc=ended,
        duration_sec=duration,
        summary_path=str(summary_path),
        log_path=str(log_path),
        error="",
        metrics=metrics,
    )


def _write_runs_csv(path: Path, results: List[TrialResult]) -> None:
    fieldnames = [
        "trial_name",
        "run_name",
        "status",
        "return_code",
        "started_utc",
        "ended_utc",
        "duration_sec",
        "summary_path",
        "log_path",
        "error",
        "best_epoch",
        "best_val_norm_mse",
        "test_norm_mse",
        "test_rmse_ps",
        "test_mae_ps",
        "test_wns_mae_ps",
        "test_wns_rmse_ps",
    ]
    ensure_dir(path.parent)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            row = {
                "trial_name": r.trial_name,
                "run_name": r.run_name,
                "status": r.status,
                "return_code": r.return_code,
                "started_utc": r.started_utc,
                "ended_utc": r.ended_utc,
                "duration_sec": f"{r.duration_sec:.3f}",
                "summary_path": r.summary_path,
                "log_path": r.log_path,
                "error": r.error,
            }
            row.update({k: r.metrics.get(k, "") for k in fieldnames if k.startswith("best_") or k.startswith("test_")})
            w.writerow(row)


def _write_leaderboard(
    path: Path,
    results: List[TrialResult],
    trial_specs: Dict[str, TrialSpec],
    metric: str,
    maximize: bool,
) -> None:
    rows: List[Dict[str, object]] = []
    for r in results:
        if r.status != "success":
            continue
        row = {
            "trial_name": r.trial_name,
            "run_name": r.run_name,
            "metric": r.metrics.get(metric, float("inf")),
            "best_val_norm_mse": r.metrics.get("best_val_norm_mse", ""),
            "test_rmse_ps": r.metrics.get("test_rmse_ps", ""),
            "test_mae_ps": r.metrics.get("test_mae_ps", ""),
            "test_wns_mae_ps": r.metrics.get("test_wns_mae_ps", ""),
            "test_norm_mse": r.metrics.get("test_norm_mse", ""),
            "duration_sec": f"{r.duration_sec:.3f}",
        }
        for k, v in trial_specs[r.trial_name].args.items():
            row[f"arg_{k}"] = v
        rows.append(row)

    rows.sort(key=lambda x: float(x["metric"]), reverse=bool(maximize))
    for i, row in enumerate(rows, start=1):
        row["rank"] = i

    if rows:
        fieldnames = ["rank"] + [k for k in rows[0].keys() if k != "rank"]
    else:
        fieldnames = ["rank", "trial_name", "run_name", "metric"]

    ensure_dir(path.parent)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in rows:
            w.writerow(row)

    summary = {
        "generated_utc": _utc_now(),
        "metric": metric,
        "maximize": maximize,
        "num_success": len(rows),
        "top": rows[:5],
    }
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run multi-task training sweep and build leaderboard")
    ap.add_argument("--config", required=True, help="Path to sweep config JSON")
    ap.add_argument("--jobs", type=int, default=0, help="Override parallel trial jobs")
    ap.add_argument("--resume", action="store_true", help="Skip trials already successful in prior runs.csv")
    ap.add_argument("--dry-run", action="store_true", help="Print planned work without launching training")
    ap.add_argument("--metric", default="", help="Override leaderboard metric key")
    ap.add_argument("--maximize", action="store_true", help="Sort metric descending")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = load_json(cfg_path)

    sweep_name = str(cfg.get("sweep_name", cfg_path.stem))
    jobs = int(args.jobs) if args.jobs > 0 else int(cfg.get("jobs", 1))
    metric = args.metric.strip() or str(cfg.get("leaderboard_metric", "test_rmse_ps"))
    maximize = bool(args.maximize or cfg.get("leaderboard_maximize", False))

    out_root = repo_root() / "results" / "sweeps" / sweep_name
    runs_csv = out_root / "runs.csv"
    leaderboard_csv = out_root / "leaderboard.csv"
    logs_dir = out_root / "logs"
    ensure_dir(out_root)
    ensure_dir(logs_dir)

    train_script_rel = str(cfg.get("train_script", "scripts/train_gnn_multitask.py"))
    train_script = repo_root() / train_script_rel
    if not train_script.exists():
        raise SystemExit(f"Training script missing: {train_script}")

    out_dir = repo_root() / str(cfg.get("train_out_dir", "results/train_runs"))
    ensure_dir(out_dir)

    base_args = dict(cfg.get("base_args", {}))
    trials = cfg.get("trials", [])
    if not isinstance(trials, list) or not trials:
        raise SystemExit("Config must include a non-empty 'trials' list.")

    existing = _read_existing_runs(runs_csv) if args.resume else {}

    trial_specs: Dict[str, TrialSpec] = {}
    plan: List[TrialSpec] = []
    for t in trials:
        trial_name = str(t["name"])
        t_args = dict(t.get("args", {}))
        merged_args = _flatten_trial_args(base_args, t_args)
        run_name = str(merged_args.pop("run_name", f"{sweep_name}__{trial_name}"))
        spec = TrialSpec(trial_name=trial_name, run_name=run_name, args=merged_args)
        trial_specs[trial_name] = spec

        if args.resume:
            prev = existing.get(trial_name)
            if prev and prev.get("status") == "success":
                continue
            if (out_dir / run_name / "summary.json").exists():
                continue
        plan.append(spec)

    print(f"sweep={sweep_name} total_trials={len(trials)} to_run={len(plan)} jobs={jobs}", flush=True)
    if args.dry_run:
        for spec in plan:
            print(f"dry_run trial={spec.trial_name} run={spec.run_name}", flush=True)
        return

    results: List[TrialResult] = []
    if args.resume and existing:
        for trial_name, row in existing.items():
            spec = trial_specs.get(trial_name)
            if spec is None:
                continue
            metrics = {}
            for k in ("best_epoch", "best_val_norm_mse", "test_norm_mse", "test_rmse_ps", "test_mae_ps", "test_wns_mae_ps", "test_wns_rmse_ps"):
                v = row.get(k, "")
                if v == "":
                    continue
                try:
                    metrics[k] = float(v)
                except ValueError:
                    pass
            results.append(
                TrialResult(
                    trial_name=trial_name,
                    run_name=row.get("run_name", spec.run_name),
                    status=row.get("status", "unknown"),
                    return_code=int(row.get("return_code", "0") or 0),
                    started_utc=row.get("started_utc", ""),
                    ended_utc=row.get("ended_utc", ""),
                    duration_sec=float(row.get("duration_sec", "0") or 0.0),
                    summary_path=row.get("summary_path", ""),
                    log_path=row.get("log_path", ""),
                    error=row.get("error", ""),
                    metrics=metrics,
                )
            )

    if plan:
        with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
            futs = [
                ex.submit(
                    _run_trial,
                    train_script,
                    out_dir,
                    logs_dir,
                    spec,
                    args.dry_run,
                )
                for spec in plan
            ]
            for fut in as_completed(futs):
                r = fut.result()
                results.append(r)
                print(
                    f"trial={r.trial_name} status={r.status} rc={r.return_code} "
                    f"dur={r.duration_sec:.1f}s test_rmse_ps={r.metrics.get('test_rmse_ps', 'NA')}",
                    flush=True,
                )

    # Keep one row per trial_name, latest result wins.
    dedup: Dict[str, TrialResult] = {}
    for r in results:
        dedup[r.trial_name] = r
    final_results = [dedup[name] for name in sorted(dedup.keys())]

    _write_runs_csv(runs_csv, final_results)
    _write_leaderboard(leaderboard_csv, final_results, trial_specs, metric=metric, maximize=maximize)

    success = sum(1 for r in final_results if r.status == "success")
    failed = sum(1 for r in final_results if r.status == "failed")
    print(f"completed success={success} failed={failed} runs_csv={runs_csv}", flush=True)
    print(f"leaderboard={leaderboard_csv}", flush=True)


if __name__ == "__main__":
    main()
