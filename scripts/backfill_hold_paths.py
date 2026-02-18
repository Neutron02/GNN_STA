#!/usr/bin/env python3
"""Backfill hold/min path summaries and hold metrics for existing processed runs.

This avoids rerunning synthesis/place/route (or graph extraction). It only runs:
- scripts/extract_paths_or.py --skip-max
and then updates global_features.json with hold/setup/combined metrics.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import math
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from pipeline_common import repo_root, resolve_path


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _load_json(path: Path) -> Dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _to_float(v: object) -> float:
    try:
        out = float(str(v).strip())
    except Exception:
        return float("nan")
    return out if math.isfinite(out) else float("nan")


def _slack_stats(csv_path: Path) -> Optional[Dict[str, float]]:
    if not csv_path.exists():
        return None
    slacks: List[float] = []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = _to_float(row.get("slack_s"))
            if math.isfinite(v):
                slacks.append(v)
    if not slacks:
        return None
    return {
        "count": float(len(slacks)),
        "wns_ns": min(slacks) * 1e9,
        "tns_ns": sum(v for v in slacks if v < 0.0) * 1e9,
    }


def _patch_global(processed_dir: Path) -> None:
    global_path = processed_dir / "global_features.json"
    payload = _load_json(global_path)

    setup = _slack_stats(processed_dir / "paths_summary.csv")
    hold = _slack_stats(processed_dir / "paths_hold_summary.csv")

    if setup is not None:
        payload["wns_ns"] = setup["wns_ns"]  # legacy setup alias
        payload["tns_ns"] = setup["tns_ns"]
        payload["worst_slack_ns"] = setup["wns_ns"]
        payload["wns_setup_ns"] = setup["wns_ns"]
        payload["tns_setup_ns"] = setup["tns_ns"]
        payload["setup_path_count"] = int(setup["count"])

    if hold is not None:
        payload["wns_hold_ns"] = hold["wns_ns"]
        payload["tns_hold_ns"] = hold["tns_ns"]
        payload["hold_path_count"] = int(hold["count"])

    setup_wns = _to_float(payload.get("wns_setup_ns"))
    hold_wns = _to_float(payload.get("wns_hold_ns"))
    vals = [v for v in [setup_wns, hold_wns] if math.isfinite(v)]
    if vals:
        payload["wns_combined_ns"] = min(vals)
        if len(vals) == 2:
            payload["dominant_check"] = "setup" if setup_wns <= hold_wns else "hold"

    global_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _run_hold_extract(
    run_id: str,
    raw_dir: Path,
    processed_dir: Path,
    openroad_bin: str,
    liberty: str,
    group_path_count: int,
    endpoint_path_count: int,
) -> None:
    cmd = [
        openroad_bin,
        "-python",
        "-no_init",
        "-exit",
        "scripts/extract_paths_or.py",
        "--odb",
        str(raw_dir / "6_final.odb"),
        "--sdc",
        str(raw_dir / "6_final.sdc"),
        "--spef",
        str(raw_dir / "6_final.spef"),
        "--liberty",
        liberty,
        "--run-id",
        run_id,
        "--out-dir",
        str(processed_dir),
        "--group-path-count",
        str(group_path_count),
        "--endpoint-path-count",
        str(endpoint_path_count),
        "--skip-max",
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill hold path summaries for existing dataset runs.")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--openroad-bin", default="OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad")
    ap.add_argument("--liberty", default="OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib")
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-runs", type=int, default=0)
    ap.add_argument("--run-id", action="append", default=[], help="Optional run_id filter (repeatable).")
    ap.add_argument("--group-path-count", type=int, default=200)
    ap.add_argument("--endpoint-path-count", type=int, default=1)
    args = ap.parse_args()

    root = repo_root()
    dataset_rows = _read_csv(resolve_path(args.dataset_index, root))
    run_filter = set(args.run_id)

    tasks: List[Dict[str, str]] = []
    for row in dataset_rows:
        run_id = str(row.get("run_id", "")).strip()
        if not run_id:
            continue
        if run_filter and run_id not in run_filter:
            continue
        status = str(row.get("status", "")).strip().lower()
        if status != "success":
            continue
        raw_dir = Path(str(row.get("raw_dir", "")).strip())
        proc_dir = Path(str(row.get("processed_dir", "")).strip())
        hold_csv = proc_dir / "paths_hold_summary.csv"
        global_json = proc_dir / "global_features.json"
        if args.resume and hold_csv.exists():
            payload = _load_json(global_json)
            if "wns_hold_ns" in payload and "wns_combined_ns" in payload:
                continue
        tasks.append({"run_id": run_id, "raw_dir": str(raw_dir), "processed_dir": str(proc_dir)})

    if args.max_runs > 0:
        tasks = tasks[: args.max_runs]

    print(f"Selected runs: {len(tasks)}")
    if not tasks:
        return

    openroad_bin = str(resolve_path(args.openroad_bin, root))
    liberty = str(resolve_path(args.liberty, root))

    lock = threading.Lock()
    done = 0
    ok = 0
    fail = 0
    t0 = time.time()

    def _status() -> str:
        elapsed = max(0.0, time.time() - t0)
        eta = None
        if done > 0 and len(tasks) > done:
            eta = (elapsed / done) * (len(tasks) - done)

        def _fmt(sec: Optional[float]) -> str:
            if sec is None:
                return "--:--:--"
            s = int(round(max(0.0, sec)))
            h, rem = divmod(s, 3600)
            m, ss = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{ss:02d}"

        return f"{done}/{len(tasks)} ok={ok} fail={fail} elapsed={_fmt(elapsed)} eta={_fmt(eta)}"

    print(_status())

    def worker(task: Dict[str, str]) -> Dict[str, str]:
        rid = task["run_id"]
        raw_dir = Path(task["raw_dir"])
        proc_dir = Path(task["processed_dir"])
        try:
            _run_hold_extract(
                run_id=rid,
                raw_dir=raw_dir,
                processed_dir=proc_dir,
                openroad_bin=openroad_bin,
                liberty=liberty,
                group_path_count=args.group_path_count,
                endpoint_path_count=args.endpoint_path_count,
            )
            _patch_global(proc_dir)
            return {"run_id": rid, "status": "success"}
        except Exception as exc:
            return {"run_id": rid, "status": "failed", "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(args.jobs))) as ex:
        futs = [ex.submit(worker, t) for t in tasks]
        for fut in concurrent.futures.as_completed(futs):
            res = fut.result()
            with lock:
                done += 1
                if res.get("status") == "success":
                    ok += 1
                else:
                    fail += 1
                print(f"{_status()} last={res.get('run_id')}:{res.get('status')}")

    print(json.dumps({"selected_runs": len(tasks), "success": ok, "failed": fail}, indent=2))


if __name__ == "__main__":
    main()
