from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from ur10e_experiment_runtime.candidate_identity import (
    ControlCandidateUid,
    OccurrenceUid,
    ParameterUid,
    TransportCandidateUid,
)


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_step5d_manual_bridge as preflight  # noqa: E402
import run_step5d_manual_bridge as wrapper  # noqa: E402
import run_step5d_manual_bridge_live as live  # noqa: E402
import run_step5d_manual_live_campaign as campaign  # noqa: E402
import step5d_manual_bridge as bridge  # noqa: E402
import step5d_autotune_live_driver as mailbox_driver  # noqa: E402
from step5d_autotune_state_machine import TpLoopState, TpPacket  # noqa: E402
from step5d_manual_atomic_release import canonical_bytes  # noqa: E402


def _fake_release() -> tuple[dict, dict]:
    manifest = {
        "identity": {
            "parent_r009_commit": "b" * 40,
            "parent_r009_release_manifest_sha256": "c" * 64,
        },
        "artifacts": {
            extension: {"path": f"program{extension}", "sha256": character * 64}
            for extension, character in ((".script", "1"), (".txt", "2"), (".urp", "3"))
        },
        "source_fingerprints": {"tools/manual.py": "4" * 64},
    }
    return {"manifest_sha256": "a" * 64}, manifest


def test_context_is_write_once_digest_bound_and_no_arm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch = tmp_path / "launch.json"
    launch.write_text("{}")
    monkeypatch.setattr(bridge, "_release_document", lambda _root: _fake_release())
    monkeypatch.setattr(bridge, "load_launch_profile", lambda _path: SimpleNamespace(fingerprint="5" * 64))
    now = datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc)
    payload = bridge.build_context(tmp_path, plant_epoch=1, launch_profile_path=launch, now=now)
    assert payload["program"] == "step5d_strict_rnn_manual_tune_v2"
    assert payload["protocol"] == "v3_full_home_manual_hold_v1"
    assert payload["wire_protocol"] == "v3_full_home_rolling_arm_v1"
    assert payload["bridge_authorized"] is True
    assert payload["arm_authorized"] is False
    assert payload["motion_authorized"] is False
    output = tmp_path / "context.json"
    bridge.write_once(output, payload)
    with pytest.raises(bridge.ManualBridgeError, match="already exists"):
        bridge.write_once(output, payload)
    loaded = bridge.load_context(tmp_path, output, now=now)
    assert loaded == payload
    tampered = json.loads(output.read_text())
    tampered["arm_authorized"] = True
    output.write_text(json.dumps(tampered))
    with pytest.raises(bridge.ManualBridgeError, match="digest"):
        bridge.load_context(tmp_path, output, now=now)


def test_preflight_requires_exact_manual_program() -> None:
    stopped = preflight._program_safe(
        {"programState": "STOPPED /programs/andyl/kunwei/step5/step5d_strict_rnn_manual_tune_v2.urp"},
        {},
    )
    wrong = preflight._program_safe(
        {"programState": "STOPPED /programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3_r009.urp"},
        {},
    )
    assert stopped["ok"] is True
    assert wrong["ok"] is False


def test_live_preflight_validation_is_no_arm_and_exact() -> None:
    predicates = {
        name: {"ok": True}
        for name in (
            "safety_normal", "program_safe_for_bridge", "robot_stationary",
            "prealign_start_clearance", "no_existing_writer", "mailbox_initial_zero",
            "runtime_dependencies",
        )
    }
    context = {"manual_release_manifest_sha256": "a" * 64}
    payload = {
        "schema": bridge.PREFLIGHT_SCHEMA,
        "ok": True,
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_stage_id": bridge.RELEASE_STAGE,
        "control_profile_id": bridge.CONTROL_PROFILE,
        "tp_program_id": bridge.PROGRAM,
        "manual_release_manifest_sha256": "a" * 64,
        "predicates": predicates,
    }
    path = ROOT / "tests" / ".manual-preflight-never-written.json"
    original = live.strict_object
    try:
        live.strict_object = lambda _path, _role: payload
        assert live._validate_preflight(path, context)["ok"] is True
        payload["tp_program_id"] = "step5d_strict_rnn_autotune_v3_r009"
        with pytest.raises(bridge.ManualBridgeError):
            live._validate_preflight(path, context)
    finally:
        live.strict_object = original


