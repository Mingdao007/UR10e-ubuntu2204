# Six-controller contact benchmark preparation

Implementation workspace: `codex/contact-six-qp-20260917`, based on the retained
R013/R014 governance fix at `9a9af59f`. The installed autotuner and the unrelated
main-checkout report remain unchanged.

## Current evidence boundary

- Native LAC, NAC, SFC, DSFC, ISFC, MSFC adapters preserve original source hashes.
  RPSFC is not selectable. Latest MSFC LAC-matching parameters are an offline
  seed, not a contact-qualified result.
- Shared OSQP code-generated equality/box solver, common fixed-attitude contact
  outer loop, raw-sensor guard before software injection, independent hard PATH
  ellipse and inherited inner PATH projection are implemented offline.
- Full law/outer/QP snapshots roll back on solver or complete-kernel deadline
  failure. Deadline rejection is not proof of a real-time writer.
- The task is 5 N, 80 x 20 mm figure eight, omega=0.1 rad/s and a complete
  62.831853-second period. Software-injected disturbances are never labeled
  physical impacts. Actual hand pushes form a separate condition.
- Per-controller budget is 24 paired units, with failed units retained; frozen
  holdout is separate. A ledger or proposer does not itself start a trial.

## Physical observations and Home correction

`readonly-kinematics.json` records payload 0.413 kg, CoG [1.1,3.1,16.3] mm,
TCP [0,0,87.4,0,0,0] mm/rad and stationary robot. The single-pose calibrated FK
error was 1.84 micrometres / 6.28 microradians. This is not a workspace audit.

The user's correction is authoritative: retain original Home XYZ
[0.487834547,0.129337053,0.033] m, use the current wrist orientation, and align
tool +Z to base -Z. `preserved-home.json` records the unchanged XYZ, newly
computed attitude and IK. It does not say the robot has reached that pose.
`home-path-geometry.json` checks the full nominal path at that derived Home;
`home-transfer-sanity.json` checks the horizontal-at-current-height then descent
route. Neither artifact is collision/contact qualification.

## Delivered files

Both triplets were uploaded and read back byte-equal under
`/programs/andyl/kunwei/step5`:

- `step5d_contact_six_qp_v1`: protocol 618001, qdot cap 0.05 rad/s, common
  force/torque guards 20 N / 2 Nm, 0.2 mm/s initial search, 15 mm maximum travel,
  full-period PATH clock. These are bounded preparation settings, not tuned
  results. The historical R013 host identity cannot dispatch this package.
- `step5d_contact_home_v1`: original XYZ/new attitude, 10 mm/s transfer,
  2 mm initial-position guard, proper relative-rotation comparison.

See `tp-package-validation.json` and `home-package-validation.json` for local
and fetched-back cache validation. No package has been played in the evidence
recorded here; update this statement only after a real run artifact exists.
No payload, CoG, TCP, hardware zero, or sensor configuration was written.

## Tests and diagnostic results

Native laws passed analytical/Python parity, failed-step atomicity, source
binding and complete MSFC memory replay. The targeted suite also covers QP
feasibility/deadlines, common force/posture signs, coverage-aware metrics,
budget/idempotence, package bounds and Home admission. Historical sign/frame
replay and textbook alignment passed. The sign replay used the retained June14
CSV explicitly; it is legacy regression evidence, not current contact data.

`kernel-smoke-current.json` records 31,416 prescribed-input steps per law. All
six remained numerically finite and QP-feasible. Observations were prescribed
at a fixed Jacobian: this is NOT plant simulation or performance comparison.
Research SFC seed hit a common speed limit in 23,028 ticks (~73%), so direct
reuse would be an unfair saturation-dominated experiment. Ordinary-scheduler
long tails exceeded 2 ms; this diagnostic does not qualify 500 Hz operation.
Subsequent complete-kernel deadline rejection is covered by its focused test;
the earlier smoke's source hashes identify the pre-deadline-enforcement code.

Baseline limitations reproduced before this change: old feedforward tests
contain an incomplete mock and stale installed-xacro binding; a historical
June13 kinematics CSV is absent from the isolated checkout. These do not count
as tests passing. New FK evidence is recorded separately, not used to silently
rewrite the historical contract.

## Remaining execution work

Home Remote/observer gate and actual Home verification; integration of the new
six-law kernel and protocol into the physical owner, including continuous
baseline-to-PATH state and the full-period clock; complete-writer real-time
qualification; contact-specific equal-budget tuning; parameter freeze and
independent randomized repeated nominal/disturbed trials. No offline or file
read-back result establishes those outcomes.
