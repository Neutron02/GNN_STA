# GNN STA Phase-1 Timing Dataset Pipeline

This repository contains a reproducible phase-1 data pipeline for pin-level timing graph extraction from OpenROAD/OpenSTA results.

## Scope
- Run ORFS sweeps on `nangate45` for `gcd` and `aes`.
- Extract pin-level graph data and setup-max labels.
- Store curated raw artifacts and processed CSV/JSON datasets.
- Keep `OpenROAD-flow-scripts/` outside git tracking.

## Directory Layout
- `configs/sweep_phase1.json`: 32-run sweep definition.
- `configs/sdc_templates/`: source SDC templates.
- `configs/sdc_generated/`: generated per-clock SDC files.
- `scripts/run_sweep.py`: end-to-end orchestrator.
- `scripts/gen_sdc_variants.py`: generate SDC clock variants.
- `scripts/collect_curated_raw.py`: copy final ORFS artifacts into `data/raw_curated/<run_id>/`.
- `scripts/extract_graph_labels_or.py`: OpenROAD Python graph/label extraction.
- `scripts/extract_paths_or.py`: OpenROAD Python path extraction.
- `scripts/validate_dataset.py`: per-run integrity checks.
- `scripts/build_split_manifest.py`: `dataset_index.csv` and `splits.json`.
- `data/raw_curated/`: curated raw run artifacts.
- `data/processed/`: extracted datasets.
- `data/manifests/`: run status and split/index manifests.
- `logs/pipeline/`: per-run execution logs.

## Prerequisites
- Local ORFS checkout at `OpenROAD-flow-scripts/`.
- OpenROAD binary at `OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad`.
- Python 3 standard library (no extra packages required).

## Git Initialization
```bash
git init
```

## Generate SDC Variants
```bash
python3 scripts/gen_sdc_variants.py --config configs/sweep_phase1.json
```

## Smoke Tests
### 1) Existing `gcd/base` extraction
```bash
python3 scripts/collect_curated_raw.py \
  --run-id gcd_base_smoke \
  --design gcd \
  --platform nangate45 \
  --variant base \
  --clock-scale 1.0 \
  --clock-period-ns 0.46 \
  --abc-area 1 \
  --place-density 0.55

OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad -python -no_init -exit \
  scripts/extract_graph_labels_or.py \
  --odb data/raw_curated/gcd_base_smoke/6_final.odb \
  --sdc data/raw_curated/gcd_base_smoke/6_final.sdc \
  --spef data/raw_curated/gcd_base_smoke/6_final.spef \
  --liberty OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib \
  --run-id gcd_base_smoke \
  --raw-dir data/raw_curated/gcd_base_smoke \
  --out-dir data/processed/gcd_base_smoke

OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad -python -no_init -exit \
  scripts/extract_paths_or.py \
  --odb data/raw_curated/gcd_base_smoke/6_final.odb \
  --sdc data/raw_curated/gcd_base_smoke/6_final.sdc \
  --spef data/raw_curated/gcd_base_smoke/6_final.spef \
  --liberty OpenROAD-flow-scripts/flow/platforms/nangate45/lib/NangateOpenCellLibrary_typical.lib \
  --run-id gcd_base_smoke \
  --out-dir data/processed/gcd_base_smoke

python3 scripts/validate_dataset.py --run-id gcd_base_smoke
```

### 2) One new `gcd` run through orchestrator
```bash
python3 scripts/run_sweep.py \
  --config configs/sweep_phase1.json \
  --jobs 1 \
  --run-id gcd__clk414ps__abc0__pd500__rlabase
```

## Run Full Sweep
```bash
python3 scripts/run_sweep.py \
  --config configs/sweep_phase1.json \
  --jobs 2 \
  --resume
```

## Dry Run
```bash
python3 scripts/run_sweep.py --config configs/sweep_phase1.json --jobs 2 --dry-run
```

## Outputs
For each successful run `data/processed/<run_id>/` contains:
- `nodes.csv`
- `edges.csv`
- `labels_setup_max.csv`
- `global_features.json`
- `paths_setup_max.json`
- `paths_summary.csv`
- `validation.json`

`data/manifests/` contains:
- `runs.csv`
- `dataset_index.csv`
- `splits.json`

## ML Smoke Test (Dependency-Free)
Quick architecture sanity check with a tiny message-passing regressor (pure Python, no extra packages):

```bash
python3 scripts/gnn_smoke_test.py \
  --eval-mode within_design \
  --design gcd \
  --max-train-runs 4 \
  --max-val-runs 2 \
  --max-test-runs 2 \
  --epochs 8 \
  --loss-nodes-per-graph 512 \
  --message-steps 2 \
  --target-scale 1e12
```

Notes:
- `--target-scale 1e12` converts second-scale labels to picoseconds before z-score normalization.
- Feature normalization and target normalization are computed from training runs only.
- Output includes normalized MSE and physical RMSE/MAE in picoseconds.

Unseen-design holdout check (train on `gcd`, test on `aes`):

```bash
python3 scripts/gnn_smoke_test.py \
  --eval-mode holdout_design \
  --train-design gcd \
  --eval-design aes \
  --max-train-runs 4 \
  --max-val-runs 2 \
  --max-test-runs 2 \
  --epochs 8 \
  --loss-nodes-per-graph 512 \
  --message-steps 2 \
  --target-scale 1e12
```

