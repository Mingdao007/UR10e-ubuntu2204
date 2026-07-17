from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_live_driver import (
    BridgeMailboxRuntime,
    BridgeTrialCsvRotator,
    MailboxError,
    TpFeedbackDecoder,
    TpFeedbackPhase,
    tp_packet_from_rtde,
)
from step5d_autotune_state_machine import TpLoopState
from step5d_autotune_v2.bridge import BridgeError, LiveWriterLock
from step5d_autotune_v2.live_adapter import LiveAdapterError, Step5dAutotuneV2LiveAdapter
from step5d_autotune_v2.mailbox import AtomicMailbox
from step5d_autotune_v2.model import BatchSpec, CandidateSpec, DeploymentSpec
from step5d_autotune_v2.reducer import LifecycleEvent
from step5d_autotune_v2.repository import Repository
from step5d_autotune_v2.runtime import JsonlBridgePort
from step5d_autotune_v2.supervisor import ArtifactSeal
import run_step5d_autotune_v2_bridge as bridge_launcher
from run_step5d_autotune_v2_bridge import bridge_argv, bridge_environment
import kunwei_rtde_bridge as live_bridge


def _deployment() -> DeploymentSpec:
    return DeploymentSpec(
        deployment_id="step5d-autotune-v2-live-test",
        code_fingerprint="1" * 64,
        tp_fingerprint="2" * 64,
        guard_fingerprint="3" * 64,
        profile={
            "normal_max_rate_rad_s": "0.05",
            "host_qdot_slew_rad_s2": "0.5",
            "tp_speedj_accel_rad_s2": "0.5",
            "qdot_cap_rad_s": "0.5",
        },
        deployment_authorized=True,
        controller_readback_verified=True,
    )


def _output(state: TpLoopState, command, *, consumed: int, reason: int = 0) -> dict:
    active = command is not None and state is not TpLoopState.READY_HOME
    return {
        "timestamp": 100.0 + consumed,
        "safety_mode": 1,
        "actual_TCP_pose": [0.1, 0.2, 0.3, 3.141592653589793, 0.0, 0.0],
        "actual_TCP_speed": [0.0] * 6,
        "actual_q": [0.1, -0.2, 0.3, -0.4, 0.5, -0.6],
        "actual_qd": [0.0] * 6,
        "output_int_register_24": command.packet.campaign_epoch if active else 0,
        "output_int_register_25": command.packet.trial_id if active else 0,
        "output_int_register_26": int(state),
        "output_int_register_27": command.packet.candidate_token if active else 0,
        "output_int_register_28": reason,
        "output_int_register_29": command.packet.execution_profile_id if active else 0,
        "output_int_register_30": consumed,
    }


def _stopped_program_output() -> dict:
    output = _output(TpLoopState.READY_HOME, None, consumed=0)
    output["output_int_register_26"] = 0
    output["runtime_state"] = 1
    return output


