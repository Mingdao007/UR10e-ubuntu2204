from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_autotune_v3_bridge as wrapper  # noqa: E402
from run_step5d_autotune_campaign import closure_sample_from_bridge_row  # noqa: E402
from step5d_autotune_v3.runtime_gate import (  # noqa: E402
    CampaignLease,
    write_campaign_lease,
)
from step5d_autotune_v3.state import atomic_json  # noqa: E402


def test_capture_worker_ignores_interactive_sigint_and_closes_from_parent_queue(
    tmp_path: Path,
    monkeypatch,
) -> None:
    observed: list[tuple[int, object]] = []
    monkeypatch.setattr(
        wrapper.signal,
        "signal",
        lambda number, handler: observed.append((number, handler)),
    )
    commands = SimpleNamespace(get=lambda: ("close",))
    errors = SimpleNamespace(put_nowait=lambda _value: None)
    wrapper._v3_capture_worker(commands, errors, str(tmp_path), ())
    assert observed == [(signal.SIGINT, signal.SIG_IGN)]


def _armed_v3_pose_bridge() -> tuple[Any, Any, Any]:
    import kunwei_rtde_bridge as bridge
    from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR

    args = bridge.parse_args(
        [
            "--bridge-profile",
            "step5d_strict_rnn_autotune_v1",
            "--bridge-mode",
            "line",
        ]
    )
    args.step5d_autotune_handshake = {"command": 1}
    args.step5d_physical_prior_reaction_normal_b = (
        STEP5D_V3_PHYSICAL_PRIOR.reaction_normal_b
    )
    args.step5d_physical_prior_approach_axis_b = (
        STEP5D_V3_PHYSICAL_PRIOR.approach_axis_b
    )
    args.step5d_physical_prior_precontact_rotvec_rad = (
        STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad
    )
    args.step5d_physical_prior_identity_payload = (
        STEP5D_V3_PHYSICAL_PRIOR.identity_payload()
    )
    args.step5d_physical_prior_sha256 = STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    args.step5d_physical_prior_binding_valid = True
    args.step5d_live_normal_load_gate_n = STEP5D_V3_PHYSICAL_PRIOR.load_gate_n
    args.step5d_live_normal_load_gate_dwell_s = (
        STEP5D_V3_PHYSICAL_PRIOR.load_gate_dwell_s
    )
    args.step5d_moving_sphere_enabled = False

    state = bridge.BridgeState()
    prior = STEP5D_V3_PHYSICAL_PRIOR.reaction_normal_b
    state.step5d_physical_prior_reaction_normal_b = prior
    state.step5d_physical_prior_approach_axis_b = (
        STEP5D_V3_PHYSICAL_PRIOR.approach_axis_b
    )
    state.step5d_physical_prior_precontact_xyz_m = (
        STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m
    )
    state.step5d_physical_prior_sha256 = STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    state.latched_normal_b = prior
    state.filtered_normal_b = prior
    state.normal_acquired = True
    return bridge, args, state


def _production_pose_tick(
    bridge: Any,
    args: Any,
    state: Any,
    *,
    stage: float,
    pose: list[float],
) -> dict[str, Any]:
    return bridge.compute_bridge_values(
        args,
        [0.0] * 6,
        {
            "actual_TCP_pose": pose,
            "actual_TCP_speed": [0.0] * 6,
            "actual_q": [0.0] * 6,
            "actual_qd": [0.0] * 6,
            "output_double_register_35": stage,
        },
        1.0,
        state,
        0.002,
    )


