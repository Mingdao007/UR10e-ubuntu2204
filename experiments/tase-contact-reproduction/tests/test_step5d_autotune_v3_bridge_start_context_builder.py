from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

import build_step5d_autotune_v3_bridge_start_context as builder  # noqa: E402
from step5d_autotune_v3.arming import (  # noqa: E402
    ArmingError,
    BRIDGE_START_CONTEXT_SCHEMA,
    BridgeStartContext,
)


def _readiness(
    *, deployment_ready: bool = True, tp_program_start_allowed: bool = True
) -> dict[str, object]:
    triplet = {".script": "a" * 64, ".txt": "b" * 64, ".urp": "c" * 64}
    return {
        "deployment_ready": deployment_ready,
        "tp_program_start_allowed": tp_program_start_allowed,
        "tp_program_id": "step5d_strict_rnn_autotune_v3_r017",
        "controller_readback_sha256": "d" * 64,
        "identity": {
            "tick_semantics_fingerprint": "1" * 64,
            "timing_harness_fingerprint": "2" * 64,
            "deployment_fingerprint": "3" * 64,
            "orchestration_fingerprint": "4" * 64,
            "local_triplet_sha256": triplet,
        },
    }


def test_builder_emits_self_contained_no_arm_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        builder,
        "resolve_release_readiness",
        lambda _root: _readiness(),
    )
    environment = {
        "capture_mode": "passive_no_pressure_test_no_host_mutation",
        "scheduler": {"policy_name": "SCHED_OTHER", "priority": 0, "nice": 0},
    }

    context = builder.build_context(
        tmp_path,
        plant_epoch=12,
        runtime_environment=environment,
    )
    document = context.document()

    assert document["schema"] == BRIDGE_START_CONTEXT_SCHEMA
    assert document["bridge_start_ready"] is True
    assert document["motion_authorized"] is False
    assert document["campaign_authorized"] is False
    assert document["plant_epoch"] == 12
    assert document["runtime_environment_manifest"]["environment"] == environment
    assert "timing_acceptance_sha256" not in document
    assert "authorization" not in json.dumps(document).lower()
    assert BridgeStartContext.from_document(document) == context

    document["runtime_environment_manifest"]["environment"]["scheduler"][
        "nice"
    ] = 1
    with pytest.raises(ArmingError, match="runtime environment identity differs"):
        BridgeStartContext.from_document(document)


def test_builder_fails_closed_before_matching_v3_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        builder,
        "resolve_release_readiness",
        lambda _root: _readiness(deployment_ready=False),
    )

    with pytest.raises(
        builder.BridgeContextBuildError,
        match="fresh V3 TP read-back",
    ):
        builder.build_context(
            tmp_path,
            plant_epoch=1,
            runtime_environment={"capture_mode": "passive"},
        )


def test_builder_rejects_known_incompatible_tp_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        builder,
        "resolve_release_readiness",
        lambda _root: _readiness(tp_program_start_allowed=False),
    )

    with pytest.raises(
        builder.BridgeContextBuildError,
        match="known_incompatible_do_not_retry",
    ):
        builder.build_context(
            tmp_path,
            plant_epoch=1,
            runtime_environment={"capture_mode": "passive"},
        )


def test_context_output_is_write_once(tmp_path: Path) -> None:
    output = tmp_path / "runtime/bridge-start-context.json"
    payload = {"schema": "fixture", "motion_authorized": False}

    builder.write_once(output, payload)
    assert json.loads(output.read_text(encoding="utf-8")) == payload

    with pytest.raises(
        builder.BridgeContextBuildError,
        match="already exists",
    ):
        builder.write_once(output, payload)
