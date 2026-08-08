# Step5d TacDiffusion formal V4 flow

This is an isolated Remote Secondary Client Direct Torque route. It does not
load or execute the TP program currently selected for Autotune, and TP upload
or controller read-back is therefore inapplicable rather than missing. The
physical controller identity is instead rebound on every attempt by the
Secondary Client receiver source hash, protocol token, live lease, episode
identity, Remote state, Safety NORMAL, and the fresh RTDE echo.

The controller endpoint is fixed at `192.168.1.18:30002`; the sole force
endpoint is Kunwei KWR75 raw TCP at `192.168.50.25:5152`.

Kunwei KWR75 raw TCP is the only force/contact/guard/training authority. UR
internal F/T fields and APIs are forbidden. The software baseline is exactly
1000 native frames; the sensor is never zeroed, tared, filtered, or configured.

Formal contact episodes are split into two independently testable control
phases:

1. `ACQUISITION`: a host-authorised bounded velocity phase. The controller
   source uses `speedl` in base `-Z` at exactly `0.0005 m/s`, with bounded
   `0.010 m/s^2` acceleration/deceleration and a `0.025 m` maximum search
   corridor. Controller-side stopping starts at or before `0.0249865 m`:
   `v^2/(2a)=12.5 micrometres`, with one `0.002 s` control-tick bound reserved,
   so the physical stop remains inside `25 mm`. The host consumes every native
   1 kHz Kunwei sample in sample order and latches at `1 N` for exactly 50
   consecutive native samples. Its host packet heartbeat runs at a `2 ms`
   control period with `40` held ticks (`0.080 s`); gap, replay, torn-read and
   timeout faults fail closed. An inert `command_prepare=0` packet is held while
   the `0.150 s` Primary start barrier is active; only the fresh sequence-2
   `command_start=1` packet after that barrier arms motion and the 80 ms active
   heartbeat. PREPARE itself has a bounded `0.400 s` inert expiry, so host death
   cannot leave the Secondary program waiting indefinitely. Normal packets are
   output-echo ACK paced; ABORT is immediate. `command_abort=2` issues bounded zero-speed
   `speedl` plus frozen-deceleration `stopl` and exits. The fixed
   `sensor_delivery_watchdog_s=0.080`
   is latest TCP-batch delivery age, not per-frame host-arrival freshness. Hard
   `20 N` / `2 Nm` guards, lease, episode, route, robot, Safety, protective and
   joint checks also fail closed. Acquisition rows use a separate non-training
   schema and are never Direct Torque protocol evidence.
2. `HANDOFF -> SETTLE -> TRACK`: after the host latch, velocity control is
   stopped. A fresh stationary dwell is proved before the actual pose is
   captured as the handoff anchor. The stationary dwell is `100 ms`; its
   translation threshold is `0.1 mm/s`, below the `0.5 mm/s` search speed, and
   TCP/rotation/joint speed limits are checked before handoff. Before the
   handoff-gated Direct Torque source starts, a fresh `MODE_IDLE` sequence-0
   packet is re-primed with that actual anchor, a fresh Kunwei guard lineage,
   raw feedforward exactly zero, and exact `K=(600,600,600,30,30,30)`.
   Direct Torque cannot start without that identity-bound handoff. Its first
   desired pose is the fresh actual handoff pose and its first feedforward is
   zero; the existing bounded settle ramp then reaches tracking. `TRACK` uses
   exact `K=(600,600,600,30,30,30)`.

No sensor, guard, protective-stop, Safety, joint, or route fault performs an
automatic retract or return. A no-contact search exhaustion is a separate
recoverable terminal outcome and is not a fault. Formal eligibility selects
only `formal_phase=TRACK` rows with `receiver_state=STATE_TORQUE`; the
acquisition phase is never admitted by relabelling.

The accepted contact qualification is invalidated whenever this source
identity changes. The independent formal pointer therefore remains at
`formal_v4_no_contact_qualification` with seven-family requalification
required; the resolver can be source-consistent while `live_ready=false`.

The ordered stages are:

1. seven independent no-contact trajectory qualifications, each 8 s at 500 Hz
   and guarded at 6 N / 0.5 Nm;
2. fixed-K expert campaign, 200 eligible episodes, first 50 pilot episodes
   included, Kxyz 600 N/m and Krot 30 Nm/rad;
3. sealed 84D/12D dataset, fixed 6D model targets, checkpoint, exact 100/50 Hz
   50-step benchmark, then two 45 s model-inactive shadows;
4. variable-K campaign only after fixed-K completion and K/load qualification,
   again 200 eligible episodes with the first 50 included;
5. sealed 84D/12D dataset, variable 7D model targets, checkpoint, exact 100/50
   Hz benchmark, and two 45 s model-inactive shadows;
6. acceptance reports, cold-read closure, and a Git checkpoint.

Normal load is the environment-on-tool reaction projected on the named
reaction normal. The acquisition controller reports a fresh stationary
handoff, and the tracking transition is bumpless: a `1 mm` position error maps
to `0.6 N` under `K=600 N/m`; the bounded handoff mismatch is `0.3 mm` and is
checked before the first Direct Torque command.

Each physical attempt has an immutable directory and exactly one terminal
hash-chained ledger record. Failed attempts never consume an eligible ordinal.
The formal dataset builder accepts only 200 cold-readable V4 artifacts with
their recorder manifests and sole formal eligibility receipts. Fixed-K and
variable-K campaigns keep their existing independent campaign identities and
are both downstream of the same acquisition/handoff contract.
