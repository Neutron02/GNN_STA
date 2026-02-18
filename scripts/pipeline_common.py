#!/usr/bin/env python3
"""Shared helpers for the phase-1 timing dataset pipeline."""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
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


def sanitize_token(value: object, default: str = "base") -> str:
    raw = str(value if value is not None else "").strip()
    if raw == "":
        return default
    tok = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return tok or default


def routing_token(rla: float | None) -> str:
    if rla is None:
        return "base"
    return "035" if abs(rla - 0.35) < 1e-9 else str(rla).replace(".", "")


def expected_run_id(
    design: str,
    clock_period_ns: float,
    abc_area: int,
    place_density: float,
    rla: float | None,
    scenario_id: str | None = None,
) -> str:
    clk_ps = int(round(clock_period_ns * 1000.0))
    pd_token = int(round(place_density * 1000.0))
    base = f"{design}__clk{clk_ps}ps__abc{abc_area}__pd{pd_token:03d}__rla{routing_token(rla)}"
    sc_tok = sanitize_token(scenario_id if scenario_id is not None else "base")
    if sc_tok == "base":
        return base
    return f"{base}__sc{sc_tok}"


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
    def normalize_run(run: Mapping[str, object], context: str) -> Dict[str, object]:
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
                raise ValueError(f"{context} missing field '{key}'")
        return {
            "run_id": str(run["run_id"]),
            "design": str(run["design"]),
            "flow_design": str(run.get("flow_design", run["design"])),
            "clock_scale": float(run["clock_scale"]),
            "clock_period_ns": float(run["clock_period_ns"]),
            "abc_area": int(run["abc_area"]),
            "place_density": float(run["place_density"]),
            "routing_layer_adjustment": parse_optional_float(run.get("routing_layer_adjustment")),
            "scenario_id": sanitize_token(run.get("scenario_id", "base")),
            "scenario_mode": str(run.get("scenario_mode", "func")).strip() or "func",
            "scenario_pvt": str(run.get("scenario_pvt", "typical")).strip() or "typical",
            "scenario_rc": str(run.get("scenario_rc", "typ")).strip() or "typ",
            "clock_uncertainty_ns": parse_optional_float(run.get("clock_uncertainty_ns")),
            "timing_derate_late": parse_optional_float(run.get("timing_derate_late")),
            "timing_derate_early": parse_optional_float(run.get("timing_derate_early")),
            "input_delay_scale": parse_optional_float(run.get("input_delay_scale")),
            "output_delay_scale": parse_optional_float(run.get("output_delay_scale")),
            "variant": str(run["variant"]),
        }

    normalized: List[Dict[str, object]] = []
    seen_by_run_id: Dict[str, Dict[str, object]] = {}

    def add_run(run: Dict[str, object], context: str) -> None:
        run_id = str(run["run_id"])
        existing = seen_by_run_id.get(run_id)
        if existing is None:
            seen_by_run_id[run_id] = run
            normalized.append(run)
            return
        compare_keys = [
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
        ]
        mismatched = [key for key in compare_keys if existing.get(key) != run.get(key)]
        if mismatched:
            raise ValueError(
                f"Duplicate run_id '{run_id}' with conflicting fields {mismatched} ({context})"
            )

    runs = config.get("runs", [])
    if not isinstance(runs, list):
        raise ValueError("Config field 'runs' must be a list")
    for idx, run in enumerate(runs):
        if not isinstance(run, dict):
            raise ValueError(f"Run index {idx} is not an object")
        add_run(normalize_run(run, f"Run index {idx}"), f"run index {idx}")

    run_grid = config.get("run_grid")
    if run_grid is not None:
        if not isinstance(run_grid, dict):
            raise ValueError("Config field 'run_grid' must be an object when present")

        designs = run_grid.get("designs", {})
        if not isinstance(designs, dict):
            raise ValueError("Config field 'run_grid.designs' must be an object")

        clock_scales = run_grid.get("clock_scales", [])
        if not isinstance(clock_scales, list):
            raise ValueError("Config field 'run_grid.clock_scales' must be a list")
        clock_scales = [float(x) for x in clock_scales]

        abc_area_values = run_grid.get("abc_area", [])
        if not isinstance(abc_area_values, list):
            raise ValueError("Config field 'run_grid.abc_area' must be a list")
        abc_area_values = [int(x) for x in abc_area_values]

        place_density_values = run_grid.get("place_density", [])
        if not isinstance(place_density_values, list):
            raise ValueError("Config field 'run_grid.place_density' must be a list")
        place_density_values = [float(x) for x in place_density_values]

        routing_values_raw = run_grid.get("routing_layer_adjustment", [None])
        if not isinstance(routing_values_raw, list):
            raise ValueError("Config field 'run_grid.routing_layer_adjustment' must be a list")
        routing_values = [parse_optional_float(value) for value in routing_values_raw]

        scenarios_raw = run_grid.get("scenarios", [{"scenario_id": "base"}])
        if not isinstance(scenarios_raw, list):
            raise ValueError("Config field 'run_grid.scenarios' must be a list")
        scenarios: List[Dict[str, object]] = []
        for idx, s in enumerate(scenarios_raw):
            if isinstance(s, str):
                s = {"scenario_id": s}
            if not isinstance(s, dict):
                raise ValueError(f"run_grid.scenarios[{idx}] must be an object or string")
            scenarios.append(
                {
                    "scenario_id": sanitize_token(s.get("scenario_id", "base")),
                    "scenario_mode": str(s.get("scenario_mode", "func")).strip() or "func",
                    "scenario_pvt": str(s.get("scenario_pvt", "typical")).strip() or "typical",
                    "scenario_rc": str(s.get("scenario_rc", "typ")).strip() or "typ",
                    "clock_uncertainty_ns": parse_optional_float(s.get("clock_uncertainty_ns")),
                    "timing_derate_late": parse_optional_float(s.get("timing_derate_late")),
                    "timing_derate_early": parse_optional_float(s.get("timing_derate_early")),
                    "input_delay_scale": parse_optional_float(s.get("input_delay_scale")),
                    "output_delay_scale": parse_optional_float(s.get("output_delay_scale")),
                }
            )
        if not scenarios:
            scenarios = [{"scenario_id": "base"}]

        for design, design_cfg in designs.items():
            if not isinstance(design_cfg, dict):
                raise ValueError(f"Config field 'run_grid.designs.{design}' must be an object")
            if "base_clock_period_ns" not in design_cfg:
                raise ValueError(f"Config field 'run_grid.designs.{design}' missing 'base_clock_period_ns'")
            base_clock_period = float(design_cfg["base_clock_period_ns"])
            flow_design = str(design_cfg.get("flow_design", design))
            for clock_scale in clock_scales:
                clock_period_ns = round(base_clock_period * clock_scale, 3)
                for abc_area in abc_area_values:
                    for place_density in place_density_values:
                        for rla in routing_values:
                            for scenario in scenarios:
                                scenario_id = str(scenario.get("scenario_id", "base"))
                                run_id = expected_run_id(
                                    str(design),
                                    clock_period_ns,
                                    int(abc_area),
                                    float(place_density),
                                    rla,
                                    scenario_id=scenario_id,
                                )
                                add_run(
                                    {
                                        "run_id": run_id,
                                        "design": str(design),
                                        "flow_design": flow_design,
                                        "clock_scale": float(clock_scale),
                                        "clock_period_ns": float(clock_period_ns),
                                        "abc_area": int(abc_area),
                                        "place_density": float(place_density),
                                        "routing_layer_adjustment": rla,
                                        "scenario_id": scenario_id,
                                        "scenario_mode": scenario.get("scenario_mode", "func"),
                                        "scenario_pvt": scenario.get("scenario_pvt", "typical"),
                                        "scenario_rc": scenario.get("scenario_rc", "typ"),
                                        "clock_uncertainty_ns": scenario.get("clock_uncertainty_ns"),
                                        "timing_derate_late": scenario.get("timing_derate_late"),
                                        "timing_derate_early": scenario.get("timing_derate_early"),
                                        "input_delay_scale": scenario.get("input_delay_scale"),
                                        "output_delay_scale": scenario.get("output_delay_scale"),
                                        "variant": run_id,
                                    },
                                    f"run_grid design '{design}'",
                                )
    return normalized


def generated_sdc_path(
    config: Mapping[str, object],
    design: str,
    clock_period_ns: float,
    root: Path | None = None,
    scenario_id: str | None = None,
) -> Path:
    base = repo_root() if root is None else root
    out_base = resolve_path(str(config["generated_sdc_dir"]), base)
    clk_ps = int(round(clock_period_ns * 1000.0))
    sc_tok = sanitize_token(scenario_id if scenario_id is not None else "base")
    if sc_tok == "base":
        fname = f"constraint_clk{clk_ps}ps.sdc"
    else:
        fname = f"constraint_clk{clk_ps}ps__sc{sc_tok}.sdc"
    return out_base / design / fname


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