def test_v2_adapter_drives_exact_arm_ack_and_event_lifecycle(tmp_path: Path) -> None:
    repo = Repository((tmp_path / "control.sqlite3").resolve())
    repo.initialize()
    deployment = _deployment()
    repo.register_deployment(deployment)
    candidates = tuple(
        CandidateSpec.from_mapping(
            {
                "group_id": f"G{12 + index}",
                "p": "0.001",
                "log2_p": "0",
                "i": i_value,
                "d": "7",
                "log2_d": "0",
            }
        )
        for index, i_value in enumerate(("0", "0.00001", "0.0001", "0.0005", "0.001"))
    )
    candidate = candidates[0]
    repo.enqueue_batch(BatchSpec("adapter", "test", candidates), deployment_id=deployment.deployment_id)
    trial_id = repo.create_trial(
        candidate_id=candidate.candidate_id, deployment_id=deployment.deployment_id
    )
    repo.apply_lifecycle_event(trial_id, LifecycleEvent.PERSIST_ARM)
    runtime_root = (tmp_path / "runtime").resolve()
    runtime_root.mkdir()
    mailbox_path = runtime_root / "command.json"
    port = JsonlBridgePort(
        repository=repo,
        mailbox=AtomicMailbox(mailbox_path),
        event_path=runtime_root / "bridge_events.jsonl",
        allowed_artifact_root=runtime_root,
        deployment_id=deployment.deployment_id,
        analyzer_argv=(),
        command_root=ROOT,
        transfer_destination=None,
    )
    arm_evidence = port.publish_arm(
        trial_id=trial_id,
        sequence=int(repo.trial_detail(trial_id)["arm_sequence"]),
        candidate=repo.candidate(candidate.candidate_id),
    )
    adapter = Step5dAutotuneV2LiveAdapter(
        mailbox_path=mailbox_path,
        runtime_root=runtime_root,
        deployment_id=deployment.deployment_id,
        launch_nonce="a" * 64,
    )
    # This test begins at the post-startup ARM lifecycle boundary.  Dedicated
    # startup tests below prove the pre-ARM gate itself.
    adapter.startup_gate_passed = True
    runtime = BridgeMailboxRuntime(
        mailbox_path,
        campaign_home_reference_path=runtime_root / "campaign_home_reference.json",
        mailbox=adapter.command_mailbox,
    )
    args = SimpleNamespace()
    ready = _stopped_program_output()
    assert runtime.poll(args, ready) is True
    arm = runtime.active
    assert arm is not None
    assert arm.packet.command_seq == arm.packet.trial_id
    assert arm.packet.execution_profile_id == 533
    assert arm.sha256 == arm_evidence["mailbox_checksum"]
    rotator = BridgeTrialCsvRotator((runtime_root / "trials").resolve(), ["sample"])
    adapter.observe(
        runtime=runtime,
        rotator=rotator,
        output=ready,
        sample_counter=0,
    )

    running_zero = dict(ready, runtime_state=2)
    assert runtime.poll(args, running_zero, observed_at_s=0.1) is False
    assert runtime.active is arm
    assert args.step5d_autotune_handshake == arm.handshake
    adapter.observe(
        runtime=runtime,
        rotator=rotator,
        output=running_zero,
        sample_counter=1,
    )
    assert not (runtime_root / "bridge_events.jsonl").exists()

    for index, state in enumerate((TpLoopState.ARMED, TpLoopState.RUN), start=2):
        output = _output(state, arm, consumed=arm.packet.command_seq)
        runtime.poll(args, output)
        rotator.observe(
            {"sample": index},
            active=runtime.active,
            observation=runtime.latest_tp_observation,
        )
        adapter.observe(
            runtime=runtime,
            rotator=rotator,
            output=output,
            sample_counter=index,
        )

    wait_ack = _output(
        TpLoopState.WAIT_ACK,
        arm,
        consumed=arm.packet.command_seq,
        reason=1,
    )
    assert runtime.poll(args, wait_ack) is False
    rotator.observe(
        {"sample": 3},
        active=runtime.active,
        observation=runtime.latest_tp_observation,
    )
    adapter.observe(runtime=runtime, rotator=rotator, output=wait_ack, sample_counter=3)
    assert rotator.sealed_path is not None
    seal = ArtifactSeal(rotator.sealed_path, "f" * 64)
    ack_sequence = arm.packet.command_seq + 1
    ack_evidence = port.publish_ack(
        trial_id=trial_id, sequence=ack_sequence, artifact=seal
    )
    assert runtime.poll(args, wait_ack) is True
    assert runtime.last_command is not None
    assert runtime.last_command.sha256 == ack_evidence["mailbox_checksum"]
    ready_after_ack = _output(TpLoopState.READY_HOME, None, consumed=ack_sequence)
    runtime.poll(args, ready_after_ack)
    adapter.observe(
        runtime=runtime,
        rotator=rotator,
        output=ready_after_ack,
        sample_counter=4,
    )
    events = [
        json.loads(line)
        for line in (runtime_root / "bridge_events.jsonl").read_text().splitlines()
    ]
    assert [row["event"] for row in events] == [
        "tp_consumed",
        "run_started",
        "home_verified",
        "raw_capture_sealed",
        "ready_home",
    ]
    assert all(row["trial_id"] == trial_id for row in events)
    assert events[-1]["command_sequence"] == ack_sequence
    assert json.loads((runtime_root / "bridge_health.json").read_text())[
        "command_transport_healthy"
    ] is True


