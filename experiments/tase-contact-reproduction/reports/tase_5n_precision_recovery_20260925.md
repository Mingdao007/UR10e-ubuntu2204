# TASE 5 N normal-force precision — recovery checkpoint

**Checkpoint:** 2026-09-25, 17:01 HKT

**Status:** Two eligible R31 rate460 TASE-RNN-MATURE A units completed 60 s PATHs (formal MAE 1.282720 N and 1.006797 N). Both complete TASE-QP paths remain failed at the frozen actual-qdot correlation gate (0.840132 and 0.848607 versus 0.900); expanded offset scans did not find a causal timing shift that passes it. QP J2/J4 setpoint changes and output-limit feedback are candidate contributors, not a confirmed cause. A separate five-block R31-parameter-aligned SFC/DSFC model proxy finished 30/30 with zero simulation failures; it is not physical evidence. Fresh preflight found the robot already powered, Safety NORMAL, TP STOPPED, and near the approved joint Home; no power command or motion was sent. Next: inspect QP active-set/realization-shortfall rows offline, then close a reviewed live SFC+TASE identity before any combined run.

**Evidence boundary:** KWR75 feedback is the controller/metric signal, not an independent task-force truth. RPSFC/SFC/DSFC drift results below remain a separate experiment.

## Earlier continuation update — 07:18 HKT (historical)

The 100 mN RPSFC run completed for 20 s at the raised short-EOAT start and returned to the approved Autotuner joint Home. The latest noise replay on the 30 s, approximately 1 kHz static KWR75 record gives `rho=0.1 N` an instantaneous rest-ball hit rate of 46.1–48.5%, but the stateful effective-rest branch only 1.9–2.0% of 500 Hz ticks. The `rho=1 N` reference stays exactly at rest on the same compensated noise sequence. This suggests 100 mN admits much of the measured noise; it does not establish comparative live drift. Full live and offline evidence remains in the separate [RPSFC report](/home/andy/.codex-worktrees/short-eoat-sfc-dsfc-drift-20260924/reports/short_eoat_rpsfc_100mn_20260925.md) and [latest noise replay](/home/andy/.codex-worktrees/short-eoat-sfc-dsfc-drift-20260924/runs/rpsfc-100mn-offline-replay-20260925T0711HKT/summary.json).

The first TASE `r013_60_rate460` retry used the frozen A parameters (`Md=9.565272137974492`, `Bd=693.6559295653944`, integral limit `1.0 N·s`) and the fresh short-EOAT package read-back. It failed with `ValueError: unsupported TASE Figure-eight protocol 'figure8_window60_r013_rate460_v1'` before ARM: `motion_commands_from_supervisor=0`, no dispatch receipt, no contact search, and no PATH. The TP was stopped; the supervisor verified `Safety NORMAL`, zero actual speed, and the approved joint Home. The original failure is [`supervisor-result.json`](../runs/tase-a-baseline-rate460-20260925T0712HKT/supervisor-result.json); it remains failed and is not an A observation.

The first defect was a missing rate460 branch in `TaseContactProvider` protocol construction, seam handling, and Home-loaded candidate duration validation. Those branches now include the separate rate460 ID/token without changing rate thresholds, PATH timing, or controller gains. The rate460 provider and real owner factory tests verify protocol propagation, bounded returning seam, matching Home-loaded candidate, and actual `Md/Bd` binding. Provider, protocol, and autotuner tests passed **67/67**, the failure-continuation test **1/1**, and the factory-level targeted tests **3/3**.

The second retry passed that seam and bound the expected A parameters, then failed constructing the R013 timing collector with `TimingError: R013 timing threshold differs from acceptance protocol`. The collector supported the legacy `r004_460` and new rate400 protocol IDs but omitted the new rate460 ID. It sent no ARM and did not begin contact or PATH: `dispatch_receipt.json` records `armed=false`, `executed=false`; the six published packets were all HOLD mode, the attempt command timeline is empty, and RTDE `actual_qd`/`target_qd` are zero. The program stopped and the same joint Home was verified at `4.63e-5 rad` maximum joint error. This failed attempt remains preserved at [`attempt-result.json`](../runs/tase-a-baseline-rate460-20260925T0720HKT/attempts/0001/attempt-result.json).

The lightweight collector now accepts both `minimum_rate_hz=460` with `figure8_window60_r013_rate460_v1` and the distinct rate400 identity; mismatched rate/identity pairs still fail closed. The regression instantiates the actual rate460 PATH evidence collector. Timing characterization, reuse, and ledger tests passed **25/25**, then the collector-integration regression passed **1/1**. The resulting live attempt is documented below.

## Continuation update — 08:23 HKT

The one eligible A attempt's sealed command trace was re-audited read-only. Its seal is intact, 29,984 reference rows join their sent PATH packets, and the inputs are usable for a **future solver-only offline QP replay**. The audit did not execute QP or establish a QP result. Its trace schema is v1 and lacks per-frame integral/saturation values, so it cannot diagnose windup; see the [input audit](tase_r013_rate460_a_replay_input_audit_20260925.md).

