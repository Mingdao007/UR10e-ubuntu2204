# TASE + SFC sealed-trace command replay — 2026-09-27

## Result

The frozen native-SFC additive candidate completed a fixed-observation replay over all **29,982 PATH rows** of the eligible rate460 A attempt. Replaying the unchanged mature RNN command reproduced every recorded Jacobian exactly and every final published qdot within **8.7e-19 rad/s** maximum absolute error. This validates the source inputs and local command replay; it does not predict the physical force response to changed commands.

Source attempt: `runs/tase-resident-autotune-A-rate460-20260926T1413Z/confirmation-rate460-e-v1/session-01/attempts/0002/`.
Additive replay receipt: `runs/tase-sfc-add-r32a-command-replay-20260927-02/report.json`.
Additive candidate trace: `runs/tase-sfc-add-r32a-command-replay-20260927-02/candidate_command_trace.jsonl.gz`.
Tangent-replacement replay receipt: `runs/tase-sfc-replace-r32a-command-replay-20260927-01/report.json`.
Source command timeline SHA-256: `89ba3e2024eac35299ac90115a35d9aec5161879e1aec7d6dbd965ad9190f8a7`.

The source attempt is complete under `figure8_window60_r013_rate460_v1`: 550/550 formal bins, 60 s PATH, accepted rate460 evidence, and verified joint Home/ready-for-next. Its receipt reports formal filtered-normal MAE **1.006105 N** and effective data rate **480.587 Hz**. This is Kunwei feedback evidence, not an independent task-force measurement.

## Force metric alignment

The command timeline contains synchronized KWR75 measured-normal and filtered-normal values. Equal-weight 100 ms bins and per-sample errors are separate reductions:

| KWR75 signal and window | Sample MAE | Equal-weight bin MAE | Coverage |
|---|---:|---:|---:|
| Filtered, `[5,60)` | 1.007247 N | 1.005974 N | 550/550 |
| Filtered, `[0,60)` | 1.044617 N | 1.043206 N | 600/600 |
| Measured, `[5,60)` | 1.898693 N | 1.715491 N | 550/550 |
| Measured, `[0,60)` | 1.970712 N | 1.773208 N | 600/600 |

The filtered receipt score differs from the trace reconstruction by 0.000131 N. The exact join/boundary cause is not established, so the receipt remains authoritative and the reconstruction remains diagnostic. For the filtered channel, sample-versus-bin averaging changes MAE only slightly. The larger difference on the measured channel reflects high-frequency sample variation reduced by bin averaging; it must not be described as a controller improvement. The filtered and measured channels also must not be conflated.

## Candidate command response

The candidate identity is `TASE_RNN_MATURE+SFC_TANGENT_AUGMENT_V1`, with frozen parameters from `config/yield_native_route_v1.json`: `m=4`, `mu=393`, `n=3`, `g=0.052126826121414976`. It transforms baseline-corrected KWR75 force to base coordinates, projects SFC output onto the TASE normal's tangent plane, applies existing task-space velocity caps, and calls the existing calibrated TASE RNN realization once.

- Mean projected tangent force: **0.587 N**.
- Mean requested/applied SFC tangent speed: **6.427 mm/s**; maximum **9.453 mm/s**. Existing tangent/total caps scaled zero rows.
- Candidate realized qdot stayed within the recorded joint box; maximum absolute joint speed was **0.01270 rad/s**. Five rows were host-slew limited.
- Candidate mean absolute change in realized normal velocity was **0.0140 mm/s**; maximum was **0.9174 mm/s**. The candidate's force-direction retry decision differed from the source on one row.

These are command-path measurements on baseline observations. The KWR75 force sequence, pose, and Jacobian remain the baseline trace after candidate commands change, so this replay produces **no candidate MAE** and cannot support a force-accuracy claim. Candidate live frequency is also unmeasured.

## Identity and internal-force correction

The selected R32 A attempt's `robot_frames.jsonl` has no `actual_TCP_force` field; its `raw_sensor.jsonl` records the external Kunwei KWR75 stream, and its command timeline contains the corrected Kunwei wrench. The current `tools/preflight_readonly.py` `RTDE_FIELDS` likewise excludes `actual_TCP_force`. A separate legacy Step6 no-contact canary explicitly includes that internal field in its trace, and a historical preflight report records a 1.062 N RTDE TCP-force diagnostic. That diagnostic is not Kunwei evidence and must not enter TASE control, score, or force protections. The exact historical preflight callsite is not proven by the current source snapshot; earlier attribution to the V3 generic field list was too specific and is withdrawn.

## Live decision and next step

Two explicit command semantics were replayed against the same complete source trace:

| Composition | Mean cosine: realized candidate tangent vs reference tangent | Positive-direction rows | Mean absolute normal-command delta vs A | Retry decision differences vs A |
|---|---:|---:|---:|---:|
| Add SFC to TASE tangent path | −0.7204 | 758/29,982 (2.53%) | 0.0140 mm/s | 1 |
| Replace TASE tangent path with SFC | −0.9034 | 0/29,982 | 0.0189 mm/s | 12 |

The SFC tangent output itself had mean direction cosine **−0.9041** against the Figure-eight tangent reference, pointing opposite on 99.99% of rows. Both candidate variants remain within the recorded qdot bounds in this command replay, but their commanded tangential direction is a clear path-following concern. The additive variant also averages 6.43 mm/s SFC velocity and peaks at 9.45 mm/s. These facts do not establish actual TCP motion or contact force response; state and force were held to the baseline observations.

On the same recorded inputs, the projected KWR75 tangential force had mean cosine **−0.7370** against the reference tangent and opposed it on **27,528/29,982 rows (91.8%)**. The native SFC output aligned with the projected force on **92.0%** of rows (mean cosine **+0.7811**). This supports a specific observed chain: the baseline KWR75 tangential signal usually points against the commanded path, and native SFC usually produces velocity along that signal. It does **not** identify whether the force is path/friction reaction, residual bias, or an external disturbance; the feedback signal cannot separate those causes in this trace. A sign flip or dead zone is therefore not justified by this replay.

The same force/reference-direction audit across all five eligible rate460 A confirmation attempts found a consistent pattern: mean attempt cosine ranged **−0.7001 to −0.7414**, and negative-direction fractions ranged **89.45% to 91.96%**. The five attempts contain **149,541 PATH rows** in total. Machine-readable audit: `reports/tase_r32a_kunwei_tangent_path_direction_audit_20260927.json`; reproducible entry point: `tools/audit_tase_kunwei_tangent_path_direction.py`.

The additive candidate remains unavailable in `config/yield_live_entry_v1.json`. The existing offline `TaseSfcFusion` identity discards the TASE tangent request and replaces it with SFC tangent output; its model comparison also includes an unapproved 120 N/m path-restoring term. These are distinct algorithms, and the no-spring replacement replay points opposite the path. Neither identity nor any new path-restoring gain is admitted for live use by this report.

Next, investigate how to distinguish path/friction reaction from a genuine external tangential disturbance using existing independent evidence or an approved offline model. Then evaluate a path-referenced, bounded SFC correction without borrowing the offline 120 N/m value. Keep each formulation explicitly named; resolve path following and measure per-stage compute timing before any live promotion. No robot or sensor write command was sent during these replays.
