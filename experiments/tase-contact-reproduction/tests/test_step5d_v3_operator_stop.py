from __future__ import annotations

import csv
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Mapping

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_v3_operator_stop as stop  # noqa: E402
from step5d_autotune_v3.runtime_gate import (  # noqa: E402
    CampaignLease,
    process_starttime,
    write_campaign_lease,
)
from step5d_parameter_queue import (  # noqa: E402
    bind_home,
    finish_dispatch,
    initialize,
    prepare_next_dispatch,
    record_terminal_receipt,
    submit,
)


RELEASE_SHA = "9adf3791ec322bacbddce0c16a7fcfd588c9f232cde3ccb438baa9e9c97e2df5"
OVERLAY_SHA = "8699b99bfbb5bbe5ee09df365dd6481d75c0b163b63f58dd0d75854d1148833b"
CONTROL_UID = "control:v2:3df8557646d32e8374092d8b5eac85e49155390b818391550ca3be1d98bfc6bc"
SOURCE = "approved_initial_10:P01"
TARGET = stop.EXPECTED_CONTROLLER_TARGET
PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"


def _release() -> SimpleNamespace:
    return SimpleNamespace(
        program_id=stop.EXPECTED_PROGRAM_ID,
        release_stage_id="step5d_strict_rnn_autotune_v3",
        controller_target=TARGET,
        manifest_sha256=RELEASE_SHA,
        source_fingerprints={"scripts/step5d-autotune-v3.sh": "b" * 64},
    )


def _queue(tmp_path: Path, *, complete: bool = True) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path / "queue"
    initialize(
        root,
        campaign_id="campaign-test",
        release_manifest_sha256=RELEASE_SHA,
        launch_profile_path=PROFILE,
    )
    request = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.0008408964152537145,
        force_i=1e-05,
        force_damping=7,
        normal_filter_tau_s=0.35,
        orientation_ko=0.4,
        motion_kp=1.5,
        source=SOURCE,
        position="next",
    )
    assert request["normalized_overlay_sha256"] == OVERLAY_SHA
    assert request["control_candidate_uid"] == CONTROL_UID
    assert request["source"] == SOURCE
    assert request["overlay"]["force_p_gain"] == 0.0008408964152537145
    assert request["overlay"]["force_i_gain"] == 1e-05
    assert request["overlay"]["force_damping"] == 7
    assert request["overlay"]["normal_filter_tau_s"] == 0.35
    assert request["overlay"]["orientation_ko"] == 0.4
    assert request["overlay"]["motion_kp"] == 1.5
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    terminal = {
        "campaign_epoch": dispatch["packet"]["campaign_epoch"],
        "trial_id": dispatch["packet"]["trial_id"],
        "state": 78,
        "candidate_token": dispatch["packet"]["candidate_token"],
        "execution_profile_id": dispatch["packet"]["execution_profile_id"],
        "consumed_command_seq": dispatch["packet"]["command_seq"],
        "logical_batch_sequence": dispatch["packet"].get("logical_batch_sequence", 1),
        "batch_row_index": 1,
        "terminal_reason": 1,
    }
    if complete:
        finish_dispatch(root, status="SUCCEEDED", observed=terminal)
        record_terminal_receipt(
            root,
            process_composition_sha256="c" * 64,
            dispatch_identity=dispatch["dispatch_identity"],
            dispatch_sequence=1,
            terminal_state=terminal,
        )
    return root, request, dispatch


def _lease(tmp_path: Path) -> tuple[Path, CampaignLease]:
    path = tmp_path / "campaign_lease.json"
    lease = CampaignLease.issue(
        lease_id="1" * 32,
        launch_id="2" * 32,
        manifest_sha256=RELEASE_SHA,
        release_stage_id="step5d_strict_rnn_autotune_v3",
        program_id=stop.EXPECTED_PROGRAM_ID,
        protocol_id="v3_full_home_rolling_arm_v1",
        campaign_id="campaign-test",
        campaign_epoch=1,
        campaign_fingerprint="d" * 64,
        safety_envelope_sha256="e" * 64,
        supervisor_pid=os.getpid(),
    )
    write_campaign_lease(path.absolute(), lease)
    return path, lease