To close that gap on future attempts, the resident trace has a v2 fixed-buffer schema that records every PATH tick's signed force channels and target/error, integral state and single/double clamp flags, actual `q/qd`, solver and slew envelopes, sent `qdot`, final published `Jqdot` saturation feedback, `dt`, solver/provider time and host-to-publish latency. The recorder commits only after a successful publish and marks missing final-output feedback as incomplete. Existing attempts stay immutable and retain their v1 identity. Focused provider, resident-session and replay-audit checks passed **92/92**. A synthetic 10,000-tick buffer-only benchmark measured 0.018 ms mean, 0.021 ms p99 and 0.043 ms max per record/commit, using a 12.18 MiB test buffer. This is an offline code-path measurement, not a live frequency guarantee; the next A repeat must verify the existing >=460 Hz gates with v2 traces.

## Continuation update — 08:44 HKT

The same frozen A configuration completed a second live 60 s unit at [`tase-a-baseline-rate460-v2-repeat-20260925T0836HKT`](../runs/tase-a-baseline-rate460-v2-repeat-20260925T0836HKT/dispatch_receipt.json). It sealed all 550 formal `[5,60)` bins, passed the 460 Hz admission floor in every raw layer, recorded continuous video, returned to the approved joint Home (maximum joint error `7.77e-5 rad`), and stopped the Dashboard program with Safety NORMAL. The 60 s formal bin-mean MAE was **1.269608 N**. The first A unit was **1.172493 N**; the two-unit mean is **1.221050 N**, with a 0.097115 N observed difference between runs. `n=2` gives no stable variability estimate or confidence interval and cannot establish an improvement.

The offline [v2 per-frame audit](tase_r013_rate460_a_repeat_v2_trace_audit_20260925.md) verified the seal and all **29,950** command rows. The matched RNN command-input audit did not run a QP. The [derived reduction](tase_r013_rate460_a_repeat_reduction_20260925.json) provides the first complete integral, saturation, joint-motion and timing view:

- Normal force from raw corrected KWR75 samples had MAE/RMSE **2.458/3.023 N** over `[0,60)`, **2.596/3.064 N** over `[0,5)`, and **2.446/3.019 N** over `[5,60)`. These are sample-level diagnostics; the primary score remains the formal 550-bin MAE. The longest below-1 N run in `[5,60)` was **0.448 s**, a contact-loss proxy rather than visual proof.
- The integral state reached at least 98% of its 1.0 N·s limit on **3,466/29,950 ticks (11.6%)**, with maximum exactly 1.0 N·s. The legacy strategy's event flags show zero clamp events because they report a clamp transition, not time spent held at a limit; the numeric state trace is the evidence for limit occupancy. Task-space saturation feedback was true on **9,157 ticks (30.6%)**; final realization saturation and host slew limiting each occurred on 16 ticks. Force-preempt warm starts occurred 263 times and direction retries 1,104 times. These are observed controller events, not yet proven causes of the force error or perceived step-like motion.
- Raw rates were host **499.2 Hz**, RTDE **499.1 Hz**, TP-consumed echoes **480.0 Hz**, and Kunwei **999.98 Hz**. The accepted synchronized-row rate is 480.0 Hz and remains separately labeled. RTDE and TP-echo longest intervals were **20.0 ms**; the maximum live freshness gap was 19.674 ms against a 20 ms limit.
- The largest recorded host observation gap was **19.674 ms** at PATH reference 8.164 s, packet 13,719. That tick's solver time was **0.098 ms**, provider time **1.164 ms**, and host publish latency **1.748 ms**, with 3.067 N measured normal force, 0.463 N·s integral, no active transition flag and no host slew scaling. This locates the long interval outside measured solver/provider work, but current traces do not identify whether the preceding wait came from scheduler, RTDE delivery or another host input wait. Host publish p99 interval was 2.641 ms; solver max was 0.181 ms and provider max 1.465 ms.
- The State-21 release replay passed the separate 0.5 s conjunction for **0.501621 s**, only 1.6 ms above the required dwell; the first PATH packet followed ramp completion by 0.727 s. Together with the first A's 0.540 s pass, both live attempts passed narrowly. Keep the gate unchanged while diagnosing its margin; the older rate400 reason-48 attempt remains a 0.267 s failure against the same 0.5 s predicate.

The attempt took **85.575 s** from start to Home receipt and **86.029 s** through sealing, but it was a cold cycle. This does not establish the ordinary reused-cycle median. Both receipts preserve joint Home as authoritative; TCP pose is only a cross-check.

## Continuation update — 09:00 HKT

### Same-input CPU/GPU RNN replay

The sealed second-A v2 trace was replayed through the existing `StrictTaseRnnSolver` with the same `legacy-r1` equations, `epsilon=0.022`, `r=1`, one iteration, and the same Jacobian, bounds, twist, `dt`, and recorded PATH-origin/warm-start states. Only the backend changed between NumPy and CuPy. This is an offline solver microbenchmark, not a new registered live profile.

