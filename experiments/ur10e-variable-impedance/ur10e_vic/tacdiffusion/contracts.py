"""Hash-bound, frame-explicit contracts for UR10e TacDiffusion preparation.

These contracts describe data and model output.  They never authorize a live
run or promote legacy velocity traces into force labels.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence


CONDITION_DIMENSION = 36
CONTROL_RATE_HZ = 500
RAW_WRENCH_RATE_HZ = 1000
MODEL_RATE_CANDIDATES_HZ = (500, 200, 100, 50)
# The tuple above is intentionally retained for legacy v2/v3 readers.  V4
# formal selection has its own immutable candidate set; a caller must opt into
# the formal validator instead of silently changing the meaning of old
# benchmark artifacts.
LEGACY_MODEL_RATE_CANDIDATES_HZ = MODEL_RATE_CANDIDATES_HZ
FORMAL_MODEL_RATE_CANDIDATES_HZ = (100, 50)
FORMAL_OBSERVATION_DIMENSION = 84
FORMAL_SAMPLER_STEPS = 50
PERMITTED_PROGRAM_CLAIM = "UR10e 500 Hz force-domain diffusion adaptation"
REQUIRED_EXPERT_CONTROLLER_PROFILE = "polyscope-5.25.2-direct-torque-v2-500hz"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,192}$")

DYNAMICS_SAMPLE_SCHEMA = "ur10e_tacdiffusion_dynamics_sample/v1"
DYNAMICS_RECEIPT_SCHEMA = "ur10e_tacdiffusion_dynamics_receipt/v1"
DYNAMICS_CONFORMANCE_SCHEMA = "ur10e_tacdiffusion_dynamics_conformance/v1"
DYNAMICS_SAMPLE_SCHEMA_VERSION = DYNAMICS_SAMPLE_SCHEMA
DYNAMICS_RECEIPT_SCHEMA_VERSION = DYNAMICS_RECEIPT_SCHEMA
DYNAMICS_CONFORMANCE_CONTRACT = "ur10e_tacdiffusion_controller_conformance/v1"
OFFLINE_FAKE_RTDE_FIXTURE_ID = "offline_fake_rtde_synthetic_fixture_v1"
DYNAMICS_MAX_SEQUENCE = (1 << 63) - 1
DYNAMICS_MAX_TIME_S = 1.0e12
DYNAMICS_MAX_INTERVAL_S = 0.100
DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE = "previous_commanded_no_gravity_torque"

FORCE_AUTHORITY_SCHEMA_V1 = "ur10e_tacdiffusion_force_authority/v1"
CONTACT_GUARD_PROFILE_SCHEMA_V1 = "ur10e_tacdiffusion_contact_guard_profile/v1"
FORMAL_EPISODE_MANIFEST_SCHEMA_V1 = "ur10e_tacdiffusion_formal_episode_manifest/v1"
FORMAL_EPISODE_MANIFEST_SCHEMA_V2 = "ur10e_tacdiffusion_formal_episode_manifest/v2"
FORMAL_MANIFEST_TARGET_LOADS_N = (3.0, 5.0, 8.0)
FORMAL_MANIFEST_CAMPAIGN_KINDS = ("fixed_k", "variable_k")
FORMAL_MANIFEST_FIXED_CAMPAIGN_ID = "fixed_k_formal_v4"
FORMAL_MANIFEST_VARIABLE_CAMPAIGN_ID = "variable_k_formal_v4"
FORMAL_REVIEW_GOVERNANCE_SCHEMA_V1 = "ur10e_tacdiffusion_review_governance/v1"
KUNWEI_ONLY_FORCE_SOURCE_ID = "kunwei_kwr75_tcp_raw_stream_v1"
KUNWEI_ONLY_SENSOR_MODEL = "KWR75"
KUNWEI_ONLY_TRANSPORT = "tcp_raw"
KUNWEI_SOFTWARE_BASELINE_SEMANTICS = "software_baseline_only"
FORMAL_NO_CONTACT_FORCE_LIMIT_N = 6.0
FORMAL_NO_CONTACT_TORQUE_LIMIT_NM = 0.5
FORMAL_EXPERT_CONTACT_FORCE_LIMIT_N = 50.0
FORMAL_EXPERT_CONTACT_TORQUE_LIMIT_NM = 4.0
FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1 = "ur10e_tacdiffusion_expert_action_limits/v1"
FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX = (50.0, 50.0, 50.0, 4.0, 4.0, 4.0)
FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N = 50.0
FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM = 4.0
FORMAL_EXPERT_ACTION_SLEW_PER_S = (100.0, 100.0, 100.0, 10.0, 10.0, 10.0)

# These values are rejected only by formal payload/recipe validators.  Legacy
# reports remain readable and are never rewritten into the V4 lineage.
FORMAL_FORBIDDEN_FORCE_TOKENS = frozenset(
    {
        "actual_tcp_force",
        "get_tcp_force",
        "onrobot",
        "ur_internal_ft",
        "ur_ft",
        "ur_force_torque",
        "alternate_force_source",
        "simulated_ft",
        "gazebo_contact",
        "actual_force",
    }
)
FORMAL_FORCE_SOURCE_KEYS = frozenset(
    {
        "force_source",
        "external_force_source",
        "contact_source",
        "guard_source",
        "training_force_source",
        "force_authority",
        "force_authority_receipt",
        "force_authority_source",
        "authority",
    }
)


def _vector(values: Iterable[float], length: int, name: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if len(result) != length:
        raise ValueError(f"{name} must contain {length} values")
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must be finite")
    return result


def _sha256(value: str, name: str) -> str:
    text = str(value)
    if not _SHA256_RE.fullmatch(text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


@dataclass(frozen=True)
class FrameCalibrationLineage:
    """Provenance for converting the Kunwei signal into the TCP frame."""

    external_sensor_frame_id: str
    canonical_tcp_frame_id: str
    twist_frame_id: str
    sensor_to_tcp_transform_sha256: str
    calibration_sha256: str
    zero_bias_sha256: str
    wrench_units: str = "N,Nm"
    twist_units: str = "m/s,rad/s"

    def __post_init__(self) -> None:
        for name in (
            "external_sensor_frame_id",
            "canonical_tcp_frame_id",
            "twist_frame_id",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.canonical_tcp_frame_id != self.twist_frame_id:
            raise ValueError("wrench and twist must use the same canonical TCP frame")
        for name in (
            "sensor_to_tcp_transform_sha256",
            "calibration_sha256",
            "zero_bias_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if self.wrench_units != "N,Nm":
            raise ValueError("wrench_units must be SI units N,Nm")
        if self.twist_units != "m/s,rad/s":
            raise ValueError("twist_units must be SI units m/s,rad/s")


@dataclass(frozen=True)
class ForceDiffusionObservation:
    """Current and previous 18D force observations in a common TCP frame.

    ``condition_36d`` is ordered as current external wrench, current internal
    wrench, current EE twist, then the same three previous-tick vectors.  No
    vision features are admitted by this schema.
    """

    sequence: int
    previous_timestamp_s: float
    current_timestamp_s: float
    previous_external_sample_timestamp_s: float
    current_external_sample_timestamp_s: float
    previous_external_wrench: tuple[float, ...] | Sequence[float]
    current_external_wrench: tuple[float, ...] | Sequence[float]
    previous_internal_wrench: tuple[float, ...] | Sequence[float]
    current_internal_wrench: tuple[float, ...] | Sequence[float]
    previous_ee_twist: tuple[float, ...] | Sequence[float]
    current_ee_twist: tuple[float, ...] | Sequence[float]
    lineage: FrameCalibrationLineage
    previous_external_source_sequence: int
    current_external_source_sequence: int
    raw_wrench_rate_hz: int = RAW_WRENCH_RATE_HZ
    control_rate_hz: int = CONTROL_RATE_HZ
    vision_included: bool = False

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence must be at least one for a two-tick observation")
        timestamps = (
            self.previous_timestamp_s,
            self.current_timestamp_s,
            self.previous_external_sample_timestamp_s,
            self.current_external_sample_timestamp_s,
        )
        if not all(math.isfinite(value) and value >= 0.0 for value in timestamps):
            raise ValueError("observation timestamps must be finite and non-negative")
        if self.current_timestamp_s <= self.previous_timestamp_s:
            raise ValueError("current_timestamp_s must follow previous_timestamp_s")
        if self.previous_external_sample_timestamp_s > self.previous_timestamp_s:
            raise ValueError("previous external wrench must be causally available")
        if self.current_external_sample_timestamp_s > self.current_timestamp_s:
            raise ValueError("current external wrench must be causally available")
        if (
            self.previous_external_source_sequence < 0
            or self.current_external_source_sequence
            < self.previous_external_source_sequence
        ):
            raise ValueError("external source sequences must be monotonic")
        if self.raw_wrench_rate_hz != RAW_WRENCH_RATE_HZ:
            raise ValueError("raw Kunwei wrench rate must remain 1000 Hz")
        if self.control_rate_hz != CONTROL_RATE_HZ:
            raise ValueError("UR10e force-domain control rate must remain 500 Hz")
        if self.vision_included:
            raise ValueError("TacDiffusion adaptation is force-only; vision is not allowed")
        if not isinstance(self.lineage, FrameCalibrationLineage):
            raise ValueError("lineage must be a FrameCalibrationLineage")
        for name in (
            "previous_external_wrench",
            "current_external_wrench",
            "previous_internal_wrench",
            "current_internal_wrench",
            "previous_ee_twist",
            "current_ee_twist",
        ):
            object.__setattr__(self, name, _vector(getattr(self, name), 6, name))
        max_external_age_s = 2.0 / float(self.raw_wrench_rate_hz)
        if (
            self.previous_timestamp_s - self.previous_external_sample_timestamp_s
            > max_external_age_s + 1e-12
            or self.current_timestamp_s - self.current_external_sample_timestamp_s
            > max_external_age_s + 1e-12
        ):
            raise ValueError("causally synchronized external wrench is stale")
        if len(self.condition_36d) != CONDITION_DIMENSION:
            raise AssertionError("force diffusion condition contract is not 36D")

    @property
    def condition_36d(self) -> tuple[float, ...]:
        return (
            self.current_external_wrench
            + self.current_internal_wrench
            + self.current_ee_twist
            + self.previous_external_wrench
            + self.previous_internal_wrench
            + self.previous_ee_twist
        )


@dataclass(frozen=True)
class ForceDiffusionProposal:
    """A raw six-axis diffusion proposal; never a live authorization."""

    generated_at_s: float
    sequence: int
    raw_f_df: tuple[float, ...] | Sequence[float]
    model_sha256: str
    model_rate_hz: int
    age_s: float
    confidence: float
    valid: bool = True
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.generated_at_s) or self.generated_at_s < 0.0:
            raise ValueError("generated_at_s must be finite and non-negative")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        object.__setattr__(self, "raw_f_df", _vector(self.raw_f_df, 6, "raw_f_df"))
        object.__setattr__(self, "model_sha256", _sha256(self.model_sha256, "model_sha256"))
        if self.model_rate_hz not in MODEL_RATE_CANDIDATES_HZ:
            raise ValueError("model_rate_hz must be 50, 100, 200, or 500")
        if not math.isfinite(self.age_s) or self.age_s < 0.0:
            raise ValueError("age_s must be finite and non-negative")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be in [0, 1]")
        if self.valid and self.confidence <= 0.0:
            raise ValueError("valid proposals require confidence > 0")


@dataclass(frozen=True)
class ExpertTraceManifest:
    """A hash-bound trace identity with an explicit force-label boundary."""

    trace_id: str
    source_kind: str
    dataset_split: str
    controller_profile: str
    canonical_frame_id: str
    controller_verified: bool
    controller_readback_sha256: str
    sample_count: int
    has_expert_ff_labels: bool
    frame_calibration_sha256: str
    expert_policy_sha256: str
    software_sha256: str
    package_sha256: str
    trace_sha256: str
    claim_boundary: str
    control_rate_hz: int = CONTROL_RATE_HZ
    permitted_program_claim: str = PERMITTED_PROGRAM_CLAIM
    vision_included: bool = False

    def __post_init__(self) -> None:
        for name in ("trace_id", "controller_profile", "canonical_frame_id"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.source_kind not in {
            "ur10e_expert_demonstration",
            "legacy_v27_replay",
            "legacy_v29_replay",
            "synthetic_pipeline",
        }:
            raise ValueError("unsupported trace source_kind")
        if self.dataset_split not in {"train", "validation", "test", "pipeline_only"}:
            raise ValueError("unsupported dataset_split")
        if self.sample_count <= 0:
            raise ValueError("sample_count must be positive")
        if self.control_rate_hz != CONTROL_RATE_HZ:
            raise ValueError("expert trace control_rate_hz must be 500")
        if self.permitted_program_claim != PERMITTED_PROGRAM_CLAIM:
            raise ValueError("manifest must preserve the UR10e 500 Hz claim boundary")
        if self.vision_included:
            raise ValueError("the UR10e TacDiffusion mainline does not use vision")
        for name in (
            "controller_readback_sha256",
            "frame_calibration_sha256",
            "expert_policy_sha256",
            "software_sha256",
            "package_sha256",
            "trace_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if self.source_kind == "ur10e_expert_demonstration":
            if self.controller_profile != REQUIRED_EXPERT_CONTROLLER_PROFILE:
                raise ValueError(
                    "UR10e expert demonstrations require the verified 5.25.2 "
                    "Direct Torque V2 500 Hz controller profile"
                )
            if self.dataset_split == "pipeline_only" or not self.has_expert_ff_labels:
                raise ValueError("UR10e expert demonstrations require a formal split and F_ff labels")
            if self.claim_boundary != "ur10e_expert_force_labels":
                raise ValueError("expert demonstrations require the expert force-label boundary")
        else:
            if self.dataset_split != "pipeline_only" or self.has_expert_ff_labels:
                raise ValueError("legacy/synthetic traces are pipeline-only and cannot contain F_ff labels")
            if self.claim_boundary != "observation_pipeline_only":
                raise ValueError("legacy/synthetic traces require observation_pipeline_only")

    @property
    def training_eligible(self) -> bool:
        return (
            self.source_kind == "ur10e_expert_demonstration"
            and self.has_expert_ff_labels
            and self.controller_verified
            and self.dataset_split in {"train", "validation", "test"}
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "claim_boundary": self.claim_boundary,
            "control_rate_hz": self.control_rate_hz,
            "controller_profile": self.controller_profile,
            "canonical_frame_id": self.canonical_frame_id,
            "controller_verified": self.controller_verified,
            "controller_readback_sha256": self.controller_readback_sha256,
            "dataset_split": self.dataset_split,
            "expert_policy_sha256": self.expert_policy_sha256,
            "frame_calibration_sha256": self.frame_calibration_sha256,
            "has_expert_ff_labels": self.has_expert_ff_labels,
            "package_sha256": self.package_sha256,
            "permitted_program_claim": self.permitted_program_claim,
            "sample_count": self.sample_count,
            "software_sha256": self.software_sha256,
            "source_kind": self.source_kind,
            "trace_id": self.trace_id,
            "trace_sha256": self.trace_sha256,
            "vision_included": self.vision_included,
        }

    @property
    def fingerprint_sha256(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _identity(value: object, name: str) -> str:
    text = str(value)
    if not _IDENTITY_RE.fullmatch(text):
        raise ValueError(f"{name} must be a bounded non-empty identity")
    return text


def _source_hashes(values: Mapping[str, str], name: str = "source_hashes") -> Mapping[str, str]:
    if not isinstance(values, Mapping) or not 1 <= len(values) <= 16:
        raise ValueError(f"{name} must contain one to sixteen SHA-256 identities")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized[_identity(key, f"{name} key")] = _sha256(str(value), f"{name}[{key}]")
    return MappingProxyType(dict(sorted(normalized.items())))


def _bounded_vector(values: Iterable[float], length: int, name: str, limit: float) -> tuple[float, ...]:
    result = _vector(values, length, name)
    if any(abs(value) > limit for value in result):
        raise ValueError(f"{name} exceeds the bounded dynamics contract")
    return result


def _bounded_matrix(
    values: Sequence[Sequence[float]],
    rows: int,
    columns: int,
    name: str,
    limit: float,
) -> tuple[tuple[float, ...], ...]:
    try:
        result = tuple(tuple(float(value) for value in row) for row in values)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite {rows}x{columns} matrix") from exc
    if len(result) != rows or any(len(row) != columns for row in result):
        raise ValueError(f"{name} must be a finite {rows}x{columns} matrix")
    if not all(math.isfinite(value) for row in result for value in row):
        raise ValueError(f"{name} must be a finite {rows}x{columns} matrix")
    if any(abs(value) > limit for row in result for value in row):
        raise ValueError(f"{name} exceeds the bounded dynamics contract")
    return result


def _canonical_hash(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _formal_text(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _formal_contains_forbidden_token(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _formal_text(value)
    for token in FORMAL_FORBIDDEN_FORCE_TOKENS:
        if token in normalized:
            return token
    return None


def validate_formal_force_source_payload(
    payload: object,
    *,
    path: str = "formal_payload",
) -> None:
    """Reject alternate force identities in a V4 recipe/receipt/manifest.

    This walk is deliberately structural rather than a blacklist over source
    files: values such as ``actual_current_as_torque`` remain legal shadow
    telemetry, while a field explicitly declaring an external/contact/guard
    source must name the Kunwei raw stream authority.
    """

    def walk(value: object, location: str, force_context: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if not isinstance(key, str) or not key.strip():
                    raise ValueError(f"{location} contains an invalid key")
                key_normalized = _formal_text(key)
                token = _formal_contains_forbidden_token(key_normalized)
                if token is not None:
                    raise ValueError(f"{location}.{key} contains forbidden force token {token}")
                if force_context and key_normalized in {"source_identity", "source_id"}:
                    if nested != KUNWEI_ONLY_FORCE_SOURCE_ID:
                        raise ValueError(
                            f"{location}.{key} must identify {KUNWEI_ONLY_FORCE_SOURCE_ID}"
                        )
                nested_context = force_context or key_normalized in FORMAL_FORCE_SOURCE_KEYS
                if key_normalized in FORMAL_FORCE_SOURCE_KEYS:
                    if not isinstance(nested, str):
                        # A nested typed authority is validated by its own
                        # contract and is allowed to carry a mapping here.
                        nested_context = True
                    elif nested.strip() != KUNWEI_ONLY_FORCE_SOURCE_ID:
                        raise ValueError(
                            f"{location}.{key} must identify {KUNWEI_ONLY_FORCE_SOURCE_ID}"
                        )
                walk(nested, f"{location}.{key}", nested_context)
            return
        if isinstance(value, (list, tuple)):
            for index, nested in enumerate(value):
                walk(nested, f"{location}[{index}]", force_context)
            return
        token = _formal_contains_forbidden_token(value)
        if token is not None:
            raise ValueError(f"{location} contains forbidden force token {token}")

    walk(payload, path)


@dataclass(frozen=True)
class KunweiOnlyForceAuthorityV1:
    """The sole external force/contact/guard/training authority for V4."""

    source_identity: str = KUNWEI_ONLY_FORCE_SOURCE_ID
    sensor_model: str = KUNWEI_ONLY_SENSOR_MODEL
    transport: str = KUNWEI_ONLY_TRANSPORT
    canonical_tcp_frame_id: str = "tool0_tcp"
    raw_wrench_units: str = "N,Nm"
    software_baseline_semantics: str = KUNWEI_SOFTWARE_BASELINE_SEMANTICS
    external_force_authority: bool = True
    contact_authority: bool = True
    guard_authority: bool = True
    training_authority: bool = True
    zero_behavior: str = "forbidden"
    tare_behavior: str = "forbidden"
    filter_behavior: str = "forbidden"
    sensor_config_behavior: str = "forbidden"
    schema_version: str = FORCE_AUTHORITY_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != FORCE_AUTHORITY_SCHEMA_V1:
            raise ValueError("unsupported Kunwei force authority schema")
        if self.source_identity != KUNWEI_ONLY_FORCE_SOURCE_ID:
            raise ValueError("formal force authority must be the Kunwei KWR75 TCP raw stream")
        if self.sensor_model != KUNWEI_ONLY_SENSOR_MODEL or self.transport != KUNWEI_ONLY_TRANSPORT:
            raise ValueError("formal force authority must use the KWR75 TCP raw stream")
        for name in ("canonical_tcp_frame_id", "raw_wrench_units", "software_baseline_semantics"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must be non-empty")
        if self.raw_wrench_units != "N,Nm":
            raise ValueError("formal Kunwei wrench units must be N,Nm")
        if self.software_baseline_semantics != KUNWEI_SOFTWARE_BASELINE_SEMANTICS:
            raise ValueError("formal force authority must be software-baseline-only")
        authority_flags = tuple(
            getattr(self, name)
            for name in (
                "external_force_authority",
                "contact_authority",
                "guard_authority",
                "training_authority",
            )
        )
        if not all(isinstance(value, bool) and value for value in authority_flags):
            raise ValueError("Kunwei must own every formal external force authority")
        if any(
            getattr(self, name) != "forbidden"
            for name in ("zero_behavior", "tare_behavior", "filter_behavior", "sensor_config_behavior")
        ):
            raise ValueError("formal force authority cannot configure zero, tare, filter, or sensor state")

    @property
    def source_id(self) -> str:
        return self.source_identity

    def validate_source_identity(self, value: object) -> None:
        if value != self.source_identity:
            raise ValueError("force source identity is not Kunwei KWR75 TCP raw stream")

    def as_json(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "source_identity": self.source_identity,
            "sensor_model": self.sensor_model,
            "transport": self.transport,
            "canonical_tcp_frame_id": self.canonical_tcp_frame_id,
            "raw_wrench_units": self.raw_wrench_units,
            "software_baseline_semantics": self.software_baseline_semantics,
            "external_force_authority": self.external_force_authority,
            "contact_authority": self.contact_authority,
            "guard_authority": self.guard_authority,
            "training_authority": self.training_authority,
            "zero_behavior": self.zero_behavior,
            "tare_behavior": self.tare_behavior,
            "filter_behavior": self.filter_behavior,
            "sensor_config_behavior": self.sensor_config_behavior,
        }
        validate_formal_force_source_payload(payload)
        return payload

    @property
    def fingerprint_sha256(self) -> str:
        return _canonical_hash(self.as_json())


@dataclass(frozen=True)
class ContactGuardProfileV1:
    """Software-baseline guard thresholds; it never performs sensor config."""

    profile_id: str
    force_limit_n: float
    torque_limit_nm: float
    authority: KunweiOnlyForceAuthorityV1 = field(default_factory=KunweiOnlyForceAuthorityV1)
    semantics: str = KUNWEI_SOFTWARE_BASELINE_SEMANTICS
    schema_version: str = CONTACT_GUARD_PROFILE_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != CONTACT_GUARD_PROFILE_SCHEMA_V1:
            raise ValueError("unsupported contact guard profile schema")
        if self.profile_id not in {"no_contact", "expert_contact"}:
            raise ValueError("unsupported formal contact guard profile")
        expected = {
            "no_contact": (FORMAL_NO_CONTACT_FORCE_LIMIT_N, FORMAL_NO_CONTACT_TORQUE_LIMIT_NM),
            "expert_contact": (FORMAL_EXPERT_CONTACT_FORCE_LIMIT_N, FORMAL_EXPERT_CONTACT_TORQUE_LIMIT_NM),
        }[self.profile_id]
        if not all(math.isfinite(float(value)) and float(value) > 0.0 for value in (self.force_limit_n, self.torque_limit_nm)):
            raise ValueError("contact guard limits must be finite and positive")
        if not math.isclose(self.force_limit_n, expected[0], rel_tol=0.0, abs_tol=1e-12) or not math.isclose(self.torque_limit_nm, expected[1], rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("contact guard profile thresholds do not match the frozen V4 defaults")
        if not isinstance(self.authority, KunweiOnlyForceAuthorityV1):
            raise ValueError("contact guard profile must use KunweiOnlyForceAuthorityV1")
        if self.semantics != KUNWEI_SOFTWARE_BASELINE_SEMANTICS:
            raise ValueError("contact guard profile must be software-baseline-only")

    @classmethod
    def no_contact(cls, *, authority: KunweiOnlyForceAuthorityV1 | None = None) -> "ContactGuardProfileV1":
        return cls("no_contact", FORMAL_NO_CONTACT_FORCE_LIMIT_N, FORMAL_NO_CONTACT_TORQUE_LIMIT_NM, authority or KunweiOnlyForceAuthorityV1())

    @classmethod
    def expert_contact(cls, *, authority: KunweiOnlyForceAuthorityV1 | None = None) -> "ContactGuardProfileV1":
        return cls("expert_contact", FORMAL_EXPERT_CONTACT_FORCE_LIMIT_N, FORMAL_EXPERT_CONTACT_TORQUE_LIMIT_NM, authority or KunweiOnlyForceAuthorityV1())

    @property
    def force_norm_max_n(self) -> float:
        return self.force_limit_n

    @property
    def torque_norm_max_nm(self) -> float:
        return self.torque_limit_nm

    def as_json(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "force_limit_n": self.force_limit_n,
            "torque_limit_nm": self.torque_limit_nm,
            "semantics": self.semantics,
            "authority": self.authority.as_json(),
        }
        validate_formal_force_source_payload(payload)
        return payload

    @property
    def fingerprint_sha256(self) -> str:
        return _canonical_hash(self.as_json())


def validate_formal_rtde_recipe(
    recipe: Mapping[str, Any],
    *,
    authority: KunweiOnlyForceAuthorityV1 | None = None,
) -> dict[str, Any]:
    """Validate a formal RTDE recipe without opening an RTDE connection."""

    if not isinstance(recipe, Mapping):
        raise ValueError("formal RTDE recipe must be a mapping")
    resolved_authority = authority or KunweiOnlyForceAuthorityV1()
    validate_formal_force_source_payload(recipe)
    raw_fields = recipe.get("output_fields", recipe.get("fields"))
    if not isinstance(raw_fields, (list, tuple)) or not raw_fields:
        raise ValueError("formal RTDE recipe output_fields are required")
    if any(not isinstance(value, str) or not value.strip() for value in raw_fields):
        raise ValueError("formal RTDE recipe output_fields must be non-empty strings")
    fields = tuple(raw_fields)
    for field_name in fields:
        if _formal_contains_forbidden_token(field_name) is not None:
            raise ValueError(f"formal RTDE recipe contains forbidden force field: {field_name}")
    supplied_authority = recipe.get("force_authority")
    if supplied_authority is not None:
        if isinstance(supplied_authority, KunweiOnlyForceAuthorityV1):
            supplied_authority.validate_source_identity(resolved_authority.source_identity)
        elif isinstance(supplied_authority, Mapping):
            if supplied_authority.get("source_identity") != resolved_authority.source_identity:
                raise ValueError("formal RTDE recipe force authority is not Kunwei-only")
        else:
            raise ValueError("formal RTDE recipe force authority has the wrong type")
    else:
        raise ValueError("formal RTDE recipe must bind KunweiOnlyForceAuthorityV1")
    for identity_key in ("source_identity", "source_id"):
        if identity_key in recipe and recipe[identity_key] != resolved_authority.source_identity:
            raise ValueError("formal RTDE recipe source identity is not Kunwei-only")
    rates = recipe.get("model_rate_candidates_hz")
    if rates is not None:
        if not isinstance(rates, (list, tuple)) or tuple(rates) != FORMAL_MODEL_RATE_CANDIDATES_HZ:
            raise ValueError("formal RTDE recipe model-rate candidates must be exactly 100 and 50 Hz")
    if recipe.get("observation_dimension", FORMAL_OBSERVATION_DIMENSION) != FORMAL_OBSERVATION_DIMENSION:
        raise ValueError("formal RTDE recipe observation dimension must be 84")
    return {
        "output_fields": list(fields),
        "force_authority": resolved_authority.as_json(),
        "model_rate_candidates_hz": list(FORMAL_MODEL_RATE_CANDIDATES_HZ),
        "observation_dimension": FORMAL_OBSERVATION_DIMENSION,
    }


@dataclass(frozen=True)
class DynamicsSample:
    """One explicitly identified previous-tick dynamics input.

    The sample is deliberately independent of any controller or sensor API.
    ``previous_commanded_no_gravity_torque_nm`` is the only torque authority;
    ``actual_current_torque_nm_shadow`` is retained for cross-checking only.
    """

    sequence: int
    timestamp_s: float
    previous_q: tuple[float, ...] | Sequence[float]
    previous_qd: tuple[float, ...] | Sequence[float]
    previous_commanded_no_gravity_torque_nm: tuple[float, ...] | Sequence[float]
    calibrated_jacobian: tuple[tuple[float, ...], ...] | Sequence[Sequence[float]]
    coriolis_torque_nm: tuple[float, ...] | Sequence[float]
    joint_damping_torque_nm: tuple[float, ...] | Sequence[float]
    source_hashes: Mapping[str, str]
    model_identity: str
    tcp_identity: str
    calibration_identity: str
    frame_id: str = "tool0_tcp"
    canonical_tcp_frame_id: str = "tool0_tcp"
    jacobian_frame_id: str = "tool0_tcp"
    source_identity: str = "offline_source"
    actual_current_torque_nm_shadow: tuple[float, ...] | Sequence[float] | None = None
    schema_version: str = DYNAMICS_SAMPLE_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not 0 <= self.sequence <= DYNAMICS_MAX_SEQUENCE:
            raise ValueError("dynamics sequence is outside the bounded contract")
        if (
            not math.isfinite(self.timestamp_s)
            or self.timestamp_s < 0.0
            or self.timestamp_s > DYNAMICS_MAX_TIME_S
        ):
            raise ValueError("dynamics timestamp is outside the bounded contract")
        if self.schema_version != DYNAMICS_SAMPLE_SCHEMA:
            raise ValueError("unsupported dynamics sample schema")
        for name in (
            "frame_id",
            "canonical_tcp_frame_id",
            "jacobian_frame_id",
            "source_identity",
            "model_identity",
            "tcp_identity",
            "calibration_identity",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if self.frame_id != self.canonical_tcp_frame_id:
            raise ValueError("dynamics frame does not match canonical TCP frame")
        if self.jacobian_frame_id != self.canonical_tcp_frame_id:
            raise ValueError("Jacobian frame does not match canonical TCP frame")
        object.__setattr__(self, "source_hashes", _source_hashes(self.source_hashes))
        for name in (
            "previous_q",
            "previous_qd",
            "previous_commanded_no_gravity_torque_nm",
            "coriolis_torque_nm",
            "joint_damping_torque_nm",
        ):
            object.__setattr__(
                self,
                name,
                _bounded_vector(getattr(self, name), 6, name, 1.0e6),
            )
        object.__setattr__(
            self,
            "calibrated_jacobian",
            _bounded_matrix(self.calibrated_jacobian, 6, 6, "calibrated_jacobian", 1.0e6),
        )
        if self.actual_current_torque_nm_shadow is not None:
            object.__setattr__(
                self,
                "actual_current_torque_nm_shadow",
                _bounded_vector(
                    self.actual_current_torque_nm_shadow,
                    6,
                    "actual_current_torque_nm_shadow",
                    1.0e6,
                ),
            )

    @property
    def time_s(self) -> float:
        return self.timestamp_s

    @property
    def tcp_frame_id(self) -> str:
        return self.canonical_tcp_frame_id

    @property
    def previous_commanded_no_gravity_torque(self) -> tuple[float, ...]:
        return self.previous_commanded_no_gravity_torque_nm

    @property
    def coriolis_torque(self) -> tuple[float, ...]:
        return self.coriolis_torque_nm

    @property
    def joint_damping_torque(self) -> tuple[float, ...]:
        return self.joint_damping_torque_nm

    @property
    def actual_current_shadow(self) -> tuple[float, ...] | None:
        return self.actual_current_torque_nm_shadow

    def validate_temporal_successor(
        self,
        previous: "DynamicsSample | None" = None,
        *,
        expected_previous_sequence: int | None = None,
        expected_previous_timestamp_s: float | None = None,
        max_interval_s: float = DYNAMICS_MAX_INTERVAL_S,
        now_s: float | None = None,
        max_age_s: float = DYNAMICS_MAX_INTERVAL_S,
    ) -> None:
        if previous is not None:
            if not isinstance(previous, DynamicsSample):
                raise ValueError("previous dynamics sample has the wrong type")
            expected_previous_sequence = previous.sequence
            expected_previous_timestamp_s = previous.timestamp_s
        if expected_previous_sequence is not None:
            if isinstance(expected_previous_sequence, bool) or self.sequence != expected_previous_sequence + 1:
                raise ValueError("dynamics sequence is nonconsecutive or stale")
        if expected_previous_timestamp_s is not None:
            if not math.isfinite(expected_previous_timestamp_s) or self.timestamp_s <= expected_previous_timestamp_s:
                raise ValueError("dynamics time is non-monotonic or stale")
            if not math.isfinite(max_interval_s) or max_interval_s <= 0.0:
                raise ValueError("dynamics max interval is invalid")
            if self.timestamp_s - expected_previous_timestamp_s > max_interval_s + 1e-12:
                raise ValueError("dynamics time gap is stale")
        if now_s is not None:
            if not math.isfinite(now_s) or now_s < self.timestamp_s:
                raise ValueError("dynamics current time is invalid")
            if not math.isfinite(max_age_s) or max_age_s <= 0.0:
                raise ValueError("dynamics max age is invalid")
            if now_s - self.timestamp_s > max_age_s + 1e-12:
                raise ValueError("dynamics sample is stale")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "frame_id": self.frame_id,
            "canonical_tcp_frame_id": self.canonical_tcp_frame_id,
            "jacobian_frame_id": self.jacobian_frame_id,
            "source_identity": self.source_identity,
            "source_hashes": dict(self.source_hashes),
            "model_identity": self.model_identity,
            "tcp_identity": self.tcp_identity,
            "calibration_identity": self.calibration_identity,
            "previous_q": list(self.previous_q),
            "previous_qd": list(self.previous_qd),
            "previous_commanded_no_gravity_torque_nm": list(
                self.previous_commanded_no_gravity_torque_nm
            ),
            "calibrated_jacobian": [list(row) for row in self.calibrated_jacobian],
            "coriolis_torque_nm": list(self.coriolis_torque_nm),
            "joint_damping_torque_nm": list(self.joint_damping_torque_nm),
            "actual_current_torque_nm_shadow": (
                None
                if self.actual_current_torque_nm_shadow is None
                else list(self.actual_current_torque_nm_shadow)
            ),
        }

    @property
    def fingerprint_sha256(self) -> str:
        return _canonical_hash(self.canonical_payload())

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"sample_fingerprint_sha256": self.fingerprint_sha256}


@dataclass(frozen=True)
class DynamicsConformanceBinding:
    """Explicit controller-conformance identity required for production data."""

    binding_id: str
    controller_receipt_identity: str
    controller_receipt_sha256: str
    sample_fingerprint_sha256: str
    model_identity: str
    tcp_identity: str
    calibration_identity: str
    contract_id: str = DYNAMICS_CONFORMANCE_CONTRACT
    schema_version: str = DYNAMICS_CONFORMANCE_SCHEMA
    binding_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != DYNAMICS_CONFORMANCE_SCHEMA:
            raise ValueError("unsupported dynamics conformance schema")
        for name in (
            "binding_id",
            "controller_receipt_identity",
            "model_identity",
            "tcp_identity",
            "calibration_identity",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if self.contract_id != DYNAMICS_CONFORMANCE_CONTRACT:
            raise ValueError("unsupported dynamics conformance contract")
        for name in (
            "controller_receipt_sha256",
            "sample_fingerprint_sha256",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        expected = _canonical_hash(self.canonical_payload())
        if self.binding_sha256 is not None and self.binding_sha256 != expected:
            raise ValueError("dynamics conformance binding hash mismatch")
        object.__setattr__(self, "binding_sha256", expected)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "binding_id": self.binding_id,
            "controller_receipt_identity": self.controller_receipt_identity,
            "controller_receipt_sha256": self.controller_receipt_sha256,
            "sample_fingerprint_sha256": self.sample_fingerprint_sha256,
            "model_identity": self.model_identity,
            "tcp_identity": self.tcp_identity,
            "calibration_identity": self.calibration_identity,
        }

    def accepted_for(self, sample: DynamicsSample) -> bool:
        return bool(
            self.contract_id == DYNAMICS_CONFORMANCE_CONTRACT
            and self.sample_fingerprint_sha256 == sample.fingerprint_sha256
            and self.model_identity == sample.model_identity
            and self.tcp_identity == sample.tcp_identity
            and self.calibration_identity == sample.calibration_identity
            and self.binding_sha256 == _canonical_hash(self.canonical_payload())
        )

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"binding_sha256": self.binding_sha256}


@dataclass(frozen=True)
class DynamicsReceipt:
    """Hash-bound result of one controller-equivalent dynamics reconstruction."""

    sequence: int
    timestamp_s: float
    internal_wrench_tcp_si: tuple[float, ...] | Sequence[float]
    sample_fingerprint_sha256: str
    frame_id: str
    canonical_tcp_frame_id: str
    jacobian_frame_id: str
    source_identity: str
    source_hashes: Mapping[str, str]
    model_identity: str
    tcp_identity: str
    calibration_identity: str
    source_kind: str
    valid: bool
    reason: str | None = None
    actual_current_shadow_wrench_tcp_si: tuple[float, ...] | Sequence[float] | None = None
    actual_current_shadow_delta_norm: float | None = None
    authoritative_torque_source: str = DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE
    conformance_binding: DynamicsConformanceBinding | None = None
    fixture_identity: str | None = None
    schema_version: str = DYNAMICS_RECEIPT_SCHEMA

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not 0 <= self.sequence <= DYNAMICS_MAX_SEQUENCE:
            raise ValueError("dynamics receipt sequence is invalid")
        if not math.isfinite(self.timestamp_s) or not 0.0 <= self.timestamp_s <= DYNAMICS_MAX_TIME_S:
            raise ValueError("dynamics receipt timestamp is invalid")
        if self.schema_version != DYNAMICS_RECEIPT_SCHEMA:
            raise ValueError("unsupported dynamics receipt schema")
        object.__setattr__(self, "sample_fingerprint_sha256", _sha256(self.sample_fingerprint_sha256, "sample_fingerprint_sha256"))
        for name in (
            "frame_id",
            "canonical_tcp_frame_id",
            "jacobian_frame_id",
            "source_identity",
            "model_identity",
            "tcp_identity",
            "calibration_identity",
            "source_kind",
        ):
            object.__setattr__(self, name, _identity(getattr(self, name), name))
        if self.frame_id != self.canonical_tcp_frame_id:
            raise ValueError("dynamics receipt frame does not match canonical TCP frame")
        if self.jacobian_frame_id != self.canonical_tcp_frame_id:
            raise ValueError("dynamics receipt Jacobian frame mismatch")
        object.__setattr__(self, "source_hashes", _source_hashes(self.source_hashes))
        object.__setattr__(self, "internal_wrench_tcp_si", _bounded_vector(self.internal_wrench_tcp_si, 6, "internal_wrench_tcp_si", 1.0e9))
        if self.actual_current_shadow_wrench_tcp_si is not None:
            object.__setattr__(self, "actual_current_shadow_wrench_tcp_si", _bounded_vector(self.actual_current_shadow_wrench_tcp_si, 6, "actual_current_shadow_wrench_tcp_si", 1.0e9))
        if self.actual_current_shadow_delta_norm is not None and (
            not math.isfinite(self.actual_current_shadow_delta_norm)
            or self.actual_current_shadow_delta_norm < 0.0
        ):
            raise ValueError("actual-current shadow delta is invalid")
        if self.authoritative_torque_source != DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE:
            raise ValueError("dynamics receipt torque authority is invalid")
        if self.conformance_binding is not None and not isinstance(
            self.conformance_binding, DynamicsConformanceBinding
        ):
            raise ValueError("dynamics receipt conformance binding has the wrong type")
        if self.source_kind not in {"production", "offline_fake_rtde", "synthetic_fixture"}:
            raise ValueError("unsupported dynamics receipt source kind")
        if self.reason is not None and not _identity(self.reason, "dynamics receipt reason"):
            raise ValueError("dynamics receipt reason is invalid")
        if self.source_kind in {"offline_fake_rtde", "synthetic_fixture"}:
            if self.fixture_identity != OFFLINE_FAKE_RTDE_FIXTURE_ID:
                object.__setattr__(self, "valid", False)
                if self.reason is None:
                    object.__setattr__(self, "reason", "missing_or_unknown_offline_fixture_identity")
        if self.source_kind == "production":
            if self.conformance_binding is None and self.valid:
                object.__setattr__(self, "valid", False)
                if self.reason is None:
                    object.__setattr__(self, "reason", "missing_controller_conformance_binding")
        if self.valid and self.reason is not None:
            raise ValueError("valid dynamics receipt cannot carry a failure reason")
        if not isinstance(self.valid, bool):
            raise ValueError("dynamics receipt valid flag must be boolean")

    @property
    def internal_wrench(self) -> tuple[float, ...]:
        return self.internal_wrench_tcp_si

    @property
    def wrench_tcp_si(self) -> tuple[float, ...]:
        return self.internal_wrench_tcp_si

    @property
    def actual_current_shadow(self) -> tuple[float, ...] | None:
        return self.actual_current_shadow_wrench_tcp_si

    @classmethod
    def from_sample(
        cls,
        sample: DynamicsSample,
        *,
        previous_sample: DynamicsSample | None = None,
        expected_previous_sequence: int | None = None,
        expected_previous_timestamp_s: float | None = None,
        now_s: float | None = None,
        max_age_s: float = DYNAMICS_MAX_INTERVAL_S,
        source_kind: str = "production",
        fixture_identity: str | None = None,
        conformance_binding: DynamicsConformanceBinding | None = None,
    ) -> "DynamicsReceipt":
        if not isinstance(sample, DynamicsSample):
            raise TypeError("DynamicsReceipt requires a DynamicsSample")
        sample.validate_temporal_successor(
            previous_sample,
            expected_previous_sequence=expected_previous_sequence,
            expected_previous_timestamp_s=expected_previous_timestamp_s,
            now_s=now_s,
            max_age_s=max_age_s,
        )
        from .signals import reconstruct_internal_wrench_from_previous_command

        estimate = reconstruct_internal_wrench_from_previous_command(
            previous_applied_no_gravity_joint_torque_nm=sample.previous_commanded_no_gravity_torque_nm,
            previous_jacobian_tcp=sample.calibrated_jacobian,
            previous_coriolis_joint_torque_nm=sample.coriolis_torque_nm,
            previous_joint_damping_torque_nm=sample.joint_damping_torque_nm,
            jacobian_frame_id=sample.jacobian_frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            actual_current_as_torque_nm_shadow=sample.actual_current_torque_nm_shadow,
        )
        source_valid = False
        reason: str | None = None
        if source_kind in {"offline_fake_rtde", "synthetic_fixture"}:
            source_valid = fixture_identity == OFFLINE_FAKE_RTDE_FIXTURE_ID
            if not source_valid:
                reason = "missing_or_unknown_offline_fixture_identity"
        elif source_kind == "production":
            source_valid = conformance_binding is not None and conformance_binding.accepted_for(sample)
            if not source_valid:
                reason = "missing_or_invalid_controller_conformance_binding"
        else:
            reason = "unsupported_dynamics_source_kind"
        return cls(
            sequence=sample.sequence,
            timestamp_s=sample.timestamp_s,
            internal_wrench_tcp_si=estimate.wrench_tcp_si,
            sample_fingerprint_sha256=sample.fingerprint_sha256,
            frame_id=sample.frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            jacobian_frame_id=sample.jacobian_frame_id,
            source_identity=sample.source_identity,
            source_hashes=sample.source_hashes,
            model_identity=sample.model_identity,
            tcp_identity=sample.tcp_identity,
            calibration_identity=sample.calibration_identity,
            source_kind=source_kind,
            valid=source_valid,
            reason=reason,
            actual_current_shadow_wrench_tcp_si=estimate.actual_current_shadow_wrench_tcp_si,
            actual_current_shadow_delta_norm=estimate.actual_current_shadow_delta_norm,
            conformance_binding=conformance_binding,
            fixture_identity=fixture_identity,
        )

    @classmethod
    def invalid_for_sample(cls, sample: DynamicsSample, reason: str) -> "DynamicsReceipt":
        if not isinstance(sample, DynamicsSample):
            raise TypeError("DynamicsReceipt requires a DynamicsSample")
        return cls(
            sequence=sample.sequence,
            timestamp_s=sample.timestamp_s,
            internal_wrench_tcp_si=(0.0,) * 6,
            sample_fingerprint_sha256=sample.fingerprint_sha256,
            frame_id=sample.frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            jacobian_frame_id=sample.jacobian_frame_id,
            source_identity=sample.source_identity,
            source_hashes=sample.source_hashes,
            model_identity=sample.model_identity,
            tcp_identity=sample.tcp_identity,
            calibration_identity=sample.calibration_identity,
            source_kind="production",
            valid=False,
            reason=reason,
        )

    def validate_against(
        self,
        sample: DynamicsSample,
        *,
        previous_sample: DynamicsSample | None = None,
        now_s: float | None = None,
        max_age_s: float = DYNAMICS_MAX_INTERVAL_S,
    ) -> None:
        if not isinstance(sample, DynamicsSample):
            raise TypeError("dynamics receipt/sample mismatch")
        sample.validate_temporal_successor(previous_sample, now_s=now_s, max_age_s=max_age_s)
        fields_to_match = (
            ("sequence", self.sequence, sample.sequence),
            ("timestamp_s", self.timestamp_s, sample.timestamp_s),
            ("frame_id", self.frame_id, sample.frame_id),
            ("canonical_tcp_frame_id", self.canonical_tcp_frame_id, sample.canonical_tcp_frame_id),
            ("jacobian_frame_id", self.jacobian_frame_id, sample.jacobian_frame_id),
            ("source_identity", self.source_identity, sample.source_identity),
            ("model_identity", self.model_identity, sample.model_identity),
            ("tcp_identity", self.tcp_identity, sample.tcp_identity),
            ("calibration_identity", self.calibration_identity, sample.calibration_identity),
        )
        for name, observed, expected in fields_to_match:
            if observed != expected:
                raise ValueError(f"dynamics receipt/sample mismatch: {name}")
        if dict(self.source_hashes) != dict(sample.source_hashes):
            raise ValueError("dynamics receipt/sample mismatch: source_hashes")
        if self.sample_fingerprint_sha256 != sample.fingerprint_sha256:
            raise ValueError("dynamics receipt/sample mismatch: sample_fingerprint_sha256")
        from .signals import reconstruct_internal_wrench_from_previous_command

        estimate = reconstruct_internal_wrench_from_previous_command(
            previous_applied_no_gravity_joint_torque_nm=sample.previous_commanded_no_gravity_torque_nm,
            previous_jacobian_tcp=sample.calibrated_jacobian,
            previous_coriolis_joint_torque_nm=sample.coriolis_torque_nm,
            previous_joint_damping_torque_nm=sample.joint_damping_torque_nm,
            jacobian_frame_id=sample.jacobian_frame_id,
            canonical_tcp_frame_id=sample.canonical_tcp_frame_id,
            actual_current_as_torque_nm_shadow=sample.actual_current_torque_nm_shadow,
        )
        if any(
            not math.isclose(observed, expected, rel_tol=0.0, abs_tol=1e-9)
            for observed, expected in zip(self.internal_wrench_tcp_si, estimate.wrench_tcp_si)
        ):
            raise ValueError("dynamics receipt/sample mismatch: reconstructed wrench")
        if self.valid and self.source_kind == "production":
            if self.conformance_binding is None or not self.conformance_binding.accepted_for(sample):
                raise ValueError("dynamics receipt conformance binding is invalid")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "timestamp_s": self.timestamp_s,
            "internal_wrench_tcp_si": list(self.internal_wrench_tcp_si),
            "sample_fingerprint_sha256": self.sample_fingerprint_sha256,
            "frame_id": self.frame_id,
            "canonical_tcp_frame_id": self.canonical_tcp_frame_id,
            "jacobian_frame_id": self.jacobian_frame_id,
            "source_identity": self.source_identity,
            "source_hashes": dict(self.source_hashes),
            "model_identity": self.model_identity,
            "tcp_identity": self.tcp_identity,
            "calibration_identity": self.calibration_identity,
            "source_kind": self.source_kind,
            "valid": self.valid,
            "reason": self.reason,
            "actual_current_shadow_wrench_tcp_si": (
                None
                if self.actual_current_shadow_wrench_tcp_si is None
                else list(self.actual_current_shadow_wrench_tcp_si)
            ),
            "actual_current_shadow_delta_norm": self.actual_current_shadow_delta_norm,
            "authoritative_torque_source": self.authoritative_torque_source,
            "conformance_binding": (
                None if self.conformance_binding is None else self.conformance_binding.as_json()
            ),
            "fixture_identity": self.fixture_identity,
        }

    @property
    def receipt_fingerprint_sha256(self) -> str:
        return _canonical_hash(self.canonical_payload())

    def as_json(self) -> dict[str, object]:
        return self.canonical_payload() | {"receipt_fingerprint_sha256": self.receipt_fingerprint_sha256}


@dataclass(frozen=True)
class ForceAuthorityReceiptV1:
    """Per-row proof that external/contact authority came from Kunwei."""

    sequence: int
    sample_index: int
    device_time_s: float
    host_visible_time_s: float
    frame_id: str
    authority: KunweiOnlyForceAuthorityV1 = field(default_factory=KunweiOnlyForceAuthorityV1)
    source_sample_sha256: str | None = None
    valid: bool = True
    schema_version: str = "ur10e_tacdiffusion_force_authority_receipt/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "ur10e_tacdiffusion_force_authority_receipt/v1":
            raise ValueError("unsupported force authority receipt schema")
        if (
            isinstance(self.sequence, bool)
            or isinstance(self.sample_index, bool)
            or self.sequence < 0
            or self.sample_index < 0
        ):
            raise ValueError("force authority receipt identity is invalid")
        if not all(
            math.isfinite(float(value))
            and 0.0 <= float(value) <= DYNAMICS_MAX_TIME_S
            for value in (self.device_time_s, self.host_visible_time_s)
        ):
            raise ValueError("force authority receipt timestamps are invalid")
        if not str(self.frame_id).strip() or self.frame_id != self.authority.canonical_tcp_frame_id:
            raise ValueError("force authority receipt frame does not match Kunwei canonical TCP frame")
        if self.source_sample_sha256 is not None:
            object.__setattr__(self, "source_sample_sha256", _sha256(self.source_sample_sha256, "source_sample_sha256"))
        if not isinstance(self.valid, bool) or not self.valid:
            raise ValueError("formal force authority receipt must be valid")

    @property
    def source_identity(self) -> str:
        return self.authority.source_identity

    def as_json(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "sequence": self.sequence,
            "sample_index": self.sample_index,
            "device_time_s": self.device_time_s,
            "host_visible_time_s": self.host_visible_time_s,
            "frame_id": self.frame_id,
            "source_identity": self.source_identity,
            "source_sample_sha256": self.source_sample_sha256,
            "valid": self.valid,
            "authority": self.authority.as_json(),
        }
        validate_formal_force_source_payload(payload)
        return payload

    @property
    def receipt_fingerprint_sha256(self) -> str:
        return _canonical_hash(self.as_json())


@dataclass(frozen=True)
class ProductionDynamicsConformanceReceiptV1:
    """Formal wrapper for the previous-tick production dynamics receipt."""

    dynamics_receipt: DynamicsReceipt
    schema_version: str = "ur10e_tacdiffusion_production_dynamics_receipt/v1"

    def __post_init__(self) -> None:
        if self.schema_version != "ur10e_tacdiffusion_production_dynamics_receipt/v1":
            raise ValueError("unsupported production dynamics receipt schema")
        if not isinstance(self.dynamics_receipt, DynamicsReceipt):
            raise ValueError("production dynamics receipt has the wrong type")
        receipt = self.dynamics_receipt
        if receipt.source_kind != "production" or not receipt.valid:
            raise ValueError("formal dynamics receipt requires production conformance")
        if receipt.conformance_binding is None:
            raise ValueError("formal dynamics receipt requires a conformance binding")
        if receipt.authoritative_torque_source != DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE:
            raise ValueError("formal dynamics receipt torque authority is invalid")

    @property
    def sequence(self) -> int:
        return self.dynamics_receipt.sequence

    @property
    def timestamp_s(self) -> float:
        return self.dynamics_receipt.timestamp_s

    @property
    def internal_wrench_tcp_si(self) -> tuple[float, ...]:
        return self.dynamics_receipt.internal_wrench_tcp_si

    @property
    def actual_current_shadow_wrench_tcp_si(self) -> tuple[float, ...] | None:
        return self.dynamics_receipt.actual_current_shadow_wrench_tcp_si

    @property
    def source_kind(self) -> str:
        return self.dynamics_receipt.source_kind

    @property
    def valid(self) -> bool:
        return self.dynamics_receipt.valid

    @property
    def previous_tick_only(self) -> bool:
        return True

    @property
    def authoritative_torque_source(self) -> str:
        return self.dynamics_receipt.authoritative_torque_source

    @property
    def conformance_binding(self) -> DynamicsConformanceBinding | None:
        return self.dynamics_receipt.conformance_binding

    def as_json(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "source_kind": "production",
            "authoritative_torque_source": DYNAMICS_AUTHORITATIVE_TORQUE_SOURCE,
            "previous_tick_only": True,
            "dynamics_receipt": self.dynamics_receipt.as_json(),
        }
        validate_formal_force_source_payload(payload)
        return payload

    @property
    def receipt_fingerprint_sha256(self) -> str:
        return _canonical_hash(self.as_json())


@dataclass(frozen=True)
class FormalEpisodeManifestV1:
    """Manifest identity required before a row can enter V4 formal data."""

    manifest_id: str
    force_authority: KunweiOnlyForceAuthorityV1
    contact_guard_profile: ContactGuardProfileV1
    rtde_output_fields: Sequence[str]
    source_hashes: Mapping[str, str]
    target_load_n: float
    campaign_kind: str
    campaign_id: str
    trajectory_family: str
    episode_index: int
    impedance_identity: Mapping[str, object]
    expert_action_limits: Mapping[str, object] | None = None
    observation_dimension: int = FORMAL_OBSERVATION_DIMENSION
    control_rate_hz: int = CONTROL_RATE_HZ
    model_rate_candidates_hz: tuple[int, ...] = FORMAL_MODEL_RATE_CANDIDATES_HZ
    sampler_steps: int = FORMAL_SAMPLER_STEPS
    model_active: bool = False
    shadow_only: bool = True
    production_dynamics_required: bool = True
    review_governance_schema: str = FORMAL_REVIEW_GOVERNANCE_SCHEMA_V1
    schema_version: str = FORMAL_EPISODE_MANIFEST_SCHEMA_V2

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_EPISODE_MANIFEST_SCHEMA_V2:
            raise ValueError("unsupported formal episode manifest schema")
        if not _IDENTITY_RE.fullmatch(str(self.manifest_id)):
            raise ValueError("formal episode manifest id is invalid")
        if not isinstance(self.force_authority, KunweiOnlyForceAuthorityV1):
            raise ValueError("formal manifest force authority has the wrong type")
        if not isinstance(self.contact_guard_profile, ContactGuardProfileV1):
            raise ValueError("formal manifest contact guard profile has the wrong type")
        if float(self.target_load_n) not in FORMAL_MANIFEST_TARGET_LOADS_N:
            raise ValueError("formal manifest target_load_n must be one of 3/5/8 N")
        if self.campaign_kind not in FORMAL_MANIFEST_CAMPAIGN_KINDS:
            raise ValueError("formal manifest campaign_kind is invalid")
        expected_campaign_id = (
            FORMAL_MANIFEST_FIXED_CAMPAIGN_ID
            if self.campaign_kind == "fixed_k"
            else FORMAL_MANIFEST_VARIABLE_CAMPAIGN_ID
        )
        if self.campaign_id != expected_campaign_id:
            raise ValueError("formal manifest campaign_id does not match campaign_kind")
        if not str(self.trajectory_family).strip():
            raise ValueError("formal manifest trajectory_family is required")
        if int(self.episode_index) < 0:
            raise ValueError("formal manifest episode_index must be non-negative")
        if not isinstance(self.impedance_identity, Mapping):
            raise ValueError("formal manifest impedance_identity must be a mapping")
        identity = dict(self.impedance_identity)
        if self.campaign_kind == "fixed_k":
            expected_identity = {
                "schema_version": "ur10e_fixed_k_expert/v1",
                "mode": "fixed_k",
                "model_output_dimension": 6,
                "stiffness_6d": [600.0, 600.0, 600.0, 30.0, 30.0, 30.0],
                "active": False,
                "shadow_only": True,
            }
        else:
            expected_identity = {
                "schema_version": "ur10e_variable_k_expert/v1",
                "mode": "variable_k",
                "model_output_dimension": 7,
                "formula": "clip(600 + 200*clip(e/0.010,0,1) - 400*clip((f-8)/4,0,1),400,800)",
                "seventh_output_semantics": "learned_isotropic_translational_K_n_m",
                "seventh_output_training_label": "deterministic_formula_above",
                "rotational_stiffness_nm_rad": 30.0,
                "translational_slew_n_m_s": 400.0,
                "active": False,
                "shadow_only": True,
            }
        if identity != expected_identity:
            raise ValueError("formal manifest impedance_identity mismatch")
        object.__setattr__(self, "impedance_identity", MappingProxyType(expected_identity))
        object.__setattr__(self, "target_load_n", float(self.target_load_n))
        object.__setattr__(self, "episode_index", int(self.episode_index))
        expected_action_limits = {
            "schema_version": FORMAL_EXPERT_ACTION_LIMITS_SCHEMA_V1,
            "frame_id": "tool0_tcp",
            "component_abs_max": list(FORMAL_EXPERT_ACTION_COMPONENT_ABS_MAX),
            "force_norm_max_n": FORMAL_EXPERT_ACTION_FORCE_NORM_MAX_N,
            "torque_norm_max_nm": FORMAL_EXPERT_ACTION_TORQUE_NORM_MAX_NM,
            "slew_per_s": list(FORMAL_EXPERT_ACTION_SLEW_PER_S),
        }
        if self.expert_action_limits is not None:
            if dict(self.expert_action_limits) != expected_action_limits:
                raise ValueError("formal manifest expert action limits mismatch")
            object.__setattr__(self, "expert_action_limits", MappingProxyType(expected_action_limits))
        if not isinstance(self.rtde_output_fields, (list, tuple)) or any(
            not isinstance(value, str) or not value.strip()
            for value in self.rtde_output_fields
        ):
            raise ValueError("formal manifest RTDE output fields must be non-empty strings")
        fields = tuple(self.rtde_output_fields)
        if not fields:
            raise ValueError("formal manifest RTDE output allowlist is empty")
        if self.observation_dimension != FORMAL_OBSERVATION_DIMENSION or self.control_rate_hz != CONTROL_RATE_HZ:
            raise ValueError("formal manifest observation/control dimension or rate is invalid")
        if tuple(self.model_rate_candidates_hz) != FORMAL_MODEL_RATE_CANDIDATES_HZ:
            raise ValueError("formal manifest model-rate candidates must be exactly 100 and 50 Hz")
        if self.sampler_steps != FORMAL_SAMPLER_STEPS:
            raise ValueError("formal manifest sampler must use exactly 50 steps")
        if self.model_active is not False or self.shadow_only is not True:
            raise ValueError("formal V4 model remains inactive and shadow-only")
        if self.production_dynamics_required is not True:
            raise ValueError("formal manifest must require production dynamics conformance")
        if self.review_governance_schema != FORMAL_REVIEW_GOVERNANCE_SCHEMA_V1:
            raise ValueError("formal manifest review governance schema is invalid")
        object.__setattr__(self, "rtde_output_fields", fields)
        object.__setattr__(self, "source_hashes", _source_hashes(self.source_hashes, "source_hashes"))
        validate_formal_rtde_recipe(
            {
                "output_fields": list(fields),
                "force_authority": self.force_authority.as_json(),
                "model_rate_candidates_hz": list(self.model_rate_candidates_hz),
                "observation_dimension": self.observation_dimension,
            },
            authority=self.force_authority,
        )

    def as_json(self) -> dict[str, object]:
        payload = {
            "schema_version": self.schema_version,
            "manifest_id": self.manifest_id,
            "force_authority": self.force_authority.as_json(),
            "contact_guard_profile": self.contact_guard_profile.as_json(),
            "rtde_output_fields": list(self.rtde_output_fields),
            "source_hashes": dict(self.source_hashes),
            "target_load_n": self.target_load_n,
            "campaign_kind": self.campaign_kind,
            "campaign_id": self.campaign_id,
            "trajectory_family": self.trajectory_family,
            "episode_index": self.episode_index,
            "impedance_identity": dict(self.impedance_identity),
            "observation_dimension": self.observation_dimension,
            "control_rate_hz": self.control_rate_hz,
            "model_rate_candidates_hz": list(self.model_rate_candidates_hz),
            "sampler_steps": self.sampler_steps,
            "model_active": self.model_active,
            "shadow_only": self.shadow_only,
            "production_dynamics_required": self.production_dynamics_required,
            "review_governance_schema": self.review_governance_schema,
        }
        if self.expert_action_limits is not None:
            payload["expert_action_limits"] = dict(self.expert_action_limits)
        validate_formal_force_source_payload(payload)
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> "FormalEpisodeManifestV1":
        """Parse one canonical manifest without accepting alternate identities."""

        if not isinstance(payload, Mapping):
            raise ValueError("formal episode manifest must be a mapping")
        raw_authority = payload.get("force_authority")
        raw_profile = payload.get("contact_guard_profile")
        if not isinstance(raw_authority, Mapping) or not isinstance(raw_profile, Mapping):
            raise ValueError("formal episode manifest authority/profile are incomplete")
        authority = KunweiOnlyForceAuthorityV1(**dict(raw_authority))
        profile_authority = raw_profile.get("authority")
        if not isinstance(profile_authority, Mapping):
            raise ValueError("formal contact guard profile authority is missing")
        if dict(profile_authority) != authority.as_json():
            raise ValueError("formal contact guard profile authority mismatch")
        profile_values = dict(raw_profile)
        profile_values.pop("authority", None)
        profile = ContactGuardProfileV1(authority=authority, **profile_values)
        manifest = cls(
            manifest_id=str(payload.get("manifest_id", "")),
            force_authority=authority,
            contact_guard_profile=profile,
            rtde_output_fields=tuple(payload.get("rtde_output_fields", ())),
            source_hashes=payload.get("source_hashes", {}),
            target_load_n=float(payload.get("target_load_n", float("nan"))),
            campaign_kind=str(payload.get("campaign_kind", "")),
            campaign_id=str(payload.get("campaign_id", "")),
            trajectory_family=str(payload.get("trajectory_family", "")),
            episode_index=int(payload.get("episode_index", -1)),
            impedance_identity=payload.get("impedance_identity", {}),
            expert_action_limits=payload.get("expert_action_limits"),
            observation_dimension=int(payload.get("observation_dimension", -1)),
            control_rate_hz=int(payload.get("control_rate_hz", -1)),
            model_rate_candidates_hz=tuple(payload.get("model_rate_candidates_hz", ())),
            sampler_steps=int(payload.get("sampler_steps", -1)),
            model_active=payload.get("model_active", True),
            shadow_only=payload.get("shadow_only", False),
            production_dynamics_required=payload.get("production_dynamics_required", False),
            review_governance_schema=str(payload.get("review_governance_schema", "")),
            schema_version=str(payload.get("schema_version", "")),
        )
        if manifest.as_json() != dict(payload):
            raise ValueError("formal episode manifest contains non-canonical or extra fields")
        return manifest

    @property
    def fingerprint_sha256(self) -> str:
        return _canonical_hash(self.as_json())
