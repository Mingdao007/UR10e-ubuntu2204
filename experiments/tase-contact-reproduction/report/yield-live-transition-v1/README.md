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