def _ticket_fixture(
    tmp_path: Path,
    argv: list[str],
) -> tuple[Path, SimpleNamespace, Path]:
    root = tmp_path / "experiment"
    (root / "config/step5d/releases/test").mkdir(parents=True)
    control = root / "config/step5d/releases/test/config/step5/control.json"
    control.parent.mkdir(parents=True)
    control.write_text('{"safety":"frozen"}\n', encoding="utf-8")
    launch = (
        root
        / "config/step5d/releases/test/config/step5/"
        "step5d_autotune_v3_launch_profile.json"
    )
    launch.write_text('{"launch":"frozen"}\n', encoding="utf-8")
    expected_program = (
        "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3_r012.urp"
    )
    safety_sha = hashlib.sha256(control.read_bytes()).hexdigest()
    runtime_identity = {
        "schema": "step5d.autotune-v3/tp-runtime-identity-v1",
        "program_id": "step5d_strict_rnn_autotune_v3_r012",
        "protocol_id": "v3_full_home_rolling_arm_v1",
        "protocol_version": 1,
        "digest_hi": 1234,
        "digest_lo": 5678,
        "script_basis_sha256": "2" * 64,
        "script_artifact_sha256": "1" * 64,
        "registers": {
            "protocol_version": 35,
            "digest_hi": 36,
            "digest_lo": 37,
        },
    }
    manifest = root / "config/step5d/releases/test/manifest.json"
    atomic_json(
        manifest,
        {
            "tp_runtime_identity": runtime_identity,
            "safety_envelope": {
                "path": "config/step5/control.json",
                "sha256": safety_sha,
            },
        },
    )
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    release = SimpleNamespace(
        program_id="step5d_strict_rnn_autotune_v3_r012",
        release_stage_id="step5d_strict_rnn_autotune_v3",
        control_profile_id="step5d_strict_rnn_autotune_v1",
        protocol_id="v3_full_home_rolling_arm_v1",
        manifest_path="config/step5d/releases/test/manifest.json",
        manifest_sha256=manifest_sha,
        artifacts={".script": {"sha256": "1" * 64}},
        generated_files={
            "config/step5/step5d_autotune_v3_launch_profile.json": hashlib.sha256(
                launch.read_bytes()
            ).hexdigest()
        },
        controller_target=expected_program,
    )
    lease = CampaignLease.issue(
        lease_id="1" * 32,
        launch_id="2" * 32,
        manifest_sha256=manifest_sha,
        release_stage_id=release.release_stage_id,
        program_id=release.program_id,
        protocol_id=release.protocol_id,
        campaign_id="step5d-native-1",
        campaign_epoch=1,
        campaign_fingerprint="8" * 64,
        safety_envelope_sha256=safety_sha,
    )
    lease_path = root / "run/campaign_lease.json"
    lease_sha = write_campaign_lease(lease_path.absolute(), lease)
    path = root / "run/runtime_ticket.json"
    encoded = json.dumps(argv, sort_keys=True, separators=(",", ":")).encode()
    atomic_json(
        path,
        {
            "schema": wrapper.TICKET_SCHEMA,
            "parent_pid": os.getppid(),
            "argv_sha256": hashlib.sha256(encoded).hexdigest(),
            "launch_id": lease.launch_id,
            "scope": wrapper.TICKET_SCOPE,
            "launch_profile": {
                "path": str(launch),
                "sha256": hashlib.sha256(launch.read_bytes()).hexdigest(),
            },
            "launch_profile_fingerprint": "b" * 64,
            "trial_overlay_fingerprint": "c" * 64,
            "release_stage_id": release.release_stage_id,
            "control_profile_id": release.control_profile_id,
            "tp_program_id": release.program_id,
            "manifest_sha256": manifest_sha,
            "safety_envelope_sha256": safety_sha,
            "launch_basis": {"path": str(root / "run/launch-basis.json"), "sha256": "e" * 64},
            "delivery_observation": {
                "path": str(root / "run/delivery-observation.json"),
                "sha256": "f" * 64,
            },
            "authority_epoch": 1,
            "campaign_prepare": {
                "path": str(root / "run/campaign-prepare.json"),
                "sha256": "0" * 64,
            },
            "campaign_binding": {
                "campaign_id": lease.campaign_id,
                "campaign_epoch": lease.campaign_epoch,
                "campaign_fingerprint": lease.campaign_fingerprint,
                "candidate_plan_revision": 1,
                "candidate_plan_sha256": "9" * 64,
                "trial_overlay_plan_sha256": "a" * 64,
                "machine_binding_sha256": "d" * 64,
            },
            "campaign_lease": {"path": str(lease_path), "sha256": lease_sha},
            "arm_gate_path": str((root / "run/arm_gate.json").absolute()),
        },
    )
    return path, release, root


