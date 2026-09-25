# R31 TASE-QP motion-gate comparison

## Result

The R31 rate460 A controls completed eligible 60 s Figure-eight paths with
formal `[5,60)` MAE **1.282720 N** and **1.006797 N**. Two TASE-QP paths also
completed 60 s and all 550 formal bins, but both failed the existing motion
gate because published joint velocity did not correlate sufficiently with
RTDE `actual_qd`. Their force MAEs are retained as diagnostics, not eligible
controller scores or BO observations.

| Attempt | Outer profile | Formal MAE (N) | Timing | `qd` correlation | Eligibility |
|---|---|---:|---|---:|---|
| R31 A1, TASE-RNN-MATURE | `Md=9.565272137974492`, `Bd=693.6559295653944`, integral `1.0 N·s` | 1.282720 | Pass | 0.970072 | Eligible |
| R31 A2, TASE-RNN-MATURE | Same A profile | 1.006797 | Pass | 0.972070 | Eligible |
| QP BO-10/B | `Md=8.592659656558919`, `Bd=772.1473715259434`, integral `0.5 N·s` | 0.914710 | Pass, 481.1 Hz accepted | 0.840132 | Failed motion gate |
| QP matched R31 A | Same outer profile as A; OSQP QP inner solver | 0.950139 | Pass, 480.05 Hz accepted | 0.848607 | Failed motion gate |

The QP live profile itself stayed fixed: OSQP codegen C, exact equality plus
box constraints, `0.05 rad/s` joint limit, `1 ms` deadline, and the existing
validation tolerances. The first QP attempt used the explicitly frozen BO-10/B
seed. The matched attempt then held the R31 A outer loop and integral fixed,
changing only the solver. Neither attempt had a failed solve: the maximum QP
times were **0.0926 ms** and **0.1001 ms**, with zero calls over the 1 ms
deadline.

## Motion-gate diagnosis

The frozen motion metric is the Pearson correlation between published joint
velocity and RTDE `actual_qd`, joined on the TP-consumed packet sequence. Its
minimum is **0.900**. The matched QP receipt reported **0.848607**; recomputing
the same consumed-sequence join gave **0.848627**. A one-packet shift raised it
only to **0.852176**, so an off-by-one packet association does not explain the
failure.

Matched-QP per-joint correlations were **0.963** (J1), **0.705** (J2),
**0.943** (J3), **0.880** (J4), **0.086** (J5), and **0.990** (J6). J5 also
has very little commanded variation (`0.000220 rad/s` standard deviation)
compared with its measured actual-velocity variation (`0.000916 rad/s`). The
RNN controls also have a weak J5 correlation (**0.211–0.223**), but their J2
and J4 correlations are higher and the flattened six-joint metric passes.
Measured command-to-RTDE packet ages have median **6.69 ms** and p95 **8.69 ms**;
this is descriptive and does not replace the frozen consumed-sequence gate.

The matched QP attempt otherwise passed the 60 s path, 550-bin coverage,
contact, timing, safety, return, and Home checks. It reached **0.950139 N**
bin-mean normal-force MAE in `[5,60)`, but the failed motion gate makes that a
diagnostic measurement only. The sensor signal is KWR75 controller feedback,
not an independent task-force truth source. The full attempt remains failed in
the denominator; its recovery returned to the bound joint Home and the
supervisor verified Dashboard STOP.

## Scheduler short probe

The 106.1 ms historical QP outlier was not reproduced. On one sealed
35-iteration R31 A input, 30,000 offline QP solves with GC disabled measured a
median of **0.03195 ms**, p99 **0.03584 ms**, and maximum **0.04601 ms**; none
exceeded 1 ms. The slowest sample used approximately the same thread CPU time
as wall time, with negligible run-queue wait. A prior diagnostic run with GC
enabled allocated one per-sample detail dictionary and produced isolated
1.4–1.5 ms spikes. Those spikes belong to the probe harness and are excluded
from QP timing conclusions. This does not identify the historical 106.1 ms
event; the two new live attempts instead show solver maxima below 0.11 ms.

