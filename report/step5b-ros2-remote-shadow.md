# Step5b ROS2 Remote Shadow Report

Generated: 2026-06-16 21:41 HKT

## Summary

`step5b_ros2_remote_shadow_v1` is the baseline-first ROS2 remote-control
plumbing validation route. It replays retained successful Step5b contact
baseline CSVs and records the bridge-owned command/reference contract without
enabling motion. This is intentionally before any Step5d live candidate because
Step5b has known successful contact evidence, while Step5d mixes remote
plumbing, strict RNN, reacquire, and recovery-policy risks.

No live action was authorized or executed: no TP program load, no TP Play, no
bridge start, no URScript, no controller writes, no payload/TCP changes, and no
`zero_ftsensor()`.

## Replay Artifact

Exact artifact path:

```text
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/step5b_ros2_remote_shadow_20260616_214131
```

Required outputs:

```text
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/step5b_ros2_remote_shadow_20260616_214131/summary.json
/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/step5b_ros2_remote_shadow_20260616_214131/shadow_trace.csv
```

Replay command:

```bash
PYTHONPATH=/home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote \
python3 -m ur10e_step5d_remote.replay_step5b_shadow \
  --config /home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote/config/default_step5b_remote.yaml
```

Replay result:

- acceptance: `default_no_motion=true`, `covers_three_step5b_csvs=true`,
  `not_mostly_fail_fast=true`, `command_valid_shadow_present=true`,
  `desired_reference_fields_present=true`, `artifact_schema_complete=true`;
- rows replayed: `149656`;
- active/contact rows: `65317`;
- command-valid shadow ratio: `0.4364475864649596`;
- fail-fast ratio: `0.0`;
- `cmd_enabled_any=false`;
- max actual TCP speed: `0.0516853243 m/s`;
- normal load min/mean/max: `0.0 / 4.3225504433578585 / 34.7394202 N`;
- desired/reference field coverage: all required desired/path-error fields
  were present on `149556` rows, ratio `0.9993318009301331`.

One retained CSV,
`bridge_step5b_contact_cycloid_baseline_v1_20260614_222058`, contains no
command-valid active rows in this replay window. It is still included as
retained evidence and is reported separately in `summary.json`; it does not
cause fail-fast classification.

## Commands Run

```bash
python3 /home/andy/codex-private-skills-shared-main/skills/action-safety-gate/scripts/repo_doctor.py --repo /home/andy/ur10e_ros2_ws
python3 -m json.tool /home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/config/step5_stage_table.json >/tmp/step5_stage_table.json.valid
PYTHONPATH=/home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote python3 -m ur10e_step5d_remote.replay_step5b_shadow --config /home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote/config/default_step5b_remote.yaml
```

Validation commands were run after implementation:

```bash
source /opt/ros/humble/setup.bash && colcon build --symlink-install
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q experiments/tase-contact-reproduction/tests/test_step5d_paper_outer_loop.py experiments/tase-contact-reproduction/tests/test_step5d_strict_rnn_solver.py experiments/tase-contact-reproduction/tests/test_ur_contact_semantic_gate.py experiments/tase-contact-reproduction/tests/test_step5d_full_chain_sanity.py experiments/tase-contact-reproduction/tests/test_step5d_v15_permissive_recovery.py experiments/tase-contact-reproduction/tests/test_step5d_ros2_remote_shadow.py experiments/tase-contact-reproduction/tests/test_step5b_ros2_remote_shadow.py
source /opt/ros/humble/setup.bash && source /home/andy/ur10e_ros2_ws/install/setup.bash && ros2 run ur10e_step5d_remote replay_step5b_shadow --config /home/andy/ur10e_ros2_ws/src/ur10e_step5d_remote/config/default_step5b_remote.yaml --output-dir /tmp/step5b_shadow_installed_full
```

## Conclusion

The Step5b shadow replay suggests the ROS2 package/CLI trace plumbing is ready
for a later no-motion driver smoke plan. It does not prove live motion safety
and does not authorize TP or controller operation. Step5d remains diagnostic
until this Step5b-first route is carried through the next no-motion driver
smoke and any later live plan is explicitly accepted.
