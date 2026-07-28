from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_parameter_manifest import (  # noqa: E402
    import_physical_attempt_uids,
    seed_initial_manifest,
    submit_candidate_pool,
    validate_manifest,
)
import prepare_step5d_autotune_launch as launch  # noqa: E402
from step5d_autotune_contract import NORMAL_FILTER_PROFILES  # noqa: E402
from step5d_autotune_live_driver import (  # noqa: E402
    execution_profile_id_for,
    validate_execution_profile_binding,
)
from step5d_parameter_queue import (  # noqa: E402
    ParameterQueueError,
    _profile_integer_id,
    bind_home,
    adopt_selected_legacy_binding,
    finish_dispatch,
    initialize,
    list_requests,
    list_pending,
    prepare_next_dispatch,
    reconcile_not_consumed,
    publish_next_arm,
    quarantine_pending_search_violations,
    read_next_arm,
    record_terminal_receipt,
    status,
    submit,
    terminalize_consumed_infra_abort,
)


PROFILE = ROOT / "config/step5/step5d_autotune_v3_launch_profile.json"
MANIFEST = ROOT / "config/step5d/parameter_receiver_initial.json"


def _queue(tmp_path: Path) -> Path:
    root = tmp_path / "receiver"
    initialize(
        root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=PROFILE,
    )
    return root