def test_wrapper_requires_parent_and_exact_argv_ticket(tmp_path: Path) -> None:
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket, release, root = _ticket_fixture(tmp_path, argv)
    assert wrapper._strict_ticket(
        ticket, argv, release_identity=release, root=root
    )["scope"] == wrapper.TICKET_SCOPE
    inline = json.loads(ticket.read_text(encoding="utf-8"))
    assert wrapper._strict_ticket(
        inline, argv, release_identity=release, root=root
    )["scope"] == wrapper.TICKET_SCOPE
    try:
        wrapper._strict_ticket(
            ticket,
            [*argv, "--duration-s", "1"],
            release_identity=release,
            root=root,
        )
    except wrapper.BridgeTicketError as exc:
        assert "argv binding" in str(exc)
    else:
        raise AssertionError("mutated argv was accepted")


def test_wrapper_uses_manifest_identity_without_stale_release_literals() -> None:
    source = Path(wrapper.__file__).read_text(encoding="utf-8")
    assert "TP_PROGRAM_ID =" not in source
    assert "step5d_strict_rnn_autotune_v3_r008" not in source
    assert "step5d_strict_rnn_autotune_v3_r006" not in source


def test_wrapper_refuses_direct_start_without_runtime_ticket(capsys) -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert wrapper.main([]) == 24
    assert "RUNTIME_TICKET" in capsys.readouterr().err


def test_bridge_ticket_requires_exact_campaign_lease_binding(tmp_path: Path) -> None:
    argv = ["--bridge-profile", "step5d_strict_rnn_autotune_v1"]
    ticket, release, root = _ticket_fixture(tmp_path, argv)
    payload = json.loads(ticket.read_text(encoding="utf-8"))
    payload["campaign_lease"].pop("sha256")
    ticket.write_text(json.dumps(payload), encoding="utf-8")
    try:
        wrapper._strict_ticket(
            ticket,
            argv,
            release_identity=release,
            root=root,
        )
    except wrapper.BridgeTicketError as exc:
        assert "campaign lease reference" in str(exc)
    else:
        raise AssertionError("incomplete campaign lease binding was accepted")


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
    assert "state.step5d_physical_prior_precontact_xyz_m" in source
    assert "_step5d_prealign_verified" in source
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


def test_r003_stage22_incident_waits_for_post_movel_stage23_admission() -> None:
    import kunwei_rtde_bridge as bridge

    incident = json.loads(
        (ROOT / "tests/fixtures/v3_r003_stage22_prealign_incident.json").read_text(
            encoding="utf-8"
        )
    )
    rotation = bridge.rotvec_to_matrix(*incident["actual_tcp_rotvec_rad"])
    tcp_z_axis_b = tuple(rotation[index][2] for index in range(3))
    axis_error_rad = bridge.angle_between_unit(
        tcp_z_axis_b,
        tuple(incident["approach_axis_base"]),
    )
    position_error_m = math.sqrt(
        sum(
            (actual - expected) ** 2
            for actual, expected in zip(
                incident["actual_tcp_xyz_m"],
                incident["expected_precontact_xyz_m"],
                strict=True,
            )
        )
    )

    assert math.isclose(
        axis_error_rad,
        incident["observed_axis_error_rad"],
        rel_tol=0.0,
        abs_tol=1e-8,
    )
    assert axis_error_rad > bridge.STEP5D_SEARCH_POSE_RUNTIME_TOLERANCE_RAD
    assert position_error_m > bridge.STEP5D_PREALIGN_POSITION_TOLERANCE_M
    assert not bridge.step5d_search_pose_contract_active_for_stage(
        step5d_liveprep_profile=True,
        robot_stage=incident["observed_stage"],
    )
    assert not bridge.step5d_prealign_position_contract_active_for_stage(
        step5d_liveprep_profile=True,
        robot_stage=incident["observed_stage"],
    )
    assert bridge.step5d_search_pose_contract_active_for_stage(
        step5d_liveprep_profile=True,
        robot_stage=incident["post_prealign_admission_stage"],
    )
    assert bridge.step5d_prealign_position_contract_active_for_stage(
        step5d_liveprep_profile=True,
        robot_stage=incident["post_prealign_admission_stage"],
    )
    assert bridge.step5d_search_pose_contract_ok_for_errors(
        position_required=True,
        position_error_m=0.0,
        axis_error_rad=0.0,
    )
    assert not bridge.step5d_search_pose_contract_ok_for_errors(
        position_required=True,
        position_error_m=bridge.STEP5D_PREALIGN_POSITION_TOLERANCE_M + 1e-6,
        axis_error_rad=0.0,
    )
    assert not bridge.step5d_search_pose_contract_ok_for_errors(
        position_required=True,
        position_error_m=0.0,
        axis_error_rad=bridge.STEP5D_SEARCH_POSE_RUNTIME_TOLERANCE_RAD + 1e-6,
    )
    for stage in (24.0, 24.2):
        assert bridge.step5d_search_pose_contract_active_for_stage(
            step5d_liveprep_profile=True,
            robot_stage=stage,
        )
        assert not bridge.step5d_prealign_position_contract_active_for_stage(
            step5d_liveprep_profile=True,
            robot_stage=stage,
        )
        assert bridge.step5d_search_pose_contract_ok_for_errors(
            position_required=False,
            position_error_m=math.inf,
            axis_error_rad=0.0,
        )
    assert not bridge.step5d_search_pose_contract_active_for_stage(
        step5d_liveprep_profile=False,
        robot_stage=23.0,
    )