## Current interpretation and next branch

The R31 matched attempt confirms that a complete, well-timed TASE-QP path does
not yet meet the existing actual-velocity motion gate. The low MAE cannot be
reported as an eligible improvement. Keep the gate and QP numerical profile
unchanged. Continue offline per-joint realization/lag analysis and the planned
SFC+TASE composition replay; any future live QP attempt must address the
measured J2/J4/J5 command-to-actual mismatch rather than repeat either profile
unchanged.

Evidence:

- BO-10/B attempt: `runs/tase-r31-qp-rate460-20260925T1600HKT/live/attempts/0001/attempt-result.json`
- Matched R31 A attempt: `runs/tase-r31-qp-matched-a-rate460-20260925T1615HKT/attempts/0001/attempt-result.json`
- BO-10/B motion analysis: `reports/tase_r31_qp_motion_gate_analysis_20260925.json`
- Matched R31 A motion analysis: `reports/tase_r31_qp_matched_a_motion_gate_analysis_20260925.json`
- Writer-policy scheduler probe: `runs/tase-r31-qp-scheduler-probe-20260925T1545HKT/result-gc-disabled/summary.json`

## Reused SFC+TASE offline evidence

The existing frozen SFC/DSFC composition campaign already contains 30/30
successful model-proxy attempts (five matched blocks, plane/tangential-pulse/
normal-pulse scenarios) and a sealed same-input replay of 15 model traces
through both laws. The replay records one TASE joint-velocity realization per
tick and maximum tangent-to-normal leakage below `8.5e-19 m/s`. This verifies
the offline composition seam; the receipt explicitly labels it
`physical_evidence=false`.

That dataset does not match the current live normal method identity. Its
`TASE_RNN_MATURE` offline adapter uses `epsilon=0.014`, `r=0.2`, and the profile
id `r013_60_rate400_A_incumbent_integral_1p0`; R31 live uses
`epsilon=0.022`, `r=1`, and the rate460 protocol. Its `120 N/m` path stiffness
is the offline adapter default, not a live-approved normal gain. The prior
model scores therefore remain historical model predictions, not current R31
SFC+TASE results.

Reuse the frozen native tangent law identities and composition architecture,
then close the R31 live-normal identity in offline replay before considering a
new live method. Any use of the historical `120 N/m` default in a live
composition requires a separate reviewed parameter binding. The current live
registry still marks `TASE_RNN_MATURE+SFC` unavailable; no SFC+TASE controller
was run on the robot.

## R31-parameter-aligned SFC/DSFC model proxy

A new five-block, 30-attempt closed-loop model proxy used the R31 A numeric profile (`epsilon=0.022`, `r=1`, minus-sign update; `Md=9.565272137974492`, `Bd=693.6559295653944`, integral cap `1.0 N·s`, 5 N target, and the rate460 Figure-eight identity). Both frozen native tangent laws used the same source-bound SFC/DSFC parameters and the same seeded plane, tangent-pulse, and normal-pulse scenarios. All 30 attempts completed; failures: **0**.

| Scenario | SFC model MAE (N) | DSFC model MAE (N) | Paired Δ DSFC−SFC (N) | SFC path RMS (mm) | DSFC path RMS (mm) |
|---|---:|---:|---:|---:|---:|
| Plane | 0.000143092 | 0.000145188 | +0.000002096 | 0.129 | 0.026 |
| Tangent pulse | 0.001344085 | 0.001658348 | +0.000314263 | 2.489 | 2.498 |
| Normal pulse | 0.101849277 | 0.110142851 | +0.008293575 | 16.117 | 15.379 |

Across five matched model blocks, plane force-error differences were negligible. Under both pulse scenarios the proxy predicted lower force MAE for SFC; DSFC had lower path RMS in the plane and normal-pulse cases, while tangent-pulse path RMS was nearly the same. These are model-specific descriptions, not physical or inferential superiority claims. Maximum tangential-to-normal leakage was `8.67e-19 m/s`; the final RNN joint-velocity realization ran `[(1,)]` times per tick (one).