def test_inactive_ready_sentinel_requires_stopped_runtime_and_zero_identity() -> None:
    stopped = _stopped_program_output()
    assert tp_packet_from_rtde(stopped).state is TpLoopState.READY_HOME

    running = dict(stopped, runtime_state=2)
    with pytest.raises(MailboxError, match="unknown loop state"):
        tp_packet_from_rtde(running)

    nonzero_identity = dict(stopped, output_int_register_24=1)
    with pytest.raises(MailboxError, match="unknown loop state"):
        tp_packet_from_rtde(nonzero_identity)


def test_incident_play_startup_transition_is_bounded_and_stateful() -> None:
    fixture = json.loads(
        (ROOT / "tests/fixtures/step5d_autotune_v2_play_startup_transition.json").read_text()
    )
    decoder = TpFeedbackDecoder()
    observations = [
        decoder.observe(sample["output"], observed_at_s=sample["monotonic_s"])
        for sample in fixture["accepted_sequence"]
    ]
    assert [observation.phase for observation in observations] == [
        TpFeedbackPhase.PREPLAY,
        TpFeedbackPhase.STARTING,
        TpFeedbackPhase.STARTING,
        TpFeedbackPhase.ACTIVE,
    ]
    assert observations[-1].packet is not None
    assert observations[-1].packet.state is TpLoopState.READY_HOME


@pytest.mark.parametrize("baseline_state", (0, 10))
@pytest.mark.parametrize("include_running_zero", (False, True))
def test_operator_ready_and_same_pid_startup_gate_cover_all_restart_paths(
    tmp_path: Path,
    baseline_state: int,
    include_running_zero: bool,
) -> None:
    mailbox_path = (tmp_path / "command.json").resolve()
    adapter = Step5dAutotuneV2LiveAdapter(
        mailbox_path=mailbox_path,
        runtime_root=tmp_path.resolve(),
        deployment_id="deployment-r13",
        launch_nonce="c" * 64,
        startup_stable_s=0.5,
    )
    decoder = TpFeedbackDecoder()
    baseline = _stopped_program_output()
    if baseline_state == 10:
        baseline["output_int_register_26"] = 10
    observation = decoder.observe(baseline, observed_at_s=0.0)
    adapter.publish_startup(
        observation=observation,
        output=baseline,
        sample_counter=1,
        infrastructure_ready=False,
        feedback_age_s=0.0,
        observed_at_s=0.0,
    )
    assert not (tmp_path / "bridge_ready.json").exists()
    adapter.publish_startup(
        observation=observation,
        output=baseline,
        sample_counter=2,
        infrastructure_ready=True,
        feedback_age_s=0.0,
        observed_at_s=0.01,
    )
    awaiting = json.loads((tmp_path / "tp_startup.json").read_text())
    assert awaiting["phase"] == "awaiting_tp_play"
    assert awaiting["operator_action"] == "press_tp_play"
    assert awaiting["startup_gate_passed"] is False

    if include_running_zero:
        running_zero = dict(_stopped_program_output(), runtime_state=2)
        starting = decoder.observe(running_zero, observed_at_s=0.1)
        adapter.publish_startup(
            observation=starting,
            output=running_zero,
            sample_counter=3,
            infrastructure_ready=True,
            feedback_age_s=0.0,
            observed_at_s=0.1,
        )

    active = _output(TpLoopState.READY_HOME, None, consumed=0)
    active["runtime_state"] = 2
    first_active = decoder.observe(active, observed_at_s=0.2)
    adapter.publish_startup(
        observation=first_active,
        output=active,
        sample_counter=4,
        infrastructure_ready=True,
        feedback_age_s=0.0,
        observed_at_s=0.2,
    )
    almost_stable = decoder.observe(active, observed_at_s=0.699999)
    adapter.publish_startup(
        observation=almost_stable,
        output=active,
        sample_counter=5,
        infrastructure_ready=True,
        feedback_age_s=0.0,
        observed_at_s=0.699999,
    )
    assert json.loads((tmp_path / "tp_startup.json").read_text())["phase"] == "stabilizing"
    stable_active = decoder.observe(active, observed_at_s=0.700001)
    adapter.publish_startup(
        observation=stable_active,
        output=active,
        sample_counter=6,
        infrastructure_ready=True,
        feedback_age_s=0.0,
        observed_at_s=0.700001,
    )
    passed = json.loads((tmp_path / "tp_startup.json").read_text())
    assert passed["phase"] == "live"
    assert passed["startup_gate_passed"] is True
    assert passed["motion_allowed"] is False
    assert adapter.command_mailbox.read_latest() is None


