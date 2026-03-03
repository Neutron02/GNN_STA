# Nangate45 Chip Sweep Reference

_Generated from `data/manifests/nangate45_chip_summary.csv` and OpenROAD SDC files on 2026-03-01 23:55._

## What This Is

- Ordered ranking of Nangate45 designs by size (estimated standard-cell count).
- Includes practical sweep planning fields: purpose, nominal clock, feasible clock target, run-time estimate, RAM estimate, and data footprint estimate.
- `recommended_clock_ns` uses `~1.05 x period_min_ns_report` when timing reports exist; otherwise it falls back to the nominal SDC period.

## Chip Ranking (Largest -> Smallest)

| Rank | Design        | Purpose                                  | Flow Design   | Status  | Cell Count (est) |     Nodes | Nominal Clock (ns) | Run Clock (ns) | Period Min (ns) | Recommended Clock (ns) | Sim Time (est) | Peak RAM (est, GiB) | Data Size (est, MB) |
|-----:|---------------|------------------------------------------|---------------|---------|-----------------:|----------:|-------------------:|---------------:|----------------:|-----------------------:|---------------:|--------------------:|--------------------:|
|    1 | bp_quad       | BlackParrot quad-core style integration  | bp_quad       | planned |        1,178,456 | 4,597,735 |              3.000 |          3.000 |               - |                  3.000 |            23m |                4.59 |              8369.0 |
|    2 | black_parrot  | BlackParrot top-level SoC-style integrati| bp            | success |          239,098 |   810,499 |              6.000 |          6.000 |           8.600 |                  9.030 |             1m |                3.37 |              1505.7 |
|    3 | ariane136     | Ariane (CVA6) RISC-V core variant        | ariane136     | success |          167,365 |   652,973 |              6.000 |          6.000 |           5.450 |                  5.723 |            <1m |                3.04 |              1216.3 |
|    4 | ariane133     | Ariane (CVA6) RISC-V core variant        | ariane133     | success |          163,544 |   636,250 |              3.000 |          3.000 |           3.330 |                  3.497 |         2h 14m |                8.98 |              1162.2 |
|    5 | mempool_group | Manycore/cluster integration block       | mempool_group | success |          132,760 |   526,403 |              3.000 |          3.000 |           3.770 |                  3.959 |            <1m |                2.28 |              1116.6 |
|    6 | swerv         | Western Digital SweRV RISC-V core        | swerv         | success |           89,055 |   344,970 |              2.000 |          2.000 |           2.150 |                  2.257 |          1h 2m |                3.93 |               564.6 |
|    7 | bp_multi_top  | BlackParrot multi-tile/top integration   | bp_multi      | success |           80,965 |   332,603 |              4.800 |          4.800 |           2.990 |                  3.140 |            <1m |                1.48 |               605.9 |
|    8 | swerv_wrapper | SweRV wrapper/integration top            | swerv_wrapper | planned |           77,286 |   301,531 |              2.000 |          2.000 |               - |                  2.000 |            12m |                1.37 |               548.9 |
|    9 | jpeg          | JPEG codec datapath block                | jpeg          | success |           60,459 |   203,162 |              1.000 |          1.000 |           1.120 |                  1.176 |            36m |                2.16 |               283.5 |
|   10 | bp_be_top     | BlackParrot backend pipeline subsystem   | bp_be         | success |           50,007 |   195,773 |              2.600 |          2.600 |           2.870 |                  3.014 |            <1m |                1.05 |               356.4 |
|   11 | tinyRocket    | Rocket-chip tinyRocket core/tile         | tinyRocket    | planned |           37,237 |   145,280 |              1.200 |          1.200 |           1.300 |                  1.365 |            29m |                1.78 |               264.4 |
|   12 | bp_fe_top     | BlackParrot frontend pipeline subsystem  | bp_fe         | success |           30,413 |   123,738 |              1.800 |          1.800 |           1.870 |                  1.964 |            <1m |                0.77 |               220.4 |
|   13 | ibex          | Small RISC-V core                        | ibex          | success |           16,070 |    60,984 |              2.200 |          2.200 |           2.200 |                  2.310 |            17m |                1.38 |               100.8 |
|   14 | aes           | AES crypto accelerator block             | aes           | success |           13,104 |    57,566 |              0.820 |          0.820 |           0.820 |                  0.861 |            16m |                1.31 |                91.4 |
|   15 | dynamic_node  | Synthetic control/datapath benchmark     | dynamic_node  | success |            9,132 |    40,001 |              6.000 |          6.000 |           6.110 |                  6.416 |            10m |                1.19 |                62.7 |
|   16 | gcd           | Tiny arithmetic/control sanity benchmark | gcd           | success |              501 |     1,826 |              0.460 |          0.460 |           0.510 |                  0.536 |            <1m |                0.82 |                 3.7 |

