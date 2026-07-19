from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

import build_step5d_autotune_v3_return_route_evidence as return_builder  # noqa: E402
import step5d_autotune_v3.arming as arming  # noqa: E402
from step5d_autotune_v3.arming import (  # noqa: E402
    ArmingError,
    BridgeStartContext,
    CAMPAIGN_ARMING_CONTEXT_SCHEMA,
    load_campaign_arming_context,
)
from step5d_autotune_v3.identity_layers import (  # noqa: E402
    release_basis_fingerprint,
    release_fingerprint,
)
from step5d_autotune_v3.profile import active_identity_snapshot  # noqa: E402
from ur10e_experiment_runtime.authorization import (  # noqa: E402
    CERTIFICATION_PROCEDURES,
    CampaignAuthorization,
    CertificationMotionAuthorization,
    STEP5D_V3_STAGE_IDENTITY,
)
from ur10e_experiment_runtime.identity import canonical_sha256  # noqa: E402
from ur10e_experiment_runtime.moving_sphere import (  # noqa: E402
    build_offline_fixture_stopping_bound,
)
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)


def _write(path: Path, payload: object, *, compact: bool = False) -> Path:
    if compact:
        encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
    else:
        encoded = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False)
    path.write_text(encoded + "\n", encoding="utf-8")
    return path.resolve()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bridge_context(*, plant_epoch: int = 11) -> tuple[BridgeStartContext, dict[str, object]]:
    identity = active_identity_snapshot()
    runtime_environment = "8" * 64
    basis = release_basis_fingerprint(
        tick_semantics_fingerprint=identity["tick_semantics_fingerprint"],
        timing_harness_fingerprint=identity["timing_harness_fingerprint"],
        runtime_environment_fingerprint=runtime_environment,
        deployment_fingerprint=identity["deployment_fingerprint"],
        orchestration_fingerprint=identity["orchestration_fingerprint"],
        plant_epoch=plant_epoch,
    )
    readback_path = ROOT / "config/step5d_autotune_controller_readback_v3.json"
    return (
        BridgeStartContext(
            tick_semantics_fingerprint=identity["tick_semantics_fingerprint"],
            timing_harness_fingerprint=identity["timing_harness_fingerprint"],
            runtime_environment_fingerprint=runtime_environment,
            deployment_fingerprint=identity["deployment_fingerprint"],
            orchestration_fingerprint=identity["orchestration_fingerprint"],
            release_basis_fingerprint=basis,
            local_triplet_sha256=identity["local_triplet_sha256"],
            plant_epoch=plant_epoch,
            deployment_readback_sha256=_file_sha256(readback_path),
            timing_acceptance_sha256="9" * 64,
        ),
        identity,
    )


def _certification_authorization(
    context: BridgeStartContext,
) -> CertificationMotionAuthorization:
    issued = datetime(2026, 7, 20, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    return CertificationMotionAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        release_basis_fingerprint=context.release_basis_fingerprint,
        deployment_fingerprint=context.deployment_fingerprint,
        plant_epoch=context.plant_epoch,
        deployment_readback_sha256=context.deployment_readback_sha256,
        allowed_procedures=CERTIFICATION_PROCEDURES,
        max_linear_speed_m_s=0.09,
        max_linear_acceleration_m_s2=0.135,
        max_angular_speed_rad_s=0.05,
        max_angular_acceleration_rad_s2=0.1,
        numeric_margin_m=0.0001,
        authorization_source="attended test owner",
        authorized_at=issued.isoformat(),
        expires_at=(issued + timedelta(hours=1)).isoformat(),
    )


