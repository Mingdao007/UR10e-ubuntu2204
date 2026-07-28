from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_campaign_start import (  # noqa: E402
    CampaignStartCoordinator,
    build_script1_loaded_receipt,
    build_script1_played_receipt,
)
from step5d_no_tube_handoff import (  # noqa: E402
    HandoffError,
    HandoffState,
    HandoffStateStore,
    load_manifest,
)


MANIFEST_PATH = ROOT / "config/step5d/no_tube_handoff.json"


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


def _script1_response(action: str, running: bool) -> dict:
    return {
        "action": action,
        "accepted": True,
        "program_id": "step5d_autotune_start_hover_r001",
        "controller_target": "/programs/andyl/kunwei/step5/step5d_autotune_start_hover_r001.urp",
        "safety_mode": "NORMAL",
        "program_running": running,
    }


def test_script1_trigger_rejects_wrong_identity_or_safety() -> None:
    manifest = load_manifest(MANIFEST_PATH)
    wrong_identity = _script1_response("LOAD", False)
    wrong_identity["program_id"] = "wrong-program"
    with pytest.raises(HandoffError, match="program_id"):
        build_script1_loaded_receipt(manifest, wrong_identity)

    wrong_safety = _script1_response("PLAY", True)
    wrong_safety["safety_mode"] = "PROTECTIVE_STOP"
    with pytest.raises(HandoffError, match="safety_mode"):
        build_script1_played_receipt(manifest, wrong_safety)


class _Script1:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def load(self, _manifest):
        self.events.append("script1.load")
        return _script1_response("LOAD", False)

    def play(self, _manifest):
        self.events.append("script1.play")
        return _script1_response("PLAY", True)


class _Home:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def observe(self, _manifest):
        self.events.append("home.observe")
        return [_home_sample(1_000_000_000, 10), _home_sample(1_500_000_000, 20)]


class _R026Loader:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def load(self, manifest):
        self.events.append("r026.load")
        return {
            "action": "LOAD",
            "accepted": True,
            "program_id": manifest.payload["script2"]["program_id"],
            "controller_target": manifest.payload["script2"]["controller_target"],
            "safety_mode": "NORMAL",
            "program_running": False,
        }


class _R026Identity:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def observe(self, manifest):
        self.events.append("r026.identity")
        return {
            "program_id": manifest.payload["script2"]["program_id"],
            "controller_target": manifest.payload["script2"]["controller_target"],
            "execution_profile_id": manifest.payload["script2"]["execution_profile_id"],
            "release_manifest_sha256": manifest.payload["script2"]["release_manifest_sha256"],
            "triplet_sha256": manifest.payload["script2"]["sha256"],
        }


class _Bridge:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def start(self, manifest):
        self.events.append("bridge.start")
        return {
            "bridge_id": "bridge-test-1",
            "controller_program_id": manifest.payload["script2"]["program_id"],
            "release_manifest_sha256": manifest.payload["script2"]["release_manifest_sha256"],
            "heartbeat_fresh": True,
            "lease_valid": True,
            "single_writer": True,
            "safety_normal": True,
        }


class _Campaign:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def play(self, manifest):
        self.events.append("campaign.play")
        return {
            "action": "PLAY",
            "accepted": True,
            "program_id": manifest.payload["script2"]["program_id"],
            "program_running": True,
            "safety_mode": "NORMAL",
            "bridge_id": "bridge-test-1",
        }


def test_campaign_start_coordinator_enforces_order_and_receipts(tmp_path: Path) -> None:
    manifest = load_manifest(MANIFEST_PATH)
    events: list[str] = []
    queue = tmp_path / "queue"
    store = HandoffStateStore(tmp_path / "handoff", handoff_id=manifest.campaign_id)

    def queue_viewer(root: Path):
        assert root == queue
        events.append("queue.ready")
        return {
            "state": {"revision": 8, "inflight": None},
            "pending_requests": [
                {"control_candidate_uid": f"candidate-{index}", "request_uid": f"request-{index}"}
                for index in range(8)
            ],
        }

    result = CampaignStartCoordinator(
        manifest,
        store,
        queue_root=queue,
        queue_viewer=queue_viewer,
        script1=_Script1(events),
        home=_Home(events),
        r026_loader=_R026Loader(events),
        r026_identity=_R026Identity(events),
        bridge=_Bridge(events),
        campaign=_Campaign(events),
    ).run()

    assert result["status"] == "CAMPAIGN_RUNNING"
    assert events == [
        "queue.ready",
        "script1.load",
        "script1.play",
        "home.observe",
        "r026.load",
        "r026.identity",
        "bridge.start",
        "campaign.play",
    ]
    assert store.status()["state"] == HandoffState.CAMPAIGN_RUNNING.value
    assert len(list((tmp_path / "handoff" / "receipts").glob("*.json"))) == 9


def test_campaign_start_cli_requires_explicit_offline_mode() -> None:
    from step5d_campaign_start import main

    with pytest.raises(SystemExit, match="fail-closed"):
        main(["campaign-start", "--manifest", str(MANIFEST_PATH)])
