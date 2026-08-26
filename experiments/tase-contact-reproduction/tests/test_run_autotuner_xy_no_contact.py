from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import gzip
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
MODULE_PATH = TOOLS / "run-autotuner-xy-no-contact"
LOADER = importlib.machinery.SourceFileLoader(
    "run_autotuner_xy_no_contact", str(MODULE_PATH)
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_frozen_xy_no_contact_slice_and_terminal_evidence(tmp_path: Path) -> None:
    anchor = MODULE.Pose(
        MODULE.BASE_ORIGIN_XY_M[0],
        MODULE.BASE_ORIGIN_XY_M[1],
        0.041,
        3.10,
        0.02,
        0.07,
    )
    for time_s in (0.0, 30.0, 60.0):
        point = MODULE.trajectory_at(time_s, anchor)
        canonical = MODULE.canonical_reference_at(time_s)
        assert point.desired_xy_m == tuple(canonical["desired_xy"])
        assert point.desired_pose.z_m == anchor.z_m
        assert point.desired_pose.as_tuple()[3:] == anchor.as_tuple()[3:]

    assert len(MODULE.sample_times_hz()) == 30001
    assert MODULE.sample_times_hz()[-1] == 60.0
    assert max(MODULE.trajectory_at(t, anchor).reference_speed_m_s for t in (i * 0.002 for i in range(30001))) <= 0.003
    assert MODULE.GUARD_ORDER == ("moving_reference_hard_ellipse", "soft_cbf_qp")
    assert MODULE.FAIL_CLOSED_LIMITS.max_force_n == 6.0
    assert MODULE.FAIL_CLOSED_LIMITS.max_torque_nm == 0.5
    assert MODULE.FAIL_CLOSED_LIMITS.max_fixed_z_error_m == 0.002
    assert MODULE.FAIL_CLOSED_LIMITS.max_orientation_error_rad == MODULE.math.radians(5.0)
    assert MODULE.FAIL_CLOSED_LIMITS.max_linear_command_speed_m_s == 0.004
    assert MODULE.FAIL_CLOSED_LIMITS.max_qdot_rad_s == 0.15

    output_dir = tmp_path / "success"
    transport = MODULE.OfflineDiagnosticTransport()
    session = MODULE.OneShotDiagnosticSession(transport, output_dir)
    result = session.run()
    assert result.state is MODULE.SessionPhase.HOMED
    assert result.path_count == 1
    assert result.path_sample_count == 30001
    assert result.return_home_verified is True
    assert transport.operations.count("return_path_origin") == 1
    assert transport.operations.count("return_session_home") == 1
    assert "zero_abort" not in transport.operations
    assert json.loads((output_dir / "evidence.json").read_text())[
        "activation"
    ]["tell_exact"] is False
    try:
        session.run()
    except MODULE.OneShotTerminalError:
        pass
    else:
        raise AssertionError("a second PATH was accepted")

    failed_dir = tmp_path / "tracking_failure"
    failed = MODULE.run_offline_dry_run(failed_dir, tracking_failure_at_s=0.004)
    assert failed.state is MODULE.SessionPhase.HOMED
    assert failed.tracking_failed is True
    assert failed.path_sample_count == 30001
    assert (failed_dir / "rtde_500hz.csv").exists()
    assert (failed_dir / "evidence.json").exists()
    failed_evidence = json.loads((failed_dir / "evidence.json").read_text())
    assert failed_evidence["lifecycle"]["automatic_recovery_motion"] is False
    assert failed_evidence["terminal"]["state"] == MODULE.SessionPhase.HOMED.value
    assert failed_evidence["terminal"]["tracking_failed"] is True


def test_dedicated_tp_package_is_one_shot_no_contact_and_guarded(tmp_path: Path) -> None:
    import build_autotuner_xy_no_contact_package as package_builder

    manifest = package_builder.build(tmp_path / "package")
    assert manifest["program"] == "step5d_autotuner_xy_no_contact_diagnostic_v1"
    script = (tmp_path / "package" / f"{manifest['program']}.script").read_text()
    urp = gzip.decompress(
        (tmp_path / "package" / f"{manifest['program']}.urp").read_bytes()
    ).decode()
    assert "force_norm > 6.0" in script
    assert "torque_norm > 0.5" in script
    assert "codex_guarded_xy_move(path_origin, 30.0" in script
    assert "codex_guarded_xy_move(path_origin, 40.0" in script
    assert "codex_guarded_xy_move(session_home, 41.0" in script
    assert script.count("write_output_float_register(35, 25.0)") == 1
    assert "contact" in script.lower()
    assert "force_mode(" not in script
    assert "tell_exact" not in script
    assert f'URProgram name="{manifest["program"]}"' in urp


def test_live_host_requires_fresh_lifecycle_before_accepting_stage_78() -> None:
    source = (TOOLS / "autotuner_xy_no_contact_live.py").read_text(encoding="utf-8")
    assert "(20.0, 30.0, 25.0, 40.0, 41.0)" in source
    assert "if saw_dedicated_stage and abs(stage - 78.0) < 0.05:" in source


def test_guard_order_and_structural_safety_faults_are_fail_closed() -> None:
    anchor = MODULE.Pose(
        MODULE.BASE_ORIGIN_XY_M[0], MODULE.BASE_ORIGIN_XY_M[1], 0.03, 3.1, 0.0, 0.05
    )
    desired = MODULE.trajectory_at(5.0, anchor)
    healthy = MODULE.Feedback(
        actual_pose=desired.desired_pose,
        actual_velocity=(0.0,) * 6,
        force_norm_n=0.0,
        torque_norm_nm=0.0,
        qdot_max_abs_rad_s=0.0,
    )
    assert MODULE.evaluate_safety(healthy, desired, desired.desired_twist).passed
    # Tracking is evidence-only: it must not short-circuit the full timeline.
    assert MODULE.evaluate_safety(
        replace(healthy, tracking_ok=False, fault_reason="tracking_gate_failed"),
        desired,
        desired.desired_twist,
    ).passed
    fault_cases = (
        (replace(healthy, force_norm_n=6.01), "force_limit_exceeded"),
        (replace(healthy, torque_norm_nm=0.51), "torque_limit_exceeded"),
        (replace(healthy, heartbeat_ok=False), "heartbeat_fault"),
        (replace(healthy, protocol_ok=False), "protocol_fault"),
        (replace(healthy, joint_mode_ok=False), "joint_mode_fault"),
        (replace(healthy, receiver_ok=False), "receiver_fault"),
        (replace(healthy, single_live_writer_ok=False), "single_live_writer_fault"),
        (
            replace(
                healthy,
                actual_pose=replace(healthy.actual_pose, z_m=healthy.actual_pose.z_m + 0.0021),
            ),
            "fixed_z_error_exceeded",
        ),
    )
    for feedback, reason in fault_cases:
        decision = MODULE.evaluate_safety(feedback, desired, desired.desired_twist)
        assert decision.passed is False
        assert decision.reason == reason

    outside_hard = replace(
        desired.desired_pose,
        x_m=desired.desired_pose.x_m + 0.030 * MODULE.U_ALONG_XY[0],
        y_m=desired.desired_pose.y_m + 0.030 * MODULE.U_ALONG_XY[1],
    )
    guarded = MODULE.guard_path_command(
        desired, outside_hard, guard_stack=MODULE.R012PathGuardStack()
    )
    assert guarded.terminal_stop is True
    assert guarded.soft_cbf_qp_active is False
    assert MODULE.GUARD_ORDER == ("moving_reference_hard_ellipse", "soft_cbf_qp")