def test_runtime_ticket_binds_parent_argv_context_and_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context_path = tmp_path / "context.json"
    preflight_path = tmp_path / "preflight.json"
    context_path.write_text("{}")
    preflight_path.write_text(json.dumps({
        "ok": True,
        "fresh": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tp_program_id": bridge.PROGRAM,
        "manual_release_manifest_sha256": "a" * 64,
        "bridge_start_context_sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
    }))
    monkeypatch.setattr(wrapper, "load_context", lambda _root, _path: {
        "manual_release_manifest_sha256": "a" * 64
    })
    argv = ["--bridge-profile", bridge.CONTROL_PROFILE]
    ticket = {
        "schema": bridge.TICKET_SCHEMA,
        "parent_pid": os.getppid(),
        "argv_sha256": wrapper._argv_sha256(argv),
        "launch_id": "1" * 32,
        "scope": wrapper.TICKET_SCOPE,
        "program": bridge.PROGRAM,
        "protocol": bridge.PROTOCOL,
        "wire_protocol": bridge.WIRE_PROTOCOL,
        "control_profile_id": bridge.CONTROL_PROFILE,
        "release_stage_id": bridge.RELEASE_STAGE,
        "manual_release_manifest_sha256": "a" * 64,
        "bridge_start_context": {
            "path": str(context_path),
            "sha256": hashlib.sha256(context_path.read_bytes()).hexdigest(),
        },
        "preflight": {
            "path": str(preflight_path),
            "sha256": hashlib.sha256(preflight_path.read_bytes()).hexdigest(),
        },
    }
    ticket_path = tmp_path / "ticket.json"
    ticket_path.write_text(json.dumps(ticket))
    assert wrapper.strict_ticket(ticket_path, argv)["scope"] == "manual_bridge_no_arm"
    ticket["program"] = "step5d_strict_rnn_autotune_v3_r009"
    ticket_path.write_text(json.dumps(ticket))
    with pytest.raises(bridge.ManualBridgeError, match="identity"):
        wrapper.strict_ticket(ticket_path, argv)


def test_manual_authorization_seam_is_bridge_only_no_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bridge = SimpleNamespace(
        require_v29_live_bridge_authorization=lambda *_args, **_kwargs: None,
        STEP5D_PERMISSIVE_CONTACT_PROFILE_IDS={bridge.CONTROL_PROFILE},
        STEP5D_V31_QDOT_CAP_RAD_S=0.5,
        STEP5D_V31_SENSOR_STALE_S=2.0,
        STEP5D_LINE_ENTRY_PARAM_VALID_CODE=521.0,
    )
    observed: dict[str, object] = {}

    def install(
        ticket,
        *,
        release_identity,
        no_arm_expected_loaded_program,
    ):
        observed.update({
            "ticket": ticket,
            "release_identity": release_identity,
            "loaded_program": no_arm_expected_loaded_program,
        })
        return fake_bridge

    monkeypatch.setattr(wrapper.r009_bridge, "install_v3_seams", install)
    monkeypatch.setattr(
        wrapper.live_driver,
        "BridgeMailboxRuntime",
        wrapper._BASE_BRIDGE_MAILBOX_RUNTIME,
    )
    monkeypatch.setattr(wrapper, "load_context", lambda _root, _path: {
        "arm_authorized": False,
        "motion_authorized": False,
    })
    installed = wrapper.install_manual_seams({
        "scope": wrapper.TICKET_SCOPE,
        "program": bridge.PROGRAM,
        "protocol": bridge.PROTOCOL,
        "wire_protocol": bridge.WIRE_PROTOCOL,
        "bridge_start_context": {"path": "/tmp/manual-context.json", "sha256": "a" * 64},
    })
    result = installed.require_v29_live_bridge_authorization(SimpleNamespace(
        bridge_profile=bridge.CONTROL_PROFILE,
        step5d_autotune_command_mailbox=Path("/tmp/manual-mailbox.json"),
        step5d_stage25_control_mode="speedj_rnn_live",
    ))
    assert result["scope"] == "manual_bridge_no_arm"
    assert result["protocol_id"] == bridge.PROTOCOL
    assert result["wire_protocol_id"] == bridge.WIRE_PROTOCOL
    assert result["live_motion_authorized"] is False
    assert observed["ticket"] is None
    assert observed["loaded_program"] == (
        "/programs/andyl/kunwei/step5/step5d_strict_rnn_manual_tune_v2.urp"
    )
    assert observed["release_identity"].program_id == bridge.PROGRAM


