"""Canonical, side-effect-free v2 wire models."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


SCHEMA_VERSION = "step5d.autotune/v2"
SEED_P = Decimal("0.001")
SEED_I = Decimal("0.00001")
SEED_D = Decimal("7")
TARGET_FORCE_N = Decimal("12")
LOG2_STEP = Decimal("0.25")
P_D_LOG2_MIN = Decimal("-1")
P_D_LOG2_MAX = Decimal("1")
I_LOG2_MIN = Decimal("0")
I_LOG2_MAX = Decimal("11")
I_GAIN_MAX = Decimal("0.021")
APPROVED_I_MULTIPLIERS = frozenset(
    Decimal(value) for value in ("10", "50", "100", "500", "1000")
)
GROUP_RE = re.compile(r"G[1-9][0-9]*\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
CANONICAL_EXECUTION_PROFILE = {
    "normal_max_rate_rad_s": "0.05",
    "host_qdot_slew_rad_s2": "0.5",
    "tp_speedj_accel_rad_s2": "0.5",
    "qdot_cap_rad_s": "0.5",
}


class ModelError(ValueError):
    """Raised when a public v2 payload is not canonical or safe."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def canonical_decimal(
    value: Any,
    *,
    name: str,
    positive: bool = False,
    non_negative: bool = True,
) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise ModelError(f"{name} must be canonical decimal text, not a float")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ModelError(f"{name} must be finite canonical decimal text") from exc
    if not number.is_finite():
        raise ModelError(f"{name} must be finite")
    if positive and number <= 0:
        raise ModelError(f"{name} must be positive")
    if not positive and non_negative and number < 0:
        raise ModelError(f"{name} must be non-negative")
    if number == 0:
        return "0"
    rendered = format(number.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def canonical_profile_id(profile: Mapping[str, Any]) -> str:
    """Derive the physical profile token from the normalized deployment profile."""

    if not isinstance(profile, Mapping):
        raise ModelError("deployment profile must be an object")
    normalized = {
        name: canonical_decimal(value, name=name, positive=True)
        for name, value in profile.items()
    }
    if normalized != CANONICAL_EXECUTION_PROFILE:
        raise ModelError("deployment profile must remain frozen at .05/.5/.5 with qdot .5")

    def token(name: str, scale: str) -> str:
        value = Decimal(normalized[name]) * Decimal(scale)
        if value != value.to_integral_value():
            raise ModelError(f"{name} cannot be represented by the canonical profile token")
        return f"{int(value):03d}"

    return (
        f"nf{token('normal_max_rate_rad_s', '1000')}-"
        f"slew{token('host_qdot_slew_rad_s2', '100')}-"
        f"a{token('tp_speedj_accel_rad_s2', '100')}"
    )


def canonical_log2(value: Any, *, name: str) -> str:
    text = canonical_decimal(value, name=name, non_negative=False)
    coordinate = Decimal(text)
    steps = coordinate / LOG2_STEP
    if steps != steps.to_integral_value():
        raise ModelError(f"{name} must lie on the 0.25-octave lattice")
    return text


def _gain_from_log2(seed: Decimal, coordinate_text: str) -> str:
    # Preserve the deployed v1 ForceCandidate mapping exactly.  The binary
    # calculation is performed once at the coordinate boundary; its shortest
    # round-trippable text is then canonicalized and all durable comparison is
    # Decimal text/hash based.
    coordinate = float(Decimal(coordinate_text))
    value = float(seed) * 2.0**coordinate
    return canonical_decimal(repr(value), name="derived_gain", positive=True)


def _require_coordinate_range(
    value: str, *, name: str, minimum: Decimal, maximum: Decimal
) -> None:
    coordinate = Decimal(value)
    if coordinate < minimum or coordinate > maximum:
        raise ModelError(f"{name} must remain inside [{minimum},{maximum}] octaves")


def _lattice_values(seed: Decimal) -> frozenset[str]:
    steps = int((P_D_LOG2_MAX - P_D_LOG2_MIN) / LOG2_STEP)
    return frozenset(
        _gain_from_log2(
            seed,
            canonical_decimal(
                P_D_LOG2_MIN + LOG2_STEP * index,
                name="grid",
                non_negative=False,
            ),
        )
        for index in range(steps + 1)
    )


APPROVED_P_GAINS = _lattice_values(SEED_P)
APPROVED_D_GAINS = _lattice_values(SEED_D)
APPROVED_DIRECT_I_GAINS = frozenset(
    {"0", canonical_decimal(SEED_I, name="baseline_i", positive=True)}
    | {
        canonical_decimal(SEED_I * multiplier, name="coarse_i", positive=True)
        for multiplier in APPROVED_I_MULTIPLIERS
    }
)


@dataclass(frozen=True)
class AttemptTupleSpec:
    """Canonical physical tuple identity without imposing the new search envelope."""

    p: str
    i: str
    d: str
    profile_id: str = "nf050-slew050-a050"

    def __post_init__(self) -> None:
        object.__setattr__(self, "p", canonical_decimal(self.p, name="p", positive=True))
        object.__setattr__(self, "i", canonical_decimal(self.i, name="i"))
        object.__setattr__(self, "d", canonical_decimal(self.d, name="d", positive=True))
        if (
            not isinstance(self.profile_id, str)
            or not self.profile_id
            or any(character.isspace() for character in self.profile_id)
        ):
            raise ModelError("profile_id must be a non-empty token")

    @property
    def comparison_key(self) -> str:
        return sha256_json(
            {
                "p": self.p,
                "i": self.i,
                "d": self.d,
                "profile_id": self.profile_id,
                "target_force_n": str(TARGET_FORCE_N),
            }
        )


@dataclass(frozen=True)
class CandidateSpec:
    group_id: str
    p: str
    i: str
    d: str
    profile_id: str = "nf050-slew050-a050"
    log2_p: str | None = None
    log2_i: str | None = None
    log2_d: str | None = None
    i_multiplier: str | None = None
    purpose: str = "search"
    replay_nonce: str | None = None
    replay_reason: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.group_id, str)
            or GROUP_RE.fullmatch(self.group_id) is None
        ):
            raise ModelError("group_id must use G<number>")
        object.__setattr__(self, "p", canonical_decimal(self.p, name="p", positive=True))
        object.__setattr__(self, "i", canonical_decimal(self.i, name="i"))
        object.__setattr__(self, "d", canonical_decimal(self.d, name="d", positive=True))
        if (
            not isinstance(self.profile_id, str)
            or not self.profile_id
            or any(character.isspace() for character in self.profile_id)
        ):
            raise ModelError("profile_id must be a non-empty token")
        for field_name in ("log2_p", "log2_i", "log2_d"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self, field_name, canonical_log2(value, name=field_name)
                )
        if self.log2_p is not None:
            _require_coordinate_range(
                self.log2_p,
                name="log2_p",
                minimum=P_D_LOG2_MIN,
                maximum=P_D_LOG2_MAX,
            )
            if self.p != _gain_from_log2(SEED_P, self.log2_p):
                raise ModelError("p does not match the declared log2_p coordinate")
        elif self.p not in APPROVED_P_GAINS:
            raise ModelError("p must lie on the bounded 0.25-octave grid")
        if self.log2_d is not None:
            _require_coordinate_range(
                self.log2_d,
                name="log2_d",
                minimum=P_D_LOG2_MIN,
                maximum=P_D_LOG2_MAX,
            )
            if self.d != _gain_from_log2(SEED_D, self.log2_d):
                raise ModelError("d does not match the declared log2_d coordinate")
        elif self.d not in APPROVED_D_GAINS:
            raise ModelError("d must lie on the bounded 0.25-octave grid")
        if Decimal(self.i) > I_GAIN_MAX:
            raise ModelError(f"i exceeds the physical search envelope {I_GAIN_MAX}")
        if self.log2_i is not None:
            if self.i_multiplier is not None:
                raise ModelError("I must use either coarse multiplier or log2 coordinate")
            _require_coordinate_range(
                self.log2_i,
                name="log2_i",
                minimum=I_LOG2_MIN,
                maximum=I_LOG2_MAX,
            )
            if self.i != _gain_from_log2(SEED_I, self.log2_i):
                raise ModelError("i does not match the declared log2_i coordinate")
        if self.i_multiplier is not None:
            multiplier = canonical_decimal(self.i_multiplier, name="i_multiplier", positive=True)
            if Decimal(multiplier) not in APPROVED_I_MULTIPLIERS:
                raise ModelError("coarse I multiplier must be one of 10,50,100,500,1000")
            expected_i = canonical_decimal(
                SEED_I * Decimal(multiplier), name="derived_i", positive=True
            )
            if self.i != expected_i:
                raise ModelError("i does not match the declared baseline multiplier")
            object.__setattr__(self, "i_multiplier", multiplier)
        elif self.log2_i is None and self.i not in APPROVED_DIRECT_I_GAINS:
            raise ModelError(
                "direct i must be off, baseline, or an approved coarse multiplier"
            )
        if not isinstance(self.purpose, str) or self.purpose not in {"search", "replay"}:
            raise ModelError("purpose must be search or replay")
        if self.purpose == "search":
            if self.replay_nonce is not None or self.replay_reason is not None:
                raise ModelError("search candidates cannot carry replay metadata")
        elif (
            not isinstance(self.replay_nonce, str)
            or not self.replay_nonce
            or not isinstance(self.replay_reason, str)
            or not self.replay_reason
        ):
            raise ModelError("replay candidates require nonce and reason")

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "CandidateSpec":
        if not isinstance(row, Mapping):
            raise ModelError("candidate must be an object")
        allowed = {
            "group_id",
            "p",
            "i",
            "d",
            "profile_id",
            "log2_p",
            "log2_i",
            "log2_d",
            "i_multiplier",
            "purpose",
            "replay_nonce",
            "replay_reason",
        }
        unknown = set(row).difference(allowed)
        if unknown:
            raise ModelError(f"candidate has unknown fields: {sorted(unknown)}")
        values = dict(row)
        log2_p = values.get("log2_p")
        log2_d = values.get("log2_d")
        multiplier = values.get("i_multiplier")
        if "p" not in values:
            if log2_p is None:
                raise ModelError("candidate requires p or log2_p")
            values["p"] = _gain_from_log2(
                SEED_P, canonical_log2(log2_p, name="log2_p")
            )
        if "d" not in values:
            if log2_d is None:
                raise ModelError("candidate requires d or log2_d")
            values["d"] = _gain_from_log2(
                SEED_D, canonical_log2(log2_d, name="log2_d")
            )
        if "i" not in values:
            if multiplier is not None:
                multiplier_text = canonical_decimal(
                    multiplier, name="i_multiplier", positive=True
                )
                values["i"] = canonical_decimal(
                    SEED_I * Decimal(multiplier_text),
                    name="derived_i",
                    positive=True,
                )
            elif values.get("log2_i") is not None:
                values["i"] = _gain_from_log2(
                    SEED_I, canonical_log2(values["log2_i"], name="log2_i")
                )
            else:
                raise ModelError("candidate requires i, i_multiplier, or log2_i")
        return cls(**values)

    @property
    def comparison_key(self) -> str:
        return sha256_json(
            {
                "p": self.p,
                "i": self.i,
                "d": self.d,
                "profile_id": self.profile_id,
                "target_force_n": str(TARGET_FORCE_N),
            }
        )

    @property
    def candidate_id(self) -> str:
        payload = self.as_dict()
        return sha256_json(payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "p": self.p,
            "i": self.i,
            "d": self.d,
            "profile_id": self.profile_id,
            "log2_p": self.log2_p,
            "log2_i": self.log2_i,
            "log2_d": self.log2_d,
            "i_multiplier": self.i_multiplier,
            "purpose": self.purpose,
            "replay_nonce": self.replay_nonce,
            "replay_reason": self.replay_reason,
            "comparison_key": self.comparison_key,
        }


