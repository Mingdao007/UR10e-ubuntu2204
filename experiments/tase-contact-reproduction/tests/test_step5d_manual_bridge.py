from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import time

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


def test_manual_production_path_has_no_capability_authorization_gate() -> None:
    sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "scripts/step5d-autotune-v3.sh",
            ROOT / "tools/run_step5d_manual_live_campaign.py",
            ROOT / "tools/run_step5d_manual_bridge.py",
            ROOT / "tools/step5d_manual_status.py",
        )
    )
    assert "step5d_manual_authorization" not in sources
    assert "manual_capability_authorization" not in sources
    assert "--authorization-file" not in sources
    assert not (ROOT / "tools/step5d_manual_authorization.py").exists()


def test_manual_owner_and_bridge_die_with_their_bound_parent() -> None:
    owner = (ROOT / "tools/run_step5d_manual_bridge_live.py").read_text(
        encoding="utf-8"
    )
    shell = (ROOT / "scripts/step5d-autotune-v3.sh").read_text(encoding="utf-8")

    assert "PR_SET_PDEATHSIG" in owner
    assert "preexec_fn=" in owner
    assert '--canonical-owner-pid "$$"' in shell
    assert '--canonical-owner-starttime "${launch_owner_starttime}"' in shell


def test_manual_runner_reaches_waiting_for_play_without_authorization_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_states: list[str] = []
    observed_contracts: list[Path] = []
    monkeypatch.setenv("STEP5D_V3_LAUNCH_ATTEMPT_ID", "attempt-no-arm")
    monkeypatch.setattr(
        campaign,
        "_release_contract_reference",
        lambda args: observed_contracts.append(args.release_contract_certificate)
        or {"path": str(args.release_contract_certificate), "sha256": "a" * 64},
    )
    monkeypatch.setattr(campaign, "seed_initial_grid", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        campaign,
        "load_queue",
        lambda _path: {"requests": [{"source": "initial:G01"}]},
    )
    monkeypatch.setattr(
        campaign,
        "_publish_status",
        lambda _args, **kwargs: observed_states.append(kwargs["state"]) or {},
    )
    monkeypatch.setattr(
        campaign,
        "validate_bridge",
        lambda *_args, **_kwargs: (tmp_path / "command.json", {}),
    )
    monkeypatch.setattr(
        campaign,
        "_observe_controller_identity",
        lambda *_args, **_kwargs: {
            "observed_at_unix_ns": 1,
            "loaded_program_response": f"Loaded program: {campaign.EXPECTED_PROGRAM}",
            "program_state": "STOPPED",
            "program_state_normalized": "STOPPED",
            "safety_mode": "Safetymode: NORMAL",
            "safety_mode_normalized": "NORMAL",
            "expected_loaded_program": campaign.EXPECTED_PROGRAM,
        },
    )
    monkeypatch.setattr(
        campaign, "_publish_canonical_readiness_claim", lambda *_args: {}
    )
    monkeypatch.setattr(
        campaign,
        "_wait_for_ready_home",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            campaign.ManualLiveError("test stop at Play barrier")
        ),
    )
    args = SimpleNamespace(
        campaign_root=tmp_path / "campaign",
        release_contract_certificate=tmp_path / "release-contract.json",
        release_manifest_sha256="a" * 64,
        queue=tmp_path / "queue.json",
        launch_profile=campaign.DEFAULT_LAUNCH_PROFILE,
        state=tmp_path / "state.json",
        campaign_id="manual-no-arm",
        bridge_output_root=tmp_path / "bridge",
        play_timeout_s=0.01,
        robot_host="127.0.0.1",
    )

    with pytest.raises(campaign.ManualLiveError, match="test stop at Play barrier"):
        campaign.run(args)

    assert observed_contracts == [tmp_path / "release-contract.json"]
    assert observed_states == ["WAITING_FOR_PLAY"]