- All **29,950** PATH rows replayed. Maximum CPU/GPU command difference was `4.62e-9 rad/s`; maximum recurrent-state differences were `4.62e-9` for theta and `1.62e-8` for lambda. CuPy's internal 100-case parallel/serial equivalence check was bitwise exact.
- On this machine, NumPy host-call time was median **0.0525 ms**, p99 **0.0585 ms**, max **0.1479 ms**. CuPy was median **0.0671 ms**, p99 **0.1417 ms**, max **0.3518 ms**. The tiny six-joint, one-iteration solve is slower on GPU because transfer/launch/completion overhead exceeds its compute benefit.
- The isolated CUDA runtime was installed under `/tmp`; the project environment and live runtime were not changed. The paper-truth gate stayed enabled. GPU is not a demonstrated cure for the 19.674 ms host gap and is not qualified for live use; keep the current CPU backend while investigating scheduling and input wait.

Full per-tick timing and numerical deltas are in [the CPU/GPU replay receipt](tase_rnn_cpu_gpu_replay_20260925.json). The sealed input check is [the repeat trace audit](tase_r013_rate460_a_repeat_replay_input_audit_20260925.md).

### Integral and saturation trace replay

The A trace's `legacy-clamp-v1` integral state was replayed exactly: maximum state difference from the sealed trace was **0**. The state was at least 98% of its `1.0 N·s` limit on **3,466/29,950 ticks (11.6%)**. Of those ticks, force error still pointed toward the positive limit on **2,940** and pointed back toward unwinding on **526**. The actual A run also had task-space saturation on **9,157 ticks (30.6%)**, realization saturation on 16, and host slew limiting on 16.

With the measured A force-error and `dt` sequence held fixed, a mechanism-only replay compared legacy caps `1.0/0.5/0.1 N·s`, integral off, and the existing double-clamp helper with a `0.5 N` authority cap. The `0.5` legacy cap reduced mean absolute integral state from **0.594 to 0.278 N·s**. The double-clamp replay had **0.388 N·s** mean absolute state, with its conditional/final-output freeze active on 9,297/9,047 ticks. It used the A run's previous output-shortfall signs as fixed inputs; it did not recompute the altered command's own saturation. Its state occupancy was therefore not a demonstrated improvement over the simple `0.5` cap.

This replay cannot estimate candidate force MAE: all cases see the same measured force history, so robot motion and sensor feedback do not respond to the changed integral. It identifies a bounded first comparison—**A versus B (`0.5 N·s` legacy cap), followed by B versus D only if the full output-feedback path is recomputed**—without declaring an anti-windup winner. See the [integral replay receipt](tase_integral_policy_trace_replay_20260925.json).

### What the 1 kHz Kunwei count means

The timing receipt's cumulative Kunwei frame counter records **59,999 parsed sensor frames in 60 s (999.98 Hz)**, while the host publishes at **499.2 Hz**. The resident transport parser confirms why those numbers differ: each nonblocking poll can parse a batch, increments the physical-frame counter by the batch size, and returns only the latest wrench to the control loop. The sealed `raw_sensor.jsonl` stores about one host-used wrench per loop observation, not all individual frames in each batch. Thus sensor acquisition is about 1 kHz, while force control still updates at about 500 Hz; the sealed trace cannot test pairwise averaging or an anti-alias filter because intermediate batch values were not retained.

The next instrumentation decision is to retain those per-frame batch values in a later diagnostic capture before claiming a filtering benefit. Do not raise controller rate or change filtering based only on the cumulative frame counter.

### 5 N normal-axis second-order model audit

Both sealed A paths were examined as passive, natural excitation only: actual TCP height was joined to the nearest earlier RTDE frame, KWR75 normal force was aligned to the host-used sensor sample, and data were restricted to 3–7 N. A descriptive model `F = c + Kx + B xdot + M xddot` was fit with `x` positive into the surface. Savitzky-Golay derivative windows of 21, 51, 101, and 201 samples were tested; gaps over 4.5 ms were split before uniform 500 Hz resampling.

The result does **not** identify a usable local second-order model. Across both A paths, force-versus-position-only `R²` was about **0.0016–0.0048**. Adding velocity and acceleration changed fit `R²` from about **0.01 to 0.30** as the smoothing window changed, while fitted K changed sign and M stayed negative and ranged from about **−1.8 to −522 kg**. Splitting the same path in half also changed the coefficients materially. The standardized regressor condition numbers were near 1.1, so simple numerical collinearity alone does not explain the mismatch; the closed-loop path travels over changing XY contact locations and provides no isolated local normal excitation. KWR75 is also the feedback signal, not independent task-force truth.

Therefore no `zeta`-derived live numbers are available. The relations `B=2*zeta*sqrt(M*K)` and `K=B^2/(4*zeta^2*M)` apply only after identifying a meaningful local second-order model with positive, supported M and K. Preserve an independent normal K/B grid as a separately named **offline** comparison; do not substitute the tangential `Bd` or stiffness, and do not load coefficients from this rejected fit. Full details are in the [normal-axis identifiability receipt](tase_normal_axis_model_identifiability_20260925.json).

