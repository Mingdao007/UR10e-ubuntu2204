# UR10e Runtime / Step5d V3 Design Gate A

Date: 2026-07-19 HKT  
Status: candidate contract for independent Sol `xhigh` review  
Scope: offline/pre-live only; no bridge, controller write, TP upload/Play, ARM, contact, calibration, or motion.

## 1. Frozen ownership

| Owner | Sole authority | Explicit non-ownership |
|---|---|---|
| Shared runtime | experiment/campaign/batch/run identity; canonical state machines; dispatch intent/receipt truth; exact ACK command/consumption truth; resume; locks; outcome; evidence finalization; promotion | trajectory math, controller math, register mapping, real-time safety decisions |
| `StageAutotuneAdapter` | logical Step identity; TP program/source identity; Stage22/25; trajectory/progress/reference; physical prior; return policy; TP/bridge/register mapping | batch truth, optimizer gate, evidence identity, safety truth |
| Shared `SafetyEnvelope` | moving-sphere invariant; certified stopping-bound inputs; fingerprint; reason codes; allocation-free tick kernel | transport, file I/O, campaign state, TP motion primitives |
| 500 Hz bridge seam | capture authoritative tick inputs; call frozen kernel; send existing exact-stop packet in the same tick | schema, safety policy, lifecycle truth, evidence publication |
| `EvidenceSink` | immutable bundle; persist runtime-validated dispatch/ACK/closure receipts; publish one `TrialBrief` only after safe closure | ACK validation, optimizer decisions, motion, 500 Hz logging |
| TP package | idempotently consume adapter-mapped dispatch/ACK tokens; execute frozen motion primitives; echo consumption and report closure | batch/ACK validation truth, optimizer eligibility, bundle identity |

There is one lifecycle truth and one safety truth. The adapter is the only numeric mailbox/register mapping layer. No bridge-, launcher-, TP-, or CLI-local shadow lifecycle is permitted.

## 2. Public API and typed identities

The sole public executable remains `ur-exp` with `validate`, `plan`, `run`, `campaign start`, `campaign resume`, and `status`. `main()` may parse, validate, and delegate only. CLI flags cannot override trajectory, force, frame, controller, or safety semantics.

`StageIdentity` separates fields that legacy artifacts currently conflate:

- `logical_stage_id = step5d_strict_rnn_autotune_v1`;
- `tp_program_id = step5d_strict_rnn_autotune_v3`;
- `source_stage_id = step5d_strict_rnn_ablation_v35`;
- `adapter_id = step5d_strict_rnn_autotune_adapter_v1`.

All four are typed and fingerprinted. Equality is field-wise; a current pointer or matching suffix is never evidence.

Return targets are disjoint types:

- `NearReadyReference(batch_uid, next_row_uid, pose, tolerances)`;
- `CampaignHomeReference(batch_uid, pose, tolerances)`.

They have no common implicit-counter constructor. Row 1–9 require the first; row 10 requires the second.

## 3. Canonical identity and persistence

Canonical JSON uses RFC 8785 JSON Canonicalization Scheme (JCS) over I-JSON inputs. Schemas reject duplicate keys, non-finite numbers, integers outside the interoperable range, and values whose declared type differs. JCS fixes UTF-8 bytes, property ordering, escape rules, ECMAScript number serialization, and `-0` serialization. Strings are preserved without Unicode normalization; schemas forbid canonically equivalent identifiers with different code-point sequences. Cross-runtime golden fixtures include exponent boundaries, `-0`, Unicode, nested ordering, and rejection cases.

SHA-256 identity includes all semantic content. Only explicitly named observation fields (`recorded_at`, PID, hostname, and absolute output path) are excluded by schema. Validity-bearing time fields such as authorization `not_before`/`not_after`, source freshness limits, controller timebase, and evidence epochs remain typed identity inputs. `current` and `latest` never satisfy a binding.

`ExperimentSpec` fingerprints typed stage identity; component IDs/versions; trajectory, frame, objective, parameter, controller, safety, git/config/model/TP bindings; `ur10e_concurrency_contract_v1` plus its source digest; timing contract; lanes/resources; and warm-start lineage.

`BatchIdentity` additionally binds:

