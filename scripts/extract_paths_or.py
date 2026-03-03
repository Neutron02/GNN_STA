#!/usr/bin/env python3
"""Extract setup-max and hold-min timing paths and summarize path ranking."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from openroad import Design, Tech


def _load_report_json(path: Path) -> dict:
    """Load report_checks JSON even when OpenROAD prepends warning lines."""
    if not path.exists():
        return {"checks": []}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {"checks": []}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            raise
        return json.loads(text[start : end + 1])


def _run_report(
    design: Design,
    out_json: Path,
    path_delay: str,
    group_path_count: int,
    endpoint_path_count: int,
) -> None:
    cmd = (
        f"report_checks -path_delay {path_delay} -sort_by_slack "
        f"-group_path_count {group_path_count} "
        f"-endpoint_path_count {endpoint_path_count} "
        "-unique_paths_to_endpoint -format json "
        f"> {out_json}"
    )
    design.evalTclString(cmd)


def _write_summary_csv(
    checks: list,
    out_csv: Path,
    run_id: str,
    path_id_prefix: str,
) -> int:
    rows = []
    for idx, check in enumerate(checks, start=1):
        rows.append(
            {
                "path_id": f"{run_id}__{path_id_prefix}{idx:04d}",
                "startpoint": check.get("startpoint", ""),
                "endpoint": check.get("endpoint", ""),
                "path_group": check.get("path_group", ""),
                "data_arrival_time_s": check.get("data_arrival_time", ""),
                "required_time_s": check.get("required_time", ""),
                "slack_s": check.get("slack", ""),
                "rank": idx,
            }
        )
    fields = [
        "path_id",
        "startpoint",
        "endpoint",
        "path_group",
        "data_arrival_time_s",
        "required_time_s",
        "slack_s",
        "rank",
    ]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract timing paths in JSON and summary CSV")
    parser.add_argument("--odb", required=True)
    parser.add_argument("--sdc", required=True)
    parser.add_argument("--spef", required=True)
    parser.add_argument("--liberty", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--group-path-count", type=int, default=200)
    parser.add_argument("--endpoint-path-count", type=int, default=1)
    parser.add_argument("--skip-max", action="store_true", help="Skip setup/max path extraction.")
    parser.add_argument("--skip-min", action="store_true", help="Skip hold/min path extraction.")
    args = parser.parse_args()

    odb_path = Path(args.odb).resolve()
    sdc_path = Path(args.sdc).resolve()
    spef_path = Path(args.spef).resolve()
    liberty_path = Path(args.liberty).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tech = Tech()
    tech.readLiberty(str(liberty_path))
    design = Design(tech)
    design.readDb(str(odb_path))
    design.evalTclString(f"read_sdc {sdc_path}")
    design.evalTclString(f"read_spef {spef_path}")

    counts = {}
    outputs = {}

    if not args.skip_max:
        max_json = out_dir / "paths_setup_max.json"
        _run_report(
            design=design,
            out_json=max_json,
            path_delay="max",
            group_path_count=args.group_path_count,
            endpoint_path_count=args.endpoint_path_count,
        )
        max_data = _load_report_json(max_json)
        max_checks = list(max_data.get("checks", []))
        max_checks.sort(key=lambda c: float(c.get("slack", 1e99)))
        # Legacy setup-max summary path kept for compatibility with existing pipeline.
        max_csv = out_dir / "paths_summary.csv"
        counts["setup_max"] = _write_summary_csv(max_checks, max_csv, args.run_id, "path")
        outputs["setup_max_json"] = str(max_json)
        outputs["setup_max_csv"] = str(max_csv)

    if not args.skip_min:
        min_json = out_dir / "paths_hold_min.json"
        _run_report(
            design=design,
            out_json=min_json,
            path_delay="min",
            group_path_count=args.group_path_count,
            endpoint_path_count=args.endpoint_path_count,
        )
        min_data = _load_report_json(min_json)
        min_checks = list(min_data.get("checks", []))
        min_checks.sort(key=lambda c: float(c.get("slack", 1e99)))
        min_csv = out_dir / "paths_hold_summary.csv"
        counts["hold_min"] = _write_summary_csv(min_checks, min_csv, args.run_id, "hold_path")
        outputs["hold_min_json"] = str(min_json)
        outputs["hold_min_csv"] = str(min_csv)

    print(
        "path_extraction_complete",
        json.dumps(
            {
                "run_id": args.run_id,
                "counts": counts,
                "outputs": outputs,
            }
        ),
    )


if __name__ == "__main__":
    main()
