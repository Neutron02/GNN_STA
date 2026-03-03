#!/usr/bin/env python3
"""Build dataset index and stratified run-level train/val/test splits."""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from pathlib import Path


def read_csv(path: Path):
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def count_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        n = -1
        for n, _ in enumerate(reader):
            pass
        return max(n, 0)


def stratified_split(run_rows, seed: int):
    by_design = defaultdict(list)
    for row in run_rows:
        by_design[row["design"]].append(row["run_id"])

    rng = random.Random(seed)
    train = []
    val = []
    test = []

    for design, run_ids in sorted(by_design.items()):
        items = list(run_ids)
        rng.shuffle(items)
        n = len(items)

        n_train = int(n * 0.70)
        n_val = int(n * 0.15)
        n_test = n - n_train - n_val

        train.extend(items[:n_train])
        val.extend(items[n_train : n_train + n_val])
        test.extend(items[n_train + n_val : n_train + n_val + n_test])

    return {
        "seed": seed,
        "fractions": {"train": 0.70, "val": 0.15, "test": 0.15},
        "train": sorted(train),
        "val": sorted(val),
        "test": sorted(test),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dataset_index.csv and splits.json")
    parser.add_argument("--manifests-dir", default="data/manifests")
    parser.add_argument("--raw-root", default="data/raw_curated")
    parser.add_argument("--processed-root", default="data/processed")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--skip-row-counts",
        action="store_true",
        help="Skip expensive CSV row counting for num_* fields (faster on large datasets).",
    )
    args = parser.parse_args()

    manifests_dir = Path(args.manifests_dir).resolve()
    raw_root = Path(args.raw_root).resolve()
    processed_root = Path(args.processed_root).resolve()

    manifests_dir.mkdir(parents=True, exist_ok=True)

    runs_csv = manifests_dir / "runs.csv"
    runs = read_csv(runs_csv)

    index_rows = []
    for run in runs:
        if run.get("status") != "success":
            continue

        run_id = run["run_id"]
        design = run["design"]

        raw_dir = raw_root / run_id
        proc_dir = processed_root / run_id

        nodes_csv = proc_dir / "nodes.csv"
        edges_csv = proc_dir / "edges.csv"
        labels_csv = proc_dir / "labels_setup_max.csv"
        paths_csv = proc_dir / "paths_summary.csv"
        paths_json = proc_dir / "paths_setup_max.json"
        global_json = proc_dir / "global_features.json"
        validation_json = proc_dir / "validation.json"

        validation_passed = ""
        if validation_json.exists():
            try:
                validation_passed = bool(json.loads(validation_json.read_text(encoding="utf-8")).get("passed"))
            except Exception:
                validation_passed = ""

        if args.skip_row_counts:
            num_nodes = ""
            num_edges = ""
            num_labels = ""
            num_paths = ""
        else:
            num_nodes = count_rows(nodes_csv)
            num_edges = count_rows(edges_csv)
            num_labels = count_rows(labels_csv)
            num_paths = count_rows(paths_csv)

        index_rows.append(
            {
                "run_id": run_id,
                "design": design,
                "status": run.get("status", ""),
                "clock_scale": run.get("clock_scale", ""),
                "clock_period_ns": run.get("clock_period_ns", ""),
                "abc_area": run.get("abc_area", ""),
                "place_density": run.get("place_density", ""),
                "routing_layer_adjustment": run.get("routing_layer_adjustment", ""),
                "scenario_id": run.get("scenario_id", "base"),
                "scenario_mode": run.get("scenario_mode", "func"),
                "scenario_pvt": run.get("scenario_pvt", "typical"),
                "scenario_rc": run.get("scenario_rc", "typ"),
                "clock_uncertainty_ns": run.get("clock_uncertainty_ns", ""),
                "timing_derate_late": run.get("timing_derate_late", ""),
                "timing_derate_early": run.get("timing_derate_early", ""),
                "input_delay_scale": run.get("input_delay_scale", ""),
                "output_delay_scale": run.get("output_delay_scale", ""),
                "raw_dir": str(raw_dir),
                "processed_dir": str(proc_dir),
                "nodes_csv": str(nodes_csv),
                "edges_csv": str(edges_csv),
                "labels_csv": str(labels_csv),
                "paths_csv": str(paths_csv),
                "paths_json": str(paths_json),
                "global_json": str(global_json),
                "validation_json": str(validation_json),
                "validation_passed": validation_passed,
                "num_nodes": num_nodes,
                "num_edges": num_edges,
                "num_labels": num_labels,
                "num_paths": num_paths,
            }
        )

    index_rows.sort(key=lambda r: r["run_id"])

    dataset_index_path = manifests_dir / "dataset_index.csv"
    fields = [
        "run_id",
        "design",
        "status",
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
        "raw_dir",
        "processed_dir",
        "nodes_csv",
        "edges_csv",
        "labels_csv",
        "paths_csv",
        "paths_json",
        "global_json",
        "validation_json",
        "validation_passed",
        "num_nodes",
        "num_edges",
        "num_labels",
        "num_paths",
    ]
    with dataset_index_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(index_rows)

    splits = stratified_split(index_rows, args.seed)
    splits_path = manifests_dir / "splits.json"
    with splits_path.open("w", encoding="utf-8") as f:
        json.dump(splits, f, indent=2)
        f.write("\n")

    print(
        json.dumps(
            {
                "dataset_index": str(dataset_index_path),
                "splits": str(splits_path),
                "successful_runs": len(index_rows),
                "train": len(splits["train"]),
                "val": len(splits["val"]),
                "test": len(splits["test"]),
            }
        )
    )


if __name__ == "__main__":
    main()