- exact ordered set of 10 `(row_uid, control_candidate_uid, normalized_overlay)` rows;
- campaign UID, plant epoch, launch fingerprint, adapter fingerprint;
- physical-prior fingerprint, safety-envelope fingerprint, return-policy fingerprint;
- TP/controller readback fingerprint and authorization reference identity.

`RunManifest` binds exact candidate and actual normalized overlay, authorization, pre/post fingerprints, controller readback, outcome, optimizer eligibility, artifacts, ACK, and closure.

Persistence rules:

- output directories use exclusive creation;
- `run_state.jsonl` is append-only, sequence-numbered, hash-chained, and fsync-bound at state boundaries;
- terminal manifest and `BatchResult` are write-once via exclusive creation;
- mutable pointers are conveniences only and never satisfy resume or evidence gates;
- resume reconstructs state from canonical typed events and verifies the chain before taking any action.

External effects use write-ahead intent. The runtime fsyncs a typed intent before calling the adapter, supplies an idempotency token derived from that intent, and accepts only a durable receipt that echoes the exact token and semantic bindings. On restart, an intent without a receipt is reconciled against authoritative TP/controller readback; missing or ambiguous readback fails closed and never causes blind redispatch.

## 4. Batch lifecycle and crash cuts

Per-row fate is exactly `unattempted | attempted_incomplete | ack_completed`.

`ack_completed` requires all three facts for the same batch/row/bundle identity: immutable bundle persisted, exact ACK consumed, and post-ACK safe closure verified. Bundle-written alone is never completion.

Canonical row states are:

`UNATTEMPTED -> DISPATCH_INTENT_DURABLE -> DISPATCH_RECEIPT_DURABLE -> ACTIVE -> RETURN_TARGET_VERIFIED -> BUNDLE_FROZEN -> WAIT_ACK -> ACK_INTENT_DURABLE -> ACK_RECEIPT_DURABLE -> POST_ACK_CLOSURE_VERIFIED -> ACK_COMPLETED -> TRIAL_BRIEF_PUBLISHED`.

`DispatchIntent` binds `(batch_uid, row_uid, control_candidate_uid, normalized_overlay_sha256, dispatch_uid)`. The adapter maps it to registers/mailbox without changing identity; the TP echoes `dispatch_uid`. `DispatchReceipt` binds the same fields plus the exact TP/controller readback fingerprint.

After the guarded return motion, `RETURN_TARGET_VERIFIED` binds the typed `NearReadyReference` for rows 1–9 or `CampaignHomeReference` for row 10, pose/stillness/guard evidence, and `closure_phase=pre_ack_return`. The immutable bundle is then frozen and the runtime produces `AckCommand(batch_uid, row_uid, control_candidate_uid, bundle_sha256, return_receipt_uid, ack_uid)`. It fsyncs `ACK_INTENT_DURABLE` before adapter send. TP consumption echoes `ack_uid`; the adapter creates an `AckConsumptionReceipt` with all command fields and controller readback fingerprint. The runtime alone validates it and fsyncs `ACK_RECEIPT_DURABLE`.

`POST_ACK_CLOSURE_VERIFIED` is a no-new-motion verification that the TCP remains at the already reached typed return target, the TP is still/stopped as required, and every route/safety guard remains valid. Only then may the runtime append `ACK_COMPLETED`; `TrialBrief` publication follows and is idempotent. For row 10, `BatchResult` follows the same row's `TRIAL_BRIEF_PUBLISHED`. Thus final-home motion is pre-ACK, while final-home closure evidence is post-ACK.

Any pre-terminal restart reduces to either `unattempted` or `attempted_incomplete`; only `ACK_COMPLETED` is skipped on resume.

