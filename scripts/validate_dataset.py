#!/usr/bin/env python3
"""Validate one extracted run dataset for schema and consistency."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path


def read_csv(path: Path):
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float(value):
    try:
        if value is None or value == "":
            return float("nan")
        return float(value)
    except Exception:
        return float("nan")


def is_finite(v):
    return isinstance(v, float) and math.isfinite(v)


def ensure_columns(rows, required, name, issues):
    if not rows:
        issues.append(f"{name}: file has no rows")
        return
    cols = set(rows[0].keys())
    missing = [c for c in required if c not in cols]
    if missing:
        issues.append(f"{name}: missing columns {missing}")


def rc_csv_has_data(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate extracted dataset for one run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--processed-root", default="data/processed")
    parser.add_argument("--raw-root", default="data/raw_curated")
    parser.add_argument("--min-finite-coverage", type=float, default=0.8)
    parser.add_argument("--required-eps", type=float, default=1e-12)
    parser.add_argument("--wns-tol-ps", type=float, default=5.0)
    args = parser.parse_args()

    run_dir = Path(args.processed_root).resolve() / args.run_id
    raw_dir = Path(args.raw_root).resolve() / args.run_id

    issues = []

    nodes_path = run_dir / "nodes.csv"
    edges_path = run_dir / "edges.csv"
    labels_path = run_dir / "labels_setup_max.csv"
    global_path = run_dir / "global_features.json"

    for p in [nodes_path, edges_path, labels_path, global_path]:
        if not p.exists():
            issues.append(f"missing file: {p}")

    if issues:
        print("validation_failed", json.dumps({"run_id": args.run_id, "issues": issues}, indent=2))
        raise SystemExit(1)

    nodes = read_csv(nodes_path)
    edges = read_csv(edges_path)
    labels = read_csv(labels_path)
    global_features = json.loads(global_path.read_text(encoding="utf-8"))

    ensure_columns(
        nodes,
        [
            "node_id",
            "node_name",
            "node_kind",
            "inst_name",
            "cell_name",
            "port_name",
            "io_type",
            "is_sequential_cell",
            "is_buffer_cell",
            "is_inverter_cell",
            "is_clock_pin",
            "is_endpoint",
            "x_um",
            "y_um",
            "inst_x_um",
            "inst_y_um",
            "cell_area_um2",
            "port_cap_max_f",
            "port_cap_min_f",
        ],
        "nodes.csv",
        issues,
    )
    ensure_columns(
        edges,
        [
            "edge_id",
            "src_node_id",
            "dst_node_id",
            "edge_type",
            "net_name",
            "net_sig_type",
            "net_fanout",
            "net_routed_length_um",
            "net_cap_max_f",
            "net_cap_min_f",
            "net_wire_res_ohm_rcx",
            "net_wire_cap_f_rcx",
            "cell_arc_master",
            "cell_arc_from_pin",
            "cell_arc_to_pin",
        ],
        "edges.csv",
        issues,
    )
    ensure_columns(
        labels,
        [
            "node_id",
            "arrival_rise_s",
            "arrival_fall_s",
            "slack_rise_setup_max_s",
            "slack_fall_setup_max_s",
            "required_rise_setup_max_s",
            "required_fall_setup_max_s",
            "arrival_setup_scalar_s",
            "slack_setup_scalar_s",
            "required_setup_scalar_s",
            "is_arrival_inf",
            "is_slack_inf",
        ],
        "labels_setup_max.csv",
        issues,
    )

    node_ids = {row.get("node_id") for row in nodes}

    for i, edge in enumerate(edges):
        if edge.get("src_node_id") not in node_ids:
            issues.append(f"edges.csv row {i}: src_node_id missing from nodes")
            break
        if edge.get("dst_node_id") not in node_ids:
            issues.append(f"edges.csv row {i}: dst_node_id missing from nodes")
            break

    seen = set()
    for edge in edges:
        key = (edge.get("src_node_id"), edge.get("dst_node_id"), edge.get("edge_type"), edge.get("net_name"))
        if key in seen:
            issues.append(f"duplicate edge tuple found: {key}")
            break
        seen.add(key)

    finite_slack_count = 0
    total = len(labels)
    req_bad = 0
    min_slack = float("nan")

    for row in labels:
        arr = to_float(row.get("arrival_setup_scalar_s"))
        slk = to_float(row.get("slack_setup_scalar_s"))
        req = to_float(row.get("required_setup_scalar_s"))
        if is_finite(slk):
            finite_slack_count += 1
            if not is_finite(min_slack) or slk < min_slack:
                min_slack = slk
        if is_finite(arr) and is_finite(slk) and is_finite(req):
            if abs((arr + slk) - req) > args.required_eps:
                req_bad += 1

    coverage = (finite_slack_count / total) if total else 0.0
    if coverage < args.min_finite_coverage:
        issues.append(
            f"finite setup-slack coverage too low: {coverage:.3f} < {args.min_finite_coverage:.3f}"
        )
    if req_bad > 0:
        issues.append(f"required != arrival + slack rows: {req_bad}")

    wns_ns = global_features.get("wns_ns")
    if wns_ns is not None and is_finite(min_slack):
        wns_s = float(wns_ns) * 1e-9
        tol_s = args.wns_tol_ps * 1e-12
        # ORFS summary WNS is often clamped at 0 when setup timing is met.
        # In that case, enforce only that min extracted slack is not negative.
        if wns_s >= 0:
            if min_slack < -tol_s:
                issues.append(
                    f"WNS/sign mismatch: min_label_slack={min_slack:.3e}s is negative while report_wns={wns_s:.3e}s (tol={tol_s:.3e}s)"
                )
        elif abs(min_slack - wns_s) > tol_s:
            issues.append(
                f"WNS mismatch: min_label_slack={min_slack:.3e}s vs report_wns={wns_s:.3e}s (tol={tol_s:.3e}s)"
            )

    rc_required = rc_csv_has_data(raw_dir / "6_net_rc.csv")
    if rc_required:
        rc_joined = 0
        for edge in edges:
            if edge.get("edge_type") != "net":
                continue
            rc = to_float(edge.get("net_wire_cap_f_rcx"))
            rr = to_float(edge.get("net_wire_res_ohm_rcx"))
            if is_finite(rc) or is_finite(rr):
                rc_joined += 1
                break
        if rc_joined == 0:
            issues.append("RC join sanity failed: no net edge has joined RCX data")

    passed = len(issues) == 0
    report = {
        "run_id": args.run_id,
        "passed": passed,
        "issues": issues,
        "stats": {
            "num_nodes": len(nodes),
            "num_edges": len(edges),
            "num_labels": len(labels),
            "finite_slack_coverage": coverage,
            "min_label_slack_s": None if not is_finite(min_slack) else min_slack,
            "wns_ns": wns_ns,
        },
    }

    with (run_dir / "validation.json").open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    if passed:
        print("validation_passed", json.dumps(report["stats"]))
        return

    print("validation_failed", json.dumps(report, indent=2))
    raise SystemExit(1)


if __name__ == "__main__":
    main()