def _owner(lease: CampaignLease, *, task: str = stop.EXPECTED_TASK) -> dict[str, Any]:
    return {
        "schema": "ur10e/live-writer-lease-owner-v1",
        "pid": lease.supervisor_pid,
        "starttime_ticks": lease.supervisor_starttime,
        "task": task,
        "acquired_at": "fixture",
    }


def _dashboard(*, running: bool, state: str) -> dict[str, Any]:
    return {
        "is in remote control": "true",
        "safetymode": "Safetymode: NORMAL",
        "robotmode": "Robotmode: RUNNING",
        "running": f"Program running: {'true' if running else 'false'}",
        "programState": state,
        "get loaded program": f"Loaded program: {TARGET}",
    }


def _csv(
    tmp_path: Path,
    dispatch: Mapping[str, Any],
    now_ns: int,
    *,
    speed: float = 0.0,
    old: bool = False,
    startup_rows: int = 2,
    terminal_rows: int = 4,
    filename: str = "bridge_rtde_500hz.csv",
) -> Path:
    path = tmp_path / filename
    tmp_path.mkdir(parents=True, exist_ok=True)
    packet = dispatch["packet"]
    fields = [
        "t_wall_ns",
        "ur_timestamp",
        "ur_runtime_state",
        "ur_safety_mode",
        *(f"ur_output_int_register_{register}" for register in range(24, 35)),
        "campaign_epoch",
        "trial_id",
        "candidate_token",
        "execution_profile_id",
        "command_seq",
        "command",
        *(f"ur_actual_TCP_speed_{i}" for i in range(6)),
    ]
    rows = []
    step_ns = 166_666_667
    last_terminal_wall = now_ns - (20_000_000_000 if old else 0)
    first_terminal_wall = last_terminal_wall - (terminal_rows - 1) * step_ns
    base = first_terminal_wall - startup_rows * step_ns
    for index in range(startup_rows):
        row = {
            "t_wall_ns": str(base + index * step_ns),
            "ur_timestamp": str(10.0 + index * 0.1),
            "ur_runtime_state": "2",
            "ur_safety_mode": "1",
            "campaign_epoch": "0",
            "trial_id": "0",
            "candidate_token": "0",
            "execution_profile_id": "0",
            "command_seq": "0",
            "command": "0",
        }
        for register in range(24, 35):
            row[f"ur_output_int_register_{register}"] = "10" if register == 26 else "0"
        for axis in range(6):
            row[f"ur_actual_TCP_speed_{axis}"] = str(speed)
        rows.append(row)
    for index in range(terminal_rows):
        row = {
            "t_wall_ns": str(first_terminal_wall + index * step_ns),
            "ur_timestamp": str(20.0 + index * 0.1),
            "ur_runtime_state": "2",
            "ur_safety_mode": "1",
            "campaign_epoch": str(packet["campaign_epoch"]),
            "trial_id": str(packet["trial_id"]),
            "candidate_token": str(packet["candidate_token"]),
            "execution_profile_id": str(packet["execution_profile_id"]),
            "command_seq": str(packet["command_seq"]),
            "command": str(packet["command"]),
        }
        register_values = {
            24: packet["campaign_epoch"],
            25: packet["trial_id"],
            26: 78,
            27: packet["candidate_token"],
            28: 1,
            29: packet["execution_profile_id"],
            30: packet["command_seq"],
            31: packet["batch_row_index"],
            32: 2,
            33: 0x7F,
            34: packet["logical_batch_sequence"],
        }
        for register, value in register_values.items():
            row[f"ur_output_int_register_{register}"] = str(value)
        for axis in range(6):
            row[f"ur_actual_TCP_speed_{axis}"] = str(speed)
        rows.append(row)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _process_reader(lease: CampaignLease):
    def read(pid: int) -> int | None:
        return lease.supervisor_starttime if pid == lease.supervisor_pid else None

    return read


