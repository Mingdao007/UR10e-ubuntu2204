from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_parameter_manifest import (  # noqa: E402
    import_physical_attempt_uids,
    seed_initial_manifest,
    validate_manifest,
)
from step5d_parameter_queue import (  # noqa: E402
    ParameterQueueError,
    bind_home,
    finish_dispatch,
    initialize,
    list_requests,
    list_pending,
    prepare_next_dispatch,
    reconcile_not_consumed,
    status,
    submit,
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


def test_initial_manifest_is_exact_quarter_octave_path() -> None:
    rows = validate_manifest(MANIFEST, launch_profile_path=PROFILE)
    assert len(rows) == 10
    assert all(row["position"] == "tail" for row in rows)
    assert rows[0]["force_p_gain"] == pytest.approx(0.0008408964152537145)
    assert rows[-1]["force_damping"] == pytest.approx(4.949747468305833)


def test_receiver_is_unbounded_file_per_request_and_next_is_fifo(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    for index in range(40):
        p_step = (index % 8) - 4
        d_step = (index // 8) - 2
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
        "execution_profile_id": 633,
        "consumed_command_seq": 10,
        "logical_batch_sequence": 1,
        "batch_row_index": 1,
    }
    receipt = finish_dispatch(
        root,
        status="FAILED",
        observed=terminal,
        detail="capture missing",
        failure_class="DATA_QUALITY",
    )
    assert receipt["physical_attempted"] is True
    assert receipt["automatic_retry_allowed"] is False
    assert receipt["failure_class"] == "DATA_QUALITY"
    assert status(root)["attempted_count"] == 1
    assert prepare_next_dispatch(root) is None


def test_succeeded_requires_no_failure_class_and_failed_class_is_strict(
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
        "execution_profile_id": 633,
        "consumed_command_seq": 1,
        "logical_batch_sequence": 1,
        "batch_row_index": 1,
    }
    with pytest.raises(ParameterQueueError, match="valid failure_class"):
        finish_dispatch(root, status="FAILED", observed=observed, failure_class="NOPE")
    with pytest.raises(ParameterQueueError, match="must not have failure_class"):
        finish_dispatch(
            root,
            status="SUCCEEDED",
            observed=observed,
            failure_class="SOFTWARE",
        )
    receipt = finish_dispatch(root, status="SUCCEEDED", observed=observed)
    assert receipt["schema"].endswith("receipt-v2")
    assert receipt["failure_class"] is None
    assert status(root)["attempted_count"] == 1
    assert len(tuple((root / "physical_attempts").glob("*.json"))) == 1


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
    assert record["physical_attempted"] is False
    assert status(root)["attempted_count"] == 0
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
    assert redispatched["dispatch_sequence"] == 2


def test_crash_restart_preserves_inflight_dispatch_and_v1_complete_imports(
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
    assert request["control_candidate_uid"] in imported


def test_duplicate_parameter_is_rejected_even_with_new_source(tmp_path: Path) -> None:
    root = _queue(tmp_path)
    submit(
        root,
        launch_profile_path=PROFILE,
        force_p=0.001189207115002721,
        force_i=0.00001,
        force_damping=5.886274906776001,
        source="operator",
    )
    with pytest.raises(ParameterQueueError, match="already attempted or queued"):
        submit(
            root,
            launch_profile_path=PROFILE,
            force_p=0.001189207115002721,
            force_i=0.00001,
            force_damping=5.886274906776001,
            source="optimizer",
        )


def test_seed_skips_prior_physical_attempt_and_is_idempotent(tmp_path: Path) -> None:
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
    assert len(seeded) == 9
    assert list_requests(queue)[0]["source"] == "approved_initial_10:P02"
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