## Current result

### Separate RPSFC result

The short-EOAT RPSFC run at `rho=0.1 N` completed a 20 s no-contact canary and returned to the approved Autotuner joint Home. RTDE measured maximum 3D TCP displacement `0.4948 mm`; no 10 mm stop was approached. The Home receipt verified maximum joint error `4.07696e-5 rad`, Safety NORMAL and Dashboard STOPPED; OctoStream video was recorded. On the static-noise replay, 100 mN admitted 46.1–48.5% of instantaneous samples into the rest ball, but the stateful effective-rest fraction was 1.9–2.0% at 500 Hz; the 1 N reference stayed at rest on those same samples. There is no matched live 1 N run, so this does **not** establish improved real drift or the response to a light human push. See the separate [RPSFC report](/home/andy/.codex-worktrees/short-eoat-sfc-dsfc-drift-20260924/reports/short_eoat_rpsfc_100mn_20260925.md) and [noise replay](/home/andy/.codex-worktrees/short-eoat-sfc-dsfc-drift-20260924/runs/rpsfc-100mn-offline-replay-20260925T0711HKT/summary.json).

### First complete TASE-RNN A unit

The run [`tase-a-baseline-rate460-20260925T0724HKT`](../runs/tase-a-baseline-rate460-20260925T0724HKT/dispatch_receipt.json) used `TASE_RNN_MATURE`, not TASE-QP, with 5 N target, 60 s Figure-eight, protocol `figure8_window60_r013_rate460_v1`, `Md=9.565272137974492`, `Bd=693.6559295653944`, and legacy-clamp integral limit `1.0 N·s`. The generic TP program name contains `six_qp`, but the actual host controller identity and solver backend were TASE mature RNN / NumPy.

| Stage or measure | Observed result | Interpretation |
|---|---:|---|
| State 20 contact search | 9.458 s; TCP z from 34.064 mm to 20.539 mm; raw normal peak 6.320 N | The fetched-back 8 mm far-to-near brake package reached contact and the 1 N latch; this is one run, not a paired causal estimate. |
| State 21 latch/ramp/release | 8.870 s; 8 s R013 ramp; complete release condition passed | See the gate correction below. The successful path had only about 0.540 s of combined qualifying samples before the first PATH command, just above the 0.5 s minimum. |
| Entry and PATH | Entry 1.010 s; full 60 s PATH; 550/550 formal bins over `[5,60)` | Full path coverage and formal score were produced. |
| Formal force score | Normal-force bin-mean MAE **1.172493 N** | One eligible A baseline unit, not an A repeatability estimate or campaign result. KWR75 is feedback, not independent force truth. |
| Raw corrected KWR75, PATH `[0,60)` | MAE 2.380 N; RMSE 2.980 N; bias −1.190 N; 23.72% below 1 N; longest below-1 N run 0.590 s | Supplementary unfiltered-sample diagnostic; do not substitute for the formal 550-bin score. |
| Raw corrected KWR75, PATH `[5,60)` | MAE 2.317 N; RMSE 2.928 N; bias −1.165 N; 22.84% below 1 N; peak 12.598 N | Same diagnostic limitation; high-frequency samples and bin-mean metric have different weighting. |
| Timing | host 499.77 Hz; raw RTDE 499.67 Hz; unique TP-consumed PATH echo 480.68 Hz; Kunwei 999.97 Hz | Every measured layer exceeded 460 Hz. The synchronized accepted-sample rate is reported separately below; it is not the raw RTDE rate. |
| End state | Approved joint Home verified, max joint error `7.10e-5 rad`; Safety NORMAL; Dashboard STOPPED | The supervisor completed stop and Home. Continuous video was unavailable at this attempt's recorder start; synchronized RTDE, Kunwei and TP traces remain. |

The attempt-level internal seal time is 85.075 s from run start; the resident-sidecar's full `SEAL complete` event is at **85.342 s**. This was a cold start with no refresh and is not the ordinary reused-cycle median criterion. Home-to-Home (`83.410 s`) is not used as the end-to-end time.

### State 20/21 gate correction

The prior report's “10 s State-21 readiness” explanation was a profile mismatch. The active r004 contract sets `qualified_path_trial_hold_s=0.1 s`; after the 1-to-5 N ramp, the separate `PathEntryReleaseGate` requires 0.5 continuous seconds with fresh sensor, filtered normal 4–5.5 N, raw normal 3–7 N, force norm ≤7 N, torque norm ≤0.30 Nm, stationary TCP and joints, and Safety NORMAL. A packet-by-packet replay is recorded in [the rate400 failed-attempt replay](tase_r013_rate400_state21_release_replay_20260924.json) and [the rate460 A replay](tase_r013_rate460_state21_release_replay_20260925.json).