def test_manual_readiness_claim_is_machine_derived_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine_status = {"state": "WAITING_FOR_PLAY"}
    expected_claim = {
        "schema": "step5d.bridge/readiness-claim-v1",
        "attempt_id": "attempt-claim",
    }
    monkeypatch.setattr(
        campaign, "resolve_bridge_status", lambda _root: machine_status
    )
    monkeypatch.setattr(
        campaign,
        "readiness_claim",
        lambda status, state: expected_claim
        if status is machine_status and state == "WAITING_FOR_PLAY"
        else pytest.fail("Manual readiness claim inputs differ"),
    )
    monkeypatch.setattr(
        campaign,
        "verify_readiness_claim",
        lambda status, claim: claim
        if status is machine_status and claim is expected_claim
        else pytest.fail("Manual readiness claim verification inputs differ"),
    )
    output = tmp_path / "bridge"
    output.mkdir()

    claim = campaign._publish_canonical_readiness_claim(
        SimpleNamespace(bridge_output_root=output),
        "WAITING_FOR_PLAY",
    )

    assert claim == expected_claim
    assert json.loads((output / "readiness-claim.json").read_text()) == expected_claim


def test_manual_controller_preflight_requires_exact_loaded_stopped_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        campaign,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {
            "programState": "STOPPED",
            "safetymode": "Safetymode: NORMAL",
            "get loaded program": f"Loaded program: {campaign.EXPECTED_PROGRAM}",
        },
    )
    observed = campaign._observe_controller_identity("192.0.2.1")
    assert observed["expected_loaded_program"] == campaign.EXPECTED_PROGRAM

    monkeypatch.setattr(
        campaign,
        "dashboard_exchange",
        lambda *_args, **_kwargs: {
            "programState": "STOPPED",
            "safetymode": "Safetymode: NORMAL",
            "get loaded program": "Loaded program: /programs/wrong.urp",
        },
    )
    with pytest.raises(campaign.ManualLiveError, match="preflight handoff"):
        campaign._observe_controller_identity("192.0.2.1")


def test_manual_running_state_requires_exact_tp_arm_acknowledgement() -> None:
    arm = campaign.HostPacket(
        campaign_epoch=1,
        trial_id=2,
        command=campaign.HostCommand.ARM,
        candidate_token=3,
        execution_profile_id=633,
        command_seq=4,
        logical_batch_sequence=5,
    )
    observed = {
        "campaign_epoch": 1,
        "trial_id": 2,
        "state": int(campaign.TpLoopState.ARMED),
        "candidate_token": 3,
        "execution_profile_id": 633,
        "consumed_command_seq": 4,
        "logical_batch_sequence": 5,
        "batch_row_index": 1,
    }
    assert campaign._arm_acknowledged(observed, arm) is True
    assert campaign._arm_acknowledged({**observed, "consumed_command_seq": 3}, arm) is False
    assert campaign._arm_acknowledged(
        {**observed, "state": int(campaign.TpLoopState.READY_HOME)}, arm
    ) is False


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
    return {
        "manifest_sha256": "a" * 64,
        "source_surface_sha256": "4" * 64,
    }, manifest


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


def test_preflight_rejects_already_playing_program() -> None:
    playing = preflight._program_safe(
        {
            "programState": "PLAYING",
            "get loaded program": (
                "Loaded program: /programs/andyl/kunwei/step5/"
                "step5d_strict_rnn_manual_tune_v2.urp"
            ),
        },
        {
            "output_int_register_26": 10,
            **{f"output_int_register_{index}": 0 for index in (24, 25, 27, 28, 29, 30)},
        },
    )
    assert playing["ok"] is False
    assert playing["mode"] == "not_stopped"