| Crash cut | Durable fact | Resume action |
|---|---|---|
| before dispatch journal | none | dispatch row |
| after dispatch intent, before receipt | `DISPATCH_INTENT_DURABLE` | reconcile exact idempotency token through TP/controller readback; fail closed if unavailable; never blind redispatch |
| after dispatch receipt, before motion evidence | `DISPATCH_RECEIPT_DURABLE` | resume observation of the same attempt; never dispatch again |
| during active trial | `ACTIVE` | preserve partial evidence, classify infrastructure/operator/safety outcome, require safe closure before retry |
| after return target, before bundle | `RETURN_TARGET_VERIFIED` | verify receipt, freeze the same bundle; no new return motion |
| after bundle write, before ACK intent | `BUNDLE_FROZEN`/`WAIT_ACK` | verify immutable digest, fsync exact ACK intent; do not rerun candidate |
| after ACK intent, before receipt | `ACK_INTENT_DURABLE` | reconcile `ack_uid`; do not send a different ACK |
| after ACK receipt, before post-ACK closure | `ACK_RECEIPT_DURABLE` | perform no-motion typed target/stillness/guard verification |
| after post-ACK closure, before completion | `POST_ACK_CLOSURE_VERIFIED` | append `ACK_COMPLETED` once |
| after `ACK_COMPLETED`, before brief | terminal fate | publish the uniquely bound `TrialBrief` once; never optimize directly from row state |
| after row 10 brief, before `BatchResult` | all rows terminal plus home closure | write durable `BatchResult` once |
| after `BatchResult`, before process exit | terminal batch | return exit 0 without work |

Runner/launcher returns 0 only after row 10 exact ACK receipt, post-ACK final-home closure verification, row 10 brief, and durable `BatchResult`. An observed bridge exit code 0 after these facts is success, not “exited before completion”.

## 5. Step5d V3 physical prior and reset

The frozen prior is:

- base-frame `reaction_normal_base`, unitless unit vector, `[-0.043955267, 0.020079909, 0.998831683]`;
- base-frame `approach_normal_base = -reaction_normal_base`, unitless unit vector, `[0.043955267, -0.020079909, -0.998831683]`;
- `base_T_tcp` axis-angle rotvec in radians `[3.120752062, 0.0, 0.068626833]`, whose TCP `+Z` maps to `approach_normal_base`;
- base-frame precontact XYZ in metres `[0.487834547, 0.129337053, 0.022863519]`.

TP fields map `base_T_tcp` to the precontact pose. Bridge load projection maps only `reaction_normal_base`; posture/press mapping uses only `approach_normal_base`; bridge initial-normal blending is explicitly reaction-normal blending. The TP pose and both bridge semantic fields present the same full prior fingerprint. Static old-path scan, synthetic/replay sign and rotvec closure, angular error-reduction, and runtime semantic/fingerprint mismatch gates all block before dispatch.

Each trial resets integral, outer-loop, normal, filter, and rate-limit state. The first loaded tick cannot relatch. Live-normal blending becomes eligible only after measured load is at least 8 N continuously for 0.10 s; loss before 0.10 s resets the dwell. Blend remains limited to 0.05 rad/s. `orientation_ko` is feedback overlay only and cannot modify the feedforward prior.

The original trials 13–22 remain immutable diagnostic evidence. Trials 13–20 and 22 have `metric_role=diagnostic_only`; trial 21 has `metric_role=unavailable`. No row is optimizer eligible. The semantic change is `retune_required` and requires a new fingerprint and plant epoch.

## 6. Moving-sphere safety contract

The sphere radius is exactly 0.015 m and is enforced only during active Stage25. V3 exclusively uses the moving sphere; legacy AABB enforcement must be disabled for this lane. Outside Stage25 the kernel returns `SPHERE_INACTIVE` without a stop; an unknown stage returns fail-closed `SPHERE_STAGE_UNKNOWN`.

`ControllerProgress` is typed as `(tick_seq:uint64, controller_mono_ns:uint64, u:float64, phase:ACTIVE|HOLD|DELAY, reference_sha256)`. `u` is finite, monotone, and within `[0,1]`; the adapter evaluates cycloid XY at `u` with per-trial anchor Z in base metres. Every 500 Hz invocation requires the same current `tick_seq`, a non-regressing monotonic clock, and input age `<=2_000_000 ns`. A stale or skipped source tick fails closed. HOLD/DELAY advances `tick_seq` and time but must keep `u` bit-identical, so the center freezes. A reference/progress hash mismatch fails closed.

The kernel receives a caller-owned `ControllerProgress` and writes into a caller-owned fixed-size result. Its tick performs no result/container construction, file I/O, JSON, terminal output, CSV scan, lock acquisition, or dynamic registry lookup; the Python seam is allocation-bounded rather than claiming a language-level zero-allocation proof.

For each tick:

