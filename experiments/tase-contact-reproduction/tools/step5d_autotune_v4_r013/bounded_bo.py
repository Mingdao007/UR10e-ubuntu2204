"""Typed identity and budget contract for the historical-incumbent BO lane."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .runtime_strategy import runtime_strategy_sha256, validate_runtime_strategy


BOUNDED_BO_SCHEMA = "step5d.autotune-v4/r013-bounded-bo-seed-v1"
BOUNDED_BO_VERSION = 1
BOUNDED_BO_POLICY = "bounded_bo_v1"
BOUNDED_BO_TARGET_N = 0.35
BOUNDED_BO_ATTEMPT_BUDGET = 100
BOUNDED_BO_HISTORICAL_CANDIDATE_TOKEN = (
    "fcf0c3255393f2ccc3474865568893556dc7ff3566d7bbca728b51b357aa8da2"
)
_CANDIDATE_FIELDS = (
    "force_p_gain",
    "force_damping",
    "force_i_gain",
    "i_off",
    "normal_filter_tau_s",
    "orientation_ko",
    "motion_kp",
    "target_force_n",
)


class BoundedBOConfigError(ValueError):
    """The bounded BO seed/config receipt is incomplete or inconsistent."""


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    if type(value) is not str or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise BoundedBOConfigError("bounded BO identity must be a lowercase SHA-256")
    return value


def _finite(value: Any, role: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise BoundedBOConfigError(f"bounded BO {role} is not numeric") from exc
    if not math.isfinite(parsed):
        raise BoundedBOConfigError(f"bounded BO {role} is not finite")
    return parsed


def _candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(_CANDIDATE_FIELDS):
        raise BoundedBOConfigError("bounded BO candidate fields differ")
    result: dict[str, Any] = {}
    for field in _CANDIDATE_FIELDS:
        if field == "i_off":
            if type(value[field]) is not bool:
                raise BoundedBOConfigError("bounded BO i_off must be bool")
            result[field] = value[field]
        else:
            result[field] = _finite(value[field], field)
    if result["target_force_n"] != 5.0:
        raise BoundedBOConfigError("bounded BO target force must be 5 N")
    if result["force_p_gain"] <= 0.0 or result["force_damping"] <= 0.0 or result["force_i_gain"] < 0.0:
        raise BoundedBOConfigError("bounded BO gains must be non-negative and non-zero where required")
    return result


def _candidate_token(candidate: Mapping[str, Any]) -> str:
    """Mirror the campaign's canonical physical-candidate token contract.

    Keeping this small identity calculation local avoids importing ``campaign``
    from the seed validator (``campaign`` imports this module), while retaining
    the same canonical JSON schema and ordering used by ``candidate_token``.
    """

    payload = json.dumps(
        {"schema": "r013-physical-candidate-v1", "candidate": dict(candidate)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return _sha256_text(payload)


@dataclass(frozen=True)
class BoundedBOSeedConfigV1:
    """Immutable seed/control-law identity plus a physical-attempt budget."""

    candidate: Mapping[str, Any]
    runtime_strategy: Mapping[str, Any]
    runtime_strategy_sha256: str
    source_closure_sha256: str
    controller_triplet_sha256: Mapping[str, str]
    provenance: Mapping[str, Any]
    physical_attempt_budget: int = BOUNDED_BO_ATTEMPT_BUDGET
    target_mae_n: float = BOUNDED_BO_TARGET_N
    checkpoint_only_target: bool = True
    policy: str = BOUNDED_BO_POLICY
    schema: str = BOUNDED_BO_SCHEMA
    version: int = BOUNDED_BO_VERSION

    def __post_init__(self) -> None:
        if self.schema != BOUNDED_BO_SCHEMA or self.version != BOUNDED_BO_VERSION:
            raise BoundedBOConfigError("bounded BO schema/version differs")
        if self.policy != BOUNDED_BO_POLICY:
            raise BoundedBOConfigError("bounded BO policy differs")
        if self.physical_attempt_budget != BOUNDED_BO_ATTEMPT_BUDGET:
            raise BoundedBOConfigError(
                "bounded BO physical attempt budget must be exactly 100"
            )
        if _finite(self.target_mae_n, "target") != BOUNDED_BO_TARGET_N:
            raise BoundedBOConfigError("bounded BO target must be exactly 0.35 N")
        if self.checkpoint_only_target is not True:
            raise BoundedBOConfigError("bounded BO target must be checkpoint-only")
        candidate = _candidate(self.candidate)
        strategy = validate_runtime_strategy(self.runtime_strategy)
        expected_strategy_sha = runtime_strategy_sha256(strategy)
        if self.runtime_strategy_sha256 != expected_strategy_sha:
            raise BoundedBOConfigError("bounded BO runtime strategy identity differs")
        if not strategy.get("enabled"):
            raise BoundedBOConfigError("bounded BO requires the historical enabled control law")
        _digest(self.source_closure_sha256)
        triplet = dict(self.controller_triplet_sha256)
        if set(triplet) != {"script", "txt", "urp"}:
            raise BoundedBOConfigError("bounded BO controller triplet is incomplete")
        for role in ("script", "txt", "urp"):
            _digest(triplet[role])
        if not isinstance(self.provenance, Mapping) or not self.provenance:
            raise BoundedBOConfigError("bounded BO provenance is missing")
        historical_token = self.provenance.get("historical_candidate_token")
        if historical_token != BOUNDED_BO_HISTORICAL_CANDIDATE_TOKEN:
            raise BoundedBOConfigError(
                "bounded BO historical candidate token is missing or differs"
            )
        if _candidate_token(candidate) != historical_token:
            raise BoundedBOConfigError(
                "bounded BO candidate does not match historical candidate token"
            )
        object.__setattr__(self, "candidate", candidate)
        object.__setattr__(self, "runtime_strategy", strategy)
        object.__setattr__(self, "controller_triplet_sha256", triplet)
        object.__setattr__(self, "provenance", dict(self.provenance))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BoundedBOSeedConfigV1":
        if not isinstance(value, Mapping):
            raise BoundedBOConfigError("bounded BO seed/config must be an object")
        required = {
            "schema", "version", "policy", "physical_attempt_budget", "target_mae_n",
            "checkpoint_only_target", "candidate", "runtime_strategy",
            "runtime_strategy_sha256", "source_closure_sha256",
            "controller_triplet_sha256", "provenance",
        }
        if set(value) != required:
            raise BoundedBOConfigError("bounded BO seed/config fields differ")
        return cls(**dict(value))

    @classmethod
    def load(cls, path: Path) -> "BoundedBOSeedConfigV1":
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise BoundedBOConfigError(f"bounded BO seed/config is not a regular file: {source}")
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BoundedBOConfigError("bounded BO seed/config is unreadable") from exc
        return cls.from_mapping(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "policy": self.policy,
            "physical_attempt_budget": self.physical_attempt_budget,
            "target_mae_n": self.target_mae_n,
            "checkpoint_only_target": self.checkpoint_only_target,
            "candidate": dict(self.candidate),
            "runtime_strategy": dict(self.runtime_strategy),
            "runtime_strategy_sha256": self.runtime_strategy_sha256,
            "source_closure_sha256": self.source_closure_sha256,
            "controller_triplet_sha256": dict(self.controller_triplet_sha256),
            "provenance": dict(self.provenance),
        }

    @property
    def receipt_sha256(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return _sha256_text(payload)

    def campaign_profile(self) -> dict[str, Any]:
        """Return the bounded policy persisted inside the campaign snapshot."""

        return {
            "schema": BOUNDED_BO_SCHEMA,
            "version": BOUNDED_BO_VERSION,
            "policy": BOUNDED_BO_POLICY,
            "physical_attempt_budget": self.physical_attempt_budget,
            "target_mae_n": self.target_mae_n,
            "checkpoint_only_target": self.checkpoint_only_target,
            "seed_receipt_sha256": self.receipt_sha256,
            "runtime_strategy_sha256": self.runtime_strategy_sha256,
            "source_closure_sha256": self.source_closure_sha256,
            "controller_triplet_sha256": dict(self.controller_triplet_sha256),
        }

    @classmethod
    def verify_profile(cls, value: Mapping[str, Any], seed: "BoundedBOSeedConfigV1") -> None:
        expected = seed.campaign_profile()
        if dict(value) != expected:
            raise BoundedBOConfigError("bounded BO campaign profile differs from seed/config receipt")


__all__ = [
    "BOUNDED_BO_ATTEMPT_BUDGET",
    "BOUNDED_BO_POLICY",
    "BOUNDED_BO_SCHEMA",
    "BOUNDED_BO_TARGET_N",
    "BOUNDED_BO_VERSION",
    "BoundedBOConfigError",
    "BoundedBOSeedConfigV1",
]
