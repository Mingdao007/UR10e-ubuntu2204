# Step5d TacDiffusion Direct Torque fixture-shadow flow

This route keeps the frozen Step5d v3 approach, contact search, preload, 12 N
outer loop, and 60 s cycloid. Stage25 uses the PolyScope 5.26 Direct Torque
backend with fixed K/D/Dq. TacDiffusion is a deterministic 50 Hz fixture
shadow only; it has no command or fallback authority and `model_active=false`.

This fixture-shadow route is not the expert-data collection route. Its TP
package requires zero feed-forward wrench, and the bridge does not persist the
84D observation plus 12D expert-action episode record required by the
TacDiffusion dataset. Starting this bridge alone therefore does not mean data
collection has started.

## Frozen offline TacDiffusion recorder plan

The offline implementation adds independently testable primitives without
changing the Direct Torque law or its controller thread. `signals.py` retains
the exact 84D layout and adapts the causal 1 kHz-to-500 Hz join: device time is
an ordering clock, host-visible time is the causal-availability clock, one TCP
batch has one arrival time, and a selected sample is explicitly held when no
new batch has arrived. The default host-batch watchdog is 80 ms. The internal
wrench field is reserved for the existing previous-applied no-gravity
torque/Jacobian/dynamics reconstruction; it is never UR F/T.

New writes use `episode_recorder.py` frame v2 and
`durability_mode=batch_fsync_10`: a bounded 8192-row non-overwriting spool,
producer-side enqueue only, background JSONL batches of ten, one flush/fsync
per batch, atomic manifest creation, and a maximum unsealed tail of nine.
Overflow, stall, writer error, and invalid/torn rows are latched and retained;
the sealer emits an atomic `recorder_health.json` receipt alongside the
manifest.
`TaskExecutor` polls that latch at task cadence and routes a fault through the
existing ordered safe exit; recorder health is not read by the 500 Hz Direct
Torque controller loop.

`eligibility.py` is the sole final training-eligibility verdict and receipt
writer. The first live shadow is forced `training_eligible=false`; future
eligibility additionally requires strict control time, exact expert/applied
actions, coherent complete echoes, causal and valid sensor lineage, valid
internal-wrench reconstruction, no spool overflow/stall/drop, and complete
tamper-free sealing. The optional sidecar in
`run_tacdiffusion_remote_direct_torque_v4.py` is therefore diagnostic-only and
reports `recorder_live_ready=false` and `training_eligible=false` until those
independent gates have evidence.

The existing output float-register bank 24--47 remains the only bank: host
command labels after any host guard are the `applied_action`; the latest atomic
12D controller-filtered value is retained separately as `echoed_action` and may
differ during a filter transient. Generation-incoherent echoes are retained
with an invalid-row flag. No second float bank is introduced.

Hash-bound no-motion references retain the 2 s `anchor_circle` plumbing
diagnostic and add `line_out_and_back_4s` (0 -> +0.5 mm -> -0.5 mm -> 0) and
`full_circle_radius_0_5mm_8s`, both smoothstep and fixed orientation. The hard
tube rejects a point 1 um outside its safe boundary. Empty and disabled guard
policies are algebraic identity. These references are offline artifacts only;
they do not enable motion.

Outstanding live gates remain separate: current controller/route identity,
Safety NORMAL, one live writer, route-bound bridge readiness, fresh authorized
live canary evidence, a valid Kunwei causal batch lineage, a valid reconstructed
internal wrench, complete sealed artifacts, and the applicable stopping/return
and contact authorization gates. No current pointer, live readiness state, or
training-eligible view is promoted by this offline tranche.

## Current state

- Local `.script/.txt/.urp` package generated and exact cachedContents checked.
- The exact triplet is uploaded to the controller and a fresh controller
  readback matches the local hashes.
- The unchanged KWR75B mounting, fresh read-only controller TCP offset, vendor
  drawing, and retained gravity-axis sweep now bind the sensor-to-active-TCP
  rotation and `90.6 mm` lever arm in a SHA-verified calibration artifact.
- An isolated official 5.25.2 URSim compatibility check parsed the PolyScope
  Script-node contents, entered Stage20 waiting for the bridge, and observed no
  joint or TCP motion. This is compatibility evidence, not 5.26 live acceptance.
- `step5d-tacdiffusion-shadow.sh status` is safe and read-only.
- Bridge `start` remains fail-closed until controller 5.26 proof, the applicable
  formal review gate, and a fresh tranche-specific authorization are all
  hash-bound. Package readback and sensor-frame calibration are verified.