The run uses the offline adapter `TaseOfflineMethodAdapter → TaseRnnBaseline → StrictTaseRnnSolver`; it shares the strict RNN solver class with the live runtime but is not a live `TaseContactProvider` run and does not replay the R31 physical inputs. Its plant, force response, and disturbances are simulated. The inherited `120 N/m` path stiffness is retained only for model comparability and remains **not live-approved**. The live registry still has no `TASE_RNN_MATURE+SFC/DSFC` method.

The first process received SIGTERM after 19 complete attempts (cause unknown); all 19 traces passed file/digest checks. The deterministic runner resumed only missing attempt identities, preserving all original attempts. Batch details are in `runs/tase-sfc-dsfc-r31-aligned-model-20260925T1631HKT/batch_interruption.json`; final summary and traces are under `runs/tase-sfc-dsfc-r31-aligned-model-20260925T1631HKT/comparison/`.

## Latest read-only bench state — 16:54 HKT

After the user asked to power on, a fresh preflight found the controller already `Robotmode: RUNNING`, Remote Control, Safety NORMAL, and the program STOPPED. The RTDE sample was stationary with actual joint positions within `2.9e-5 rad` of the approved Figure-eight joint Home; payload/CoG/TCP remained `0.413 kg / [0.0011,0.0031,0.0163] m / 87.4 mm`. Kunwei TCP connect-only passed. No Power On command, motion, zeroing, or controller write was sent; there was no live writer before or after the proxy run. Evidence: `runs/poweron-continuation-final-preflight-20260925T165408HKT/preflight_readonly.json`. This is a one-snapshot read-only status check, not a new sustained stationarity qualification.

## Extended QP command-to-actual diagnostic — 2026-09-25T17:01:55+08:00 HKT

A wider sealed-trace association scan tested consumed-sequence offsets from −24 to +24 packets (approximately ±48 ms). The matched QP flattened correlation was `0.848627` at the official same-sequence join; the maximum was `0.852176` at offset `−1`, which compares an observed frame with a *future* command. This is noncausal and does not explain the gate. Per-joint best correlations for QP J2/J4 were only `0.709` / `0.889`; their peaks also used offset `−1`. J5 reached `0.279` at the edge of the scan, but RNN A also has weak J5 correlation and a command standard deviation several times smaller than actual qd.

The sealed command traces show a separate difference in the QP setpoint profile:

| Attempt | qd gate correlation | J2 setpoint-change rate p95 (rad/s²) | J4 setpoint-change rate p95 (rad/s²) | Host slew-limited rows | Output-feedback realization-limited rows |
|---|---:|---:|---:|---:|---:|
| R31 A1 RNN | 0.970072 | 0.03054 | 0.02516 | 7 / 29778 (0.024%) | 7 / 29778 |
| R31 A2 RNN | 0.972070 | 0.02957 | 0.02437 | 3 / 29991 (0.010%) | 3 / 29991 |
| R31 matched QP | 0.848607 | 0.89196 | 1.01713 | 513 / 29956 (1.713%) | 1857 / 29956 |

QP J2/J4 setpoint-change p95 is much higher, and the QP trace has more host slew interventions and output-feedback realization-limit flags. This makes abrupt joint-velocity changes a plausible proximal contributor to the qd mismatch, alongside the low-correlation joints. The change-rate statistic is computed from commanded setpoints, not measured robot acceleration; the flags describe the controller's output feedback, not a confirmed hardware saturation event. Correlation and output flags do not establish causality. The next offline diagnosis should compare QP active-set transitions and qdot change/actual-qd response at those specific rows. Do not change the frozen QP profile, relax the motion gate, or repeat it unchanged.

Detailed 49-offset/per-joint results: `reports/tase_r31_qdot_offset_scan_20260925.json`; transition counts and per-joint command-change distributions: `reports/tase_r31_qdot_output_transition_20260925.json`. Both source timelines were checked against their sealed hashes.
