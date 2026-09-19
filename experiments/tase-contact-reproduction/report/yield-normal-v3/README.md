# NO-v3: prior correction improves, intervention recovery remains a cost

NO-v3 is a versioned common-observer development candidate, not a fourth
controller proposal. SFC remains the baseline and DSFC/MSFC remain the proposed
control dynamics. This bounded observer study fixes DSFC to separate a common
module issue before claiming controller-level benefits. Legacy defaults are
unchanged; the candidate is not promoted.

Nine new 62.831853 s PATH trials completed at 2 ms with eight plant substeps,
plus four hash-verified retained frozen-observer references. Every new trial
completed without failure or contact loss. There is one deterministic trial per
cell, no uncertainty estimate or holdout, and no robot execution.

## Evidence and tradeoffs

- Independent initial-prior errors: the combined observer's whole-PATH normal
  RMS is 1.647 degrees for along-feed and 2.899 degrees for across-feed, compared
  with 9.380 and 9.700 degrees for their respective frozen references. Their
  final-ten-second RMS values are 0.544 and 0.543 degrees. PATH-start errors
  were 8.514 and 9.059 degrees, so the result is not explained by entry alone.
- Across-feed ablation: motion-only RMS is 7.645 degrees, tail 5.876 degrees,
  versus combined 2.899 and 0.543. Path RMS changes from 6.697 to 5.368 mm, but
  measured progress falls from 0.890 to 0.857. Peak contact force is essentially
  unchanged (6.266 versus 6.268 N). This is a limited estimator/trajectory benefit,
  not simultaneous improvement of all task objectives.
- Greater curvature: combined normal RMS is 5.950 degrees versus frozen 7.931;
  path RMS is still 7.026 mm and measured progress only 0.745 (frozen 7.242 mm,
  0.723). The observer has not solved the complete unknown-surface task.
- Normal hold/release: combined force MAE is 0.637 N versus frozen 0.578 N;
  matched nominal recovery is 3.834 s versus 0 s. The frozen zero means it is
  already inside the fixed 2 mm/0.5 N bands at the end of the five-second
  release ramp; it does not mean instantaneous recovery from a step removal.
- Tangent hold/release: combined force MAE is 0.128 N versus frozen 0.065 N;
  normal RMS rises to 5.678 degrees and recovery is 8.424 s versus 0.942 s.
  During intervention, the CP residual RMS is 0.169 versus 0.011 in nominal.
  This is consistent with residual contamination, not a unique causal proof.
  Yield displacement is 22.025 versus 23.035 mm, so this is also not a simple
  claim of increased yielding. Both end near their matched nominal paths.

The frozen favorable prior is an ablation, not a proposed solution for unknown
surfaces. It already fails the prior/curvature challenge. Conversely, correcting
initial normal error does not justify ignoring the observed recovery penalty.
NO-v3 is retained as a mechanism candidate with explicit negative results.
Further controller comparison must not attribute shared-estimator effects to
DSFC or MSFC. No gain has been retuned after viewing these data.

## Reproduction and limitations

`protocol.md` freezes equations and the nine cells; `protocol.json` binds exact
parameters and retained hashes. `results.md/json` contains every task metric,
matched recovery result, initial/PATH-start errors, gate diagnostics and raw
receipt paths. `comparison.png/pdf/svg` shows the task tradeoffs. Raw full-state
receipts remain under `runs/yield-normal-v3/` (local, excluded from Git).
`validation.md`, `regression.txt` and `writer-receipt.json` document code checks
and the verified Homepool writer route. The study/report/verification/plot
scripts are named `*_yield_normal_v3.py` under tools.

The follow-up numerical check freezes the same combined and motion-only
across-prior cases at 1 ms, eight plant substeps. It tests discretization
sensitivity of the observed correction benefit, not a new tuning candidate.

All evidence uses the existing ideal Coulomb, filtered-force, servo-lag
simulator. It does not establish sensor-bias/stick-slip robustness, contact-patch
orientation physics, actual transverse-force tolerance, compliant-material
transfer, hardware timing or physical safety. Wrist F/T does not independently
identify human force. No freshness, timing, identity or contact gate was relaxed.

The 1 ms follow-up completed both full periods without failure/contact loss.
The combined-versus-motion-only normal RMS advantage persists (3.130 versus
7.685 degrees), along with lower measured progress. Maximum common-time
position differences from the 2 ms traces are 0.442/0.490 mm; this is qualitative
persistence, not numerical equivalence. See refinement.md/json and the saved
manifest. Seven matched-invariant checks and the 31,916-record full-state replay
passed; see verification.json.