def test_running_zero_startup_rejects_missing_preplay_timeout_and_active_regression() -> None:
    running_zero = dict(_stopped_program_output(), runtime_state=2)
    with pytest.raises(MailboxError, match="preceding stopped"):
        TpFeedbackDecoder().observe(running_zero, observed_at_s=0.0)

    timeout_decoder = TpFeedbackDecoder()
    timeout_decoder.observe(_stopped_program_output(), observed_at_s=0.0)
    timeout_decoder.observe(running_zero, observed_at_s=0.1)
    with pytest.raises(MailboxError, match="exceeded 1.0 s"):
        timeout_decoder.observe(running_zero, observed_at_s=1.100001)

    active_decoder = TpFeedbackDecoder()
    active_decoder.observe(_stopped_program_output(), observed_at_s=0.0)
    active_decoder.observe(running_zero, observed_at_s=0.1)
    active_decoder.observe(_output(TpLoopState.READY_HOME, None, consumed=0), observed_at_s=0.2)
    with pytest.raises(MailboxError, match="preceding stopped"):
        active_decoder.observe(running_zero, observed_at_s=0.3)


def test_adapter_ignores_only_bootstrap_mailbox_from_prior_deployment(tmp_path: Path) -> None:
    mailbox_path = (tmp_path / "command.json").resolve()
    mailbox = AtomicMailbox(mailbox_path)
    mailbox.publish(
        sequence=1,
        payload={"command": "ARM", "deployment_id": "prior-deployment"},
    )
    adapter = Step5dAutotuneV2LiveAdapter(
        mailbox_path=mailbox_path,
        runtime_root=tmp_path.resolve(),
        deployment_id="current-deployment",
        launch_nonce="b" * 64,
    )
    assert adapter.command_mailbox.read_latest() is None

    adapter.command_mailbox.current_deployment_seen = True
    with pytest.raises(LiveAdapterError, match="deployment identity differs"):
        adapter.command_mailbox.read_latest()


def test_campaign_home_reference_is_namespaced_by_deployment(tmp_path: Path) -> None:
    first = SimpleNamespace(deployment_id="deployment-r1")
    second = SimpleNamespace(deployment_id="deployment-r2")
    first_path = live_bridge.step5d_campaign_home_reference_path(tmp_path, first)
    second_path = live_bridge.step5d_campaign_home_reference_path(tmp_path, second)
    assert first_path.parent == tmp_path
    assert second_path.parent == tmp_path
    assert first_path != second_path
    assert first_path.name.startswith("campaign_home_reference.")


