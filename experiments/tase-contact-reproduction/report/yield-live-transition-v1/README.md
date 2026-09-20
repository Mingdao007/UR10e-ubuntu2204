# Live-first transition

The user approved direct live on the retained UR10e/Kunwei platform and confirmed exclusive use, attendance and no contact. Required comparisons now include TASE original outer loop plus finite-time RNN and SFC baselines; DSFC/MSFC and evidence-driven new proposals; TASE-QP is a same-outer-loop solver ablation. Existing simulated data remain development evidence only.

## Actual actions and observations

- Stopped the task-owned offline scheduler and preserved 22 terminal trials plus the next interrupted registration. Its SIGTERM exit143 was intentional, not a controller failure. The phase reporter writer exited0; isolated edits are preserved and unreviewed.
- Public contact CPU/runtime/model preflight passed. Fresh read-only Dashboard and RTDE confirmed the owned resident contact package, Remote, Safety NORMAL and zero measured velocity.
- Sent one Dashboard Stop to that identified resident. Fresh Dashboard confirmed STOPPED/runningfalse and RTDE confirmed zero velocity. No Load, Play, Home or contact motion occurred.
- Freshly fetched both contact and Home triplets with the controller-access owner. Both passed byte and internal URP verification against local packages. No controller files were uploaded or changed.
- Observed payload0.413kg, CoG[.0011,.0031,.0163]m and TCP[0,0,.0874,0,0,0]. Home position error28.75micrometres and relative SO3 error.00312degrees. This is a stationary single-pose observation, not workspace or collision validation. A first local analysis invocation missed ROS Pinocchio paths; it was rerun with the existing ROS environment, without any device effect or source change.
- Public Kunwei logger ran3s without --write-rtde-inputs; 2985frames, emptyzeroevents, baseline not applied. It sent only stream start/stop to Kunwei; no tare/config/UR zero. Raw sensor values contain bias/gravity and are not independently measured contact loads. This is a fresh stream check, not500Hz force-control qualification.

## Active work

Astra kickoff uses hp-astra/gpt-6-astra/xhigh in isolated tase-live-baseline worktree; native session metadata verifies route/model/effort/cwd. It owns TASE baseline and QP ablation modules and advisory. Grok High independently owns the public live entry and registry in yield-live-entry worktree. One writer per scope. Main owns integration, verification and hardware. Discussion cadence is hourly; old Fable2hour policy is superseded.

## Next acceptance

The public native live dispatch is still being implemented. Validate its exact sole-writer composition, real transport/stop behavior and fresh machine gates, then execute2s/10s/fullperiod contact pilots without waiting for simulated searches or TASE completion. No physical task success is claimed here. No payload, TCP, force/velocity guard or freshness policy was modified.

## Stopped real-transport check at 07:27 UTC

The mature R004 RTDE transport established output-before-input recipes on the real controller and captured 4,999 frames over 10 s with SCHED_FIFO/20. No command packet, Load, Play or motion was sent. Dashboard before and after was STOPPED, Remote, Safety NORMAL, on the exact native contact package; observed joint/TCP velocities remained below 1 mm/s or mrad/s checks. Receive-gap p99 was 2.076 ms and maximum 6.738 ms; no gap reached the 20 ms held or 80 ms stale boundary. The host scheduler permission was verified. This is stopped transport/recipe evidence, not full writer timing, command-consumption, contact or task qualification. The process used its existing allowed CPU mask without a dedicated affinity allocation. Capture source, raw frames and summary are bound in raw-manifest.json.

## Geometry and combined transport findings

The 4,999-frame stopped capture demonstrates the rotation-vector branch issue: 2,495 samples exceed the 0.01 rad Home tolerance under naive vector subtraction even though maximum relative SO3 error is 0.0001804 rad. Maximum positional deviation from the Home reference was 0.0945 mm. This motivates the pending return-guard SO3 correction, not a tolerance increase. See static-home-geometry.json.

At 07:38 UTC the mature RTDE and Kunwei transports captured together for 10 s, using SCHED_FIFO/20 pinned to CPU0 selected from a preceding idle-load sample. 5,000 robot frames and 10,000 physical sensor frames were received. After the 0.5 s startup exclusion, no sensor age reached held/stale; host receive-age maximum was 1.408 ms, not sensor-internal sample age. RTDE receive gap p99 was 2.080 ms and maximum 4.472 ms. One gap exceeded the native controller integration limit of 4 ms; complete writer/command timing remains unqualified and limits have not changed. Raw force norm remained below 8.952 N and torque below 0.362 Nm, including gravity/bias. No baseline was applied and no tare, robot zero, Load, Play or command packet was sent. Both Dashboard observations remained STOPPED/Remote/Safety NORMAL.

The workspace stage selection now names the native contact package and records the actual pending entry/admission work. The old RNN selection and pending data remain in the preceding Git state and the retained governed-release pointer. This metadata correction did not change robot state or establish physical qualification.

## Video and entry review follow-up