def _common(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, complete: bool = True):
    queue, request, dispatch = _queue(tmp_path, complete=complete)
    lease_path, lease = _lease(tmp_path)
    now_ns = __import__("time").time_ns()
    bridge = _csv(tmp_path, dispatch, now_ns)
    monkeypatch.setattr(stop, "load_current_release", lambda _root: _release())
    return queue, request, dispatch, lease_path, lease, now_ns, bridge


def _call(
    queue: Path,
    bridge: Path,
    lease_path: Path,
    receipt: Path,
    *,
    now_ns: int,
    execute: bool = False,
    dashboard_reader=None,
    writer_factory=None,
    process_reader=None,
    owner_reader=None,
    policy: stop.StopPolicy | None = None,
    monotonic=None,
    sleep=None,
):
    return stop.governed_operator_stop(
        queue_root=queue,
        bridge_csv=bridge,
        campaign_lease=lease_path,
        robot_host="robot",
        receipt_path=receipt,
        expected_overlay_sha256=OVERLAY_SHA,
        expected_control_candidate_uid=CONTROL_UID,
        expected_source=SOURCE,
        policy=policy or stop.StopPolicy(freshness_s=5.0),
        execute=execute,
        dashboard_reader=dashboard_reader,
        writer_factory=writer_factory,
        process_starttime_reader=process_reader,
        writer_owner_reader=owner_reader,
        time_ns=lambda: now_ns,
        monotonic=monotonic or (lambda: 0.0),
        sleep=sleep or (lambda _seconds: None),
    )


def test_pass_validate_primitives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _request, dispatch, lease_path, lease, now_ns, bridge = _common(tmp_path, monkeypatch)
    gate = stop.validate_queue_gate(
        queue,
        expected_overlay_sha256=OVERLAY_SHA,
        expected_control_candidate_uid=CONTROL_UID,
        expected_source=SOURCE,
        now_ns=now_ns,
    )
    dwell = stop.validate_stationary_dwell(
        bridge,
        queue_gate=gate,
        now_ns=now_ns,
    )
    dashboard = stop.validate_dashboard(
        _dashboard(running=True, state="PLAYING r034.urp"),
        expected_target=TARGET,
        expected_running=True,
        expected_program_state="PLAYING",
    )
    assert gate.dispatch["dispatch_sequence"] == 1
    assert dwell.duration_s >= 0.5 - 0.002
    assert dashboard.loaded_program == TARGET


def test_old_request_and_dispatch_mtimes_do_not_block_queue_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, _request, _dispatch, _lease_path, _lease, now_ns, _bridge = _common(tmp_path, monkeypatch)
    old_ns = now_ns - 60_000_000_000
    request_path = next((queue / "requests").glob("*.json"))
    dispatch_path = next((queue / "dispatches").glob("*.json"))
    os.utime(request_path, ns=(old_ns, old_ns))
    os.utime(dispatch_path, ns=(old_ns, old_ns))

    gate = stop.validate_queue_gate(
        queue,
        expected_overlay_sha256=OVERLAY_SHA,
        expected_control_candidate_uid=CONTROL_UID,
        expected_source=SOURCE,
        now_ns=now_ns,
        freshness_s=5.0,
    )
    assert gate.dispatch["dispatch_sequence"] == 1


def test_per_trial_single_terminal_row_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, _request, dispatch, _lease_path, _lease, now_ns, _bridge = _common(tmp_path, monkeypatch)
    capture = _csv(
        tmp_path / "per-trial",
        dispatch,
        now_ns,
        startup_rows=0,
        terminal_rows=1,
        filename="capture.csv",
    )
    gate = stop.validate_queue_gate(
        queue,
        expected_overlay_sha256=OVERLAY_SHA,
        expected_control_candidate_uid=CONTROL_UID,
        expected_source=SOURCE,
        now_ns=now_ns,
    )
    with pytest.raises(stop.OperatorStopError, match="at least two rows|dwell"):
        stop.validate_stationary_dwell(capture, queue_gate=gate, now_ns=now_ns)


