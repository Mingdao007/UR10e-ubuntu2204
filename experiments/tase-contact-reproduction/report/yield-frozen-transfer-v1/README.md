# FT-v1: large recovery differences do not persist under a frozen observer

Four new full-period trials (SFC/MSFC, nominal/tangent hold) complete the
frozen-observer arm with two retained DSFC receipts. All six finish without
execution failure. Six mechanical/settings/initial-state/reference comparisons
against NO-v3 pass; every recorded normal state is exactly frozen. A fresh
instance replays all 31,916 records of MSFC tangent hold without mismatch.
The 12 legacy/NO-v3 nominal/tangent references are retained and hash-verified.

Frozen-observer tangent recovery is SFC 1.004 s, DSFC 0.942 s and MSFC g50
1.008 s. Nominal path RMS is respectively 5.052, 5.044 and 5.033 mm, with
progress about 0.872 for all three. Yield peaks are 23.387, 23.035 and 21.930 mm.
The earlier legacy recovery spread (10.926 / 2.660 / 0.460 s) does not persist.
NO-v3 instead gives 8.848 / 8.424 / 8.630 s. Thus an unconditional proposal
claim of much faster recovery is unsupported at these fixed candidate gains.
This does not prove that every difference was exclusively observer-caused;
observer, plant and task dynamics remain coupled.

Force regulation still differs: frozen tangent force MAE is 0.1695 N for SFC,
0.0648 for DSFC, and 0.0485 for MSFC g50. This is a development signal for a
load-regulation/yielding tradeoff, not a fair-optimization winner: the methods
have different fixed mechanical parameters and nominal force errors. MSFC's
relative force error cannot be attributed to memory without a matched ablation.
The companion FM-v1 performs exactly that check.

The frozen prior is a diagnostic instrument, not a replacement for unknown
surface following. Earlier independent prior/strong-curvature experiments
already show its limitation. The full task, three controller roles, shared
constraints and eventual equal-budget tuning/holdout requirements remain.
There is one deterministic trial per cell, no uncertainty estimate or physical
validation. Short-pulse behavior is not inferred from this slow-release arm.

Reproduction: tools/study_yield_frozen_transfer_v1.py,
report_yield_frozen_transfer_v1.py and plot_yield_frozen_transfer_v1.py.
results.json contains all task metrics, paired recovery and six invariant
checks. Raw full-state paths/hashes and the fixed protocol are retained.
comparison.png/pdf/svg were rendered and inspected. msfc-replay.json records
the complete-state check. Runtime/controller code was unchanged; earlier
runtime regression evidence remains applicable. No robot command was sent.
