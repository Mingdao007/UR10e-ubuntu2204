"""Immutable R011 behavior manifest, source closure, and release identity."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .behavior import (
    DEFAULT_MANUAL_WAVE,
    R011_LINEAGE,
    R011_PROGRAM,
    R011_RUNTIME_PROTOCOL,
    validate_manual_wave,
)
from .common import (
    R011ValueError,
    canonical_bytes,
    digest,
    freeze_tree,
    json_tree,
    require_digest,
    sha256_bytes,
    strict_json_object,
)
from .noise import CALIBRATION_GRID_N2, NOISE_FLOOR_N2, NOISE_POLICY_VERSION, VARIANCE_ESTIMATOR
from .qlognei import ProductionQLogNEIBinding
from .safety_filter import FILTER_DT_S, SafetyFilterConfig
from .censor import CENSOR_SCHEMA, GUARD_FRACTION, KAPPA_END, KAPPA_MIDPOINT, KAPPA_START, KAPPA_STEEPNESS, CensorProtocol
from .runtime_composition import runtime_composition_manifest, validate_runtime_composition


ROOT = Path(__file__).resolve().parents[2]
BEHAVIOR_VERSION = "r011-v1"
BEHAVIOR_MANIFEST_SCHEMA = "step5d.autotune-v4/r011-behavior-manifest-v1"
SOURCE_CLOSURE_SCHEMA = "step5d.autotune-v4/r011-source-closure-v1"
RELEASE_IDENTITY_SCHEMA = "step5d.autotune-v4/r011-release-identity-v1"
DEFAULT_WAVE_PATH = ROOT / "config/step5d/autotune_v4_r011_wave_schedule.json"
DEFAULT_NOISE_PATH = ROOT / "config/step5d/autotune_v4_r011_observation_noise.json"
DEFAULT_CENSOR_PATH = ROOT / "config/step5d/autotune_v4_r011_censoring.json"
DEFAULT_SAFETY_PATH = ROOT / "config/step5d/autotune_v4_r011_safety_filter.json"
DEFAULT_R010_IDENTITY_PATH = ROOT / "config/step5d/autotune_v4_r010.release-identity.json"

GENERATED_PATHS = frozenset(
    {
        "config/step5d/autotune_v4_r011.behavior-manifest.json",
        "config/step5d/autotune_v4_r011.json",
        "config/step5d/autotune_v4_r011.release-identity.json",
        "config/step5d/autotune_v4_r011.observation-binding.json",
        "config/step5d/r011_release_identity.json",
        "config/step5d/autotune_v4_r011_offline_closure.json",
        "config/step5d/autotune_v4_r011_empty_ledger.jsonl",
    }
)

DEFAULT_SOURCE_PATHS = (
    "tools/step5d_autotune_v4_r011/__init__.py",
    "tools/step5d_autotune_v4_r011/common.py",
    "tools/step5d_autotune_v4_r011/behavior.py",
    "tools/step5d_autotune_v4_r011/wave.py",
    "tools/step5d_autotune_v4_r011/noise.py",
    "tools/step5d_autotune_v4_r011/censor.py",
    "tools/step5d_autotune_v4_r011/async_ts.py",
    "tools/step5d_autotune_v4_r011/safety_filter.py",
    "tools/step5d_autotune_v4_r011/qlognei.py",
    "tools/step5d_autotune_v4_r011/runtime_composition.py",
    "tools/step5d_autotune_v4_r011/identity.py",
    "tools/step5d_autotune_v4_r011/contracts.py",
    "tools/step5d_autotune_v4_r011/ledger.py",
    "config/step5d/autotune_v4_r011_wave_schedule.json",
    "config/step5d/autotune_v4_r011_observation_noise.json",
    "config/step5d/autotune_v4_r011_censoring.json",
    "config/step5d/autotune_v4_r011_safety_filter.json",
    # Complete validated R010 behavior/runtime closure, including its direct
    # R008/R009 dependencies. STARS remains an excluded analysis sidecar.
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
    "tools/step5d_autotune_v4_r009/early_abort.py",
    "tools/step5d_autotune_v4_r009/observability.py",
    "tools/step5d_autotune_v4_r009/observer.py",
    "tools/step5d_autotune_v4_r009/freshness.py",
    "tools/step5d_autotune_v4_r009/transport.py",
    "tools/step5d_autotune_v4_r009/fake_rtde.py",
    "tools/step5d_autotune_v4_r009/diagnostics.py",
    "tools/step5d_autotune_v4_r009/tp.py",
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
    "tools/build_step5d_autotune_v4_r011.py",
)


class R011IdentityError(R011ValueError):
    """R011 identity or closure is invalid."""


def _relative(value: str | Path) -> str:
    path = PurePosixPath(Path(value).as_posix())
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise R011IdentityError(f"unsafe R011 source path: {value}")
    return path.as_posix()


def sha256_file(path: Path) -> str:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise R011IdentityError(f"source is missing or unsafe: {path}")
    return sha256_bytes(path.read_bytes())


@dataclass(frozen=True)
class SourceClosure:
    files: Mapping[str, str]
    schema: str = SOURCE_CLOSURE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != SOURCE_CLOSURE_SCHEMA or not isinstance(self.files, Mapping) or not self.files:
            raise R011IdentityError("R011 source closure differs")
        normalized: dict[str, str] = {}
        for path, file_digest in self.files.items():
            relative = _relative(path)
            if relative in GENERATED_PATHS or relative.startswith("tools/stars_ft_bias_shadow/"):
                raise R011IdentityError("generated/STARS sidecar entered R011 source closure")
            normalized[relative] = require_digest(file_digest, f"source {relative}")
        object.__setattr__(self, "files", freeze_tree(dict(sorted(normalized.items()))))

    @classmethod
    def from_files(cls, root: Path, paths: Sequence[str | Path]) -> "SourceClosure":
        root = Path(root).resolve()
        rows = {}
        for value in paths:
            relative = _relative(value)
            rows[relative] = sha256_file(root / relative)
        return cls(rows)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceClosure":
        if not isinstance(value, Mapping) or set(value) != {"schema", "files", "sha256"}:
            raise R011IdentityError("R011 source closure fields differ")
        closure = cls(value["files"], schema=value["schema"])
        if value["sha256"] != closure.sha256:
            raise R011IdentityError("R011 source closure digest differs")
        return closure

    @property
    def sha256(self) -> str:
        return digest({"schema": self.schema, "files": dict(self.files)})

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "files": dict(self.files), "sha256": self.sha256}

    def verify_files(self, root: Path = ROOT) -> None:
        for relative, expected in self.files.items():
            if sha256_file(Path(root) / relative) != expected:
                raise R011IdentityError(f"R011 source closure bytes differ: {relative}")


def default_source_closure(root: Path = ROOT) -> SourceClosure:
    return SourceClosure.from_files(root, DEFAULT_SOURCE_PATHS)


@dataclass(frozen=True)
class BehaviorManifest:
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", freeze_tree(json_tree(self.raw)))

    def as_dict(self) -> dict[str, Any]:
        return json_tree(self.raw)

    @property
    def behavior_manifest_sha256(self) -> str:
        return digest(self.raw)

    @property
    def campaign_fingerprint(self) -> str:
        return self.behavior_manifest_sha256

    @property
    def source_closure(self) -> SourceClosure:
        return SourceClosure.from_mapping(self.raw["source_closure"])


def _r010_parent(value: Any | None) -> dict[str, Any]:
    if value is None:
        value = strict_json_object(DEFAULT_R010_IDENTITY_PATH, "R010 release identity")
    if hasattr(value, "as_dict"):
        value = value.as_dict()
    if not isinstance(value, Mapping):
        raise R011IdentityError("R011 parent must be the validated R010 release identity")
    required = {"schema", "version", "program", "lineage", "campaign_fingerprint", "behavior_manifest_sha256", "final_contract_sha256", "source_closure_sha256", "controller_triplet_sha256", "runtime_protocol_summary"}
    if set(value) != required or "r010" not in str(value.get("schema", "")).lower():
        raise R011IdentityError("R011 parent identity is not R010")
    try:
        from step5d_autotune_v4_r010.identity import validate_release_identity as validate_r010_identity
        from step5d_autotune_v4_r010.contracts import load_contract as load_r010_contract

        parent = validate_r010_identity(value).as_dict()
        contract = load_r010_contract()
        if contract.release_identity.as_dict() != parent:
            raise R011IdentityError("R010 parent identity is not the validated contract identity")
        for relative, expected in contract.behavior_manifest.raw["source_closure"]["files"].items():
            if sha256_file(ROOT / relative) != expected:
                raise R011IdentityError(f"R010 parent source closure bytes differ: {relative}")
    except (ImportError, KeyError, TypeError, ValueError, OSError) as exc:
        raise R011IdentityError("R011 parent R010 release identity is not valid") from exc
    for key in ("campaign_fingerprint", "behavior_manifest_sha256", "final_contract_sha256", "source_closure_sha256"):
        require_digest(parent[key], f"parent R010 {key}")
    triplet = parent.get("controller_triplet_sha256")
    if not isinstance(triplet, Mapping) or set(triplet) != {"script", "txt", "urp"}:
        raise R011IdentityError("R010 parent controller triplet differs")
    for key in triplet:
        require_digest(triplet[key], f"parent R010 controller {key}")
    return parent


def build_behavior_manifest(
    *,
    parent_r010: Any | None = None,
    source_closure: SourceClosure | None = None,
    schedule: Mapping[str, Any] | None = None,
    controller_triplet_sha256: Mapping[str, str] | None = None,
    wave_path: Path = DEFAULT_WAVE_PATH,
    noise_path: Path = DEFAULT_NOISE_PATH,
    censor_path: Path = DEFAULT_CENSOR_PATH,
    safety_path: Path = DEFAULT_SAFETY_PATH,
) -> BehaviorManifest:
    parent = _r010_parent(parent_r010)
    active_schedule = validate_manual_wave(
        strict_json_object(wave_path, "R011 manual Wave schedule") if schedule is None else schedule
    )
    closure = default_source_closure() if source_closure is None else source_closure
    parent_triplet = parent["controller_triplet_sha256"]
    triplet = dict(parent_triplet if controller_triplet_sha256 is None else controller_triplet_sha256)
    if set(triplet) != {"script", "txt", "urp"}:
        raise R011IdentityError("R011 controller triplet fields differ")
    for key in triplet:
        require_digest(triplet[key], f"R011 controller {key}")
    # The three small policy files are byte-bound source inputs; their contents
    # are validated when the release is assembled, not during live operation.
    for path, role in ((noise_path, "noise policy"), (censor_path, "censor policy"), (safety_path, "safety policy")):
        strict_json_object(path, role)
    qlognei = ProductionQLogNEIBinding("tools/step5d_autotune_v4_r010/optimizer_worker.py")
    from step5d_autotune_v4_r010.contracts import load_contract as load_r010_contract
    parent_contract = load_r010_contract()
    parent_manifest = parent_contract.behavior_manifest.raw
    for relative, expected in parent_manifest["source_closure"]["files"].items():
        if closure.files.get(relative) != expected:
            raise R011IdentityError(f"R011 closure omits or changes validated R010 byte: {relative}")
    reuse_paths = {
        "kernel": "tools/step5d_autotune_v4_r010/kernel.py",
        "feature_map": "tools/step5d_autotune_v4_r010/optimizer_worker.py",
        "objective": "tools/step5d_force_objective.py",
        "raw_codec": "tools/step5d_autotune_v4_r008/raw_force_binary_v2.py",
        "qlognei_worker": "tools/step5d_autotune_v4_r010/optimizer_worker.py",
        "qlognei_loop": "tools/step5d_autotune_v4_r010/optimizer_worker_loop.py",
        "qlognei_keepalive": "tools/step5d_autotune_v4_r010/optimizer_keepalive.py",
        "hard_safety": "tools/step5d_autotune_v4_r010/runtime_composition.py",
        "reason43": "tools/step5d_autotune_v4_r009/transport.py",
        "observability": "tools/step5d_autotune_v4_r009/observability.py",
    }
    r010_reuse = {
        "formal_objective_semantic_fingerprint": parent_manifest["formal_objective"]["semantic_fingerprint"],
        "raw_codec": parent_manifest["raw_codec"],
        "feature_map": parent_manifest["optimizer"]["feature_map"],
        "kernel_family": parent_manifest["optimizer"]["kernel_family"],
        "paths": {name: {"path": path, "sha256": closure.files.get(path)} for name, path in reuse_paths.items()},
        "hard_safety_reason43_observability_unchanged": True,
    }
    document = {
        "schema": BEHAVIOR_MANIFEST_SCHEMA,
        "version": BEHAVIOR_VERSION,
        "program": R011_PROGRAM,
        "lineage": R011_LINEAGE,
        "parent_r010_release_identity": parent,
        "parent_r010_release_identity_sha256": digest(parent),
        "source_closure": closure.as_dict(),
        "controller_triplet_sha256": triplet,
        "controller_triplet_provenance": {"sha256": triplet, "source": "R010_parent_wire_compatibility_only", "r011_built": False, "r011_readback": False, "r011_deployed": False, "live_blocked": True},
        "manual_wave": active_schedule.as_dict(),
        "r010_reuse": r010_reuse,
        "observation_noise": {
            "schema": "step5d.autotune-v4/r011-observation-noise-v1",
            "policy_version": NOISE_POLICY_VERSION,
            "noise_floor_n2": NOISE_FLOOR_N2,
            "calibration_grid_n2": list(CALIBRATION_GRID_N2),
            "variance_estimator": VARIANCE_ESTIMATOR,
            "strata": "kind x campaign_epoch",
            "small_sample": "deterministic_log_space_shrink_and_backoff",
            "freeze": {"full_observations": 30, "repeat_groups": 8, "consecutive_refits": 2, "lengthscale_change": 0.20, "noise_change": 0.25},
            "i_axes": "not_identifiable_until_five_fold_varying_support",
        },
        "production_optimizer": {
            **qlognei.as_dict(),
            "historical_observations_imported": False,
            "reused_r010_source_sha256": closure.files.get("tools/step5d_autotune_v4_r010/optimizer_worker.py"),
        },
        "censored_observations": {
            "schema": CENSOR_SCHEMA,
            "denominator_bins": CensorProtocol().denominator_bins,
            "watermark_unit": "seconds",
            "kappa_semantics": {"start": KAPPA_START, "end": KAPPA_END, "guard_fraction": GUARD_FRACTION, "midpoint": KAPPA_MIDPOINT, "steepness": KAPPA_STEEPNESS, "decreasing_with_post_guard_progress": True},
            "lower_bound_semantics": "r009_causal_550_bin",
            "active_early_abort_allowed": False,
            "first_release_mode": "shadow_only",
        },
        "theory_shadow_async_ts": {
            "schema": "step5d.autotune-v4/r011-theory-shadow-async-ts-v3",
            "epsilon": "min(0.2,n^-0.5)",
            "exploration": "uniform_admissible_finite_lattice_forces_full_evaluation",
            "completed_only": True,
            "pending_excluded": True,
            "hypothetical_censoring": True,
            "joint_posterior_sampling": True,
            "common_release_identity_required": True,
            "completed_observation_set_digest": True,
            "production_authority": False,
        },
        "safety_filter": {
            **SafetyFilterConfig().as_dict(),
            "uncertainty_tightening": "tracking_plus_latency_plus_frame_componentwise",
            "constraint": "exact_one_step_tightened_ellipsoid",
            "hard_safety_independent": True,
        },
        "runtime_composition": runtime_composition_manifest(),
        "stars_admission": {
            "schema": "step5d.autotune-v4/r011-stars-idle-admission-v1",
            "sidecar": "stars_ft_bias_shadow/overnight_v0",
            "explicit_batch_replay_only": True,
            "requires": ["no_live_writer_lease", "no_active_attempt", "all_inputs_sealed", "no_gpu", "no_concurrent_worker", "one_worker", "one_cpu", "nice_19", "idle_io"],
            "auto_launch": False,
            "campaign_identity_member": False,
            "gp_observation": False,
            "force_correction": False,
            "completion_certificate": False,
        },
        "offline_boundary": {
            "network": False, "controller_upload": False, "controller_readback": False,
            "dashboard": False, "load": False, "play": False, "bridge": False,
            "motion": False, "contact": False, "sensor_write": False,
            "current_pointer_switch": False, "live_process": False, "live_evidence": False,
        },
    }
    return validate_behavior_manifest(document)


def validate_behavior_manifest(value: Mapping[str, Any]) -> BehaviorManifest:
    required = {
        "schema", "version", "program", "lineage", "parent_r010_release_identity",
        "parent_r010_release_identity_sha256", "source_closure", "controller_triplet_sha256", "controller_triplet_provenance", "r010_reuse",
        "manual_wave", "observation_noise", "production_optimizer", "censored_observations",
        "theory_shadow_async_ts", "safety_filter", "runtime_composition", "stars_admission", "offline_boundary",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise R011IdentityError("R011 behavior manifest fields differ")
    if value.get("schema") != BEHAVIOR_MANIFEST_SCHEMA or value.get("version") != BEHAVIOR_VERSION or value.get("program") != R011_PROGRAM or value.get("lineage") != R011_LINEAGE:
        raise R011IdentityError("R011 behavior identity differs")
    parent = _r010_parent(value["parent_r010_release_identity"])
    if value.get("parent_r010_release_identity_sha256") != digest(parent):
        raise R011IdentityError("R011 parent identity digest differs")
    closure = SourceClosure.from_mapping(value["source_closure"])
    triplet = value["controller_triplet_sha256"]
    if not isinstance(triplet, Mapping) or set(triplet) != {"script", "txt", "urp"}:
        raise R011IdentityError("R011 controller triplet differs")
    for key in triplet:
        require_digest(triplet[key], f"R011 controller {key}")
    provenance = value["controller_triplet_provenance"]
    if provenance.get("sha256") != triplet or provenance.get("source") != "R010_parent_wire_compatibility_only" or any(provenance.get(key) is not False for key in ("r011_built", "r011_readback", "r011_deployed")) or provenance.get("live_blocked") is not True:
        raise R011IdentityError("R011 controller triplet provenance incorrectly implies deployment")
    reuse = value["r010_reuse"]
    if not isinstance(reuse.get("formal_objective_semantic_fingerprint"), str):
        raise R011IdentityError("R010 formal objective reuse fingerprint is invalid")
    if reuse.get("raw_codec") != "r009raw_v1" or not reuse.get("hard_safety_reason43_observability_unchanged"):
        raise R011IdentityError("R010 raw/runtime reuse binding differs")
    try:
        from step5d_autotune_v4_r010.contracts import load_contract as load_r010_contract
        parent_manifest = load_r010_contract().behavior_manifest.raw
    except Exception as exc:
        raise R011IdentityError("validated R010 parent contract is unavailable") from exc
    if reuse.get("formal_objective_semantic_fingerprint") != parent_manifest["formal_objective"]["semantic_fingerprint"] or reuse.get("raw_codec") != parent_manifest["raw_codec"] or reuse.get("feature_map") != parent_manifest["optimizer"]["feature_map"] or reuse.get("kernel_family") != parent_manifest["optimizer"]["kernel_family"]:
        raise R011IdentityError("R010 formal objective/kernel/feature reuse binding differs")
    for relative, expected in parent_manifest["source_closure"]["files"].items():
        if closure.files.get(relative) != expected:
            raise R011IdentityError(f"R011 source closure omits validated R010 byte: {relative}")
    if not isinstance(reuse.get("paths"), Mapping): raise R011IdentityError("R010 transitive runtime paths are absent")
    for item in reuse["paths"].values():
        if not isinstance(item, Mapping) or item.get("path") not in closure.files or item.get("sha256") != closure.files[item["path"]]:
            raise R011IdentityError("R010 transitive runtime byte is not closure-bound")
    validate_manual_wave(value["manual_wave"])
    noise = value["observation_noise"]
    if noise.get("noise_floor_n2") != NOISE_FLOOR_N2 or noise.get("variance_estimator") != VARIANCE_ESTIMATOR or noise.get("strata") != "kind x campaign_epoch":
        raise R011IdentityError("R011 hierarchical noise semantics differ")
    optimizer = value["production_optimizer"]
    if optimizer.get("acquisition") != "qLogNEI" or optimizer.get("censored_observations_allowed") is not False or optimizer.get("theory_shadow_separate") is not True:
        raise R011IdentityError("R011 production qLogNEI binding differs")
    censor = value["censored_observations"]
    if censor.get("denominator_bins") != 550 or censor.get("watermark_unit") != "seconds" or censor.get("kappa_semantics") != {"start": KAPPA_START, "end": KAPPA_END, "guard_fraction": GUARD_FRACTION, "midpoint": KAPPA_MIDPOINT, "steepness": KAPPA_STEEPNESS, "decreasing_with_post_guard_progress": True} or censor.get("active_early_abort_allowed") is not False or censor.get("first_release_mode") != "shadow_only":
        raise R011IdentityError("R011 censor protocol differs")
    theory = value["theory_shadow_async_ts"]
    if theory.get("completed_only") is not True or theory.get("pending_excluded") is not True or theory.get("production_authority") is not False or theory.get("joint_posterior_sampling") is not True or theory.get("common_release_identity_required") is not True or theory.get("completed_observation_set_digest") is not True:
        raise R011IdentityError("R011 theory shadow boundary differs")
    safety = value["safety_filter"]
    if safety.get("dt_s") != FILTER_DT_S or safety.get("force_cbf_claim") is not False or safety.get("constraint") != "exact_one_step_tightened_ellipsoid":
        raise R011IdentityError("R011 safety filter binding differs")
    validate_runtime_composition(value["runtime_composition"])
    stars = value["stars_admission"]
    if any(stars.get(key) is not False for key in ("campaign_identity_member", "gp_observation", "force_correction", "completion_certificate")) or stars.get("auto_launch") is not False:
        raise R011IdentityError("STARS entered R011 authority")
    boundary = value["offline_boundary"]
    if not isinstance(boundary, Mapping) or any(boundary.values()):
        raise R011IdentityError("R011 offline boundary is not fail-closed")
    return BehaviorManifest(value)


@dataclass(frozen=True)
class ReleaseIdentity:
    raw: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw", freeze_tree(json_tree(self.raw)))

    def as_dict(self) -> dict[str, Any]:
        return json_tree(self.raw)

    @property
    def release_identity_sha256(self) -> str:
        return digest(self.raw)

    @property
    def campaign_fingerprint(self) -> str:
        return str(self.raw["campaign_fingerprint"])


def build_release_identity(*, behavior_manifest: BehaviorManifest, final_contract_sha256: str, controller_triplet_sha256: Mapping[str, str]) -> ReleaseIdentity:
    if not isinstance(behavior_manifest, BehaviorManifest):
        raise R011IdentityError("release identity requires a typed behavior manifest")
    triplet = {key: require_digest(controller_triplet_sha256.get(key), f"R011 controller {key}") for key in ("script", "txt", "urp")}
    document = {
        "schema": RELEASE_IDENTITY_SCHEMA,
        "version": BEHAVIOR_VERSION,
        "program": R011_PROGRAM,
        "lineage": R011_LINEAGE,
        "campaign_fingerprint": behavior_manifest.campaign_fingerprint,
        "behavior_manifest_sha256": behavior_manifest.behavior_manifest_sha256,
        "final_contract_sha256": require_digest(final_contract_sha256, "R011 final contract"),
        "source_closure_sha256": behavior_manifest.source_closure.sha256,
        "controller_triplet_sha256": triplet,
        "controller_triplet_provenance": behavior_manifest.raw["controller_triplet_provenance"],
        "parent_r010_release_identity_sha256": behavior_manifest.raw["parent_r010_release_identity_sha256"],
        "runtime_protocol_summary": {"protocol": R011_RUNTIME_PROTOCOL, "raw_codec": "r009raw_v1", "reason43_subtypes": [1, 2, 3]},
    }
    return validate_release_identity(document)


def validate_release_identity(value: Mapping[str, Any]) -> ReleaseIdentity:
    required = {
        "schema", "version", "program", "lineage", "campaign_fingerprint", "behavior_manifest_sha256",
        "final_contract_sha256", "source_closure_sha256", "controller_triplet_sha256",
        "controller_triplet_provenance", "parent_r010_release_identity_sha256", "runtime_protocol_summary",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise R011IdentityError("R011 release identity fields differ")
    if value.get("schema") != RELEASE_IDENTITY_SCHEMA or value.get("version") != BEHAVIOR_VERSION or value.get("program") != R011_PROGRAM or value.get("lineage") != R011_LINEAGE:
        raise R011IdentityError("R011 release identity program/lineage differs")
    for key in ("campaign_fingerprint", "behavior_manifest_sha256", "final_contract_sha256", "source_closure_sha256", "parent_r010_release_identity_sha256"):
        require_digest(value[key], f"R011 release {key}")
    triplet = value["controller_triplet_sha256"]
    if not isinstance(triplet, Mapping) or set(triplet) != {"script", "txt", "urp"}:
        raise R011IdentityError("R011 release controller triplet differs")
    for key in triplet:
        require_digest(triplet[key], f"R011 release controller {key}")
    provenance = value["controller_triplet_provenance"]
    if provenance.get("sha256") != triplet or provenance.get("source") != "R010_parent_wire_compatibility_only" or any(provenance.get(key) is not False for key in ("r011_built", "r011_readback", "r011_deployed")) or provenance.get("live_blocked") is not True:
        raise R011IdentityError("R011 release controller triplet provenance differs")
    if value.get("runtime_protocol_summary") != {"protocol": R011_RUNTIME_PROTOCOL, "raw_codec": "r009raw_v1", "reason43_subtypes": [1, 2, 3]}:
        raise R011IdentityError("R011 runtime protocol summary differs")
    return ReleaseIdentity(value)


__all__ = [
    "BEHAVIOR_MANIFEST_SCHEMA", "BEHAVIOR_VERSION", "BehaviorManifest", "DEFAULT_SOURCE_PATHS",
    "DEFAULT_WAVE_PATH", "R011IdentityError", "RELEASE_IDENTITY_SCHEMA", "ReleaseIdentity",
    "SOURCE_CLOSURE_SCHEMA", "SourceClosure", "build_behavior_manifest", "build_release_identity",
    "canonical_bytes", "default_source_closure", "digest", "sha256_file", "validate_behavior_manifest",
    "validate_release_identity",
]