def test_live_preflight_validation_is_no_arm_and_exact() -> None:
    predicates = {
        name: {"ok": True}
        for name in (
            "safety_normal", "program_safe_for_bridge", "robot_stationary",
            "no_existing_writer", "mailbox_initial_zero",
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
        "launch_attempt_id": "attempt-ticket",
        "campaign_id": "manual-ticket",
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
    production_contract = SimpleNamespace(
        STEP5D_PERMISSIVE_CONTACT_PROFILE_IDS={bridge.CONTROL_PROFILE},
        STEP5D_V31_QDOT_CAP_RAD_S=0.5,
        STEP5D_V31_SENSOR_STALE_S=2.0,
        STEP5D_LINE_ENTRY_PARAM_VALID_CODE=521.0,
    )

    wrapper.require_manual_guard_semantics(production_contract)
    assert bridge.CONTROL_PROFILE in production_contract.STEP5D_PERMISSIVE_CONTACT_PROFILE_IDS
    assert production_contract.STEP5D_LINE_ENTRY_PARAM_VALID_CODE == 521.0
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


def test_manual_runtime_uses_full_home_protocol_and_command_bound_arm_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = "attempt-manual-gate"
    campaign_id = "manual-gate-campaign"
    release_sha = "a" * 64
    monkeypatch.setenv("STEP5D_V3_LAUNCH_ATTEMPT_ID", attempt_id)
    wrapper.ManualBridgeMailboxRuntime.configure(
        {
            "manual_release_manifest_sha256": release_sha,
            "launch_attempt_id": attempt_id,
            "campaign_id": campaign_id,
        }
    )
    mailbox = (tmp_path / "runtime/command.json").resolve()
    mailbox.parent.mkdir(parents=True)
    runtime = wrapper.ManualBridgeMailboxRuntime(mailbox)
    assert runtime.completion_protocol == bridge.WIRE_PROTOCOL
    assert isinstance(runtime.arming_context_provider, wrapper.ManualArmGateProvider)

    now_ns = time.time_ns()
    binding = {
        "mailbox_sha256": "b" * 64,
        "campaign_epoch": 1,
        "trial_id": 1,
        "command": 1,
        "candidate_token": 2,
        "execution_profile_id": 633,
        "command_seq": 1,
        "logical_batch_sequence": 1,
        "trial_uid": "trial-1",
    }
    gate_path = mailbox.parent / "manual_arm_gate.json"
    gate_path.write_text(
        json.dumps(
            {
                "schema": wrapper.ARM_GATE_SCHEMA,
                "attempt_id": attempt_id,
                "campaign_id": campaign_id,
                "release_manifest_sha256": release_sha,
                "created_at_unix_ns": now_ns,
                "arm_binding": binding,
            }
        ),
        encoding="utf-8",
    )
    admitted = runtime.arming_context_provider(binding, connection_epoch=3)
    assert admitted is not None
    assert admitted["arm_binding"] == binding
    assert runtime.arming_context_provider(
        {**binding, "command_seq": 2}, connection_epoch=3
    ) is None


def _pending_identity_runtime(
    tmp_path: Path,
) -> tuple[wrapper.ManualBridgeMailboxRuntime, object]:
    wrapper.ManualBridgeMailboxRuntime.configure(
        {
            "manual_release_manifest_sha256": "a" * 64,
            "launch_attempt_id": "attempt-commit-test",
            "campaign_id": "manual-commit-test",
        }
    )
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
    args = SimpleNamespace(
        bridge_output_root=tmp_path,
        release_manifest_sha256="a" * 64,
        robot_host="192.0.2.1",
    )
    with pytest.raises(campaign.ManualLiveError, match="neither zero nor READY_HOME"):
        campaign._wait_for_ready_home(
            args,
            campaign.time.monotonic() + 1.0,
        )


def test_preplay_wait_accepts_ready_home_without_dashboard_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        campaign,
        "validate_bridge",
        lambda *_args, **_kwargs: (
            tmp_path / "runtime/command.json",
            {
                "state": campaign.READY_HOME,
                "campaign_epoch": 1,
                "trial_id": 0,
                "consumed_command_seq": 0,
                "command": 0,
                "controller_state": 0,
                "safety_mode": 1,
            },
        ),
    )
    monkeypatch.setattr(
        campaign,
        "_observe_controller_identity",
        lambda *_args, **_kwargs: pytest.fail(
            "pre-Play wait must not re-read Dashboard"
        ),
    )
    args = SimpleNamespace(
        bridge_output_root=tmp_path,
        release_manifest_sha256="a" * 64,
        robot_host="192.0.2.1",
        campaign_id="manual-edge",
    )
    observed = campaign._wait_for_ready_home(
        args,
        campaign.time.monotonic() + 1.0,
    )
    assert observed["state"] == campaign.READY_HOME


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