def test_manual_guard_semantics_are_fail_closed_and_diagnostic_only() -> None:
    import kunwei_rtde_bridge as production

    wrapper.require_manual_guard_semantics(production)
    assert bridge.CONTROL_PROFILE in production.STEP5D_PERMISSIVE_CONTACT_PROFILE_IDS
    assert production.STEP5D_LINE_ENTRY_PARAM_VALID_CODE == 521.0
    assert wrapper.MANUAL_HARD_GUARDS == {
        "max_normal_force_n": 60.0,
        "max_force_norm_n": 100.0,
        "max_torque_norm_nm": 3.0,
        "qdot_cap_rad_s": 0.5,
        "sensor_stale_s": 2.0,
    }

    drifted = SimpleNamespace(
        STEP5D_PERMISSIVE_CONTACT_PROFILE_IDS=set(),
        STEP5D_V31_QDOT_CAP_RAD_S=0.5,
        STEP5D_V31_SENSOR_STALE_S=2.0,
        STEP5D_LINE_ENTRY_PARAM_VALID_CODE=521.0,
    )
    with pytest.raises(bridge.ManualBridgeError, match="guard semantics differ"):
        wrapper.require_manual_guard_semantics(drifted)


def _pending_identity_runtime(
    tmp_path: Path,
) -> tuple[wrapper.ManualBridgeMailboxRuntime, object]:
    overlay = campaign.normalize_trial_overlay(
        {
            "force_p_gain": 0.001,
            "force_i_gain": 0.0001,
            "force_damping": 7.0,
            "orientation_ko": 0.4,
            "execution_profile_id": "nf100-slew050-a050",
            "step5d_preload_filtered_min_n": 7.5,
            "step5d_preload_filtered_max_n": 14.0,
            "step5d_preload_raw_min_n": 7.0,
            "step5d_preload_raw_max_n": 15.0,
            "step5d_preload_force_norm_max_n": 25.0,
            "step5d_preload_hold_s": 0.1,
            "step5d_preload_timeout_s": 10.0,
        },
        profile=campaign.load_launch_profile(campaign.DEFAULT_LAUNCH_PROFILE),
    )
    intent = {
        "campaign_id": "manual-commit-test",
        "release_manifest_sha256": "a" * 64,
        "packet": {
            "campaign_epoch": 1,
            "trial_id": 1,
            "command": 1,
            "candidate_token": 123,
            "execution_profile_id": 633,
            "command_seq": 1,
            "logical_batch_sequence": 1,
            "batch_row_index": 1,
        },
        "request_identity": _request_identity(overlay),
        "overlay": overlay,
    }
    packet, prepared = campaign._prepared(intent)
    mailbox_path = (tmp_path / "command.json").resolve()
    mailbox_path.parent.mkdir(parents=True, exist_ok=True)
    sink = campaign.AtomicCommandMailbox(
        mailbox_path,
        network_mode=True,
        launch_profile=campaign.load_launch_profile(campaign.DEFAULT_LAUNCH_PROFILE),
    )
    sink.send_command(packet, prepared_trial=prepared)
    command = sink.read_latest()
    assert command is not None
    runtime = wrapper.ManualBridgeMailboxRuntime(mailbox_path)
    runtime.active = command
    runtime._begin_arm_identity_commit(
        command,
        _identity_snapshot(command, TpLoopState.READY_HOME, 0),
        connection_epoch=0,
    )
    return runtime, command


def _identity_snapshot(
    command: object,
    state: TpLoopState,
    consumed_seq: int,
    *,
    token_delta: int = 0,
) -> TpPacket:
    packet = command.packet
    return TpPacket(
        campaign_epoch_echo=packet.campaign_epoch,
        trial_id_echo=packet.trial_id,
        state=state,
        candidate_token_echo=packet.candidate_token + token_delta,
        terminal_reason=0,
        execution_profile_id_echo=packet.execution_profile_id,
        consumed_command_seq=consumed_seq,
        logical_batch_sequence_echo=packet.logical_batch_sequence,
    )


