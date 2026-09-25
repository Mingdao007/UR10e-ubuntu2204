# R31 command-to-Cartesian velocity diagnostic

**Checkpoint:** 2026-09-25, 17:55 HKT
**Claim class:** sealed live telemetry diagnostic; not a controller acceptance score
**Experiment:** `TASE_RNN_MATURE` versus matched-outer-loop `TASE_QP`, Figure-eight `r013_60_rate460`, 5 N, A profile `Md=9.565272137974492`, `Bd=693.6559295653944`, legacy integral limit `1.0 N·s`.

## Current result

The robot was already powered on. A fresh read-only preflight found Safety NORMAL, the short-EOAT Installation values unchanged, Kunwei reachable, the controller stopped and stationary at the approved joint Home, and no live writer. The live owner then passed a 10 s resident no-motion start-check with 1,000 RTDE frames. A first `required` video preparation failed before dispatch; the failed directory has a `terminal-no-dispatch` receipt and no writer artifacts. The approved `evidence-only` route then passed, recorded usable video, and ran one complete A baseline.

The new R31 A unit completed a 60.0 s PATH with **550/550** formal `[5,60)` bins, formal MAE **1.017505 N**, effective TP-consumed rate **480.567 Hz**, official joint-velocity correlation **0.967947**, and verified joint Home (maximum joint error `6.29e-5 rad`). The program and all writer processes were stopped after the run. Its full run-to-seal time was **85.293 s**; this is one cold single-attempt cycle, not a reused-cycle median.

The three eligible same-profile A units now have formal MAEs **1.282720, 1.006797, and 1.017505 N** (mean **1.102341 N**, sample SD **0.156304 N**, range **0.275924 N**). These are descriptive baseline repeats, not a treatment comparison.

The matched TASE-QP attempt still fails its frozen `actual_qd` correlation gate (**0.848607 < 0.900**). Its **0.950139 N** MAE remains diagnostic only. It completed the PATH, passed rate and solver deadlines, returned to Home, and had endpoint error `0.0555 mm`; none of those outcomes overrides the failed motion gate.

## Sealed sequence join

The analyzer verified the SHA-256 values for each attempt's sealed `command_timeline.jsonl` and `robot_frames.jsonl`. It joined each host command to the first RTDE row echoing the exact same `consumed_packet_sequence`, with no packet offsets. It analyzed both PATH `[0,60)` and formal `[5,60)` windows separately. The formal window has about 28.8k joined samples per attempt; joined command coverage is 95.9–96.2% because the host publishes at 500 Hz while the TP-consumed stream is about 480 Hz.

The command Jacobian is sampled at publish time. RTDE target and actual joint velocities and `actual_TCP_speed` come from the first RTDE frame echoing that command sequence. Results localize the observed discrepancy across these telemetry layers, but they do not establish the robot-side cause.

| Attempt | Formal MAE (N) | Official `qd` correlation | Command → RTDE target `qd` J2/J4/J5 | Target → actual `qd` J2/J4/J5 | `J·command` vs RTDE linear speed: r / RMSE (m/s) | Eligible |
|---|---:|---:|---|---|---:|---|
| RNN A1 | 1.282720 | 0.970072 | 0.9960 / 0.9962 / 0.9847 | 0.8666 / 0.9448 / 0.2056 | 0.9577 / 0.000560 | Yes |
| RNN A2 | 1.006797 | 0.972070 | 0.9960 / 0.9968 / 0.9871 | 0.8844 / 0.9514 / 0.1983 | 0.9685 / 0.000475 | Yes |
| RNN A3 | 1.017505 | 0.967947 | 0.9967 / 0.9967 / 0.9899 | 0.8758 / 0.9502 / 0.2064 | 0.9654 / 0.000499 | Yes |
| TASE-QP matched A | 0.950139 | 0.848607 | 0.9714 / 0.9728 / 0.6607 | 0.7104 / 0.8896 / 0.0522 | 0.7829 / 0.001643 | **No** |

