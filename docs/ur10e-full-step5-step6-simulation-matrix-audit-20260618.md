# UR10e Full Step5/Step6 Offline Simulation Matrix Audit

Date: 2026-06-18

Scope: Step5a, Step5b, Step5c, Step5d, Step6a, Step6b.

Boundary: offline/no-motion only. This audit does not authorize live robot
motion, TP Play/upload, bridge start, URScript send, `zero_ftsensor()`, Kunwei
tare/config writes, or payload/TCP/safety/controller setting writes.

Primary artifact:

- `experiments/tase-contact-reproduction/runs/step56_simulation_matrix_20260618_194316/matrix_summary.json`

## Current Matrix

| Stage | Source spec | Safe frame | Runner status | Artifact status | Test status | Gazebo status | Known blocker |
|---|---|---|---|---|---|---|---|
| Step5a | `config/local_control_textbook_spec.json#step5a_cycloid_no_contact_v3`; `config/step5a_local_control_spec.json`; `config/step5_stage_table.json#step5a_cycloid_no_contact_v3` | `config/step5_safe_frame.json`; TP v3 affine map preserved | `local_control_textbook_offline_shadow` | Full offline task-space artifact in `step5a/summary.json` | `test_step56_simulation_matrix.py` passes | Shared world marker | No live ROS2 air-motion authorization; `0.009 m/s` remains TP v3 command-vector clamp provenance, not achieved-speed truth. |
| Step5b | `config/local_control_textbook_spec.json#step5_contact_cycloid_baseline_v1`; `config/step5_stage_table.json#step5_contact_cycloid_baseline_v1`; `config/step5b_authorization_state.json` | `config/step5_safe_frame.json`; safe-frame rotation | `step5b_mvp_offline_shadow_live_locked` | Full offline MVP artifact in `step5b/summary.json`; `goal_count=1` | `test_step56_simulation_matrix.py` and `test_step5b_simulation_mvp.py` cover this surface | Shared contact surface | Step5b live runner remains locked/revoked after 2026-06-18 table vibration; no live retry. |
| Step5c | `config/step5_stage_table.json#step5c_joint_rnn_cycloid_v1`; `config/step5c_tase_paper_truth.json` | `config/step5_safe_frame.json`; schematic safe-frame rotation only | `quarantined_offline_only` | Schematic offline artifact in `step5c/summary.json` | Quarantine behavior asserted in `test_step56_simulation_matrix.py` | Shared marker only | 2026-06-13 live dry-run moved in the wrong XY/Z direction; DLS/MuJoCo Jacobian mapping is not trusted. |
| Step5d | `config/step5_stage_table.json#step5d_strict_rnn_liveprep_v15a`; strict RNN reproduction target remains blocked | `config/step5_safe_frame.json`; schematic safe-frame rotation only | `retained_evidence_offline_shadow_only` | Schematic contact artifact in `step5d/summary.json`; no physics closed-loop claim | Contact semantics and no-physics claim asserted in `test_step56_simulation_matrix.py` | Shared contact surface | v15a stopped by `step5d_contact_safety:hold_duty_limit` after about 0.998 s of Stage25; no current live package. |
| Step6a | `config/local_control_textbook_spec.json#step6a_eight_no_contact_v1`; `config/step6_stage_table.json#step6a_eight_no_contact_v1` | `config/step6_eight_safe_frame.json`; five-point no-scale frame preserved | `local_control_textbook_offline_shadow` | Full offline eight-path artifact in `step6a/summary.json` | Step6 safe-frame mapping asserted in `test_step56_simulation_matrix.py` | Shared world marker | Retained no-contact rehearsal only; no live ROS2 air-motion authorization. |
| Step6b | `config/local_control_textbook_spec.json#step6_contact_eight_baseline_v2`; `config/step6_stage_table.json#step6_contact_eight_baseline_v2` | `config/step6_eight_safe_frame.json`; five-point no-scale frame preserved | `contact_baseline_offline_shadow` | Full offline eight contact artifact in `step6b/summary.json`; simulated Kunwei force evidence | Contact normal contract asserted in `test_step56_simulation_matrix.py` | Shared contact surface | Contact path is represented as offline simulated force evidence only; no live bridge/TP fallback. |

## Implementation Surface

- `ur10e_example_controllers.step56_simulation_matrix`
- `step56_simulation_matrix` console script
- `worlds/step5_table_world.sdf` now contains Step5 and Step6 safe-frame
  markers/contact surfaces.
- `experiments/tase-contact-reproduction/tests/test_step56_simulation_matrix.py`

## Artifact Contract

Every stage artifact records:

- `schema`, `stage_id`, `mode=offline_no_motion`;
- `live_robot_command_authorized=false`;
- `contact_motion_authorized=false`;
- `zero_ftsensor_authorized=false`;
- `payload_tcp_safety_writes_authorized=false`;
- source paths for textbook spec, stage table, safe frame, calibrated URDF,
  world, and launch;
- frames and units;
- acceptance fields that explicitly keep `physics_closed_loop_claimed=false`.

Contact artifacts additionally record:

- `reaction_normal=[0, 0, 1]` for load;
- `approach_normal=[0, 0, -1]` for posture/press direction;
- `normal_load_definition=dot(force_base, reaction_normal)`.

## Completion Classification

Completed:

- Full Step5/Step6 offline artifact matrix generation for 5a, 5b, 5c, 5d, 6a,
  and 6b.
- Stage-aware source-path, safe-frame, unit, frame, force-semantics, and
  no-live authorization provenance.
- Shared Gazebo world markers/surfaces for Step5 and Step6.

Not completed and not claimed:

- Full Gazebo physics closed-loop contact simulation.
- `gz_ros2_control` or controller-manager based simulation.
- Any live UR10e execution or live Step5b retry.
- Step5c or Step5d strict RNN re-enable.