def _request_identity(overlay: dict, *, sequence: int = 1) -> dict[str, str]:
    overlay_sha = campaign.normalized_overlay_sha256(
        campaign.load_launch_profile(campaign.DEFAULT_LAUNCH_PROFILE), overlay
    )
    control = ControlCandidateUid.parse(overlay["control_candidate_uid"])
    occurrence = OccurrenceUid.from_control(
        control,
        protocol=campaign.PROTOCOL,
        logical_batch_sequence=sequence,
        row_index=1,
        plan_revision=sequence,
        selection_role="test",
        replicate_ordinal=1,
    )
    transport = TransportCandidateUid.from_occurrence(
        occurrence,
        parameter_uid=ParameterUid.from_candidate_digest(overlay_sha),
        protocol=campaign.PROTOCOL,
    )
    return {
        "occurrence_uid": str(occurrence),
        "transport_candidate_uid": str(transport),
        "normalized_overlay_sha256": overlay_sha,
    }


def test_manual_identity_commit_accepts_torn_armed_then_exact_commit(
    tmp_path: Path,
) -> None:
    runtime, command = _pending_identity_runtime(tmp_path)
    runtime._reconcile_snapshot(
        _identity_snapshot(command, TpLoopState.ARMED, 0, token_delta=1),
        durable_command_seq=command.packet.command_seq,
        connection_epoch=0,
    )
    assert runtime.identity_commit_pending is True
    runtime._reconcile_snapshot(
        _identity_snapshot(
            command,
            TpLoopState.ARMED,
            command.packet.command_seq,
        ),
        durable_command_seq=command.packet.command_seq,
        connection_epoch=0,
    )
    assert runtime.identity_commit_pending is False


@pytest.mark.parametrize(
    ("state", "consumed_seq", "token_delta", "message"),
    (
        (TpLoopState.RUN, 0, 0, "RUN before ARM identity commit"),
        (TpLoopState.ARMED, 2, 0, "newer than this bridge mailbox"),
        (TpLoopState.ARMED, 1, 1, "different identity"),
    ),
)
def test_manual_identity_commit_fails_closed(
    tmp_path: Path,
    state: TpLoopState,
    consumed_seq: int,
    token_delta: int,
    message: str,
) -> None:
    runtime, command = _pending_identity_runtime(tmp_path)
    with pytest.raises(mailbox_driver.MailboxError, match=message):
        runtime._reconcile_snapshot(
            _identity_snapshot(
                command,
                state,
                consumed_seq,
                token_delta=token_delta,
            ),
            durable_command_seq=command.packet.command_seq,
            connection_epoch=0,
        )


def test_manual_identity_commit_fails_on_reconnect_timeout_and_regression(
    tmp_path: Path,
) -> None:
    runtime, command = _pending_identity_runtime(tmp_path / "reconnect")
    runtime._poll_connection_epoch = 1
    with pytest.raises(mailbox_driver.MailboxError, match="reconnected during"):
        runtime._reconcile_snapshot(
            _identity_snapshot(command, TpLoopState.ARMED, 0),
            durable_command_seq=command.packet.command_seq,
            connection_epoch=1,
        )

    runtime, command = _pending_identity_runtime(tmp_path / "timeout")
    assert runtime._pending_arm_started_s is not None
    runtime._pending_arm_started_s -= runtime.IDENTITY_COMMIT_TIMEOUT_S + 0.001
    with pytest.raises(mailbox_driver.MailboxError, match="commit timed out"):
        runtime._reconcile_snapshot(
            _identity_snapshot(command, TpLoopState.ARMED, 0),
            durable_command_seq=command.packet.command_seq,
            connection_epoch=0,
        )

    runtime, command = _pending_identity_runtime(tmp_path / "regression")
    runtime._pending_arm_previous_seq = 1
    with pytest.raises(mailbox_driver.MailboxError, match="regressed"):
        runtime._reconcile_snapshot(
            _identity_snapshot(command, TpLoopState.ARMED, 0),
            durable_command_seq=command.packet.command_seq,
            connection_epoch=0,
        )


