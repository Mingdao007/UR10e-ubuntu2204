# Round 4 main adjudication

One advisory iteration completed, native session 2c0dbe08-2cea-45a0-9c6f-8f7e57187f8a,
model claude-fable-5-1, ubuntu-homepool, terminal end_turn and wrapper exit 0.
The xhigh effort is launch evidence, not independent server attestation.
Main independently checked all 15 raw hashes, 19 report inputs, 13 other inputs,
17 tool hashes and five advisory output hashes. Original advice is preserved.
No second iteration is needed; no hardware authority follows.

Accepted: do not add another observer based on these data. Shared-estimator
state and gating can substantially change measured recovery; compare control
dynamics under fixed common observers, and keep physical force identifiability
limits explicit. Reference-tangent forcing need not align with actual sliding.
The fixed Kp/friction equilibrium is a useful approximate mechanism. A frozen
observer arm is an instrument to remove observer adaptation, not a candidate
solution for unknown curved surfaces. The smallest remaining arm is SFC/MSFC
nominal and tangent hold under the frozen approach prior, with existing gains.

Main's independent OT-v1 already completed the adaptive interaction arm plus
normal hold: the legacy tangent recovery separation largely disappears under
NO-v3. All nine within-method observer invariants match. DC-v1 additionally
separates control-period from plant-step sensitivity. These new results were
not inputs to the original advisory and should not be attributed to Fable.

Qualified/rejected claims:

- The advisory calls the displacement/estimate relationship exact and assigns
  the whole delay to observer state. Main reloaded the two original raw traces:
  slope 0.72670 mm/degree matches the 0.72722 heuristic, but the fit has a
  1.05212 mm intercept. At recovery, actual displacement is 1.99977 mm versus
  a leak-only prediction of 1.33397 mm. Thus the mechanism is supported, but
  exclusive causality and an exact dynamic identity are not established.
  See report/yield-discretization-v1/recovery-leak-check.json. Frozen adaptation
  removes one mechanism; it does not remove plant/common-outer dynamics.
- The simple CP expression involving external force divided by normal load is
  an approximation with denominator and normal-velocity assumptions. A clean
  CP residual alone does not uniquely identify a 3D normal. Nor is measured
  n dot v generically force-immune: compliance, deformation and coupled motion
  can produce normal velocity. Arbitrary external force and arbitrary unknown
  surface variation cannot be separated without additional assumptions.
- Holding an estimate is not the same operation as resetting/reinitializing
  it. A proposed gate would still need an observable basis and a separate
  bounded experiment; no gate change is adopted here.
- Step sensitivity from another cell is a reason to check a near-threshold
  result, not an error bound proving the frozen DSFC recovery classification
  flips. The current DC-v1 check concerns SFC, not that DSFC frozen pair.
- The expression '0.5 N sin 15 deg approximately 1.3 N' contains a factor-ten
  typo: 5 N sin 15 deg is approximately 1.3 N.

Metric correction: main initially called OT-v1's <1 N duration physical contact
loss. DC-v1's four traces all retain positive force and negative geometric gap
at recorded samples. The reports now say low-load duration, with original
metrics/receipts unchanged. This correction does not negate the recovery and
sampling-sensitivity findings.

Next: finish the frozen-observer cross-controller arm before redesign or final
tuning, retaining nominal accuracy, yielding, force, progress and recovery.
Do not promote NO-v3 or claim step-independent continuous-law superiority.
