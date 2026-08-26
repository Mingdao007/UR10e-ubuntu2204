"""Typed identity contracts for fresh R013 campaign state."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Mapping


CAMPAIGN_FINGERPRINT_SCHEMA = "step5d.autotune-v4/r013-campaign-fingerprint-v1"
CAMPAIGN_FINGERPRINT_VERSION = 1
LEGACY_PROFILE_IDENTITY = "legacy-v4-default"
R013_PROFILE_IDENTITY_FIELDS = frozenset(
    {
        "feedforward_profile_identity",
        "motion_admission_profile_identity",
        "baseline_transition_profile_identity",
        "baseline_residual_policy_identity",
    }
)


class CampaignIdentityError(ValueError):
    """A campaign identity is incomplete or differs from its bound function."""


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise CampaignIdentityError(f"R013 {name} identity is incomplete")
    return value


@dataclass(frozen=True)
class CampaignFingerprint:
    """The complete function identity used by a fresh R013 campaign."""

    path_id: str
    metric_fingerprint: str
    handoff_policy: str
    correction_runtime_strategy_identity: str
    source_identity: str
    eoat_identity: str
    home_tare_identity: str
    controller_lineage: str
    feedforward_profile_identity: str = LEGACY_PROFILE_IDENTITY
    motion_admission_profile_identity: str = LEGACY_PROFILE_IDENTITY
    baseline_transition_profile_identity: str = LEGACY_PROFILE_IDENTITY
    baseline_residual_policy_identity: str = LEGACY_PROFILE_IDENTITY
    schema: str = CAMPAIGN_FINGERPRINT_SCHEMA
    version: int = CAMPAIGN_FINGERPRINT_VERSION

    def __post_init__(self) -> None:
        if (
            self.schema != CAMPAIGN_FINGERPRINT_SCHEMA
            or type(self.version) is not int
            or self.version != CAMPAIGN_FINGERPRINT_VERSION
        ):
            raise CampaignIdentityError("R013 campaign fingerprint schema/version differs")
        for name in (
            "path_id", "metric_fingerprint", "handoff_policy",
            "correction_runtime_strategy_identity", "source_identity",
            "eoat_identity", "home_tare_identity", "controller_lineage",
            "feedforward_profile_identity", "motion_admission_profile_identity",
            "baseline_transition_profile_identity", "baseline_residual_policy_identity",
        ):
            _text(getattr(self, name), name)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CampaignFingerprint":
        if not isinstance(value, Mapping):
            raise CampaignIdentityError("R013 campaign fingerprint must be an object")
        base_required = {
            "schema", "version", "path_id", "metric_fingerprint", "handoff_policy",
            "correction_runtime_strategy_identity", "source_identity", "eoat_identity",
            "home_tare_identity", "controller_lineage",
        }
        if set(value) not in (base_required, base_required | R013_PROFILE_IDENTITY_FIELDS):
            raise CampaignIdentityError("R013 campaign fingerprint fields differ")
        return cls(
            path_id=value["path_id"],
            metric_fingerprint=value["metric_fingerprint"],
            handoff_policy=value["handoff_policy"],
            correction_runtime_strategy_identity=value["correction_runtime_strategy_identity"],
            source_identity=value["source_identity"],
            eoat_identity=value["eoat_identity"],
            home_tare_identity=value["home_tare_identity"],
            controller_lineage=value["controller_lineage"],
            feedforward_profile_identity=value.get(
                "feedforward_profile_identity", LEGACY_PROFILE_IDENTITY
            ),
            motion_admission_profile_identity=value.get(
                "motion_admission_profile_identity", LEGACY_PROFILE_IDENTITY
            ),
            baseline_transition_profile_identity=value.get(
                "baseline_transition_profile_identity", LEGACY_PROFILE_IDENTITY
            ),
            baseline_residual_policy_identity=value.get(
                "baseline_residual_policy_identity", LEGACY_PROFILE_IDENTITY
            ),
            schema=value["schema"],
            version=value["version"],
        )

    @classmethod
    def legacy_default(cls, *, handoff_policy: str, runtime_strategy_identity: str) -> "CampaignFingerprint":
        """Stable identity for retained pre-budgeted R013 artifacts."""

        return cls(
            path_id="r013_legacy_path",
            metric_fingerprint="force-mae-v2-sealed|legacy-r013",
            handoff_policy=handoff_policy,
            correction_runtime_strategy_identity=runtime_strategy_identity,
            source_identity="legacy_r013_seed_coordinates_only",
            eoat_identity="legacy_r013_eoat",
            home_tare_identity="legacy_r013_home_tare",
            controller_lineage="legacy_r013_controller",
        )

    def as_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "version": self.version,
            "path_id": self.path_id,
            "metric_fingerprint": self.metric_fingerprint,
            "handoff_policy": self.handoff_policy,
            "correction_runtime_strategy_identity": self.correction_runtime_strategy_identity,
            "source_identity": self.source_identity,
            "eoat_identity": self.eoat_identity,
            "home_tare_identity": self.home_tare_identity,
            "controller_lineage": self.controller_lineage,
        }
        profile_values = {
            "feedforward_profile_identity": self.feedforward_profile_identity,
            "motion_admission_profile_identity": self.motion_admission_profile_identity,
            "baseline_transition_profile_identity": self.baseline_transition_profile_identity,
            "baseline_residual_policy_identity": self.baseline_residual_policy_identity,
        }
        if any(identity != LEGACY_PROFILE_IDENTITY for identity in profile_values.values()):
            value.update(profile_values)
        return value

    def with_r013_profiles(
        self,
        *,
        feedforward_profile_identity: str,
        motion_admission_profile_identity: str,
        baseline_transition_profile_identity: str,
        baseline_residual_policy_identity: str,
    ) -> "CampaignFingerprint":
        return CampaignFingerprint(
            path_id=self.path_id,
            metric_fingerprint=self.metric_fingerprint,
            handoff_policy=self.handoff_policy,
            correction_runtime_strategy_identity=self.correction_runtime_strategy_identity,
            source_identity=self.source_identity,
            eoat_identity=self.eoat_identity,
            home_tare_identity=self.home_tare_identity,
            controller_lineage=self.controller_lineage,
            feedforward_profile_identity=feedforward_profile_identity,
            motion_admission_profile_identity=motion_admission_profile_identity,
            baseline_transition_profile_identity=baseline_transition_profile_identity,
            baseline_residual_policy_identity=baseline_residual_policy_identity,
        )

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            self.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def runtime_strategy_identity(self) -> str:
        return self.correction_runtime_strategy_identity


def validate_campaign_fingerprint(value: CampaignFingerprint | Mapping[str, Any]) -> CampaignFingerprint:
    if isinstance(value, CampaignFingerprint):
        return value
    return CampaignFingerprint.from_mapping(value)


def bind_r013_profile_identities(
    fingerprint: CampaignFingerprint | Mapping[str, Any],
    *,
    feedforward_profile: Any,
    motion_admission_profile: Any,
    baseline_transition_profile: Any,
    baseline_residual_policy: Any,
) -> CampaignFingerprint:
    """Bind one explicit R013 profile tuple into a fresh campaign identity."""

    parsed = validate_campaign_fingerprint(fingerprint)
    values = {
        "feedforward_profile_identity": getattr(feedforward_profile, "profile_id", ""),
        "motion_admission_profile_identity": getattr(
            motion_admission_profile, "profile_id", ""
        ),
        "baseline_transition_profile_identity": getattr(
            baseline_transition_profile, "profile_id", ""
        ),
        "baseline_residual_policy_identity": getattr(
            baseline_residual_policy, "policy_id", ""
        ),
    }
    if any(type(value) is not str or not value for value in values.values()):
        raise CampaignIdentityError("R013 profile identity tuple is incomplete")
    return parsed.with_r013_profiles(**values)


def require_r013_profile_binding(
    fingerprint: CampaignFingerprint | Mapping[str, Any],
    *,
    feedforward_profile: Any,
    motion_admission_profile: Any,
    baseline_transition_profile: Any,
    baseline_residual_policy: Any,
) -> CampaignFingerprint:
    expected = bind_r013_profile_identities(
        fingerprint,
        feedforward_profile=feedforward_profile,
        motion_admission_profile=motion_admission_profile,
        baseline_transition_profile=baseline_transition_profile,
        baseline_residual_policy=baseline_residual_policy,
    )
    parsed = validate_campaign_fingerprint(fingerprint)
    if parsed != expected:
        raise CampaignIdentityError("R013 campaign profile identities differ")
    return parsed


__all__ = [
    "CAMPAIGN_FINGERPRINT_SCHEMA",
    "CAMPAIGN_FINGERPRINT_VERSION",
    "CampaignFingerprint",
    "CampaignIdentityError",
    "bind_r013_profile_identities",
    "require_r013_profile_binding",
    "LEGACY_PROFILE_IDENTITY",
    "R013_PROFILE_IDENTITY_FIELDS",
    "validate_campaign_fingerprint",
]