def _consumed_abort_artifacts(
    tmp_path: Path,
    dispatch: dict,
    *,
    command_seq_delta: int = 0,
) -> tuple[Path, Path]:
    summary = tmp_path / "bridge-summary.json"
    summary.write_text(
        json.dumps(
            {
                "stop_reason": "v30_control_exception_stop_published",
                "rtde_reconnect_events": [
                    {
                        "event": "v30_control_exception_fail_closed_publish",
                        "stop_publish_succeeded": True,
                        "stop_command": {
                            "cmd_valid": False,
                            "qdot": [0.0] * 6,
                            "stop_request": True,
                        },
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    packet = dispatch["packet"]
    partial = tmp_path / "capture.csv.part"
    header = (
        "campaign_epoch,trial_id,candidate_token,execution_profile_id,"
        "command_seq,_step5d_stage25_echo_consumed,t_monotonic_s\n"
    )
    rows = [
        (
            f"{packet['campaign_epoch']},{packet['trial_id']},"
            f"{packet['candidate_token']},{packet['execution_profile_id']},"
            f"{packet['command_seq'] + command_seq_delta},,10.0\n"
        ),
        (
            f"{packet['campaign_epoch']},{packet['trial_id']},"
            f"{packet['candidate_token']},{packet['execution_profile_id']},"
            f"{packet['command_seq'] + command_seq_delta},1,10.2\n"
        ),
    ]
    partial.write_text(header + "".join(rows), encoding="utf-8")
    return summary, partial


def test_initial_manifest_is_exact_quarter_octave_path() -> None:
    rows = validate_manifest(MANIFEST, launch_profile_path=PROFILE)
    assert len(rows) == 10
    assert all(row["position"] == "tail" for row in rows)
    assert rows[0]["force_p_gain"] == pytest.approx(0.0008408964152537145)
    assert all(row["force_damping"] >= 5.0 for row in rows)
    assert rows[-1]["force_damping"] == pytest.approx(7.0)


def test_receiver_rejects_new_candidate_below_damping_floor(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    with pytest.raises(ParameterQueueError, match="below the 5 search floor"):
        submit(
            root,
            launch_profile_path=PROFILE,
            force_p=0.001,
            force_i=0.00001,
            force_damping=4.949747468305833,
        )
    assert list_requests(root) == ()


def test_legacy_pending_candidate_below_floor_is_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _queue(tmp_path)
    import step5d_parameter_queue as queue_module

    monkeypatch.setattr(
        queue_module,
        "require_search_candidate",
        lambda candidate, *, role: candidate,
    )
    rejected = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001,
        force_i=0.00001,
        force_damping=4.949747468305833,
        source="legacy-before-floor",
    )
    monkeypatch.undo()
    accepted = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001,
        force_i=0.00001,
        force_damping=5.886274906776001,
        source="after-floor",
    )

    records = quarantine_pending_search_violations(root)
    assert len(records) == 1
    assert records[0]["request_uid"] == rejected["request_uid"]
    assert records[0]["physical_attempt"] is False
    assert [row["request_uid"] for row in list_pending(root)] == [
        accepted["request_uid"]
    ]


def test_legacy_inflight_candidate_below_floor_cannot_be_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _queue(tmp_path)
    import step5d_parameter_queue as queue_module

    with monkeypatch.context() as patch:
        patch.setattr(
            queue_module,
            "require_search_candidate",
            lambda candidate, *, role: candidate,
        )
        patch.setattr(
            queue_module,
            "search_candidate_allowed",
            lambda candidate: True,
        )
        submit(
            root,
            launch_profile_path=PROFILE,
            force_p=0.001,
            force_i=0.00001,
            force_damping=4.949747468305833,
            source="legacy-before-floor",
        )
        bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
        assert prepare_next_dispatch(root) is not None

    with pytest.raises(ParameterQueueError, match="cannot be replayed"):
        prepare_next_dispatch(root)


def test_receiver_is_unbounded_file_per_request_and_next_is_fifo(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    for index in range(40):
        p_step = (index % 8) - 4
        d_step = (index // 8) - 1
        submit(
            root,
            launch_profile_path=PROFILE,
            force_p=0.001 * (2 ** (p_step / 4)),
            force_i=0.00001,
            force_damping=7.0 * (2 ** (d_step / 4)),
            source=f"tail-{index}",
        )
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001 * (2 ** (4 / 4)),
        force_i=0.00001,
        force_damping=7.0 * (2 ** (3 / 4)),
        normal_filter_tau_s=0.7,
        source="priority-one",
        position="next",
    )
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001 * (2 ** (5 / 4)),
        force_i=0.00001,
        force_damping=7.0 * (2 ** (3 / 4)),
        source="priority-two",
        position="next",
    )
    pending = list_pending(root)
    assert len(pending) == 42
    assert [pending[0]["source"], pending[1]["source"]] == [
        "priority-one",
        "priority-two",
    ]
    assert pending[0]["overlay"]["normal_filter_tau_s"] == 0.7
    assert status(root)["capacity"] is None
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert "requests" not in state
    assert len(tuple((root / "requests").glob("*.json"))) == 42


def test_dispatch_identity_is_separate_and_failed_outcome_never_retries(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    request = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
        source="operator",
    )
    bind_home(root, campaign_epoch=2, last_trial_id=7, last_command_seq=9)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    assert dispatch["request"]["request_uid"] == request["request_uid"]
    assert dispatch["packet"]["trial_id"] == 8
    assert dispatch["dispatch_sequence"] == 1
    terminal = {
        "campaign_epoch": 2,
        "trial_id": 8,
        "state": 78,
        "candidate_token": dispatch["packet"]["candidate_token"],
        "execution_profile_id": dispatch["packet"]["execution_profile_id"],
        "consumed_command_seq": 10,
        "logical_batch_sequence": 1,
        "batch_row_index": 1,
        "terminal_reason": 1,
    }
    receipt = finish_dispatch(
        root,
        status="FAILED",
        observed=terminal,
        detail="capture missing",
    )
    assert "failure_class" not in receipt
    assert status(root)["terminal_receipt_count"] == 1
    assert prepare_next_dispatch(root) is None


def test_consumed_arm_does_not_create_a_physical_attempt_marker(tmp_path: Path) -> None:
    root = (
        tmp_path
        / "runs/step5d_autotune_v3/parameter-campaign/control/"
        "parameter_receiver_bindings/release-v1/queue"
    )
    initialize(
        root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=PROFILE,
    )
    request = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    packet = dispatch["packet"]

    assert status(root)["inflight"] is not None
    assert not (root / "physical_attempts").exists()
    reconcile_not_consumed(
        root,
        detail="a lagging observer still saw the preceding Home row",
        observed_command_seq=0,
    )
    redispatched = prepare_next_dispatch(root)
    assert redispatched is not None
    assert redispatched["request"]["request_uid"] == request["request_uid"]


def test_receipt_omits_classifier_and_identity_marker_is_internal_only(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    observed = {
        "campaign_epoch": 1,
        "trial_id": 1,
        "state": 78,
        "candidate_token": dispatch["packet"]["candidate_token"],
        "execution_profile_id": dispatch["packet"]["execution_profile_id"],
        "consumed_command_seq": 1,
        "logical_batch_sequence": 1,
        "batch_row_index": 1,
        "terminal_reason": 1,
    }
    with pytest.raises(ParameterQueueError, match="internal-only"):
        finish_dispatch(root, status="FAILED", observed=observed, failure_class="NOPE")
    with pytest.raises(ParameterQueueError, match="internal-only"):
        finish_dispatch(
            root,
            status="SUCCEEDED",
            observed=observed,
            failure_class="SOFTWARE",
        )
    receipt = finish_dispatch(root, status="SUCCEEDED", observed=observed)
    assert receipt["schema"].endswith("receipt-v3")
    assert "failure_class" not in receipt
    assert status(root)["terminal_receipt_count"] == 1
    assert not (root / "physical_attempts").exists()


def test_identity_failure_accepts_safe_terminal_home_and_advances_observed_identity(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.0014142135623730952,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=2, last_trial_id=7, last_command_seq=9)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    observed = {
        "campaign_epoch": 4,
        "trial_id": 21,
        "state": 78,
        "candidate_token": dispatch["packet"]["candidate_token"] + 1,
        "execution_profile_id": 999,
        "consumed_command_seq": 12,
        "logical_batch_sequence": 99,
        "batch_row_index": 4,
        "safety_mode": 1,
        "controller_state": 0,
    }

    receipt = finish_dispatch(
        root,
        status="FAILED",
        observed=observed,
        detail="terminal identity drift",
        failure_class="IDENTITY",
    )

    assert receipt["terminal_observation"] == observed
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert state["home_identity"] == {
        "campaign_epoch": 4,
        "last_trial_id": 21,
        "last_command_seq": 12,
    }
    next_dispatch = prepare_next_dispatch(root)
    assert next_dispatch is not None
    assert next_dispatch["packet"]["campaign_epoch"] == 4
    assert next_dispatch["packet"]["trial_id"] == 22
    assert next_dispatch["packet"]["command_seq"] == 13


def test_identity_failure_rejects_nonterminal_or_unconfirmed_safety(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    observed = {
        "campaign_epoch": 1,
        "trial_id": 1,
        "state": 20,
        "consumed_command_seq": 1,
        "safety_mode": 1,
    }
    with pytest.raises(ParameterQueueError, match="safe terminal Home"):
        finish_dispatch(
            root,
            status="FAILED",
            observed=observed,
            failure_class="IDENTITY",
        )
    assert status(root)["inflight"] is not None


def test_dispatch_packet_profile_survives_the_live_mailbox_cross_check(
    tmp_path: Path,
) -> None:
    """A dispatched packet must encode the overlay's own execution profile.

    ``validate_execution_profile_binding`` is the check the ARM mailbox runs
    before it writes anything, so a packet that disagrees with the overlay is
    never delivered to the TP.
    """

    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    overlay_profile_id = dispatch["request"]["overlay"]["execution_profile_id"]
    profile = next(
        row for row in NORMAL_FILTER_PROFILES if row.profile_id == overlay_profile_id
    )
    validate_execution_profile_binding(
        profile,
        dispatch["packet"]["execution_profile_id"],
        network_mode=True,
    )


def test_every_launch_profile_execution_profile_encodes_its_own_integer() -> None:
    """A release that rotates the execution profile cannot strand the packet."""

    allowed = json.loads(PROFILE.read_text())["trial_overlay_policy"][
        "execution_profile_id"
    ]["allowed"]
    assert len(allowed) > 1
    for profile_id in allowed:
        profile = next(
            row for row in NORMAL_FILTER_PROFILES if row.profile_id == profile_id
        )
        assert _profile_integer_id({"execution_profile_id": profile_id}) == (
            execution_profile_id_for(profile, network_mode=True)
        )


def test_not_consumed_is_immutable_non_attempt_and_request_remains_pending(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    request = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=10)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    record = reconcile_not_consumed(
        root,
        detail="TP command was not consumed",
        observed_command_seq=10,
    )
    assert status(root)["terminal_receipt_count"] == 0
    assert status(root)["inflight"] is None
    assert list_pending(root)[0]["request_uid"] == request["request_uid"]
    assert not tuple((root / "receipts").glob("*.json"))
    assert len(tuple((root / "reconciliations").glob("*.json"))) == 1

    restarted = initialize(
        root,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=PROFILE,
    )
    assert restarted["inflight"] is None
    redispatched = prepare_next_dispatch(root)
    assert redispatched is not None
    assert redispatched["request"]["request_uid"] == request["request_uid"]
    assert redispatched["dispatch_sequence"] == 1
    assert redispatched["dispatch_sha256"] == dispatch["dispatch_sha256"]


def test_consumed_infra_abort_is_tombstoned_and_requires_transport_rebind(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    request = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    publish_next_arm(
        root,
        dispatch_identity=dispatch["dispatch_identity"],
        dispatch_sequence=dispatch["dispatch_sequence"],
        campaign_fingerprint="c" * 64,
        mailbox_packet_sha256="d" * 64,
        observed_at=1,
    )
    summary, partial = _consumed_abort_artifacts(tmp_path, dispatch)

    reconciliation = terminalize_consumed_infra_abort(
        root,
        bridge_summary_path=summary,
        partial_capture_path=partial,
        detail="source closure fail-closed",
    )

    assert reconciliation["status"] == "INFRA_ABORTED_CONSUMED"
    assert reconciliation["requires_transport_rebind"] is True
    assert reconciliation["terminal_observation"]["partial_capture"]["rows"] == 2
    assert status(root)["inflight"] is None
    assert status(root)["pending_count"] == 0
    assert status(root)["terminal_receipt_count"] == 1
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    assert state["home_identity"] is None
    receipt = json.loads(
        (
            root
            / "receipts"
            / f"{request['request_uid'].rsplit(':', 1)[-1]}.json"
        ).read_text(encoding="utf-8")
    )
    assert receipt["status"] == "FAILED"
    assert receipt["terminal_observation"]["kind"] == "INFRA_ABORTED_CONSUMED"
    with pytest.raises(ParameterQueueError, match="not bound to READY_HOME"):
        prepare_next_dispatch(root)


def test_consumed_infra_abort_rejects_mismatched_partial_identity(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    publish_next_arm(
        root,
        dispatch_identity=dispatch["dispatch_identity"],
        dispatch_sequence=dispatch["dispatch_sequence"],
        campaign_fingerprint="c" * 64,
        mailbox_packet_sha256="d" * 64,
        observed_at=1,
    )
    summary, partial = _consumed_abort_artifacts(
        tmp_path, dispatch, command_seq_delta=1
    )

    with pytest.raises(
        ParameterQueueError, match="partial capture identity differs"
    ):
        terminalize_consumed_infra_abort(
            root,
            bridge_summary_path=summary,
            partial_capture_path=partial,
            detail="source closure fail-closed",
        )

    assert status(root)["inflight"]["dispatch_sequence"] == 1
    assert status(root)["terminal_receipt_count"] == 0


def test_crash_restart_preserves_inflight_dispatch_and_ignores_legacy_queue_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _queue(tmp_path)
    request = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=3, last_trial_id=4, last_command_seq=20)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    assert prepare_next_dispatch(root)["dispatch_sha256"] == dispatch["dispatch_sha256"]

    monkeypatch.setattr(
        Path,
        "rglob",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("bounded importer must not call Path.rglob")
        ),
    )
    legacy_root = (
        tmp_path
        / "runs/step5d_autotune_v3/parameter-campaign/control/"
        "parameter_receiver_bindings/release-v1/queue"
    )
    (legacy_root / "dispatches").mkdir(parents=True)
    (legacy_root / "receipts").mkdir(parents=True)
    (legacy_root / "dispatches" / "000000000001.json").write_text(
        json.dumps(dispatch), encoding="utf-8"
    )
    (legacy_root / "receipts" / "legacy.json").write_text(
        json.dumps(
            {
                "schema": "step5d.parameter-receiver/receipt-v1",
                "request_uid": request["request_uid"],
                "dispatch_sequence": 1,
                "dispatch_sha256": dispatch["dispatch_sha256"],
                "status": "COMPLETE",
                "physical_attempted": True,
            }
        ),
        encoding="utf-8",
    )
    imported = import_physical_attempt_uids(tmp_path, launch_profile_path=PROFILE)
    assert request["control_candidate_uid"] not in imported


def test_receiver_accepts_distinct_request_uids_for_same_control(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
        source="operator",
    )
    second = submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
        source="optimizer",
    )
    assert second["request_uid"] != list_requests(root)[0]["request_uid"]


def test_seed_keeps_frozen_attempt_provenance_out_of_dispatch_and_is_idempotent(tmp_path: Path) -> None:
    experiment = tmp_path / "experiment"
    ledger = experiment / "config/step5/step5d_autotune_v3_attempt_ledger.json"
    ledger.parent.mkdir(parents=True)
    first = validate_manifest(MANIFEST, launch_profile_path=PROFILE)[0]
    ledger.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "parameters": {
                            "force_p_gain": first["force_p_gain"],
                            "force_i_gain": first["force_i_gain"],
                            "force_damping": first["force_damping"],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    queue = tmp_path / "receiver"
    seeded = seed_initial_manifest(
        queue,
        campaign_id="campaign-test",
        release_manifest_sha256="a" * 64,
        launch_profile_path=PROFILE,
        manifest_path=MANIFEST,
        experiment_root=experiment,
    )
    assert len(seeded) == 10
    assert list_requests(queue)[0]["source"] == "approved_initial_10:P01"
    assert (
        seed_initial_manifest(
            queue,
            campaign_id="campaign-test",
            release_manifest_sha256="a" * 64,
            launch_profile_path=PROFILE,
            manifest_path=MANIFEST,
            experiment_root=experiment,
        )
        == ()
    )


def test_selected_legacy_migration_preserves_p04_and_next_p05(tmp_path: Path) -> None:
    source = _queue(tmp_path / "source")
    for index in range(1, 11):
        submit(
            source,
            launch_profile_path=PROFILE,
            force_p=0.0005 * (2 ** (index / 4)),
            force_i=0.00001,
            force_damping=7.0,
            source=f"P{index:02d}",
        )
    bind_home(source, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    for _ in range(4):
        dispatch = prepare_next_dispatch(source)
        packet = dispatch["packet"]
        finish_dispatch(
            source,
            status="SUCCEEDED",
            observed={
                "campaign_epoch": packet["campaign_epoch"],
                "trial_id": packet["trial_id"],
                "state": 78,
                "candidate_token": packet["candidate_token"],
                "execution_profile_id": packet["execution_profile_id"],
                "consumed_command_seq": packet["command_seq"],
                "logical_batch_sequence": packet["logical_batch_sequence"],
                "batch_row_index": 1,
                "terminal_reason": 1,
            },
        )
    state_path = source / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        schema="step5d.parameter-receiver/state-v1",
        release_manifest_sha256="447110" + "0" * 58,
        launch_profile_sha256="1" * 64,
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    legacy = tmp_path / "legacy-447110"
    shutil.copytree(source, legacy)
    before = (legacy / "state.json").read_bytes()
    stable = tmp_path / "stable"
    migrated = adopt_selected_legacy_binding(
        stable,
        legacy_root=legacy,
        campaign_id="campaign-test",
    )
    assert migrated["dispatch_sequence"] == 4
    assert migrated["revision"] == 10
    assert (stable / "receipts").is_dir()
    assert len(tuple((stable / "receipts").glob("*.json"))) == 4
    assert list_pending(stable)[0]["source"] == "P05"
    assert (legacy / "state.json").read_bytes() == before
    assert adopt_selected_legacy_binding(
        stable, legacy_root=legacy, campaign_id="campaign-test"
    )["dispatch_sequence"] == 4
    other_legacy = tmp_path / "other-447110"
    shutil.copytree(legacy, other_legacy)
    with pytest.raises(ParameterQueueError, match="migration binding differs"):
        adopt_selected_legacy_binding(
            stable, legacy_root=other_legacy, campaign_id="campaign-test"
        )


def test_selected_legacy_migration_missing_source_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ParameterQueueError, match="missing or unsafe"):
        adopt_selected_legacy_binding(
            tmp_path / "stable",
            legacy_root=tmp_path / "missing-447110",
            campaign_id="campaign-test",
        )


def test_sender_dedups_existing_requests_but_not_frozen_attempt_provenance(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    rows = validate_manifest(MANIFEST, launch_profile_path=PROFILE)
    for row in rows[4:]:
        submit(
            queue,
            launch_profile_path=PROFILE,
            force_p=row["force_p_gain"],
            force_i=row["force_i_gain"],
            force_damping=row["force_damping"],
            orientation_ko=row["orientation_ko"],
            source=row["source"],
            position=row["position"],
            occurrence_nonce=row["occurrence_nonce"],
        )
    experiment = tmp_path / "experiment"
    ledger = experiment / "config/step5/step5d_autotune_v3_attempt_ledger.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "parameters": {
                            "force_p_gain": rows[3]["force_p_gain"],
                            "force_i_gain": rows[3]["force_i_gain"],
                            "force_damping": rows[3]["force_damping"],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    submitted = submit_candidate_pool(
        queue,
        campaign_id="campaign-test",
        launch_profile_path=PROFILE,
        manifest_path=MANIFEST,
        experiment_root=experiment,
    )
    assert [row["source"] for row in submitted] == [
        "approved_initial_10:P01",
        "approved_initial_10:P02",
        "approved_initial_10:P03",
        "approved_initial_10:P04",
    ]
    assert submit_candidate_pool(
        queue,
        campaign_id="campaign-test",
        launch_profile_path=PROFILE,
        manifest_path=MANIFEST,
        experiment_root=experiment,
    ) == ()


def test_receiver_handles_one_hundred_fast_continuous_dispatches(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    for index in range(100):
        submit(
            root,
            launch_profile_path=PROFILE,
            force_p=0.0008408964152537145 * (2 ** ((index % 10) / 4)),
            force_i=0.00001,
            force_damping=5.886274906776001 * (2 ** ((index // 10) / 4)),
            source=f"fast-{index}",
        )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    for _ in range(100):
        dispatch = prepare_next_dispatch(root)
        assert dispatch is not None
        packet = dispatch["packet"]
        finish_dispatch(
            root,
            status="SUCCEEDED",
            observed={
                "campaign_epoch": packet["campaign_epoch"],
                "trial_id": packet["trial_id"],
                "state": 78,
                "candidate_token": packet["candidate_token"],
                "execution_profile_id": packet["execution_profile_id"],
                "consumed_command_seq": packet["command_seq"],
                "logical_batch_sequence": packet["logical_batch_sequence"],
                "batch_row_index": 1,
                "terminal_reason": 1,
            },
        )
    assert status(root)["dispatch_sequence"] == 100
    assert status(root)["terminal_receipt_count"] == 100
    assert status(root)["pending_count"] == 0


def test_prepare_keeps_receiver_root_stable_across_release_rollover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign_root = tmp_path / "campaign"
    request = launch.LaunchPreparationRequest(
        experiment_root=ROOT,
        campaign_root=campaign_root,
        binding_file=tmp_path / "binding.json",
        binding_source="offline-test",
        launch_profile_path=PROFILE,
        candidate_batch_size=5,
        rolling_plan=False,
        campaign_fingerprint="a" * 64,
    )
    releases = iter(
        [
            SimpleNamespace(manifest_sha256="a" * 64),
            SimpleNamespace(manifest_sha256="b" * 64),
        ]
    )
    monkeypatch.setattr(launch, "load_current_release", lambda _root: next(releases))
    monkeypatch.setattr(launch, "release_payload_path", lambda _root, _release, _path: MANIFEST)
    first = launch.prepare(request)
    second = launch.prepare(request)
    expected = campaign_root / "control" / "parameter_receiver"
    assert Path(first["receiver_root"]) == expected.resolve()
    assert Path(second["receiver_root"]) == expected.resolve()
    assert first["candidate_plan"] != second["candidate_plan"]
    state = json.loads((expected / "state.json").read_text(encoding="utf-8"))
    assert state["campaign_id"] == first["campaign_id"]
    assert state["revision"] == 0
    assert not (expected / "migration.json").exists()


def test_publish_next_arm_is_atomic_idempotent_and_monotonic_for_composition(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    published = publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        campaign_fingerprint="a" * 64,
        mailbox_packet_sha256="b" * 64,
        observed_at=100,
    )
    assert read_next_arm(root) == published
    assert publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        campaign_fingerprint="a" * 64,
        mailbox_packet_sha256="b" * 64,
        observed_at=100,
    ) == published
    next_published = publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "1" * 64,
        dispatch_sequence=2,
        campaign_fingerprint="a" * 64,
        mailbox_packet_sha256="c" * 64,
        observed_at=101,
    )
    assert read_next_arm(root) == next_published
    assert next_published["dispatch_sequence"] == 2
    rolled_release = publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "2" * 64,
        dispatch_sequence=3,
        campaign_fingerprint="b" * 64,
        mailbox_packet_sha256="d" * 64,
        observed_at=102,
    )
    assert read_next_arm(root) == rolled_release
    assert rolled_release["dispatch_sequence"] == 3
    assert rolled_release["campaign_fingerprint"] == "b" * 64
    with pytest.raises(ParameterQueueError, match="conflicts"):
        publish_next_arm(
            root,
            dispatch_identity="dispatch:v1:" + "2" * 64,
            dispatch_sequence=3,
            campaign_fingerprint="b" * 64,
            mailbox_packet_sha256="b" * 64,
            observed_at=102,
        )
    with pytest.raises(ParameterQueueError, match="must increase"):
        publish_next_arm(
            root,
            dispatch_identity="dispatch:v1:" + "0" * 64,
            dispatch_sequence=1,
            campaign_fingerprint="a" * 64,
            mailbox_packet_sha256="b" * 64,
            observed_at=100,
        )


def test_publish_next_arm_delayed_retry_is_idempotent_for_observed_at(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    published = publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        campaign_fingerprint="a" * 64,
        mailbox_packet_sha256="b" * 64,
        observed_at=100,
    )
    delayed = publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        campaign_fingerprint="a" * 64,
        mailbox_packet_sha256="b" * 64,
        observed_at=200,
    )
    assert delayed == published
    assert delayed["observed_at"] == 100


def test_publish_next_arm_conflicting_retry_rejects_same_sequence_with_different_immutable_fields(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        campaign_fingerprint="a" * 64,
        mailbox_packet_sha256="b" * 64,
        observed_at=100,
    )
    with pytest.raises(ParameterQueueError, match="conflicts"):
        publish_next_arm(
            root,
            dispatch_identity="dispatch:v1:" + "1" * 64,
            dispatch_sequence=1,
            campaign_fingerprint="a" * 64,
            mailbox_packet_sha256="b" * 64,
            observed_at=100,
        )


def test_finish_dispatch_with_optional_composition_writes_terminal_receipt(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.0008408964152537145,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
    packet = dispatch["packet"]
    finish_dispatch(
        root,
        status="SUCCEEDED",
        observed={
            "campaign_epoch": packet["campaign_epoch"],
            "trial_id": packet["trial_id"],
            "state": 78,
            "candidate_token": packet["candidate_token"],
            "execution_profile_id": packet["execution_profile_id"],
            "consumed_command_seq": packet["command_seq"],
            "logical_batch_sequence": packet["logical_batch_sequence"],
            "batch_row_index": 1,
            "terminal_reason": 1,
        },
        process_composition_sha256="d" * 64,
    )
    receipts = tuple((root / "governance" / "terminal_receipts").glob("*.json"))
    assert len(receipts) == 1


def test_next_arm_read_missing_and_schema_validation(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    assert read_next_arm(root) is None
    with pytest.raises(ParameterQueueError, match="SHA-256"):
        publish_next_arm(
            root,
            dispatch_identity="dispatch:v1:" + "0" * 64,
            dispatch_sequence=1,
            campaign_fingerprint="not-a-sha",
            mailbox_packet_sha256="b" * 64,
            observed_at=100,
        )


def test_record_terminal_receipt_enforces_identity_and_sequence_and_duplicate_replay_fails(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    composition = "c" * 64
    publish_next_arm(
        root,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        campaign_fingerprint=composition,
        mailbox_packet_sha256="d" * 64,
        observed_at=10,
    )
    for index in range(1, 11):
        record_terminal_receipt(
            root,
            process_composition_sha256=composition,
            dispatch_identity=f"dispatch:v1:{index:064x}",
            dispatch_sequence=index,
            terminal_state={"state": index},
        )
    with pytest.raises(ParameterQueueError, match="already exists"):
        record_terminal_receipt(
            root,
            process_composition_sha256=composition,
            dispatch_identity=f"dispatch:v1:{1:064x}",
            dispatch_sequence=11,
            terminal_state={"state": 10},
        )
    with pytest.raises(ParameterQueueError, match="monotonic"):
        record_terminal_receipt(
            root,
            process_composition_sha256=composition,
            dispatch_identity="dispatch:v1:" + "2" * 64,
            dispatch_sequence=10,
            terminal_state={"state": 10},
        )


def test_record_terminal_receipt_delayed_retry_returns_existing_record(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    composition = "c" * 64
    first = record_terminal_receipt(
        root,
        process_composition_sha256=composition,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        terminal_state={"state": 1},
    )
    second = record_terminal_receipt(
        root,
        process_composition_sha256=composition,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        terminal_state={"state": 1},
    )
    assert second == first
    assert len(tuple((root / "governance" / "terminal_receipts").glob("*.json"))) == 1


def test_record_terminal_receipt_conflicting_retry_is_rejected_for_same_dispatch_identity(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    composition = "c" * 64
    record_terminal_receipt(
        root,
        process_composition_sha256=composition,
        dispatch_identity="dispatch:v1:" + "0" * 64,
        dispatch_sequence=1,
        terminal_state={"state": 1},
    )
    with pytest.raises(ParameterQueueError, match="already exists"):
        record_terminal_receipt(
            root,
            process_composition_sha256=composition,
            dispatch_identity="dispatch:v1:" + "0" * 64,
            dispatch_sequence=1,
            terminal_state={"state": 2},
        )


def test_finish_dispatch_with_identical_governance_receipt_is_idempotent_while_inflight(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.0008408964152537145,
        force_i=0.00001,
        force_damping=5.886274906776001,
    )
    bind_home(root, campaign_epoch=1, last_trial_id=0, last_command_seq=0)
    dispatch = prepare_next_dispatch(root)
    assert dispatch is not None
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
        "terminal_reason": 1,
    }
    first = finish_dispatch(
        root,
        status="SUCCEEDED",
        observed=observed,
        process_composition_sha256="d" * 64,
    )
    state_path = root / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["inflight"] = {
        "dispatch_sequence": dispatch["dispatch_sequence"],
        "request_uid": dispatch["request"]["request_uid"],
    }
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    replay = finish_dispatch(
        root,
        status="SUCCEEDED",
        observed=observed,
        process_composition_sha256="d" * 64,
    )
    assert replay == first
    assert len(tuple((root / "governance" / "terminal_receipts").glob("*.json"))) == 1
    assert status(root)["terminal_receipt_count"] == 1
    assert status(root)["inflight"] is None


def test_terminal_receipts_are_transport_records_not_readiness_claims(
    tmp_path: Path,
) -> None:
    root = _queue(tmp_path)
    composition = "c" * 64
    record_terminal_receipt(
        root,
        process_composition_sha256=composition,
        dispatch_identity=f"dispatch:v1:{'e'*64}",
        dispatch_sequence=10,
        terminal_state={"state": 10},
    )
    records = tuple((root / "governance" / "terminal_receipts").glob("*.json"))
    assert len(records) == 1
