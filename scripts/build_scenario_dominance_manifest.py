#!/usr/bin/env python3
"""Build dominant-scenario labels for early MCMM pruning experiments.

This script pivots from per-node STA prediction to a scenario selection task:
- Group runs by implementation knobs (e.g. design + abc_area + place_density + routing_layer_adjustment).
- Use selected STA outcome metric to rank scenarios within each group.
- Emit labels for top-1 / top-k dominant scenarios.

Outputs:
- CSV manifest with one row per run and dominance labels.
- JSON summary with dataset stats and a cheap baseline score.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Tuple


DEFAULT_GROUP_KEYS = (
    "design",
    "abc_area",
    "place_density",
    "routing_layer_adjustment",
)


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _safe_float(v: object) -> float | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "" or s.lower() == "none":
        return None
    try:
        out = float(s)
    except Exception:
        return None
    return out


def _min_ignore_none(a: float | None, b: float | None) -> float | None:
    vals = []
    if a is not None:
        vals.append(a)
    if b is not None:
        vals.append(b)
    if not vals:
        return None
    return min(vals)


def _load_global(path: Path) -> Dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _group_id(row: Dict[str, object], keys: Iterable[str]) -> str:
    parts = []
    for k in keys:
        v = row.get(k)
        if v is None:
            parts.append(f"{k}=base")
        else:
            parts.append(f"{k}={v}")
    return "|".join(parts)


def _write_csv(path: Path, rows: List[Dict[str, object]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def main() -> None:
    ap = argparse.ArgumentParser(description="Build dominant scenario manifest from completed runs")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--runs-csv", default="data/manifests/runs.csv")
    ap.add_argument("--output-csv", default="data/manifests/scenario_dominance.csv")
    ap.add_argument("--output-summary", default="data/manifests/scenario_dominance_summary.json")
    ap.add_argument("--top-k", type=int, default=2)
    ap.add_argument("--min-group-size", type=int, default=2)
    ap.add_argument(
        "--metric-col",
        default="wns_ns",
        help="Global feature key used to rank dominance within each group (default: wns_ns; lower is worse).",
    )
    ap.add_argument(
        "--metric-higher-is-worse",
        action="store_true",
        help="If set, larger metric values are treated as more dominant/worse.",
    )
    ap.add_argument(
        "--group-keys",
        default="design,abc_area,place_density,routing_layer_adjustment",
        help="Comma-separated columns that define a scenario-selection group",
    )
    args = ap.parse_args()

    if args.top_k < 1:
        raise SystemExit("--top-k must be >= 1")
    if args.min_group_size < 2:
        raise SystemExit("--min-group-size must be >= 2")

    dataset_rows = _read_csv(Path(args.dataset_index))
    runs_rows = _read_csv(Path(args.runs_csv))
    run_status = {r["run_id"]: (r.get("status") or "").strip().lower() for r in runs_rows}

    group_keys = [k.strip() for k in args.group_keys.split(",") if k.strip()]
    if not group_keys:
        group_keys = list(DEFAULT_GROUP_KEYS)

    rows_for_groups: List[Dict[str, object]] = []
    skipped_non_success = 0
    skipped_missing_wns = 0

    for r in dataset_rows:
        run_id = str(r.get("run_id", "")).strip()
        if not run_id:
            continue
        if run_status.get(run_id, "") != "success":
            skipped_non_success += 1
            continue

        global_path = Path(str(r.get("global_json", "")).strip())
        if not global_path.exists():
            skipped_missing_wns += 1
            continue
        g = _load_global(global_path)
        metric_val = _safe_float(g.get(args.metric_col))
        if metric_val is None:
            skipped_missing_wns += 1
            continue

        row: Dict[str, object] = {
            "run_id": run_id,
            "design": str(r.get("design", "")).strip(),
            "clock_period_ns": _safe_float(g.get("clock_period_ns")),
            "clock_scale": _safe_float(r.get("clock_scale")),
            "abc_area": int(_safe_float(g.get("abc_area")) or 0),
            "place_density": _safe_float(g.get("place_density")),
            "routing_layer_adjustment": _safe_float(g.get("routing_layer_adjustment")),
            "scenario_id": str(g.get("scenario_id") or r.get("scenario_id") or "base").strip() or "base",
            "scenario_mode": str(g.get("scenario_mode") or r.get("scenario_mode") or "func").strip() or "func",
            "scenario_pvt": str(g.get("scenario_pvt") or r.get("scenario_pvt") or "typical").strip() or "typical",
            "scenario_rc": str(g.get("scenario_rc") or r.get("scenario_rc") or "typ").strip() or "typ",
            "clock_uncertainty_ns": _safe_float(g.get("clock_uncertainty_ns") or r.get("clock_uncertainty_ns")),
            "timing_derate_late": _safe_float(g.get("timing_derate_late") or r.get("timing_derate_late")),
            "timing_derate_early": _safe_float(g.get("timing_derate_early") or r.get("timing_derate_early")),
            "input_delay_scale": _safe_float(g.get("input_delay_scale") or r.get("input_delay_scale")),
            "output_delay_scale": _safe_float(g.get("output_delay_scale") or r.get("output_delay_scale")),
            "source_run_id": str(g.get("source_run_id") or run_id).strip() or run_id,
            "metric_col": str(args.metric_col),
            "metric_value": float(metric_val),
            "wns_ns": _safe_float(g.get("wns_ns")),
            "tns_ns": _safe_float(g.get("tns_ns")),
            "wns_setup_ns": _safe_float(g.get("wns_setup_ns") or g.get("wns_ns")),
            "tns_setup_ns": _safe_float(g.get("tns_setup_ns") or g.get("tns_ns")),
            "wns_hold_ns": _safe_float(g.get("wns_hold_ns") or g.get("wns_min_ns")),
            "tns_hold_ns": _safe_float(g.get("tns_hold_ns")),
            "wns_combined_ns": _safe_float(g.get("wns_combined_ns")),
            "dominant_check": str(g.get("dominant_check", "")).strip(),
            "worst_slack_ns": _safe_float(g.get("worst_slack_ns")),
            "critical_path_delay_ns": _safe_float(g.get("critical_path_delay_ns")),
            "critical_path_slack_ns": _safe_float(g.get("critical_path_slack_ns")),
        }
        if row["wns_combined_ns"] is None:
            row["wns_combined_ns"] = _min_ignore_none(
                row.get("wns_setup_ns"),  # type: ignore[arg-type]
                row.get("wns_hold_ns"),  # type: ignore[arg-type]
            )
        row["group_id"] = _group_id(row, group_keys)
        rows_for_groups.append(row)

    groups: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in rows_for_groups:
        groups[str(row["group_id"])].append(row)

    kept_groups = {gid: grp for gid, grp in groups.items() if len(grp) >= args.min_group_size}
    dropped_small = len(groups) - len(kept_groups)

    out_rows: List[Dict[str, object]] = []
    group_sizes: List[int] = []
    top1_clock_counter: Counter[str] = Counter()
    top1_scenario_counter: Counter[str] = Counter()

    # Baseline: predict dominant scenarios by tightest clock (smallest period).
    baseline_top1_hits = 0
    baseline_topk_hits = 0
    total_groups = len(kept_groups)
    sign = -1.0 if bool(args.metric_higher_is_worse) else 1.0

    for gid, grp in sorted(kept_groups.items()):
        # Ground truth rank on selected metric.
        truth = sorted(grp, key=lambda x: (sign * float(x["metric_value"]), str(x["run_id"])))
        for rank, row in enumerate(truth, start=1):
            row["dominance_rank"] = rank
            row["is_dominant_top1"] = 1 if rank == 1 else 0
            row["is_dominant_topk"] = 1 if rank <= args.top_k else 0
            row["group_size"] = len(truth)
            out_rows.append(row)

        group_sizes.append(len(truth))
        top1_clock = truth[0].get("clock_period_ns")
        top1_clock_counter[str(top1_clock)] += 1
        top1_scenario = str(truth[0].get("scenario_id", "")).strip() or "unknown"
        top1_scenario_counter[top1_scenario] += 1

        # Baseline predictions by tightest clock.
        pred = sorted(
            grp,
            key=lambda x: (
                float(x.get("clock_period_ns") if x.get("clock_period_ns") is not None else 1e18),
                str(x["run_id"]),
            ),
        )
        truth_top1 = {str(truth[0]["run_id"])}
        truth_topk = {str(r["run_id"]) for r in truth[: args.top_k]}
        pred_top1 = {str(pred[0]["run_id"])}
        pred_topk = {str(r["run_id"]) for r in pred[: args.top_k]}
        if pred_top1 & truth_top1:
            baseline_top1_hits += 1
        if pred_topk & truth_topk:
            baseline_topk_hits += 1

    fieldnames = [
        "group_id",
        "group_size",
        "run_id",
        "design",
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
        "source_run_id",
        "metric_col",
        "metric_value",
        "wns_ns",
        "tns_ns",
        "wns_setup_ns",
        "tns_setup_ns",
        "wns_hold_ns",
        "tns_hold_ns",
        "wns_combined_ns",
        "dominant_check",
        "worst_slack_ns",
        "critical_path_delay_ns",
        "critical_path_slack_ns",
        "dominance_rank",
        "is_dominant_top1",
        "is_dominant_topk",
    ]
    _write_csv(Path(args.output_csv), out_rows, fieldnames)

    summary = {
        "inputs": {
            "dataset_index": str(Path(args.dataset_index).resolve()),
            "runs_csv": str(Path(args.runs_csv).resolve()),
            "group_keys": group_keys,
            "top_k": int(args.top_k),
            "min_group_size": int(args.min_group_size),
            "metric_col": str(args.metric_col),
            "metric_higher_is_worse": bool(args.metric_higher_is_worse),
        },
        "counts": {
            "dataset_rows": len(dataset_rows),
            "rows_after_success_and_wns": len(rows_for_groups),
            "groups_total": len(groups),
            "groups_kept": total_groups,
            "groups_dropped_small": dropped_small,
            "rows_output": len(out_rows),
            "skipped_non_success": skipped_non_success,
            "skipped_missing_wns": skipped_missing_wns,
        },
        "group_size_stats": {
            "min": min(group_sizes) if group_sizes else 0,
            "median": median(group_sizes) if group_sizes else 0,
            "max": max(group_sizes) if group_sizes else 0,
        },
        "dominant_top1_clock_period_ns_hist": dict(top1_clock_counter),
        "dominant_top1_scenario_id_hist": dict(top1_scenario_counter),
        "dominance_diversity": {
            "num_unique_top1_scenarios": len(top1_scenario_counter),
            "is_degenerate_single_scenario": len(top1_scenario_counter) <= 1,
            "note": "If true, ranking is likely dominated by one scenario template and may overestimate model quality.",
        },
        "baseline_tightest_clock": {
            "top1_accuracy": (baseline_top1_hits / total_groups) if total_groups > 0 else 0.0,
            "topk_hit_rate": (baseline_topk_hits / total_groups) if total_groups > 0 else 0.0,
            "note": "Predict dominant scenario(s) by smallest clock_period_ns within each group.",
        },
    }

    out_summary = Path(args.output_summary)
    out_summary.parent.mkdir(parents=True, exist_ok=True)
    out_summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(
        f"wrote_csv={args.output_csv} rows={len(out_rows)} "
        f"groups={total_groups} top1_acc={summary['baseline_tightest_clock']['top1_accuracy']:.3f} "
        f"top{args.top_k}_hit={summary['baseline_tightest_clock']['topk_hit_rate']:.3f}"
    )
    print(f"wrote_summary={args.output_summary}")


if __name__ == "__main__":
    main()