For the 2026-09-24 rate400 reason-48 failure, that complete conjunction lasted at most **0.267 s**, the stationary predicate at most 0.410 s, and the raw-force range at most 0.488 s. No host PATH packet was published; TP remained in baseline mode 1 until the 30.008 s watchdog. That is consistent with failure to open the 0.5 s release gate, not a failed 10 s hold. In the successful 2026-09-25 rate460 A run, the longest complete pre-PATH interval was **0.540 s**; the first host PATH command followed 0.599 s after the 8 s ramp completed. This is a narrow pass and merits an A replication. The previous `tase_state21_readiness_replay_20260925.json` used an inapplicable 10 s predicate and is superseded for these R013 attempts; original failed receipts are unchanged.

The earlier 11.029 mm near-switch trace had 10.551 N peak normal load and 10.651 N force norm. The rate460 run used the fresh 8 mm package and peaked at 6.320 N during State 20. This direction is consistent with reduced contact overshoot, but the attempts were not randomized matched pairs. It does not prove a causal improvement.

### Rate-layer count reconciliation

`timing_evidence.layer_rates_hz` remains a conservative count of distinct identities represented in fully synchronized accepted `PathSample` rows. Its `writer_publishes`, `rtde_frames`, and `tp_consumed_packet_echoes` values all read 480.683 Hz because each counts the matched accepted sample set, not each raw interface stream. The corrected [resident evidence analyzer](../tools/analyze_tase_resident_evidence.py) now reports raw layers separately:

- 29,986 successful host PATH publishes (499.767 Hz) and 29,984 formal reference sequences.
- 29,980 raw RTDE frames in the controller-clock 60 s formal window (499.667 Hz).
- 28,841 distinct TP-consumed formal echoes (480.683 Hz); 1,143 formal host sequences had no echo in that window, with 1,139 skipped sequence steps between observed echoes.
- 59,998 distinct Kunwei frames (999.967 Hz).

The TP-consumed echo count is the narrowest layer and still clears 460 Hz. Raw RTDE frames and the synchronized acceptance sample rate now have explicit separate names.

The sealed A command trace also passes the read-only input-integrity audit for a future offline QP replay: 29,984 command rows join the sent packets, and the seal is verified. This means the recorded outer twist, actual `dt`, Jacobian, bounds and RNN origin/warm-start states are suitable for a solver-only replay. It does **not** mean a QP was run or compared, and the old v1 trace still lacks per-frame integral/saturation diagnostics; that audit cannot answer the windup question. See the [rate460 A replay-input audit](tase_r013_rate460_a_replay_input_audit_20260925.md) and [machine-readable result](tase_r013_rate460_a_replay_input_audit_20260925.json).

## TASE evidence reviewed

| Question | Observed evidence | Meaning and limitation |
|---|---|---|
| Did the new A attempts reach PATH? | Yes. Two 2026-09-25 rate460 attempts each completed 60 s, 550/550 bins, passed the four-layer rate gate, sealed data and returned to joint Home. Formal `[5,60)` bin-mean MAEs were **1.172493 N** and **1.269608 N**. | Two score-eligible baseline units; too few to estimate a stable repeatability distribution or demonstrate a treatment effect. |
| What happened in State 20? | The older 11.029 mm near-switch attempt reached the near branch at 11.066 mm and 4.912 mm/s; peak normal was 10.551 N. The fresh 8 mm package reached contact in 9.458 s; TCP z moved 13.525 mm from the Home start and State-20 raw normal peaked at 6.320 N. | The 8 mm candidate is now live-observed once. Results are consistent with reduced impact, but are not matched randomized trials and cannot establish causality. |
| Why did the 2026-09-24 attempt stop at reason 48? | It stayed in TP `baseline_mode=1` for 30.008 s and emitted no host PATH command. Replaying its published packets against the active 0.5 s release gate gives a longest complete post-ramp conjunction of **0.267 s**; stationary alone lasts 0.410 s and raw-normal range alone 0.488 s. | The 10 s readiness replay used a mismatched predicate. The observed failure is consistent with not achieving the actual 0.5 s continuous force/freshness/stationary release. The original attempt remains failed. |
| Did the new State 21 gate pass with margin? | Yes. The two complete pre-PATH release dwells were **0.540 s** and **0.501621 s**, with the second only 1.6 ms over the 0.5 s minimum. | Both passed narrowly; preserve the current gate and continue recording its margin. |
| What do the four raw data layers show? | Host sends 499.767 Hz; raw RTDE 499.667 Hz; TP unique consumed echoes 480.683 Hz; Kunwei 999.967 Hz. | Each exceeds 460 Hz. About 1,143 formal host sequences were not echoed in the 60 s window. The receipt's equal 480.683 Hz `writer/RTDE/TP` fields count identities in synchronized accepted samples, not raw interface rates. |
| Is there earlier timing history? | The 2026-09-23 `compat_v1` resident run had five timing-eligible paths and a sixth at 454.65 Hz below 460; its eligible `[5,60)` scores were 1.772–1.882 N with 0.1 N·s integral. | Historical timing/force diagnostics only: protocol and integral differ from the new A baseline. The old rate400 best `1.163876 N` remains historical and is not a new rate460 paired control. |

