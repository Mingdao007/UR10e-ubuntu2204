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
and fetched-back cache validation. Home executed successfully on 2026-09-18:
`home-live-result.json` records 0.030 mm position error, 0.0026 degree attitude
error, stopped program and Safety NORMAL. The contact resident has not played.
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

## Offline closure and remaining live gates

The contact provider is now carried through the single mature-writer injection
seam, with an explicit optional factory hook on the R013 owner. The provider
owns the contact outer loop and native QP; the mature writer remains the only
command owner. State21 sends zero qdot through the provider's freeze-carry
pause seam, and the first State25 tick starts formal PATH at time zero.

`writer-timing.json` is an offline complete-composition receipt. It exercises
all six laws, baseline, the 1 s smooth entry, the stationary seam, and PATH,
while recording p50/p95/p99/max tick duration, observed deadline overruns,
fresh/held/stale counts, held fraction, stale stops and geometric rejects. It
also separates the first warm-up tick, steady-state ticks, native kernel and
QP spans, and records State21/State25 tick counts. Its deadline field is
diagnostic only; it is not a live 500 Hz qualification.

`runs/contact-six-offline-20260918/campaign-summary.json` records the paired
24-unit budget for every controller: 48 sealed nominal/disturbed trials per
controller, one shared capture-session identity, a measured feasible candidate
freeze, and 25 randomized holdout replays per controller. The replay uses a
fixed-Jacobian native law/QP path with an explicitly named synthetic contact
plant proxy. It is offline comparison evidence and does not establish physical
performance. `freshness-sensitivity.json` reports the 20/40/60/80 ms cutoff
replays; stale rows are censored while held rows remain eligible. In this tape,
20/40 ms do not yield a complete ranking because the 50 ms held segment is
censored; 60/80 ms both yield the same complete force-RMSE order
`ISFC > SFC > LAC > NAC > DSFC > MSFC`. That is a policy-sensitivity result
for this offline tape, not a physical controller ranking.

Remaining work is live readiness only: fresh Home/tool/package read-back,
Remote Control and bridge predicates, observer barrier, and a complete writer
timing receipt from the real transport. No offline receipt authorizes Load,
Play, ARM, motion, contact, or a physical acceptance claim.

## September 18 implementation update

The native adapters now integrate explicit measured intervals in (0,4ms].
Baseline and PATH use one law/filter/QP state and separate sample/path clocks.
The inner projection obeys the common XY speed norm cap, and raw force guards
reject at (not only above) the package limit.

`kernel-preload-path-smoke.json` covers 500 prescribed preload ticks plus a
complete path including its exact endpoint, 31,917 steps per law. All six
remained QP-feasible. This is prescribed-input replay, not a plant or live test.
Ordinary scheduling still produced 8.4ms and 10.9ms outliers; real-time writer
qualification is outstanding. SFC seed saturation persists (22,888 ticks).

The new measured-observation runtime uses calibrated Jacobians, timestamped
sensor/RTDE age checks, current tool binding and complete computation deadline
rollback. The proposer and ledger consume only sealed paired training units;
failed units consume budget without scores. Unmeasured candidate feasibility
is unknown, never true. None of these interfaces admits live dispatch.

The offline timing and campaign receipts are separate from the historical R004
20 ms qualification gate. They use contact-six's 20 ms fresh / 80 ms stale
policy and keep geometric-latency rejection as a separate censor reason.

The contact freshness contract now follows the SFC-compatible three bands:
`fresh` for age `<20 ms`, latest-value/ZOH `held` for `20 ms <= age <80 ms`,
and fail-closed `stale` at `>=80 ms`. The 20 ms boundary is a diagnostic, not
a per-sample hard stop. The contact outer loop keeps the separate geometric
latency budget, and a stale stop or geometric latency rejection censors the
metric objective. See `CONTACT_BENCHMARK_FRESHNESS.md` and the generated
`static-delivery.json` for the policy and host-gap evidence. The current
static capture has no host gaps at or above 80 ms; its roughly 50 ms gaps are
held-delivery evidence only, not sensor-internal age or controller acceptance.

Historical R004 suite: 8 failures / 9 passes, reproduced identically in the
clean base worktree at 9a9af59f. These concern old TP strings, baseline budget,
ledger/transport fixtures and promotion assumptions, not a passing regression
suite. New behavior is checked separately with focused tests.