For the matched QP, command-to-target tracking is worse than all three A runs on J2/J4/J5; target-to-actual tracking is especially worse on J2/J4. Its requested-command to RTDE linear-speed correlation is also lower, with about three times the RNN A RMSE. The main method-specific signal is the larger J2/J4 command and actual-velocity variation, consistent with the earlier finding that QP setpoints change more sharply during flagged episodes.

| Joint | RNN A3 command std (rad/s) | RNN A3 actual std (rad/s) | Matched QP command std (rad/s) | Matched QP actual std (rad/s) | Target-to-actual RMSE / command std: RNN A3 / QP |
|---|---:|---:|---:|---:|---:|
| J2 | 0.001985 | 0.002283 | 0.004041 | 0.007936 | 0.56 / 1.44 |
| J4 | 0.002700 | 0.003049 | 0.004758 | 0.008510 | 0.36 / 1.01 |
| J5 | 0.000254 | 0.001418 | 0.000212 | 0.000882 | 5.46 / 4.24 |

This also limits the J5 interpretation: its command has very small variation in both methods, and actual J5 variation is much larger than that command in RNN A as well as QP. J5's low correlation is not by itself a QP-specific root. Correlation, amplitude and RMSE are kept separate in the machine-readable record.

The QP J2/J4 setpoint dispersions are about 1.8–2.0 times RNN A3, while actual dispersions are about 2.8–3.5 times larger. This pattern is compatible with a method-specific command/servo interaction, but remains an association; it does not prove the QP solver alone caused the tracking loss.

Across all four attempts, `J·actual_qd` and the recorded RTDE `actual_TCP_speed` agree to numerical precision. This is a kinematic consistency check between telemetry fields, not independent position-derived motion evidence or force truth. RTDE velocity therefore narrows where the mismatch appears but does not provide an independent physical measurement of it.

## Source-level mechanism candidate

The deployed native QP is a per-tick minimum-norm solution: the builder uses `P=I`, `q=0`, so the objective is `min 0.5 ||qdot||^2`, subject to exact `J qdot = twist` and the joint-velocity box bounds. It contains no previous-command term. The native solver's retained `x/y` values are OSQP warm-start state; the mathematical objective still does not prefer the last published `qdot`. See [`build_contact_qp.py`](../tools/build_contact_qp.py), [`contact_qp.py`](../tools/contact_qp.py), and the QP branch in [`calibrated_runtime.py`](../tools/step5d_autotune_v4_r004/calibrated_runtime.py).

After the solver returns, the shared TASE provider applies a same-direction scalar qdot slew rescale against the previous published command. That common post-solver step can move `J qdot` away from the equality solution for the original QP task. In the matched QP trace it activated on **513/29,956 rows (1.71%)**, compared with **7/29,778** in A1 and **3/29,991** in A2; the QP also had **1,857** final-realization feedback flags. The implementation is in [`tase_contact_provider.py`](../tools/tase_contact_provider.py).

This gives a specific, testable mechanism: a configuration-dependent minimum-norm solution may change more sharply than the stateful RNN output, causing more post-solver slew scaling and downstream tracking error. The measured J2/J4 variation and flag-conditioned qd results are consistent with it. It is **not yet a confirmed root cause**; the unflagged QP subset still correlates at only 0.907. Do not retry this profile unchanged. The exact frozen-input replay below now captures pre-slew solver output and post-slew command. Any added previous-qdot objective term or altered bound needs a separate numerical discussion before a live profile change.

## Interpretation and next branch

- Preserve both QP attempts as failed at the unchanged `0.900` gate. Do not use their diagnostic MAEs as eligible BO observations, and do not repeat either live profile unchanged.
- The new evidence narrows the observed chain to both host-command-to-controller-target and controller-target-to-actual-velocity tracking. The proximal hypothesis remains abrupt, QP-specific setpoint changes interacting with TP/robot velocity realization; the exact cause is **unconfirmed**.
- Next compare representing the same slew envelope inside QP bounds against the current post-solve rescale, including infeasible rows and residuals. Keep the live QP numbers and gate fixed.
- Continue the R31-identity SFC+TASE same-input replay offline. The live registry does not yet offer this composed method, so no SFC+TASE hardware result is implied.
- The post-run preflight confirmed the robot remained at joint Home, Safety NORMAL, program STOPPED, with no writer/bridge process. Kunwei remains controller feedback, not independent task-force truth.

