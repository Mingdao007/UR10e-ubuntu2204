# Main adjudication of round 8

The numerical extraction and both analysis JSONs reproduce exactly, as recorded in this directory. This accepts their reproducibility, not every causal interpretation in the advisory prose. The advisory used the fixed 25-attempt snapshot; subsequent initial-stage outcomes were not available to it. The actual initial stage ended with 2/8 feasible SFC pairs and 3/8 each for DSFC and MSFC.

## Decisions

1. Keep the running campaign unchanged. SFC stops before EI; DSFC and MSFC finish their registered budgets. Preserve all failed feasibility checks, objectives, parameters, and raw state. Do not produce a three-method full-budget freeze from this campaign.
2. Reject the suggested reduced-budget SFC incumbent as a route to the required formal fair comparison. Labeling 8 versus 24 units makes the inequality transparent but does not satisfy the user's equal-budget requirement. The untuned-seed alternative has the same limitation. No validation precondition will be relaxed to admit either alternative. The existing validation reservation remains unobserved.
3. An initial feasibility stop is a legitimate terminal outcome of the registered campaign, not necessarily a software defect that must be patched away. A future fair study, if justified by the complete development results, needs a separately frozen protocol and the same budget and selection rules for all methods. It must retain this campaign's negative outcome and disclose that its design used these development data. Do not launch a replacement campaign or make a new controller merely to obtain a positive result.
4. Retain fixed-triple comparisons as development evidence. They distinguish coefficient effects from cross-method seed differences. DSFC versus MSFC at matched coefficients is informative about the active metric, subject to the prior native identity-equivalence checks and solver differences; it does not replace the reserved explicit identity arm or resolution checks.

## Accepted findings and limits

- The failed bands are heterogeneous: path/attitude/progress do not explain every nominal failure. Load MAE and early load peaks also matter. The bands were empirical development bands, not externally specified task tolerances or physical safety limits.
- Lower J can coexist with an infeasible nominal and a deeper load minimum. Preserve absolute descriptors and phase decomposition alongside J. Do not promote a retained finite objective into selection eligibility.
- In the fixed snapshot, whole-episode load maxima occurred before disturbance onset. Report the maximum after onset as a descriptive supplement; keep the registered whole-episode guard unchanged. This finding is scoped to those samples, not every later training or validation sample.
- Memory engagement and its decay are measurable state observations. The near-identity metric at release in these samples does not establish that memory is universally inactive during sustained intervention or that its earlier effect cannot alter later plant trajectories.

## Corrections to the advisory prose

- The statement that every SFC disturbed progress guard passes only because of the push is too broad. In the advisory's own arithmetic replacement diagnostic, SFC-00 remains at 0.8528133862, above 0.85; SFC-01/02/03 fall below. This replacement is a post-processing diagnostic, not a simulated counterfactual without the push, so it cannot establish that the push alone caused the pass. Print the windowed descriptors without changing the guard.
- Load below 1 N for 0.61 s is a low-load episode under the declared metric. It is not proof of geometric separation or physical contact loss; the reported minimum of 0.562 N is positive.
- The co-timing of law motion, estimator updates and attitude error supports a coupling hypothesis. It does not causally isolate per-axis geometry, nonlinear dynamics, the observer, or QP. The reserved radial-geometry ablation remains necessary; do not label the difference a proven geometry mechanism.
- An unresolved difference under available resolution checks is not an equivalence or inertness result. Report insufficiently resolved benefit/adversity, with its numerical evidence. Do not infer a null mechanism from a small J difference alone.
- The low-damping sample retained a longer engagement window (about 7.8 s). Therefore the claim that this metric acts only in the first one or two seconds is not supported even across the four shared triples.
- A later decision to remove active memory would be a versioned selection informed by the ablation, not a new independently validated design. Viewed cells cannot remain final independent evidence for a design selected from them. No such removal is authorized by this adjudication itself; the full development evidence and explicit identity comparison remain outstanding.
- A mechanism hypothesis can legitimately be developed using observed development data, including negative results and J, provided the choice and data use are disclosed and independent evaluation is preserved. The advisory's blanket description of any choice informed by viewed J as overfitting is too restrictive. Conversely, a prewritten mechanism story alone does not eliminate overfitting.

No controller equations, tuning bounds, objective, threshold, training schedule, validation runner, or hardware state is changed by this adjudication. Main proceeds with the existing training and accepts no proposal-superiority or physical-safety claim.
