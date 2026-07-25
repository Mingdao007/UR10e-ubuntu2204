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
            FakeFollower([_observation(seq=2, trial=1, state=78)]),
            arm=arm,
            timeout_s=0.0,
        )
    assert caught.value.failure_class == "IDENTITY"


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
