"""Offline R009 behavior and release identity primitives.

R009 identity is deliberately a sidecar to the final contract payload.  The
behavior manifest never contains a generated contract digest, and the final
contract payload never contains the release identity digest.  This keeps the
build graph acyclic while still making every release input content addressed.

The module is dependency-light and read-only: it hashes supplied bytes or
local source files, validates typed mappings, and never persists a contract,
ledger, pointer, or controller artifact.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from step5d_force_objective import (
    BIN_WIDTH_S,
    FORMAL_END_S,
    FORMAL_START_S,
    FORCE_OBJECTIVE_SCHEMA,
    FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
    FORCE_OBJECTIVE_VERSION,
    REQUIRED_BINS,
    TARGET_FORCE_N,
)


ROOT = Path(__file__).resolve().parents[2]

R009_PROGRAM = "step5d_strict_rnn_autotune_v4_r009"
R009_LINEAGE = "step5d_strict_rnn_autotune_v4"
R009_BEHAVIOR_MANIFEST_SCHEMA = "step5d.autotune-v4/r009-behavior-manifest-v1"
R009_SOURCE_SET_SCHEMA = "step5d.autotune-v4/r009-source-set-v1"
R009_CONTACT_SEARCH_SCHEMA = "step5d.autotune-v4/r009-contact-search-schedule-v1"
R009_EXECUTABLE_CONFIG_SCHEMA = "step5d.autotune-v4/r009-executable-behavior-v1"
R009_OBSERVABILITY_CONFIG_SCHEMA = "step5d.autotune-v4/r009-observability-v1"
R009_OBSERVABILITY_VERSION = "r009-observability-v1"
R009_OBSERVABILITY_TTL_FORMULA = "max(3*poll_interval_s,5.0)"
R009_EARLY_ABORT_CONFIG_SCHEMA = "step5d.autotune-v4/r009-early-abort-v1"
R009_EARLY_ABORT_VERSION = "r009-early-abort-v1"
R009_EARLY_ABORT_MODE_ENV = "R009_EARLY_ABORT_MODE"
R009_EARLY_ABORT_SIDECAR_SCHEMA = "step5d.autotune-v4/r009-early-abort-shadow-v1"
R009_EARLY_ABORT_AUDIT_SCHEMA = "step5d.autotune-v4/r009-early-abort-audit-v1"
R009_RELEASE_IDENTITY_SCHEMA = "step5d.autotune-v4/r009-release-identity-v1"
R009_BEHAVIOR_VERSION = "r009-v1"
R009_RAW_CODEC = "r009raw_v1"
R009_RUNTIME_PROTOCOL = 609009
R009_OBJECTIVE_SEMANTIC_FINGERPRINT = FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT

DEFAULT_CONTROLLER_TRIPLET_SHA256 = {
    "script": "0" * 64,
    "txt": "0" * 64,
    "urp": "0" * 64,
}

DEFAULT_CONTACT_SEARCH_SCHEDULE: dict[str, Any] = {
    "schema": R009_CONTACT_SEARCH_SCHEMA,
    "version": "r009-schedule-v1",
    "stages": [
        {
            "name": "far",
            "speed_m_s": 0.0008,
            "acceleration_m_s2": 0.02,
            "max_travel_m": 0.0035,
        },
        {
            "name": "near",
            "speed_m_s": 0.0005,
            "acceleration_m_s2": 0.005,
            "max_travel_m": 0.0215,
        },
    ],
    "timeout_s": 10.0,
    "force_fuse_n": 50.0,
    "force_fuse_reason": 75,
    "confirm_normal_n": 0.5,
    "confirm_force_norm_n": 0.7,
    "confirm_hold_s": 0.08,
}

DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG: dict[str, Any] = {
    "schema": R009_EXECUTABLE_CONFIG_SCHEMA,
    "version": "r009-behavior-v1",
    "values": {
        "program": R009_PROGRAM,
        "path_duration_s": 60.0,
        "path_amplitude_m": 0.015,
        "path_omega_rad_s": 0.1,
        "host_hz": 500.0,
        "rtde_hz": 500.0,
        "kunwei_hz": 500.0,
        "tp_hz": 500.0,
        "minimum_rate_hz": 460.0,
        "one_inflight_dispatch": True,
        "pending_max": 2,
        "physical_inflight_max": 1,
        "cuda_required": True,
        "degraded_fallback": False,
        "observability": {
            "schema": R009_OBSERVABILITY_CONFIG_SCHEMA,
            "version": R009_OBSERVABILITY_VERSION,
            "hot_path_hz": 500,
            "retention_s": 5.0,
            "ring_capacity_rows": 2500,
            "disk_sample_hz": 25,
            "queue_max_rows": 2048,
            "batch_max_rows": 128,
            "batch_max_wait_s": 0.02,
            "attempt_cap_bytes": 2 * 1024 * 1024,
            "run_cap_bytes": 512 * 1024 * 1024,
            "stop_tail_rows": 100,
            "observer_poll_interval_s": 0.2,
            "freshness_ttl_formula": R009_OBSERVABILITY_TTL_FORMULA,
            "stop_states": [90],
            "state20_filename": "r009-state20-observability.jsonl",
            "state25_filename": "r009-state25-observability.jsonl",
            "audit_schema": "step5d.autotune-v4/r009-observability-audit-v1",
        },
        "early_abort": {
            "schema": R009_EARLY_ABORT_CONFIG_SCHEMA,
            "version": R009_EARLY_ABORT_VERSION,
            "mode_env": R009_EARLY_ABORT_MODE_ENV,
            "default_mode": "shadow",
            "channel": "A",
            "guard_fraction": 0.10,
            "kappa_start": 3.0,
            "kappa_end": 1.3,
            "kappa_midpoint": 0.5,
            "kappa_steepness": 10.0,
            "minimum_complete_bins": 1,
            "enters_gp_training": False,
            "enters_raw_ledger": False,
            "sidecar_schema": R009_EARLY_ABORT_SIDECAR_SCHEMA,
            "sidecar_filename": "r009-early-abort-shadow.jsonl",
            "audit_schema": R009_EARLY_ABORT_AUDIT_SCHEMA,
            "audit_filename": "r009-early-abort-audit.jsonl",
            "resume_schema": "step5d.autotune-v4/r009-early-abort-resume-v1",
            "max_sidecar_bytes": 64 * 1024 * 1024,
            "max_audit_rows": 256,
            "max_audit_bytes": 64 * 1024,
            "max_audit_metadata_depth": 4,
            "max_audit_metadata_items": 64,
            "max_audit_metadata_string_chars": 256,
            "channel_c": {
                "status": "future_deferred",
                "requires_new_tp": True,
                "requires_new_fingerprint": True,
                "requires_new_contract": True,
                "reuse_hard_stop_channel": False,
            },
            "objective": {
                "schema": FORCE_OBJECTIVE_SCHEMA,
                "version": FORCE_OBJECTIVE_VERSION,
                "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
                "target_force_n": TARGET_FORCE_N,
                "formal_start_s": FORMAL_START_S,
                "formal_end_s": FORMAL_END_S,
                "bin_width_s": BIN_WIDTH_S,
                "required_bins": REQUIRED_BINS,
                "statistic": "mean(abs(mean(force_in_bin)-5N))",
                "window_semantics": "[start,end)",
                "partial_open_bin_policy": "exclude_until_bin_end",
                "partial_denominator": "required_bins",
            },
        },
    },
}

_GENERATED_R009_PATHS = frozenset(
    {
        "config/step5d/autotune_v4_r009.json",
        "config/step5d/autotune_v4_r009.release-identity.json",
        "config/step5d/r009_release_identity.json",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r009.script",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r009.txt",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r009.urp",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r009.numeric-sanity.json",
        "programs/step5/step5d/step5d_strict_rnn_autotune_v4_r009.deploy-manifest.json",
    }
)


class R009IdentityError(ValueError):
    """A typed R009 behavior or release identity is invalid."""


# Descriptive alias for callers that want to classify manifest-only failures.
R009BehaviorManifestError = R009IdentityError


def _json_compatible_tree(value: Any) -> Any:
    """Detach mapping/sequence containers before passing them to ``json``."""

    if isinstance(value, Mapping):
        return {key: _json_compatible_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible_tree(item) for item in value]
    return value


def _freeze_json(value: Any) -> Any:
    """Recursively remove all JSON-container mutators from a validated value."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    """Return a detached ordinary dict/list JSON tree for public serialization."""

    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return copy.deepcopy(value)


def canonical_bytes(value: Any) -> bytes:
    """Return the one canonical JSON byte representation used for digests."""

    try:
        return json.dumps(
            _json_compatible_tree(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R009IdentityError(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    if not isinstance(value, bytes):
        raise R009IdentityError("SHA-256 input must be bytes")
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R009IdentityError(f"source is not a regular file: {path}")
    return sha256_bytes(path.read_bytes())


def require_digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R009IdentityError(f"{role} must be a lowercase SHA-256")
    return value


def _strict_json_copy(value: Any, role: str) -> Any:
    """Validate JSON-serializability and detach mutable caller state."""

    try:
        encoded = json.dumps(
            _json_compatible_tree(value), ensure_ascii=True, allow_nan=False
        )
        return json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise R009IdentityError(f"{role} is not finite JSON") from exc


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise R009IdentityError(f"{role} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise R009IdentityError(f"{role} must be finite")
    return number


def _nonempty_text(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value:
        raise R009IdentityError(f"{role} must be a non-empty string")
    return value


def _safe_relative_path(value: Any, role: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise R009IdentityError(f"{role} is not a safe relative POSIX path")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise R009IdentityError(f"{role} is not a safe relative POSIX path")
    return value


def _source_files(value: Mapping[str, Any], role: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise R009IdentityError(f"{role} must contain source hash rows")
    result: dict[str, str] = {}
    for raw_path, raw_digest in value.items():
        relative = _safe_relative_path(raw_path, f"{role} path")
        if relative in _GENERATED_R009_PATHS:
            raise R009IdentityError(
                f"{role} must not include generated R009 contract: {relative}"
            )
        result[relative] = require_digest(raw_digest, f"{role} {relative}")
    return dict(sorted(result.items()))


def source_set_digest(source_hashes: Mapping[str, str], *, exclude_paths: Iterable[str] = ()) -> str:
    """Hash a source set while making generated-contract exclusion explicit.

    The caller may explicitly exclude generated paths before hashing.  If a
    generated R009 contract is supplied without that exclusion, fail closed
    instead of silently creating an identity cycle.
    """

    if not isinstance(source_hashes, Mapping):
        raise R009IdentityError("source hash set must be a mapping")
    excluded = {
        _safe_relative_path(path, "excluded source path") for path in exclude_paths
    }
    normalized: dict[str, str] = {}
    for raw_path, raw_digest in source_hashes.items():
        relative = _safe_relative_path(raw_path, "source path")
        if relative in _GENERATED_R009_PATHS and relative not in excluded:
            raise R009IdentityError(
                f"source set includes self-referential generated contract: {relative}"
            )
        if relative in excluded:
            continue
        normalized[relative] = require_digest(raw_digest, f"source {relative}")
    if not normalized:
        raise R009IdentityError("R009 source set cannot be empty")
    return sha256_bytes(
        canonical_bytes(
            {
                "schema": R009_SOURCE_SET_SCHEMA,
                "files": dict(sorted(normalized.items())),
            }
        )
    )


@dataclass(frozen=True)
class R009SourceSet:
    """Immutable source-set binding used by the behavior manifest."""

    files: Mapping[str, str]
    schema: str = R009_SOURCE_SET_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != R009_SOURCE_SET_SCHEMA:
            raise R009IdentityError("R009 source-set schema differs")
        files = _source_files(self.files, "R009 source set")
        if any(path in _GENERATED_R009_PATHS for path in files):
            raise R009IdentityError("R009 source set contains a generated contract")
        object.__setattr__(self, "files", MappingProxyType(files))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009SourceSet":
        if not isinstance(value, Mapping):
            raise R009IdentityError("R009 source closure must be an object")
        required = {"schema", "files", "sha256"}
        if set(value) != required:
            raise R009IdentityError("R009 source closure fields differ")
        source = cls(files=value["files"], schema=value["schema"])
        if require_digest(value["sha256"], "R009 source closure sha256") != source.sha256:
            raise R009IdentityError("R009 source closure digest differs")
        return source

    @classmethod
    def from_files(
        cls,
        root: Path,
        paths: Sequence[str | Path],
        *,
        exclude_paths: Iterable[str] = (),
    ) -> "R009SourceSet":
        root = Path(root).resolve()
        excluded = {_safe_relative_path(path, "excluded source path") for path in exclude_paths}
        rows: dict[str, str] = {}
        for raw_path in paths:
            path = Path(raw_path)
            if path.is_absolute():
                resolved = path.resolve()
                try:
                    relative = resolved.relative_to(root).as_posix()
                except ValueError as exc:
                    raise R009IdentityError("R009 source path escapes root") from exc
            else:
                relative = _safe_relative_path(path.as_posix(), "R009 source path")
                resolved = (root / relative).resolve()
            if relative in excluded:
                continue
            if relative in _GENERATED_R009_PATHS:
                raise R009IdentityError(
                    f"generated R009 contract requires explicit exclusion: {relative}"
                )
            if resolved.is_symlink() or not resolved.is_file():
                raise R009IdentityError(f"R009 source is missing or unsafe: {relative}")
            rows[relative] = sha256_file(resolved)
        return cls(files=rows)

    @property
    def sha256(self) -> str:
        return source_set_digest(self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "files": dict(self.files),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ContactSearchSchedule:
    """Typed, bounded schedule payload; behavior remains replaceable."""

    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        document = _strict_json_copy(self.raw, "contact search schedule")
        if not isinstance(document, dict):
            raise R009IdentityError("contact search schedule must be an object")
        if document.get("schema") != R009_CONTACT_SEARCH_SCHEMA:
            raise R009IdentityError("R009 contact search schedule schema differs")
        _nonempty_text(document.get("version"), "contact search schedule version")
        stages = document.get("stages")
        if not isinstance(stages, list) or not stages:
            raise R009IdentityError("contact search schedule stages are missing")
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                raise R009IdentityError(f"contact search stage {index} is not an object")
            _nonempty_text(stage.get("name"), f"contact search stage {index} name")
            for field in ("speed_m_s", "acceleration_m_s2", "max_travel_m"):
                if _finite(stage.get(field), f"contact search stage {index} {field}") <= 0.0:
                    raise R009IdentityError(
                        f"contact search stage {index} {field} must be positive"
                    )
        if _finite(document.get("timeout_s"), "contact search timeout_s") <= 0.0:
            raise R009IdentityError("contact search timeout_s must be positive")
        if _finite(document.get("force_fuse_n"), "contact search force_fuse_n") <= 0.0:
            raise R009IdentityError("contact search force_fuse_n must be positive")
        reason = document.get("force_fuse_reason")
        if isinstance(reason, bool) or not isinstance(reason, int) or reason <= 0:
            raise R009IdentityError("contact search force_fuse_reason must be a positive int")
        for field in ("confirm_normal_n", "confirm_force_norm_n", "confirm_hold_s"):
            if _finite(document.get(field), f"contact search {field}") <= 0.0:
                raise R009IdentityError(f"contact search {field} must be positive")
        object.__setattr__(self, "raw", _freeze_json(document))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ContactSearchSchedule":
        return cls(raw=value)

    @property
    def schema(self) -> str:
        return str(self.raw["schema"])

    @property
    def version(self) -> str:
        return str(self.raw["version"])

    def as_dict(self) -> dict[str, Any]:
        return _thaw_json(self.raw)


@dataclass(frozen=True)
class R009ObservabilityConfig:
    """Typed, bounded R009 observability behavior parameters.

    The values are deliberately part of the executable behavior config rather
    than module-level runtime knobs.  A session must receive this typed value,
    so changing a retention, sampling, queue, budget, or freshness default
    changes the R009 behavior manifest identity.
    """

    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        document = _strict_json_copy(self.raw, "R009 observability config")
        if not isinstance(document, dict):
            raise R009IdentityError("R009 observability config must be an object")
        required = {
            "schema",
            "version",
            "hot_path_hz",
            "retention_s",
            "ring_capacity_rows",
            "disk_sample_hz",
            "queue_max_rows",
            "batch_max_rows",
            "batch_max_wait_s",
            "attempt_cap_bytes",
            "run_cap_bytes",
            "stop_tail_rows",
            "observer_poll_interval_s",
            "freshness_ttl_formula",
            "stop_states",
            "state20_filename",
            "state25_filename",
            "audit_schema",
        }
        if set(document) != required:
            raise R009IdentityError("R009 observability config fields differ")
        if document.get("schema") != R009_OBSERVABILITY_CONFIG_SCHEMA:
            raise R009IdentityError("R009 observability config schema differs")
        if document.get("version") != R009_OBSERVABILITY_VERSION:
            raise R009IdentityError("R009 observability config version differs")
        if document.get("freshness_ttl_formula") != R009_OBSERVABILITY_TTL_FORMULA:
            raise R009IdentityError("R009 freshness TTL formula differs")

        def positive_int(value: Any, role: str) -> int:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise R009IdentityError(f"{role} must be a positive int")
            return int(value)

        def positive_float(value: Any, role: str) -> float:
            number = _finite(value, role)
            if number <= 0.0:
                raise R009IdentityError(f"{role} must be positive")
            return number

        hot_path_hz = positive_int(document["hot_path_hz"], "R009 hot_path_hz")
        retention_s = positive_float(document["retention_s"], "R009 retention_s")
        ring_capacity = positive_int(
            document["ring_capacity_rows"], "R009 ring_capacity_rows"
        )
        disk_sample_hz = positive_int(
            document["disk_sample_hz"], "R009 disk_sample_hz"
        )
        if disk_sample_hz > hot_path_hz or hot_path_hz % disk_sample_hz:
            raise R009IdentityError(
                "R009 disk_sample_hz must divide hot_path_hz and not exceed it"
            )
        queue_max = positive_int(document["queue_max_rows"], "R009 queue_max_rows")
        batch_max = positive_int(document["batch_max_rows"], "R009 batch_max_rows")
        positive_float(document["batch_max_wait_s"], "R009 batch_max_wait_s")
        attempt_cap = positive_int(
            document["attempt_cap_bytes"], "R009 attempt_cap_bytes"
        )
        run_cap = positive_int(document["run_cap_bytes"], "R009 run_cap_bytes")
        if attempt_cap > run_cap:
            raise R009IdentityError("R009 attempt cap cannot exceed run cap")
        positive_int(document["stop_tail_rows"], "R009 stop_tail_rows")
        positive_float(
            document["observer_poll_interval_s"], "R009 observer_poll_interval_s"
        )
        states = document["stop_states"]
        if not isinstance(states, list) or not states:
            raise R009IdentityError("R009 stop_states must be a non-empty list")
        normalized_states: list[int] = []
        for state in states:
            if isinstance(state, bool) or not isinstance(state, int) or state < 0:
                raise R009IdentityError("R009 stop_states must contain non-negative ints")
            normalized_states.append(int(state))
        if len(set(normalized_states)) != len(normalized_states):
            raise R009IdentityError("R009 stop_states must be unique")
        for field in ("state20_filename", "state25_filename", "audit_schema"):
            if field == "audit_schema":
                _nonempty_text(document[field], f"R009 {field}")
            else:
                _safe_relative_path(document[field], f"R009 {field}")

        # Keep the checked values canonical while preserving the exact
        # user-supplied JSON tree for identity hashing.
        _ = retention_s, ring_capacity, queue_max, batch_max
        object.__setattr__(self, "raw", _freeze_json(document))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009ObservabilityConfig":
        return cls(raw=value)

    @property
    def schema(self) -> str:
        return str(self.raw["schema"])

    @property
    def version(self) -> str:
        return str(self.raw["version"])

    @property
    def hot_path_hz(self) -> int:
        return int(self.raw["hot_path_hz"])

    @property
    def retention_s(self) -> float:
        return float(self.raw["retention_s"])

    @property
    def ring_capacity_rows(self) -> int:
        return int(self.raw["ring_capacity_rows"])

    @property
    def disk_sample_hz(self) -> int:
        return int(self.raw["disk_sample_hz"])

    @property
    def queue_max_rows(self) -> int:
        return int(self.raw["queue_max_rows"])

    @property
    def batch_max_rows(self) -> int:
        return int(self.raw["batch_max_rows"])

    @property
    def batch_max_wait_s(self) -> float:
        return float(self.raw["batch_max_wait_s"])

    @property
    def attempt_cap_bytes(self) -> int:
        return int(self.raw["attempt_cap_bytes"])

    @property
    def run_cap_bytes(self) -> int:
        return int(self.raw["run_cap_bytes"])

    @property
    def stop_tail_rows(self) -> int:
        return int(self.raw["stop_tail_rows"])

    @property
    def observer_poll_interval_s(self) -> float:
        return float(self.raw["observer_poll_interval_s"])

    @property
    def stop_states(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.raw["stop_states"])

    @property
    def state20_filename(self) -> str:
        return str(self.raw["state20_filename"])

    @property
    def state25_filename(self) -> str:
        return str(self.raw["state25_filename"])

    @property
    def audit_schema(self) -> str:
        return str(self.raw["audit_schema"])

    def as_dict(self) -> dict[str, Any]:
        return _thaw_json(self.raw)


@dataclass(frozen=True)
class R009EarlyAbortConfig:
    """Identity-bound, shadow-only R009 early-abort policy.

    The policy is executable host behavior, so thresholds, schemas, default
    modes, persistence limits, and the future Channel C boundary all live in
    this manifest value.  The formal objective fields are checked against the
    canonical ``step5d_force_objective`` primitive rather than redefined here.
    """

    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        document = _strict_json_copy(self.raw, "R009 early-abort config")
        if not isinstance(document, dict):
            raise R009IdentityError("R009 early-abort config must be an object")
        required = {
            "schema",
            "version",
            "mode_env",
            "default_mode",
            "channel",
            "guard_fraction",
            "kappa_start",
            "kappa_end",
            "kappa_midpoint",
            "kappa_steepness",
            "minimum_complete_bins",
            "enters_gp_training",
            "enters_raw_ledger",
            "sidecar_schema",
            "sidecar_filename",
            "audit_schema",
            "audit_filename",
            "resume_schema",
            "max_sidecar_bytes",
            "max_audit_rows",
            "max_audit_bytes",
            "max_audit_metadata_depth",
            "max_audit_metadata_items",
            "max_audit_metadata_string_chars",
            "channel_c",
            "objective",
        }
        if set(document) != required:
            raise R009IdentityError("R009 early-abort config fields differ")
        if document["schema"] != R009_EARLY_ABORT_CONFIG_SCHEMA:
            raise R009IdentityError("R009 early-abort config schema differs")
        if document["version"] != R009_EARLY_ABORT_VERSION:
            raise R009IdentityError("R009 early-abort config version differs")
        _nonempty_text(document["mode_env"], "R009 early-abort mode_env")
        if document["default_mode"] != "shadow":
            raise R009IdentityError("R009 early-abort default mode must be shadow")
        if document["channel"] != "A":
            raise R009IdentityError("R009 early-abort channel must be A")

        def positive_int(value: Any, role: str) -> int:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise R009IdentityError(f"{role} must be a positive int")
            return int(value)

        def nonnegative_float(value: Any, role: str) -> float:
            number = _finite(value, role)
            if number < 0.0:
                raise R009IdentityError(f"{role} must be non-negative")
            return number

        guard = _finite(document["guard_fraction"], "R009 early-abort guard_fraction")
        if not 0.0 <= guard < 1.0:
            raise R009IdentityError("R009 early-abort guard_fraction must be in [0,1)")
        kappa_start = nonnegative_float(
            document["kappa_start"], "R009 early-abort kappa_start"
        )
        kappa_end = nonnegative_float(
            document["kappa_end"], "R009 early-abort kappa_end"
        )
        if kappa_end <= 0.0 or kappa_end > kappa_start or kappa_start <= 0.0:
            raise R009IdentityError("R009 early-abort kappa range is invalid")
        midpoint = _finite(document["kappa_midpoint"], "R009 early-abort kappa_midpoint")
        if not 0.0 <= midpoint <= 1.0:
            raise R009IdentityError("R009 early-abort kappa_midpoint must be in [0,1]")
        steepness = _finite(
            document["kappa_steepness"], "R009 early-abort kappa_steepness"
        )
        if steepness <= 0.0:
            raise R009IdentityError("R009 early-abort kappa_steepness must be positive")
        minimum_bins = positive_int(
            document["minimum_complete_bins"],
            "R009 early-abort minimum_complete_bins",
        )
        if minimum_bins > REQUIRED_BINS:
            raise R009IdentityError("R009 early-abort minimum_complete_bins exceeds formal bins")
        for field in ("enters_gp_training", "enters_raw_ledger"):
            if document[field] is not False:
                raise R009IdentityError(f"R009 early-abort {field} must be false")
        for field in ("sidecar_schema", "audit_schema", "resume_schema"):
            _nonempty_text(document[field], f"R009 early-abort {field}")
        for field in ("sidecar_filename", "audit_filename"):
            _safe_relative_path(document[field], f"R009 early-abort {field}")
        for field in (
            "max_sidecar_bytes",
            "max_audit_rows",
            "max_audit_bytes",
            "max_audit_metadata_depth",
            "max_audit_metadata_items",
            "max_audit_metadata_string_chars",
        ):
            positive_int(document[field], f"R009 early-abort {field}")

        channel_c = document["channel_c"]
        if not isinstance(channel_c, dict) or set(channel_c) != {
            "status",
            "requires_new_tp",
            "requires_new_fingerprint",
            "requires_new_contract",
            "reuse_hard_stop_channel",
        }:
            raise R009IdentityError("R009 Channel C declaration fields differ")
        if channel_c["status"] != "future_deferred":
            raise R009IdentityError("R009 Channel C must remain future-deferred")
        for field in (
            "requires_new_tp",
            "requires_new_fingerprint",
            "requires_new_contract",
        ):
            if channel_c[field] is not True:
                raise R009IdentityError(f"R009 Channel C {field} must be true")
        if channel_c["reuse_hard_stop_channel"] is not False:
            raise R009IdentityError("R009 Channel C cannot reuse hard-stop channel")

        objective = document["objective"]
        if not isinstance(objective, dict) or set(objective) != {
            "schema",
            "version",
            "semantic_fingerprint",
            "target_force_n",
            "formal_start_s",
            "formal_end_s",
            "bin_width_s",
            "required_bins",
            "statistic",
            "window_semantics",
            "partial_open_bin_policy",
            "partial_denominator",
        }:
            raise R009IdentityError("R009 early-abort objective declaration fields differ")
        expected_objective = {
            "schema": FORCE_OBJECTIVE_SCHEMA,
            "version": FORCE_OBJECTIVE_VERSION,
            "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
            "target_force_n": TARGET_FORCE_N,
            "formal_start_s": FORMAL_START_S,
            "formal_end_s": FORMAL_END_S,
            "bin_width_s": BIN_WIDTH_S,
            "required_bins": REQUIRED_BINS,
            "statistic": "mean(abs(mean(force_in_bin)-5N))",
            "window_semantics": "[start,end)",
            "partial_open_bin_policy": "exclude_until_bin_end",
            "partial_denominator": "required_bins",
        }
        if objective != expected_objective:
            raise R009IdentityError(
                "R009 early-abort objective declaration differs from canonical formal objective"
            )
        object.__setattr__(self, "raw", _freeze_json(document))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009EarlyAbortConfig":
        return cls(raw=value)

    @property
    def schema(self) -> str:
        return str(self.raw["schema"])

    @property
    def version(self) -> str:
        return str(self.raw["version"])

    @property
    def mode_env(self) -> str:
        return str(self.raw["mode_env"])

    @property
    def default_mode(self) -> str:
        return str(self.raw["default_mode"])

    @property
    def channel(self) -> str:
        return str(self.raw["channel"])

    @property
    def guard_fraction(self) -> float:
        return float(self.raw["guard_fraction"])

    @property
    def kappa_start(self) -> float:
        return float(self.raw["kappa_start"])

    @property
    def kappa_end(self) -> float:
        return float(self.raw["kappa_end"])

    @property
    def kappa_midpoint(self) -> float:
        return float(self.raw["kappa_midpoint"])

    @property
    def kappa_steepness(self) -> float:
        return float(self.raw["kappa_steepness"])

    @property
    def minimum_complete_bins(self) -> int:
        return int(self.raw["minimum_complete_bins"])

    @property
    def sidecar_schema(self) -> str:
        return str(self.raw["sidecar_schema"])

    @property
    def sidecar_filename(self) -> str:
        return str(self.raw["sidecar_filename"])

    @property
    def audit_schema(self) -> str:
        return str(self.raw["audit_schema"])

    @property
    def audit_filename(self) -> str:
        return str(self.raw["audit_filename"])

    @property
    def resume_schema(self) -> str:
        return str(self.raw["resume_schema"])

    @property
    def max_sidecar_bytes(self) -> int:
        return int(self.raw["max_sidecar_bytes"])

    @property
    def max_audit_rows(self) -> int:
        return int(self.raw["max_audit_rows"])

    @property
    def max_audit_bytes(self) -> int:
        return int(self.raw["max_audit_bytes"])

    @property
    def max_audit_metadata_depth(self) -> int:
        return int(self.raw["max_audit_metadata_depth"])

    @property
    def max_audit_metadata_items(self) -> int:
        return int(self.raw["max_audit_metadata_items"])

    @property
    def max_audit_metadata_string_chars(self) -> int:
        return int(self.raw["max_audit_metadata_string_chars"])

    @property
    def objective(self) -> Mapping[str, Any]:
        return _thaw_json(self.raw["objective"])

    @property
    def channel_c(self) -> Mapping[str, Any]:
        return _thaw_json(self.raw["channel_c"])

    def as_dict(self) -> dict[str, Any]:
        return _thaw_json(self.raw)


@dataclass(frozen=True)
class ExecutableBehaviorConfig:
    """Typed executable behavior configuration bound into the manifest."""

    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        document = _strict_json_copy(self.raw, "executable behavior config")
        if not isinstance(document, dict):
            raise R009IdentityError("executable behavior config must be an object")
        if document.get("schema") != R009_EXECUTABLE_CONFIG_SCHEMA:
            raise R009IdentityError("R009 executable behavior schema differs")
        _nonempty_text(document.get("version"), "executable behavior version")
        if not isinstance(document.get("values"), dict) or not document["values"]:
            raise R009IdentityError("executable behavior values are missing")
        if "observability" not in document["values"]:
            raise R009IdentityError(
                "R009 executable behavior config lacks observability values"
            )
        if "early_abort" not in document["values"]:
            raise R009IdentityError(
                "R009 executable behavior config lacks early-abort values"
            )
        R009ObservabilityConfig.from_mapping(document["values"]["observability"])
        R009EarlyAbortConfig.from_mapping(document["values"]["early_abort"])
        object.__setattr__(self, "raw", _freeze_json(document))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutableBehaviorConfig":
        return cls(raw=value)

    def as_dict(self) -> dict[str, Any]:
        return _thaw_json(self.raw)

    @property
    def observability(self) -> R009ObservabilityConfig:
        return R009ObservabilityConfig.from_mapping(self.raw["values"]["observability"])

    @property
    def early_abort(self) -> R009EarlyAbortConfig:
        return R009EarlyAbortConfig.from_mapping(self.raw["values"]["early_abort"])


@dataclass(frozen=True)
class ControllerTriplet:
    """Typed controller artifact hash triplet; no controller I/O is performed."""

    script: str
    txt: str
    urp: str

    def __post_init__(self) -> None:
        for field in ("script", "txt", "urp"):
            require_digest(getattr(self, field), f"controller {field}")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ControllerTriplet":
        if not isinstance(value, Mapping) or set(value) != {"script", "txt", "urp"}:
            raise R009IdentityError("controller triplet fields differ")
        return cls(
            script=require_digest(value["script"], "controller script"),
            txt=require_digest(value["txt"], "controller txt"),
            urp=require_digest(value["urp"], "controller urp"),
        )

    def as_dict(self) -> dict[str, str]:
        return {"script": self.script, "txt": self.txt, "urp": self.urp}


@dataclass(frozen=True)
class R009BehaviorManifest:
    """Canonical typed behavior identity.  No campaign digest is stored in it."""

    parent_r006_contract_sha256: str
    parent_r006_source_closure_sha256: str
    source_set: R009SourceSet
    contact_search_schedule: ContactSearchSchedule
    raw_codec: str
    objective_semantic_fingerprint: str
    runtime_protocol: int
    executable_behavior_config: ExecutableBehaviorConfig
    program: str = R009_PROGRAM
    lineage: str = R009_LINEAGE
    schema: str = R009_BEHAVIOR_MANIFEST_SCHEMA
    version: str = R009_BEHAVIOR_VERSION

    def __post_init__(self) -> None:
        if self.schema != R009_BEHAVIOR_MANIFEST_SCHEMA:
            raise R009IdentityError("R009 behavior manifest schema differs")
        if self.version != R009_BEHAVIOR_VERSION:
            raise R009IdentityError("R009 behavior manifest version differs")
        if self.program != R009_PROGRAM or self.lineage != R009_LINEAGE:
            raise R009IdentityError("R009 behavior program/lineage differs")
        require_digest(self.parent_r006_contract_sha256, "parent r006 contract")
        require_digest(self.parent_r006_source_closure_sha256, "parent r006 source closure")
        _nonempty_text(self.raw_codec, "R009 raw codec")
        _nonempty_text(self.objective_semantic_fingerprint, "R009 objective semantic fingerprint")
        if (
            isinstance(self.runtime_protocol, bool)
            or not isinstance(self.runtime_protocol, int)
            or self.runtime_protocol <= 0
        ):
            raise R009IdentityError("R009 runtime protocol must be a positive int")
        if not isinstance(self.source_set, R009SourceSet):
            raise R009IdentityError("R009 source set is not typed")
        if not isinstance(self.contact_search_schedule, ContactSearchSchedule):
            raise R009IdentityError("R009 contact search schedule is not typed")
        if not isinstance(self.executable_behavior_config, ExecutableBehaviorConfig):
            raise R009IdentityError("R009 executable behavior config is not typed")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "program": self.program,
            "lineage": self.lineage,
            "parent_r006_contract_sha256": self.parent_r006_contract_sha256,
            "parent_r006_source_closure_sha256": self.parent_r006_source_closure_sha256,
            "r009_source_closure_sha256": self.source_set.sha256,
            "source_closure": self.source_set.as_dict(),
            "contact_search_schedule": self.contact_search_schedule.as_dict(),
            "raw_codec": self.raw_codec,
            "objective_semantic_fingerprint": self.objective_semantic_fingerprint,
            "runtime_protocol": self.runtime_protocol,
            "executable_behavior_config": self.executable_behavior_config.as_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009BehaviorManifest":
        if not isinstance(value, Mapping):
            raise R009IdentityError("R009 behavior manifest must be an object")
        required = {
            "schema",
            "version",
            "program",
            "lineage",
            "parent_r006_contract_sha256",
            "parent_r006_source_closure_sha256",
            "r009_source_closure_sha256",
            "source_closure",
            "contact_search_schedule",
            "raw_codec",
            "objective_semantic_fingerprint",
            "runtime_protocol",
            "executable_behavior_config",
        }
        if set(value) != required:
            raise R009IdentityError("R009 behavior manifest fields differ")
        source_set = R009SourceSet.from_mapping(value["source_closure"])
        if value["r009_source_closure_sha256"] != source_set.sha256:
            raise R009IdentityError("R009 source closure alias differs")
        return cls(
            schema=value["schema"],
            version=value["version"],
            program=value["program"],
            lineage=value["lineage"],
            parent_r006_contract_sha256=require_digest(
                value["parent_r006_contract_sha256"], "parent r006 contract"
            ),
            parent_r006_source_closure_sha256=require_digest(
                value["parent_r006_source_closure_sha256"], "parent r006 source closure"
            ),
            source_set=source_set,
            contact_search_schedule=ContactSearchSchedule.from_mapping(
                value["contact_search_schedule"]
            ),
            raw_codec=value["raw_codec"],
            objective_semantic_fingerprint=value["objective_semantic_fingerprint"],
            runtime_protocol=value["runtime_protocol"],
            executable_behavior_config=ExecutableBehaviorConfig.from_mapping(
                value["executable_behavior_config"]
            ),
        )

    @property
    def behavior_manifest_sha256(self) -> str:
        return sha256_bytes(canonical_bytes(self.as_dict()))

    @property
    def campaign_fingerprint(self) -> str:
        """The canonical campaign fingerprint is exactly the manifest digest."""

        return self.behavior_manifest_sha256


def default_source_set(root: Path = ROOT) -> R009SourceSet:
    paths = (
        "tools/step5d_autotune_v4_r009/__init__.py",
        "tools/step5d_autotune_v4_r009/identity.py",
        "tools/step5d_autotune_v4_r009/contracts.py",
        "tools/step5d_autotune_v4_r009/ledger.py",
        "tools/step5d_autotune_v4_r009/quarantine.py",
        "tools/step5d_autotune_v4_r009/freshness.py",
        "tools/step5d_autotune_v4_r009/transport.py",
        "tools/step5d_autotune_v4_r009/fake_rtde.py",
        "tools/step5d_autotune_v4_r009/diagnostics.py",
        "tools/step5d_autotune_v4_r009/early_abort.py",
        "tools/step5d_autotune_v4_r009/observability.py",
        "tools/step5d_autotune_v4_r009/observer.py",
        "tools/step5d_autotune_v4_r009/tp.py",
        "tools/build_step5d_autotune_v4_r009.py",
        "tools/run_step5d_autotune_v4_r008.py",
        "tools/run_step5d_autotune_v4_r008_b3_two_stage.py",
        "tools/launch_step5d_autotune_v4_r008_control.py",
        "tools/step5d_autotune_v4_r008/live_adapter.py",
        "tools/r008_rtde_seq_probe_inject.py",
        "config/schemas/step5d_autotune_v4_r009_behavior_manifest.schema.json",
        "config/schemas/step5d_autotune_v4_r009_release_identity.schema.json",
        "config/schemas/step5d_autotune_v4_r009_reason43_runtime_protocol.schema.json",
        "tools/step5d_force_objective.py",
        "tests/test_step5d_autotune_v4_r009_identity_quarantine.py",
        "tests/test_step5d_autotune_v4_r009_reason43_runtime_protocol.py",
        "tests/test_step5d_autotune_v4_r009_observability.py",
        "tests/test_step5d_autotune_v4_r009_early_abort.py",
    )
    return R009SourceSet.from_files(root, paths)


def _coerce_source_set(value: R009SourceSet | Mapping[str, Any] | None) -> R009SourceSet:
    if value is None:
        return default_source_set()
    if isinstance(value, R009SourceSet):
        return value
    if not isinstance(value, Mapping):
        raise R009IdentityError("source_set must be R009SourceSet or hash mapping")
    if set(value) == {"schema", "files", "sha256"}:
        return R009SourceSet.from_mapping(value)
    return R009SourceSet(files=value)


def _coerce_alias(primary: str | None, alias: str | None, role: str) -> str:
    if primary is None and alias is None:
        raise R009IdentityError(f"{role} is required")
    if primary is not None and alias is not None and primary != alias:
        raise R009IdentityError(f"{role} aliases differ")
    return require_digest(primary if primary is not None else alias, role)


def build_behavior_manifest(
    *,
    parent_r006_contract_sha256: str | None = None,
    parent_r006_source_closure_sha256: str | None = None,
    source_set: R009SourceSet | Mapping[str, Any] | None = None,
    contact_search_schedule: ContactSearchSchedule | Mapping[str, Any] | None = None,
    raw_codec: str = R009_RAW_CODEC,
    objective_semantic_fingerprint: str = R009_OBJECTIVE_SEMANTIC_FINGERPRINT,
    runtime_protocol: int = R009_RUNTIME_PROTOCOL,
    executable_behavior_config: ExecutableBehaviorConfig | Mapping[str, Any] | None = None,
    parent_contract_sha256: str | None = None,
    parent_source_closure_sha256: str | None = None,
) -> R009BehaviorManifest:
    """Build an R009 manifest without reading or writing a generated contract."""

    parent_contract = _coerce_alias(
        parent_r006_contract_sha256, parent_contract_sha256, "parent r006 contract"
    )
    parent_closure = _coerce_alias(
        parent_r006_source_closure_sha256,
        parent_source_closure_sha256,
        "parent r006 source closure",
    )
    schedule = (
        contact_search_schedule
        if isinstance(contact_search_schedule, ContactSearchSchedule)
        else ContactSearchSchedule.from_mapping(
            DEFAULT_CONTACT_SEARCH_SCHEDULE
            if contact_search_schedule is None
            else contact_search_schedule
        )
    )
    executable = (
        executable_behavior_config
        if isinstance(executable_behavior_config, ExecutableBehaviorConfig)
        else ExecutableBehaviorConfig.from_mapping(
            DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG
            if executable_behavior_config is None
            else executable_behavior_config
        )
    )
    return R009BehaviorManifest(
        parent_r006_contract_sha256=parent_contract,
        parent_r006_source_closure_sha256=parent_closure,
        source_set=_coerce_source_set(source_set),
        contact_search_schedule=schedule,
        raw_codec=raw_codec,
        objective_semantic_fingerprint=objective_semantic_fingerprint,
        runtime_protocol=runtime_protocol,
        executable_behavior_config=executable,
    )


def build_behavior_manifest_from_r006(
    *,
    parent: Any | None = None,
    source_set: R009SourceSet | Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> R009BehaviorManifest:
    """Read the frozen r006 parent binding and build an offline R009 manifest."""

    if parent is None:
        from step5d_autotune_v4_r006.contracts import load_contract

        parent = load_contract()
    parent_contract_sha256 = require_digest(parent.sha256, "parent r006 contract")
    raw = getattr(parent, "raw", {})
    closure = raw.get("source_closure") if isinstance(raw, Mapping) else None
    parent_closure_sha256: str | None = None
    if isinstance(closure, Mapping) and closure.get("sha256") is not None:
        # R006's declaration is the content-addressed source-closure basis.
        # The closure JSON file has a separate transport/file digest and must
        # not be substituted for the declared parent identity.
        parent_closure_sha256 = require_digest(
            closure["sha256"], "parent r006 source closure"
        )
    closure_path = getattr(parent, "source_closure_path", None)
    if parent_closure_sha256 is None:
        if closure_path is None:
            if not isinstance(closure, Mapping):
                raise R009IdentityError("parent r006 source closure declaration is unavailable")
            if not isinstance(closure.get("path"), str):
                raise R009IdentityError("parent r006 source closure path is unavailable")
            closure_path = ROOT / closure["path"]
        parent_closure_sha256 = sha256_file(Path(closure_path))
    return build_behavior_manifest(
        parent_r006_contract_sha256=parent_contract_sha256,
        parent_r006_source_closure_sha256=parent_closure_sha256,
        source_set=source_set,
        **kwargs,
    )


@dataclass(frozen=True)
class R009ReleaseIdentity:
    """The single immutable release identity carried by an admitted ledger."""

    campaign_fingerprint: str
    behavior_manifest_sha256: str
    final_contract_sha256: str
    source_closure_sha256: str
    controller_triplet_sha256: Mapping[str, str]
    runtime_protocol: int
    program: str = R009_PROGRAM
    lineage: str = R009_LINEAGE
    schema: str = R009_RELEASE_IDENTITY_SCHEMA
    version: str = R009_BEHAVIOR_VERSION

    def __post_init__(self) -> None:
        if self.schema != R009_RELEASE_IDENTITY_SCHEMA:
            raise R009IdentityError("R009 release identity schema differs")
        if self.version != R009_BEHAVIOR_VERSION:
            raise R009IdentityError("R009 release identity version differs")
        if self.program != R009_PROGRAM or self.lineage != R009_LINEAGE:
            raise R009IdentityError("R009 release program/lineage differs")
        for field in (
            "campaign_fingerprint",
            "behavior_manifest_sha256",
            "final_contract_sha256",
            "source_closure_sha256",
        ):
            require_digest(getattr(self, field), f"R009 {field}")
        if self.campaign_fingerprint != self.behavior_manifest_sha256:
            raise R009IdentityError(
                "R009 campaign fingerprint must equal the behavior manifest sha256"
            )
        triplet = ControllerTriplet.from_mapping(self.controller_triplet_sha256)
        object.__setattr__(
            self, "controller_triplet_sha256", MappingProxyType(triplet.as_dict())
        )
        if (
            isinstance(self.runtime_protocol, bool)
            or not isinstance(self.runtime_protocol, int)
            or self.runtime_protocol <= 0
        ):
            raise R009IdentityError("R009 release runtime protocol must be a positive int")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R009ReleaseIdentity":
        if not isinstance(value, Mapping):
            raise R009IdentityError("R009 release identity must be an object")
        required = {
            "schema",
            "version",
            "program",
            "lineage",
            "campaign_fingerprint",
            "behavior_manifest_sha256",
            "final_contract_sha256",
            "source_closure_sha256",
            "controller_triplet_sha256",
            "runtime_protocol",
        }
        if set(value) != required:
            raise R009IdentityError("R009 release identity fields differ")
        return cls(
            schema=value["schema"],
            version=value["version"],
            program=value["program"],
            lineage=value["lineage"],
            campaign_fingerprint=require_digest(
                value["campaign_fingerprint"], "R009 campaign fingerprint"
            ),
            behavior_manifest_sha256=require_digest(
                value["behavior_manifest_sha256"], "R009 behavior manifest sha256"
            ),
            final_contract_sha256=require_digest(
                value["final_contract_sha256"], "R009 final contract sha256"
            ),
            source_closure_sha256=require_digest(
                value["source_closure_sha256"], "R009 source closure sha256"
            ),
            controller_triplet_sha256=value["controller_triplet_sha256"],
            runtime_protocol=value["runtime_protocol"],
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "program": self.program,
            "lineage": self.lineage,
            "campaign_fingerprint": self.campaign_fingerprint,
            "behavior_manifest_sha256": self.behavior_manifest_sha256,
            "final_contract_sha256": self.final_contract_sha256,
            "source_closure_sha256": self.source_closure_sha256,
            "controller_triplet_sha256": dict(self.controller_triplet_sha256),
            "runtime_protocol": self.runtime_protocol,
        }

    @property
    def release_identity_sha256(self) -> str:
        """Canonical digest of the identity document, excluding itself."""

        return sha256_bytes(canonical_bytes(self.as_dict()))

    def validate_against(
        self,
        behavior_manifest: R009BehaviorManifest,
        *,
        final_contract_sha256: str,
    ) -> None:
        if self.campaign_fingerprint != behavior_manifest.campaign_fingerprint:
            raise R009IdentityError("release campaign fingerprint differs from behavior manifest")
        if self.behavior_manifest_sha256 != behavior_manifest.behavior_manifest_sha256:
            raise R009IdentityError("release behavior manifest digest differs")
        if self.source_closure_sha256 != behavior_manifest.source_set.sha256:
            raise R009IdentityError("release source closure digest differs")
        if self.runtime_protocol != behavior_manifest.runtime_protocol:
            raise R009IdentityError("release runtime protocol differs")
        if self.final_contract_sha256 != require_digest(
            final_contract_sha256, "final contract sha256"
        ):
            raise R009IdentityError("release final contract digest differs")


def build_release_identity(
    *,
    behavior_manifest: R009BehaviorManifest,
    final_contract_sha256: str,
    controller_triplet_sha256: Mapping[str, str] | None = None,
) -> R009ReleaseIdentity:
    triplet = (
        DEFAULT_CONTROLLER_TRIPLET_SHA256
        if controller_triplet_sha256 is None
        else controller_triplet_sha256
    )
    return R009ReleaseIdentity(
        campaign_fingerprint=behavior_manifest.campaign_fingerprint,
        behavior_manifest_sha256=behavior_manifest.behavior_manifest_sha256,
        final_contract_sha256=require_digest(final_contract_sha256, "final contract sha256"),
        source_closure_sha256=behavior_manifest.source_set.sha256,
        controller_triplet_sha256=triplet,
        runtime_protocol=behavior_manifest.runtime_protocol,
    )


def validate_behavior_manifest(value: Mapping[str, Any]) -> R009BehaviorManifest:
    return R009BehaviorManifest.from_mapping(value)


def validate_release_identity(value: Mapping[str, Any]) -> R009ReleaseIdentity:
    return R009ReleaseIdentity.from_mapping(value)


def release_identity_sha256(value: R009ReleaseIdentity | Mapping[str, Any]) -> str:
    identity = value if isinstance(value, R009ReleaseIdentity) else validate_release_identity(value)
    return identity.release_identity_sha256


__all__ = [
    "ContactSearchSchedule",
    "ControllerTriplet",
    "DEFAULT_CONTACT_SEARCH_SCHEDULE",
    "DEFAULT_CONTROLLER_TRIPLET_SHA256",
    "DEFAULT_EXECUTABLE_BEHAVIOR_CONFIG",
    "R009_EARLY_ABORT_AUDIT_SCHEMA",
    "R009_EARLY_ABORT_CONFIG_SCHEMA",
    "R009_EARLY_ABORT_MODE_ENV",
    "R009_EARLY_ABORT_SIDECAR_SCHEMA",
    "R009_EARLY_ABORT_VERSION",
    "ExecutableBehaviorConfig",
    "R009ObservabilityConfig",
    "R009EarlyAbortConfig",
    "R009_OBSERVABILITY_CONFIG_SCHEMA",
    "R009_OBSERVABILITY_TTL_FORMULA",
    "R009_OBSERVABILITY_VERSION",
    "R009BehaviorManifest",
    "R009BehaviorManifestError",
    "R009_BEHAVIOR_MANIFEST_SCHEMA",
    "R009_BEHAVIOR_VERSION",
    "R009_CONTACT_SEARCH_SCHEMA",
    "R009_EXECUTABLE_CONFIG_SCHEMA",
    "R009IdentityError",
    "R009_LINEAGE",
    "R009_OBJECTIVE_SEMANTIC_FINGERPRINT",
    "R009_PROGRAM",
    "R009_RAW_CODEC",
    "R009_RELEASE_IDENTITY_SCHEMA",
    "R009_RUNTIME_PROTOCOL",
    "R009SourceSet",
    "R009_SOURCE_SET_SCHEMA",
    "R009ReleaseIdentity",
    "build_behavior_manifest",
    "build_behavior_manifest_from_r006",
    "build_release_identity",
    "canonical_bytes",
    "default_source_set",
    "release_identity_sha256",
    "require_digest",
    "sha256_bytes",
    "sha256_file",
    "source_set_digest",
    "validate_behavior_manifest",
    "validate_release_identity",
]
