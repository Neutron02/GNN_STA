#!/usr/bin/env python3
"""Train a graph-conditioned scenario dominance ranker.

Goal:
- Predict dominant scenario(s) within each group before expensive STA loops.

Input manifest (from build_scenario_dominance_manifest.py):
- One row per run with `group_id`, per-run scenario features, and dominance rank.

Model:
- Lightweight edge-aware GNN encoder for per-run netlist graphs.
- Scenario feature encoder.
- Group-wise ranking head trained with top-1 CE + pairwise hinge ranking.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ml_training_common import load_dataset_index, load_graph_multi, require_torch

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception:  # pragma: no cover
    torch = None
    nn = None
    F = None


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _to_float(v: object) -> float:
    if v is None:
        return 0.0
    s = str(v).strip()
    if s == "" or s.lower() == "none":
        return 0.0
    try:
        out = float(s)
    except Exception:
        return 0.0
    if not math.isfinite(out):
        return 0.0
    return out


def _stable_seed(s: str) -> int:
    h = hashlib.sha256(str(s).encode("utf-8")).digest()
    return int.from_bytes(h[:8], byteorder="big", signed=False)


def _resolve_device(name: str):
    if name == "cpu":
        return torch.device("cpu")
    if name == "cuda":
        return torch.device("cuda")
    return torch.device("cuda" if torch.cuda.is_available() else "cpu")


def _set_seed(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def _write_json(path: Path, obj: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")


def _write_epoch_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    fields = [
        "epoch",
        "train_loss",
        "train_top1",
        "train_topk",
        "train_mrr",
        "train_contains_true_worst_rate",
        "train_violation_group_rate",
        "train_missed_violation_rate",
        "train_worst_regret_ps_mean",
        "train_worst_regret_ps_p95",
        "val_loss",
        "val_top1",
        "val_topk",
        "val_mrr",
        "val_contains_true_worst_rate",
        "val_violation_group_rate",
        "val_missed_violation_rate",
        "val_worst_regret_ps_mean",
        "val_worst_regret_ps_p95",
        "test_top1",
        "test_topk",
        "test_mrr",
        "test_contains_true_worst_rate",
        "test_violation_group_rate",
        "test_missed_violation_rate",
        "test_worst_regret_ps_mean",
        "test_worst_regret_ps_p95",
        "elapsed_s",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})


@dataclass
class ScenarioCandidate:
    run_id: str
    design: str
    group_id: str
    metric_col: str
    scenario_id: str
    scenario_mode: str
    scenario_pvt: str
    scenario_rc: str
    rank: int
    is_top1: int
    metric_value: float
    scenario_vec: List[float]


@dataclass
class ScenarioGroup:
    group_id: str
    design: str
    candidates: List[ScenarioCandidate]


@dataclass
class GraphData:
    x: "torch.Tensor"
    edge_index: "torch.Tensor"
    edge_attr: "torch.Tensor"


class GraphStore:
    def __init__(
        self,
        dataset_index: Dict[str, Dict[str, str]],
        target_col: str,
        max_nodes_per_graph: int,
        max_edges_per_graph: int,
        cache_size: int,
        seed: int,
    ) -> None:
        self.dataset_index = dataset_index
        self.target_col = target_col
        self.max_nodes_per_graph = max(0, int(max_nodes_per_graph))
        self.max_edges_per_graph = max(0, int(max_edges_per_graph))
        self.cache_size = int(cache_size)
        self.seed = int(seed)
        self.cache: "OrderedDict[str, GraphData]" = OrderedDict()

    def _subsample(self, run_id: str, x: "torch.Tensor", edge_index: "torch.Tensor", edge_attr: "torch.Tensor") -> GraphData:
        n = int(x.size(0))
        e = int(edge_index.size(1))
        seed = _stable_seed(f"{self.seed}:{run_id}")
        rng = random.Random(seed)

        if self.max_nodes_per_graph > 0 and n > self.max_nodes_per_graph:
            keep_idx = sorted(rng.sample(range(n), self.max_nodes_per_graph))
            keep_t = torch.tensor(keep_idx, dtype=torch.long)
            node_mask = torch.zeros((n,), dtype=torch.bool)
            node_mask[keep_t] = True

            src = edge_index[0]
            dst = edge_index[1]
            keep_e = node_mask[src] & node_mask[dst]
            edge_index = edge_index[:, keep_e]
            edge_attr = edge_attr[keep_e]

            old_to_new = torch.full((n,), -1, dtype=torch.long)
            old_to_new[keep_t] = torch.arange(int(keep_t.numel()), dtype=torch.long)
            edge_index = old_to_new[edge_index]
            x = x[keep_t]

        if self.max_edges_per_graph > 0 and int(edge_index.size(1)) > self.max_edges_per_graph:
            e_idx = sorted(rng.sample(range(int(edge_index.size(1))), self.max_edges_per_graph))
            e_t = torch.tensor(e_idx, dtype=torch.long)
            edge_index = edge_index[:, e_t]
            edge_attr = edge_attr[e_t]

        return GraphData(x=x, edge_index=edge_index, edge_attr=edge_attr)

    def get(self, run_id: str) -> GraphData:
        cached = self.cache.get(run_id)
        if cached is not None:
            self.cache.move_to_end(run_id)
            return cached

        row = self.dataset_index.get(run_id)
        if row is None:
            raise KeyError(f"run_id not found in dataset index: {run_id}")
        g = load_graph_multi(row, [self.target_col])
        out = self._subsample(run_id, g.x, g.edge_index, g.edge_attr)

        self.cache[run_id] = out
        self.cache.move_to_end(run_id)
        if self.cache_size > 0:
            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
        return out


if nn is not None:

    class GraphScenarioRanker(nn.Module):
        def __init__(
            self,
            node_dim: int,
            edge_dim: int,
            scenario_dim: int,
            hidden_dim: int = 96,
            scenario_hidden_dim: int = 48,
            message_steps: int = 2,
            dropout: float = 0.1,
        ) -> None:
            super().__init__()
            self.hidden_dim = int(hidden_dim)
            self.message_steps = int(message_steps)

            self.node_encoder = nn.Sequential(
                nn.Linear(node_dim, self.hidden_dim),
                nn.ReLU(),
                nn.Linear(self.hidden_dim, self.hidden_dim),
                nn.ReLU(),
            )
            self.edge_encoder = nn.Sequential(
                nn.Linear(edge_dim, self.hidden_dim),
                nn.ReLU(),
                nn.Linear(self.hidden_dim, self.hidden_dim),
                nn.ReLU(),
            )
            self.msg_mlp = nn.Sequential(
                nn.Linear(self.hidden_dim * 2, self.hidden_dim),
                nn.ReLU(),
                nn.Linear(self.hidden_dim, self.hidden_dim),
            )
            self.upd_mlp = nn.Sequential(
                nn.Linear(self.hidden_dim * 2, self.hidden_dim),
                nn.ReLU(),
                nn.Linear(self.hidden_dim, self.hidden_dim),
            )
            self.norm = nn.LayerNorm(self.hidden_dim)
            self.dropout = nn.Dropout(float(dropout))

            self.scenario_encoder = nn.Sequential(
                nn.Linear(scenario_dim, scenario_hidden_dim),
                nn.ReLU(),
                nn.Linear(scenario_hidden_dim, scenario_hidden_dim),
                nn.ReLU(),
            )
            graph_dim = self.hidden_dim * 2
            self.score_head = nn.Sequential(
                nn.Linear(graph_dim + scenario_hidden_dim + self.hidden_dim, self.hidden_dim),
                nn.ReLU(),
                nn.Linear(self.hidden_dim, 1),
            )

        def encode_graph(self, x: "torch.Tensor", edge_index: "torch.Tensor", edge_attr: "torch.Tensor") -> "torch.Tensor":
            h = self.node_encoder(x)
            e = self.edge_encoder(edge_attr) if int(edge_attr.size(0)) > 0 else edge_attr.new_zeros((0, self.hidden_dim))

            if int(edge_index.size(1)) > 0:
                src = edge_index[0]
                dst = edge_index[1]
                for _ in range(self.message_steps):
                    msg_in = torch.cat([h[src], e], dim=1)
                    msg = self.msg_mlp(msg_in)

                    agg = torch.zeros_like(h)
                    agg.index_add_(0, dst, msg)

                    deg = torch.zeros((h.size(0),), dtype=h.dtype, device=h.device)
                    deg.index_add_(0, dst, torch.ones((dst.size(0),), dtype=h.dtype, device=h.device))
                    agg = agg / deg.clamp(min=1.0).unsqueeze(1)

                    upd = self.upd_mlp(torch.cat([h, agg], dim=1))
                    h = self.norm(h + self.dropout(torch.relu(upd)))

            h_mean = h.mean(dim=0)
            h_max = h.max(dim=0).values
            return torch.cat([h_mean, h_max], dim=0)

        def score(self, graph_emb: "torch.Tensor", scenario_vec: "torch.Tensor") -> "torch.Tensor":
            s = self.scenario_encoder(scenario_vec.unsqueeze(0)).squeeze(0)
            g_core = graph_emb[: self.hidden_dim]
            interaction = torch.zeros((self.hidden_dim,), dtype=g_core.dtype, device=g_core.device)
            k = min(int(g_core.numel()), int(s.numel()), self.hidden_dim)
            if k > 0:
                interaction[:k] = g_core[:k] * s[:k]
            z = torch.cat([graph_emb, s, interaction], dim=0)
            return self.score_head(z.unsqueeze(0)).squeeze(0).squeeze(0)

else:

    class GraphScenarioRanker:  # pragma: no cover
        def __init__(self, *args, **kwargs) -> None:
            raise RuntimeError("PyTorch is unavailable in this environment.")


def _parse_manifest(path: Path) -> List[ScenarioGroup]:
    rows = list(csv.DictReader(path.open("r", newline="", encoding="utf-8")))
    by_group: Dict[str, List[ScenarioCandidate]] = defaultdict(list)
    for r in rows:
        run_id = str(r.get("run_id", "")).strip()
        group_id = str(r.get("group_id", "")).strip()
        design = str(r.get("design", "")).strip()
        if not run_id or not group_id:
            continue
        rla = _to_float(r.get("routing_layer_adjustment"))
        clock_uncertainty = _to_float(r.get("clock_uncertainty_ns"))
        derate_late = _to_float(r.get("timing_derate_late"))
        derate_early = _to_float(r.get("timing_derate_early"))
        in_scale = _to_float(r.get("input_delay_scale"))
        out_scale = _to_float(r.get("output_delay_scale"))
        scenario_vec = [
            _to_float(r.get("clock_period_ns")),
            _to_float(r.get("clock_scale")),
            _to_float(r.get("abc_area")),
            _to_float(r.get("place_density")),
            rla,
            1.0 if abs(rla) > 1e-12 else 0.0,
            clock_uncertainty,
            derate_late,
            derate_early,
            in_scale,
            out_scale,
            1.0 if abs(clock_uncertainty) > 1e-12 else 0.0,
        ]
        by_group[group_id].append(
            ScenarioCandidate(
                run_id=run_id,
                design=design,
                group_id=group_id,
                metric_col=str(r.get("metric_col", "wns_ns")).strip() or "wns_ns",
                scenario_id=str(r.get("scenario_id", "base")).strip() or "base",
                scenario_mode=str(r.get("scenario_mode", "func")).strip() or "func",
                scenario_pvt=str(r.get("scenario_pvt", "typical")).strip() or "typical",
                scenario_rc=str(r.get("scenario_rc", "typ")).strip() or "typ",
                rank=int(_to_float(r.get("dominance_rank")) or 0),
                is_top1=int(_to_float(r.get("is_dominant_top1")) or 0),
                metric_value=_to_float(r.get("metric_value", r.get("wns_ns"))),
                scenario_vec=scenario_vec,
            )
        )

    groups: List[ScenarioGroup] = []
    for gid, cands in sorted(by_group.items()):
        cands_sorted = sorted(cands, key=lambda c: (c.rank, c.run_id))
        design = cands_sorted[0].design if cands_sorted else ""
        groups.append(ScenarioGroup(group_id=gid, design=design, candidates=cands_sorted))
    return groups


def _split_groups(
    groups: List[ScenarioGroup],
    seed: int,
    train_frac: float,
    val_frac: float,
    holdout_design: str,
) -> Tuple[List[ScenarioGroup], List[ScenarioGroup], List[ScenarioGroup]]:
    holdout = holdout_design.strip()
    if holdout:
        test = [g for g in groups if g.design == holdout]
        rest = [g for g in groups if g.design != holdout]
        if not test:
            raise SystemExit(f"No groups found for holdout design '{holdout}'")
        rng = random.Random(seed)
        rng.shuffle(rest)
        n_train = max(1, int(round(len(rest) * train_frac)))
        n_train = min(n_train, max(1, len(rest) - 1))
        train = rest[:n_train]
        val = rest[n_train:]
        if not val:
            val = rest[-1:]
            train = rest[:-1]
        return train, val, test

    gids = list(range(len(groups)))
    rng = random.Random(seed)
    rng.shuffle(gids)
    n = len(gids)
    n_train = max(1, int(round(n * train_frac)))
    n_val = max(1, int(round(n * val_frac)))
    if n_train + n_val >= n:
        n_val = max(1, n - n_train - 1)
    if n_val <= 0:
        n_val = 1
    n_test = n - n_train - n_val
    if n_test <= 0:
        n_test = 1
        if n_train > 1:
            n_train -= 1
        else:
            n_val -= 1
    train = [groups[i] for i in gids[:n_train]]
    val = [groups[i] for i in gids[n_train : n_train + n_val]]
    test = [groups[i] for i in gids[n_train + n_val :]]
    return train, val, test


def _scenario_norm(groups: Sequence[ScenarioGroup]) -> Tuple[List[float], List[float]]:
    vals: List[List[float]] = []
    for g in groups:
        for c in g.candidates:
            vals.append(c.scenario_vec)
    if not vals:
        raise SystemExit("No training scenario vectors found")
    dim = len(vals[0])
    mean = [0.0] * dim
    std = [0.0] * dim
    for j in range(dim):
        col = [v[j] for v in vals]
        m = sum(col) / len(col)
        var = sum((x - m) * (x - m) for x in col) / len(col)
        s = math.sqrt(max(var, 1e-12))
        mean[j] = m
        std[j] = s
    return mean, std


def _norm_vec(v: Sequence[float], mean: Sequence[float], std: Sequence[float]) -> List[float]:
    return [(float(v[i]) - float(mean[i])) / float(std[i]) for i in range(len(v))]


def _group_losses(
    scores: "torch.Tensor",
    true_ranks: List[int],
    pairwise_margin: float,
) -> Tuple["torch.Tensor", "torch.Tensor"]:
    # Top-1 CE: best (rank=1) should receive highest score.
    try:
        top1_idx = true_ranks.index(1)
    except ValueError:
        top1_idx = int(min(range(len(true_ranks)), key=lambda i: true_ranks[i]))
    target = torch.tensor([top1_idx], dtype=torch.long, device=scores.device)
    ce = F.cross_entropy(scores.unsqueeze(0), target)

    terms = []
    m = float(pairwise_margin)
    for i in range(len(true_ranks)):
        for j in range(len(true_ranks)):
            if true_ranks[i] < true_ranks[j]:
                terms.append(torch.relu(m - (scores[i] - scores[j])))
    if terms:
        pair = torch.stack(terms).mean()
    else:
        pair = scores.new_tensor(0.0)
    return ce, pair


def _evaluate_groups(
    model: GraphScenarioRanker,
    groups: Sequence[ScenarioGroup],
    graph_store: Optional[GraphStore],
    scenario_mean: Sequence[float],
    scenario_std: Sequence[float],
    device: "torch.device",
    top_k: int,
    pairwise_margin: float,
    pairwise_weight: float,
    disable_graph: bool,
) -> Dict[str, float]:
    model.eval()
    loss_sum = 0.0
    top1_hits = 0
    topk_hits = 0
    mrr_sum = 0.0
    contains_true_worst_sum = 0
    groups_with_violation = 0
    missed_violation = 0
    regrets_ps: List[float] = []

    with torch.no_grad():
        for g in groups:
            scores: List["torch.Tensor"] = []
            ranks: List[int] = []
            metric_vals: List[float] = []
            for c in g.candidates:
                scen = torch.tensor(_norm_vec(c.scenario_vec, scenario_mean, scenario_std), dtype=torch.float32, device=device)
                if disable_graph:
                    emb = torch.zeros((model.hidden_dim * 2,), dtype=scen.dtype, device=device)
                else:
                    if graph_store is None:
                        raise RuntimeError("graph_store is required when graph encoder is enabled")
                    gd = graph_store.get(c.run_id)
                    x = gd.x.to(device)
                    ei = gd.edge_index.to(device)
                    ea = gd.edge_attr.to(device)
                    emb = model.encode_graph(x, ei, ea)
                s = model.score(emb, scen)
                scores.append(s)
                ranks.append(int(c.rank))
                metric_vals.append(float(c.metric_value))

            scores_t = torch.stack(scores, dim=0)
            ce, pair = _group_losses(scores_t, ranks, pairwise_margin=pairwise_margin)
            loss = ce + float(pairwise_weight) * pair
            loss_sum += float(loss.item())

            pred_order = sorted(range(len(ranks)), key=lambda i: float(scores_t[i].item()), reverse=True)
            true_order = sorted(range(len(ranks)), key=lambda i: ranks[i])

            true_top1 = true_order[0]
            if pred_order[0] == true_top1:
                top1_hits += 1

            k = min(int(top_k), len(ranks))
            pred_topk = set(pred_order[:k])
            true_topk = set(true_order[:k])
            if pred_topk & true_topk:
                topk_hits += 1

            rank_pos = pred_order.index(true_top1) + 1
            mrr_sum += 1.0 / rank_pos

            selected_idx = pred_order[:k]
            if true_top1 in selected_idx:
                contains_true_worst_sum += 1

            vio = {i for i, w in enumerate(metric_vals) if math.isfinite(w) and w < 0.0}
            if vio:
                groups_with_violation += 1
                if not (set(selected_idx) & vio):
                    missed_violation += 1

            valid_true = [w for w in metric_vals if math.isfinite(w)]
            valid_sel = [metric_vals[i] for i in selected_idx if math.isfinite(metric_vals[i])]
            if valid_true and valid_sel:
                regret_ns = max(0.0, min(valid_sel) - min(valid_true))
                regrets_ps.append(regret_ns * 1e3)

    n = max(1, len(groups))
    regrets_sorted = sorted(regrets_ps)
    if regrets_sorted:
        p95_idx = min(len(regrets_sorted) - 1, int(round(0.95 * (len(regrets_sorted) - 1))))
        regret_p95 = regrets_sorted[p95_idx]
        regret_max = regrets_sorted[-1]
        regret_mean = sum(regrets_sorted) / len(regrets_sorted)
    else:
        regret_mean = 0.0
        regret_p95 = 0.0
        regret_max = 0.0
    return {
        "loss": loss_sum / n,
        "top1": top1_hits / n,
        "topk": topk_hits / n,
        "mrr": mrr_sum / n,
        "contains_true_worst_rate": contains_true_worst_sum / n,
        "violation_group_rate": (groups_with_violation / n) if n > 0 else 0.0,
        "missed_violation_rate": (missed_violation / groups_with_violation) if groups_with_violation > 0 else 0.0,
        "worst_regret_ps_mean": regret_mean,
        "worst_regret_ps_p95": regret_p95,
        "worst_regret_ps_max": regret_max,
    }


def _write_group_predictions_csv(
    path: Path,
    model: GraphScenarioRanker,
    groups: Sequence[ScenarioGroup],
    graph_store: Optional[GraphStore],
    scenario_mean: Sequence[float],
    scenario_std: Sequence[float],
    device: "torch.device",
    top_k: int,
    disable_graph: bool,
) -> None:
    fields = [
        "group_id",
        "design",
        "metric_col",
        "group_size",
        "pred_top1_run_id",
        "pred_top1_scenario_id",
        "true_top1_run_id",
        "true_top1_scenario_id",
        "hit_top1",
        "pred_topk_run_ids",
        "true_topk_run_ids",
        "pred_topk_scenario_ids",
        "true_topk_scenario_ids",
        "contains_true_worst_in_topk",
        "violation_exists",
        "missed_violation_in_topk",
        "true_worst_metric_ns",
        "selected_worst_metric_ns",
        "regret_ps",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, object]] = []
    model.eval()
    with torch.no_grad():
        for g in groups:
            scores: List[float] = []
            metric_vals: List[float] = []
            for c in g.candidates:
                scen = torch.tensor(_norm_vec(c.scenario_vec, scenario_mean, scenario_std), dtype=torch.float32, device=device)
                if disable_graph:
                    emb = torch.zeros((model.hidden_dim * 2,), dtype=scen.dtype, device=device)
                else:
                    if graph_store is None:
                        raise RuntimeError("graph_store is required when graph encoder is enabled")
                    gd = graph_store.get(c.run_id)
                    x = gd.x.to(device)
                    ei = gd.edge_index.to(device)
                    ea = gd.edge_attr.to(device)
                    emb = model.encode_graph(x, ei, ea)
                score = float(model.score(emb, scen).item())
                scores.append(score)
                metric_vals.append(float(c.metric_value))

            pred_order = sorted(range(len(g.candidates)), key=lambda i: scores[i], reverse=True)
            true_order = sorted(range(len(g.candidates)), key=lambda i: (g.candidates[i].rank, g.candidates[i].run_id))
            k = min(int(top_k), len(g.candidates))
            pred_topk = pred_order[:k]
            true_topk = true_order[:k]

            vio = {i for i, w in enumerate(metric_vals) if math.isfinite(w) and w < 0.0}
            pred_set = set(pred_topk)
            true_worst_idx = true_order[0]

            valid_true = [w for w in metric_vals if math.isfinite(w)]
            valid_sel = [metric_vals[i] for i in pred_topk if math.isfinite(metric_vals[i])]
            true_worst_wns = min(valid_true) if valid_true else float("nan")
            selected_worst_wns = min(valid_sel) if valid_sel else float("nan")
            if math.isfinite(true_worst_wns) and math.isfinite(selected_worst_wns):
                regret_ps = max(0.0, (selected_worst_wns - true_worst_wns) * 1e3)
            else:
                regret_ps = float("nan")

            rows.append(
                {
                    "group_id": g.group_id,
                    "design": g.design,
                    "metric_col": g.candidates[0].metric_col if g.candidates else "unknown",
                    "group_size": len(g.candidates),
                    "pred_top1_run_id": g.candidates[pred_order[0]].run_id,
                    "pred_top1_scenario_id": g.candidates[pred_order[0]].scenario_id,
                    "true_top1_run_id": g.candidates[true_worst_idx].run_id,
                    "true_top1_scenario_id": g.candidates[true_worst_idx].scenario_id,
                    "hit_top1": int(pred_order[0] == true_worst_idx),
                    "pred_topk_run_ids": ";".join(g.candidates[i].run_id for i in pred_topk),
                    "true_topk_run_ids": ";".join(g.candidates[i].run_id for i in true_topk),
                    "pred_topk_scenario_ids": ";".join(g.candidates[i].scenario_id for i in pred_topk),
                    "true_topk_scenario_ids": ";".join(g.candidates[i].scenario_id for i in true_topk),
                    "contains_true_worst_in_topk": int(true_worst_idx in pred_set),
                    "violation_exists": int(bool(vio)),
                    "missed_violation_in_topk": int(bool(vio) and not bool(pred_set & vio)),
                    "true_worst_metric_ns": true_worst_wns,
                    "selected_worst_metric_ns": selected_worst_wns,
                    "regret_ps": regret_ps,
                }
            )

    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

def _empty_metrics() -> Dict[str, object]:
    return {
        "loss": "",
        "top1": "",
        "topk": "",
        "mrr": "",
        "contains_true_worst_rate": "",
        "violation_group_rate": "",
        "missed_violation_rate": "",
        "worst_regret_ps_mean": "",
        "worst_regret_ps_p95": "",
        "worst_regret_ps_max": "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Train graph-conditioned scenario dominance ranker")
    ap.add_argument("--manifest", default="data/manifests/scenario_dominance.csv")
    ap.add_argument("--dataset-index", default="data/manifests/dataset_index.csv")
    ap.add_argument("--target-col", default="slack_setup_scalar_s")
    ap.add_argument("--top-k", type=int, default=2)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--holdout-design", default="")
    ap.add_argument("--max-groups", type=int, default=0)

    ap.add_argument("--hidden-dim", type=int, default=96)
    ap.add_argument("--scenario-hidden-dim", type=int, default=48)
    ap.add_argument("--message-steps", type=int, default=2)
    ap.add_argument("--dropout", type=float, default=0.1)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--weight-decay", type=float, default=1e-5)
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--eval-every", type=int, default=1)
    ap.add_argument("--early-stop-patience", type=int, default=10)
    ap.add_argument("--pairwise-weight", type=float, default=0.2)
    ap.add_argument("--pairwise-margin", type=float, default=0.1)
    ap.add_argument("--grad-clip-norm", type=float, default=1.0)

    ap.add_argument("--max-nodes-per-graph", type=int, default=4096)
    ap.add_argument("--max-edges-per-graph", type=int, default=50000)
    ap.add_argument(
        "--graph-cache-size",
        type=int,
        default=256,
        help="Graph cache size (0 = unbounded). Increase to reduce CSV reload churn.",
    )
    ap.add_argument("--disable-graph", action="store_true", help="Scenario-only ablation (no graph encoder)")

    ap.add_argument("--device", choices=["auto", "cpu", "cuda"], default="auto")
    ap.add_argument("--out-dir", default="results/scenario_ranker_runs")
    ap.add_argument("--run-name", default="")
    args = ap.parse_args()

    require_torch()
    if torch is None or nn is None or F is None:
        raise RuntimeError("PyTorch is unavailable in this environment.")
    if args.eval_every < 1:
        raise SystemExit("--eval-every must be >= 1")
    if args.top_k < 1:
        raise SystemExit("--top-k must be >= 1")

    _set_seed(args.seed)
    device = _resolve_device(args.device)

    groups = _parse_manifest(Path(args.manifest))
    if args.max_groups > 0:
        groups = groups[: args.max_groups]
    if len(groups) < 3:
        raise SystemExit("Need at least 3 groups to train/val/test split")

    train_groups, val_groups, test_groups = _split_groups(
        groups=groups,
        seed=args.seed,
        train_frac=float(args.train_frac),
        val_frac=float(args.val_frac),
        holdout_design=str(args.holdout_design),
    )
    metric_col = train_groups[0].candidates[0].metric_col if train_groups and train_groups[0].candidates else "wns_ns"
    print(
        f"groups train={len(train_groups)} val={len(val_groups)} test={len(test_groups)} "
        f"top_k={args.top_k} metric_col={metric_col}"
    )

    scenario_mean, scenario_std = _scenario_norm(train_groups)

    graph_store: Optional[GraphStore] = None
    if args.disable_graph:
        node_dim = 1
        edge_dim = 1
        print("graph_encoder=disabled (scenario-only)")
    else:
        dataset_index = load_dataset_index(Path(args.dataset_index))
        graph_store = GraphStore(
            dataset_index=dataset_index,
            target_col=args.target_col,
            max_nodes_per_graph=args.max_nodes_per_graph,
            max_edges_per_graph=args.max_edges_per_graph,
            cache_size=args.graph_cache_size,
            seed=args.seed,
        )
        print(f"graph_cache_size={args.graph_cache_size}")
        first_run = train_groups[0].candidates[0].run_id
        first_graph = graph_store.get(first_run)
        node_dim = int(first_graph.x.size(1))
        edge_dim = int(first_graph.edge_attr.size(1))
    scenario_dim = len(train_groups[0].candidates[0].scenario_vec)

    model = GraphScenarioRanker(
        node_dim=node_dim,
        edge_dim=edge_dim,
        scenario_dim=scenario_dim,
        hidden_dim=args.hidden_dim,
        scenario_hidden_dim=args.scenario_hidden_dim,
        message_steps=args.message_steps,
        dropout=args.dropout,
    ).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)

    run_name = args.run_name.strip() or f"scenario_ranker_{_utc_stamp()}"
    run_dir = Path(args.out_dir) / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    config_payload = {
        "run_name": run_name,
        "args": vars(args),
        "node_dim": node_dim,
        "edge_dim": edge_dim,
        "scenario_dim": scenario_dim,
        "scenario_norm": {"mean": scenario_mean, "std": scenario_std},
        "metric_col": metric_col,
        "splits": {
            "train_groups": [g.group_id for g in train_groups],
            "val_groups": [g.group_id for g in val_groups],
            "test_groups": [g.group_id for g in test_groups],
        },
    }
    _write_json(run_dir / "config.json", config_payload)

    best_val = -1.0
    best_val_mrr = -1.0
    best_val_loss = float("inf")
    best_epoch = 0
    bad_epochs = 0
    rows: List[Dict[str, object]] = []
    train_rng = random.Random(args.seed + 101)
    t0 = time.time()

    for epoch in range(1, args.epochs + 1):
        model.train()
        epoch_loss = 0.0
        epoch_groups = 0

        order = list(range(len(train_groups)))
        train_rng.shuffle(order)
        for gi in order:
            g = train_groups[gi]
            scores: List["torch.Tensor"] = []
            ranks: List[int] = []
            for c in g.candidates:
                scen = torch.tensor(
                    _norm_vec(c.scenario_vec, scenario_mean, scenario_std),
                    dtype=torch.float32,
                    device=device,
                )
                if args.disable_graph:
                    emb = torch.zeros((model.hidden_dim * 2,), dtype=scen.dtype, device=device)
                else:
                    if graph_store is None:
                        raise RuntimeError("graph_store is required when graph encoder is enabled")
                    gd = graph_store.get(c.run_id)
                    x = gd.x.to(device)
                    ei = gd.edge_index.to(device)
                    ea = gd.edge_attr.to(device)
                    emb = model.encode_graph(x, ei, ea)
                score = model.score(emb, scen)
                scores.append(score)
                ranks.append(int(c.rank))

            scores_t = torch.stack(scores, dim=0)
            ce, pair = _group_losses(scores_t, ranks, pairwise_margin=float(args.pairwise_margin))
            loss = ce + float(args.pairwise_weight) * pair

            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            if float(args.grad_clip_norm) > 0.0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), float(args.grad_clip_norm))
            optimizer.step()

            epoch_loss += float(loss.item())
            epoch_groups += 1

        train_metrics = _evaluate_groups(
            model=model,
            groups=train_groups,
            graph_store=graph_store,
            scenario_mean=scenario_mean,
            scenario_std=scenario_std,
            device=device,
            top_k=args.top_k,
            pairwise_margin=args.pairwise_margin,
            pairwise_weight=args.pairwise_weight,
            disable_graph=bool(args.disable_graph),
        )

        do_eval = (epoch == 1) or (epoch == args.epochs) or ((epoch % args.eval_every) == 0)
        if do_eval:
            val_metrics = _evaluate_groups(
                model=model,
                groups=val_groups,
                graph_store=graph_store,
                scenario_mean=scenario_mean,
                scenario_std=scenario_std,
                device=device,
                top_k=args.top_k,
                pairwise_margin=args.pairwise_margin,
                pairwise_weight=args.pairwise_weight,
                disable_graph=bool(args.disable_graph),
            )
            test_metrics = _evaluate_groups(
                model=model,
                groups=test_groups,
                graph_store=graph_store,
                scenario_mean=scenario_mean,
                scenario_std=scenario_std,
                device=device,
                top_k=args.top_k,
                pairwise_margin=args.pairwise_margin,
                pairwise_weight=args.pairwise_weight,
                disable_graph=bool(args.disable_graph),
            )
            print(
                f"epoch={epoch:03d} "
                f"train_loss={train_metrics['loss']:.5f} "
                f"train_top1={train_metrics['top1']:.3f} "
                f"val_top1={val_metrics['top1']:.3f} "
                f"val_mrr={val_metrics['mrr']:.3f} "
                f"val_miss_vio={val_metrics['missed_violation_rate']:.3f} "
                f"val_regret_p95_ps={val_metrics['worst_regret_ps_p95']:.2f} "
                f"test_top1={test_metrics['top1']:.3f} "
                f"test_miss_vio={test_metrics['missed_violation_rate']:.3f}"
            )
        else:
            val_metrics = _empty_metrics()
            test_metrics = _empty_metrics()
            print(
                f"epoch={epoch:03d} train_loss={(epoch_loss / max(1, epoch_groups)):.5f} eval=skipped"
            )

        row = {
            "epoch": epoch,
            "train_loss": train_metrics["loss"],
            "train_top1": train_metrics["top1"],
            "train_topk": train_metrics["topk"],
            "train_mrr": train_metrics["mrr"],
            "train_contains_true_worst_rate": train_metrics["contains_true_worst_rate"],
            "train_violation_group_rate": train_metrics["violation_group_rate"],
            "train_missed_violation_rate": train_metrics["missed_violation_rate"],
            "train_worst_regret_ps_mean": train_metrics["worst_regret_ps_mean"],
            "train_worst_regret_ps_p95": train_metrics["worst_regret_ps_p95"],
            "val_loss": val_metrics["loss"],
            "val_top1": val_metrics["top1"],
            "val_topk": val_metrics["topk"],
            "val_mrr": val_metrics["mrr"],
            "val_contains_true_worst_rate": val_metrics["contains_true_worst_rate"],
            "val_violation_group_rate": val_metrics["violation_group_rate"],
            "val_missed_violation_rate": val_metrics["missed_violation_rate"],
            "val_worst_regret_ps_mean": val_metrics["worst_regret_ps_mean"],
            "val_worst_regret_ps_p95": val_metrics["worst_regret_ps_p95"],
            "test_top1": test_metrics["top1"],
            "test_topk": test_metrics["topk"],
            "test_mrr": test_metrics["mrr"],
            "test_contains_true_worst_rate": test_metrics["contains_true_worst_rate"],
            "test_violation_group_rate": test_metrics["violation_group_rate"],
            "test_missed_violation_rate": test_metrics["missed_violation_rate"],
            "test_worst_regret_ps_mean": test_metrics["worst_regret_ps_mean"],
            "test_worst_regret_ps_p95": test_metrics["worst_regret_ps_p95"],
            "elapsed_s": time.time() - t0,
        }
        rows.append(row)

        ckpt = {
            "epoch": epoch,
            "model_state": model.state_dict(),
            "model_cfg": {
                "node_dim": node_dim,
                "edge_dim": edge_dim,
                "scenario_dim": scenario_dim,
                "hidden_dim": args.hidden_dim,
                "scenario_hidden_dim": args.scenario_hidden_dim,
                "message_steps": args.message_steps,
                "dropout": args.dropout,
            },
            "scenario_norm": {"mean": scenario_mean, "std": scenario_std},
            "args": vars(args),
        }
        torch.save(ckpt, run_dir / "last.pt")

        if do_eval:
            cur = float(val_metrics["top1"])
            cur_mrr = float(val_metrics["mrr"])
            cur_loss = float(val_metrics["loss"])
            is_better = (
                (cur > best_val)
                or (abs(cur - best_val) <= 1e-12 and cur_mrr > best_val_mrr + 1e-12)
                or (
                    abs(cur - best_val) <= 1e-12
                    and abs(cur_mrr - best_val_mrr) <= 1e-12
                    and cur_loss < best_val_loss - 1e-12
                )
            )
            if is_better:
                best_val = cur
                best_val_mrr = cur_mrr
                best_val_loss = cur_loss
                best_epoch = epoch
                bad_epochs = 0
                torch.save(ckpt, run_dir / "best.pt")
            else:
                bad_epochs += 1

            if bad_epochs >= args.early_stop_patience:
                print(f"early_stop epoch={epoch} patience={args.early_stop_patience}")
                break

    _write_epoch_csv(run_dir / "epoch_metrics.csv", rows)

    best_ckpt = torch.load(run_dir / "best.pt", map_location=device)
    model.load_state_dict(best_ckpt["model_state"])
    best_test = _evaluate_groups(
        model=model,
        groups=test_groups,
        graph_store=graph_store,
        scenario_mean=scenario_mean,
        scenario_std=scenario_std,
        device=device,
        top_k=args.top_k,
        pairwise_margin=args.pairwise_margin,
        pairwise_weight=args.pairwise_weight,
        disable_graph=bool(args.disable_graph),
    )
    group_pred_csv = run_dir / "test_group_predictions.csv"
    _write_group_predictions_csv(
        path=group_pred_csv,
        model=model,
        groups=test_groups,
        graph_store=graph_store,
        scenario_mean=scenario_mean,
        scenario_std=scenario_std,
        device=device,
        top_k=args.top_k,
        disable_graph=bool(args.disable_graph),
    )

    summary = {
        "run_name": run_name,
        "best_epoch": best_epoch,
        "best_val_top1": best_val,
        "best_val_mrr": best_val_mrr,
        "best_val_loss": best_val_loss,
        "best_test_metrics": best_test,
        "artifacts": {
            "run_dir": str(run_dir.resolve()),
            "best_ckpt": str((run_dir / "best.pt").resolve()),
            "last_ckpt": str((run_dir / "last.pt").resolve()),
            "epoch_csv": str((run_dir / "epoch_metrics.csv").resolve()),
            "config_json": str((run_dir / "config.json").resolve()),
            "test_group_predictions_csv": str(group_pred_csv.resolve()),
        },
    }
    _write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
