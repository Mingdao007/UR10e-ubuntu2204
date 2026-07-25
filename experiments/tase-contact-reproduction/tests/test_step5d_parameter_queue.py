from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_parameter_manifest import seed_initial_manifest, validate_manifest  # noqa: E402
from step5d_parameter_queue import (  # noqa: E402
    ParameterQueueError,
    bind_home,
    finish_dispatch,
    initialize,
    list_requests,
    list_pending,
    prepare_next_dispatch,
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


def test_dispatch_identity_is_separate_and_data_issue_never_retries(
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
        status="DATA_ISSUE",
        observed=terminal,
        detail="capture missing",
    )
    assert receipt["physical_attempted"] is True
    assert receipt["automatic_retry_allowed"] is False
    assert prepare_next_dispatch(root) is None


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
