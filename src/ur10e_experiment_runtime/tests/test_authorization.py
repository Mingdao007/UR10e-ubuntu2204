from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from ur10e_experiment_runtime.authorization import (
    AuthorizationError,
    CampaignAuthorization,
    CERTIFICATION_PROCEDURES,
    CertificationMotionAuthorization,
    STEP5D_V3_STAGE_IDENTITY,
    issue_certification_procedure_ticket,
    load_campaign_authorization,
    load_certification_motion_authorization,
)


CONTROL = "a" * 64
ORCHESTRATION = "b" * 64
READBACK = "c" * 64


def document() -> dict[str, object]:
    return CertificationMotionAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        control_fingerprint=CONTROL,
        orchestration_fingerprint=ORCHESTRATION,
        plant_epoch=7,
        deployment_readback_sha256=READBACK,
        allowed_procedures=CERTIFICATION_PROCEDURES,
        max_linear_speed_m_s=0.09,
        max_linear_acceleration_m_s2=0.135,
        max_angular_speed_rad_s=0.05,
        max_angular_acceleration_rad_s2=0.1,
        numeric_margin_m=0.0001,
        authorization_source="attended owner gate",
        authorized_at="2026-07-20T09:00:00+08:00",
        expires_at="2026-07-20T10:00:00+08:00",
    ).document()


def test_certification_authorization_is_narrow_and_identity_bound(tmp_path) -> None:
    path = (tmp_path / "certification-authorization.json").resolve()
    path.write_text(json.dumps(document()), encoding="utf-8")

    authorization = load_certification_motion_authorization(
        path,
        expected_control_fingerprint=CONTROL,
        expected_orchestration_fingerprint=ORCHESTRATION,
        expected_plant_epoch=7,
        expected_deployment_readback_sha256=READBACK,
        now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc),
    )

    assert authorization.allowed_procedures == CERTIFICATION_PROCEDURES
    assert authorization.document()["campaign_allowed"] is False
    assert authorization.document()["optimizer_eligible"] is False
    assert authorization.numeric_margin_m == 0.0001
    assert len(authorization.authorization_ref_sha256) == 64
    authorization.require_procedure(
        "direct_exact_stop",
        now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc),
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("campaign_allowed", True, "escaped its purpose"),
        ("optimizer_eligible", True, "escaped its purpose"),
        ("no_contact", False, "escaped its purpose"),
        ("allowed_procedures", ["return_route"], "procedures/order"),
    ],
)
def test_certification_authorization_cannot_expand_scope(
    field: str, value: object, message: str
) -> None:
    payload = document()
    payload[field] = value
    with pytest.raises(AuthorizationError, match=message):
        CertificationMotionAuthorization.from_document(payload)


def test_certification_authorization_rejects_wrong_epoch_and_expiry(tmp_path) -> None:
    path = (tmp_path / "certification-authorization.json").resolve()
    path.write_text(json.dumps(document()), encoding="utf-8")

    with pytest.raises(AuthorizationError, match="exact deployment epoch"):
        load_certification_motion_authorization(
            path,
            expected_control_fingerprint=CONTROL,
            expected_orchestration_fingerprint=ORCHESTRATION,
            expected_plant_epoch=8,
            expected_deployment_readback_sha256=READBACK,
            now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc),
        )
    with pytest.raises(AuthorizationError, match="not currently valid"):
        load_certification_motion_authorization(
            path,
            expected_control_fingerprint=CONTROL,
            expected_orchestration_fingerprint=ORCHESTRATION,
            expected_plant_epoch=7,
            expected_deployment_readback_sha256=READBACK,
            now=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
        )


