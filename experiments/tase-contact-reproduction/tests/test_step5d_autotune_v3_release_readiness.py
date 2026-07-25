from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_autotune_v3.arming import BridgeStartContext  # noqa: E402
from step5d_autotune_v3.identity_layers import (  # noqa: E402
    release_basis_fingerprint,
    runtime_environment_fingerprint,
)
from step5d_autotune_v3 import readiness  # noqa: E402
from step5d_autotune_v3 import admission  # noqa: E402


V1 = "step5d_strict_rnn_autotune_v1"
V3 = "step5d_strict_rnn_autotune_v3"
R009 = "step5d_strict_rnn_autotune_v3_r009"


def _write(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path.resolve()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "experiment"
    triplet = {".script": "a" * 64, ".txt": "b" * 64, ".urp": "c" * 64}
    identity = {
        "schema": "step5d.autotune-v3/layered-identity-snapshot-v1",
        "tick_semantics_fingerprint": "1" * 64,
        "timing_harness_fingerprint": "2" * 64,
        "runtime_environment_fingerprint": None,
        "deployment_fingerprint": "3" * 64,
        "orchestration_fingerprint": "4" * 64,
        "release_basis_fingerprint": None,
        "release_fingerprint": None,
        "local_triplet_sha256": triplet,
        "controller_readback_triplet_sha256": triplet,
        "verifier_provenance": {},
        "legacy_provenance": {},
    }
    _write(
        root / "config/current_stage.json",
        {
            "current_stage_id": V3,
            "program": V3,
            "selection_state": "current",
        },
    )
    _write(
        root / "config/step5_stage_table.json",
        {
            "stages": [
                {
                    "id": V1,
                    "active": False,
                    "bridge": False,
                    "current_binding": {"is_current": False},
                },
                {
                    "id": V3,
                    "active": True,
                    "bridge": True,
                    "current_binding": {"is_current": True},
                    "package_delivery": {
                        "controller_readback_manifest": "config/readback.json"
                    },
                },
            ]
        },
    )
    _write(
        root / "config/tase_protocol_table.json",
        {"experiment_profiles": {"Step5.step5d_rnn": {"current_program": V3}}},
    )
    _write(
        root / "config/step5d/current.json",
        {
            "schema": "step5d.autotune-v3/current-release-pointer-v1",
            "manifest_path": "config/step5d/releases/fixture/manifest.json",
            "manifest_sha256": "f" * 64,
        },
    )
    _write(root / "config/step5/step5d_autotune_v3_control_contract.json", {})
    readback_payload = {
        "schema": "step5d.autotune.controller-readback/v3",
        "verified": True,
        "program": R009,
        "control_profile_id": V1,
        "triplet_sha256": triplet,
    }
    readback = _write(root / "config/readback.json", readback_payload)
    _write(root / "config/step5d_autotune_controller_readback_v3.json", readback_payload)
    release = SimpleNamespace(
        program_id=R009,
        protocol_id="v3_full_home_rolling_arm_v1",
        manifest_path="config/step5d/releases/fixture/manifest.json",
        manifest_sha256="f" * 64,
        controller_readback={"path": "config/readback.json"},
    )
    contract = {"deployment_tp_identity": {"program": R009}}
    monkeypatch.setattr(readiness, "load_current_release", lambda *_args: release)
    monkeypatch.setattr(
        readiness,
        "verify_release_manifest",
        lambda *_args, **_kwargs: {"ok": True},
    )
    monkeypatch.setattr(readiness, "load_contract", lambda *_args, **_kwargs: contract)
    monkeypatch.setattr(
        readiness,
        "active_identity_snapshot",
        lambda *_args, **_kwargs: dict(identity),
    )
    runtime_manifest = {
        "schema": "step5d.autotune-v3/runtime-environment-identity-v1",
        "environment": {"fixture": "release-readiness"},
    }
    bridge_identity = {
        "tick_semantics_fingerprint": identity["tick_semantics_fingerprint"],
        "timing_harness_fingerprint": identity["timing_harness_fingerprint"],
        "runtime_environment_fingerprint": runtime_environment_fingerprint(
            runtime_manifest["environment"]
        ),
        "deployment_fingerprint": identity["deployment_fingerprint"],
        "orchestration_fingerprint": identity["orchestration_fingerprint"],
    }
    bridge_identity["release_basis_fingerprint"] = release_basis_fingerprint(
        **bridge_identity,
        plant_epoch=7,
    )
    bridge = BridgeStartContext(
        **bridge_identity,
        local_triplet_sha256=triplet,
        tp_program_id=contract["deployment_tp_identity"]["program"],
        plant_epoch=7,
        deployment_readback_sha256=_sha256(readback),
        runtime_environment_manifest=runtime_manifest,
    )
    bridge_path = _write(root / "runtime/bridge-start.json", bridge.document())
    return root, identity, bridge, bridge_path


def test_selected_release_and_bridge_start_are_independent_from_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _identity, bridge, bridge_path = _fixture(tmp_path, monkeypatch)

    report = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
    )

    assert report["selected_release"] == V3
    assert report["deployment_ready"] is True
    assert report["bridge_start_ready"] is True
    assert report["bridge_process_ready"] is False
    assert report["motion_arm_ready"] is False
    assert report["campaign_ready"] is False
    assert report["release_identity"] == bridge.identity