The new A identity is `TASE_RNN_MATURE`, 5 N, 60 s PATH, formal window `[5,60)`, `Md=9.565272137974492`, `Bd=693.6559295653944`, legacy-clamp integral limit `1.0 N·s`, Figure-eight joint Home. The old rate400 result `1.163876 N` remains historical only.

## Next milestones — 09:47 checkpoint (historical)

1. The two A units establish that the 8 mm contact package and 0.5 s release gate can pass, but the release dwell has almost no margin. Preserve both successes and the old failed attempt; do not infer a contact-gate reliability rate from only two passes.
2. The full-trace CPU/GPU solver comparison is complete and did not improve timing. Attribute the 19.674 ms gaps to the unresolved scheduler/RTDE/input-wait path, not the six-joint solver. The sensor counter confirms roughly 1 kHz physical frames, but only the latest of each polled batch is consumed and sealed, so high-rate filtering utility remains unmeasured.
3. The A integral state and saturation signals are now quantified. The fixed-input replay supports `0.5 N·s` as the first simple one-factor live candidate; it does not estimate candidate force MAE. Recompute D's final-output feedback from D commands before treating D as a live candidate.
4. The passive natural-path fit did not identify the 5 N normal-axis model; do not derive `zeta` candidates from it. Next design an isolated local identification capture with the same physical gates, or keep any K/B grid as an independent offline candidate list. Then continue to TASE-QP and SFC+TASE with tangential path parameters frozen.

## Decision tree and evidence index

- [Binary decision tree](tase_5n_precision_decision_tree_20260925.md)
- [Raw evidence index](tase_5n_precision_evidence_index_20260925.json)

## Review notes — 09:47 checkpoint (historical)

Use this section for comments on the evidence, decision branches or the next candidate. This report records two eligible A baselines and the successful RPSFC canary; it does not claim A repeatability, an integral effect, a TASE-QP result, or a causal early-brake improvement.

## Continuation update — 09:30 HKT

### First randomized integral pair: B then A

The first resident pair was randomized to run B (`0.5 N·s`, legacy clamp) and then A (`1.0 N·s`), with all other parameters frozen at `Md=9.565272137974492`, `Bd=693.6559295653944`.

| Attempt | Observed result | Classification |
|---|---|---|
| B, attempt 0001 | Full 60 s Figure-eight PATH, 550 formal bins, MAE **1.272056 N**; each measured rate layer cleared 460 Hz, with the minimum reported layer at **465.783 Hz**; automatic joint Home verified with maximum joint error `5.01e-5 rad`. | One eligible B observation; no comparison conclusion from one run. |
| A, attempt 0002 | The trace reached `formal_time_s=60.002 s` and records 29,954 formal command rows, but TP stopped with `reason=58 return_guard=0`. The lifecycle did not certify `path_complete`, Home, or readiness; no formal score was emitted. | Failed A attempt. Preserve in the failure denominator; do not count as a complete pair or infer its MAE from the incomplete lifecycle receipt. |

### Recovery status and live gate

The failed A attempt remains failed. Home-first was correctly rejected because the measured TCP was below the clearance-entry floor. The first staged-recovery invocation then failed before geometry construction because the launcher omitted `AMENT_PREFIX_PATH`; `home_motion_dispatched=false`, and its RTDE file contains observer samples only with zero wrench rows. This was a software-environment failure, not a robot command.

After setting the ROS environment correctly, a new read-only preflight confirmed Remote Control, Safety NORMAL, Dashboard STOPPED, the short EOAT (`payload=0.413 kg`, `CoG=[0.0011,0.0031,0.0163] m`, `TCP z=87.4 mm`), Kunwei reachability, and no competing writer. A fresh camera frame was available, but the continuous video recorder did not advance; the frame is only a visual cross-check.

The corrected single recovery owner retried Home-first and then entered staged-relief admission. It stopped before any relief/Home motion because the exact existing stationarity predicate failed. A 5.01 s RTDE-only observation contained 502 frames; only 87 (17.3%) met `norm(actual_TCP_speed)<0.0005` and `max(abs(actual_qd))<0.001`, and the longest consecutive pass was 9.5 ms. Over the same interval, TCP XYZ spans were about **0.11–0.14 mm** and joint-position spans **0.14–0.19 mrad**. The RTDE report therefore shows small, fluctuating motion/velocity, not a stationary window. The sample recorded by the recovery owner had Safety bits `2049` (NORMAL), but that does not override the stationarity gate.

At the last recovery sample, TCP was `[0.467325,0.166710,0.020307] m`, about **13.8 mm below** the approved Home Z. The recovery owner dispatched no motion. The observed qd fluctuation's cause is unknown; position-difference estimates are poorly correlated with the controller's reported qd and are too noisy to label it either true mechanical oscillation or RTDE quantization. No velocity threshold was relaxed. One earlier recovery receipt refreshed a stale software baseline while at this low pose and mislabeled it a “Home-window” no-contact sample. That reading is not accepted as independent no-contact proof and must not be reused as such.