- No bridge, load, Play, sensor stream command, URScript send to the physical
  controller, or robot motion has been performed by this implementation tranche.
  The only controller mutation was the scoped TP triplet upload plus fresh
  readback.

## Unknown-surface and controller-simulation boundaries

- The physical curved surface remains unknown to the controller. The live
  reference is nominal in-plane path plus target load; it must not inject the
  registered v11 CAD height or CAD local normals.
- The PolyScope Simulation button may run the exact 5.26 TP program without
  moving the robot. A hash-bound Simulation Mode trace can verify parser/API
  availability, RTDE register roundtrip, Stage25 entry, and controlled stop.
  This is controller-runtime no-motion evidence, not physical torque/contact
  acceptance.
- The complete `controller_verified` claim still separately needs controller
  and robot identity plus installation, safety, TCP/payload, URCap,
  calibration, and API evidence.

## Operator sequence after all gates pass

1. Open `/programs/andyl/kunwei/step5/step5d_tacdiffusion_direct_torque_fixture_shadow_v1.urp` on the TP. Do not press Play yet.
2. Run `step5d-tacdiffusion-shadow.sh start --live --allow-kunwei-stream-command --write-rtde-inputs --authorization <fresh-artifact.json>`.
3. The bridge collects exactly 1000 software baseline samples and waits. It does not use hardware tare or `zero_ftsensor`.
4. The user presses Play once. The bridge sends legacy precontact fields, then arms Direct Torque only after TP Stage25 reports WAITING while the program is PLAYING.
5. Only normal 60 s completion may use the frozen guarded retract/home route. Packet, sensor, runtime, or safety faults exit torque and latch without auto-home.

No-contact acceptance is 2 s, then 10 s, then 60 s with a fresh authorization
for each tranche and deterministic `0+0` validation. One final frozen-
fingerprint `2+1` review is deferred until immediately before expert-data
collection contact, which also requires fresh contact authorization.

## Inactive native remote Direct Torque v4 candidate

The v4 candidate is separate from the frozen fixture-shadow route and remains
inactive, blocked, non-current, and no-contact. A pre-refactor 100 ms hold
completed on the real robot with Direct Torque and COMPLETE observed, 500 Hz
RTDE output evidence, and Kunwei raw capture around 1 kHz. That run is retained
as historical evidence only because the runtime source fingerprint changes in
the transport-reuse refactor.

Remote Control changes only how the controller-resident program is started.
After the Secondary Client send and lease/episode handshake, the runtime now
reuses the Step5d v34 RTDE framing, packet drain, and absolute-deadline
no-burst behavior with the v35 `SCHED_OTHER` policy. It does not reuse v34's
contact ablation, `speedj` command law, or failed FIFO/20 scheduler.
Authorization v2 also binds the complete host runtime fingerprint (runner,
v34 transport primitives, native Kunwei parser, torque contract, calibration
validator, writer lease, and controller status helpers); a prior-stage
certificate from a different runtime is rejected.

Kunwei remains a native 1 kHz sensor stream, while `80 ms` is only the maximum
allowed host TCP receive-batch delivery gap. Every raw row records sample
index, receive-batch identity, one batch-arrival timestamp, and nominal
`sample_index/1000` time. TCP batching means these captures are not valid
1 ms causal robot/force alignment and remain `training_dataset=false`.

The port-30002 wire source is one outer `def ... end` Secondary Client
program. Inside that transport wrapper, an official-style minimal
`torqueThread()` runs alongside the main state machine. Once torque starts,
the torque thread repeats the
latest shared command through
`direct_torque(..., viscous_scale=..., coulomb_scale=...)` every robot tick,
while the main thread computes the next bounded command and handles packet,
guard, and evidence work;
the receiver first requires 25 consecutive stationary controller ticks
(50 ms), then startup blends from the fresh actual pose for 100 ms. The
no-contact entry canary sets all UR viscous and Coulomb friction scales to
zero. `direct_torque()` continues to provide its documented internal gravity
compensation; the commanded torque therefore contains no gravity term.
The stage trajectory clock begins only after the host first observes the
Direct Torque state, so the stationary dwell cannot shorten the 100 ms hold
or advance a later ramp/reference before torque entry.
The certified tube axes and half-widths remain bundle-bound, but their absolute
center and orientation are rebound on the controller to the first fresh actual
TCP pose of each valid RUN episode. Host preflight, timeline validation, and
per-sample guards use the same episode-entry rebase. The historical reference
anchor therefore defines the validated relative path shape, not a requirement
to return the robot to a stale absolute pose before a no-contact canary.
Every exit converges to one `stopj(10.0)` site. There is no deliberately
zero-vector startup or exit command; the computed non-gravity torque is
expected to be zero at exact equilibrium and zero velocity.

