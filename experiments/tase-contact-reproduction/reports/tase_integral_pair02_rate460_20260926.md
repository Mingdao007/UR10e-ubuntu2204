# TASE-RNN integral pair 02 at rate460

## Decision

The robot was already powered and in `Robotmode: RUNNING` when the user asked to power it on. No redundant Dashboard `power on` command was sent. A fresh resident start-check passed at the approved Autotuner joint Home, then one randomized resident pair ran in order B (0.5 N·s) → A (1.0 N·s).

The B attempt is eligible. The A attempt completed the full path and returned Home, but its host/RTDE rates fell below the frozen 460 Hz admission gate. Preserve its 1.020614 N MAE as a complete-path diagnostic only. The pair is incomplete and supports no paired treatment conclusion.

## Bound protocol

- Controller: `TASE_RNN_MATURE`; Figure-eight `r013_60_rate460`, 60.0 s PATH, formal window `[5,60)`, 5 N normal-force target.
- Shared parameters: `Md=9.565272137974492`, `Bd=693.6559295653944`; same R31 runtime, EOAT, approved Autotuner `home_q`, and controller package.
- A: legacy clamp, 1.0 N·s integral limit. B: legacy clamp, 0.5 N·s. No other control value changed.
- Randomization: seed `2026092601`, B then A. The frozen plan and parameter files are in [the pair directory](../runs/tase-integral-ab-pair02-rate460-20260926/).
- Fresh start-check: [figure8-20260925T180306-892422](../runs/figure8-20260925T180306-892422/). It observed 10 s of no-motion RTDE, 1,001 frames, stable Home, Safety NORMAL, and available video; it dispatched no ARM or PATH command.

## Pair results

| Arm | Result | Formal MAE | Host publish / RTDE / TP echo / Kunwei | Turnaround |
|---|---|---:|---|---:|
| B, 0.5 N·s | Complete 60 s, 550/550 bins; rate gate passed; joint Home and `ready_for_next` verified | 1.186203 N | 499.80 / 499.80 / 480.77 / 999.98 Hz | 86.69 s, cold |
| A, 1.0 N·s | Complete 60 s, 550/550 bins; rate gate failed; joint Home verified | 1.020614 N, diagnostic only | 429.15 / 429.15 / 376.62 / 1000.00 Hz | 85.20 s, reused |

The complete sealed session is [figure8-20260925T180605-892740](../runs/figure8-20260925T180605-892740/). Both attempts have raw sensor, RTDE, published-packet, command-timeline, and video records. The final Dashboard state was Remote Control, Safety NORMAL, robot powered, and program STOPPED. The final joint-Home error was `5.48e-5 rad`; the live owner and writer processes exited. Session closure is successful, while `all_evidence_eligible=false` because arm A failed timing admission.

## Timing diagnosis

The first failing layer is the host control/publish cadence. A's host-published and accepted RTDE-frame counts both correspond to 429.15 Hz, below the unchanged 460 Hz threshold. TP-consumed echoes fall further to 376.62 Hz. Kunwei remained at 1 kHz, so sensor sampling is not the limiting layer in this attempt.

In A, the host tick interval had median 2.002 ms, p95 4.027 ms, p99 4.194 ms, and maximum 5.827 ms; 41 of 60 one-second bins were below 460 publishes. The provider's maximum compute time was 1.540 ms and there were no provider calls over 2 ms. Publish latency had p95 1.653 ms. The scheduler recorded 2,525 missed 2 ms slots and 1,092 wakeups more than 0.5 ms late. In B, the host/RTDE rate was 499.80 Hz, with 10 missed-slot proxies and 23 wakeups over 0.5 ms.

These traces localize the shortfall to timing between host ticks and show that provider compute alone does not account for the observed p95 cycle interval. They do not yet identify whether RTDE frame availability, host poll cadence, OS scheduling, or an interaction caused the late ticks. The slightly higher count of force-rise/preempt branches under A is an association; it does not establish that the integral limit caused the timing failure.

## Next action

Do not repeat this pair unchanged and do not lower the 460 Hz threshold. Before another paired live block, add or expose bounded per-poll telemetry already available from the RTDE adapter: `ready`, `drained_packet_count`, poll wall/thread duration, plus the adjacent host-deadline and publish timestamps. That evidence can distinguish missing/late RTDE input from time consumed between a fresh frame and the next publish. Keep the failed A attempt in the denominator, and require a complete eligible A/B pair before calculating a paired effect.

## Metric boundary

The MAE values use the KWR75 feedback stream and the declared R31 `[5,60)` reducer. KWR75 feedback is not an independent task-force reference. A's complete-path metric remains diagnostic because its timing gate failed; it is not included as a valid A observation or as an optimizer objective.
