#!/usr/bin/env python3
"""Generate clock-period SDC variants for sweep runs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from pipeline_common import (
    dump_json,
    generated_sdc_path,
    load_json,
    planned_runs_from_config,
    repo_root,
    resolve_path,
)

SET_CLK_RE = re.compile(r"^(\s*set\s+clk_period\s+)([^\s]+)(.*)$")
CREATE_CLK_RE = re.compile(r"^(\s*create_clock\b.*?-period\s+)([^\s]+)(.*)$")
SET_INPUT_DELAY_NUM_RE = re.compile(r"^(\s*set_input_delay\s+)([-+]?[\d.]+(?:[eE][-+]?\d+)?)(\s+.*)$")
SET_OUTPUT_DELAY_NUM_RE = re.compile(r"^(\s*set_output_delay\s+)([-+]?[\d.]+(?:[eE][-+]?\d+)?)(\s+.*)$")


def _scale_numeric_delay_lines(template_text: str, scale: float, rgx: re.Pattern[str]) -> str:
    if abs(scale - 1.0) < 1e-12:
        return template_text
    out_lines = []
    for line in template_text.splitlines():
        m = rgx.match(line)
        if m:
            v = float(m.group(2)) * scale
            out_lines.append(f"{m.group(1)}{v:.6f}{m.group(3)}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def rewrite_sdc(
    template_text: str,
    clock_period_ns: float,
    clock_uncertainty_ns: float | None,
    clock_uncertainty_setup_ns: float | None,
    clock_uncertainty_hold_ns: float | None,
    timing_derate_late: float | None,
    timing_derate_early: float | None,
    input_delay_scale: float | None,
    output_delay_scale: float | None,
) -> str:
    lines = template_text.splitlines()

    replaced = False
    out_lines = []
    for line in lines:
        m = SET_CLK_RE.match(line)
        if m and not replaced:
            out_lines.append(f"{m.group(1)}{clock_period_ns:.3f}{m.group(3)}")
            replaced = True
        else:
            out_lines.append(line)
    if replaced:
        body = "\n".join(out_lines) + "\n"
    else:
        replaced = False
        out_lines = []
        for line in lines:
            m = CREATE_CLK_RE.match(line)
            if m and not replaced:
                out_lines.append(f"{m.group(1)}{clock_period_ns:.3f}{m.group(3)}")
                replaced = True
            else:
                out_lines.append(line)
        if not replaced:
            raise ValueError(
                "Could not find either 'set clk_period' or 'create_clock ... -period' in SDC template"
            )
        body = "\n".join(out_lines) + "\n"

    if input_delay_scale is not None:
        body = _scale_numeric_delay_lines(body, float(input_delay_scale), SET_INPUT_DELAY_NUM_RE)
    if output_delay_scale is not None:
        body = _scale_numeric_delay_lines(body, float(output_delay_scale), SET_OUTPUT_DELAY_NUM_RE)

    scenario_lines = []
    if clock_uncertainty_setup_ns is not None or clock_uncertainty_hold_ns is not None:
        if clock_uncertainty_setup_ns is not None:
            scenario_lines.append(
                f"set_clock_uncertainty -setup {float(clock_uncertainty_setup_ns):.6f} [all_clocks]"
            )
        if clock_uncertainty_hold_ns is not None:
            scenario_lines.append(
                f"set_clock_uncertainty -hold {float(clock_uncertainty_hold_ns):.6f} [all_clocks]"
            )
    elif clock_uncertainty_ns is not None:
        scenario_lines.append(f"set_clock_uncertainty {float(clock_uncertainty_ns):.6f} [all_clocks]")
    if timing_derate_early is not None and abs(float(timing_derate_early) - 1.0) > 1e-12:
        scenario_lines.append(f"set_timing_derate -early {float(timing_derate_early):.6f}")
    if timing_derate_late is not None and abs(float(timing_derate_late) - 1.0) > 1e-12:
        scenario_lines.append(f"set_timing_derate -late {float(timing_derate_late):.6f}")
    if scenario_lines:
        body += "\n# Scenario overrides\n" + "\n".join(scenario_lines) + "\n"
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate SDC files for all configured runs")
    parser.add_argument("--config", default="configs/sweep_phase1.json", help="Path to sweep config JSON")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing generated SDC files")
    args = parser.parse_args()

    root = repo_root()
    config_path = resolve_path(args.config, root)
    config = load_json(config_path)
    runs = planned_runs_from_config(config)

    template_map = config.get("template_sdc")
    if not isinstance(template_map, dict):
        raise ValueError("Config must define 'template_sdc' mapping")

    written = 0
    skipped = 0
    seen = set()
    for run in runs:
        design = run["design"]
        period = run["clock_period_ns"]
        scenario_id = str(run.get("scenario_id", "base"))
        clock_uncertainty_ns = run.get("clock_uncertainty_ns")
        clock_uncertainty_setup_ns = run.get("clock_uncertainty_setup_ns")
        clock_uncertainty_hold_ns = run.get("clock_uncertainty_hold_ns")
        timing_derate_late = run.get("timing_derate_late")
        timing_derate_early = run.get("timing_derate_early")
        input_delay_scale = run.get("input_delay_scale")
        output_delay_scale = run.get("output_delay_scale")
        key = (
            design,
            period,
            scenario_id,
            clock_uncertainty_ns,
            clock_uncertainty_setup_ns,
            clock_uncertainty_hold_ns,
            timing_derate_late,
            timing_derate_early,
            input_delay_scale,
            output_delay_scale,
        )
        if key in seen:
            continue
        seen.add(key)

        if design not in template_map:
            raise ValueError(f"No template SDC configured for design '{design}'")

        template_path = resolve_path(str(template_map[design]), root)
        template_text = template_path.read_text(encoding="utf-8")

        out_path = generated_sdc_path(config, design, period, root, scenario_id=scenario_id)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if out_path.exists() and not args.overwrite:
            skipped += 1
            continue

        body = rewrite_sdc(
            template_text=template_text,
            clock_period_ns=period,
            clock_uncertainty_ns=clock_uncertainty_ns,
            clock_uncertainty_setup_ns=clock_uncertainty_setup_ns,
            clock_uncertainty_hold_ns=clock_uncertainty_hold_ns,
            timing_derate_late=timing_derate_late,
            timing_derate_early=timing_derate_early,
            input_delay_scale=input_delay_scale,
            output_delay_scale=output_delay_scale,
        )
        header = (
            "# AUTO-GENERATED by scripts/gen_sdc_variants.py\n"
            f"# Template: {template_path}\n"
            f"# Clock period (ns): {period:.3f}\n"
            f"# Scenario ID: {scenario_id}\n"
            f"# Clock uncertainty (ns): {clock_uncertainty_ns}\n"
            f"# Clock uncertainty setup (ns): {clock_uncertainty_setup_ns}\n"
            f"# Clock uncertainty hold (ns): {clock_uncertainty_hold_ns}\n"
            f"# Timing derate early: {timing_derate_early}\n"
            f"# Timing derate late: {timing_derate_late}\n"
            f"# Input delay scale: {input_delay_scale}\n"
            f"# Output delay scale: {output_delay_scale}\n"
        )
        out_path.write_text(header + body, encoding="utf-8")
        written += 1

    print(f"Generated SDC files: written={written}, skipped_existing={skipped}, unique_targets={len(seen)}")


if __name__ == "__main__":
    main()
