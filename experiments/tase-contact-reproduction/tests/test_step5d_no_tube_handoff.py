from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_no_tube_handoff import (  # noqa: E402
    HOME_VERIFIED_SCHEMA,
    HandoffError,
    HandoffState,
    HandoffStateStore,
    build_home_verified_receipt,
    build_queue_ready_receipt,
    load_manifest,
    validate_release_binding,
)
MANIFEST = ROOT / "config/step5d/no_tube_handoff.json"


def test_existing_script_pair_is_hash_closed_and_r026_current() -> None:
    manifest = load_manifest(MANIFEST)
    binding = validate_release_binding(manifest)

    assert binding["script1"]["program_id"] == "step5d_autotune_start_hover_r001"
    assert binding["script2"]["program_id"] == "step5d_strict_rnn_autotune_v3_r026"
    assert binding["script2"]["sha256"][".script"].startswith("bd0058")


def test_queue_ready_counts_pending_not_inflight(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST)
    pending = [
        {"control_candidate_uid": f"candidate-{index}", "request_uid": f"request-{index}"}
        for index in range(8)
    ]
    receipt = build_queue_ready_receipt(
        manifest,
        {
            "state": {"revision": 11, "inflight": {"request_uid": "inflight"}},
            "pending_requests": pending,
        },
    )

    assert receipt["status"] == "QUEUE_READY"
    assert receipt["pending_depth"] == 8
    assert receipt["inflight"] == {"request_uid": "inflight"}
    assert receipt["high_watermark"] == 8
    assert receipt["low_watermark"] == 4
    assert len(receipt["candidate_ids"]) == 8


def test_queue_ready_rejects_short_pending_buffer() -> None:
    manifest = load_manifest(MANIFEST)
    with pytest.raises(HandoffError, match="high watermark"):
        build_queue_ready_receipt(
            manifest,
            {"state": {"revision": 1, "inflight": None}, "pending_requests": []},
        )


def _home_sample(monotonic_ns: int, controller_ns: int) -> dict:
    return {
        "program_id": "step5d_autotune_start_hover_r001",
        "program_running": False,
        "program_state": "STOPPED",
        "safety_mode": "NORMAL",
        "observed_monotonic_ns": monotonic_ns,
        "controller_timestamp_ns": controller_ns,
        "tcp_pose": [0.487834547, 0.129337053, 0.033, 3.120752062, 0.0, 0.068626833],
        "tcp_speed": [0.0] * 6,
        "actual_q": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        "qdot": [0.0] * 6,
    }


def test_home_verified_requires_pose_speed_qdot_and_dwell() -> None:
    manifest = load_manifest(MANIFEST)
    receipt = build_home_verified_receipt(
        manifest,
        [_home_sample(1_000_000_000, 10), _home_sample(1_500_000_000, 20)],
    )

    assert receipt["schema"] == HOME_VERIFIED_SCHEMA
    assert receipt["status"] == "HOME_VERIFIED"
    assert receipt["stationary_dwell_s"] == pytest.approx(0.5)
    assert receipt["observed_actual_q"] == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    assert receipt["target_joint_q"] is None


def test_home_verified_rejects_running_or_short_dwell() -> None:
    manifest = load_manifest(MANIFEST)
    running = _home_sample(1_000_000_000, 10)
    running["program_running"] = True
    with pytest.raises(HandoffError, match="stopped"):
        build_home_verified_receipt(manifest, [running, _home_sample(1_500_000_000, 20)])
    with pytest.raises(HandoffError, match="dwell"):
        build_home_verified_receipt(manifest, [_home_sample(1_000_000_000, 10), _home_sample(1_100_000_000, 20)])


def test_state_store_requires_every_transition_in_order(tmp_path: Path) -> None:
    store = HandoffStateStore(tmp_path / "handoff", handoff_id="handoff-test")
    store.transition(HandoffState.RELEASE_READY, {"validated": True})
    store.transition(HandoffState.QUEUE_READY, {"pending_depth": 8})
    store.transition(HandoffState.SCRIPT1_LOADED, {"program_id": "script1"})

    with pytest.raises(HandoffError, match="invalid handoff transition"):
        store.transition(HandoffState.HOME_VERIFIED, {})

    status = store.status()
    assert status["state"] == HandoffState.SCRIPT1_LOADED.value
    assert status["revision"] == 2
    assert len(list((tmp_path / "handoff" / "receipts").glob("*.json"))) == 3
