# Round 2 main adjudication

Status: advisory completed, one iteration; development evidence only. Original advice retained unchanged. No NO-v3 campaign or default change adopted in this round.

Accepted: forceoff barely updates the approach prior; its smaller error is not demonstrated surface identification. The old force-direction update absorbs friction into the estimated normal. Shared coordinate and friction effects must be separated before attributing performance to DSFC/MSFC. The normalized velocity update has a plausible rectification failure under single-direction sliding with normal motion. Open-loop replay supports a mechanism hypothesis, not closed-loop robustness. All supplied artifact and source receipt hashes independently verified.

Qualified: the transverse growth derivation assumes locally fixed sliding direction and small errors. It does not prove that every gain or persistently excited 2D trajectory fails. An average of normalized quantities generally differs from the ratio of their averages. Coplanarity is valid under the stated ideal force geometry, not arbitrary transverse intervention. A residual gate of sin(15 degrees) neither enforces a true-normal prior cone nor proves safety: the velocity term can still update when the coplanarity term is gated out.

Not adopted: the suggested 18-run matrix and numerical rejection thresholds are proposals, not independently justified acceptance criteria. Freezing the normal cannot replace the required unknown-surface task. A tilted estimator prior should be separated from rotating the whole task frame so that observability and task changes are not confounded. Prior uncertainty, varying surface normals, cross-sliding intervention, friction and servo-model limitations need explicit probes before selecting an observer. Physical identification remains pending current hardware qualification.

Next: retain all three estimator versions and their negative results; measure the full control execution budget, then conduct a bounded shared-observer identifiability experiment with separate estimator initialization and fixed physical task. Do not expand controller candidates or infer a proposal winner.