Machine-readable metrics and the source-hash-bound reducer output: [`tase_r31_cartesian_velocity_diagnostic_20260925.json`](tase_r31_cartesian_velocity_diagnostic_20260925.json). The reducer and focused regression tests are [`analyze_tase_r31_cartesian_tracking.py`](../tools/analyze_tase_r31_cartesian_tracking.py) and [`test_analyze_tase_r31_cartesian_tracking.py`](../tests/test_analyze_tase_r31_cartesian_tracking.py).

## Evidence

- RNN A3 attempt: `runs/tase-r31-a-baseline-repeat-03-20260925T1730HKT/attempts/0001/attempt-result.json`
- RNN A3 lifecycle, video, and stop receipts: `runs/tase-r31-a-baseline-repeat-03-20260925T1730HKT/{dispatch_receipt.json,supervisor-result.json}`
- No-motion start-check: `runs/tase-r31-resident-startcheck-evidenceonly-20260925T1728HKT/supervisor-result.json`
- Earlier `required` video-preparation no-dispatch: `runs/tase-r31-resident-startcheck-poweron-20260925T1725HKT/terminal-no-dispatch-receipt.json`
- Fresh post-A3 preflight: `runs/post-qp-replay-preflight-20260925T1753HKT/preflight_readonly.json` (observed 17:55:37 HKT); the earlier A3 preflight is retained at `runs/post-a-repeat03-preflight-20260925T1740HKT/preflight_readonly.json`.
- Matched TASE-QP attempt: `runs/tase-r31-qp-matched-a-rate460-20260925T1615HKT/attempts/0001/attempt-result.json`
- Exact frozen-input QP plus host-slew replay: `runs/tase-r31-qp-matched-a-fixed-input-replay-20260925T1748HKT/summary.json`
- Earlier per-joint, offset, and event evidence: `reports/tase_r31_qp_matched_a_motion_gate_analysis_20260925.json`, `reports/tase_r31_qdot_offset_scan_20260925.json`, `reports/tase_r31_qdot_row_linkage_20260925.json`

## Exact frozen-input QP and host-slew replay — 17:53 HKT

A subsequent read-only preflight at 17:55:37 HKT recorded Robotmode RUNNING, Safety NORMAL, Dashboard program STOPPED, zero reported TCP speed, the approved short EOAT payload/CoG/TCP, Kunwei connect-only reachability, and no live writer. Its joint sample is within `3.3e-5 rad` of the approved Figure-eight joint Home. No Power On, program start, sensor write, or motion command was sent during this check.


The replay verified the matched attempt's sealed command-timeline and robot-frame hashes, exact packet-sequence inputs, and the same OSQP codegen-C library SHA recorded in the live dispatch receipt (`a38ba13d…6480878`). It ran the complete 29,956-row sequence through the native QP followed by the captured host slew operation, with no solver rejection.

- Replayed host-slew-limited rows: **513**, exactly matching the sealed trace; maximum slew-scale difference was `4.04e-7`.
- Final replayed qdot versus published qdot: median infinity error `3.69e-18 rad/s`, p95 `1.73e-17 rad/s`, maximum `1.224e-5 rad/s`.
- Pre-slew QP equality residual maximum: `1.01e-8 m/s`; after the captured slew, task residual maximum was `0.001640 m/s` (p95 `0.000896 m/s` on slew-limited rows).
- The official actual-qdot correlation remains `0.848607`, below the frozen `0.900` gate. The run remains failed for method eligibility; this replay changes neither its classification nor denominator.

This confirms the solver-plus-host-slew command reconstruction, and quantifies that post-solve rescaling can move a row off the exact Cartesian equality. It does **not** show that the rescale caused the robot-side actual-qdot mismatch, nor predict a closed-loop force/path result. Next offline branch: compare representing the existing slew envelope inside QP bounds against current post-solve rescaling, recording infeasibility and task residual without changing the live profile. Any live numerical profile change remains separately discussed first. Replay details are in [`summary.json`](../runs/tase-r31-qp-matched-a-fixed-input-replay-20260925T1748HKT/summary.json).
