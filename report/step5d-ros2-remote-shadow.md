# Step5d ROS2 Remote Shadow Report

Generated: 2026-06-16 16:39 HKT

## Summary

`ur10e_step5d_remote` adds a ROS2 Python package for offline Step5d
remote-control shadow replay. The default launch/config is no-motion:
`enable_motion: false`, `cmd_enabled=false` in the generated trace, and no
bridge, URScript, TP package, controller upload, payload/TCP write, or
`zero_ftsensor()` action is authorized or executed.

The v1 shadow policy removes the v15a long zero-qdot hold recovery. Low-load or
speed-risk rows can debounce for only `short_dwell_cycles=2`, then transition
to `ACTIVE_REACQUIRE` along `approach_normal = -reaction_normal` while cage and
semantic gates allow it, or to `FAIL_FAST_STOP_REQUEST` when a hard boundary is
hit.

## Replay Artifact

Exact artifact path:

```text
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/step5d_ros2_remote_shadow_20260616_163906
```

Required outputs:

```text
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/step5d_ros2_remote_shadow_20260616_163906/summary.json
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/step5d_ros2_remote_shadow_20260616_163906/shadow_trace.csv
```

Replay command:

```bash
PYTHONPATH=/home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote \
python3 -m ur10e_step5d_remote.replay_shadow \
  --config /home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote/config/default_step5d_remote.yaml
```

Replay result:

- acceptance: `default_no_motion=true`, `long_hold_removed=true`,
  `reacquire_or_failfast_seen=true`, `artifact_schema_complete=true`;
- `cmd_enabled_any=false`;
- max hold duty: `0.03334114494347948`;
- state counts: `ACTIVE_REACQUIRE=917`, `CONTACT_TRACK=747`,
  `SHORT_DWELL_HOLD=17`, `FAIL_FAST_STOP_REQUEST=126594`.

## Commands Run

Repo and implementation checks:

```bash
python3 /home/andy/codex-private-skills-shared-main/skills/action-safety-gate/scripts/repo_doctor.py --repo /home/andy/ur10e_ros2_ws
python3 -m json.tool /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/config/step5_stage_table.json >/tmp/step5_stage_table.json.valid
source /opt/ros/humble/setup.bash && colcon build --symlink-install
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q experiments/tase-contact-reproduction/tests/test_step5d_ros2_remote_shadow.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q experiments/tase-contact-reproduction/tests/test_step5d_paper_outer_loop.py experiments/tase-contact-reproduction/tests/test_step5d_strict_rnn_solver.py experiments/tase-contact-reproduction/tests/test_ur_contact_semantic_gate.py experiments/tase-contact-reproduction/tests/test_step5d_full_chain_sanity.py experiments/tase-contact-reproduction/tests/test_step5d_v15_permissive_recovery.py experiments/tase-contact-reproduction/tests/test_step5d_ros2_remote_shadow.py
```

Smoke replay command:

```bash
PYTHONPATH=/home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote \
python3 -m ur10e_step5d_remote.replay_shadow \
  --config /home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote/config/default_step5d_remote.yaml \
  --output-dir /tmp/step5d_shadow_smoke \
  --max-rows-per-csv 20
```

Focused v15a replay command:

```bash
PYTHONPATH=/home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote \
python3 -m ur10e_step5d_remote.replay_shadow \
  --config /home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote/config/default_step5d_remote.yaml \
  --output-dir /tmp/step5d_shadow_v15a \
  --csv /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/bridge_step4e_line_outerloop_step5d_strict_rnn_liveprep_v15a_20260616_160355/bridge_rtde_500hz.csv
```

Installed console smoke:

```bash
source /opt/ros/humble/setup.bash
source /home/andy/ur10e_ros2_ws/install/setup.bash
ros2 run ur10e_step5d_remote replay_shadow \
  --config /home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote/config/default_step5d_remote.yaml \
  --output-dir /tmp/step5d_shadow_installed_smoke \
  --max-rows-per-csv 1
```

## TP Guidance

For `step5d_ros2_remote_shadow_v1`, do nothing on the Teach Pendant:

- do not load v15a or any other Step5d program;
- do not press TP Play;
- do not start/open the old bridge as a live runner;
- do not send URScript;
- do not call `zero_ftsensor()`;
- do not change payload, TCP, installation, safety, or controller settings.

Any future live `enable_motion:=true` route requires a separate live plan,
controller/package gates, read-back verification where applicable, and explicit
authorization. This report is not live-motion permission.
