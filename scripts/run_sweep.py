#!/usr/bin/env python3
"""Run ORFS sweeps and build timing graph datasets for phase-1."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import subprocess
import threading
from pathlib import Path
from typing import Dict, List

from pipeline_common import (
    generated_sdc_path,
    load_json,
    planned_runs_from_config,
    repo_root,
    resolve_path,
    run_paths,
    utc_now_iso,
)

RUNS_FIELDS = [
    "run_id",
    "design",
    "clock_scale",
    "clock_period_ns",
    "abc_area",
    "place_density",
    "routing_layer_adjustment",
    "variant",
    "sdc_file",
    "status",
    "last_error",
    "last_update_utc",
]


def read_runs_csv(path: Path) -> Dict[str, Dict[str, str]]:
    if not path.exists():
        return {}
    with path.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {row["run_id"]: row for row in rows}


def write_runs_csv(path: Path, ordered_rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RUNS_FIELDS)
        writer.writeheader()
        for row in ordered_rows:
            out = {k: row.get(k, "") for k in RUNS_FIELDS}
            if out["routing_layer_adjustment"] is None:
                out["routing_layer_adjustment"] = ""
            writer.writerow(out)


def run_cmd(cmd: List[str], log_file: Path, cwd: Path, dry_run: bool) -> None:
    with log_file.open("a", encoding="utf-8") as lf:
        lf.write("\n$ " + " ".join(cmd) + "\n")
    if dry_run:
        return
    with log_file.open("a", encoding="utf-8") as lf:
        subprocess.run(cmd, cwd=str(cwd), check=True, stdout=lf, stderr=subprocess.STDOUT)


def command_for_make_flow(root: Path, config: Dict, run: Dict, sdc_path: Path, num_cores: int) -> List[str]:
    orfs_flow = resolve_path(str(config["orfs_flow_dir"]), root)
    platform = config["platform"]
    cmd = [
        "make",
        "-C",
        str(orfs_flow),
        f"DESIGN_CONFIG=./designs/{platform}/{run['design']}/config.mk",
        f"FLOW_VARIANT={run['variant']}",
        f"SDC_FILE={sdc_path}",
        f"ABC_AREA={run['abc_area']}",
        f"PLACE_DENSITY={run['place_density']:.2f}",
        f"NUM_CORES={num_cores}",
    ]
    if run.get("routing_layer_adjustment") is not None:
        cmd.append(f"ROUTING_LAYER_ADJUSTMENT={run['routing_layer_adjustment']:.2f}")
    # Build through final report target without invoking finish/GDS merge.
    report_target = f"logs/{platform}/{run['design']}/{run['variant']}/6_report.log"
    final_sdc_target = f"results/{platform}/{run['design']}/{run['variant']}/6_final.sdc"
    cmd.extend([report_target, final_sdc_target])
    return cmd


def command_for_make_net_rc(root: Path, config: Dict, run: Dict, num_cores: int) -> List[str]:
    orfs_flow = resolve_path(str(config["orfs_flow_dir"]), root)
    platform = config["platform"]
    return [
        "make",
        "-C",
        str(orfs_flow),
        f"DESIGN_CONFIG=./designs/{platform}/{run['design']}/config.mk",
        f"FLOW_VARIANT={run['variant']}",
        "write_net_rc",
        f"NUM_CORES={num_cores}",
    ]


def check_success_artifacts(config: Dict, run: Dict, root: Path) -> bool:
    paths = run_paths(config["platform"], run["design"], run["variant"], root)
    results = paths["results_dir"]
    reports = paths["reports_dir"]
    needed = [
        results / "6_final.odb",
        results / "6_final.def",
        results / "6_final.v",
        results / "6_final.sdc",
        results / "6_final.spef",
        results / "6_net_rc.csv",
        reports / "6_finish.rpt",
    ]
    return all(p.exists() for p in needed)


def check_processed_artifacts(run_id: str, root: Path) -> bool:
    run_dir = root / "data" / "processed" / run_id
    needed = [
        run_dir / "nodes.csv",
        run_dir / "edges.csv",
        run_dir / "labels_setup_max.csv",
        run_dir / "global_features.json",
        run_dir / "paths_setup_max.json",
        run_dir / "paths_summary.csv",
    ]
    return all(p.exists() for p in needed)


def validate_existing_dataset(run_id: str, root: Path) -> bool:
    if not check_processed_artifacts(run_id, root):
        return False
    cmd = ["python3", "scripts/validate_dataset.py", "--run-id", run_id]
    result = subprocess.run(
        cmd,
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run phase-1 ORFS sweep + dataset extraction")
    parser.add_argument("--config", default="configs/sweep_phase1.json")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--run-id", action="append", default=[], help="Only execute selected run_id (repeatable)")
    parser.add_argument("--max-runs", type=int, default=None, help="Only execute first N selected runs")
    args = parser.parse_args()

    root = repo_root()
    config_path = resolve_path(args.config, root)
    config = load_json(config_path)
    planned_runs = planned_runs_from_config(config)

    subprocess.run(["python3", "scripts/gen_sdc_variants.py", "--config", str(config_path)], cwd=str(root), check=True)

    manifests_dir = root / "data" / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    runs_csv = manifests_dir / "runs.csv"

    existing_rows = read_runs_csv(runs_csv)

    ordered_rows: List[Dict[str, object]] = []
    reconciled_from_existing = 0
    run_order: Dict[str, int] = {}
    for idx, run in enumerate(planned_runs):
        run_order[run["run_id"]] = idx
        existing = existing_rows.get(run["run_id"], {})
        sdc_path = generated_sdc_path(config, run["design"], run["clock_period_ns"], root)
        row = {
            "run_id": run["run_id"],
            "design": run["design"],
            "clock_scale": run["clock_scale"],
            "clock_period_ns": run["clock_period_ns"],
            "abc_area": run["abc_area"],
            "place_density": run["place_density"],
            "routing_layer_adjustment": run["routing_layer_adjustment"],
            "variant": run["variant"],
            "sdc_file": str(sdc_path),
            "status": existing.get("status", "planned") if (args.resume or args.dry_run) else "planned",
            "last_error": existing.get("last_error", "") if (args.resume or args.dry_run) else "",
            "last_update_utc": existing.get("last_update_utc", "") if (args.resume or args.dry_run) else "",
        }
        if row["status"] == "success" and args.resume:
            if not check_success_artifacts(config, run, root):
                row["status"] = "planned"
                row["last_error"] = "success artifacts missing; scheduled again"
        elif args.resume and row["status"] in {"failed", "running"}:
            # Fast reconcile: if prior run already produced a valid dataset, mark success.
            if check_success_artifacts(config, run, root) and validate_existing_dataset(run["run_id"], root):
                row["status"] = "success"
                row["last_error"] = ""
                row["last_update_utc"] = utc_now_iso()
                reconciled_from_existing += 1
        ordered_rows.append(row)

    write_runs_csv(runs_csv, ordered_rows)

    run_id_filter = set(args.run_id)
    work_items = []
    for run in planned_runs:
        if run_id_filter and run["run_id"] not in run_id_filter:
            continue
        row = ordered_rows[run_order[run["run_id"]]]
        if args.resume and row["status"] == "success":
            continue
        work_items.append(run)

    if args.max_runs is not None:
        work_items = work_items[: args.max_runs]

    log_dir = root / "logs" / "pipeline"
    log_dir.mkdir(parents=True, exist_ok=True)

    lock = threading.Lock()

    def update_status(run_id: str, status: str, error: str = "") -> None:
        with lock:
            row = ordered_rows[run_order[run_id]]
            row["status"] = status
            row["last_error"] = error
            row["last_update_utc"] = utc_now_iso()
            write_runs_csv(runs_csv, ordered_rows)

    openroad_bin = str(resolve_path(str(config["openroad_bin"]), root))
    liberty = str(resolve_path(str(config["liberty"]), root))

    def do_run(run: Dict[str, object]) -> Dict[str, object]:
        run_id = str(run["run_id"])
        variant = str(run["variant"])
        design = str(run["design"])
        sdc_path = generated_sdc_path(config, design, float(run["clock_period_ns"]), root)

        raw_dir = root / "data" / "raw_curated" / run_id
        processed_dir = root / "data" / "processed" / run_id
        log_file = log_dir / f"{run_id}.log"

        update_status(run_id, "running")

        try:
            num_cores = int(config.get("default_num_cores", 4))
            reuse_flow = check_success_artifacts(config, run, root)

            if reuse_flow:
                with log_file.open("a", encoding="utf-8") as lf:
                    lf.write("\n# Reusing existing ORFS final artifacts; skipping flow + write_net_rc.\n")
            else:
                run_cmd(
                    command_for_make_flow(root, config, run, sdc_path, num_cores),
                    log_file,
                    root,
                    args.dry_run,
                )
                run_cmd(
                    command_for_make_net_rc(root, config, run, num_cores),
                    log_file,
                    root,
                    args.dry_run,
                )

            curated_cmd = [
                "python3",
                "scripts/collect_curated_raw.py",
                "--run-id",
                run_id,
                "--design",
                design,
                "--platform",
                str(config["platform"]),
                "--variant",
                variant,
                "--clock-scale",
                str(run["clock_scale"]),
                "--clock-period-ns",
                str(run["clock_period_ns"]),
                "--abc-area",
                str(run["abc_area"]),
                "--place-density",
                str(run["place_density"]),
            ]
            if run.get("routing_layer_adjustment") is not None:
                curated_cmd.extend(["--routing-layer-adjustment", str(run["routing_layer_adjustment"])])
            run_cmd(curated_cmd, log_file, root, args.dry_run)

            odb = raw_dir / "6_final.odb"
            sdc = raw_dir / "6_final.sdc"
            spef = raw_dir / "6_final.spef"

            extract_graph_cmd = [
                openroad_bin,
                "-python",
                "-no_init",
                "-exit",
                "scripts/extract_graph_labels_or.py",
                "--odb",
                str(odb),
                "--sdc",
                str(sdc),
                "--spef",
                str(spef),
                "--liberty",
                liberty,
                "--run-id",
                run_id,
                "--raw-dir",
                str(raw_dir),
                "--out-dir",
                str(processed_dir),
            ]
            run_cmd(extract_graph_cmd, log_file, root, args.dry_run)

            extract_paths_cmd = [
                openroad_bin,
                "-python",
                "-no_init",
                "-exit",
                "scripts/extract_paths_or.py",
                "--odb",
                str(odb),
                "--sdc",
                str(sdc),
                "--spef",
                str(spef),
                "--liberty",
                liberty,
                "--run-id",
                run_id,
                "--out-dir",
                str(processed_dir),
            ]
            run_cmd(extract_paths_cmd, log_file, root, args.dry_run)

            validate_cmd = [
                "python3",
                "scripts/validate_dataset.py",
                "--run-id",
                run_id,
            ]
            run_cmd(validate_cmd, log_file, root, args.dry_run)

            update_status(run_id, "success")
            return {"run_id": run_id, "status": "success"}

        except subprocess.CalledProcessError as exc:
            update_status(run_id, "failed", f"command failed with exit {exc.returncode}")
            return {"run_id": run_id, "status": "failed", "error": f"exit {exc.returncode}"}
        except Exception as exc:
            update_status(run_id, "failed", str(exc))
            return {"run_id": run_id, "status": "failed", "error": str(exc)}

    print(f"Selected runs: {len(work_items)}")
    if args.dry_run:
        for run in work_items:
            print(
                json.dumps(
                    {
                        "run_id": run["run_id"],
                        "design": run["design"],
                        "clock_period_ns": run["clock_period_ns"],
                        "abc_area": run["abc_area"],
                        "place_density": run["place_density"],
                        "routing_layer_adjustment": run["routing_layer_adjustment"],
                    }
                )
            )

    results = []
    if not args.dry_run:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futs = [ex.submit(do_run, run) for run in work_items]
            for fut in concurrent.futures.as_completed(futs):
                results.append(fut.result())

    subprocess.run(["python3", "scripts/build_split_manifest.py"], cwd=str(root), check=True)

    summary = {
        "planned_runs": len(planned_runs),
        "selected_runs": len(work_items),
        "reconciled_from_existing": reconciled_from_existing,
        "dry_run": bool(args.dry_run),
        "success": sum(1 for r in results if r.get("status") == "success"),
        "failed": sum(1 for r in results if r.get("status") == "failed"),
        "runs_csv": str(runs_csv),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