The active recovery branch is therefore: keep the original A failure, leave the robot STOPPED, and resume the same staged recovery owner only after a fresh physical/RTDE stationarity gate passes and the force reference is valid for the observed pose. Do not start another TASE attempt while the robot is outside the approved joint Home. The low pose, video limitation, recovery samples, and both no-motion receipts are indexed in the evidence file.

This event does not change the separate RPSFC result: the 100 mN short-EOAT no-contact canary completed 20 s and verified return to its Autotuner joint Home. It also does not turn B's single success into an integral-policy effect. The two earlier eligible A baselines remain the only complete A units; the first B is a standalone eligible B unit, and this randomized pair is incomplete.

### TP reason-58 source localization

Fresh GET read-back of the installed `step5d_contact_six_qp_v1` script identifies the active implementation as R006. The last synchronized TP echo after the 60.002 s host formal window was `state=40`, `reason=0`, `return_guard=0`; the last recorded PATH RTDE frame still had maximum joint speed about **0.00902 rad/s** and TCP linear speed about **4.50 mm/s**. In the fetched-back script, the R012 end acknowledgement exits the PATH loop, then `stopj` is followed by `codex_r006_stationary(0.25 s)`. That function retains the existing 0.5 mm/s linear and 5 mrad/s angular thresholds and allows at most 1.25 s to collect the 250 ms dwell. If it fails, reason 58 is raised before any return guard bit is set.

The TP fault handler then calls `codex_r006_recover_home_after_fault`, which enters the same bounded return function. That route also requires a 250 ms stationary interval before its first vertical motion. The receipt's `reason=58, return_guard=0` is consistent with that retry failing its start gate, leaving the arm at the PATH endpoint. The fresh host recovery followed the same safety decision: direct joint Home was outside its clearance floor; staged relief was withheld because its own current RTDE sample failed the existing stationary gate. No motion was dispatched by either host recovery attempt. This localizes the stop condition to **post-PATH settling / return admission**; the persistent low-amplitude post-stop fluctuation still lacks a confirmed physical or sensor cause.

The installed TP's component-wise stationary test and the host recovery helper's stricter combined TCP-speed/joint-speed test are not identical. Re-evaluating the same 5.01 s / 502-frame observer capture against the TP's component-wise speed thresholds, 171 frames pass but the longest consecutive interval is only 30.1 ms—well below its required 250 ms dwell. This 100 Hz observer is not a substitute for the TP's 500 Hz predicate, but it supplies no evidence that the full dwell is available. The live data do not justify lowering either gate. Resolve the mismatch offline and validate the exact recovery branch before another live attempt; do not treat a one-frame threshold crossing as a stable interval.

### Failed A force metrics, clearly marked diagnostic

The sealed command trace contains all **29,954** host rows through a 59.998 s reference span and all **550** nominal 100 ms `[5,60)` bins can be reconstructed from unique TP-consumed state-25 rows. The B reconstruction differs from B's official 1.272056 N score by only 0.000124 N, which is a useful method cross-check. Applying that same reconstruction to the failed A gives filtered-feedback bin-mean MAE **0.989 N** on `[5,60)`, **1.014 N** on `[0,60)`, and **1.286 N** on `[0,5)`. These are not official attempt scores because the lifecycle never certified `path_complete` or Home; they remain diagnostic and cannot be used as successful BO or paired-confirmation observations.

A separate raw corrected KWR75 sample calculation gives `[5,60)` MAE/RMSE **1.908/2.515 N**, with **17.0%** of samples below 1 N; `[0,60)` MAE/RMSE are **1.964/2.558 N**. The difference from the filtered bin-mean metric reflects both filtering and sample weighting. KWR75 remains the feedback signal, not independent task-force truth. State-20 search lasted 9.45 s and State-21 ramp/release 9.99 s; their constant-5-N sample MAEs are descriptive only because the target is not held at 5 N throughout those stages. The stage and path windows are reproducible from [the failed-A diagnostic receipt](tase_integral_pair01_a_failure_diagnostic_20260925.json).

### Partial-attempt reducer correction

The existing read-only evidence reducer had returned zero raw force samples for this A attempt because the failed lifecycle receipt omitted `path_started_monotonic_s`, even though its sealed v2 command timeline contains the formal PATH reference clock. The reducer now reconstructs the missing origin only from sealed rows marked `reference_phase=path` using `host_monotonic_s-reference_time_s`. It records that source and leaves `path_complete=false`, `home_verified=false`, and the official MAE null. The focused reducer suite passes **2/2**; its output reproduces the raw KWR75 diagnostics above. This repairs reporting, not the TP return failure or hardware state.

## Current R31 rate460 and TASE-QP checkpoint — 16:18 HKT