1. compute actual distance from TCP to center;
2. compute a conservative full-interval radial bound rather than an endpoint. With `d0=||tcp_base-center_base||`, certified reaction latency `L`, measured TCP speed `v0`, certified worst acceleration growth `a_g`, certified minimum braking deceleration `a_min`, certified center speed/acceleration bounds `v_c/a_c`, let `vL=v0+a_g*L`, `Tstop=vL/a_min`, `tcp_sweep=v0*L+0.5*a_g*L^2+vL^2/(2*a_min)`, `center_sweep=v_c*(L+Tstop)+0.5*a_c*(L+Tstop)^2`, and `predicted_radial_bound=d0+tcp_sweep+center_sweep+numeric_margin`;
3. require both `d0<=0.015` and `predicted_radial_bound<=0.015`;
4. on actual or predicted breach, emit the existing exact-stop packet and deterministic reason code in that same tick, with no dwell.

This triangle/swept bound upper-bounds every relative excursion over reaction plus braking even if the center continues to move. In HOLD/DELAY, certified center bounds are exactly zero. Missing/non-finite input, unknown stage, uncertified bound, stale progress, or hash mismatch returns fail-closed. Reason codes are fixed: `SPHERE_INACTIVE`, `SPHERE_STAGE_UNKNOWN`, `SPHERE_ACTUAL_BREACH`, `SPHERE_PREDICTED_STOP_BREACH`, `SPHERE_INPUT_MISSING`, `SPHERE_INPUT_NONFINITE`, `SPHERE_REFERENCE_MISMATCH`, `SPHERE_PROGRESS_STALE`, and `SPHERE_STOP_BOUND_UNCERTIFIED`.

The stopping-bound artifact binds `L`, `a_g`, `a_min`, `v_c`, `a_c`, numerical margin, evidence digests, metres/seconds units, base frame, equations/version, and validity domain. It requires `L>=0`, `a_g>=0`, `a_min>0`, `v_c>=0`, `a_c>=0`, and margin `>0`. No numeric default is allowed. Until certified evidence exists, live/HIL execution is blocked.

Timing identity binds the source-exact harness digest, `SCHED_FIFO/20`, frozen affinity, 10,000 solver samples, 30,000 full-tick samples, 30,000 safe-hold samples, a 2.0 ms release deadline, zero compute deadline misses, and zero absolute release deadline misses. The sphere kernel is included in the measured full tick; a separate microbenchmark is diagnostic only. Pre/post source fingerprints must match.

Sphere checks supplement, never replace, orientation, force, torque, joint, sensor, heartbeat, contact-loss, or transfer-route guards.

## 7. Return policy

Stage22 is retained for the first row. After rows 1–9, closure targets the typed `NearReadyReference`; after row 10, closure targets `CampaignHomeReference`.

`ReturnRouteV1` is a fingerprinted base-frame `movel` route. Translational `a` is in m/s² and `v` in m/s; rotvec is in radians. URScript `movel` does not expose an independently verifiable angular acceleration/velocity parameter, so this contract does not invent one. Orientation is instead bound by the exact target rotvec and verified after motion together with TCP/joint stillness. The fixed route is:

1. vertical rise to `safe_transfer_z=0.033 m`, `a=0.060`, `v=0.040`;
2. constant-Z translation to precontact XY and prior orientation, `a=0.135`, `v=0.090`;
3. vertical descent to `precontact_z=0.022863519 m`, `a=0.060 m/s²`, `v=0.040 m/s`;
4. pose, stillness, and enumerated guard verification, persist `RETURN_TARGET_VERIFIED`, then enter `WAIT_ACK`.

The return thread continuously guards force, torque, joints, sensor freshness, heartbeat, and route workspace. The active-Stage25 contact-loss invariant must already have closed in the exact terminal transition before return; it is not misrepresented as a condition that contact must remain present during retract. Segment 1 preserves current orientation; segment 2 moves to the typed target orientation; segment 3 holds it. Pose error, orientation error, TCP/joint stillness, all guard facts, and exact TP/controller identity are verified before `WAIT_ACK` and again after ACK without motion. Failure preserves evidence, produces a non-optimizer outcome, and blocks ACK completion. Selection depends on exact batch/row identity, never an implicit counter.

## 8. TrialBrief and optimizer gate

`EvidenceSink` publishes at most one `TrialBrief` after immutable bundle, exact ACK consumption, and post-ACK safe closure. Publication identity is `(batch_uid, row_uid, bundle_sha256, ack_uid, closure_uid)` and uses exclusive creation.