def test_global_live_writer_lock_fails_closed(tmp_path: Path) -> None:
    path = (tmp_path / "locks/live-writer.lock").resolve()
    first = LiveWriterLock(path)
    second = LiveWriterLock(path)
    first.acquire()
    try:
        with pytest.raises(BridgeError, match="another live writer"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_same_pid_launcher_snapshot_is_frozen(tmp_path: Path) -> None:
    argv = bridge_argv(ROOT, tmp_path.resolve())
    assert argv[0] == sys.executable
    assert argv[1].endswith("tools/kunwei_rtde_bridge.py")
    assert "chrt" not in argv
    expected = {
        "--rtde-hz": "500",
        "--target-force-n": "12",
        "--max-normal-force-n": "60",
        "--max-force-norm-n": "100",
        "--max-torque-norm-nm": "3",
        "--step5d-qdot-limit-rad-s": "0.5",
        "--step5d-rnn-backend": "cupy",
        "--step5d-autotune-normal-rate-rad-s": "0.05",
        "--step5d-autotune-host-slew-rad-s2": "0.5",
        "--step5d-autotune-speedj-acceleration-rad-s2": "0.5",
        "--duration-s": "360",
        "--dashboard-program-watch-timeout-s": "120",
    }
    assert {flag: argv[argv.index(flag) + 1] for flag in expected} == expected
    assert argv[argv.index("--step5d-autotune-command-mailbox") + 1] == str(
        tmp_path.resolve() / "command.json"
    )
    offset_index = argv.index("--step5d-tcp-offset-tool0-m")
    assert argv[offset_index + 1 : offset_index + 4] == ["0", "0", "0.1221"]


def test_same_pid_launcher_fails_closed_without_exact_startup_gate_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "STEP5D_AUTOTUNE_V2_ADAPTER",
        "STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID",
        "STEP5D_AUTOTUNE_V2_LAUNCH_NONCE",
        "STEP5D_AUTOTUNE_V2_STARTUP_GATE_REQUIRED",
        "STEP5D_AUTOTUNE_V2_STARTUP_STABLE_S",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("STEP5D_AUTOTUNE_V2_RUNTIME_ROOT", str(tmp_path.resolve()))
    monkeypatch.setenv("STEP5D_AUTOTUNE_V2_ADAPTER", "1")
    monkeypatch.setenv("STEP5D_AUTOTUNE_V2_DEPLOYMENT_ID", "deployment-r13")
    monkeypatch.setenv("STEP5D_AUTOTUNE_V2_LAUNCH_NONCE", "d" * 64)
    monkeypatch.setenv("STEP5D_AUTOTUNE_V2_STARTUP_STABLE_S", "0.5")
    with pytest.raises(SystemExit, match="environment is incomplete"):
        bridge_launcher.main([])

    monkeypatch.setenv("STEP5D_AUTOTUNE_V2_STARTUP_GATE_REQUIRED", "1")
    monkeypatch.setenv("STEP5D_AUTOTUNE_V2_STARTUP_STABLE_S", "0.4")
    with pytest.raises(SystemExit, match="environment is incomplete"):
        bridge_launcher.main([])


def test_frozen_launcher_offset_prewarms_without_archived_runtime_csv(tmp_path: Path) -> None:
    argv = bridge_argv(ROOT, tmp_path.resolve())
    args = live_bridge.parse_args(argv[2:])
    state = live_bridge.BridgeState()
    model_bundle = SimpleNamespace(
        model=SimpleNamespace(
            lowerPositionLimit=np.full(6, -3.14),
            upperPositionLimit=np.full(6, 3.14),
        )
    )
    with (
        patch.object(live_bridge.step5d_kin, "build_calibrated_model", return_value=model_bundle),
        patch.object(
            live_bridge.step5d_kin,
            "finite_run_rows",
            side_effect=AssertionError("production prewarm must not read an archived runtime CSV"),
        ),
        patch.object(
            live_bridge,
            "StrictTaseRnnSolver",
            return_value=SimpleNamespace(reset_state=lambda: None),
        ),
    ):
        live_bridge.ensure_step5d_liveprep_runtime(state, args)

    assert np.array_equal(state.step5d_tcp_offset_tool0, np.array([0.0, 0.0, 0.1221]))


def test_launcher_constructs_runtime_paths_from_empty_environment() -> None:
    environment = bridge_environment({})
    assert environment["PYTHONPATH"].split(":")[:3] == [
        "/home/andy/.codex-python/ur10e-digital-twin-20260711",
        "/opt/ros/humble/lib/python3.10/site-packages",
        "/opt/ros/humble/local/lib/python3.10/dist-packages",
    ]
    assert "nvidia/cuda_nvrtc/lib" in environment["LD_LIBRARY_PATH"]
    assert environment["LD_LIBRARY_PATH"].split(":")[-1] == "/opt/ros/humble/lib"
    assert environment["AMENT_PREFIX_PATH"] == "/opt/ros/humble"
    assert environment["CMAKE_PREFIX_PATH"] == "/opt/ros/humble"
    assert environment["ROS_DISTRO"] == "humble"