The earlier morning recovery note above is historical. The robot was subsequently recovered to its approved joint Home. At the latest R31 checks it was powered (`Robotmode: RUNNING`), Safety NORMAL, stopped at the bound joint Home, with the short EOAT `0.413 kg / [0.0011,0.0031,0.0163] m / TCP z=87.4 mm`. No writer/bridge remains after each sealed attempt.

Two fresh rate460 TASE-RNN-MATURE A units completed the 60 s PATH and 550 formal bins, passed timing/motion/Home/STOP gates, and scored **1.282720 N** and **1.006797 N** formal bin-mean MAE. With only two observations, this is descriptive and not a stable estimate. Run 2 used the already approved evidence-only video fallback after a required-video preflight recorder stall; its main run video did record.

Two TASE-QP paths also completed, but both failed the unchanged motion gate requiring published-versus-consumed-RTDE `actual_qd` correlation >=0.900. The frozen BO-10/B profile attempt had correlation **0.840132** and diagnostic-only `[5,60)` MAE **0.914710 N**. The matched attempt used the same `Md=9.565272137974492`, `Bd=693.6559295653944`, and `1.0 N·s` integral as A, changing only to the frozen QP solver; it had correlation **0.848607** and diagnostic-only MAE **0.950139 N**. Both passed 60 s/550-bin completion, >=460 Hz timing, safety, return, joint Home, and program STOP. Both remain failures for method eligibility; neither MAE is a BO or formal controller score.

The matched trace recomputation verified the failure on the exact TP-consumed sequence (0.848627); a one-packet shift reached only 0.852176. Joints 2, 4, and 5 are the weak correlations. QP calls themselves stayed well below the 1 ms deadline (maximum 0.1001 ms; zero failures). An offline 30,000-solve probe with live-writer GC behavior recorded a maximum 0.0460 ms and no deadline overruns; it did not reproduce or explain the historical 106.1 ms event. Keep both explanations open: the measured QP motion-gate miss is real, while the older isolated solver outlier remains unattributed.

The second matched attempt initially hit an early identity rejection because the host owner only allowed BO-10/B. The software now recognizes exactly that frozen profile and the separately identified R31-A matched profile; it rejects any other identity or altered numeric values before Dashboard Load/Play. The existing BO-10 receipt is preserved, and the QP library, 0.05 rad/s bound, 1 ms deadline, and validation tolerances were not changed. The two relevant focused test modules pass **83/83**.

The detailed trace and per-joint analysis are in [the R31 TASE-QP motion-gate comparison](tase_r31_qp_motion_gate_comparison_20260925.md). SFC+TASE remains the next research branch after offline command/actual-velocity diagnosis and composition replay; these TASE-QP failures do not establish an SFC result.

## Current continuation — 16:54 HKT

- The user requested power-on. Fresh read-only preflight showed the UR10e was already powered (`Robotmode: RUNNING`), Remote Control, Safety NORMAL, TP STOPPED, and at the approved joint Home within `2.9e-5 rad`. No extra Dashboard power command was sent. Kunwei was connect-only reachable; there is no current writer or bridge. The check was one RTDE snapshot, not a sustained stationarity test.
- The R31-aligned SFC/DSFC model proxy is now sealed at 30/30 attempts, five matched blocks per scenario, zero failed simulations. Force-MAE differences favored SFC in the two pulse scenarios inside this proxy, while path RMS had mixed outcomes. It is not physical evidence and does not qualify a live combined method. See [the R31/QP and SFC/DSFC report](tase_r31_qp_motion_gate_comparison_20260925.md) and `runs/tase-sfc-dsfc-r31-aligned-model-20260925T1631HKT/comparison/summary.json`.
- The prior QP conclusion is unchanged: both complete live paths failed the frozen `actual_qd` correlation gate (`0.8401` and `0.8486` versus `0.900`). Do not repeat the same profile unchanged. The R31-aligned SFC composition still uses a non-live-approved `120 N/m` proxy parameter and no live registry entry exists.
- Next: diagnose QP command-to-actual velocity mismatch offline and close a reviewed, live-eligible SFC+TASE parameter/registry seam before any combined live run. Keep QP failures and all simulator claims separate.

## QP qd follow-up — 2026-09-25T17:01

Extended same-sequence offset scan (−24 to +24 packets) peaked at QP correlation `0.852176` at a noncausal future-command offset; it does not support simple delay correction. QP J2/J4 setpoint changes and output-feedback limit flags are higher than RNN A. Treat abrupt command change as a candidate mechanism only. The current output-gate miss remains unresolved; no new live run or gate change was made. Details: `reports/tase_r31_qdot_offset_scan_20260925.json` and `reports/tase_r31_qdot_output_transition_20260925.json`.

## QP output-event comparison — 2026-09-25T17:07:30+08:00

The event-conditioned qd comparison against both RNN A traces shows a solver-specific interaction: RNN remains highly correlated on output-flagged rows, whereas matched QP does not. This narrows the next offline audit to command/`Jqdot` changes and active constraints at those timestamps. It does not prove cause or qualify a QP fix. No live motion was attempted.