The zero-friction policy is a root-cause isolation change, not a `Δtau`
workaround. In the failed 2026-07-26 hold, the first echoed six-axis custom
torque was exactly zero while derived joint acceleration reached
`10.8513 rad/s²` within the first 20 ms. The replay artifact is
`runs/tacdiffusion/direct_torque_v4_v34reuse_live_20260726T1859HKT/hold_100ms/entry_bumplessness_analysis_v1.json`.
This proves that the initial motion preceded any nonzero custom impedance
torque, while the next zero-friction canary remains necessary to distinguish
friction/stiction injection from other controller-internal mode-transition
effects. A separate `5 rad/s²` guard, derived from consecutive 500 Hz
`actual_qd` samples, reports fault 12; it is defense-in-depth and is not
claimed as the root fix.

The subsequent zero-friction live hold still failed with zero custom torque
preceding motion, while Kunwei remained near zero load. Removing the explicit
`sync()` after each `direct_torque()` call reduced maximum derived acceleration
from `20.09` to `13.72 rad/s²`, but did not eliminate the failure. The receiver
therefore now follows UR's official thread separation: the torque thread
contains no `sync()` or packet/control computation, and the main thread may
`sync()` concurrently after publishing its next bounded shared command. This
threaded fingerprint is controller-validated. The first structurally correct
live attempt then exposed a separate axis-angle branch-cut bug: linearly
blending equivalent `+pi` and `-pi` rotation-vector representations produced
a raw `6.283 rad` orientation excursion and grew the commanded torque to
`5.66 Nm`.

The no-contact stages have no orientation trajectory. Their corrected entry
therefore blends translation only and holds the fresh measured entry
orientation. The resulting 2026-07-26 live hold passed: Direct Torque was
observed for `114.96 ms`, COMPLETE was observed, maximum derived joint
acceleration was `2.714 rad/s²`, the first custom torque was zero, controller
rows were `500 Hz`, and Kunwei captured `999.40 Hz`. This accepts only the
100 ms no-contact hold; it does not authorize contact or claim later stages.

A post-run cadence audit separated the `500 Hz` RTDE output rate from the
controller-law refresh rate. In the accepted hold, commanded torque changed
22 times across 54 active 500 Hz rows, approximately `198 Hz`; ACK advanced
at approximately `94 Hz`. The dedicated torque thread still owns the only
`direct_torque()` call site, but that historical source did not expose an
explicit torque-call counter. The old main loop incorrectly used fixed 2 ms
discretization for its finite-difference acceleration guard, entry
dwell/blend, heartbeat age, and force filter.

The next candidate therefore uses monotonic controller `time()` to measure
each main-loop `control_dt_s`; dwell, blend, heartbeat, and filter evolution
are expressed in elapsed seconds. The controller-side acceleration guard uses
the official encoder-derived `get_actual_joint_accelerations()`. RTDE output
registers 44--47 now expose control-update dt/count/max-gap and the dedicated
torque-thread tick count. Future evidence separately gates `500 Hz` RTDE
output, `450--550 Hz` torque application, at least `150 Hz` control-law
refresh, and at most `10 ms` control-update gap. This timing-corrected source
uses URScript's supported `pow(e, -omega*dt)` form rather than the unsupported
`exp()` spelling. The exact frozen source passed an official 5.25.2 URSim
full-source no-motion check and a real 5.26 bounded parser/start probe with
WAITING and COMPLETE observed, no RTDE inputs, no Direct Torque, and no
motion. The later 2026-07-28 canary chain below supersedes the earlier
physical-hold gap.