@dataclass(frozen=True)
class BatchSpec:
    batch_id: str
    source: str
    candidates: tuple[CandidateSpec, ...]
    recovery: bool = False
    completed_groups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.batch_id, str)
            or not self.batch_id
            or not isinstance(self.source, str)
            or not self.source
        ):
            raise ModelError("batch_id and source are required")
        if type(self.recovery) is not bool:
            raise ModelError("recovery must be a boolean")
        if not isinstance(self.candidates, tuple):
            raise ModelError("candidates must be an immutable tuple")
        if not isinstance(self.completed_groups, tuple) or any(
            not isinstance(group, str) for group in self.completed_groups
        ):
            raise ModelError("completed_groups must be a string tuple")
        expected = 4 if self.recovery else 5
        if len(self.candidates) != expected:
            raise ModelError(
                f"{'recovery' if self.recovery else 'normal'} batch requires exactly {expected} candidates"
            )
        groups = [candidate.group_id for candidate in self.candidates]
        if len(groups) != len(set(groups)):
            raise ModelError("candidate group ids must be unique within a batch")
        keys = [candidate.comparison_key for candidate in self.candidates]
        if len(keys) != len(set(keys)):
            raise ModelError("physical search tuples must be unique within a batch")
        if self.recovery and self.completed_groups != ("G11",):
            raise ModelError("the bounded four-candidate recovery must bind completed G11")
        if any(candidate.purpose != "search" for candidate in self.candidates):
            raise ModelError("batch enqueue accepts search candidates only")
        if any(
            candidate.log2_p is None or candidate.log2_d is None
            for candidate in self.candidates
        ):
            raise ModelError("batch candidates must declare log2 P/D coordinates")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "BatchSpec":
        if not isinstance(payload, Mapping):
            raise ModelError("batch must be an object")
        allowed = {"schema", "batch_id", "source", "recovery", "completed_groups", "candidates"}
        unknown = set(payload).difference(allowed)
        if unknown:
            raise ModelError(f"batch has unknown fields: {sorted(unknown)}")
        if payload.get("schema") != "step5d.autotune.batch/v2":
            raise ModelError("unknown batch schema")
        rows = payload.get("candidates")
        if not isinstance(rows, list):
            raise ModelError("candidates must be an array")
        recovery = payload.get("recovery", False)
        if type(recovery) is not bool:
            raise ModelError("recovery must be a boolean")
        completed_groups = payload.get("completed_groups", [])
        if not isinstance(completed_groups, list) or any(
            not isinstance(group, str) for group in completed_groups
        ):
            raise ModelError("completed_groups must be a string array")
        batch_id = payload.get("batch_id")
        source = payload.get("source")
        if not isinstance(batch_id, str) or not isinstance(source, str):
            raise ModelError("batch_id and source must be strings")
        return cls(
            batch_id=batch_id,
            source=source,
            candidates=tuple(CandidateSpec.from_mapping(row) for row in rows),
            recovery=recovery,
            completed_groups=tuple(completed_groups),
        )


@dataclass(frozen=True)
class DeploymentSpec:
    deployment_id: str
    code_fingerprint: str
    tp_fingerprint: str
    guard_fingerprint: str
    profile: Mapping[str, Any]
    deployment_authorized: bool
    controller_readback_verified: bool

    def __post_init__(self) -> None:
        if not isinstance(self.deployment_id, str) or not self.deployment_id:
            raise ModelError("deployment_id is required")
        if type(self.deployment_authorized) is not bool or type(
            self.controller_readback_verified
        ) is not bool:
            raise ModelError("deployment authorization facts must be booleans")
        if not isinstance(self.profile, Mapping):
            raise ModelError("deployment profile must be an object")
        for name in ("code_fingerprint", "tp_fingerprint", "guard_fingerprint"):
            if SHA256_RE.fullmatch(getattr(self, name)) is None:
                raise ModelError(f"{name} must be a lowercase SHA-256")
        normalized = {
            name: canonical_decimal(value, name=name, positive=True)
            for name, value in self.profile.items()
        }
        canonical_profile_id(normalized)
        object.__setattr__(self, "profile", normalized)

    @property
    def profile_id(self) -> str:
        return canonical_profile_id(self.profile)