def test_r003_tick_timeline_runs_the_production_pose_guard_after_movel() -> None:
    from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR

    incident = json.loads(
        (ROOT / "tests/fixtures/v3_r003_stage22_prealign_incident.json").read_text(
            encoding="utf-8"
        )
    )
    bridge, args, state = _armed_v3_pose_bridge()
    initial_pose = [
        *incident["actual_tcp_xyz_m"],
        *incident["actual_tcp_rotvec_rad"],
    ]
    target_pose = [
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m,
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad,
    ]
    mid_movel_pose = [
        *(
            (start + target) / 2.0
            for start, target in zip(initial_pose[:3], target_pose[:3], strict=True)
        ),
        *target_pose[3:],
    ]
    search_far_pose = [target_pose[0], target_pose[1], 0.017, *target_pose[3:]]
    search_near_pose = [target_pose[0], target_pose[1], 0.010, *target_pose[3:]]
    timeline = [
        ("pre_campaign", 20.0, initial_pose, False, False),
        ("stage22_before_movel", 22.0, initial_pose, False, False),
        ("stage22_movel_in_progress", 22.0, mid_movel_pose, False, False),
        ("stage22_movel_complete", 22.0, target_pose, False, False),
        ("stage23_post_movel_admission", 23.0, target_pose, True, True),
        ("stage24_search_far", 24.0, search_far_pose, True, False),
        ("stage24_2_search_near", 24.2, search_near_pose, True, False),
        ("trial_terminal", 29.0, search_near_pose, False, False),
    ]

    observed: dict[str, dict[str, Any]] = {}
    for name, stage, pose, active, position_required in timeline:
        values = _production_pose_tick(
            bridge,
            args,
            state,
            stage=stage,
            pose=pose,
        )
        observed[name] = values
        assert values["_step5d_search_pose_contract_active"] == float(active)
        assert values["_step5d_search_pose_contract_position_required"] == float(
            position_required
        )
        assert values.get("stop_request", 0.0) == 0.0
        assert values.get("_step5d_contact_safety_reason") != (
            "physical_prior_search_pose_mismatch"
        )

    admitted = observed["stage23_post_movel_admission"]
    assert admitted["_step5d_search_pose_contract_position_ok"] == 1.0
    assert admitted["_step5d_search_pose_contract_orientation_ok"] == 1.0
    assert admitted["_step5d_prealign_verified"] == 1.0
    for name in ("stage24_search_far", "stage24_2_search_near"):
        assert observed[name]["_step5d_search_pose_contract_position_ok"] == 1.0
        assert observed[name]["_step5d_search_pose_contract_orientation_ok"] == 1.0


