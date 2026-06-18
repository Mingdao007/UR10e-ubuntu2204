# UR10e Step5/Step5b Correction Ledger

This ledger turns high-impact user corrections into executable engineering
rules for the ROS2 Remote Control/headless and Gazebo/offline migration.

| Date | Source | Wrong assumption | Correction | Required rule | Affected surfaces | Validation gate | Status |
|---|---|---|---|---|---|---|---|
| 2026-06-18 | `/home/andy/codex-private-skills-shared-main/skills/ur10e-remote-control/references/current-remote-stage.md` | TP, URCap, or legacy bridge can remain the current Step5b route. | Current route is ROS2 Remote Control/headless; TP/URCap/bridge are archive/reference unless explicitly routed. | New Step5b work must not select TP/bridge fallback as current execution. | launch, scripts, docs, handoffs | `step5b_authorization_status.py`; reviewer scan for TP/bridge current-route wording | Active |
| 2026-06-17 | `/home/andy/codex_handoffs/ur10e-step5a-achieved-speed-root-cause-audit-20260617-1823.md` | Step5a v1/v2 can be treated as current baselines. | Active Local Control baseline is Step5a TP v3; v1/v2 are retained provenance. | Migration docs must label v1/v2 as provenance and v3 as active baseline. | spec docs, tests, stage tables | `audit_local_control_textbook_alignment.py --json` | Active |
| 2026-06-17 | `/home/andy/codex_handoffs/ur10e-step5a-achieved-speed-root-cause-audit-20260617-1823.md` | `0.009 m/s` is naturally a ROS2 achieved-speed hard gate and `22 s` is a proven user primitive. | `0.009 m/s` is TP v3 command-vector clamp provenance; `22 s` is assistant-selected active-v3 timing in inspected evidence. | Preserve provenance labels and do not convert command clamp into unexamined achieved-speed truth. | Step5a/Step5b specs, acceptance gates, docs | textbook alignment audit and unit tests that assert provenance labels | Active |
| 2026-06-17 | `/home/andy/codex-private-skills-shared-main/skills/ur10e-remote-control/references/current-remote-stage.md` | Step5 local XY can be applied as raw base-X/base-Y offsets. | Step5 local XY must map through `u_along_xy` and `p_lateral_xy` from `step5_safe_frame.json`. | Any simulation, replay, or live code must use safe-frame basis mapping. | trajectory generation, Gazebo geometry, artifact schemas | `test_step5b_simulation_mvp.py`; `test_step5b_contact_control_core.py` | Active |
| 2026-06-17 | `/home/andy/codex_handoffs/ur10e-kunwei-force-gate-supersedes-force-topic-handoff-20260617-0034.md` and current stage doc | UR internal `/force_torque_sensor_broadcaster/ft_data` can hard-block Step5/Step5b force evidence. | Kunwei persistent monitor is Step5/Step5b force evidence; UR internal wrench is advisory. | Offline/sim artifacts must name force source and avoid using internal wrench as required evidence. | force gates, simulation force traces, reports | force-source fields in generated artifacts; contact semantic gate | Active |
| 2026-06-18 | `/home/andy/.codex/context/ur-contact-force-frame-contract.md` | Raw force direction can be normalized into posture/press direction. | `reaction_normal` is for load; `approach_normal = -reaction_normal` is for posture and press direction. | Name vector roles explicitly and test normal-load and approach direction semantics. | contact-control core, simulation force trace, future Gazebo contact | `ur_contact_semantic_gate.py`; `test_step5b_simulation_mvp.py` | Active |
| 2026-06-18 | `/home/andy/codex_handoffs/ur10e-step5b-live-entry-locked-after-vibration-20260618-1625.md` | Step5b live can be retried after runner diagnostics. | Step5b live runner is revoked/locked after table vibration; next work is continuous low-vibration offline/sim preposition validation. | Do not unlock or live retry until replacement preposition has offline artifact and explicit approval. | Step5b runner, authorization, launch docs | `step5b_authorization_status.py`; simulation preposition artifact goal_count=1 | Active |
| 2026-06-18 | Gazebo launch smoke in this goal | Calibrated URDF can be published to Gazebo unchanged because no controller manager is launched. | The calibrated URDF contains a real `ur_robot_driver/URPositionHardwareInterface` `ros2_control` block. Simulation launch must strip it before publishing `/robot_description`. | Gazebo/offline launch paths must not expose real-driver control metadata unless a future explicit sim-control design owns it. | Gazebo launch, tests, simulator docs | `test_simulation_urdf_strips_real_ros2_control_driver` | Active |

## Current Ledger Policy

- Evidence files and raw runs are retained evidence, not disposable logs.
- Failed versions stay archived as retained evidence before any newer version is
  treated as current.
- Simulation artifacts must state whether they are offline/sim, shadow, or live
  evidence.
- A passing package/read-back or offline gate is not a live-motion permission.
- Offline/Gazebo robot descriptions must be sanitized so a future sim smoke
  cannot accidentally inherit a real UR hardware plugin configuration.
