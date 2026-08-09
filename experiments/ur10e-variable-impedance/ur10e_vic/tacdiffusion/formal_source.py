"""Loader for the canonical, repository-local TacDiffusion V4 source contract."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import (
    CONTACT_GUARD_PROFILE_SCHEMA_V1,
    FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX,
    FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N,
    FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1,
    FORMAL_EXPERT_ACTION_SLEW_PER_S,
    FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM,
    FORMAL_MODEL_RATE_CANDIDATES_HZ,
    FORMAL_OBSERVATION_DIMENSION,
    FORMAL_REVIEW_GOVERNANCE_SCHEMA_V1,
    FORMAL_SAMPLER_STEPS,
    FORCE_AUTHORITY_SCHEMA_V1,
    ContactGuardProfileV1,
    KunweiOnlyForceAuthorityV1,
    validate_formal_force_source_payload,
    validate_formal_rtde_recipe,
)
from .governance import (
    FORMAL_V4_LINEAGE,
    ReviewGovernanceSourceContractV1,
    load_review_governance_source,
)
from .expert import FormalHostPoseTrackingFeedforwardV1, FormalMotionFeedforwardV1
from .formal_tracking_quality import FormalTrackingQualityContractV1


FORMAL_V4_SOURCE_SCHEMA_V1 = "ur10e_tacdiffusion_formal_v4_source_contract/v1"


@dataclass(frozen=True)
class FormalV4SourceContractV1:
    """Validated source payload; no generated report or hash is rewritten."""

    payload: Mapping[str, Any]
    force_authority: KunweiOnlyForceAuthorityV1
    no_contact_guard: ContactGuardProfileV1
    expert_contact_guard: ContactGuardProfileV1
    rtde_output_allowlist: tuple[str, ...]
    review_governance: ReviewGovernanceSourceContractV1 | None = None

    @property
    def schema(self) -> str:
        return str(self.payload["schema"])

    @property
    def lineage(self) -> str:
        return str(self.payload["lineage"])

    @property
    def model_active(self) -> bool:
        return bool(self.payload["model_active"])

    @property
    def shadow_only(self) -> bool:
        return bool(self.payload["shadow_only"])

    def as_json(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.payload, sort_keys=True, allow_nan=False))

    @property
    def fingerprint_sha256(self) -> str:
        encoded = json.dumps(
            self.as_json(), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _profile_from_source(
    payload: Mapping[str, Any],
    *,
    authority: KunweiOnlyForceAuthorityV1,
    expected_id: str,
) -> ContactGuardProfileV1:
    if not isinstance(payload, Mapping):
        raise ValueError(f"formal source guard profile {expected_id} is not an object")
    profile = ContactGuardProfileV1(
        profile_id=str(payload.get("profile_id", "")),
        force_limit_n=float(payload.get("force_limit_n", float("nan"))),
        torque_limit_nm=float(payload.get("torque_limit_nm", float("nan"))),
        authority=authority,
        semantics=str(payload.get("semantics", "")),
        schema_version=str(payload.get("schema_version", "")),
    )
    if profile.profile_id != expected_id:
        raise ValueError(f"formal source guard profile id mismatch: {expected_id}")
    expected = {
        "schema_version": profile.schema_version,
        "profile_id": profile.profile_id,
        "force_limit_n": profile.force_limit_n,
        "torque_limit_nm": profile.torque_limit_nm,
        "semantics": profile.semantics,
    }
    if dict(payload) != expected:
        raise ValueError(f"formal source guard profile {expected_id} is non-canonical")
    return profile


def load_formal_v4_source_contract(path: str | Path) -> FormalV4SourceContractV1:
    """Load and validate the V4 source contract without writing any view."""

    source_path = Path(path)
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("formal V4 source contract must be a JSON object")
    if raw.get("schema") != FORMAL_V4_SOURCE_SCHEMA_V1:
        raise ValueError("unsupported formal V4 source contract schema")
    if raw.get("lineage") != "tacdiffusion_formal_v4":
        raise ValueError("formal V4 source contract lineage is invalid")
    validate_formal_force_source_payload(raw, path="formal_v4_source")
    raw_authority = raw.get("force_authority")
    if not isinstance(raw_authority, Mapping):
        raise ValueError("formal V4 source force authority is missing")
    authority = KunweiOnlyForceAuthorityV1(**dict(raw_authority))
    if authority.as_json() != dict(raw_authority):
        raise ValueError("formal V4 source force authority is non-canonical")
    profiles = raw.get("contact_guard_profiles")
    if not isinstance(profiles, Mapping):
        raise ValueError("formal V4 source guard profiles are missing")
    if set(profiles) != {"no_contact", "expert_contact"}:
        raise ValueError("formal V4 source must declare exactly two guard profiles")
    no_contact = _profile_from_source(
        profiles["no_contact"], authority=authority, expected_id="no_contact"
    )
    expert_contact = _profile_from_source(
        profiles["expert_contact"], authority=authority, expected_id="expert_contact"
    )
    expected_action_limits = {
        "schema_version": FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1,
        "frame_id": "tool0_tcp",
        "component_abs_max": list(FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX),
        "force_norm_max_n": FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N,
        "torque_norm_max_nm": FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM,
        "slew_per_s": list(FORMAL_EXPERT_ACTION_SLEW_PER_S),
    }
    if raw.get("expert_action_limits") != expected_action_limits:
        raise ValueError("formal V4 source expert action limits are invalid")
    raw_fields = raw.get("rtde_output_allowlist")
    if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
        raise ValueError("formal V4 source RTDE allowlist is missing")
    fields = tuple(str(value) for value in raw_fields)
    validate_formal_rtde_recipe(
        {
            "output_fields": list(fields),
            "force_authority": authority.as_json(),
            "model_rate_candidates_hz": raw.get("formal_model_rate_candidates_hz"),
            "observation_dimension": raw.get("formal_observation_dimension"),
        },
        authority=authority,
    )
    if raw.get("formal_observation_dimension") != FORMAL_OBSERVATION_DIMENSION:
        raise ValueError("formal V4 source observation dimension is invalid")
    if raw.get("control_rate_hz") != 500:
        raise ValueError("formal V4 source control rate is invalid")
    if raw.get("torque_application_rate_hz") != 500:
        raise ValueError("formal V4 source torque application rate is invalid")
    if raw.get("control_law_update_rate_range_hz") != [150.0, 200.0]:
        raise ValueError("formal V4 source control-law update rate range is invalid")
    expected_motion = FormalMotionFeedforwardV1().as_json()
    expected_motion.update(
        {
            "friction_profile": "ur_full_v3_formal_motion",
            "viscous_scale": [1.0] * 6,
            "coulomb_scale": [1.0] * 6,
            "host_pose_tracking": FormalHostPoseTrackingFeedforwardV1().as_json(),
        }
    )
    expected_motion.pop("frame_id")
    if raw.get("formal_motion_authority") != expected_motion:
        raise ValueError("formal V4 source motion authority is invalid")
    if raw.get("formal_tracking_quality") != FormalTrackingQualityContractV1().as_json():
        raise ValueError("formal V4 source tracking quality contract is invalid")
    if raw.get("formal_model_rate_candidates_hz") != list(FORMAL_MODEL_RATE_CANDIDATES_HZ):
        raise ValueError("formal V4 source model-rate candidates are invalid")
    if raw.get("sampler_steps") != FORMAL_SAMPLER_STEPS:
        raise ValueError("formal V4 source sampler steps are invalid")
    if raw.get("model_active") is not False or raw.get("shadow_only") is not True:
        raise ValueError("formal V4 source must remain inactive and shadow-only")
    if raw.get("production_dynamics_required") is not True:
        raise ValueError("formal V4 source must require production dynamics")
    if raw.get("legacy_v2_v3_readers") is not True:
        raise ValueError("formal V4 source must preserve legacy readers")
    if raw.get("legacy_v2_v3_formal_eligible") is not False or raw.get("diagnostic_shadow_formal_eligible") is not False:
        raise ValueError("formal V4 source must reject legacy and diagnostic formal eligibility")
    if raw.get("review_governance_schema") != FORMAL_REVIEW_GOVERNANCE_SCHEMA_V1:
        raise ValueError("formal V4 source review-governance schema is invalid")
    governance_reference = raw.get("review_governance_source")
    if not isinstance(governance_reference, str) or not governance_reference.strip():
        raise ValueError("formal V4 source review-governance source is missing")
    governance_relative = Path(governance_reference)
    if governance_relative.is_absolute() or ".." in governance_relative.parts:
        raise ValueError("formal V4 source review-governance path must be repository-relative")
    governance_path = source_path.parent.parent / governance_relative
    if not governance_path.is_file():
        raise ValueError("formal V4 source review-governance source is missing on disk")
    governance = load_review_governance_source(governance_path)
    if governance.lineage != FORMAL_V4_LINEAGE:
        raise ValueError("formal V4 source review-governance lineage is invalid")
    if raw.get("force_authority", {}).get("schema_version") != FORCE_AUTHORITY_SCHEMA_V1:
        raise ValueError("formal V4 source authority schema is invalid")
    if any(
        profile.schema_version != CONTACT_GUARD_PROFILE_SCHEMA_V1
        for profile in (no_contact, expert_contact)
    ):
        raise ValueError("formal V4 source guard profile schema is invalid")
    canonical = json.loads(json.dumps(raw, sort_keys=True, allow_nan=False))
    return FormalV4SourceContractV1(
        payload=canonical,
        force_authority=authority,
        no_contact_guard=no_contact,
        expert_contact_guard=expert_contact,
        rtde_output_allowlist=fields,
        review_governance=governance,
    )


__all__ = [
    "FORMAL_V4_SOURCE_SCHEMA_V1",
    "FormalV4SourceContractV1",
    "load_formal_v4_source_contract",
]