def test_manual_arm_runtime_applies_exact_i1e4_and_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bridge = SimpleNamespace(STEP5D_V33_ORIENTATION_KO=None)
    prior = SimpleNamespace(
        reaction_normal_b=(0.0, 0.0, 1.0),
        approach_axis_b=(0.0, 0.0, -1.0),
        precontact_rotvec_rad=(0.0, 0.0, 0.0),
        fingerprint="a" * 64,
        load_gate_n=1.0,
        load_gate_dwell_s=0.1,
        normal_rate_limit_rad_s=0.1,
        identity_payload=lambda: {"identity": "test"},
    )
    monkeypatch.setattr(wrapper.r009_bridge, "STEP5D_V3_PHYSICAL_PRIOR", prior)
    monkeypatch.setattr(wrapper, "canonical_sha256", lambda _payload: "a" * 64)
    overlay = dict(campaign.normalize_trial_overlay(
        {
            "force_p_gain": 0.001,
            "force_i_gain": 0.0001,
            "force_damping": 7.0,
            "orientation_ko": 0.4,
            "execution_profile_id": "nf100-slew050-a050",
            "step5d_preload_filtered_min_n": 7.5,
            "step5d_preload_filtered_max_n": 14.0,
            "step5d_preload_raw_min_n": 7.0,
            "step5d_preload_raw_max_n": 15.0,
            "step5d_preload_force_norm_max_n": 25.0,
            "step5d_preload_hold_s": 0.1,
            "step5d_preload_timeout_s": 10.0,
        },
        profile=campaign.load_launch_profile(campaign.DEFAULT_LAUNCH_PROFILE),
    ))
    profile = next(row for row in campaign.NORMAL_FILTER_PROFILES if row.profile_id == "nf100-slew050-a050")
    args = SimpleNamespace()
    wrapper.apply_manual_arm_runtime(fake_bridge, args, SimpleNamespace(
        trial_overlay=overlay,
        profile=profile,
        batch_row_index=1,
        logical_batch_sequence=1,
    ))
    assert args.step5d_autotune_force_i == 0.0001
    assert args.step5d_autotune_control_candidate_uid == overlay["control_candidate_uid"]
    assert args.step5d_autotune_batch_row_index == 1
    assert args.max_normal_force_n == 60.0
    assert args.max_force_norm_n == 100.0
    assert args.max_torque_norm_nm == 3.0
    assert args.step5d_qdot_limit_rad_s == 0.5
    assert args.sensor_stale_s == 2.0


def test_preplay_wait_rejects_nonzero_nonready_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        campaign,
        "validate_bridge",
        lambda *_args, **_kwargs: (
            tmp_path / "runtime/command.json",
            {
                "state": 20,
                "campaign_epoch": 1,
                "trial_id": 1,
                "consumed_command_seq": 1,
                "command": 0,
                "controller_state": 0,
                "safety_mode": 1,
            },
        ),
    )
    args = SimpleNamespace(bridge_output_root=tmp_path, release_manifest_sha256="a" * 64)
    with pytest.raises(campaign.ManualLiveError, match="neither zero nor READY_HOME"):
        campaign._wait_for_ready_home(args, campaign.time.monotonic() + 1.0)


def test_manual_prepared_mailbox_round_trip_i1e4(tmp_path: Path) -> None:
    overlay = campaign.normalize_trial_overlay(
        {
            "force_p_gain": 0.001,
            "force_i_gain": 0.0001,
            "force_damping": 7.0,
            "orientation_ko": 0.4,
            "execution_profile_id": "nf100-slew050-a050",
            "step5d_preload_filtered_min_n": 7.5,
            "step5d_preload_filtered_max_n": 14.0,
            "step5d_preload_raw_min_n": 7.0,
            "step5d_preload_raw_max_n": 15.0,
            "step5d_preload_force_norm_max_n": 25.0,
            "step5d_preload_hold_s": 0.1,
            "step5d_preload_timeout_s": 10.0,
        },
        profile=campaign.load_launch_profile(campaign.DEFAULT_LAUNCH_PROFILE),
    )
    intent = {
        "campaign_id": "manual-test",
        "release_manifest_sha256": "a" * 64,
        "packet": {
            "campaign_epoch": 1,
            "trial_id": 2,
            "command": 1,
            "candidate_token": 123,
            "execution_profile_id": 633,
            "command_seq": 2,
            "logical_batch_sequence": 1,
            "batch_row_index": 1,
        },
            "request_identity": _request_identity(overlay),
        "overlay": overlay,
    }
    packet, prepared = campaign._prepared(intent)
    mailbox_path = (tmp_path / "command.json").resolve()
    mailbox = campaign.AtomicCommandMailbox(
        mailbox_path,
        network_mode=True,
        launch_profile=campaign.load_launch_profile(campaign.DEFAULT_LAUNCH_PROFILE),
    )
    mailbox.send_command(packet, prepared_trial=prepared)
    decoded = mailbox.read_latest()
    assert decoded is not None
    assert decoded.packet.command_seq == 2
    assert decoded.binding.candidate.force_i_gain == 0.0001
    assert decoded.binding.trial_overlay["control_candidate_uid"] == overlay["control_candidate_uid"]
