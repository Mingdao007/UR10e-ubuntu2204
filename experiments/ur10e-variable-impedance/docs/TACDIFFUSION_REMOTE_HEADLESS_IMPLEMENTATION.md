# TacDiffusion Remote/headless implementation report

This lane is offline/no-motion only. It does not start Dashboard, bridge,
RTDE writers, Direct Torque, ARM, Play, contact, or robot motion.

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
  `tools/tacdiffusion_remote_headless.py`: captured-response parsers and dry-run
  lifecycle. `build_receiver_source()` emits the thin controller-resident
  URScript source with exact-ZOH 500 Hz filtering, K slew, derived D,
  WAIT_FOREVER, and distinct END/DRAIN branches; parsing remains no-motion.
- `experiments/tase-contact-reproduction/tools/step5d_tacdiffusion_direct_torque.py`
  now has an explicit `mainline=True` route for guarded 12D actions, dynamic K,
  derived D, model identity, and episode-local failure smoothing. Its default
  fixture mode is legacy-only and cannot be selected by the live bridge.
- `tacdiffusion/filter.py` and the six-force dataset/model are compatibility
  facades/replay symbols; mainline routing is 50 ms rate-invariant and 12D and
  rejects incompatible dimensions.

Validation command:

```bash
PYTHONPATH=experiments/ur10e-variable-impedance \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  experiments/ur10e-variable-impedance/tests/test_tacdiffusion_*.py
```

Live acceptance remains unproven. The next user-owned actions are Remote mode,
four-corner light-touch teaching, hardware recovery if needed, and explicit
final live authorization.
