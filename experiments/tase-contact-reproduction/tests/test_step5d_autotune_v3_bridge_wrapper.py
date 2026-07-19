from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_bridge as wrapper  # noqa: E402
from run_step5d_autotune_campaign import closure_sample_from_bridge_row  # noqa: E402


def _ticket(path: Path, argv: list[str]) -> Path:
    encoded = json.dumps(argv, sort_keys=True, separators=(",", ":")).encode()
    path.write_text(
        json.dumps(
            {
                "schema": wrapper.TICKET_SCHEMA,
                "parent_pid": os.getppid(),
                "argv_sha256": hashlib.sha256(encoded).hexdigest(),
                "launch_id": "1" * 32,
                "scope": "live_continuous_campaign",
                "identity": {
                    "contract_sha256": "0" * 64,
                    "control_fingerprint": "a" * 64,
                    "orchestration_fingerprint": "d" * 64,
                },
                "launch_profile_fingerprint": "b" * 64,
                "trial_overlay_fingerprint": "c" * 64,
                "release_stage_id": "step5d_strict_rnn_autotune_v3",
                "control_profile_id": "step5d_strict_rnn_autotune_v1",
                "tp_program_id": "step5d_strict_rnn_autotune_v3",
                "campaign_binding": {
                    "campaign_id": "campaign-v3",
                    "campaign_epoch": 2,
                    "candidate_plan_revision": 1,
                    "candidate_plan_sha256": "e" * 64,
                    "trial_overlay_plan_sha256": "f" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_wrapper_requires_parent_and_exact_argv_ticket(tmp_path: Path) -> None:
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket = _ticket(tmp_path / "ticket.json", argv)
    assert wrapper._strict_ticket(ticket, argv)["scope"] == "live_continuous_campaign"
    try:
        wrapper._strict_ticket(ticket, [*argv, "--duration-s", "1"])
    except wrapper.BridgeTicketError as exc:
        assert "argv binding" in str(exc)
    else:
        raise AssertionError("mutated argv was accepted")


def test_wrapper_refuses_direct_start_without_runtime_ticket(capsys) -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert wrapper.main([]) == 24
    assert "RUNTIME_TICKET" in capsys.readouterr().err


def test_live_ticket_requires_exact_campaign_binding(tmp_path: Path) -> None:
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket = _ticket(tmp_path / "ticket.json", argv)
    payload = json.loads(ticket.read_text(encoding="utf-8"))
    payload["campaign_binding"].pop("trial_overlay_plan_sha256")
    ticket.write_text(json.dumps(payload), encoding="utf-8")
    try:
        wrapper._strict_ticket(ticket, argv)
    except wrapper.BridgeTicketError as exc:
        assert "campaign binding" in str(exc)
    else:
        raise AssertionError("incomplete campaign binding was accepted")


def test_wrapper_source_has_no_campaign_runner_arm_or_motion_surface() -> None:
    source = Path(wrapper.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "Popen" not in calls
    assert "run" not in calls
    assert "start_step5d_autotune_runner" not in source
    assert "HostCommand.ARM" not in source
    assert "speedj(" not in source
    assert "movel(" not in source


def test_production_compact_schema_satisfies_wait_ack_closure_consumer() -> None:
    production_fields = [
        *wrapper._V3_COMPACT_EXACT_FIELDS,
        *(f"ur_actual_TCP_pose_{index}" for index in range(6)),
        *(f"ur_actual_TCP_speed_{index}" for index in range(6)),
        *(f"ur_actual_q_{index}" for index in range(6)),
        *(f"ur_actual_qd_{index}" for index in range(6)),
        *(f"ur_actual_qdd_{index}" for index in range(6)),
        *(f"ur_output_int_register_{index}" for index in range(24, 31)),
    ]
    compact = wrapper.compact_v3_fieldnames(production_fields)
    assert wrapper._V3_RUNNER_CLOSURE_FIELDS.issubset(compact)
    row = {name: "0" for name in compact}
    row["ur_output_int_register_26"] = "70"
    sample = closure_sample_from_bridge_row(row)
    assert sample["output_double_register_36"] == 0.0
    assert sample["output_double_register_37"] == 0.0
    assert sample["output_double_register_38"] == 0.0
    assert sample["output_double_register_39"] == 0.0
    assert sample["output_double_register_44"] == 0.0


def test_observed_wait_ack_schema_incident_is_exactly_closed() -> None:
    incident = json.loads(
        (ROOT / "tests/fixtures/v3_wait_ack_schema_incident.json").read_text(
            encoding="utf-8"
        )
    )
    observed = set(incident["observed_compact_output_double_register_fields"])
    required = set(incident["runner_required_output_double_register_fields"])
    assert sorted(required - observed) == incident["missing_fields"]
    assert required.issubset(wrapper._V3_RUNNER_CLOSURE_FIELDS)
    assert required.issubset(wrapper._V3_COMPACT_EXACT_FIELDS)


def test_autotune_relatch_is_excluded_and_load_gate_is_explicit() -> None:
    source = (ROOT / "tools/kunwei_rtde_bridge.py").read_text(encoding="utf-8")
    assert "physical_prior_load_gate" in source
    assert "args.bridge_profile != STEP5D_AUTOTUNE_STAGE_ID" in source
    assert "step5d_live_normal_load_gate_dwell_s" in source
    assert "step5d_moving_sphere_kernel" in source
    assert "_step5d_moving_sphere_reason" in source
    assert "state.step5d_physical_prior_approach_axis_b" in source
    assert "physical_prior_search_pose_mismatch" in source
    search_guard = source.split(
        "def apply_step5d_search_pose_fail_stop", 1
    )[1].split("def apply_v29_fail_stop", 1)[0]
    assert 'values["step4e_cmd_valid"] = 0.0' in search_guard
    assert 'values["stop_request"] = 1.0' in search_guard
    reset_hunk = source.split("def reset_step5d_autotune_diagnostics_for_trial", 1)[1].split(
        "def step5d_liveprep_runtime_missing", 1
    )[0]
    assert "state.integral_error_n_s = 0.0" in reset_hunk
    assert "state.normal_velocity_m_s = 0.0" in reset_hunk
    assert "state.latched_normal_b = prior" in reset_hunk
    assert "state.filtered_normal_b = prior" in reset_hunk
    assert "state.step5d_stage25_normal_relatched = False" in reset_hunk
    assert "progress_adapter.reset()" in reset_hunk
    assert "sphere_kernel.reset()" in reset_hunk


def test_pre_arm_hold_tick_keeps_bridge_alive_with_zero_command() -> None:
    import kunwei_rtde_bridge as bridge

    args = bridge.parse_args(
        [
            "--bridge-profile",
            "step5d_strict_rnn_autotune_v1",
            "--bridge-mode",
            "line",
        ]
    )
    latest_output = {
        "actual_TCP_pose": [0.49, 0.14, 0.033, 3.12, 0.0, 0.0686],
        "actual_TCP_speed": [0.0] * 6,
        "actual_q": [0.0] * 6,
        "actual_qd": [0.0] * 6,
        "output_double_register_35": 70.0,
    }
    values = bridge.compute_bridge_values(
        args,
        [0.0] * 6,
        latest_output,
        1.0,
        bridge.BridgeState(),
        0.002,
    )
    assert values["_step5d_autotune_pre_arm_hold"] == 1.0
    assert values["_step5d_contact_safety_reason"] == "autotune_pre_arm_hold"
    assert all(values[name] == 0.0 for name in bridge.BRIDGE_INPUT_NAMES)


def test_production_sphere_seam_uses_typed_progress_and_exact_stop() -> None:
    import kunwei_rtde_bridge as bridge
    from ur10e_experiment_runtime.moving_sphere import (
        build_offline_fixture_stopping_bound,
        MovingSphereKernel,
        SphereReason,
    )
    from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
    from ur10e_experiment_runtime.stage_adapters import (
        PATH_ORIGIN_XY_M,
        Stage25ControllerProgressAdapter,
    )

    bound = build_offline_fixture_stopping_bound(
        reaction_latency_s=0.002,
        acceleration_growth_m_s2=0.1,
        minimum_deceleration_m_s2=2.0,
        center_speed_bound_m_s=0.003,
        center_acceleration_bound_m_s2=0.00015,
        numeric_margin_m=0.0001,
        evidence_sha256="b" * 64,
        validity_domain="offline_fixture_only_not_live_certification",
    )
    adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    )
    args = SimpleNamespace(
        step5d_controller_progress_adapter=adapter,
        step5d_moving_sphere_kernel=MovingSphereKernel(
            reference_sha256=adapter.reference_sha256,
            stopping_bound=bound,
            required_validity_domain=bound.validity_domain,
        ),
        step5d_moving_sphere_progress_age_ns=0,
    )
    values = bridge.bridge_zero_values()
    values["stop_request"] = 0.0
    pose = [*PATH_ORIGIN_XY_M, 0.008, 0.0, 0.0, 0.0]
    bridge.apply_step5d_moving_sphere_guard(
        values=values,
        args=args,
        latest_output={"output_double_register_31": 0.0, "timestamp": 1.0},
        robot_stage=25.0,
        pose=pose,
        tcp_speed_m_s=0.0,
    )
    assert values["_step5d_moving_sphere_reason"] == SphereReason.SPHERE_OK.name
    assert values["stop_request"] == 0.0

    for nonfinite_timestamp in (math.inf, -math.inf):
        values.update({name: 0.1 for name in bridge.BRIDGE_INPUT_NAMES[:6]})
        values["step4e_cmd_valid"] = 1.0
        values["stop_request"] = 0.0
        bridge.apply_step5d_moving_sphere_guard(
            values=values,
            args=args,
            latest_output={
                "output_double_register_31": 0.0,
                "timestamp": nonfinite_timestamp,
            },
            robot_stage=25.0,
            pose=pose,
            tcp_speed_m_s=0.0,
        )
        assert values["_step5d_moving_sphere_reason"] == (
            SphereReason.SPHERE_PROGRESS_STALE.name
        )
        assert values["stop_request"] == 1.0
        assert values["step4e_cmd_valid"] == 0.0
        assert all(values[name] == 0.0 for name in bridge.BRIDGE_INPUT_NAMES[:6])

    mismatch_adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    )
    mismatch_args = SimpleNamespace(
        step5d_controller_progress_adapter=mismatch_adapter,
        step5d_moving_sphere_kernel=MovingSphereKernel(
            reference_sha256="c" * 64,
            stopping_bound=bound,
            required_validity_domain=bound.validity_domain,
        ),
        step5d_moving_sphere_progress_age_ns=0,
    )
    values.update({name: 0.1 for name in bridge.BRIDGE_INPUT_NAMES[:6]})
    values["step4e_cmd_valid"] = 1.0
    bridge.apply_step5d_moving_sphere_guard(
        values=values,
        args=mismatch_args,
        latest_output={"output_double_register_31": 0.0, "timestamp": 2.0},
        robot_stage=25.0,
        pose=pose,
        tcp_speed_m_s=0.0,
    )
    assert values["_step5d_moving_sphere_reason"] == (
        SphereReason.SPHERE_REFERENCE_MISMATCH.name
    )
    assert values["stop_request"] == 1.0
    assert values["step4e_cmd_valid"] == 0.0
    assert all(values[name] == 0.0 for name in bridge.BRIDGE_INPUT_NAMES[:6])
