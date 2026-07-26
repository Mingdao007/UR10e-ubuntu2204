"""Typed authorization contracts for bounded Step5d certification motion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .identity import canonical_sha256, load_strict_json


CERTIFICATION_AUTHORIZATION_SCHEMA = (
    "ur-exp/step5d-certification-motion-authorization-v2"
)
CERTIFICATION_PROCEDURE_TICKET_SCHEMA = (
    "ur-exp/step5d-certification-procedure-ticket-v2"
)
CAMPAIGN_AUTHORIZATION_SCHEMA = "ur-exp/step5d-campaign-authorization-v2"
CERTIFICATION_PROCEDURES = (
    "direct_exact_stop",
    "stale_watchdog_exact_stop",
    "return_route",
)
STEP5D_V3_RELEASE_STAGE_ID = "step5d_strict_rnn_autotune_v3"
STEP5D_V3_CONTROL_PROFILE_ID = "step5d_strict_rnn_autotune_v1"
STEP5D_V3_TP_PROGRAM_ID = "step5d_strict_rnn_autotune_v3"
STEP5D_V3_SOURCE_STAGE_ID = "step5d_strict_rnn_ablation_v35"
STEP5D_V3_STAGE_ID = STEP5D_V3_RELEASE_STAGE_ID
CERTIFICATION_LINEAR_SPEED_MAX_M_S = 0.090
CERTIFICATION_LINEAR_ACCELERATION_MAX_M_S2 = 0.135
CERTIFICATION_ANGULAR_SPEED_MAX_RAD_S = 0.050
CERTIFICATION_ANGULAR_ACCELERATION_MAX_RAD_S2 = 0.100
CERTIFICATION_NUMERIC_MARGIN_MAX_M = 0.001


class AuthorizationError(ValueError):
    """An authorization is ambiguous, stale, or outside its narrow purpose."""


@dataclass(frozen=True)
class Step5dV3StageIdentity:
    release_stage_id: str
    control_profile_id: str
    tp_program_id: str
    source_stage_id: str

    def __post_init__(self) -> None:
        if self.document() != {
            "release_stage_id": STEP5D_V3_RELEASE_STAGE_ID,
            "control_profile_id": STEP5D_V3_CONTROL_PROFILE_ID,
            "tp_program_id": STEP5D_V3_TP_PROGRAM_ID,
            "source_stage_id": STEP5D_V3_SOURCE_STAGE_ID,
        }:
            raise AuthorizationError("Step5d V3 stage identity differs")

    def document(self) -> dict[str, str]:
        return {
            "release_stage_id": self.release_stage_id,
            "control_profile_id": self.control_profile_id,
            "tp_program_id": self.tp_program_id,
            "source_stage_id": self.source_stage_id,
        }

    @classmethod
    def from_document(cls, payload: object) -> "Step5dV3StageIdentity":
        fields = {
            "release_stage_id",
            "control_profile_id",
            "tp_program_id",
            "source_stage_id",
        }
        if not isinstance(payload, Mapping) or set(payload) != fields:
            raise AuthorizationError("Step5d V3 stage identity fields differ")
        return cls(**dict(payload))


STEP5D_V3_STAGE_IDENTITY = Step5dV3StageIdentity(
    release_stage_id=STEP5D_V3_RELEASE_STAGE_ID,
    control_profile_id=STEP5D_V3_CONTROL_PROFILE_ID,
    tp_program_id=STEP5D_V3_TP_PROGRAM_ID,
    source_stage_id=STEP5D_V3_SOURCE_STAGE_ID,
)


def _sha256(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise AuthorizationError(f"{name} must be a lowercase SHA256")
    return value


def _timestamp(name: str, value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise AuthorizationError(f"{name} must be a non-empty timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorizationError(f"{name} must include a timezone")
    return parsed


def _positive_limit(name: str, value: Any, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthorizationError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result <= 0.0 or result > maximum:
        raise AuthorizationError(f"{name} exceeds the certification envelope")
    return result


@dataclass(frozen=True)
class CertificationMotionAuthorization:
    """One expiring authorization for no-contact certification procedures only."""

    stage_identity: Step5dV3StageIdentity
    release_basis_fingerprint: str
    deployment_fingerprint: str
    plant_epoch: int
    deployment_readback_sha256: str
    allowed_procedures: tuple[str, ...]
    max_linear_speed_m_s: float
    max_linear_acceleration_m_s2: float
    max_angular_speed_rad_s: float
    max_angular_acceleration_rad_s2: float
    numeric_margin_m: float
    authorization_source: str
    authorized_at: str
    expires_at: str

    def __post_init__(self) -> None:
        if self.stage_identity != STEP5D_V3_STAGE_IDENTITY:
            raise AuthorizationError("certification authorization stage differs")
        _sha256("release_basis_fingerprint", self.release_basis_fingerprint)
        _sha256("deployment_fingerprint", self.deployment_fingerprint)
        _sha256("deployment_readback_sha256", self.deployment_readback_sha256)
        if isinstance(self.plant_epoch, bool) or not isinstance(self.plant_epoch, int):
            raise AuthorizationError("plant_epoch must be an integer")
        if self.plant_epoch < 1:
            raise AuthorizationError("plant_epoch must be positive")
        if self.allowed_procedures != CERTIFICATION_PROCEDURES:
            raise AuthorizationError("certification procedures/order differ")
        _positive_limit(
            "max_linear_speed_m_s",
            self.max_linear_speed_m_s,
            CERTIFICATION_LINEAR_SPEED_MAX_M_S,
        )
        _positive_limit(
            "max_linear_acceleration_m_s2",
            self.max_linear_acceleration_m_s2,
            CERTIFICATION_LINEAR_ACCELERATION_MAX_M_S2,
        )
        _positive_limit(
            "max_angular_speed_rad_s",
            self.max_angular_speed_rad_s,
            CERTIFICATION_ANGULAR_SPEED_MAX_RAD_S,
        )
        _positive_limit(
            "max_angular_acceleration_rad_s2",
            self.max_angular_acceleration_rad_s2,
            CERTIFICATION_ANGULAR_ACCELERATION_MAX_RAD_S2,
        )
        _positive_limit(
            "numeric_margin_m",
            self.numeric_margin_m,
            CERTIFICATION_NUMERIC_MARGIN_MAX_M,
        )
        if not isinstance(self.authorization_source, str) or not self.authorization_source.strip():
            raise AuthorizationError("authorization_source must be non-empty")
        authorized = _timestamp("authorized_at", self.authorized_at)
        expires = _timestamp("expires_at", self.expires_at)
        if expires <= authorized:
            raise AuthorizationError("certification authorization must expire after issue")

    def document(self) -> dict[str, object]:
        return {
            "schema": CERTIFICATION_AUTHORIZATION_SCHEMA,
            "stage_identity": self.stage_identity.document(),
            "release_basis_fingerprint": self.release_basis_fingerprint,
            "deployment_fingerprint": self.deployment_fingerprint,
            "plant_epoch": self.plant_epoch,
            "deployment_readback_sha256": self.deployment_readback_sha256,
            "allowed_procedures": list(self.allowed_procedures),
            "motion_envelope": {
                "max_linear_speed_m_s": self.max_linear_speed_m_s,
                "max_linear_acceleration_m_s2": self.max_linear_acceleration_m_s2,
                "max_angular_speed_rad_s": self.max_angular_speed_rad_s,
                "max_angular_acceleration_rad_s2": (
                    self.max_angular_acceleration_rad_s2
                ),
                "numeric_margin_m": self.numeric_margin_m,
            },
            "no_contact": True,
            "optimizer_eligible": False,
            "campaign_allowed": False,
            "authorization_source": self.authorization_source,
            "authorized_at": self.authorized_at,
            "expires_at": self.expires_at,
        }

    @property
    def authorization_ref_sha256(self) -> str:
        return canonical_sha256(self.document())

    def require_current(self, *, now: datetime | None = None) -> None:
        observed = now if now is not None else datetime.now(timezone.utc)
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise AuthorizationError("authorization comparison time must be zoned")
        authorized = _timestamp("authorized_at", self.authorized_at)
        expires = _timestamp("expires_at", self.expires_at)
        if observed < authorized or observed >= expires:
            raise AuthorizationError("certification authorization is not currently valid")

    def require_procedure(
        self, procedure: str, *, now: datetime | None = None
    ) -> None:
        self.require_current(now=now)
        if procedure not in self.allowed_procedures:
            raise AuthorizationError("procedure is outside certification authorization")

    @classmethod
    def from_document(cls, payload: Any) -> "CertificationMotionAuthorization":
        required = {
            "schema",
            "stage_identity",
            "release_basis_fingerprint",
            "deployment_fingerprint",
            "plant_epoch",
            "deployment_readback_sha256",
            "allowed_procedures",
            "motion_envelope",
            "no_contact",
            "optimizer_eligible",
            "campaign_allowed",
            "authorization_source",
            "authorized_at",
            "expires_at",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise AuthorizationError("certification authorization fields differ")
        if payload["schema"] != CERTIFICATION_AUTHORIZATION_SCHEMA:
            raise AuthorizationError("certification authorization schema differs")
        if (
            payload["no_contact"] is not True
            or payload["optimizer_eligible"] is not False
            or payload["campaign_allowed"] is not False
        ):
            raise AuthorizationError("certification authorization escaped its purpose")
        procedures = payload["allowed_procedures"]
        if not isinstance(procedures, Sequence) or isinstance(procedures, (str, bytes)):
            raise AuthorizationError("certification procedures must be an array")
        envelope = payload["motion_envelope"]
        envelope_fields = {
            "max_linear_speed_m_s",
            "max_linear_acceleration_m_s2",
            "max_angular_speed_rad_s",
            "max_angular_acceleration_rad_s2",
            "numeric_margin_m",
        }
        if not isinstance(envelope, Mapping) or set(envelope) != envelope_fields:
            raise AuthorizationError("certification motion envelope fields differ")
        return cls(
            stage_identity=Step5dV3StageIdentity.from_document(
                payload["stage_identity"]
            ),
            release_basis_fingerprint=payload["release_basis_fingerprint"],
            deployment_fingerprint=payload["deployment_fingerprint"],
            plant_epoch=payload["plant_epoch"],
            deployment_readback_sha256=payload["deployment_readback_sha256"],
            allowed_procedures=tuple(procedures),
            max_linear_speed_m_s=envelope["max_linear_speed_m_s"],
            max_linear_acceleration_m_s2=envelope[
                "max_linear_acceleration_m_s2"
            ],
            max_angular_speed_rad_s=envelope["max_angular_speed_rad_s"],
            max_angular_acceleration_rad_s2=envelope[
                "max_angular_acceleration_rad_s2"
            ],
            numeric_margin_m=envelope["numeric_margin_m"],
            authorization_source=payload["authorization_source"],
            authorized_at=payload["authorized_at"],
            expires_at=payload["expires_at"],
        )


@dataclass(frozen=True)
class CertificationProcedureTicket:
    """Pre-motion proof that one exact no-contact procedure is authorized."""

    authorization_ref_sha256: str
    stage_identity: Step5dV3StageIdentity
    procedure: str
    plant_epoch: int
    deployment_readback_sha256: str
    release_basis_fingerprint: str
    deployment_fingerprint: str
    max_linear_speed_m_s: float
    max_linear_acceleration_m_s2: float
    max_angular_speed_rad_s: float
    max_angular_acceleration_rad_s2: float
    issued_at: str
    expires_at: str

    def __post_init__(self) -> None:
        _sha256("authorization_ref_sha256", self.authorization_ref_sha256)
        if self.stage_identity != STEP5D_V3_STAGE_IDENTITY:
            raise AuthorizationError("certification procedure ticket stage differs")
        if self.procedure not in CERTIFICATION_PROCEDURES:
            raise AuthorizationError("certification procedure ticket is out of scope")
        if isinstance(self.plant_epoch, bool) or not isinstance(self.plant_epoch, int):
            raise AuthorizationError("plant_epoch must be an integer")
        if self.plant_epoch < 1:
            raise AuthorizationError("plant_epoch must be positive")
        _sha256("deployment_readback_sha256", self.deployment_readback_sha256)
        _sha256("release_basis_fingerprint", self.release_basis_fingerprint)
        _sha256("deployment_fingerprint", self.deployment_fingerprint)
        _positive_limit(
            "max_linear_speed_m_s",
            self.max_linear_speed_m_s,
            CERTIFICATION_LINEAR_SPEED_MAX_M_S,
        )
        _positive_limit(
            "max_linear_acceleration_m_s2",
            self.max_linear_acceleration_m_s2,
            CERTIFICATION_LINEAR_ACCELERATION_MAX_M_S2,
        )
        _positive_limit(
            "max_angular_speed_rad_s",
            self.max_angular_speed_rad_s,
            CERTIFICATION_ANGULAR_SPEED_MAX_RAD_S,
        )
        _positive_limit(
            "max_angular_acceleration_rad_s2",
            self.max_angular_acceleration_rad_s2,
            CERTIFICATION_ANGULAR_ACCELERATION_MAX_RAD_S2,
        )
        issued = _timestamp("issued_at", self.issued_at)
        expires = _timestamp("expires_at", self.expires_at)
        if expires <= issued:
            raise AuthorizationError("certification procedure ticket has no lifetime")

    def document(self) -> dict[str, object]:
        return {
            "schema": CERTIFICATION_PROCEDURE_TICKET_SCHEMA,
            "authorization_ref_sha256": self.authorization_ref_sha256,
            "stage_identity": self.stage_identity.document(),
            "procedure": self.procedure,
            "plant_epoch": self.plant_epoch,
            "deployment_readback_sha256": self.deployment_readback_sha256,
            "release_basis_fingerprint": self.release_basis_fingerprint,
            "deployment_fingerprint": self.deployment_fingerprint,
            "motion_envelope": {
                "max_linear_speed_m_s": self.max_linear_speed_m_s,
                "max_linear_acceleration_m_s2": self.max_linear_acceleration_m_s2,
                "max_angular_speed_rad_s": self.max_angular_speed_rad_s,
                "max_angular_acceleration_rad_s2": (
                    self.max_angular_acceleration_rad_s2
                ),
            },
            "no_contact": True,
            "campaign_allowed": False,
            "optimizer_eligible": False,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    @property
    def ticket_sha256(self) -> str:
        return canonical_sha256(self.document())


def issue_certification_procedure_ticket(
    authorization: CertificationMotionAuthorization,
    procedure: str,
    *,
    now: datetime | None = None,
) -> CertificationProcedureTicket:
    """Issue the object a motion runner must consume before one procedure."""

    if not isinstance(authorization, CertificationMotionAuthorization):
        raise AuthorizationError(
            "a certification motion authorization is required for a procedure ticket"
        )
    observed = now if now is not None else datetime.now(timezone.utc)
    authorization.require_procedure(procedure, now=observed)
    return CertificationProcedureTicket(
        authorization_ref_sha256=authorization.authorization_ref_sha256,
        stage_identity=authorization.stage_identity,
        procedure=procedure,
        plant_epoch=authorization.plant_epoch,
        deployment_readback_sha256=authorization.deployment_readback_sha256,
        release_basis_fingerprint=authorization.release_basis_fingerprint,
        deployment_fingerprint=authorization.deployment_fingerprint,
        max_linear_speed_m_s=authorization.max_linear_speed_m_s,
        max_linear_acceleration_m_s2=authorization.max_linear_acceleration_m_s2,
        max_angular_speed_rad_s=authorization.max_angular_speed_rad_s,
        max_angular_acceleration_rad_s2=(
            authorization.max_angular_acceleration_rad_s2
        ),
        issued_at=observed.isoformat(),
        expires_at=authorization.expires_at,
    )


def load_certification_motion_authorization(
    path: str | Path,
    *,
    expected_release_basis_fingerprint: str,
    expected_deployment_fingerprint: str,
    expected_plant_epoch: int,
    expected_deployment_readback_sha256: str,
    now: datetime | None = None,
) -> CertificationMotionAuthorization:
    """Load one external owner-produced authorization and bind it exactly."""

    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise AuthorizationError(
            "certification authorization must be an absolute regular file"
        )
    authorization = CertificationMotionAuthorization.from_document(
        load_strict_json(source)
    )
    if (
        authorization.release_basis_fingerprint
        != expected_release_basis_fingerprint
        or authorization.deployment_fingerprint
        != expected_deployment_fingerprint
        or authorization.plant_epoch != expected_plant_epoch
        or authorization.deployment_readback_sha256
        != expected_deployment_readback_sha256
    ):
        raise AuthorizationError(
            "certification authorization is not bound to the exact deployment epoch"
        )
    authorization.require_current(now=now)
    return authorization


@dataclass(frozen=True)
class CampaignAuthorization:
    """Separate contact-campaign authorization; never accepted for certification."""

    stage_identity: Step5dV3StageIdentity
    campaign_id: str
    campaign_epoch: int
    campaign_fingerprint: str
    release_fingerprint: str
    deployment_fingerprint: str
    plant_epoch: int
    deployment_readback_sha256: str
    authorization_source: str
    authorized_at: str
    expires_at: str

    def __post_init__(self) -> None:
        if self.stage_identity != STEP5D_V3_STAGE_IDENTITY:
            raise AuthorizationError("campaign authorization stage differs")
        if not isinstance(self.campaign_id, str) or not self.campaign_id.strip():
            raise AuthorizationError("campaign_id must be non-empty")
        for name in (
            "campaign_fingerprint",
            "release_fingerprint",
            "deployment_fingerprint",
            "deployment_readback_sha256",
        ):
            _sha256(name, getattr(self, name))
        for name in ("campaign_epoch", "plant_epoch"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise AuthorizationError(f"{name} must be positive")
        if (
            not isinstance(self.authorization_source, str)
            or not self.authorization_source.strip()
        ):
            raise AuthorizationError("authorization_source must be non-empty")
        issued = _timestamp("authorized_at", self.authorized_at)
        expires = _timestamp("expires_at", self.expires_at)
        if expires <= issued:
            raise AuthorizationError("campaign authorization must expire after issue")

    def document(self) -> dict[str, object]:
        return {
            "schema": CAMPAIGN_AUTHORIZATION_SCHEMA,
            "stage_identity": self.stage_identity.document(),
            "campaign_id": self.campaign_id,
            "campaign_epoch": self.campaign_epoch,
            "campaign_fingerprint": self.campaign_fingerprint,
            "release_fingerprint": self.release_fingerprint,
            "deployment_fingerprint": self.deployment_fingerprint,
            "plant_epoch": self.plant_epoch,
            "deployment_readback_sha256": self.deployment_readback_sha256,
            "bounded_baseline_and_loop": True,
            "contact_campaign_allowed": True,
            "optimizer_allowed": True,
            "authorization_source": self.authorization_source,
            "authorized_at": self.authorized_at,
            "expires_at": self.expires_at,
        }

    @property
    def authorization_ref_sha256(self) -> str:
        return canonical_sha256(self.document())

    def require_current(self, *, now: datetime | None = None) -> None:
        observed = now if now is not None else datetime.now(timezone.utc)
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise AuthorizationError("authorization comparison time must be zoned")
        if not (
            _timestamp("authorized_at", self.authorized_at)
            <= observed
            < _timestamp("expires_at", self.expires_at)
        ):
            raise AuthorizationError("campaign authorization is not currently valid")

    @classmethod
    def from_document(cls, payload: object) -> "CampaignAuthorization":
        required = {
            "schema",
            "stage_identity",
            "campaign_id",
            "campaign_epoch",
            "campaign_fingerprint",
            "release_fingerprint",
            "deployment_fingerprint",
            "plant_epoch",
            "deployment_readback_sha256",
            "bounded_baseline_and_loop",
            "contact_campaign_allowed",
            "optimizer_allowed",
            "authorization_source",
            "authorized_at",
            "expires_at",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise AuthorizationError("campaign authorization fields differ")
        if payload["schema"] != CAMPAIGN_AUTHORIZATION_SCHEMA:
            raise AuthorizationError("campaign authorization schema differs")
        if (
            payload["bounded_baseline_and_loop"] is not True
            or payload["contact_campaign_allowed"] is not True
            or payload["optimizer_allowed"] is not True
        ):
            raise AuthorizationError("campaign authorization escaped its contract")
        return cls(
            stage_identity=Step5dV3StageIdentity.from_document(
                payload["stage_identity"]
            ),
            campaign_id=payload["campaign_id"],
            campaign_epoch=payload["campaign_epoch"],
            campaign_fingerprint=payload["campaign_fingerprint"],
            release_fingerprint=payload["release_fingerprint"],
            deployment_fingerprint=payload["deployment_fingerprint"],
            plant_epoch=payload["plant_epoch"],
            deployment_readback_sha256=payload["deployment_readback_sha256"],
            authorization_source=payload["authorization_source"],
            authorized_at=payload["authorized_at"],
            expires_at=payload["expires_at"],
        )


def load_campaign_authorization(
    path: str | Path,
    *,
    expected_campaign_id: str,
    expected_campaign_epoch: int,
    expected_campaign_fingerprint: str,
    expected_release_fingerprint: str,
    expected_deployment_fingerprint: str,
    expected_plant_epoch: int,
    expected_deployment_readback_sha256: str,
    now: datetime | None = None,
) -> CampaignAuthorization:
    source = Path(path)
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise AuthorizationError(
            "campaign authorization must be an absolute regular file"
        )
    authorization = CampaignAuthorization.from_document(load_strict_json(source))
    expected = (
        expected_campaign_id,
        expected_campaign_epoch,
        expected_campaign_fingerprint,
        expected_release_fingerprint,
        expected_deployment_fingerprint,
        expected_plant_epoch,
        expected_deployment_readback_sha256,
    )
    observed = (
        authorization.campaign_id,
        authorization.campaign_epoch,
        authorization.campaign_fingerprint,
        authorization.release_fingerprint,
        authorization.deployment_fingerprint,
        authorization.plant_epoch,
        authorization.deployment_readback_sha256,
    )
    if observed != expected:
        raise AuthorizationError(
            "campaign authorization is not bound to the exact campaign epoch"
        )
    authorization.require_current(now=now)
    return authorization
