# TacDiffusion Remote/headless implementation report

This lane is offline/no-motion only. It does not start Dashboard, bridge,
RTDE writers, Direct Torque, ARM, Play, contact, or robot motion.

The JSON-only operator is `tools/tacdiffusion_remote_headless.py`. Its
deterministic plan calls `trajectory.episode_plan(50, seed)` and covers all
seven trajectory families, emitting `speed_scale`, `normal_force_target_n`,
and `preload_n` for every episode. Local queue commands use the durable
`PersistentRollingQueue`; `run-offline-campaign` composes `CampaignRunner`
with an injected synthetic episode executor, explicit safe retract, Home ACK
and consume, failure continuation, `WAIT_FOREVER`, and explicit `END`/`DRAIN`.
It reports 50 executions and is fixture-only, never readiness evidence.

Implemented surfaces:

- `ur10e_vic/tacdiffusion/surface.py` and `trajectory.py`: ordered four-corner
  calibration, safe inset, explicit reaction/approach normals, intrinsic local
  paths, physical speed/acceleration/curvature bounds, and seeded families.
- `observation.py`, `action.py`, `expert.py`, `dynamic_filter.py`, and
  `mailbox.py`: versioned 84D temporal observation, 12D action, state-dependent
  expert pre-filter labels, six-axis K/D guards, and exact settling-time filter.
- `queue.py`: atomic state-path persistence, dispatch identity, restart
  no-replay recovery, FIFO priority, END/DRAIN, and episode-local failure.
- `raw_artifact.py`, `mainline_dataset.py`, `mainline_model.py`,
  `checkpoint.py`, `environment.py`, and `promotion.py`: durable 500 Hz raw
  evidence, derived training views, trainable conditional model, hash-bound
  artifacts, cached environment fingerprint, replay metrics, and virtual-clock
  shadow promotion.
- `remote_headless.py`, `direct_torque_receiver.py`, and
  `tools/tacdiffusion_remote_headless.py`: captured-response parsers, JSON-only
  offline lifecycle evaluation, and durable queue/campaign composition.
  `build_receiver_source()` emits the thin controller-resident
  URScript source with exact-ZOH 500 Hz filtering, K slew, derived D,
  WAIT_FOREVER, and distinct END/DRAIN branches; parsing remains no-motion.
- `experiments/tase-contact-reproduction/tools/step5d_tacdiffusion_direct_torque.py`
  now has an explicit `mainline=True` route for guarded 12D actions, dynamic K,
  derived D, model identity, and episode-local failure smoothing. Its default
  fixture mode is legacy-only and cannot be selected by the live bridge.
- `tacdiffusion/filter.py` and the six-force dataset/model are compatibility
  facades/replay symbols; mainline routing is 50 ms rate-invariant and 12D and
  rejects incompatible dimensions.

Mainline artifact lineage is v3: the strict dataset binds 84D observations,
12D actions, frozen episode splits and raw hashes; the checkpoint binds the
trained conditional DDPM; and active authorization requires the validated v3
checkpoint binding, hash-linked promotion manifest, and separate explicit v3
live authorization. Active remains false until exactly two distinct named live
shadow artifacts (`smooth_low_curvature` and `turning_high_curvature`) each
pass the roughly 45-second hardware-shadow gate; synthetic fixtures never
count.

URSim is a read-only image/protocol boundary. The locally inspected image is
`universalrobots/ursim_e-series:5.25.2`; no PolyScope 5.26 URSim runtime is
available or run. Image inspection does not pull, create, start, exec, or
connect to a container. Dashboard, Load, Play, bridge, ARM, RTDE writer,
contact, and motion remain forbidden in this lane.

Validation command:

```bash
PYTHONPATH=experiments/ur10e-variable-impedance \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest -q \
  experiments/ur10e-variable-impedance/tests

PYTHONPATH=experiments/ur10e-variable-impedance:experiments/tase-contact-reproduction/tools \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /usr/bin/python3 -m pytest -q \
  experiments/tase-contact-reproduction/tests/test_step5d_tacdiffusion_direct_torque.py
```

Live acceptance remains unproven. The next user-owned actions are Remote mode,
four-corner light-touch teaching, hardware recovery if needed, and explicit
final live authorization.