def test_selected_release_rejects_a_different_tp_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _identity, _bridge, bridge_path = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        readiness,
        "load_current_release",
        lambda *_args: (_ for _ in ()).throw(
            readiness.ReleaseIdentityError("release program differs")
        ),
    )

    report = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
    )

    assert report["ok"] is False
    assert report["bridge_start_ready"] is False
    assert report["blockers"] == [
        "canonical_active_release_verification_failed:release program differs"
    ]


def test_historical_tp_disposition_cannot_override_canonical_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _identity, _bridge, bridge_path = _fixture(tmp_path, monkeypatch)
    current_path = root / "config/step5d/current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["tp_program_disposition"] = "known_incompatible_do_not_retry"
    _write(current_path, current)

    report = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
    )

    assert report["deployment_ready"] is True
    assert report["bridge_start_ready"] is True
    assert report["tp_program_start_allowed"] is True
    assert report["tp_program_disposition"] == "controller_readback_verified"
    assert report["blockers"] == []


def test_historical_host_disposition_cannot_override_canonical_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _identity, _bridge, bridge_path = _fixture(tmp_path, monkeypatch)
    current_path = root / "config/step5d/current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    current["host_runtime_disposition"] = "known_incompatible_do_not_retry"
    _write(current_path, current)

    report = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
    )

    assert report["deployment_ready"] is True
    assert report["bridge_start_ready"] is True
    assert report["host_runtime_start_allowed"] is True
    assert report["host_runtime_disposition"] == "canonical_r009_rolling_release_verified"
    assert report["blockers"] == []


def test_runtime_no_arm_claim_requires_live_pid_and_cannot_claim_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _identity, bridge, bridge_path = _fixture(tmp_path, monkeypatch)
    runtime_path = _write(
        root / "runtime/readiness.json",
        {
            "schema": readiness.RUNTIME_READINESS_SCHEMA,
            "selected_release": V3,
            "deployment_ready": True,
            "bridge_start_ready": True,
            "bridge_process_ready": True,
            "motion_arm_ready": False,
            "campaign_ready": False,
            "identity": bridge.identity,
            "bridge_start_context_sha256": _sha256(bridge_path),
            "campaign_arming_context_sha256": None,
            "bridge_process_pid": os.getpid(),
            "bridge_launch_id": "7" * 32,
            "release_fingerprint": None,
        },
    )

    report = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
        runtime_readiness_path=runtime_path,
    )
    assert report["bridge_process_ready"] is True
    assert report["motion_arm_ready"] is False
    assert report["campaign_ready"] is False

    payload = json.loads(runtime_path.read_text(encoding="utf-8"))
    payload["motion_arm_ready"] = True
    _write(runtime_path, payload)
    overclaim = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
        runtime_readiness_path=runtime_path,
    )
    assert overclaim["bridge_process_ready"] is False
    assert any("overclaims capability" in row for row in overclaim["blockers"])


def test_campaign_ready_requires_typed_context_bound_to_running_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _identity, bridge, bridge_path = _fixture(tmp_path, monkeypatch)
    campaign_path = _write(root / "runtime/campaign-arming.json", {"fixture": True})
    fake = SimpleNamespace(
        bridge_start=bridge,
        release_fingerprint="8" * 64,
    )
    monkeypatch.setattr(
        readiness,
        "load_campaign_arming_context",
        lambda *_args, **_kwargs: fake,
    )
    runtime_path = _write(
        root / "runtime/readiness.json",
        {
            "schema": readiness.RUNTIME_READINESS_SCHEMA,
            "selected_release": V3,
            "deployment_ready": True,
            "bridge_start_ready": True,
            "bridge_process_ready": True,
            "motion_arm_ready": True,
            "campaign_ready": True,
            "identity": bridge.identity,
            "bridge_start_context_sha256": _sha256(bridge_path),
            "campaign_arming_context_sha256": _sha256(campaign_path),
            "bridge_process_pid": os.getpid(),
            "bridge_launch_id": "9" * 32,
            "release_fingerprint": fake.release_fingerprint,
        },
    )

    report = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
        campaign_arming_context_path=campaign_path,
        runtime_readiness_path=runtime_path,
    )

    assert report["motion_arm_ready"] is False
    assert report["campaign_ready"] is False
    candidate_plan = _write(root / "campaign/control/candidate_plan.json", {})
    _write(root / "campaign/control/v3_trial_overlays.json", {})
    monkeypatch.setattr(
        admission,
        "verify_first_row_admission",
        lambda *_args, **_kwargs: {"ok": True, "fixture": True},
    )
    admitted = readiness.resolve_release_readiness(
        root,
        bridge_start_context_path=bridge_path,
        campaign_arming_context_path=campaign_path,
        runtime_readiness_path=runtime_path,
        campaign_root=candidate_plan.parents[1],
        launch_profile_path=root / "launch.json",
        campaign_epoch=7,
        ready_consumed_command_seq=0,
    )
    assert admitted["first_row_admission_ready"] is True
    assert admitted["motion_arm_ready"] is True
    assert admitted["campaign_ready"] is True
    assert report["release_fingerprint"] == fake.release_fingerprint
