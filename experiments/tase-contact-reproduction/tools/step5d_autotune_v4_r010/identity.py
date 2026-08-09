"""Canonical R010 behavior manifest and immutable release identity."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from step5d_force_objective import FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT
from step5d_autotune_v4_r009.contracts import load_contract as load_r009_contract

from .behavior import (
    EARLY_ABORT_SIGMOID_SEMANTICS,
    R010_PROGRAM,
    R010_RUNTIME_PROTOCOL,
    Wave7Schedule,
    validate_wave7_schedule,
)
from .gp_calibration import (
    VARIANCE_ESTIMATOR,
    load_calibration_artifact,
)
from .runtime_composition import (
    runtime_composition_manifest,
    validate_runtime_composition,
)


ROOT = Path(__file__).resolve().parents[2]
R010_LINEAGE = "step5d_strict_rnn_autotune_v4"
BEHAVIOR_MANIFEST_SCHEMA = "step5d.autotune-v4/r010-behavior-manifest-v1"
SOURCE_CLOSURE_SCHEMA = "step5d.autotune-v4/r010-source-closure-v1"
RELEASE_IDENTITY_SCHEMA = "step5d.autotune-v4/r010-release-identity-v1"
BEHAVIOR_VERSION = "r010-v1"
RAW_CODEC = "r009raw_v1"
DEFAULT_SCHEDULE_PATH = ROOT / "config/step5d/autotune_v4_r010_wave7_schedule.json"
DEFAULT_CALIBRATION_PATH = ROOT / "config/step5d/autotune_v4_r010_gp_calibration.json"

GENERATED_PATHS = frozenset(
    {
        "config/step5d/autotune_v4_r010.json",
        "config/step5d/autotune_v4_r010.behavior-manifest.json",
        "config/step5d/autotune_v4_r010.release-identity.json",
        "config/step5d/autotune_v4_r010_offline_closure.json",
        "config/step5d/r010_release_identity.json",
        "config/step5d/autotune_v4_r010_empty_ledger.jsonl",
        f"programs/step5/step5d/{R010_PROGRAM}.script",
        f"programs/step5/step5d/{R010_PROGRAM}.txt",
        f"programs/step5/step5d/{R010_PROGRAM}.urp",
        f"programs/step5/step5d/{R010_PROGRAM}.numeric-sanity.json",
        f"programs/step5/step5d/{R010_PROGRAM}.deploy-manifest.json",
    }
)

DEFAULT_SOURCE_PATHS = (
    "tools/step5d_autotune_v4_r010/__init__.py",
    "tools/step5d_autotune_v4_r010/behavior.py",
    "tools/step5d_autotune_v4_r010/kernel.py",
    "tools/step5d_autotune_v4_r010/gp_calibration.py",
    "tools/step5d_autotune_v4_r010/calibration_runner.py",
    "tools/step5d_autotune_v4_r010/optimizer_worker.py",
    "tools/step5d_autotune_v4_r010/optimizer_worker_loop.py",
    "tools/step5d_autotune_v4_r010/optimizer_keepalive.py",
    "tools/step5d_autotune_v4_r010/identity.py",
    "tools/step5d_autotune_v4_r010/contracts.py",
    "tools/step5d_autotune_v4_r010/ledger.py",
    "tools/step5d_autotune_v4_r010/runtime_composition.py",
    "tools/step5d_autotune_v4_r010/tp.py",
    "tools/calibrate_step5d_autotune_v4_r010_gp.py",
    "tools/build_step5d_autotune_v4_r010.py",
    "config/step5d/autotune_v4_r010_wave7_schedule.json",
    "config/step5d/autotune_v4_r010_gp_calibration.json",
    # Reused R009 runtime components are pinned by byte, not by lineage name.
    "tools/step5d_autotune_v4_r009/early_abort.py",
    "tools/step5d_autotune_v4_r009/observability.py",
    "tools/step5d_autotune_v4_r009/observer.py",
    "tools/step5d_autotune_v4_r009/freshness.py",
    "tools/step5d_autotune_v4_r009/transport.py",
    "tools/step5d_autotune_v4_r009/fake_rtde.py",
    "tools/step5d_autotune_v4_r009/diagnostics.py",
    "tools/step5d_autotune_v4_r009/tp.py",
    # Optimizer worker, keepalive, feature map, qLogNEI, and direct imports.
    "tools/step5d_autotune_v4_r008/optimizer_worker_batched.py",
    "tools/step5d_autotune_v4_r008/optimizer_worker_loop.py",
    "tools/step5d_autotune_v4_r008/optimizer_keepalive.py",
    "tools/step5d_autotune_v4_r008/optimizer.py",
    "tools/step5d_autotune_v4_r008/qlognei_compile.py",
    "tools/step5d_autotune_v4_r008/bounded_worker_artifact_binding.py",
    "tools/step5d_autotune_v4_r008/bounded_sidecar_verify.py",
    "tools/step5d_autotune_v4_r008/binary_seal.py",
    "tools/step5d_autotune_v4_r008/raw_force_binary.py",
    "tools/step5d_autotune_v4_r008/raw_force_binary_v2.py",
    "tools/step5d_autotune_v4_r008/raw_force_columnar_verify.py",
    "tools/step5d_autotune_v4_r008/hard_stop_penalty.py",
    "tools/step5d_autotune_v4_r008/early_abort_penalty.py",
    "tools/step5d_autotune_v4_r006/optimizer_worker.py",
    "tools/step5d_autotune_v4_r006/contracts.py",
    "tools/step5d_autotune_v4_r006/lattice.py",
    "tools/step5d_autotune_v4_r006/objective.py",
    "tools/step5d_optimizer_runtime.py",
    "tools/step5d_force_objective.py",
)


class R010IdentityError(ValueError):
    """R010 behavior/source/release identity is invalid."""


def _json_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_tree(item) for item in value]
    return copy.deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _json_tree(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R010IdentityError(f"value is not canonical JSON: {exc}") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R010IdentityError(f"source is missing or unsafe: {path}")
    return sha256_bytes(path.read_bytes())


def require_digest(value: Any, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise R010IdentityError(f"{role} must be a lowercase SHA-256")
    return value


def _safe_relative(value: str | Path) -> str:
    path = PurePosixPath(Path(value).as_posix())
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise R010IdentityError(f"unsafe source path: {value}")
    return path.as_posix()


@dataclass(frozen=True)
class SourceClosure:
    files: Mapping[str, str]
    schema: str = SOURCE_CLOSURE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SOURCE_CLOSURE_SCHEMA or not isinstance(self.files, Mapping) or not self.files:
            raise R010IdentityError("R010 source closure differs")
        normalized: dict[str, str] = {}
        for path, digest in self.files.items():
            relative = _safe_relative(path)
            if relative in GENERATED_PATHS:
                raise R010IdentityError("R010 source closure contains generated release output")
            if relative.startswith("tools/stars_ft_bias_shadow/"):
                raise R010IdentityError("STARS analysis sidecar cannot enter R010 campaign identity")
            normalized[relative] = require_digest(digest, f"source {relative}")
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(normalized.items()))))

    @classmethod
    def from_files(cls, root: Path, paths: Sequence[str | Path]) -> "SourceClosure":
        root = Path(root).resolve()
        rows: dict[str, str] = {}
        for value in paths:
            relative = _safe_relative(value)
            path = root / relative
            if path.resolve().parent == root and path.name == "":
                raise R010IdentityError("source path is invalid")
            rows[relative] = sha256_file(path)
        return cls(rows)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceClosure":
        if not isinstance(value, Mapping) or set(value) != {"schema", "files", "sha256"}:
            raise R010IdentityError("R010 source closure fields differ")
        closure = cls(value["files"], schema=value["schema"])
        if value["sha256"] != closure.sha256:
            raise R010IdentityError("R010 source closure digest differs")
        return closure

    @property
    def sha256(self) -> str:
        return sha256_bytes(canonical_bytes({"schema": self.schema, "files": dict(self.files)}))

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "files": dict(self.files), "sha256": self.sha256}


def default_source_closure(root: Path = ROOT) -> SourceClosure:
    return SourceClosure.from_files(root, DEFAULT_SOURCE_PATHS)


@dataclass(frozen=True)
class BehaviorManifest:
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", _freeze(_json_tree(self.raw)))

    def as_dict(self) -> dict[str, Any]:
        return _json_tree(self.raw)

    @property
    def behavior_manifest_sha256(self) -> str:
        return sha256_bytes(canonical_bytes(self.raw))

    @property
    def campaign_fingerprint(self) -> str:
        return self.behavior_manifest_sha256

    @property
    def source_closure(self) -> SourceClosure:
        return SourceClosure.from_mapping(self.raw["source_closure"])

    @property
    def schedule(self) -> Wave7Schedule:
        return validate_wave7_schedule(self.raw["wave7_contact_entry"])


def _strict_json_file(path: Path, role: str) -> dict[str, Any]:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R010IdentityError(f"{role} is missing or unsafe: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise R010IdentityError(f"{role} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise R010IdentityError(f"{role} must be an object")
    return value


def build_behavior_manifest(
    *,
    parent_r009: Any | None = None,
    source_closure: SourceClosure | None = None,
    schedule: Wave7Schedule | Mapping[str, Any] | None = None,
    calibration: Mapping[str, Any] | None = None,
    schedule_path: Path = DEFAULT_SCHEDULE_PATH,
    calibration_path: Path = DEFAULT_CALIBRATION_PATH,
) -> BehaviorManifest:
    parent = load_r009_contract() if parent_r009 is None else parent_r009
    parent_identity = getattr(parent, "release_identity", None)
    if parent_identity is None or not hasattr(parent_identity, "as_dict"):
        raise R010IdentityError("R010 parent must be a validated R009 contract")
    parent_document = parent_identity.as_dict()
    parent_sha = require_digest(parent_identity.release_identity_sha256, "parent R009 release identity")
    active_schedule = (
        validate_wave7_schedule(_strict_json_file(schedule_path, "Wave7 schedule"))
        if schedule is None
        else schedule
        if isinstance(schedule, Wave7Schedule)
        else validate_wave7_schedule(schedule)
    )
    active_calibration = (
        load_calibration_artifact(calibration_path)
        if calibration is None
        else _json_tree(calibration)
    )
    from .gp_calibration import validate_calibration_artifact

    active_calibration = validate_calibration_artifact(active_calibration)
    closure = default_source_closure() if source_closure is None else source_closure
    reused = {
        role: {
            "path": path,
            "sha256": closure.files[path],
        }
        for role, path in {
            "reason43": "tools/step5d_autotune_v4_r009/transport.py",
            "observability": "tools/step5d_autotune_v4_r009/observability.py",
            "early_abort": "tools/step5d_autotune_v4_r009/early_abort.py",
        }.items()
    }
    calibration_binding = {
        "path": Path(calibration_path).resolve().relative_to(ROOT.resolve()).as_posix(),
        "file_sha256": sha256_file(calibration_path),
        "calibration_sha256": active_calibration["calibration_sha256"],
        "noise_floor_n2": active_calibration["noise"]["selected_floor_n2"],
        "variance_estimator": VARIANCE_ESTIMATOR,
        "lengthscale_policy": active_calibration["lengthscales"]["policy"],
        "kernel_implementation_sha256": active_calibration["kernel"]["implementation_sha256"],
    }
    document = {
        "schema": BEHAVIOR_MANIFEST_SCHEMA,
        "version": BEHAVIOR_VERSION,
        "program": R010_PROGRAM,
        "lineage": R010_LINEAGE,
        "parent_r009_release_identity": parent_document,
        "parent_r009_release_identity_sha256": parent_sha,
        "source_closure": closure.as_dict(),
        "wave7_contact_entry": active_schedule.as_dict(),
        "gp_calibration": calibration_binding,
        "formal_objective": {
            "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
            "changed_from_r009": False,
        },
        "raw_codec": RAW_CODEC,
        "runtime_protocol": R010_RUNTIME_PROTOCOL,
        "runtime_composition": runtime_composition_manifest(),
        "optimizer": {
            "worker": "tools/step5d_autotune_v4_r010/optimizer_worker.py",
            "keepalive": "tools/step5d_autotune_v4_r010/optimizer_keepalive.py",
            "feature_map": "r008-log2-p-over-d-v1",
            "acquisition": "qLogNEI",
            "kernel_family": "conditional_matern52_shared_same_mode_i_on_only",
            "domain_changed": False,
            "historical_observations_imported": False,
        },
        "executable_behavior": {
            "baseline_ramp_changed": False,
            "anchor_force_gains_changed": False,
            "path_entry_limiter_changed": False,
            "early_abort": EARLY_ABORT_SIGMOID_SEMANTICS,
            "early_abort_active_rejected": True,
        },
        "reused_r009_components": reused,
        "analysis_sidecars": {
            "stars_ft_bias_shadow": {
                "campaign_identity_member": False,
                "gp_observation": False,
                "force_correction": False,
                "completion_certificate": False,
                "science_not_promoted": True,
            }
        },
    }
    return validate_behavior_manifest(document)


def validate_behavior_manifest(value: Mapping[str, Any]) -> BehaviorManifest:
    required = {
        "schema",
        "version",
        "program",
        "lineage",
        "parent_r009_release_identity",
        "parent_r009_release_identity_sha256",
        "source_closure",
        "wave7_contact_entry",
        "gp_calibration",
        "formal_objective",
        "raw_codec",
        "runtime_protocol",
        "runtime_composition",
        "optimizer",
        "executable_behavior",
        "reused_r009_components",
        "analysis_sidecars",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise R010IdentityError("R010 behavior manifest fields differ")
    if (
        value.get("schema") != BEHAVIOR_MANIFEST_SCHEMA
        or value.get("version") != BEHAVIOR_VERSION
        or value.get("program") != R010_PROGRAM
        or value.get("lineage") != R010_LINEAGE
        or value.get("runtime_protocol") != R010_RUNTIME_PROTOCOL
        or value.get("raw_codec") != RAW_CODEC
    ):
        raise R010IdentityError("R010 behavior identity differs")
    parent = value.get("parent_r009_release_identity")
    if not isinstance(parent, Mapping):
        raise R010IdentityError("R010 parent release identity is absent")
    parent_sha = require_digest(value.get("parent_r009_release_identity_sha256"), "parent R009 identity")
    from step5d_autotune_v4_r009.identity import validate_release_identity

    typed_parent = validate_release_identity(parent)
    if typed_parent.release_identity_sha256 != parent_sha:
        raise R010IdentityError("R010 parent R009 release identity digest differs")
    closure = SourceClosure.from_mapping(value["source_closure"])
    schedule = validate_wave7_schedule(value["wave7_contact_entry"])
    validate_runtime_composition(value["runtime_composition"])
    calibration = value.get("gp_calibration")
    if not isinstance(calibration, Mapping) or set(calibration) != {
        "path",
        "file_sha256",
        "calibration_sha256",
        "noise_floor_n2",
        "variance_estimator",
        "lengthscale_policy",
        "kernel_implementation_sha256",
    }:
        raise R010IdentityError("R010 calibration binding fields differ")
    for key in ("file_sha256", "calibration_sha256", "kernel_implementation_sha256"):
        require_digest(calibration[key], f"R010 calibration {key}")
    if calibration.get("variance_estimator") != VARIANCE_ESTIMATOR:
        raise R010IdentityError("R010 calibration variance estimator differs")
    if value.get("formal_objective") != {
        "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
        "changed_from_r009": False,
    }:
        raise R010IdentityError("R010 formal objective differs")
    optimizer = value.get("optimizer")
    if not isinstance(optimizer, Mapping) or optimizer.get("acquisition") != "qLogNEI" or optimizer.get("historical_observations_imported") is not False:
        raise R010IdentityError("R010 optimizer semantics differ")
    executable = value.get("executable_behavior")
    if not isinstance(executable, Mapping) or executable.get("early_abort_active_rejected") is not True:
        raise R010IdentityError("R010 early-abort boundary differs")
    sidecars = value.get("analysis_sidecars")
    if not isinstance(sidecars, Mapping) or sidecars.get("stars_ft_bias_shadow", {}).get("campaign_identity_member") is not False:
        raise R010IdentityError("STARS entered the R010 campaign identity")
    reused = value.get("reused_r009_components")
    if not isinstance(reused, Mapping):
        raise R010IdentityError("R010 reused-component bindings are absent")
    for binding in reused.values():
        if not isinstance(binding, Mapping) or closure.files.get(binding.get("path")) != binding.get("sha256"):
            raise R010IdentityError("R010 reused-component binding differs from closure")
    return BehaviorManifest(value)


@dataclass(frozen=True)
class ReleaseIdentity:
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", _freeze(_json_tree(self.raw)))

    def as_dict(self) -> dict[str, Any]:
        return _json_tree(self.raw)

    @property
    def release_identity_sha256(self) -> str:
        return sha256_bytes(canonical_bytes(self.raw))

    @property
    def campaign_fingerprint(self) -> str:
        return str(self.raw["campaign_fingerprint"])


def build_release_identity(
    *,
    behavior_manifest: BehaviorManifest,
    final_contract_sha256: str,
    controller_triplet_sha256: Mapping[str, str],
) -> ReleaseIdentity:
    triplet = {
        key: require_digest(controller_triplet_sha256.get(key), f"controller {key}")
        for key in ("script", "txt", "urp")
    }
    document = {
        "schema": RELEASE_IDENTITY_SCHEMA,
        "version": BEHAVIOR_VERSION,
        "program": R010_PROGRAM,
        "lineage": R010_LINEAGE,
        "campaign_fingerprint": behavior_manifest.campaign_fingerprint,
        "behavior_manifest_sha256": behavior_manifest.behavior_manifest_sha256,
        "final_contract_sha256": require_digest(final_contract_sha256, "final contract"),
        "source_closure_sha256": behavior_manifest.source_closure.sha256,
        "controller_triplet_sha256": triplet,
        "runtime_protocol_summary": {
            "protocol": R010_RUNTIME_PROTOCOL,
            "raw_codec": RAW_CODEC,
            "reason43_subtypes": [1, 2, 3],
        },
    }
    return validate_release_identity(document)


def validate_release_identity(value: Mapping[str, Any]) -> ReleaseIdentity:
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
        "runtime_protocol_summary",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise R010IdentityError("R010 release identity fields differ")
    if (
        value.get("schema") != RELEASE_IDENTITY_SCHEMA
        or value.get("version") != BEHAVIOR_VERSION
        or value.get("program") != R010_PROGRAM
        or value.get("lineage") != R010_LINEAGE
    ):
        raise R010IdentityError("R010 release identity program/lineage differs")
    for key in (
        "campaign_fingerprint",
        "behavior_manifest_sha256",
        "final_contract_sha256",
        "source_closure_sha256",
    ):
        require_digest(value[key], f"release {key}")
    triplet = value.get("controller_triplet_sha256")
    if not isinstance(triplet, Mapping) or set(triplet) != {"script", "txt", "urp"}:
        raise R010IdentityError("R010 release controller triplet differs")
    for key, digest in triplet.items():
        require_digest(digest, f"controller {key}")
    if value.get("runtime_protocol_summary") != {
        "protocol": R010_RUNTIME_PROTOCOL,
        "raw_codec": RAW_CODEC,
        "reason43_subtypes": [1, 2, 3],
    }:
        raise R010IdentityError("R010 release runtime protocol summary differs")
    return ReleaseIdentity(value)


__all__ = [
    "BEHAVIOR_MANIFEST_SCHEMA",
    "BEHAVIOR_VERSION",
    "BehaviorManifest",
    "DEFAULT_CALIBRATION_PATH",
    "DEFAULT_SCHEDULE_PATH",
    "DEFAULT_SOURCE_PATHS",
    "RAW_CODEC",
    "R010IdentityError",
    "R010_LINEAGE",
    "RELEASE_IDENTITY_SCHEMA",
    "ReleaseIdentity",
    "SOURCE_CLOSURE_SCHEMA",
    "SourceClosure",
    "build_behavior_manifest",
    "build_release_identity",
    "canonical_bytes",
    "default_source_closure",
    "require_digest",
    "sha256_bytes",
    "sha256_file",
    "validate_behavior_manifest",
    "validate_release_identity",
]