The timing-corrected source was exercised in one authorized no-contact hold on
2026-07-27. The receiver followed WAITING -> STARTUP -> TORQUE -> SAFE_EXIT ->
COMPLETE, all sampled robot/safety states stayed RUNNING/NORMAL, and the robot
returned STOPPED and stationary. Measured rates were `500.00 Hz` RTDE output,
`481.13 Hz` torque-thread calls, and `198.11 Hz` controller-law refresh. The
active maximum control-update gap was `6.00 ms`, maximum derived joint
acceleration was `1.257 rad/s²`, maximum translation from entry was
`0.102 mm`, and Kunwei captured `1013.15 Hz`. The immutable original evidence
remains `ok=false` because its postprocessor incorrectly included a stale
pre-STARTUP output-register value of `524.0` when computing the gap. The
read-only active-state audit is
`runs/tacdiffusion/direct_torque_v4_pow_decay_hold_live_20260727T1019HKT/active_cadence_audit_v1.json`.
Commit `a2a3a7ec` limits cadence aggregation to STARTUP/TORQUE and adds a
regression test. A fresh 100 ms hold is still required to produce the
canonical `ok=true` prior-stage receipt; this run does not authorize the
0.2 mm ramp.

On 2026-07-28, repeated exact-source sends exposed a separate Secondary Client
startup condition: port 30002 could retain the previous terminal output
registers without starting the new receiver. Controlled idle-only A/B probes
showed that a fresh read-only Primary Client connection on port 30001,
established immediately beside the Secondary send, reliably produced the
controller `PROGRAM_XXX_STARTED` event and the new WAITING identity. A delay
without that connection did not. The runner now holds a 150 ms fresh Primary
start barrier that only receives controller state/messages and never writes.
It also permits at most one resend, and only after fresh RTDE ticks prove that
the controller is still STOPPED in a stale terminal identity. PLAYING,
WAITING, or either new lease/episode echo blocks the resend.

The corrected runtime fingerprint
`4f615e45c92f2e210bfb0dc614f5fa9cac3f5d0fd5b6ee9fec389eeab95df54f`
then passed the complete ordered no-contact chain:

1. `hold_100ms`: `500.00 Hz` RTDE, `481.13 Hz` torque thread,
   `198.11 Hz` control refresh, `6.00 ms` maximum active update gap,
   `0.292 rad/s²` maximum derived joint acceleration, and `1007.26 Hz`
   Kunwei;
2. `ramp_0_2mm_500ms`: `500.00 Hz`, `496.05 Hz`, `199.60 Hz`,
   `6.00 ms`, `1.482 rad/s²`, and `1001.72 Hz`, respectively;
3. `reference_2s`: `500.00 Hz`, `499.50 Hz`, `199.90 Hz`, `6.00 ms`,
   `2.157 rad/s²`, and `1005.57 Hz`, respectively.

All three runs used one receiver send, observed Direct Torque and COMPLETE,
had zero lineage/nonmonotonic/parse/drop errors, and returned the robot to
STOPPED, stationary, RUNNING/NORMAL. The chain evidence and hashes are in
`runs/tacdiffusion/direct_torque_v4_pow_decay_reference_2s_fresh_primary_20260728T1259HKT/canary_chain_audit_v1.json`.
Trajectory fidelity is not a strict gate in this canary: the 0.2 mm ramp
command reached `0.183 mm` in the captured TORQUE rows while actual maximum
translation was `0.092 mm`; the bounded 2 s command reached `0.634 mm` while
actual maximum translation was `0.119 mm`. The chain therefore qualifies
transport, cadence, bounded Direct Torque execution, and capture, not
trajectory fidelity, contact control, or training-data eligibility.

The `reference_10s_diagnostic` time-scale isolation passed on 2026-07-28 with
runtime fingerprint `baa666d2`. It retained the same spatial reference,
stiffness, zero friction scales, tube, and guards while stretching time by
`5x`. Relative to its fresh 2 s baseline, maximum derived joint acceleration
fell from `2.061 rad/s²` to `0.917 rad/s²`, but actual/desired maximum
translation improved only from `15.7%` (`0.099/0.631 mm`) to `20.8%`
(`0.132/0.636 mm`), and the 10 s actual endpoint displacement was only
`0.029 mm`. Short-horizon acceleration is therefore not the primary cause of
weak tracking. The next isolation is offline-first inspection of effective
impedance gain and torque/Jacobian mapping, not an increase in torque, speed,
or contact force. Numeric sanity is recorded in
`config/direct_torque_v4_reference_10s_diagnostic_sanity.json`; immutable run
data and the derived audit are under
`runs/tacdiffusion/direct_torque_v4_reference_10s_diagnostic_20260728T1314HKT/`.
This diagnostic does not qualify trajectory fidelity, contact control, or
expert-data eligibility. Reusable failure, timing, sensor, and acceptance
lessons are collected in `DIRECT_TORQUE_V4_LIVE_LESSONS.md`.

