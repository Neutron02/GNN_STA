#!/usr/bin/env python3
"""Replay STA extraction on existing routed designs for new scenario assumptions.

This avoids rerunning synthesis/place/route by reusing:
- 6_final.odb
- 6_final.spef
- 6_final.v / 6_final.def / 6_net_rc.csv

and regenerating only:
- scenario-specific 6_final.sdc
- extracted graph/path labels under data/processed/<run_id>
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from gen_sdc_variants import rewrite_sdc
from pipeline_common import (
    expected_run_id,
    parse_optional_float,
    repo_root,
    resolve_path,
    sanitize_token,
    utc_now_iso,
)

RUNS_REQUIRED_FIELDS = [
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
    "clock_uncertainty_setup_ns",
    "clock_uncertainty_hold_ns",
    "timing_derate_late",
    "timing_derate_early",
    "input_delay_scale",
    "output_delay_scale",
    "variant",
    "sdc_file",
    "status",
    "last_error",
    "last_update_utc",
    "source_run_id",
]

RAW_LINK_FILES = [
    "6_final.odb",
    "6_final.def",
    "6_final.v",
    "6_final.spef",
    "6_net_rc.csv",
    "6_finish.rpt",
]

PROCESSED_REQUIRED = [
    "nodes.csv",
    "edges.csv",
    "labels_setup_max.csv",
    "global_features.json",
    "paths_setup_max.json",
    "paths_summary.csv",
]


def _read_csv(path: Path) -> tuple[List[str], List[Dict[str, str]]]:
    if not path.exists():
        return ([], [])
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    return fields, rows


def _write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def _build_runs_fields(existing_fields: List[str]) -> List[str]:
    out = []
    seen = set()
    for f in existing_fields + RUNS_REQUIRED_FIELDS:
        if f and f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "":
            return default
        return float(s)
    except Exception:
        return default


def _load_json(path: Path) -> Dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _scenario_list(path: Path) -> List[Dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        items = payload.get("scenarios", [])
    elif isinstance(payload, list):
        items = payload
    else:
        raise ValueError("Scenario JSON must be a list or object with key 'scenarios'")
    out: List[Dict[str, object]] = []
    for idx, sc in enumerate(items):
        if isinstance(sc, str):
            sc = {"scenario_id": sc}
        if not isinstance(sc, dict):
            raise ValueError(f"Scenario index {idx} must be object or string")
        sid = sanitize_token(sc.get("scenario_id", "base"))
        if sid == "base":
            continue
        out.append(
            {
                "scenario_id": sid,
                "scenario_mode": str(sc.get("scenario_mode", "func")).strip() or "func",
                "scenario_pvt": str(sc.get("scenario_pvt", "typical")).strip() or "typical",
                "scenario_rc": str(sc.get("scenario_rc", "typ")).strip() or "typ",
                "clock_uncertainty_ns": parse_optional_float(sc.get("clock_uncertainty_ns")),
                "clock_uncertainty_setup_ns": parse_optional_float(sc.get("clock_uncertainty_setup_ns")),
                "clock_uncertainty_hold_ns": parse_optional_float(sc.get("clock_uncertainty_hold_ns")),
                "timing_derate_late": parse_optional_float(sc.get("timing_derate_late")),
                "timing_derate_early": parse_optional_float(sc.get("timing_derate_early")),
                "input_delay_scale": parse_optional_float(sc.get("input_delay_scale")),
                "output_delay_scale": parse_optional_float(sc.get("output_delay_scale")),
            }
        )
    if not out:
        raise ValueError("No non-base scenarios found in scenario JSON.")
    return out


def _processed_ok(run_id: str, root: Path) -> bool:
    d = root / "data" / "processed" / run_id
    return all((d / n).exists() for n in PROCESSED_REQUIRED)


def _validate(
    run_id: str,
    root: Path,
    min_finite_coverage: float,
    allow_wns_mismatch: bool,
) -> bool:
    cmd = [
        "python3",
        "scripts/validate_dataset.py",
        "--run-id",
        run_id,
        "--min-finite-coverage",
        f"{min_finite_coverage}",
    ]
    if allow_wns_mismatch:
        cmd.append("--allow-wns-mismatch")
    p = subprocess.run(
        cmd,
        cwd=str(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return p.returncode == 0


def _copy_or_link(src: Path, dst: Path, symlink: bool) -> None:
    if dst.exists() or dst.is_symlink():
        if dst.is_dir():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    if symlink:
        dst.symlink_to(src)
    else:
        shutil.copy2(src, dst)


def _build_subprocess_env() -> Dict[str, str]:
    env = os.environ.copy()
    if Path("/usr/bin/python3").exists():
        path = env.get("PATH", "")
        prefix = "/usr/bin:/bin"
        env["PATH"] = f"{prefix}:{path}" if path else prefix
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def _run_cmd(cmd: List[str], cwd: Path, log_file: Path, dry_run: bool) -> None:
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
            env=_build_subprocess_env(),
        )


def _slack_stats_from_csv(path: Path) -> Optional[Dict[str, float]]:
    if not path.exists():
        return None
    slacks: List[float] = []
    with path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                v = float(str(row.get("slack_s", "")).strip())
            except Exception:
                continue
            if v == v and abs(v) < 1e30:  # finite/non-NaN guard
                slacks.append(v)
    if not slacks:
        return None
    return {
        "path_count": float(len(slacks)),
        "wns_ns": min(slacks) * 1e9,
        "tns_ns": sum(v for v in slacks if v < 0.0) * 1e9,
    }


def _patch_global_features(processed_dir: Path, run_id: str, scenario: Mapping[str, object], source_run_id: str) -> None:
    global_json = processed_dir / "global_features.json"
    payload = _load_json(global_json)

    setup_stats = _slack_stats_from_csv(processed_dir / "paths_summary.csv")
    hold_stats = _slack_stats_from_csv(processed_dir / "paths_hold_summary.csv")

    if setup_stats is not None:
        payload["wns_ns"] = setup_stats["wns_ns"]  # legacy setup alias
        payload["worst_slack_ns"] = setup_stats["wns_ns"]
        payload["tns_ns"] = setup_stats["tns_ns"]  # legacy setup alias
        payload["wns_setup_ns"] = setup_stats["wns_ns"]
        payload["tns_setup_ns"] = setup_stats["tns_ns"]
        payload["setup_path_count"] = int(setup_stats["path_count"])

    if hold_stats is not None:
        payload["wns_hold_ns"] = hold_stats["wns_ns"]
        payload["tns_hold_ns"] = hold_stats["tns_ns"]
        payload["hold_path_count"] = int(hold_stats["path_count"])

    setup_wns = payload.get("wns_setup_ns")
    hold_wns = payload.get("wns_hold_ns")
    try:
        setup_v = float(setup_wns) if setup_wns is not None else float("nan")
    except Exception:
        setup_v = float("nan")
    try:
        hold_v = float(hold_wns) if hold_wns is not None else float("nan")
    except Exception:
        hold_v = float("nan")
    candidates = [v for v in [setup_v, hold_v] if v == v]
    if candidates:
        payload["wns_combined_ns"] = min(candidates)
        if len(candidates) == 2:
            payload["dominant_check"] = "setup" if setup_v <= hold_v else "hold"

    payload["run_id"] = run_id
    payload["source_run_id"] = source_run_id
    payload["scenario_id"] = scenario.get("scenario_id", "base")
    payload["scenario_mode"] = scenario.get("scenario_mode", "func")
    payload["scenario_pvt"] = scenario.get("scenario_pvt", "typical")
    payload["scenario_rc"] = scenario.get("scenario_rc", "typ")
    payload["clock_uncertainty_ns"] = scenario.get("clock_uncertainty_ns")
    payload["clock_uncertainty_setup_ns"] = scenario.get("clock_uncertainty_setup_ns")
    payload["clock_uncertainty_hold_ns"] = scenario.get("clock_uncertainty_hold_ns")
    payload["timing_derate_late"] = scenario.get("timing_derate_late")
    payload["timing_derate_early"] = scenario.get("timing_derate_early")
    payload["input_delay_scale"] = scenario.get("input_delay_scale")
    payload["output_delay_scale"] = scenario.get("output_delay_scale")

    global_json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _run_params(base_row: Mapping[str, str]) -> Dict[str, object]:
    g = _load_json(Path(str(base_row.get("global_json", ""))))
    design = str(base_row.get("design", "")).strip()
    clock_period_ns = _to_float(base_row.get("clock_period_ns"), _to_float(g.get("clock_period_ns"), 0.0))
    clock_scale = _to_float(base_row.get("clock_scale"), 1.0)
    abc_area = int(round(_to_float(base_row.get("abc_area"), _to_float(g.get("abc_area"), 0.0))))
    place_density = _to_float(base_row.get("place_density"), _to_float(g.get("place_density"), 0.0))
    rla = parse_optional_float(base_row.get("routing_layer_adjustment"))
    if rla is None:
        rla = parse_optional_float(g.get("routing_layer_adjustment"))
    return {
        "design": design,
        "clock_period_ns": clock_period_ns,
        "clock_scale": clock_scale,
        "abc_area": abc_area,
        "place_density": place_density,
        "routing_layer_adjustment": rla,
    }


def _status_line(done: int, total: int, ok: int, fail: int, t0: float) -> str:
    elapsed = max(0.0, time.time() - t0)
    eta = None
    if done > 0 and total > done:
        eta = (elapsed / done) * (total - done)
    def fmt(sec: Optional[float]) -> str:
        if sec is None:
            return "--:--:--"
        s = int(round(max(0.0, sec)))
        h, rem = divmod(s, 3600)
        m, ss = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{ss:02d}"
    return f"{done}/{total} ok={ok} fail={fail} elapsed={fmt(elapsed)} eta={fmt(eta)}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay extracted STA for scenario variants without rerunning place/route")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--runs-csv", default="data/manifests/runs.csv")
    ap.add_argument("--scenarios-json", default="configs/scenario_replay_pilot.json")
    ap.add_argument("--openroad-bin", default="OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad")
    ap.add_argument("--liberty", default="OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--run-id", action="append", default=[], help="Base run_id filter (repeatable)")
    ap.add_argument("--max-runs", type=int, default=0, help="Limit number of replay runs")
    ap.add_argument("--no-symlink-raw", action="store_true", help="Copy raw artifacts instead of symlinking")
    ap.add_argument("--paths-only", action="store_true", help="Only rerun path extraction + metrics patch")
    ap.add_argument(
        "--validate-min-finite-coverage",
        type=float,
        default=0.65,
        help="Coverage threshold passed to validate_dataset.py.",
    )
    ap.add_argument(
        "--strict-wns-check",
        action="store_true",
        help="If set, do not pass --allow-wns-mismatch to validate_dataset.py.",
    )
    ap.add_argument(
        "--resume-verify-success",
        action="store_true",
        help="When --resume is set, re-run validation checks before skipping existing success rows.",
    )
    args = ap.parse_args()

    root = repo_root()
    dataset_fields, dataset_rows = _read_csv(resolve_path(args.dataset_index, root))
    if not dataset_rows:
        raise SystemExit(f"No rows found in dataset index: {args.dataset_index}")
    _ = dataset_fields  # keep for potential debug

    scenarios = _scenario_list(resolve_path(args.scenarios_json, root))
    openroad_bin = str(resolve_path(args.openroad_bin, root))
    liberty = str(resolve_path(args.liberty, root))

    runs_csv_path = resolve_path(args.runs_csv, root)
    runs_fields, runs_rows = _read_csv(runs_csv_path)
    runs_fields = _build_runs_fields(runs_fields)
    rows_by_run: Dict[str, Dict[str, object]] = {str(r.get("run_id", "")): dict(r) for r in runs_rows}
    lock = threading.Lock()

    base_filter = set(args.run_id)
    base_rows: List[Dict[str, str]] = []
    for row in dataset_rows:
        rid = str(row.get("run_id", "")).strip()
        if not rid:
            continue
        if base_filter and rid not in base_filter:
            continue
        scid = str(row.get("scenario_id", "base")).strip().lower()
        if scid not in {"", "base"}:
            continue
        if str(row.get("status", "")).strip().lower() != "success":
            continue
        if str(row.get("validation_passed", "")).strip().lower() not in {"1", "true"}:
            continue
        base_rows.append(row)

    tasks: List[Dict[str, object]] = []
    for base in base_rows:
        params = _run_params(base)
        source_run_id = str(base.get("run_id", "")).strip()
        for sc in scenarios:
            run_id = expected_run_id(
                design=str(params["design"]),
                clock_period_ns=float(params["clock_period_ns"]),
                abc_area=int(params["abc_area"]),
                place_density=float(params["place_density"]),
                rla=parse_optional_float(params["routing_layer_adjustment"]),
                scenario_id=str(sc.get("scenario_id", "base")),
            )
            status = str(rows_by_run.get(run_id, {}).get("status", "")).strip().lower()
            if args.resume and status == "success" and _processed_ok(run_id, root):
                if args.resume_verify_success and not _validate(
                    run_id,
                    root,
                    min_finite_coverage=float(args.validate_min_finite_coverage),
                    allow_wns_mismatch=not args.strict_wns_check,
                ):
                    pass
                else:
                    continue
            tasks.append(
                {
                    "run_id": run_id,
                    "source_run_id": source_run_id,
                    "scenario": sc,
                    "base": base,
                    "params": params,
                }
            )

    if args.max_runs > 0:
        tasks = tasks[: args.max_runs]

    print(f"Selected replay runs: {len(tasks)} (bases={len(base_rows)}, scenarios={len(scenarios)})")
    if args.dry_run:
        for t in tasks:
            print(
                json.dumps(
                    {
                        "run_id": t["run_id"],
                        "source_run_id": t["source_run_id"],
                        "scenario_id": t["scenario"]["scenario_id"],
                        "design": t["params"]["design"],
                    }
                )
            )
        return

    log_dir = root / "logs" / "pipeline"
    log_dir.mkdir(parents=True, exist_ok=True)

    def update_runs_row(run_id: str, payload: Dict[str, object], status: str, error: str = "") -> None:
        with lock:
            row = dict(rows_by_run.get(run_id, {}))
            row.update(payload)
            row["status"] = status
            row["last_error"] = error
            row["last_update_utc"] = utc_now_iso()
            rows_by_run[run_id] = row
            rows_out = [rows_by_run[k] for k in sorted(rows_by_run.keys()) if k]
            _write_csv(runs_csv_path, runs_fields, rows_out)

    def do_task(task: Dict[str, object]) -> Dict[str, object]:
        run_id = str(task["run_id"])
        source_run_id = str(task["source_run_id"])
        scenario = dict(task["scenario"])
        base = dict(task["base"])
        params = dict(task["params"])
        log_file = log_dir / f"{run_id}.log"
        design = str(params["design"])
        clock_period_ns = float(params["clock_period_ns"])
        clock_scale = float(params["clock_scale"])
        abc_area = int(params["abc_area"])
        place_density = float(params["place_density"])
        rla = parse_optional_float(params.get("routing_layer_adjustment"))

        raw_dir = root / "data" / "raw_curated" / run_id
        processed_dir = root / "data" / "processed" / run_id
        base_raw_dir = Path(str(base["raw_dir"]))
        base_sdc = base_raw_dir / "6_final.sdc"

        row_payload = {
            "run_id": run_id,
            "source_run_id": source_run_id,
            "design": design,
            "clock_scale": clock_scale,
            "clock_period_ns": clock_period_ns,
            "abc_area": abc_area,
            "place_density": place_density,
            "routing_layer_adjustment": rla,
            "scenario_id": scenario.get("scenario_id", "base"),
            "scenario_mode": scenario.get("scenario_mode", "func"),
            "scenario_pvt": scenario.get("scenario_pvt", "typical"),
            "scenario_rc": scenario.get("scenario_rc", "typ"),
            "clock_uncertainty_ns": scenario.get("clock_uncertainty_ns"),
            "clock_uncertainty_setup_ns": scenario.get("clock_uncertainty_setup_ns"),
            "clock_uncertainty_hold_ns": scenario.get("clock_uncertainty_hold_ns"),
            "timing_derate_late": scenario.get("timing_derate_late"),
            "timing_derate_early": scenario.get("timing_derate_early"),
            "input_delay_scale": scenario.get("input_delay_scale"),
            "output_delay_scale": scenario.get("output_delay_scale"),
            "variant": run_id,
            "sdc_file": str(raw_dir / "6_final.sdc"),
        }
        update_runs_row(run_id, row_payload, "running")

        try:
            raw_dir.mkdir(parents=True, exist_ok=True)
            processed_dir.mkdir(parents=True, exist_ok=True)

            for name in RAW_LINK_FILES:
                src = base_raw_dir / name
                if not src.exists():
                    raise FileNotFoundError(f"Missing base raw artifact: {src}")
                _copy_or_link(src, raw_dir / name, symlink=not args.no_symlink_raw)

            if not base_sdc.exists():
                raise FileNotFoundError(f"Missing base SDC for replay: {base_sdc}")
            base_sdc_text = base_sdc.read_text(encoding="utf-8")
            sdc_text = rewrite_sdc(
                template_text=base_sdc_text,
                clock_period_ns=clock_period_ns,
                clock_uncertainty_ns=scenario.get("clock_uncertainty_ns"),
                clock_uncertainty_setup_ns=scenario.get("clock_uncertainty_setup_ns"),
                clock_uncertainty_hold_ns=scenario.get("clock_uncertainty_hold_ns"),
                timing_derate_late=scenario.get("timing_derate_late"),
                timing_derate_early=scenario.get("timing_derate_early"),
                input_delay_scale=scenario.get("input_delay_scale"),
                output_delay_scale=scenario.get("output_delay_scale"),
            )
            (raw_dir / "6_final.sdc").write_text(sdc_text, encoding="utf-8")

            base_meta = _load_json(base_raw_dir / "run_meta.json")
            replay_meta = dict(base_meta)
            replay_meta.update(
                {
                    "run_id": run_id,
                    "source_run_id": source_run_id,
                    "status": "success",
                    "collected_utc": utc_now_iso(),
                    "design": design,
                    "clock_scale": clock_scale,
                    "clock_period_ns": clock_period_ns,
                    "abc_area": abc_area,
                    "place_density": place_density,
                    "routing_layer_adjustment": rla,
                    "scenario_id": scenario.get("scenario_id", "base"),
                    "scenario_mode": scenario.get("scenario_mode", "func"),
                    "scenario_pvt": scenario.get("scenario_pvt", "typical"),
                    "scenario_rc": scenario.get("scenario_rc", "typ"),
                    "clock_uncertainty_ns": scenario.get("clock_uncertainty_ns"),
                    "clock_uncertainty_setup_ns": scenario.get("clock_uncertainty_setup_ns"),
                    "clock_uncertainty_hold_ns": scenario.get("clock_uncertainty_hold_ns"),
                    "timing_derate_late": scenario.get("timing_derate_late"),
                    "timing_derate_early": scenario.get("timing_derate_early"),
                    "input_delay_scale": scenario.get("input_delay_scale"),
                    "output_delay_scale": scenario.get("output_delay_scale"),
                }
            )
            (raw_dir / "run_meta.json").write_text(json.dumps(replay_meta, indent=2) + "\n", encoding="utf-8")

            odb = raw_dir / "6_final.odb"
            sdc = raw_dir / "6_final.sdc"
            spef = raw_dir / "6_final.spef"

            if not args.paths_only:
                _run_cmd(
                    [
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
                    ],
                    cwd=root,
                    log_file=log_file,
                    dry_run=False,
                )
            _run_cmd(
                [
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
                ],
                cwd=root,
                log_file=log_file,
                dry_run=False,
            )

            _patch_global_features(processed_dir, run_id, scenario, source_run_id)

            validate_cmd = [
                "python3",
                "scripts/validate_dataset.py",
                "--run-id",
                run_id,
                "--min-finite-coverage",
                f"{float(args.validate_min_finite_coverage)}",
            ]
            if not args.strict_wns_check:
                validate_cmd.append("--allow-wns-mismatch")
            _run_cmd(
                validate_cmd,
                cwd=root,
                log_file=log_file,
                dry_run=False,
            )

            update_runs_row(run_id, row_payload, "success", "")
            return {"run_id": run_id, "status": "success"}
        except subprocess.CalledProcessError as exc:
            update_runs_row(run_id, row_payload, "failed", f"command failed with exit {exc.returncode}")
            return {"run_id": run_id, "status": "failed", "error": f"exit {exc.returncode}"}
        except Exception as exc:
            update_runs_row(run_id, row_payload, "failed", str(exc))
            return {"run_id": run_id, "status": "failed", "error": str(exc)}

    t0 = time.time()
    ok = 0
    fail = 0
    done = 0
    results = []
    print(_status_line(done, len(tasks), ok, fail, t0))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(args.jobs))) as ex:
        futs = [ex.submit(do_task, t) for t in tasks]
        for fut in concurrent.futures.as_completed(futs):
            r = fut.result()
            results.append(r)
            done += 1
            if r.get("status") == "success":
                ok += 1
            else:
                fail += 1
            print(f"{_status_line(done, len(tasks), ok, fail, t0)} last={r.get('run_id')}:{r.get('status')}")

    subprocess.run(["python3", "scripts/build_split_manifest.py"], cwd=str(root), check=True)
    print(
        json.dumps(
            {
                "selected_runs": len(tasks),
                "success": ok,
                "failed": fail,
                "runs_csv": str(runs_csv_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
