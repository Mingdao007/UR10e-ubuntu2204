from __future__ import annotations

import sys
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import run_step5d_parameter_campaign as runner  # noqa: E402
from step5d_autotune_state_machine import HostCommand, HostPacket  # noqa: E402
from step5d_parameter_outbox import enqueue_postprocess_task  # noqa: E402
from step5d_parameter_queue import (  # noqa: E402
    bind_home,
    initialize,
    prepare_next_dispatch,
    status as receiver_status,
    submit,
)


def _observation(*, seq: int, trial: int, state: int = 20, reason: int = 0):
    return {
        "ur_output_int_register_24": 1,
        "ur_output_int_register_25": trial,
        "ur_output_int_register_26": state,
        "ur_output_int_register_27": 99 if trial == 1 else 100,
        "ur_output_int_register_28": reason,
        "ur_output_int_register_29": 633,
        "ur_output_int_register_30": seq,
        "ur_output_int_register_34": trial,
        "ur_output_int_register_31": 1,
        "ur_safety_mode": 1,
        "step4e_controller_state": 0,
    }


class FakeFollower:
    def __init__(self, rows):
        self._rows = iter(rows)

    def rows(self, *, timeout_s):
        del timeout_s
        yield from self._rows


def _arm(*, trial: int, seq: int, token: int, batch: int) -> HostPacket:
    return HostPacket(
        campaign_epoch=1,
        trial_id=trial,
        command=HostCommand.ARM,
        candidate_token=token,
        execution_profile_id=633,
        command_seq=seq,
        logical_batch_sequence=batch,
    )


def test_terminal_wait_ignores_stale_state78_before_next_trial():
    arm = _arm(trial=2, seq=2, token=100, batch=2)
    follower = FakeFollower(
        [
            _observation(seq=1, trial=1, state=78, reason=1),
            _observation(seq=2, trial=2, state=78, reason=1),
        ]
    )

    observed, _ = runner._wait_terminal(follower, arm=arm, timeout_s=0.0)

    assert observed["trial_id"] == 2


def test_same_sequence_identity_mismatch_is_typed_trial_outcome():
    arm = _arm(trial=2, seq=2, token=100, batch=2)
    with pytest.raises(runner.TrialOutcomeError) as caught:
        runner._wait_terminal(
            FakeFollower(
                [
                    _observation(seq=2, trial=1, state=20),
                    _observation(seq=2, trial=1, state=78),
                ]
            ),
            arm=arm,
            timeout_s=0.0,
        )
    assert caught.value.failure_class == "IDENTITY"
    assert caught.value.observed["state"] == 78


def test_overshoot_latches_until_authoritative_terminal_home():
    arm = _arm(trial=2, seq=2, token=100, batch=2)
    with pytest.raises(runner.TrialOutcomeError) as caught:
        runner._wait_terminal(
            FakeFollower(
                [
                    _observation(seq=3, trial=3, state=20),
                    _observation(seq=3, trial=3, state=78),
                ]
            ),
            arm=arm,
            timeout_s=0.0,
        )
    assert caught.value.failure_class == "IDENTITY"
    assert caught.value.observed["state"] == 78
    assert caught.value.observed["consumed_command_seq"] == 3


def test_safety_state_is_hardware_recovery_not_a_fake_terminal():
    arm = _arm(trial=1, seq=1, token=99, batch=1)
    unsafe = _observation(seq=1, trial=1, state=78)
    unsafe["ur_safety_mode"] = 0

    with pytest.raises(runner.HardwareRecoveryRequired):
        runner._wait_terminal(
            FakeFollower([unsafe]),
            arm=arm,
            timeout_s=0.0,
        )


