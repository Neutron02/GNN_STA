#!/usr/bin/env python3
"""Shared helpers for the phase-1 timing dataset pipeline."""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def resolve_path(path_str: str, root: Path | None = None) -> Path:
    base = root if root is not None else repo_root()
    p = Path(path_str)
    return p if p.is_absolute() else (base / p)


def load_json(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: Path, data: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def write_csv(path: Path, rows: Sequence[Mapping[str, object]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            out = {k: row.get(k, "") for k in fieldnames}
            writer.writerow(out)


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = str(value).strip()
    if value == "" or value.lower() == "none":
        return None
    return float(value)


def routing_token(rla: float | None) -> str:
    if rla is None:
        return "base"
    return "035" if abs(rla - 0.35) < 1e-9 else str(rla).replace(".", "")


def expected_run_id(design: str, clock_period_ns: float, abc_area: int, place_density: float, rla: float | None) -> str:
    clk_ps = int(round(clock_period_ns * 1000.0))
    pd_token = int(round(place_density * 1000.0))
    return f"{design}__clk{clk_ps}ps__abc{abc_area}__pd{pd_token:03d}__rla{routing_token(rla)}"


def run_paths(platform: str, design: str, variant: str, root: Path | None = None) -> Dict[str, Path]:
    base = repo_root() if root is None else root
    flow_dir = base / "OpenROAD-flow-scripts" / "flow"
    return {
        "flow_dir": flow_dir,
        "results_dir": flow_dir / "results" / platform / design / variant,
        "reports_dir": flow_dir / "reports" / platform / design / variant,
        "logs_dir": flow_dir / "logs" / platform / design / variant,
    }


def planned_runs_from_config(config: Mapping[str, object]) -> List[Dict[str, object]]:
    runs = config.get("runs", [])
    if not isinstance(runs, list):
        raise ValueError("Config field 'runs' must be a list")
    normalized = []
    for idx, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"Run index {idx} is not an object")
        required = [
            "run_id",
            "design",
            "clock_scale",
            "clock_period_ns",
            "abc_area",
            "place_density",
            "variant",
        ]
        for key in required:
            if key not in run:
                raise ValueError(f"Run index {idx} missing field '{key}'")
        normalized.append(
            {
                "run_id": str(run["run_id"]),
                "design": str(run["design"]),
                "clock_scale": float(run["clock_scale"]),
                "clock_period_ns": float(run["clock_period_ns"]),
                "abc_area": int(run["abc_area"]),
                "place_density": float(run["place_density"]),
                "routing_layer_adjustment": parse_optional_float(run.get("routing_layer_adjustment")),
                "variant": str(run["variant"]),
            }
        )
    return normalized


def generated_sdc_path(config: Mapping[str, object], design: str, clock_period_ns: float, root: Path | None = None) -> Path:
    base = repo_root() if root is None else root
    out_base = resolve_path(str(config["generated_sdc_dir"]), base)
    clk_ps = int(round(clock_period_ns * 1000.0))
    return out_base / design / f"constraint_clk{clk_ps}ps.sdc"


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def assert_ascii(s: str) -> None:
    s.encode("ascii")


def parse_finish_report_metrics(finish_rpt: Path) -> Dict[str, float | None]:
    metrics = {
        "tns_ns": None,
        "wns_ns": None,
        "worst_slack_ns": None,
        "critical_path_delay_ns": None,
        "critical_path_slack_ns": None,
    }
    if not finish_rpt.exists():
        return metrics
    import re

    patterns = {
        "tns_ns": re.compile(r"^\s*tns\s+max\s+([-+]?\d+(?:\.\d+)?)\s*$"),
        "wns_ns": re.compile(r"^\s*wns\s+max\s+([-+]?\d+(?:\.\d+)?)\s*$"),
        "worst_slack_ns": re.compile(r"^\s*worst\s+slack\s+max\s+([-+]?\d+(?:\.\d+)?)\s*$"),
    }

    lines = finish_rpt.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        for key, rgx in patterns.items():
            m = rgx.match(line)
            if m:
                metrics[key] = float(m.group(1))

    def parse_block_value(title: str) -> float | None:
        title_idx = None
        for i, line in enumerate(lines):
            if line.strip().lower().endswith(title.lower()):
                title_idx = i
        if title_idx is None:
            return None
        for line in lines[title_idx + 1 : title_idx + 8]:
            val = line.strip()
            try:
                return float(val)
            except ValueError:
                continue
        return None

    metrics["critical_path_delay_ns"] = parse_block_value("critical path delay")
    metrics["critical_path_slack_ns"] = parse_block_value("critical path slack")
    return metrics