def test_no_execute_never_constructs_writer_and_writes_validation_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, _request, _dispatch, lease_path, lease, now_ns, bridge = _common(tmp_path, monkeypatch)
    calls: list[str] = []

    def dashboard(_host: str, _commands: Any, **_kwargs: Any) -> Mapping[str, Any]:
        calls.append("dashboard")
        return _dashboard(running=True, state="PLAYING")

    def forbidden(_target: str) -> Any:
        pytest.fail("writer_factory must not be called without --execute")

    receipt = tmp_path / "validation.json"
    result = _call(
        queue,
        bridge,
        lease_path,
        receipt,
        now_ns=now_ns,
        dashboard_reader=dashboard,
        writer_factory=forbidden,
        process_reader=_process_reader(lease),
        owner_reader=lambda: _owner(lease),
    )
    assert result["outcome"]["status"] == "VALIDATED_NO_EXECUTE"
    assert result["writer"]["constructed"] is False
    assert calls == ["dashboard"]
    assert json.loads(receipt.read_text()) ["outcome"]["command_sent"] is False


def test_pending_or_inflight_queue_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _request, _dispatch = _queue(tmp_path, complete=False)
    with pytest.raises(stop.OperatorStopError, match="pending|inflight"):
        stop.validate_queue_gate(
            queue,
            expected_overlay_sha256=OVERLAY_SHA,
            expected_control_candidate_uid=CONTROL_UID,
            expected_source=SOURCE,
        )


@pytest.mark.parametrize("bad_source", ["P02", "wrong-source"])
def test_wrong_p01_request_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_source: str) -> None:
    queue, _request, _dispatch, _lease_path, _lease, now_ns, _bridge = _common(tmp_path, monkeypatch)
    with pytest.raises(stop.OperatorStopError, match="source"):
        stop.validate_queue_gate(
            queue,
            expected_overlay_sha256=OVERLAY_SHA,
            expected_control_candidate_uid=CONTROL_UID,
            expected_source=bad_source,
            now_ns=now_ns,
        )


def test_wrong_terminal_receipt_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _request, dispatch, _lease_path, _lease, now_ns, _bridge = _common(tmp_path, monkeypatch)
    receipt_path = next((queue / "governance" / "terminal_receipts").glob("*.json"))
    payload = json.loads(receipt_path.read_text())
    payload["dispatch_identity"] = "dispatch:v1:wrong"
    receipt_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(stop.OperatorStopError, match="dispatch identity"):
        stop.validate_queue_gate(
            queue,
            expected_overlay_sha256=OVERLAY_SHA,
            expected_control_candidate_uid=CONTROL_UID,
            expected_source=SOURCE,
            now_ns=now_ns,
        )


