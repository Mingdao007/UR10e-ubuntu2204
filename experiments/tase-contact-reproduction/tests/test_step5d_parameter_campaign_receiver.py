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
from step5d_production_csv import BridgeCsvFollowerStats, BridgeCsvTimeout  # noqa: E402


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

    observed, _ = runner._wait_terminal(follower, arm=arm, poll_s=0.0)

    assert observed["trial_id"] == 2


def test_terminal_wait_records_consumption_before_terminal_home():
    arm = _arm(trial=1, seq=1, token=99, batch=1)
    consumed = []

    class ActiveThenTerminal:
        def rows(self, *, timeout_s):
            del timeout_s
            yield _observation(seq=1, trial=1, state=20)
            assert len(consumed) == 1
            yield _observation(seq=1, trial=1, state=78, reason=1)

    observed, _ = runner._wait_terminal(
        ActiveThenTerminal(),
        arm=arm,
        poll_s=0.0,
        on_consumed=lambda row: consumed.append(dict(row)),
    )

    assert consumed[0]["state"] == 20
    assert observed["state"] == 78


def test_resume_home_accepts_only_exact_durable_state78(monkeypatch):
    monkeypatch.setattr(runner, "_publish_status", lambda *args, **kwargs: None)
    row = _observation(seq=1, trial=1, state=78, reason=1)
    row["ur_runtime_state"] = runner.UR_RUNTIME_PLAYING

    observed = runner._wait_resume_home(
        SimpleNamespace(),
        FakeFollower([row]),
        home_identity={
            "campaign_epoch": 1,
            "last_trial_id": 1,
            "last_command_seq": 1,
        },
    )

    assert observed["state"] == runner.READY_HOME_NEXT
    assert observed["consumed_command_seq"] == 1


def test_resume_home_rejects_durable_identity_mismatch(monkeypatch):
    monkeypatch.setattr(runner, "_publish_status", lambda *args, **kwargs: None)
    row = _observation(seq=2, trial=2, state=78, reason=1)
    row["ur_runtime_state"] = runner.UR_RUNTIME_PLAYING

    with pytest.raises(
        runner.ParameterCampaignError,
        match="identity differs from durable Home",
    ):
        runner._wait_resume_home(
            SimpleNamespace(),
            FakeFollower([row]),
            home_identity={
                "campaign_epoch": 1,
                "last_trial_id": 1,
                "last_command_seq": 1,
            },
        )


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
            poll_s=0.0,
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
            poll_s=0.0,
        )
    assert caught.value.failure_class == "IDENTITY"
    assert caught.value.observed["state"] == 78
    assert caught.value.observed["consumed_command_seq"] == 3


def test_inflight_observation_decisions_are_conservative():
    dispatch = {
        "packet": {
            "campaign_epoch": 1,
            "trial_id": 1,
            "candidate_token": 99,
            "execution_profile_id": 633,
            "command_seq": 1,
            "logical_batch_sequence": 1,
            "batch_row_index": 1,
        }
    }

    not_consumed, _ = runner._classify_inflight_observation(
        dispatch,
        runner._tp_observation(_observation(seq=0, trial=0, state=10)),
    )
    consumed_nonterminal, _ = runner._classify_inflight_observation(
        dispatch,
        runner._tp_observation(_observation(seq=1, trial=1, state=20)),
    )
    consumed_terminal, _ = runner._classify_inflight_observation(
        dispatch,
        runner._tp_observation(_observation(seq=1, trial=1, state=78, reason=1)),
    )
    mismatch_terminal, _ = runner._classify_inflight_observation(
        dispatch,
        runner._tp_observation(_observation(seq=2, trial=2, state=78)),
    )

    assert not_consumed == "NOT_CONSUMED"
    assert consumed_nonterminal == "WAITING_FOR_HARDWARE"
    assert consumed_terminal == "TERMINAL"
    assert mismatch_terminal == "IDENTITY"


@pytest.mark.parametrize(
    ("terminal_reason", "failure_class"),
    [
        (1, None),
        (2, "EXTERNAL_HARDWARE"),
        (3, "EXTERNAL_HARDWARE"),
        (17, "EXTERNAL_HARDWARE"),
        (4, "PARAMETER_GUARD"),
        (14, "PARAMETER_GUARD"),
        (13, "SOFTWARE"),
        (9, "SOFTWARE"),
        (999, "SOFTWARE"),
    ],
)
def test_terminal_reason_classification_is_narrow_and_shared(
    terminal_reason: int, failure_class: str | None
):
    assert runner._terminal_failure_class(terminal_reason) == failure_class


