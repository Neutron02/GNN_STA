# GNN_STA

Graph-based timing learning and scenario-dominance ranking pipeline built on OpenROAD/OpenSTA artifacts.

## Current Objective
The primary objective is to reduce ECO loopbacks by selecting the dominant timing scenarios/corners early.

What this means in practice:
- Build implementation snapshots once (or a small number of times).
- Replay scenario assumptions cheaply on those snapshots.
- Train a model to rank which scenarios are most likely to dominate (worst timing) per implementation.
- Use top-k predicted scenarios to focus signoff effort and reduce late-stage rework.

Node/path slack regression is still supported, but it is now a supporting capability, not the final product goal.

## Repository Scope
This repo includes:
- ORFS/OpenROAD sweep orchestration and extraction scripts.
- Scenario replay scripts (no re-place/re-route).
- Scenario-dominance manifest builders and ranker training.
- Timing regression model training/eval scripts (multitask, hetero, tripath).

External dependency (not tracked in git):
- `OpenROAD-flow-scripts/` checkout with an OpenROAD binary.

## Environment Setup
### ML environment
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements-ml.txt
```

### OpenROAD/ORFS expectation
The scripts expect:
- `OpenROAD-flow-scripts/flow`
- `OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad`

## End-to-End Data Build
### 1) Run base implementation sweep
Example:
```bash
python3 scripts/run_sweep.py --config configs/sweep_phase1.json --resume --jobs 2
```

Larger non-tinyRocket pilots:
```bash
python3 scripts/run_sweep.py --config configs/sweep_nonrocket_smoke3.json --resume --jobs 2
python3 scripts/run_sweep.py --config configs/sweep_nonrocket_pilot16.json --resume --jobs 3
python3 scripts/run_sweep.py --config configs/sweep_nonrocket_pilot24.json --resume --jobs 3
```

Notes:
- `--resume` reconciles already-completed runs.
- `run_sweep.py` now supports `flow_design` mapping in config (needed for designs like `bp_fe_top`/`bp_be_top`).

### 2) Build split/index manifests
```bash
python3 scripts/build_split_manifest.py
```

Outputs:
- `data/manifests/runs.csv`
- `data/manifests/dataset_index.csv`
- `data/manifests/splits.json`

### 3) Replay scenarios on existing implementations (no re-place/re-route)
```bash
python3 scripts/replay_sta_scenarios.py \
  --dataset-index data/manifests/dataset_index.csv \
  --runs-csv data/manifests/runs.csv \
  --scenarios-json configs/scenario_replay_pilot.json \
  --jobs 6 --resume
```

Optional:
- `--paths-only` to only rerun path extraction/metrics patch.
- `--run-id <id>` to filter base runs.

### 4) Build scenario-dominance labels
```bash
python3 scripts/build_scenario_dominance_manifest.py \
  --dataset-index data/manifests/dataset_index.csv \
  --runs-csv data/manifests/runs.csv \
  --output-csv data/manifests/scenario_dominance.csv \
  --output-summary data/manifests/scenario_dominance_summary.json \
  --top-k 2
```

## Scenario Ranker Training
### Single run
```bash
python3 scripts/train_scenario_ranker.py \
  --manifest data/manifests/scenario_dominance.csv \
  --dataset-index data/manifests/dataset_index.csv \
  --top-k 2 \
  --epochs 60 \
  --device cuda \
  --run-name scenario_ranker_v1
```

### Holdout battery
```bash
python3 scripts/run_scenario_ranker_battery.py \
  --manifest data/manifests/scenario_dominance.csv \
  --dataset-index data/manifests/dataset_index.csv \
  --mode both \
  --top-k 2 \
  --epochs 40
```

## Timing Regression Models (Supporting)
### Multitask GNN
```bash
python3 scripts/train_gnn_multitask.py \
  --eval-mode within_design \
  --design all \
  --splits data/manifests/splits.json \
  --data-workers 12 \
  --batch-graphs 1 \
  --loss-nodes-per-graph-train 1024 \
  --loss-nodes-per-graph-eval 1024 \
  --device cuda
```

### Tripath dual-pass
```bash
python3 scripts/train_tripath_dualpass.py \
  --eval-mode within_design \
  --design all \
  --splits data/manifests/splits.json \
  --data-workers 12 \
  --batch-graphs 1 \
  --loss-nodes-per-graph-train 512 \
  --loss-nodes-per-graph-eval 512 \
  --device cuda
```

Evaluate checkpoint:
```bash
python3 scripts/eval_tripath_dualpass.py --checkpoint results/train_runs/<run_name>/best.pt
```

## Configs You Should Use
- `configs/sweep_nonrocket_smoke3.json`: quick stability gate.
- `configs/sweep_nonrocket_pilot16.json`: medium pilot (jpeg/swerv/ariane133/ariane136).
- `configs/sweep_nonrocket_pilot24.json`: broader pilot including dynamic/ibex/bp_fe_top/bp_be_top with `flow_design` mapping.
- `configs/scenario_replay_pilot.json`: setup/hold/RC stress replay scenarios.

Use `configs/sweep_tinyrocket_pilot16.json` only for targeted debugging; tinyRocket extraction has been unstable in this environment.

## Artifact Contract
Per successful run:

Raw curated (`data/raw_curated/<run_id>/`):
- `6_final.odb`, `6_final.def`, `6_final.v`, `6_final.sdc`, `6_final.spef`
- `6_net_rc.csv`, `6_finish.rpt`, `run_meta.json`

Processed (`data/processed/<run_id>/`):
- `nodes.csv`, `edges.csv`, `labels_setup_max.csv`, `global_features.json`
- `paths_setup_max.json`, `paths_summary.csv`
- `paths_hold_min.json`, `paths_hold_summary.csv` (when available)
- `validation.json`

## Known Issues / Notes
- Some designs can fail due to ORFS design nickname/path mismatch. `flow_design` support in config is now implemented to handle this.
- `validate_dataset.py` now supports `--allow-wns-mismatch` and `run_sweep.py` uses it, so WNS-only mismatches are warnings instead of hard failures.
- Large sweeps generate huge local data churn; commit source/docs/configs separately from generated artifacts.