def test_certification_authorization_rejects_duplicate_and_nonfinite_json(
    tmp_path,
) -> None:
    duplicate = (tmp_path / "duplicate.json").resolve()
    duplicate.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_certification_motion_authorization(
            duplicate,
            expected_control_fingerprint=CONTROL,
            expected_orchestration_fingerprint=ORCHESTRATION,
            expected_plant_epoch=7,
            expected_deployment_readback_sha256=READBACK,
        )

    nonfinite = document()
    nonfinite["motion_envelope"]["max_linear_speed_m_s"] = float("nan")
    invalid = (tmp_path / "nonfinite.json").resolve()
    invalid.write_text(json.dumps(nonfinite), encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        load_certification_motion_authorization(
            invalid,
            expected_control_fingerprint=CONTROL,
            expected_orchestration_fingerprint=ORCHESTRATION,
            expected_plant_epoch=7,
            expected_deployment_readback_sha256=READBACK,
        )


def test_certification_authorization_rejects_procedure_superset_and_expiry_boundary() -> None:
    payload = document()
    payload["allowed_procedures"] = [*CERTIFICATION_PROCEDURES, "campaign_start"]
    with pytest.raises(AuthorizationError, match="procedures/order"):
        CertificationMotionAuthorization.from_document(payload)
    authorization = CertificationMotionAuthorization.from_document(document())
    with pytest.raises(AuthorizationError, match="not currently valid"):
        authorization.require_procedure(
            "return_route",
            now=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
        )


def test_campaign_authorization_is_typed_and_rejects_certification_schema(
    tmp_path,
) -> None:
    issued = "2026-07-20T09:00:00+08:00"
    campaign = CampaignAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        campaign_id="round-a",
        campaign_epoch=3,
        campaign_fingerprint="d" * 64,
        control_fingerprint=CONTROL,
        orchestration_fingerprint=ORCHESTRATION,
        plant_epoch=7,
        deployment_readback_sha256=READBACK,
        authorization_source="attended owner gate",
        authorized_at=issued,
        expires_at="2026-07-20T10:00:00+08:00",
    )
    path = (tmp_path / "campaign.json").resolve()
    path.write_text(json.dumps(campaign.document()), encoding="utf-8")
    loaded = load_campaign_authorization(
        path,
        expected_campaign_id="round-a",
        expected_campaign_epoch=3,
        expected_campaign_fingerprint="d" * 64,
        expected_control_fingerprint=CONTROL,
        expected_orchestration_fingerprint=ORCHESTRATION,
        expected_plant_epoch=7,
        expected_deployment_readback_sha256=READBACK,
        now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc),
    )
    assert loaded.authorization_ref_sha256 != (
        CertificationMotionAuthorization.from_document(document())
        .authorization_ref_sha256
    )
    path.write_text(json.dumps(document()), encoding="utf-8")
    with pytest.raises(AuthorizationError, match="campaign authorization fields differ"):
        load_campaign_authorization(
            path,
            expected_campaign_id="round-a",
            expected_campaign_epoch=3,
            expected_campaign_fingerprint="d" * 64,
            expected_control_fingerprint=CONTROL,
            expected_orchestration_fingerprint=ORCHESTRATION,
            expected_plant_epoch=7,
            expected_deployment_readback_sha256=READBACK,
        )


def test_procedure_ticket_is_pre_motion_narrow_and_rechecks_expiry() -> None:
    authorization = CertificationMotionAuthorization.from_document(document())
    now = datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc)
    ticket = issue_certification_procedure_ticket(
        authorization,
        "return_route",
        now=now,
    )
    assert ticket.document()["no_contact"] is True
    assert ticket.document()["campaign_allowed"] is False
    assert ticket.document()["optimizer_eligible"] is False
    assert ticket.authorization_ref_sha256 == authorization.authorization_ref_sha256
    assert len(ticket.ticket_sha256) == 64

    with pytest.raises(AuthorizationError, match="not currently valid"):
        issue_certification_procedure_ticket(
            authorization,
            "return_route",
            now=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
        )


def test_campaign_authorization_cannot_issue_a_certification_ticket() -> None:
    campaign = CampaignAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        campaign_id="round-a",
        campaign_epoch=3,
        campaign_fingerprint="d" * 64,
        control_fingerprint=CONTROL,
        orchestration_fingerprint=ORCHESTRATION,
        plant_epoch=7,
        deployment_readback_sha256=READBACK,
        authorization_source="attended owner gate",
        authorized_at="2026-07-20T09:00:00+08:00",
        expires_at="2026-07-20T10:00:00+08:00",
    )
    with pytest.raises(AuthorizationError, match="certification motion authorization"):
        issue_certification_procedure_ticket(
            campaign,  # type: ignore[arg-type]
            "return_route",
            now=datetime(2026, 7, 20, 1, 30, tzinfo=timezone.utc),
        )


def test_return_route_envelope_fits_certification_authorization() -> None:
    from ur10e_experiment_runtime.return_route import (
        RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2,
        RETURN_ANGULAR_SPEED_LIMIT_RAD_S,
        RETURN_SEGMENT_HORIZONTAL_ACCELERATION_M_S2,
        RETURN_SEGMENT_HORIZONTAL_SPEED_M_S,
        RETURN_SEGMENT_VERTICAL_ACCELERATION_M_S2,
        RETURN_SEGMENT_VERTICAL_SPEED_M_S,
    )

    authorization = CertificationMotionAuthorization.from_document(document())
    assert max(
        RETURN_SEGMENT_HORIZONTAL_SPEED_M_S,
        RETURN_SEGMENT_VERTICAL_SPEED_M_S,
    ) <= authorization.max_linear_speed_m_s
    assert max(
        RETURN_SEGMENT_HORIZONTAL_ACCELERATION_M_S2,
        RETURN_SEGMENT_VERTICAL_ACCELERATION_M_S2,
    ) <= authorization.max_linear_acceleration_m_s2
    assert RETURN_ANGULAR_SPEED_LIMIT_RAD_S <= authorization.max_angular_speed_rad_s
    assert (
        RETURN_ANGULAR_ACCELERATION_LIMIT_RAD_S2
        <= authorization.max_angular_acceleration_rad_s2
    )
