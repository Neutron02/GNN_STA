#!/usr/bin/env python3
"""Run ORFS sweeps and build timing graph datasets for phase-1."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import subprocess
import threading
import time
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
    "flow_design",
    "clock_scale",
    "clock_period_ns",
    "abc_area",
    "place_density",
    "routing_layer_adjustment",
    "scenario_id",
    "scenario_mode",
    "scenario_pvt",
    "scenario_rc",
    "clock_uncertainty_ns",
    "timing_derate_late",
    "timing_derate_early",
    "input_delay_scale",
    "output_delay_scale",
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
        subprocess.run(
            cmd,
            cwd=str(cwd),
            check=True,
            stdout=lf,
            stderr=subprocess.STDOUT,
            env=build_subprocess_env(),
        )


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "--:--:--"
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def progress_bar(done: int, total: int, width: int = 30) -> str:
    if total <= 0:
        return "[" + ("-" * width) + "]"
    filled = min(width, int((done / total) * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def build_subprocess_env() -> Dict[str, str]:
    env = os.environ.copy()
    # Prefer system python to avoid toolchain-specific site-package drift (e.g., missing PyYAML).
    if Path("/usr/bin/python3").exists():
        path = env.get("PATH", "")
        prefix = "/usr/bin:/bin"
        env["PATH"] = f"{prefix}:{path}" if path else prefix
    # ORFS final report may touch GUI paths; force headless-safe Qt backend.
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def command_for_make_flow(root: Path, config: Dict, run: Dict, sdc_path: Path, num_cores: int) -> List[str]:
    orfs_flow = resolve_path(str(config["orfs_flow_dir"]), root)
    platform = config["platform"]
    flow_design = str(run.get("flow_design", run["design"]))
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
    report_target = f"logs/{platform}/{flow_design}/{run['variant']}/6_report.log"
    final_sdc_target = f"results/{platform}/{flow_design}/{run['variant']}/6_final.sdc"
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
    flow_design = str(run.get("flow_design", run["design"]))
    paths = run_paths(config["platform"], flow_design, run["variant"], root)
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


def validate_existing_dataset(run_id: str, root: Path, min_finite_coverage: float | None = None) -> bool:
    if not check_processed_artifacts(run_id, root):
        return False
    cmd = [
        "python3",
        "scripts/validate_dataset.py",
        "--run-id",
        run_id,
        "--allow-wns-mismatch",
    ]
    if min_finite_coverage is not None:
        cmd.extend(["--min-finite-coverage", str(float(min_finite_coverage))])
    result = subprocess.run(
        cmd,
        cwd=str(root),
        env=build_subprocess_env(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return result.returncode == 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run phase-1 ORFS sweep + dataset extraction")
    parser.add_argument("--config", default="configs/sweep_phase1.json")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument(
        "--num-cores",
        type=int,
        default=0,
        help="Override per-run ORFS/OpenROAD threads (NUM_CORES). 0 uses config default_num_cores.",
    )
    parser.add_argument(
        "--validate-min-finite-coverage",
        type=float,
        default=None,
        help="Override validate_dataset --min-finite-coverage (default validator value if omitted).",
    )
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
        sdc_path = generated_sdc_path(
            config,
            run["design"],
            run["clock_period_ns"],
            root,
            scenario_id=run.get("scenario_id"),
        )
        row = {
            "run_id": run["run_id"],
            "design": run["design"],
            "flow_design": run.get("flow_design", run["design"]),
            "clock_scale": run["clock_scale"],
            "clock_period_ns": run["clock_period_ns"],
            "abc_area": run["abc_area"],
            "place_density": run["place_density"],
            "routing_layer_adjustment": run["routing_layer_adjustment"],
            "scenario_id": run.get("scenario_id", "base"),
            "scenario_mode": run.get("scenario_mode", "func"),
            "scenario_pvt": run.get("scenario_pvt", "typical"),
            "scenario_rc": run.get("scenario_rc", "typ"),
            "clock_uncertainty_ns": run.get("clock_uncertainty_ns"),
            "timing_derate_late": run.get("timing_derate_late"),
            "timing_derate_early": run.get("timing_derate_early"),
            "input_delay_scale": run.get("input_delay_scale"),
            "output_delay_scale": run.get("output_delay_scale"),
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
            if check_success_artifacts(config, run, root) and validate_existing_dataset(
                run["run_id"], root, min_finite_coverage=args.validate_min_finite_coverage
            ):
                row["status"] = "success"
                row["last_error"] = ""
                row["last_update_utc"] = utc_now_iso()
                reconciled_from_existing += 1
        ordered_rows.append(row)

    # Preserve rows that are not part of this config so subset sweeps do not
    # clobber the global manifest state.
    for run_id, existing in sorted(existing_rows.items()):
        if run_id in run_order:
            continue
        keep_row = {k: existing.get(k, "") for k in RUNS_FIELDS}
        keep_row["run_id"] = run_id
        if keep_row.get("status", "") == "":
            keep_row["status"] = "planned"
        ordered_rows.append(keep_row)

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
        flow_design = str(run.get("flow_design", design))
        sdc_path = generated_sdc_path(
            config,
            design,
            float(run["clock_period_ns"]),
            root,
            scenario_id=run.get("scenario_id"),
        )

        raw_dir = root / "data" / "raw_curated" / run_id
        processed_dir = root / "data" / "processed" / run_id
        log_file = log_dir / f"{run_id}.log"

        update_status(run_id, "running")

        try:
            num_cores = int(args.num_cores) if int(args.num_cores) > 0 else int(config.get("default_num_cores", 4))
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
                "--flow-design",
                flow_design,
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
                "--scenario-id",
                str(run.get("scenario_id", "base")),
                "--scenario-mode",
                str(run.get("scenario_mode", "func")),
                "--scenario-pvt",
                str(run.get("scenario_pvt", "typical")),
                "--scenario-rc",
                str(run.get("scenario_rc", "typ")),
            ]
            if run.get("routing_layer_adjustment") is not None:
                curated_cmd.extend(["--routing-layer-adjustment", str(run["routing_layer_adjustment"])])
            if run.get("clock_uncertainty_ns") is not None:
                curated_cmd.extend(["--clock-uncertainty-ns", str(run["clock_uncertainty_ns"])])
            if run.get("timing_derate_late") is not None:
                curated_cmd.extend(["--timing-derate-late", str(run["timing_derate_late"])])
            if run.get("timing_derate_early") is not None:
                curated_cmd.extend(["--timing-derate-early", str(run["timing_derate_early"])])
            if run.get("input_delay_scale") is not None:
                curated_cmd.extend(["--input-delay-scale", str(run["input_delay_scale"])])
            if run.get("output_delay_scale") is not None:
                curated_cmd.extend(["--output-delay-scale", str(run["output_delay_scale"])])
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
                "--allow-wns-mismatch",
            ]
            if args.validate_min_finite_coverage is not None:
                validate_cmd.extend(["--min-finite-coverage", str(float(args.validate_min_finite_coverage))])
            run_cmd(validate_cmd, log_file, root, args.dry_run)

            update_status(run_id, "success")
            return {"run_id": run_id, "status": "success"}

        except subprocess.CalledProcessError as exc:
            update_status(run_id, "failed", f"command failed with exit {exc.returncode}")
            return {"run_id": run_id, "status": "failed", "error": f"exit {exc.returncode}"}
        except Exception as exc:
            update_status(run_id, "failed", str(exc))
            return {"run_id": run_id, "status": "failed", "error": str(exc)}

    effective_num_cores = int(args.num_cores) if int(args.num_cores) > 0 else int(config.get("default_num_cores", 4))
    print(f"Selected runs: {len(work_items)} | jobs={max(1, args.jobs)} | num_cores={effective_num_cores}")
    if args.dry_run:
        for run in work_items:
            print(
                json.dumps(
                    {
                        "run_id": run["run_id"],
                        "design": run["design"],
                        "flow_design": run.get("flow_design", run["design"]),
                        "clock_period_ns": run["clock_period_ns"],
                        "abc_area": run["abc_area"],
                        "place_density": run["place_density"],
                        "routing_layer_adjustment": run["routing_layer_adjustment"],
                        "scenario_id": run.get("scenario_id", "base"),
                        "scenario_mode": run.get("scenario_mode", "func"),
                        "scenario_pvt": run.get("scenario_pvt", "typical"),
                        "scenario_rc": run.get("scenario_rc", "typ"),
                        "clock_uncertainty_ns": run.get("clock_uncertainty_ns"),
                        "timing_derate_late": run.get("timing_derate_late"),
                        "timing_derate_early": run.get("timing_derate_early"),
                        "input_delay_scale": run.get("input_delay_scale"),
                        "output_delay_scale": run.get("output_delay_scale"),
                    }
                )
            )

    results = []
    if not args.dry_run:
        sweep_start = time.time()
        total_items = len(work_items)
        success_count = 0
        failed_count = 0
        if total_items > 0:
            print(
                f"{progress_bar(0, total_items)} 0/{total_items} "
                f"| ok=0 fail=0 | elapsed={format_duration(0)} | eta={format_duration(None)}"
            )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
            futs = [ex.submit(do_run, run) for run in work_items]
            for fut in concurrent.futures.as_completed(futs):
                result = fut.result()
                results.append(result)
                if result.get("status") == "success":
                    success_count += 1
                elif result.get("status") == "failed":
                    failed_count += 1

                done = len(results)
                elapsed = time.time() - sweep_start
                remaining = max(0, total_items - done)
                eta = (elapsed / done) * remaining if done > 0 else None
                run_id = result.get("run_id", "unknown")
                run_status = result.get("status", "unknown")
                print(
                    f"{progress_bar(done, total_items)} {done}/{total_items} "
                    f"| last={run_id}:{run_status} "
                    f"| ok={success_count} fail={failed_count} "
                    f"| elapsed={format_duration(elapsed)} "
                    f"| eta={format_duration(eta)}",
                    flush=True,
                )

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