## Nominal Clock Source (OpenROAD SDC)

| Design | SDC File | Clock Periods Found (ns) |
|---|---|---|
| aes | `OpenROAD-flow-scripts/flow/designs/nangate45/aes/constraint.sdc` | 0.820 |
| ariane133 | `OpenROAD-flow-scripts/flow/designs/nangate45/ariane133/ariane.sdc` | 3.000 |
| ariane136 | `OpenROAD-flow-scripts/flow/designs/nangate45/ariane136/constraint.sdc` | 6.000 |
| black_parrot | `OpenROAD-flow-scripts/flow/designs/nangate45/black_parrot/constraint.sdc` | 6.000 |
| bp_be_top | `OpenROAD-flow-scripts/flow/designs/nangate45/bp_be_top/constraint.sdc` | 2.600 |
| bp_fe_top | `OpenROAD-flow-scripts/flow/designs/nangate45/bp_fe_top/constraint.sdc` | 1.800 |
| bp_multi_top | `OpenROAD-flow-scripts/flow/designs/nangate45/bp_multi_top/constraint.sdc` | 4.800 |
| bp_quad | `OpenROAD-flow-scripts/flow/designs/nangate45/bp_quad/bsg_chip.sdc` | 3.000 |
| dynamic_node | `OpenROAD-flow-scripts/flow/designs/nangate45/dynamic_node/constraint.sdc` | 6.000 |
| gcd | `OpenROAD-flow-scripts/flow/designs/nangate45/gcd/constraint.sdc` | 0.460 |
| ibex | `OpenROAD-flow-scripts/flow/designs/nangate45/ibex/constraint.sdc` | 2.200 |
| jpeg | `OpenROAD-flow-scripts/flow/designs/nangate45/jpeg/constraint.sdc` | 1.000 |
| mempool_group | `OpenROAD-flow-scripts/flow/designs/nangate45/mempool_group/mempool_group.sdc` | 3.000 |
| swerv | `OpenROAD-flow-scripts/flow/designs/nangate45/swerv/constraint.sdc` | 2.000 |
| swerv_wrapper | `OpenROAD-flow-scripts/flow/designs/nangate45/swerv_wrapper/constraint.sdc` | 2.000 |
| tinyRocket | `OpenROAD-flow-scripts/flow/designs/nangate45/tinyRocket/constraint.sdc` | 1.200 |

## Clock Guidance

- Use `nominal_clock_ns_sdc` to mirror each design's default OpenROAD constraint.
- Use `recommended_clock_ns` for a feasibility-first sweep (fewer hard-fail timing corners).
- For designs without `period_min_ns_report` yet (`planned` runs), start with nominal, then update after first successful route/STA pass.

## Notes

- Estimates are derived from current logs/manifests and can shift with machine load, threading, and config changes.
- `planned` rows are not fully completed in the latest manifest; values shown are provisional planning estimates.
- Some low run-time entries came from resume/reuse paths (flow artifacts already present), so treat them as lower-bound wall times rather than clean full-flow baselines.