The next A/B changes only the Direct Torque v2 friction profile. The accepted
zero-scale chain remains immutable as `zero_isolation`. A separate
`ur_default_v2_diagnostic` bundle uses UR's documented PolyScope 5.25+
defaults: viscous `[0.9, 0.9, 0.8, 0.9, 0.9, 0.9]` and Coulomb
`[0.8, 0.8, 0.7, 0.8, 0.8, 0.8]`. UR documents scale `0` as no compensation
and the listed values as the V2 defaults. The spatial reference, stiffness,
zero feedforward, orientation policy, tube, guards, and no-contact claim stay
unchanged. A new source fingerprint must begin again at `hold_100ms`; it may
advance only through the ordered chain. Numeric sanity is
`config/direct_torque_v4_ur_default_v2_friction_diagnostic_sanity.json`.

After the first official-friction hold, ramp, and 2 s reference all passed
numeric safety gates, the operator reported audible sound in every recent
run and requested one continuous 3 s trial for a complete subjective
startup/steady/exit assessment. The 10 s stage is therefore paused. A
`reference_3s_sound_diagnostic` retains the official-friction profile and the
same spatial path, stiffness, feedforward, tube, and guards, stretching only
the 2 s reference time by `1.5x`. It is diagnostic-only and requires a fresh
same-runtime hold and ramp receipt before execution.

The 3 s diagnostic passed all recorded gates, but the operator judged its
listening window too short. It is retained as evidence and superseded only
for subjective listening duration by `reference_7s_sound_diagnostic`. The
7 s stage changes no control or spatial parameter, uses a `3.5x` time stretch
of the same 2 s reference, and again requires fresh same-runtime hold and ramp
receipts. The paused 10 s stage remains out of scope.

The 7 s diagnostic completed with 3590 RTDE rows and 8303 Kunwei frames.
Direct Torque remained active for `7.176 s`; RTDE was `500 Hz`, torque calls
were `499.7 Hz`, and Kunwei was `1002.0 Hz`. Maximum derived joint
acceleration was `1.686 rad/s²`, maximum zero-baselined force norm was
`0.619 N`, and no guard or lineage fault occurred. Tracking remained weak:
the command reached `0.636 mm`, actual maximum translation was `0.129 mm`
(`20.3%`), and actual endpoint displacement was `0.059 mm`. Restoring the
documented friction profile therefore does not by itself resolve tracking.
The operator reported no audible abnormal sound during this 7 s window. This
falsifies the expectation that sound must recur in every official-friction
run, but does not isolate the cause of the earlier sounds because duration and
the fresh ordered receipt chain also differed. The derived audit is
`runs/tacdiffusion/direct_torque_v4_reference_7s_sound_diagnostic_20260728T1442HKT/friction_and_sound_audit_v1.json`.

The subsequent offline root-cause audit supersedes the earlier tracking
interpretation without rewriting the immutable live receipts. The historical
`actual maximum displacement / desired maximum displacement` values
(`16.8--21.4%` in the newest 2/7/10 s comparison) are max-of-norm noise
envelopes, not tracking coefficients. After excluding the first 20 ms, a joint
three-axis fit `actual_xyz = alpha * desired_xyz + intercept_xyz` gives
`alpha=-0.792%` for 2 s, `0.629%` for 7 s, and `0.414%` for 10 s, with
`R²<=0.0021`; time-reversed desired controls are of the same magnitude. The
retained runs therefore do not show repeatable Cartesian following above the
null/noise floor.

An independent calibrated Pinocchio cross-check on the 10 s run reconstructs
the translation-only `J^T w` command from the recorded q, desired/actual TCP,
K, and TCP speed. It reaches `0.2389 Nm` maximum torque norm versus
`0.2381 Nm` recorded, with `0.0241 Nm` RMS residual that also contains the
unreconstructed rotational, Coriolis, and joint-damping terms. This makes a
gross stiffness sign, base-frame direction, or Jacobian-transpose error
unlikely. The reconstructed TCP wrench is only about `0.39 N` on the sampled
rows (`0.42 N` without downsampling), so near-zero breakaway
friction/stiction/deadband is the leading physical hypothesis, not yet a
proven cause.

The same audit found a P0 data-publication defect: the controller wrote
state/cadence registers before the 12D applied-action registers, allowing a
500 Hz RTDE row to mix a fresh publication prefix with the previous run's
action tail. In the 10 s entry-lowpass CSV, the first STARTUP row has fresh
`control_update_count=1` and `torque_thread_tick_count=0`, but carries the
preceding 2 s run's `0.1902146167 Nm` joint-0 action. Historical
`first custom torque` and entry classifications are therefore retired.