def test_authorization_reference_separates_file_and_canonical_digests(
    tmp_path: Path,
) -> None:
    context, _ = _bridge_context()
    authorization = _certification_authorization(context)
    pretty = _write(tmp_path / "pretty.json", authorization.document())
    compact = _write(
        tmp_path / "compact.json",
        authorization.document(),
        compact=True,
    )
    canonical = authorization.authorization_ref_sha256
    assert _file_sha256(pretty) != _file_sha256(compact)
    assert canonical == canonical_sha256(authorization.document())

    for path in (pretty, compact):
        loaded_path, file_digest, semantic_digest = arming._authorization_reference(
            {
                "path": str(path),
                "file_sha256": _file_sha256(path),
                "authorization_ref_sha256": canonical,
            },
            "certification authorization",
        )
        assert loaded_path == path
        assert file_digest == _file_sha256(path)
        assert semantic_digest == canonical

    with pytest.raises(ArmingError, match="file digest differs"):
        arming._authorization_reference(
            {
                "path": str(pretty),
                "file_sha256": canonical,
                "authorization_ref_sha256": _file_sha256(pretty),
            },
            "certification authorization",
        )


def test_campaign_arming_context_requires_both_typed_authorization_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bridge, static_identity = _bridge_context()
    bridge_path = _write(tmp_path / "bridge.json", bridge.document())
    certification = _certification_authorization(bridge)
    certification_path = _write(
        tmp_path / "certification.json", certification.document()
    )
    stopping_path = _write(tmp_path / "stopping.json", {"fixture": "stopping"})
    return_path = _write(tmp_path / "return.json", {"fixture": "return"})
    stopping_bound = build_offline_fixture_stopping_bound(
        reaction_latency_s=0.01,
        acceleration_growth_m_s2=0.1,
        minimum_deceleration_m_s2=1.0,
        center_speed_bound_m_s=0.003,
        center_acceleration_bound_m_s2=0.00015,
        numeric_margin_m=0.0001,
        evidence_sha256="a" * 64,
        validity_domain="step5d_v3_test_not_live",
    )
    return_subject = "b" * 64
    final_release = release_fingerprint(
        release_basis_fingerprint=bridge.release_basis_fingerprint,
        stopping_bound_fingerprint=stopping_bound.fingerprint,
        return_evidence_fingerprint=return_subject,
    )
    campaign_fingerprint = "c" * 64
    issued = datetime(2026, 7, 20, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    campaign = CampaignAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        campaign_id="round-a",
        campaign_epoch=1,
        campaign_fingerprint=campaign_fingerprint,
        release_fingerprint=final_release,
        deployment_fingerprint=bridge.deployment_fingerprint,
        plant_epoch=bridge.plant_epoch,
        deployment_readback_sha256=bridge.deployment_readback_sha256,
        authorization_source="attended test owner",
        authorized_at=issued.isoformat(),
        expires_at=(issued + timedelta(hours=1)).isoformat(),
    )
    campaign_path = _write(tmp_path / "campaign.json", campaign.document())

    monkeypatch.setattr(
        arming,
        "_current_source_sha256",
        lambda *_args, **_kwargs: {
            "bridge": "d" * 64,
            "moving_sphere": "e" * 64,
            "stage_adapter": "f" * 64,
        },
    )
    monkeypatch.setattr(
        arming,
        "load_stopping_bound_artifact",
        lambda *_args, **_kwargs: stopping_bound,
    )
    monkeypatch.setattr(
        arming,
        "_load_certified_return_evidence",
        lambda *_args, **_kwargs: ({"fixture": "return"}, return_subject),
    )

    document = {
        "schema": CAMPAIGN_ARMING_CONTEXT_SCHEMA,
        "bridge_start_context": {
            "path": str(bridge_path),
            "sha256": _file_sha256(bridge_path),
        },
        "certification_authorization": {
            "path": str(certification_path),
            "file_sha256": _file_sha256(certification_path),
            "authorization_ref_sha256": certification.authorization_ref_sha256,
        },
        "stopping_bound": {
            "path": str(stopping_path),
            "sha256": _file_sha256(stopping_path),
        },
        "return_evidence": {
            "path": str(return_path),
            "sha256": _file_sha256(return_path),
        },
        "campaign_authorization": {
            "path": str(campaign_path),
            "file_sha256": _file_sha256(campaign_path),
            "authorization_ref_sha256": campaign.authorization_ref_sha256,
        },
        "campaign": {
            "campaign_id": "round-a",
            "campaign_epoch": 1,
            "campaign_fingerprint": campaign_fingerprint,
        },
        "release_fingerprint": final_release,
        "physical_prior_fingerprint": STEP5D_V3_PHYSICAL_PRIOR.fingerprint,
    }
    context_path = _write(tmp_path / "arming.json", document)
    loaded = load_campaign_arming_context(
        context_path,
        expected_static_identity=static_identity,
        now=issued + timedelta(minutes=10),
    )
    assert loaded.release_fingerprint == final_release
    assert (
        loaded.certification_authorization_sha256
        == certification.authorization_ref_sha256
    )
    assert loaded.certification_authorization_sha256 != _file_sha256(
        certification_path
    )
    assert loaded.context_file_sha256 == _file_sha256(context_path)

    swapped = deepcopy(document)
    swapped["campaign_authorization"] = {
        "path": str(campaign_path),
        "file_sha256": campaign.authorization_ref_sha256,
        "authorization_ref_sha256": _file_sha256(campaign_path),
    }
    swapped_path = _write(tmp_path / "arming-swapped.json", swapped)
    with pytest.raises(ArmingError, match="file digest differs"):
        load_campaign_arming_context(
            swapped_path,
            expected_static_identity=static_identity,
            now=issued + timedelta(minutes=10),
        )