The brief includes actual normalized `trial_overlay`, `control_candidate_uid`, outcome class, artifact digests, and force metric role `trainable_objective | diagnostic_only | unavailable`. An unavailable metric is null, never zero.

`OutcomeClassV1` is a closed enum: `valid_parameter_observation | infrastructure_failure | startup_failure | model_mismatch | safety_stop | observer_gap | operator_stop | code_contract_failure`. `OracleStatusV1` is `credible | failed | unavailable`; `ObserverStatusV1` is `complete | incomplete | unavailable`. Their schemas and policy digests are fingerprinted.

Only a uniquely validated `TrialBrief` with `outcome=valid_parameter_observation`, `metric_role=trainable_objective`, non-null finite objective, `oracle_status=credible`, `observer_status=complete`, exact fingerprint closure, exact ACK receipt, and post-ACK closure may set `optimizer_eligible=true`. The optimizer consumes briefs only through an append-only uniqueness ledger keyed by publication identity and rejects null, diagnostic, unavailable, duplicate, unknown-enum, or incomplete inputs. Every other outcome remains in history with `objective=null` and `optimizer_eligible=false`.

## 9. Failure-to-Guard and debt lane

`ur-exp plan` produces a deterministic `ChangeContract` from touched paths, canonical failure fixtures, invariant registry, and coverage map. It selects `none | replay_required | retune_required` by maximum impact.

The ledger must cover wrong-stage prior, first-tick relatch, overlay mismatch, launcher false failure, metric-role confusion, near-ready/home confusion, trajectory-reference mismatch, and AABB/sphere dual enforcement.

Debt triggers are: first HIL/live failure escaping tests, second canonical occurrence, fourth patch-test-evidence loop, or sampling round 41. An active campaign writes only `FailureEvent`; an independent debt worktree may update tracked governance but cannot acquire live/formal resources or touch the active campaign.

## 9a. Concurrency and timing binding

Every spec binds `ur10e_concurrency_contract_v1` and its digest. Every run persists `parallel_run_manifest.json`, resource-lock acquisition/release receipts, lane/claim class, dependencies, output roots, and pre/post source fingerprints. `live_writer`, `formal_timing`, and visible Gazebo are exclusive; robot runs are serialized; `UR10E_PARALLEL=0` is a tested semantic-equivalent fallback. A debt lane cannot acquire live or formal locks. Promotion rejects missing manifests, lock overlap, source drift, or resource/barrier violations.

## 10. Migration, rollback, and commit boundaries

Source base is fresh `origin/refactor/step5d-autotune-v3-20260718` at `60e87e886a2a6accb95f7ae35f91b7f88ab04d5f`. Old dirty worktrees are read-only audit sources.

Commit boundaries are:

1. schemas, canonical identity, typed registry/resource contracts;
2. CLI dry-plan, journal/store/resume/evidence finalization;
3. Failure Ledger/Invariant Registry/Coverage Map/Change Contract/outcome/debt/extractor;
4. frozen Step5d adapter parity only;
5. exact batch identity/lifecycle/resume/`BatchResult`;
6. physical prior/reset/no-relatch/load-gated blend;
7. shared moving-sphere kernel and bridge seam;
8. typed return route;
9. post-ACK `TrialBrief`.

Rollback is commit-wise and pointer-free: old entrypoints remain intact until parity and offline acceptance. No commit may mix parity with behavior changes. No `reset --hard`, `clean -fd`, forced worktree removal, or mutation of immutable evidence is allowed.

## 11. Gate-A acceptance questions

The independent Sol `xhigh` reviewer must return PASS only if:

- every lifecycle and safety fact has one owner;
- crash cuts cannot duplicate a completed row, optimize an invalid row, or confuse near-ready with final home;
- fingerprints bind all semantics and exclude volatile values;
- prior sign/frame/unit and TP/bridge binding are unambiguous;
- moving-sphere progress, freeze behavior, stopping evidence, exact-stop timing, and AABB exclusivity are fail-closed;
- TrialBrief cannot publish before exact ACK and closure;
- migration and commit boundaries preserve parity and rollback.

Any P0/P1 or unverifiable reviewer provenance blocks implementation.