def test_terminal_rtde_echo_mismatch_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _request, dispatch, _lease_path, _lease, now_ns, bridge = _common(tmp_path, monkeypatch)
    with bridge.open("r", newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    rows[-1]["ur_output_int_register_30"] = str(dispatch["packet"]["command_seq"] + 1)
    with bridge.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    gate = stop.validate_queue_gate(
        queue,
        expected_overlay_sha256=OVERLAY_SHA,
        expected_control_candidate_uid=CONTROL_UID,
        expected_source=SOURCE,
        now_ns=now_ns,
    )
    with pytest.raises(stop.OperatorStopError, match="ur_output_int_register_30"):
        stop.validate_stationary_dwell(bridge, queue_gate=gate, now_ns=now_ns)


@pytest.mark.parametrize("kwargs", [{"old": True}, {"speed": 0.002}])
def test_stale_or_nonstationary_csv_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    queue, _request, dispatch, lease_path, lease, now_ns, _bridge = _common(tmp_path, monkeypatch)
    # Keep the fixture local and explicit; no bridge/controller I/O is involved.
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    bridge = _csv(bad_dir, dispatch, now_ns, **kwargs)
    with pytest.raises(stop.OperatorStopError, match="stale|speed"):
        _call(
            queue,
            bridge,
            lease_path,
            tmp_path / "failure.json",
            now_ns=now_ns,
            dashboard_reader=lambda *_args, **_kwargs: _dashboard(running=True, state="PLAYING"),
            process_reader=_process_reader(lease),
            owner_reader=lambda: _owner(lease),
        )


def test_wrong_lease_or_writer_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _request, _dispatch, lease_path, lease, now_ns, bridge = _common(tmp_path, monkeypatch)
    with pytest.raises(stop.OperatorStopError, match="writer_lease_owner task"):
        _call(
            queue,
            bridge,
            lease_path,
            tmp_path / "failure.json",
            now_ns=now_ns,
            dashboard_reader=lambda *_args, **_kwargs: _dashboard(running=True, state="PLAYING"),
            process_reader=_process_reader(lease),
            owner_reader=lambda: _owner(lease, task="wrong-task"),
        )


def test_pre_dashboard_gate_blocks_before_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _request, _dispatch, lease_path, lease, now_ns, bridge = _common(tmp_path, monkeypatch)
    writer_called = False

    def writer(_target: str) -> Any:
        nonlocal writer_called
        writer_called = True
        return object()

    with pytest.raises(stop.OperatorStopError, match="Remote control"):
        _call(
            queue,
            bridge,
            lease_path,
            tmp_path / "pre-block.json",
            now_ns=now_ns,
            dashboard_reader=lambda *_args, **_kwargs: {
                **_dashboard(running=True, state="PLAYING"),
                "is in remote control": "false",
            },
            writer_factory=writer,
            process_reader=_process_reader(lease),
            owner_reader=lambda: _owner(lease),
        )
    assert writer_called is False


class _FakeWriter:
    binding = {"writer": "fake", "target_path": TARGET, "command": "stop"}

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = dict(result)
        self.calls: list[str] = []

    def write(self, command: str) -> Mapping[str, Any]:
        self.calls.append(command)
        return self.result


def test_successful_stop_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    queue, _request, _dispatch, lease_path, lease, now_ns, bridge = _common(tmp_path, monkeypatch)
    fake = _FakeWriter({"command_sent": True, "response": "Stopped"})
    observations = iter([_dashboard(running=True, state="PLAYING"), _dashboard(running=False, state="STOPPED")])
    result = _call(
        queue,
        bridge,
        lease_path,
        tmp_path / "success.json",
        now_ns=now_ns,
        execute=True,
        dashboard_reader=lambda *_args, **_kwargs: next(observations),
        writer_factory=lambda target: fake,
        process_reader=_process_reader(lease),
        owner_reader=lambda: _owner(lease),
    )
    assert fake.calls == ["stop"]
    assert result["outcome"]["status"] == "STOPPED"
    assert result["outcome"]["command_sent"] is True
    assert result["dashboard"]["post"]["program_state"] == "STOPPED"


def test_command_sent_post_observation_failure_is_ambiguous_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue, _request, _dispatch, lease_path, lease, now_ns, bridge = _common(tmp_path, monkeypatch)
    fake = _FakeWriter({"command_sent": True, "response": "Stopped"})
    clock = {"value": 0.0}

    def monotonic() -> float:
        return clock["value"]

    def sleep(seconds: float) -> None:
        clock["value"] += seconds

    receipt_path = tmp_path / "ambiguous.json"
    with pytest.raises(stop.OperatorStopError, match="post Dashboard"):
        _call(
            queue,
            bridge,
            lease_path,
            receipt_path,
            now_ns=now_ns,
            execute=True,
            dashboard_reader=lambda *_args, **_kwargs: _dashboard(running=True, state="PLAYING"),
            writer_factory=lambda target: fake,
            process_reader=_process_reader(lease),
            owner_reader=lambda: _owner(lease),
            policy=stop.StopPolicy(
                freshness_s=5.0,
                post_poll_timeout_s=0.03,
                post_poll_interval_s=0.01,
            ),
            monotonic=monotonic,
            sleep=sleep,
        )
    payload = json.loads(receipt_path.read_text())
    assert payload["outcome"]["status"] == "AMBIGUOUS"
    assert payload["outcome"]["command_sent"] is True
    assert payload["writer"]["outcome"]["command_sent"] is True