The existing local MediaMTX service is active, but an actual RTSP DESCRIBE for `/arm` returned 404. The operator replied that the phone publisher cannot currently be enabled. No video has been recorded and no video/telemetry alignment is claimed. This missing video does not replace the standing task authority or stop transport/software work; intervention evidence must explicitly retain this limitation.

Main independently exercised the drafted native Home-return SO3 change with endpoint doubles: equivalent +pi/-pi attitudes pass and a real 0.02 rad error fails at the original limit (2 tests passed). The first invocation imported the unchanged main writer through pytest path setup and reproduced the old failure; the corrected invocation pre-imported and printed the isolated draft writer path. This is a draft regression result, not integrated production or hardware acceptance. The tests remain uncommitted until the reviewed production change is integrated.

Entry review remains open for real six-axis software baseline provenance, raw wrench limits during approach/search, persistent attempt evidence and truthful stop confirmation. Fabricated admission receipts belong only to test fixtures and must never admit hardware. No contact trial has started.

## Main takeover and R012 receive/baseline evidence

Main intentionally stopped the Grok repair session after preserving its 11 dirty files in `/tmp/yield-live-entry-main-takeover-20260920.tar.gz`. This was a source-review takeover: real baseline, pre-compensation wrench protection, truthful stop confirmation and actual transport extension remained incomplete. The launcher reports exit -2 / unparseable native JSON because main sent SIGINT; this must not be presented as a spontaneous provider outage or verified final model attestation. No Luna fallback was used. Astra kickoff remains in its original live session.

The native Home-return SO3 fix is integrated in `f9edf33d`; its return tests plus the existing sole-writer loop test passed (3 tests). Main has removed synthetic receipt generation from production imports, added actual baseline-file/capture binding, raw 20 N / 2 Nm checks before compensation, and fresh stationary STOP observation. Related seam tests passed 39 cases; these are software results. The full pilot entry remains under repair and must not be called physically qualified. Its QP must honor the mature host slew bound internally; PATH soft/hard ellipse binding remains outstanding. CPU-only endpoint tests now explicitly disable runtime/solver wall-clock deadlines; production deadlines remain 1.5 ms / 1 ms.

At 08:21:10-08:21:20 UTC, the real R012 input35 transport and Kunwei ran together for 10 s on CPU1 with FIFO20, without command publication, Load/Play, tare or zero. Dashboard before and after remained STOPPED/Remote/Safety NORMAL at the native contact program. 4,996 distinct robot frames and 9,997 sensor frames were observed. Home geometry, stationary speeds and EOAT were checked against actual observations. Software baseline statistics were derived from 4,746 post-warmup samples and bound to the exact raw capture; timestamps remain original and expire normally. No baseline was applied to a command.

R012 now records actual host-monotonic receive timestamps on distinct frames. A dedicated regression proves that duplicate controller frames return None without refreshing the previous receive time. Real receive-gap p99 was 2.080 ms; maximum was 12.097 ms, with one gap over the unchanged 4 ms native integration bound. Sensor age never reached held/stale. This is transport and baseline evidence, not complete writer timing or contact acceptance. See `r012-stopped-baseline-summary.json` and the appended raw manifest entries.

## Entry integration checkpoint after endpoint validation

The public native entry tests now pass 27 cases, including real lifecycle calls against endpoint doubles, three measured-and-durably-recorded qualification results before PATH, the 2 s register35 early-end request, independent fresh STOP confirmation, raw wrench protection and native motion-profile bounds. This remains software validation: all endpoint protocol tests disable wall-clock solver/runtime deadlines explicitly, while production retains them. Separate provider/qualification/admission seam tests passed 39 cases before the final motion-profile adapter changes. No physical contact has run.

The entry no longer inherits the old RNN profile's 2.5 rad/s / 20 rad/s2 values: the native profile binds joint speed .05 rad/s and TP acceleration 5 rad/s2, with the existing proposal outer-loop normal .003 m/s, tangent .01 m/s and attitude .05 rad/s caps. The 80x20 mm amplitude / 0.1 rad/s reference peaks at 8.94 mm/s; the inherited 4 mm/s tangential cap was inconsistent with it.

QP command-history binding now intersects the measured-dt host slew bounds internally. The live variant computes continuous feasible task scaling, because discrete fallback values can miss a narrow feasible interval. If preserving the entire normal component is itself unreachable from the last published command, it reports a reduced normal-only task (`normal_task_scale`, `normal_unloading_preserved=false`) rather than claiming unchanged force tracking. This is a common execution policy change, not demonstrated proposal benefit. The historical unbound offline route retains discrete scaling. Full PATH ellipse protection still needs binding before hardware pilot admission; no tuning, holdout or contribution result is implied.

Astra's kickoff session exited normally with TASE source, five new tests and 35 passing tests including dependency regressions. Main identified pending production native QP, complete configuration/state identity and atomic failure behavior. At 08:32 UTC the same native session was resumed for one bounded hourly repair/discussion round. The new turn metadata verifies provider hp-astra, model gpt-6-astra, effort xhigh and the isolated TASE worktree. Main continues to own all live-entry and hardware changes.
