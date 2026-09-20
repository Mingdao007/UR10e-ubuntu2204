# FM-v1: no memory-metric benefit in the matched slow tangent-release pair

Two new MSFC g50 identity-metric trials reuse the frozen observer, full PATH
period, material, gains and common control configuration from FT-v1. The sole
parameter change is minimum_metric_eigenvalue = 1.0, the existing ablation.
Force-history and filter states still exist; this is not an all-state memory
reset. Every stored metric eigenvalue in the ablation is exactly one.

Both settings recover in 1.008 s. On minus identity-metric differences for the
tangent trial are +0.001271 N force MAE, +0.011533 N peak, +0.010668 mm path RMS,
+0.031229 mm yielding, and +0.000228 measured progress ratio. Saturation exposure
is 1.532 s with the metric active versus 1.276 s under identity. These small,
mixed effects do not support a memory benefit in this slow-release case.

The metric was active: its minimum eigenvalue reaches 0.783556 during tangent
hold, versus 0.964693 in nominal. Hence the negative result cannot be dismissed
as a disconnected memory implementation. It also does not prove memory can
never help other parameter ranges, stronger/shorter disturbances or physically
identified contact. The original short-pulse process remains a separate test.
No new controller, gain tuning, promotion or physical claim is introduced.

Strict matching is checked after reconstructing each native law and verifying
its actual law identity and snapshot binding against the receipt. The derived
identities necessarily change with the metric-floor parameter; only after that
verification are they omitted from the mechanical invariant comparison. Two
comparisons then pass with no unexpected differences. The initial report
incorrectly expected those derived identities to match; its failed check is
retained in ../yield-frozen-memory-v1-initial-check, not counted as a run failure
or discarded. Raw runs are unchanged.

Reproduce with tools/study_yield_frozen_memory_v1.py and
report_yield_frozen_memory_v1.py. results.json contains the exact metrics,
pairing, invariant checks and metric eigenvalue extrema. All data are
single-trial development evidence; no formal tuning or holdout was used.