## PyTorch Training/Evaluation (Production Path)
This is the real neural training stack (message-passing neural network + AdamW + checkpoints).
`TimingMPNN` is already a neural network; you can optionally enable a hybrid variant that adds learned categorical embeddings for cell types (`--cell-emb-dim`).

Prerequisite:
- Install PyTorch in your Python environment.
- A minimal dependency list is in `requirements-ml.txt`.

Example setup:
```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements-ml.txt
```

Train within-design (`gcd`):
```bash
python3 scripts/train_gnn.py \
  --eval-mode within_design \
  --design gcd \
  --target-col slack_setup_scalar_s \
  --target-scale 1e12 \
  --batch-graphs 2 \
  --hidden-dim 128 \
  --message-steps 3 \
  --cell-emb-dim 16 \
  --epochs 50 \
  --early-stop-patience 10
```

Train with unseen-design holdout (train on `gcd`, test on `aes`):
```bash
python3 scripts/train_gnn.py \
  --eval-mode holdout_design \
  --train-design gcd \
  --eval-design aes \
  --target-col slack_setup_scalar_s \
  --target-scale 1e12 \
  --batch-graphs 2 \
  --hidden-dim 128 \
  --message-steps 3 \
  --cell-emb-dim 16 \
  --epochs 50 \
  --early-stop-patience 10
```

Optional critical-node weighted loss (emphasize nodes at/under setup limit):
```bash
python3 scripts/train_gnn.py \
  --eval-mode holdout_design \
  --train-design gcd \
  --eval-design aes \
  --target-col slack_setup_scalar_s \
  --target-scale 1e12 \
  --cell-emb-dim 16 \
  --critical-loss-weight 3.0 \
  --critical-threshold-ps 0.0
```

Evaluate a saved checkpoint:
```bash
python3 scripts/eval_gnn.py \
  --checkpoint results/train_runs/<run_name>/best.pt \
  --dataset-index data/manifests/dataset_index.csv \
  --splits data/manifests/splits.json
```

Multi-task training (arrival + slack + required, shared GNN encoder):
```bash
python3 scripts/train_gnn_multitask.py \
  --eval-mode holdout_design \
  --train-design gcd \
  --eval-design aes \
  --target-cols arrival_setup_scalar_s,slack_setup_scalar_s,required_setup_scalar_s \
  --primary-target-col slack_setup_scalar_s \
  --target-weights 0.7,1.0,0.7 \
  --cell-emb-dim 16 \
  --consistency-weight 1e-4 \
  --rank-loss-weight 0.02 \
  --epochs 60
```

Evaluate a multi-task checkpoint:
```bash
python3 scripts/eval_gnn_multitask.py \
  --checkpoint results/train_runs/<run_name>/best.pt \
  --dataset-index data/manifests/dataset_index.csv \
  --splits data/manifests/splits.json
```

Automated multi-task hyperparameter sweep + leaderboard:
```bash
python3 scripts/sweep_multitask.py \
  --config configs/ml_sweep_mt_holdout_v1.json \
  --jobs 2 \
  --resume
```

Architecture-level variant (hetero dual-pass with lifted net nodes):
```bash
python3 scripts/train_hetero_dualpass.py \
  --eval-mode holdout_design \
  --train-design gcd \
  --eval-design aes \
  --target-cols arrival_setup_scalar_s,slack_setup_scalar_s,required_setup_scalar_s \
  --primary-target-col slack_setup_scalar_s \
  --target-weights 0.7,1.0,0.7 \
  --hidden-dim 192 \
  --message-steps 5 \
  --cell-emb-dim 24 \
  --consistency-weight 5e-4 \
  --rank-loss-weight 0.03 \
  --epochs 40
```

Evaluate hetero dual-pass checkpoint:
```bash
python3 scripts/eval_hetero_dualpass.py \
  --checkpoint results/train_runs/<run_name>/best.pt \
  --dataset-index data/manifests/dataset_index.csv \
  --splits data/manifests/splits.json
```

## Dataset Schemas
### `nodes.csv`
- `node_id`, `node_name`, `node_kind`
- `inst_name`, `cell_name`, `port_name`
- `io_type`, `is_sequential_cell`, `is_buffer_cell`, `is_inverter_cell`
- `is_clock_pin`, `is_endpoint`
- `x_um`, `y_um`, `inst_x_um`, `inst_y_um`, `cell_area_um2`
- `port_cap_max_f`, `port_cap_min_f`

### `edges.csv`
- `edge_id`, `src_node_id`, `dst_node_id`, `edge_type`
- `net_name`, `net_sig_type`, `net_fanout`
- `net_routed_length_um`, `net_cap_max_f`, `net_cap_min_f`
- `net_wire_res_ohm_rcx`, `net_wire_cap_f_rcx`
- `cell_arc_master`, `cell_arc_from_pin`, `cell_arc_to_pin`

### `labels_setup_max.csv`
- `node_id`
- `arrival_rise_s`, `arrival_fall_s`
- `slack_rise_setup_max_s`, `slack_fall_setup_max_s`
- `required_rise_setup_max_s`, `required_fall_setup_max_s`
- `arrival_setup_scalar_s`, `slack_setup_scalar_s`, `required_setup_scalar_s`
- `is_arrival_inf`, `is_slack_inf`