def test_production_pose_timeline_fails_closed_on_stage_pose_mismatch() -> None:
    from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR

    incident = json.loads(
        (ROOT / "tests/fixtures/v3_r003_stage22_prealign_incident.json").read_text(
            encoding="utf-8"
        )
    )
    initial_pose = [
        *incident["actual_tcp_xyz_m"],
        *incident["actual_tcp_rotvec_rad"],
    ]
    target_pose = [
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_xyz_m,
        *STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad,
    ]
    bad_position = [
        target_pose[0] + 0.004,
        target_pose[1],
        target_pose[2],
        *target_pose[3:],
    ]
    bad_orientation = [*target_pose[:3], *initial_pose[3:]]
    bad_search_orientation = [target_pose[0], target_pose[1], 0.012, *initial_pose[3:]]
    cases = [
        ("stage23_published_before_movel_completion", 23.0, initial_pose),
        ("stage23_position_outside_tolerance", 23.0, bad_position),
        ("stage23_orientation_outside_tolerance", 23.0, bad_orientation),
        ("stage24_orientation_drift", 24.0, bad_search_orientation),
    ]

    for name, stage, pose in cases:
        bridge, args, state = _armed_v3_pose_bridge()
        values = _production_pose_tick(
            bridge,
            args,
            state,
            stage=stage,
            pose=pose,
        )
        assert values["_step5d_search_pose_contract_active"] == 1.0, name
        assert values["_step5d_search_pose_contract_ok"] == 0.0, name
        assert values["stop_request"] == 1.0, name
        assert values["step4e_cmd_valid"] == 0.0, name
        assert values["_step5d_contact_safety_reason"] == (
            "physical_prior_search_pose_mismatch"
        ), name
        assert all(values[field] == 0.0 for field in bridge.BRIDGE_INPUT_NAMES[:6]), name


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


def test_production_hard_tube_seam_is_30_mm_100_hz_and_exact_stop() -> None:
    import kunwei_rtde_bridge as bridge
    from ur10e_experiment_runtime.hard_tube import (
        HardTubeGuard,
        HardTubeReason,
    )
    from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
    from ur10e_experiment_runtime.stage_adapters import (
        PATH_ORIGIN_XY_M,
        Stage25ControllerProgressAdapter,
    )

    adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    )
    guard = HardTubeGuard(reference_sha256=adapter.reference_sha256)
    args = SimpleNamespace(
        step5d_controller_progress_adapter=adapter,
        step5d_hard_tube_guard=guard,
        step5d_hard_tube_progress_age_ns=0,
    )
    values = bridge.bridge_zero_values()
    values["stop_request"] = 0.0
    inside = [
        PATH_ORIGIN_XY_M[0] + 0.029,
        PATH_ORIGIN_XY_M[1],
        0.008,
        0.0,
        0.0,
        0.0,
    ]
    bridge.apply_step5d_hard_tube_guard(
        values=values,
        args=args,
        latest_output={"output_double_register_31": 0.0, "timestamp": 1.0},
        robot_stage=25.0,
        pose=inside,
    )
    assert values["_step5d_hard_tube_reason"] == HardTubeReason.TUBE_OK.name
    assert values["_step5d_hard_tube_radius_m"] == 0.03
    assert values["_step5d_hard_tube_evaluation_hz"] == 100.0
    assert values["stop_request"] == 0.0

    outside = [inside[0] + 0.002, *inside[1:]]
    for tick in range(1, 5):
        bridge.apply_step5d_hard_tube_guard(
            values=values,
            args=args,
            latest_output={
                "output_double_register_31": 0.0,
                "timestamp": 1.0 + 0.002 * tick,
            },
            robot_stage=25.0,
            pose=outside,
        )
        assert values["_step5d_hard_tube_reason"] == (
            HardTubeReason.TUBE_BETWEEN_SAMPLES.name
        )
        assert values["stop_request"] == 0.0

    values.update({name: 0.1 for name in bridge.BRIDGE_INPUT_NAMES[:6]})
    values["step4e_cmd_valid"] = 1.0
    bridge.apply_step5d_hard_tube_guard(
        values=values,
        args=args,
        latest_output={"output_double_register_31": 0.0, "timestamp": 1.01},
        robot_stage=25.0,
        pose=outside,
    )
    assert values["_step5d_hard_tube_reason"] == (
        HardTubeReason.TUBE_ACTUAL_BREACH.name
    )
    assert values["stop_request"] == 1.0
    assert values["step4e_cmd_valid"] == 0.0
    assert all(values[name] == 0.0 for name in bridge.BRIDGE_INPUT_NAMES[:6])