The source now:

1. brackets every active action/cadence publication with generation stamps in
   output integer registers 29 and 33;
2. marks a row coherent only when `begin == end > 0`, retains all rows, and
   excludes incoherent rows from action-derived metrics;
3. stops the torque thread if `control_update_count` is stale for 25 torque
   ticks (nominal 50 ms), reporting fault code 13;
4. requires 150 ms (three filter time constants) of entry-velocity-filter
   warm-up before the 50 ms stable dwell may advance.

The next source revision also records the fresh entry joint positions and
fails with fault 14 if the first 100 ms exceeds `0.5 mrad` joint excursion or
`0.3 mm` TCP translation excursion. The host computes those physical
excursions from every RTDE row even when an action-publication row is rejected
as incoherent.

Autotune was then stopped through its controlled shutdown path and released
the exclusive live writer. Runtime fingerprint
`91318bee57c10f2760cfdfde8743b899021034e9471b899a60d4654ba73b10e4`
passed a fresh compile probe, exact receiver handshake, 100 ms hold, 0.2 mm
ramp, and 2 s reference. The three motion stages observed Direct Torque and
COMPLETE with Safety NORMAL. Their torque-call rates were `490.20`,
`498.01`, and `499.50 Hz`; maximum controller-update gaps were all `6 ms`.
Maximum entry joint/TCP excursions were respectively
`0.112 mrad / 0.076 mm`, `0.151 mrad / 0.087 mm`, and
`0.121 mrad / 0.055 mm`, below the fault-14 limits. This is live evidence for
the seqlock/watchdog/position-excursion source, not contact acceptance.

The same fresh reference capture adds read-only RTDE motor diagnostics:
`target_current`, `actual_current`, `actual_current_as_torque`,
`joint_control_output`, and `joint_mode`. Across 759 coherent active rows,
`joint_control_output` exactly equalled `target_current`, while per-joint
correlation between the approximately `0.23 Nm` commanded torque and
`target_current` remained between `-0.241` and `0.056`.
`actual_current_as_torque` peak-to-peak noise was `1.81--10.22 Nm`.
These outputs therefore do not resolve the small command and are not an
authoritative applied Direct Torque echo. They also are not F/T data: Kunwei
remains the sole experimental wrench source, and UR internal F/T is not used.

Directional tracking remains unsupported: the 2 s desired translation reached
`0.629 mm`, actual max-norm displacement was `0.106 mm`, directional
correlation was `0.150`, and fitted gain was `1.20%`. This repeats the prior
weak-tracking conclusion; the new evidence is live publication coherence,
entry fail-closed behavior, and actuator telemetry. The durable run audit is
`runs/tacdiffusion/direct_torque_v4_motor_diag_reference_2s_20260728/tracking_and_actuator_audit_v1.json`.
All captures remain `training_dataset=false`; the coherent action fraction in
the three motion stages was only `66.0--79.3%`, and Kunwei TCP batching still
does not establish causal 1 ms robot/wrench alignment.

A 2026-07-26 read-only 2 s position-control shadow at the fresh bench pose
captured 954 RTDE rows without sending a program or writing RTDE inputs. Mean
`target_moment` was approximately
`[0.000, 35.732, 22.270, 3.767, 0.00667, -0.00000057] Nm`, while maximum
observed joint speed was `4.93e-5 rad/s`. These values are diagnostic evidence
that a stationary position-control state does not present as an all-zero
`target_moment`; they are not copied into the Direct Torque command and are
not training data.

The compile probe is a distinct no-motion program: it contains no
`direct_torque`, `stopj`, motion primitive, or RTDE input read. A successful
probe evidence artifact is required before any v4 live canary. The live stages
cannot be skipped:

1. 100 ms hold at the fresh actual pose;
2. 0.2 mm smooth in-plane ramp over 0.5 s;
3. bounded 2 s reference.

The native runner does not require or parse a separate authorization JSON.
Its one-shot CLI live flags express the already-issued operator instruction;
after the first stage, strict-success evidence from the immediately preceding
stage remains mandatory. The live
CSV records every decoded controller packet, command lineage, actual state,
applied `F_ff`/`K`, and the six commanded joint torques. These diagnostic
captures remain `training_dataset=false`; Expert/contact collection needs a
separate authorization and acceptance gate.