def test_return_subject_digest_excludes_verifier_provenance(tmp_path: Path) -> None:
    bridge, _ = _bridge_context()
    authorization_ref = "a" * 64
    document = return_builder.build_document(include_ursim_trace=False)
    document.update(
        {
            "status": "certified_attended_return_measurement",
            "certified": True,
            "optimizer_eligible": False,
            "certification_authorization_sha256": authorization_ref,
            "plant_epoch": bridge.plant_epoch,
            "deployment_readback_sha256": bridge.deployment_readback_sha256,
            "motion_capable_ursim_trace_sha256": "b" * 64,
            "motion_capable_ursim_trace_binding": {
                "claim_role": "fixture"
            },
            "attended_controller_readback_sha256": "c" * 64,
            "source_exact_return_telemetry_sha256": "d" * 64,
            "telemetry_summary": {"maxima": {"sample_gap_s": 0.002}},
            "live_effect": (
                "return_route_angular_envelope_certified_for_exact_epoch"
            ),
        }
    )
    document.pop("missing_certification", None)
    binding = canonical_sha256(
        {
            "schema": "ur-exp/step5d-return-route-certification-binding-v1",
            "source_binding_sha256": document["source_binding_sha256"],
            "triplet_sha256": dict(bridge.local_triplet_sha256),
            "deployment_readback_sha256": bridge.deployment_readback_sha256,
            "certification_authorization_sha256": authorization_ref,
            "plant_epoch": bridge.plant_epoch,
            "motion_capable_ursim_trace_sha256": "b" * 64,
            "attended_controller_readback_sha256": "c" * 64,
            "source_exact_return_telemetry_sha256": "d" * 64,
        }
    )
    document["certification_binding_sha256"] = binding
    first_path = _write(tmp_path / "return-a.json", document)
    _, first_subject = arming._load_certified_return_evidence(
        first_path,
        context=bridge,
        certification_authorization_sha256=authorization_ref,
    )

    changed = deepcopy(document)
    changed["verifier_provenance"] = {
        "fingerprint": "e" * 64,
        "source_sha256": {"new_verifier": "f" * 64},
    }
    second_path = _write(tmp_path / "return-b.json", changed)
    _, second_subject = arming._load_certified_return_evidence(
        second_path,
        context=bridge,
        certification_authorization_sha256=authorization_ref,
    )
    assert first_subject == second_subject == binding