def test_restart_adopts_not_consumed_without_sending_arm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    receiver_root = tmp_path / "receiver"
    initialize(
        receiver_root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    submit(
        receiver_root,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
        force_p=0.001,
        force_i=0.00001,
        force_damping=7.0,
    )
    bind_home(receiver_root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(receiver_root)
    assert dispatch is not None
    statuses = []
    monkeypatch.setattr(
        runner,
        "_publish_status",
        lambda args, **kwargs: statuses.append(kwargs),
    )

    args = SimpleNamespace(
        receiver_root=receiver_root,
        sleep=lambda _delay: pytest.fail("not-consumed evidence should return immediately"),
    )
    observed = runner._adopt_inflight(
        args,
        binding={},
        follower=FakeFollower([_observation(seq=0, trial=0, state=10)]),
        dispatch=dispatch,
    )

    assert observed["consumed_command_seq"] == 0
    assert receiver_status(receiver_root)["inflight"] is None
    assert receiver_status(receiver_root)["attempted_count"] == 0
    assert statuses[0]["state"] == "WAITING_FOR_HARDWARE"


@pytest.mark.parametrize(
    ("reason", "expected_status", "expected_failure_class"),
    [
        (1, "SUCCEEDED", None),
        (2, "FAILED", "EXTERNAL_HARDWARE"),
        (999, "FAILED", "SOFTWARE"),
    ],
)
def test_restart_adopts_terminal_outcome_without_resending_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: int,
    expected_status: str,
    expected_failure_class: str | None,
):
    receiver_root = tmp_path / "receiver"
    initialize(
        receiver_root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    submit(
        receiver_root,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
        force_p=0.001,
        force_i=0.00001,
        force_damping=7.0,
    )
    bind_home(receiver_root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(receiver_root)
    assert dispatch is not None
    campaign_root = tmp_path / "campaign"
    bridge_run = tmp_path / "bridge"
    campaign_root.mkdir()
    bridge_run.mkdir()
    prepared = SimpleNamespace(trial=SimpleNamespace(trial_uid="trial-1"))
    capture = bridge_run / "autotune_trials/trial-1/capture.csv"
    capture.parent.mkdir(parents=True)
    capture.write_text("time,value\n0,0\n", encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "_prepared",
        lambda args, *, binding, dispatch: (
            _arm(
                trial=dispatch["packet"]["trial_id"],
                seq=dispatch["packet"]["command_seq"],
                token=dispatch["packet"]["candidate_token"],
                batch=dispatch["packet"]["logical_batch_sequence"],
            ),
            prepared,
        ),
    )
    monkeypatch.setattr(
        runner,
        "_publish_status",
        lambda args, **kwargs: None,
    )
    monkeypatch.setattr(
        runner,
        "_send",
        lambda *args, **kwargs: pytest.fail("inflight adoption must not replay ARM"),
    )

    args = SimpleNamespace(
        receiver_root=receiver_root,
        campaign_root=campaign_root,
        bridge_run=bridge_run,
        release_manifest_sha256="a" * 64,
        sleep=lambda _delay: pytest.fail("terminal evidence should return immediately"),
    )
    terminal_row = _observation(seq=1, trial=1, state=78, reason=reason)
    terminal_row["ur_output_int_register_27"] = dispatch["packet"]["candidate_token"]
    observed = runner._adopt_inflight(
        args,
        binding={},
        follower=FakeFollower([terminal_row]),
        dispatch=dispatch,
    )

    assert observed["state"] == 78
    assert receiver_status(receiver_root)["inflight"] is None
    assert receiver_status(receiver_root)["attempted_count"] == 1
    receipt = next((receiver_root / "receipts").glob("*.json"))
    payload = json.loads(receipt.read_text())
    assert payload["schema"].endswith("receipt-v2")
    assert payload["status"] == expected_status
    assert payload["failure_class"] == expected_failure_class


def test_inflight_adoption_keeps_waiting_through_hardware_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    receiver_root = tmp_path / "receiver"
    initialize(
        receiver_root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
    )
    submit(
        receiver_root,
        launch_profile_path=ROOT / "config/step5/step5d_autotune_v3_launch_profile.json",
        force_p=0.001,
        force_i=0.00001,
        force_damping=7.0,
    )
    bind_home(receiver_root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(receiver_root)
    assert dispatch is not None

    class TimeoutThenFreshHome:
        calls = 0
        timeouts = []

        def rows(self, *, timeout_s):
            self.timeouts.append(timeout_s)
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("bridge unavailable")
            yield _observation(seq=0, trial=0, state=10)

    statuses = []
    inflight_snapshots = []
    monkeypatch.setattr(
        runner,
        "_publish_status",
        lambda args, **kwargs: (
            statuses.append(kwargs),
            inflight_snapshots.append(receiver_status(args.receiver_root)["inflight"]),
        ),
    )
    sleeps = []
    follower = TimeoutThenFreshHome()
    args = SimpleNamespace(
        receiver_root=receiver_root,
        sleep=sleeps.append,
    )
    runner._adopt_inflight(
        args,
        binding={},
        follower=follower,
        dispatch=dispatch,
    )

    assert sleeps == [runner.INFLIGHT_RECOVERY_SLEEP_S]
    assert follower.timeouts == [runner.INFLIGHT_OBSERVATION_POLL_S] * 2
    assert all(item["state"] == "WAITING_FOR_HARDWARE" for item in statuses)
    assert inflight_snapshots[1] is not None
    assert receiver_status(receiver_root)["attempted_count"] == 0


def test_safety_state_is_hardware_recovery_not_a_fake_terminal():
    arm = _arm(trial=1, seq=1, token=99, batch=1)
    unsafe = _observation(seq=1, trial=1, state=78)
    unsafe["ur_safety_mode"] = 0

    with pytest.raises(runner.HardwareRecoveryRequired):
        runner._wait_terminal(
            FakeFollower([unsafe]),
            arm=arm,
            poll_s=0.0,
        )


def test_ten_dispatches_continue_after_per_trial_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    args = SimpleNamespace(bridge_run=tmp_path)
    prepared = SimpleNamespace(trial=SimpleNamespace(trial_uid="trial"))
    finished = []
    failure_classes = []

    def fake_finish(*args, **kwargs):
        del args
        finished.append(kwargs["status"])
        failure_classes.append(kwargs["failure_class"])

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
    assert failure_classes.count("EXTERNAL_HARDWARE") == 3


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


def test_capture_health_never_waits_for_async_seal(tmp_path: Path):
    status, detail = runner._capture_health(tmp_path / "capture.csv")

    assert status == "PENDING"
    assert detail == "capture seal pending asynchronous outbox validation"


def test_async_capture_seal_does_not_block_next_trial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    finished = []
    monkeypatch.setattr(
        runner,
        "_finish_trial",
        lambda *args, **kwargs: finished.append(kwargs),
    )

    observed = runner._run_trial(
        SimpleNamespace(bridge_run=tmp_path),
        FakeFollower([_observation(seq=1, trial=1, state=78, reason=1)]),
        dispatch={"dispatch_sequence": 1, "request": {"request_uid": "request-1"}},
        arm=_arm(trial=1, seq=1, token=99, batch=1),
        prepared=SimpleNamespace(trial=SimpleNamespace(trial_uid="trial-1")),
        observation={},
    )

    assert observed["state"] == 78
    assert finished[0]["status"] == "SUCCEEDED"
    assert finished[0]["failure_class"] is None
    assert "pending asynchronous outbox" in finished[0]["detail"]


def test_terminal_poll_boundary_never_becomes_trial_outcome():
    arm = _arm(trial=1, seq=1, token=99, batch=1)

    class PollBoundaryThenTerminal:
        calls = 0

        def rows(self, *, timeout_s):
            assert timeout_s == runner.OBSERVATION_POLL_S
            self.calls += 1
            if self.calls == 1:
                raise BridgeCsvTimeout(
                    "no_fresh_rows",
                    stats=BridgeCsvFollowerStats(),
                )
            yield _observation(seq=1, trial=1, state=78, reason=1)

    follower = PollBoundaryThenTerminal()
    observed, _ = runner._wait_terminal(follower, arm=arm)

    assert follower.calls == 2
    assert observed["state"] == 78


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