def test_ten_dispatches_continue_after_per_trial_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args = SimpleNamespace(trial_timeout_s=0.0, bridge_run=tmp_path)
    prepared = SimpleNamespace(trial=SimpleNamespace(trial_uid="trial"))
    finished = []

    def fake_finish(*args, **kwargs):
        del args
        finished.append(kwargs["status"])

    monkeypatch.setattr(runner, "_finish_trial", fake_finish)
    for number in range(1, 11):
        terminal_reason = 2 if number % 3 == 0 else 1
        trial_uid = f"trial-{number}"
        prepared.trial.trial_uid = trial_uid
        capture = tmp_path / "autotune_trials" / trial_uid / "capture.csv"
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text("time,value\n0,0\n", encoding="utf-8")
        observed = _observation(
            seq=number,
            trial=number,
            state=78,
            reason=terminal_reason,
        )
        runner._run_trial(
            args,
            FakeFollower([observed]),
            dispatch={
                "dispatch_sequence": number,
                "request": {"request_uid": f"request-{number}"},
            },
            arm=_arm(trial=number, seq=number, token=99 if number == 1 else 100, batch=number),
            prepared=prepared,
            observation={},
        )

    assert len(finished) == 10
    assert finished.count("FAILED") == 3


def test_runner_continuous_loop_finishes_ten_queue_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    class StopAfterQueue(Exception):
        pass

    receiver_root = tmp_path / "receiver"
    initialize(
        receiver_root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    for index in range(10):
        submit(
            receiver_root,
            launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
            force_p=0.0005946035575013605 * (2 ** (index / 4)),
            force_i=0.00001,
            force_damping=7.0,
            source=f"continuous-{index}",
        )
    bind_home(receiver_root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    campaign_root = tmp_path / "campaign"
    bridge_run = tmp_path / "bridge"
    campaign_root.mkdir()
    bridge_run.mkdir()
    args = SimpleNamespace(
        experiment_root=tmp_path,
        bridge_run=bridge_run,
        campaign_root=campaign_root,
        receiver_root=receiver_root,
        mailbox=tmp_path / "mailbox.json",
        runner_ready_file=tmp_path / "runner-ready.json",
        campaign_binding=tmp_path / "binding.json",
        campaign_lease=tmp_path / "lease.json",
        release_manifest_sha256="a" * 64,
        v3_launch_profile=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
        v3_program_id="step5d_strict_rnn_autotune_v3",
        initial_manifest=tmp_path / "initial.json",
        trial_timeout_s=0.0,
    )
    binding = {
        "campaign_id": "campaign-test",
        "campaign_epoch": 1,
        "campaign_fingerprint": "campaign-fingerprint",
    }
    monkeypatch.setattr(runner, "_strict_object", lambda path, role: binding)
    monkeypatch.setattr(runner, "_validate_authority", lambda args, binding: None)
    monkeypatch.setattr(runner, "seed_initial_manifest", lambda *args, **kwargs: ())
    monkeypatch.setattr(runner, "BridgeCsvFollower", lambda path: object())
    monkeypatch.setattr(
        runner,
        "_wait_initial_home",
        lambda args, follower: runner._tp_observation(
            _observation(seq=0, trial=0, state=10)
        ),
    )

    def fake_wait_next(args, follower, observation):
        del follower, observation
        dispatch = prepare_next_dispatch(args.receiver_root)
        if dispatch is None:
            raise StopAfterQueue
        return dispatch

    def fake_send(args, *, binding, dispatch):
        del args, binding
        packet = dispatch["packet"]
        return (
            _arm(
                trial=packet["trial_id"],
                seq=packet["command_seq"],
                token=packet["candidate_token"],
                batch=packet["logical_batch_sequence"],
            ),
            SimpleNamespace(
                trial=SimpleNamespace(
                    trial_uid=f"trial-{dispatch['dispatch_sequence']}"
                )
            ),
        )

    finished = []

    def fake_run_trial(args, follower, *, dispatch, arm, prepared, observation):
        del follower, arm, observation
        packet = dispatch["packet"]
        observed = {
            "campaign_epoch": packet["campaign_epoch"],
            "trial_id": packet["trial_id"],
            "state": 78,
            "candidate_token": packet["candidate_token"],
            "execution_profile_id": packet["execution_profile_id"],
            "consumed_command_seq": packet["command_seq"],
            "logical_batch_sequence": packet["logical_batch_sequence"],
            "batch_row_index": 1,
            "safety_mode": 1,
            "controller_state": 0,
        }
        number = int(dispatch["dispatch_sequence"])
        if number % 4 == 0:
            observed.update(
                campaign_epoch=2,
                trial_id=packet["trial_id"] + 1,
                consumed_command_seq=packet["command_seq"] + 1,
                candidate_token=packet["candidate_token"] + 1,
            )
            status = "FAILED"
            failure_class = "IDENTITY"
        elif number % 4 == 1:
            status = "FAILED"
            failure_class = "PARAMETER_GUARD"
        elif number % 4 == 2:
            status = "FAILED"
            failure_class = "DATA_QUALITY"
        else:
            status = "SUCCEEDED"
            failure_class = None
            capture = (
                args.bridge_run
                / "autotune_trials"
                / prepared.trial.trial_uid
                / "capture.csv"
            )
            capture.parent.mkdir(parents=True)
            capture.write_text("time,value\n0,0\n", encoding="utf-8")
        runner._finish_trial(
            args,
            dispatch=dispatch,
            prepared=prepared,
            observed=observed,
            status=status,
            failure_class=failure_class,
            detail=f"continuous-{number}",
            capture=(
                args.bridge_run
                / "autotune_trials"
                / prepared.trial.trial_uid
                / "capture.csv"
            ),
        )
        finished.append((number, status, failure_class))
        return observed

    monkeypatch.setattr(runner, "_wait_next_dispatch", fake_wait_next)
    monkeypatch.setattr(runner, "_send", fake_send)
    monkeypatch.setattr(runner, "_run_trial", fake_run_trial)

    with pytest.raises(StopAfterQueue):
        runner.run(args)

    assert [number for number, _status, _failure in finished] == list(range(1, 11))
    assert receiver_status(receiver_root)["attempted_count"] == 10
    assert receiver_status(receiver_root)["inflight"] is None


def test_capture_health_uses_bounded_virtual_clock_wait(tmp_path: Path):
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    status, detail = runner._capture_health(
        tmp_path / "capture.csv",
        clock=clock,
        sleep=sleep,
        wait_s=10.0,
    )

    assert status == "DATA_ISSUE"
    assert detail == "capture missing after terminal Home"
    assert now[0] <= runner.TERMINAL_CAPTURE_WAIT_S
    assert sleeps


def test_unexpected_main_error_publishes_recovering_and_accepting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    receiver_root = tmp_path / "receiver"
    initialize(
        receiver_root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    args = SimpleNamespace(
        receiver_root=receiver_root,
        campaign_root=tmp_path / "campaign",
        bridge_run=tmp_path / "bridge",
        release_manifest_sha256="a" * 64,
    )
    args.campaign_root.mkdir()
    args.bridge_run.mkdir()
    monkeypatch.setattr(runner, "parse_args", lambda: args)

    def fail_run(_args):
        raise RuntimeError("unexpected test failure")

    monkeypatch.setattr(runner, "run", fail_run)
    assert runner.main() == 2
    payload = json.loads(
        (args.campaign_root / "parameter_receiver_status.json").read_text()
    )
    assert payload["state"] == "RECOVERING"
    assert payload["receiver_accepting"] is True


def test_outbox_task_is_durable_and_optimizer_is_optional(tmp_path: Path):
    path = enqueue_postprocess_task(
        tmp_path / "outbox",
        dispatch_sequence=10,
        trial_uid="trial-10",
        capture_path=tmp_path / "capture.csv",
        result_path=tmp_path / "result.json",
    )
    payload = json.loads(path.read_text())
    assert payload["optimizer_required"] is False
    assert payload["operations"] == ["sha256", "analysis", "png"]
    assert enqueue_postprocess_task(
        tmp_path / "outbox",
        dispatch_sequence=10,
        trial_uid="trial-10",
        capture_path=tmp_path / "capture.csv",
        result_path=tmp_path / "result.json",
    ) == path


def test_allowed_runner_states_exclude_legacy_intermediate_states():
    assert runner.RUNNER_STATES == {
        "RUNNING",
        "WAITING_FOR_PARAMETERS",
        "WAITING_FOR_HOME",
        "WAITING_FOR_HARDWARE",
        "RECOVERING",
        "SHUTDOWN",
    }
