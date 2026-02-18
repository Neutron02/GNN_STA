#!/usr/bin/env python3
"""Copy selected final-stage ORFS artifacts into curated raw dataset folders."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from pipeline_common import dump_json, repo_root, resolve_path, run_paths, utc_now_iso


REQUIRED_FILES = [
    "6_final.odb",
    "6_final.def",
    "6_final.v",
    "6_final.sdc",
    "6_final.spef",
    "6_net_rc.csv",
]
REQUIRED_REPORTS = ["6_finish.rpt"]


def copy_required(src_dir: Path, dst_dir: Path, filenames: list[str]) -> dict[str, str]:
    copied = {}
    for name in filenames:
        src = src_dir / name
        if not src.exists():
            raise FileNotFoundError(f"Required artifact missing: {src}")
        dst = dst_dir / name
        shutil.copy2(src, dst)
        copied[name] = str(src)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect curated raw artifacts for one run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--design", required=True)
    parser.add_argument("--flow-design", default="")
    parser.add_argument("--platform", default="nangate45")
    parser.add_argument("--variant", required=True)
    parser.add_argument("--clock-scale", type=float, required=True)
    parser.add_argument("--clock-period-ns", type=float, required=True)
    parser.add_argument("--abc-area", type=int, required=True)
    parser.add_argument("--place-density", type=float, required=True)
    parser.add_argument("--routing-layer-adjustment", type=float, default=None)
    parser.add_argument("--scenario-id", default="base")
    parser.add_argument("--scenario-mode", default="func")
    parser.add_argument("--scenario-pvt", default="typical")
    parser.add_argument("--scenario-rc", default="typ")
    parser.add_argument("--clock-uncertainty-ns", type=float, default=None)
    parser.add_argument("--timing-derate-late", type=float, default=None)
    parser.add_argument("--timing-derate-early", type=float, default=None)
    parser.add_argument("--input-delay-scale", type=float, default=None)
    parser.add_argument("--output-delay-scale", type=float, default=None)
    parser.add_argument("--source-run-id", default="")
    parser.add_argument("--status", default="success")
    parser.add_argument("--orfs-flow-dir", default="OpenROAD-flow-scripts/flow")
    parser.add_argument("--out-root", default="data/raw_curated")
    args = parser.parse_args()

    root = repo_root()
    out_root = resolve_path(args.out_root, root)
    out_dir = out_root / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    flow_design = args.flow_design.strip() or args.design
    paths = run_paths(args.platform, flow_design, args.variant, root)
    results_dir = paths["results_dir"]
    reports_dir = paths["reports_dir"]

    copied_results = copy_required(results_dir, out_dir, REQUIRED_FILES)
    copied_reports = copy_required(reports_dir, out_dir, REQUIRED_REPORTS)

    run_meta = {
        "run_id": args.run_id,
        "design": args.design,
        "flow_design": flow_design,
        "platform": args.platform,
        "variant": args.variant,
        "clock_scale": args.clock_scale,
        "clock_period_ns": args.clock_period_ns,
        "abc_area": args.abc_area,
        "place_density": args.place_density,
        "routing_layer_adjustment": args.routing_layer_adjustment,
        "scenario_id": args.scenario_id,
        "scenario_mode": args.scenario_mode,
        "scenario_pvt": args.scenario_pvt,
        "scenario_rc": args.scenario_rc,
        "clock_uncertainty_ns": args.clock_uncertainty_ns,
        "timing_derate_late": args.timing_derate_late,
        "timing_derate_early": args.timing_derate_early,
        "input_delay_scale": args.input_delay_scale,
        "output_delay_scale": args.output_delay_scale,
        "source_run_id": args.source_run_id or args.run_id,
        "status": args.status,
        "collected_utc": utc_now_iso(),
        "source_paths": {
            "results_dir": str(results_dir),
            "reports_dir": str(reports_dir),
            "results_files": copied_results,
            "report_files": copied_reports,
        },
    }
    dump_json(out_dir / "run_meta.json", run_meta)
    print(f"Collected curated raw artifacts -> {out_dir}")


if __name__ == "__main__":
    main()
