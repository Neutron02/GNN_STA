#!/usr/bin/env python3
"""Extract setup-max timing paths and summarize critical path ranking."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from openroad import Design, Tech


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract max-delay timing paths in JSON and summary CSV")
    parser.add_argument("--odb", required=True)
    parser.add_argument("--sdc", required=True)
    parser.add_argument("--spef", required=True)
    parser.add_argument("--liberty", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--group-path-count", type=int, default=200)
    parser.add_argument("--endpoint-path-count", type=int, default=1)
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

    json_path = out_dir / "paths_setup_max.json"
    cmd = (
        "report_checks -path_delay max -sort_by_slack "
        f"-group_path_count {args.group_path_count} "
        f"-endpoint_path_count {args.endpoint_path_count} "
        "-unique_paths_to_endpoint -format json "
        f"> {json_path}"
    )
    design.evalTclString(cmd)

    data = {"checks": []}
    if json_path.exists():
        with json_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

    checks = list(data.get("checks", []))
    checks.sort(key=lambda c: float(c.get("slack", 1e99)))

    rows = []
    for idx, check in enumerate(checks, start=1):
        rows.append(
            {
                "path_id": f"{args.run_id}__path{idx:04d}",
                "startpoint": check.get("startpoint", ""),
                "endpoint": check.get("endpoint", ""),
                "path_group": check.get("path_group", ""),
                "data_arrival_time_s": check.get("data_arrival_time", ""),
                "required_time_s": check.get("required_time", ""),
                "slack_s": check.get("slack", ""),
                "rank": idx,
            }
        )

    csv_path = out_dir / "paths_summary.csv"
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
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print(
        "path_extraction_complete",
        json.dumps(
            {
                "run_id": args.run_id,
                "path_count": len(rows),
                "json": str(json_path),
                "csv": str(csv_path),
            }
        ),
    )


if __name__ == "__main__":
    main()
