from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_live_driver import BridgeMailboxRuntime, BridgeTrialCsvRotator
from step5d_autotune_state_machine import TpLoopState
from step5d_autotune_v2.bridge import BridgeError, LiveWriterLock
from step5d_autotune_v2.live_adapter import Step5dAutotuneV2LiveAdapter
from step5d_autotune_v2.mailbox import AtomicMailbox
from step5d_autotune_v2.model import BatchSpec, CandidateSpec, DeploymentSpec
from step5d_autotune_v2.reducer import LifecycleEvent
from step5d_autotune_v2.repository import Repository
from step5d_autotune_v2.runtime import JsonlBridgePort
from step5d_autotune_v2.supervisor import ArtifactSeal
from run_step5d_autotune_v2_bridge import bridge_argv, bridge_environment


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
    runtime = BridgeMailboxRuntime(
        mailbox_path,
        campaign_home_reference_path=runtime_root / "campaign_home_reference.json",
        mailbox=adapter.command_mailbox,
    )
    args = SimpleNamespace()
    ready = _output(TpLoopState.READY_HOME, None, consumed=0)
    assert runtime.poll(args, ready) is True
    arm = runtime.active
    assert arm is not None
    assert arm.packet.command_seq == arm.packet.trial_id
    assert arm.packet.execution_profile_id == 533
    assert arm.sha256 == arm_evidence["mailbox_checksum"]
    rotator = BridgeTrialCsvRotator((runtime_root / "trials").resolve(), ["sample"])

    for index, state in enumerate((TpLoopState.ARMED, TpLoopState.RUN), start=1):
        output = _output(state, arm, consumed=arm.packet.command_seq)
        runtime.poll(args, output)
        rotator.observe({"sample": index}, active=runtime.active, rtde_output=output)
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
    rotator.observe({"sample": 3}, active=runtime.active, rtde_output=wait_ack)
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
    assert json.loads((runtime_root / "bridge_ready.json").read_text())[
        "startup_stationary_verified"
    ] is True


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
    }
    assert {flag: argv[argv.index(flag) + 1] for flag in expected} == expected
    assert argv[argv.index("--step5d-autotune-command-mailbox") + 1] == str(
        tmp_path.resolve() / "command.json"
    )


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
