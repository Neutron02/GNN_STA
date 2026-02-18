#!/usr/bin/env python3
"""Extract pin-level timing graph + setup-max labels using OpenROAD Python API."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from openroad import Design, Tech, Timing

from pipeline_common import dump_json, parse_finish_report_metrics


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return float("nan")


def is_finite(value: float) -> bool:
    return isinstance(value, float) and math.isfinite(value)


def finite_max(values: Iterable[float]) -> float:
    vals = [v for v in values if is_finite(v)]
    return max(vals) if vals else float("nan")


def finite_min(values: Iterable[float]) -> float:
    vals = [v for v in values if is_finite(v)]
    return min(vals) if vals else float("nan")


def finite_sum(values: Iterable[float]) -> float:
    vals = [v for v in values if is_finite(v)]
    return sum(vals) if vals else float("nan")


def time_or_nan(fn, timing: Timing) -> float:
    try:
        val = float(fn())
    except Exception:
        return float("nan")
    try:
        if timing.isTimeInf(val):
            return float("nan")
    except Exception:
        pass
    return val


def parse_rcx_csv(path: Path) -> Dict[str, Tuple[float, float]]:
    rcx = {}
    if not path.exists():
        return rcx
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            if row[0].startswith("#"):
                continue
            if len(row) < 8:
                continue
            net_name = row[0]
            try:
                rcx_res = float(row[6])
                rcx_cap = float(row[7])
            except ValueError:
                continue
            rcx[net_name] = (rcx_res, rcx_cap)
    return rcx


def dbu_xy_to_um(block, value, fallback=(float("nan"), float("nan"))):
    if value is None:
        return fallback
    try:
        if isinstance(value, (list, tuple)):
            if len(value) == 2:
                x, y = value
            elif len(value) == 3 and isinstance(value[0], (bool, int)):
                _, x, y = value
            else:
                return fallback
            return float(block.dbuToMicrons(x)), float(block.dbuToMicrons(y))
    except Exception:
        return fallback
    return fallback


def term_xy_um(block, term, kind: str, inst=None):
    if kind == "iterm":
        try:
            xy = term.getAvgXY()
            x, y = dbu_xy_to_um(block, xy)
            if is_finite(x) and is_finite(y):
                return x, y
        except Exception:
            pass
        if inst is not None:
            return dbu_xy_to_um(block, inst.getLocation())
    if kind == "bterm":
        try:
            x, y = dbu_xy_to_um(block, term.getFirstPinLocation())
            if is_finite(x) and is_finite(y):
                return x, y
        except Exception:
            pass
    return float("nan"), float("nan")


def load_run_meta(raw_dir: Path) -> Dict:
    meta_path = raw_dir / "run_meta.json"
    if not meta_path.exists():
        return {}
    with meta_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract graph + setup labels from finalized design artifacts")
    parser.add_argument("--odb", required=True)
    parser.add_argument("--sdc", required=True)
    parser.add_argument("--spef", required=True)
    parser.add_argument("--liberty", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    odb_path = Path(args.odb).resolve()
    sdc_path = Path(args.sdc).resolve()
    spef_path = Path(args.spef).resolve()
    liberty_path = Path(args.liberty).resolve()
    raw_dir = Path(args.raw_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tech = Tech()
    tech.readLiberty(str(liberty_path))

    design = Design(tech)
    design.readDb(str(odb_path))
    design.evalTclString(f"read_sdc {sdc_path}")
    design.evalTclString(f"read_spef {spef_path}")

    timing = Timing(design)
    corners = timing.getCorners()
    if not corners:
        raise RuntimeError("No timing corner available after loading SDC/SPEF")
    corner = corners[0]

    block = design.getBlock()
    rcx_map = parse_rcx_csv(raw_dir / "6_net_rc.csv")

    nodes: List[Dict] = []
    node_name_to_id: Dict[str, int] = {}
    node_terms: Dict[int, Tuple[str, object]] = {}

    def add_iterm(iterm) -> int:
        node_name = design.getITermName(iterm)
        if node_name in node_name_to_id:
            return node_name_to_id[node_name]

        inst = iterm.getInst()
        master = inst.getMaster()
        mterm = iterm.getMTerm()

        inst_x_um, inst_y_um = dbu_xy_to_um(block, inst.getLocation())
        x_um, y_um = term_xy_um(block, iterm, "iterm", inst=inst)

        node_id = len(nodes)
        node_name_to_id[node_name] = node_id
        node_terms[node_id] = ("iterm", iterm)

        area_um2 = float("nan")
        try:
            area_um2 = float(block.dbuAreaToMicrons(master.getArea()))
        except Exception:
            pass

        nodes.append(
            {
                "node_id": node_id,
                "node_name": node_name,
                "node_kind": "iterm",
                "inst_name": inst.getName(),
                "cell_name": master.getName(),
                "port_name": mterm.getName(),
                "io_type": str(iterm.getIoType()),
                "is_sequential_cell": int(bool(design.isSequential(master))),
                "is_buffer_cell": int(bool(design.isBuffer(master))),
                "is_inverter_cell": int(bool(design.isInverter(master))),
                "is_clock_pin": int(bool(design.isInClock(iterm))),
                "is_endpoint": int(bool(timing.isEndpoint(iterm))),
                "x_um": x_um,
                "y_um": y_um,
                "inst_x_um": inst_x_um,
                "inst_y_um": inst_y_um,
                "cell_area_um2": area_um2,
                "port_cap_max_f": time_or_nan(lambda: timing.getPortCap(iterm, corner, Timing.Max), timing),
                "port_cap_min_f": time_or_nan(lambda: timing.getPortCap(iterm, corner, Timing.Min), timing),
            }
        )
        return node_id

    def add_bterm(bterm) -> int:
        node_name = bterm.getName()
        if node_name in node_name_to_id:
            return node_name_to_id[node_name]

        x_um, y_um = term_xy_um(block, bterm, "bterm")
        is_clock_pin = 0
        try:
            is_clock_pin = int(bool(design.isInClock(bterm)))
        except Exception:
            if str(bterm.getSigType()) == "CLOCK":
                is_clock_pin = 1

        node_id = len(nodes)
        node_name_to_id[node_name] = node_id
        node_terms[node_id] = ("bterm", bterm)

        nodes.append(
            {
                "node_id": node_id,
                "node_name": node_name,
                "node_kind": "bterm",
                "inst_name": "",
                "cell_name": "",
                "port_name": bterm.getName(),
                "io_type": str(bterm.getIoType()),
                "is_sequential_cell": 0,
                "is_buffer_cell": 0,
                "is_inverter_cell": 0,
                "is_clock_pin": is_clock_pin,
                "is_endpoint": 0,
                "x_um": x_um,
                "y_um": y_um,
                "inst_x_um": float("nan"),
                "inst_y_um": float("nan"),
                "cell_area_um2": float("nan"),
                "port_cap_max_f": time_or_nan(lambda: timing.getPortCap(bterm, corner, Timing.Max), timing),
                "port_cap_min_f": time_or_nan(lambda: timing.getPortCap(bterm, corner, Timing.Min), timing),
            }
        )
        return node_id

    eligible_sig_types = {"SIGNAL", "CLOCK"}
    eligible_nets = []

    for net in block.getNets():
        sig_type = str(net.getSigType())
        if sig_type not in eligible_sig_types:
            continue
        eligible_nets.append(net)

        for iterm in net.getITerms():
            add_iterm(iterm)
        for bterm in net.getBTerms():
            add_bterm(bterm)

    edges: List[Dict] = []
    edge_id = 0

    def add_edge(row: Dict):
        nonlocal edge_id
        row["edge_id"] = edge_id
        edges.append(row)
        edge_id += 1

    for net in eligible_nets:
        net_name = net.getName()
        sig_type = str(net.getSigType())

        drivers = []
        sinks = []

        for iterm in net.getITerms():
            node_name = design.getITermName(iterm)
            node_id = node_name_to_id.get(node_name)
            if node_id is None:
                continue
            io_type = str(iterm.getIoType())
            if io_type == "OUTPUT":
                drivers.append(node_id)
            elif io_type == "INPUT":
                sinks.append(node_id)

        for bterm in net.getBTerms():
            node_name = bterm.getName()
            node_id = node_name_to_id.get(node_name)
            if node_id is None:
                continue
            io_type = str(bterm.getIoType())
            if io_type == "INPUT":
                drivers.append(node_id)
            elif io_type == "OUTPUT":
                sinks.append(node_id)

        fanout = len(sinks)
        net_routed_len_um = safe_float(design.getNetRoutedLength(net))
        net_cap_max = time_or_nan(lambda: timing.getNetCap(net, corner, Timing.Max), timing)
        net_cap_min = time_or_nan(lambda: timing.getNetCap(net, corner, Timing.Min), timing)
        rcx_res, rcx_cap = rcx_map.get(net_name, (float("nan"), float("nan")))

        for src in drivers:
            for dst in sinks:
                if src == dst:
                    continue
                add_edge(
                    {
                        "src_node_id": src,
                        "dst_node_id": dst,
                        "edge_type": "net",
                        "net_name": net_name,
                        "net_sig_type": sig_type,
                        "net_fanout": fanout,
                        "net_routed_length_um": net_routed_len_um,
                        "net_cap_max_f": net_cap_max,
                        "net_cap_min_f": net_cap_min,
                        "net_wire_res_ohm_rcx": rcx_res,
                        "net_wire_cap_f_rcx": rcx_cap,
                        "cell_arc_master": "",
                        "cell_arc_from_pin": "",
                        "cell_arc_to_pin": "",
                    }
                )

    arc_seen = set()
    for inst in block.getInsts():
        master = inst.getMaster()
        if design.isSequential(master):
            continue

        inst_iterm_map = {iterm.getMTerm().getName(): iterm for iterm in inst.getITerms()}

        for mterm_in in master.getMTerms():
            if str(mterm_in.getIoType()) not in {"INPUT", "INOUT"}:
                continue
            try:
                fanouts = timing.getTimingFanoutFrom(mterm_in)
            except Exception:
                continue

            src_iterm = inst_iterm_map.get(mterm_in.getName())
            if src_iterm is None:
                continue
            src_name = design.getITermName(src_iterm)
            src_id = node_name_to_id.get(src_name)
            if src_id is None:
                continue

            for mterm_out in fanouts:
                out_name = mterm_out.getName()
                if str(mterm_out.getIoType()) not in {"OUTPUT", "INOUT"}:
                    continue
                dst_iterm = inst_iterm_map.get(out_name)
                if dst_iterm is None:
                    continue
                dst_name = design.getITermName(dst_iterm)
                dst_id = node_name_to_id.get(dst_name)
                if dst_id is None or src_id == dst_id:
                    continue

                arc_key = (src_id, dst_id, master.getName(), mterm_in.getName(), out_name)
                if arc_key in arc_seen:
                    continue
                arc_seen.add(arc_key)

                add_edge(
                    {
                        "src_node_id": src_id,
                        "dst_node_id": dst_id,
                        "edge_type": "cell_arc",
                        "net_name": "",
                        "net_sig_type": "",
                        "net_fanout": "",
                        "net_routed_length_um": float("nan"),
                        "net_cap_max_f": float("nan"),
                        "net_cap_min_f": float("nan"),
                        "net_wire_res_ohm_rcx": float("nan"),
                        "net_wire_cap_f_rcx": float("nan"),
                        "cell_arc_master": master.getName(),
                        "cell_arc_from_pin": mterm_in.getName(),
                        "cell_arc_to_pin": out_name,
                    }
                )

    labels: List[Dict] = []
    for node in nodes:
        node_id = node["node_id"]
        kind, term = node_terms[node_id]

        arr_r = time_or_nan(lambda: timing.getPinArrival(term, Timing.Rise), timing)
        arr_f = time_or_nan(lambda: timing.getPinArrival(term, Timing.Fall), timing)
        slk_r = time_or_nan(lambda: timing.getPinSlack(term, Timing.Rise, Timing.Max), timing)
        slk_f = time_or_nan(lambda: timing.getPinSlack(term, Timing.Fall, Timing.Max), timing)

        req_r = arr_r + slk_r if is_finite(arr_r) and is_finite(slk_r) else float("nan")
        req_f = arr_f + slk_f if is_finite(arr_f) and is_finite(slk_f) else float("nan")

        arr_scalar = finite_max([arr_r, arr_f])
        slk_scalar = finite_min([slk_r, slk_f])
        req_scalar = arr_scalar + slk_scalar if is_finite(arr_scalar) and is_finite(slk_scalar) else float("nan")

        labels.append(
            {
                "node_id": node_id,
                "arrival_rise_s": arr_r,
                "arrival_fall_s": arr_f,
                "slack_rise_setup_max_s": slk_r,
                "slack_fall_setup_max_s": slk_f,
                "required_rise_setup_max_s": req_r,
                "required_fall_setup_max_s": req_f,
                "arrival_setup_scalar_s": arr_scalar,
                "slack_setup_scalar_s": slk_scalar,
                "required_setup_scalar_s": req_scalar,
                "is_arrival_inf": int(not (is_finite(arr_r) or is_finite(arr_f))),
                "is_slack_inf": int(not (is_finite(slk_r) or is_finite(slk_f))),
            }
        )

    node_fields = [
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
    ]

    edge_fields = [
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
    ]

    label_fields = [
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
    ]

    with (out_dir / "nodes.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=node_fields)
        w.writeheader()
        w.writerows(nodes)

    with (out_dir / "edges.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=edge_fields)
        w.writeheader()
        w.writerows(edges)

    with (out_dir / "labels_setup_max.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=label_fields)
        w.writeheader()
        w.writerows(labels)

    run_meta = load_run_meta(raw_dir)
    finish_metrics = parse_finish_report_metrics(raw_dir / "6_finish.rpt")

    slack_vals_s = [safe_float(r.get("slack_setup_scalar_s")) for r in labels]
    arrival_vals_s = [safe_float(r.get("arrival_setup_scalar_s")) for r in labels]
    req_vals_s = [safe_float(r.get("required_setup_scalar_s")) for r in labels]
    label_wns_ns = finite_min(slack_vals_s) * 1e9
    label_tns_ns = finite_sum(v for v in slack_vals_s if is_finite(v) and v < 0.0) * 1e9
    label_cpd_ns = finite_max(arrival_vals_s) * 1e9
    label_cps_ns = finite_min(req_vals_s[i] - arrival_vals_s[i] for i in range(min(len(req_vals_s), len(arrival_vals_s)))) * 1e9

    rcx_joined_edges = sum(
        1
        for edge in edges
        if edge["edge_type"] == "net" and is_finite(safe_float(edge["net_wire_cap_f_rcx"]))
    )

    global_features = {
        "run_id": args.run_id,
        "design": run_meta.get("design"),
        "platform": run_meta.get("platform"),
        "clock_period_ns": run_meta.get("clock_period_ns"),
        "abc_area": run_meta.get("abc_area"),
        "place_density": run_meta.get("place_density"),
        "routing_layer_adjustment": run_meta.get("routing_layer_adjustment"),
        "scenario_id": run_meta.get("scenario_id", "base"),
        "scenario_mode": run_meta.get("scenario_mode", "func"),
        "scenario_pvt": run_meta.get("scenario_pvt", "typical"),
        "scenario_rc": run_meta.get("scenario_rc", "typ"),
        "clock_uncertainty_ns": run_meta.get("clock_uncertainty_ns"),
        "timing_derate_late": run_meta.get("timing_derate_late"),
        "timing_derate_early": run_meta.get("timing_derate_early"),
        "input_delay_scale": run_meta.get("input_delay_scale"),
        "output_delay_scale": run_meta.get("output_delay_scale"),
        "source_run_id": run_meta.get("source_run_id", args.run_id),
        "num_nodes": len(nodes),
        "num_edges": len(edges),
        "num_net_edges": sum(1 for e in edges if e["edge_type"] == "net"),
        "num_cell_arc_edges": sum(1 for e in edges if e["edge_type"] == "cell_arc"),
        "num_rcx_joined_net_edges": rcx_joined_edges,
        "tns_ns": label_tns_ns if is_finite(label_tns_ns) else finish_metrics.get("tns_ns"),
        "wns_ns": label_wns_ns if is_finite(label_wns_ns) else finish_metrics.get("wns_ns"),
        "worst_slack_ns": label_wns_ns if is_finite(label_wns_ns) else finish_metrics.get("worst_slack_ns"),
        "critical_path_delay_ns": label_cpd_ns
        if is_finite(label_cpd_ns)
        else finish_metrics.get("critical_path_delay_ns"),
        "critical_path_slack_ns": label_cps_ns
        if is_finite(label_cps_ns)
        else finish_metrics.get("critical_path_slack_ns"),
    }

    dump_json(out_dir / "global_features.json", global_features)
    print(
        "extraction_complete",
        json.dumps(
            {
                "run_id": args.run_id,
                "nodes": len(nodes),
                "edges": len(edges),
                "labels": len(labels),
                "rcx_joined_net_edges": rcx_joined_edges,
            }
        ),
    )


if __name__ == "__main__":
    main()
