# Main adjudication of round 10

Round 10 is advisory, not formal experimental acceptance. Its process exited 0. The launch selected Homepool Fable; xhigh is a requested CLI setting, not an independently attested server effort. No second iteration is needed: the qualifications below can be resolved by main without altering the frozen diagnostic.

## Accepted use

- Phase-resolved load, path, attitude, yielding and recovery descriptors are necessary alongside whole-period registered values. The 17 examined pairs have their whole-period load maximum before the disturbance. A lower whole-period peak is therefore not evidence of a lower disturbance-induced peak in this subset.
- There are 14 exactly shared non-repeat DSFC/MSFC parameter triples, including six EI coincidences. These are coefficient-conditional development comparisons, not equal nominal accuracy or independent tests.
- The fixed five-arm diagnostic provides explicit geometry and active-memory ablations. Compare MSFC with MSFC_IDENTITY, and inspect MSFC_IDENTITY versus DSFC rather than assuming full-period equality from a short forced-input check.
- Metric engagement during nominal contact and release is a useful, falsifiable candidate explanation for unit 4. Preserve its adverse results and examine the numerical sensitivity before a design change.
- No 8-versus-24 fair-budget claim, no fabricated three-method freeze, no deterministic-repeat confidence interval, and no use of the reserved validation cells for development.

## Qualifications and rejected inferences

1. The proposed contrast threshold based on maximum within-arm spread is a descriptive sensitivity heuristic, not a certified numerical error bound, convergence result or statistical uncertainty. Errors in two arms can add; common discretization bias can persist across all sampled resolutions. Report each arm and contrast at every resolution, with guard changes, rather than treating the heuristic as proof.
2. The new interpretation rule was proposed after training and after some diagnostic results were visible to main. It cannot retrospectively be called preregistered for the current diagnostic. It changes no frozen metric, run selection or acceptance gate. The metric-engagement hypothesis and any post-hoc analysis remain explicitly exploratory.
3. A falsified engagement predictor does not establish that memory can never help. Conversely, reproducing an adverse metric effect does not justify promoting the current MSFC. Retain/stop decisions apply only to this frozen design, parameter region and mechanism hypothesis; a broader redesign requires its own rationale and version.
4. Numerical checks at two triples do not validate all training contrasts. SFC_RADIAL is a Python implementation; its existing short axis-restriction regression is real evidence, but does not alone cover the present full force histories. A supplemental main native/Python axis replay is recorded separately, and must not be described as closed-loop geometric equivalence.
5. The analysis key `all_pre_onset_rows_bitwise_equal` tests position, normal load and attitude-error arrays, not every row field or full state. Limit that claim accordingly. Phase descriptors use sampled masks and fixed dt, whereas the registered objective clips its final integration cell; do not equate all phase summaries with exact continuous-time integrals.
6. The report's statement that inputs were unchanged needs a chronology qualification: its receipt lists diagnostic files added after the start HEAD and read later. Current file hashes verify delivered bytes, not the exact time of reading. Training scientific bindings remain unchanged.
7. A load below 1 N is the defined low-load proxy, not automatically geometric separation. QP feasibility, speed bounds and low simulator forces are not hardware safety guarantees.

No scientific code, parameters, budgets, runtime state or reserved validation inputs were changed for this review. The fixed 60-slot diagnostic continues independently.

## Verification completed

Main re-extracted all 34 raw artifacts with SHA-256 verified before parsing. Every cached array and scientific metadata field matched (elapsed extraction time excluded). Supplied phase analysis reproduced exactly for 34 members and 17 pairs; the shared-triple analysis also reproduced exactly. These are reproducibility checks of the supplied implementations, not independent implementations. All 49 receipt-listed input/output files and 21 execution bindings matched current bytes. The supplemental 12 axis-restriction replays had exactly zero native/Python command difference, for both frozen parameter triples, each Cartesian axis and 1/2 ms steps. This supports the one-dimensional implementation binding only.
