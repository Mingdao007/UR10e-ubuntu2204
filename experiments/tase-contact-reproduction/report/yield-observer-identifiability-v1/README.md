# Observer identifiability v1: P0 prior-only baseline

Status: six complete development cycles, no hardware execution, no formal holdout and no observer/controller promotion. Default estimator behavior remains unchanged. SFC remains the research baseline; DSFC is held fixed here solely to isolate the common observer.

Implementation: `NormalEstimator` now accepts an optional unit `initial_inward_normal_base` distinct from the physical approach. The prior is included in controller identity and survives fresh-instance replay. An explicitly zero motion gain is allowed for a frozen-observer ablation; negative gains remain rejected. Omitting the new initial-state option preserves the prior identity parameter set and default arithmetic. The physical robot/task/platform are not redefined by estimator uncertainty.

[All six results](prior-results.md) quantify two materials and three initial directions. The approach prior has 1.29-1.36 degree normal RMS despite having no online estimator update. A 10-degree initial error yields about 9.28-9.70 degree normal RMS and 9.01-9.59 degree attitude RMS, with greater path error. Some force peaks decrease instead of increasing. Thus neither low normal error under a favorable prior nor a reduced force peak alone establishes a superior task controller.

Full cycle means the complete scheduled entry/PATH duration was recorded, NOT that the path was accurately completed: measured progress ratios range from 0.610 to 0.900. All are nominal trials, and none proves disturbance rejection or following a substantially changing normal. The frozen observer is a diagnostic baseline, not a replacement for the required unknown-surface task.

Validation: 197 regression tests pass. Eight focused prior/NO-v2 tests pass. All six raw SHA-256 hashes were verified; within each material their initial simulator states, plant identities, physical approaches and every reference sample hash match. Every estimator state is exactly unchanged across the complete records. The compliant/across-prior case passes 31916-sample full-state replay with no mismatch. Replay uses the same implementation and is not independent physics validation.

Raw data: `runs/yield-observer-prior-v1/` retains six compressed full-state receipts plus frozen protocol, start/completion manifests and summary. SHA receipts and verification are retained in this report. No previous result was overwritten. Tests and simulation do not authorize motion.

Fable round 3 remains a nonblocking advisory task with writes confined to `discussion/`; route status is recorded separately. Its recommendation is not yet an accepted design. Next decisions must separate initial-prior correction, genuine varying-normal tracking and transverse-force contamination before selecting a shared observer. No new controller family or winner is declared.
