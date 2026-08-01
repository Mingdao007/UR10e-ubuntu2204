"""Offline-only qualification contracts for the frozen TacDiffusion fixture.

This module has two deliberately separate surfaces:

* the acyclic, hash-bound fixture/source/checkpoint contract; and
* a bounded inference diagnostic loop which can never select a formal rate.

The CUDA execution surface is opt-in.  The default and gate-deferred paths do
not query CUDA, create a checkpoint, or claim that a model was executed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import time
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .mainline_model import (
    MainlineModelConfig,
    evaluate_mainline_checkpoint,
    load_mainline_checkpoint,
    resume_mainline_model,
    train_mainline_model,
    torch,
)


QUALIFICATION_SCHEMA = "ur10e_tacdiffusion_offline_model_qualification/v1"
CONTRACT_SCHEMA = "ur10e_tacdiffusion_offline_model_contract/v1"
CHECKPOINT_BINDING_SCHEMA = "ur10e_tacdiffusion_offline_checkpoint_binding/v1"
DIAGNOSTIC_SCHEMA = "ur10e_tacdiffusion_offline_rate_diagnostic/v1"
K800_SCHEMA = "ur10e_tacdiffusion_inactive_k800_candidate/v1"
NUMERIC_SANITY_SCHEMA = "ur10e_tacdiffusion_inactive_k800_numeric_sanity/v1"

CONTRACT_ARTIFACT_NAME = "qualification.contract.json"
DEFERRED_ARTIFACT_NAME = "qualification.gate-deferred.json"
CUDA_ARTIFACT_NAME = "qualification.cuda.json"
EVALUATION_ARTIFACT_NAME = "evaluation.json"
DIAGNOSTICS_ARTIFACT_NAME = "diagnostics.json"
INITIAL_CHECKPOINT_NAME = "fixture_checkpoint.initial.pt"
RESUMED_CHECKPOINT_NAME = "fixture_checkpoint.resumed.pt"

DIAGNOSTIC_KIND_RUNTIME = "runtime_timing"
DIAGNOSTIC_KIND_POLICY_PROBE = "deterministic_lifecycle_policy_probe"
POLICY_PROBE_LABEL = "deterministic lifecycle-policy probe; not latency performance"
EXPECTED_UNCHANGED_IDENTITIES = (
    "friction",
    "path",
    "guards",
    "limits",
    "routing",
    "source_identity",
)
EXPECTED_DAMPING_DERIVATION = "2*sqrt(stiffness*virtual_mass), source formula unchanged"
EXPECTED_NUMERIC_DAMPING_DERIVATION = "2*sqrt(stiffness*virtual_mass)"

DEFERRED_OUTPUT_NAMES = frozenset({CONTRACT_ARTIFACT_NAME, DEFERRED_ARTIFACT_NAME})
CUDA_OUTPUT_NAMES = frozenset(
    {
        CONTRACT_ARTIFACT_NAME,
        CUDA_ARTIFACT_NAME,
        INITIAL_CHECKPOINT_NAME,
        RESUMED_CHECKPOINT_NAME,
        EVALUATION_ARTIFACT_NAME,
        DIAGNOSTICS_ARTIFACT_NAME,
    }
)

CONTRACT_FIELDS = frozenset(
    {
        "schema",
        "contract",
        "contract_sha256",
        "fixture_binding",
        "code_source_hashes",
        "device_policy",
        "checkpoint_policy",
        "artifact_sha256",
    }
)
DEFERRED_FIELDS = frozenset(
    {
        "schema",
        "status",
        "cuda_execution_status",
        "fixture_only",
        "production_promotion_allowed",
        "formal_checkpoint",
        "active_allowed",
        "active",
        "reproduction_status",
        "model_rate_selected_hz",
        "selection_eligible",
        "selected_rate_hz",
        "formal_rate_selection",
        "qualification_executed",
        "no_fake_checkpoint",
        "checkpoint_artifacts",
        "evaluation_artifacts",
        "sampler_smoke",
        "diagnostics",
        "fixture_binding",
        "contract_artifact",
        "contract_artifact_sha256",
        "code_source_hashes",
        "artifact_sha256",
    }
)
DIAGNOSTIC_FIELDS = frozenset(
    {
        "schema",
        "diagnostic_only",
        "diagnostic_kind",
        "latency_performance_claim",
        "probe_label",
        "rates_hz",
        "warmup_excluded",
        "checkpoint_sha256",
        "dataset_sha256",
        "source_receipt_sha256",
        "code_source_hashes",
        "sampler_trace",
        "sampler_steps",
        "selection_eligible",
        "selected_rate_hz",
        "formal_rate_selection",
        "active_allowed",
        "active",
        "fixture_only",
        "production_promotion_allowed",
        "rate_results",
        "policy",
        "lifecycle_policy_probe",
        "artifact_sha256",
    }
)
RATE_RESULT_FIELDS = frozenset(
    {
        "rate_hz",
        "period_s",
        "deadline_s",
        "warmup_samples",
        "steady_samples",
        "steady_sample_count",
        "deadline_misses",
        "deadline_met_count",
        "stale_action_transition",
        "stale_action_transition_index",
        "governed_stop",
        "governed_stop_reason",
    }
)
WARMUP_ROW_FIELDS = frozenset(
    {"index", "release_s", "start_s", "end_s", "latency_s", "deadline_class", "action", "action_finite"}
)
STEADY_ROW_FIELDS = frozenset(
    {
        "index",
        "release_s",
        "start_s",
        "end_s",
        "latency_s",
        "deadline_s",
        "deadline_class",
        "action_state",
        "action",
        "action_finite",
        "prediction_error",
        "consecutive_deadline_misses",
        "consecutive_stale_ticks",
    }
)
CUDA_REPORT_FIELDS = frozenset(
    {
        "schema",
        "status",
        "cuda_execution_status",
        "fixture_only",
        "production_promotion_allowed",
        "formal_checkpoint",
        "active_allowed",
        "active",
        "reproduction_status",
        "model_rate_selected_hz",
        "selection_eligible",
        "selected_rate_hz",
        "formal_rate_selection",
        "qualification_executed",
        "no_cpu_fallback",
        "execution",
        "fixture_binding",
        "code_source_hashes",
        "contract_artifact",
        "contract_artifact_sha256",
        "checkpoint_artifacts",
        "evaluation_artifacts",
        "diagnostic_artifacts",
        "sampler_smoke",
        "artifact_sha256",
    }
)
EXECUTION_FIELDS = frozenset(
    {
        "environment_threads",
        "torch_num_threads",
        "torch_num_interop_threads",
        "deterministic_algorithms_requested",
        "deterministic_algorithms_enabled",
        "deterministic_warn_only",
        "device",
        "device_string",
        "unavoidable_cuda_nondeterminism",
    }
)
DEVICE_FIELDS = frozenset(
    {"backend", "device_index", "device_name", "compute_capability", "total_memory_bytes", "torch_version", "platform"}
)
CHECKPOINT_RECEIPT_BASE_FIELDS = frozenset(
    {
        "schema",
        "stage",
        "checkpoint_sha256",
        "checkpoint_path",
        "fixture_binding",
        "source_receipt_sha256",
        "split_sha256",
        "code_source_hashes",
        "configuration",
        "device",
        "fixture_only",
        "formal_checkpoint",
        "active_allowed",
        "active",
        "reproduction_status",
        "model_rate_selected_hz",
        "memory",
        "artifact_sha256",
    }
)
EVALUATION_FIELDS = frozenset(
    {
        "schema",
        "checkpoint_path",
        "split",
        "sample_count",
        "diffusion_steps",
        "distinct_reverse_steps",
        "reverse_step_trace",
        "normalized_action_mse",
        "device",
        "require_cuda",
        "checkpoint_binding",
        "fixture_only",
        "formal_checkpoint",
        "active_allowed",
        "active",
        "reproduction_status",
        "model_rate_selected_hz",
        "memory",
        "checkpoint_sha256",
        "dataset_sha256",
        "source_receipt_sha256",
        "code_source_hashes",
        "artifact_sha256",
    }
)

CAMPAIGN_BUNDLE_SCHEMA = "ur10e_tacdiffusion_offline_fixture_bundle/v1"
CAMPAIGN_DATASET_SCHEMA = "ur10e_tacdiffusion_offline_fixture_dataset/v1"
EXPECTED_BUNDLE_DIGEST = "27b5908f4e111798d75682836bfed44f928ed90f3c7d754213f2ee96f28ad956"
EXPECTED_DATASET_MANIFEST_SHA256 = "5f4c72b023b9f401ec89fa81ce203fa8330c86e99fe7d1e61f6434f118b3d172"
EXPECTED_DATASET_SHA256 = "d6dab167520e50895247417a040a4bf050d5ba70415366fbad57231d063ccc99"
EXPECTED_SOURCE_RECEIPT_SHA256 = "4aa33d04613f0ec7d44e0e0d350a0bda5c623b9973f62325e08a5a5bb2fadc07"
EXPECTED_SPLIT_SHA256 = "ec6f02aab5a2fdad38ab8240bb42d345e070e64e7f801073d5d45d2567ea1b6e"

OBSERVATION_DIMENSION = 84
ACTION_DIMENSION = 12
ROW_COUNT = 84
EPISODE_GROUP_COUNT = 42
DIAGNOSTIC_RATES_HZ = (50, 100)
REVERSE_STEP_TRACE = tuple(range(49, -1, -1))
CUDA_GATE_DEFERRED = "gate_closed_external_deferred"
CUDA_GATE_OPEN = "gate_open_external_authorized"
K600_SOURCE_RELATIVE_PATH = "experiments/tase-contact-reproduction/programs/step5/step5d_tacdiffusion/step5d_tacdiffusion_direct_torque_fixture_shadow_v1.script"
EXPECTED_K600_SOURCE_SHA256 = "4c21c13826f25b5b61e44663f80cbf0da2ec5484b1d6209b82d850398fe3f4ec"
K600_STIFFNESS = (600.0, 600.0, 600.0, 30.0, 30.0, 30.0)
K800_STIFFNESS = (800.0, 800.0, 800.0, 30.0, 30.0, 30.0)
K600_DAMPING = (69.2820323, 69.2820323, 69.2820323, 4.898979486, 4.898979486, 4.898979486)
K800_DAMPING = (80.0, 80.0, 80.0, 4.898979486, 4.898979486, 4.898979486)

EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_CAMPAIGN_ROOT = EXPERIMENT_ROOT / "evidence" / "tacdiffusion_offline_fixture_campaign_v1"
DEFAULT_QUALIFICATION_ROOT = EXPERIMENT_ROOT / "evidence" / "tacdiffusion_offline_model_qualification_v1"
DEFAULT_K800_ROOT = EXPERIMENT_ROOT / "evidence" / "tacdiffusion_inactive_k800_candidate_v1"


class QualificationContractError(ValueError):
    """Raised when an offline qualification binding cannot be verified."""


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise QualificationContractError(f"{name} must be a lowercase SHA-256")
    return value


def canonical_sha256(payload: Mapping[str, object]) -> str:
    try:
        encoded = json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QualificationContractError("payload is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _artifact_with_hash(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    result.pop("artifact_sha256", None)
    _assert_portable_payload(result)
    result["artifact_sha256"] = canonical_sha256(result)
    return result


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return (json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise QualificationContractError("artifact is not serializable canonical JSON") from exc


def _serialized_json_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_json_bytes(payload)).hexdigest()


def _assert_portable_payload(value: object, *, context: str = "artifact", repo_root: Path | None = None) -> None:
    """Reject persisted absolute paths and worktree-specific path leakage."""

    worktree = str((repo_root or REPO_ROOT).resolve())
    if isinstance(value, Mapping):
        for key, nested in value.items():
            _assert_portable_payload(nested, context=f"{context}.{key}", repo_root=repo_root)
        return
    if isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_portable_payload(nested, context=f"{context}[{index}]", repo_root=repo_root)
        return
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute() or "/home/" in value or worktree in value:
            raise QualificationContractError(f"{context} contains a non-portable absolute path")


_PATH_RECEIPT_KEYS = frozenset(
    {
        "checkpoint_path",
        "source_checkpoint_path",
        "resumed_checkpoint_path",
        "artifact_path",
        "contract_path",
        "qualification_artifact_path",
    }
)


def _portableize_receipt(value: object, *, key: str | None = None) -> object:
    """Convert runtime path returns into artifact-local basenames."""

    if isinstance(value, Mapping):
        return {str(name): _portableize_receipt(nested, key=str(name)) for name, nested in value.items()}
    if isinstance(value, list):
        return [_portableize_receipt(nested, key=key) for nested in value]
    if isinstance(value, tuple):
        return [_portableize_receipt(nested, key=key) for nested in value]
    if isinstance(value, str) and key in _PATH_RECEIPT_KEYS:
        return Path(value).name
    return value


def _require_exact_keys(value: object, expected: frozenset[str], name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        actual = sorted(value) if isinstance(value, Mapping) else type(value).__name__
        raise QualificationContractError(f"{name} fields are missing or extra: {actual}")
    return value


def _require_bool(value: object, name: str, expected: bool | None = None) -> bool:
    if not isinstance(value, bool) or (expected is not None and value is not expected):
        raise QualificationContractError(f"{name} must be {expected if expected is not None else 'a boolean'}")
    return value


def _require_int(value: object, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (minimum is not None and value < minimum):
        raise QualificationContractError(f"{name} must be an integer")
    return value


def _require_finite(value: object, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise QualificationContractError(f"{name} must be finite")
    numeric = float(value)
    if minimum is not None and numeric < minimum:
        raise QualificationContractError(f"{name} is below its lower bound")
    return numeric


def _require_finite_vector(value: object, dimension: int, name: str) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != dimension:
        raise QualificationContractError(f"{name} must be a {dimension}D vector")
    result = tuple(_require_finite(item, f"{name}[{index}]") for index, item in enumerate(value))
    return result


def _read_json(path: str | Path) -> dict[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationContractError(f"invalid JSON artifact: {path}") from exc
    if not isinstance(payload, dict):
        raise QualificationContractError(f"JSON artifact must be an object: {path}")
    return payload


def _write_json_atomic(path: str | Path, payload: Mapping[str, object]) -> str:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_portable_payload(payload, context=str(destination))
    encoded = _json_bytes(payload)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("wb", dir=destination.parent, prefix=f".{destination.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return sha256_file(destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise QualificationContractError(f"path is outside repository: {path}") from exc


@dataclass(frozen=True)
class QualificationContract:
    """Versioned, bounded, full-architecture qualification configuration."""

    schema: str = CONTRACT_SCHEMA
    fixture_only: bool = True
    production_promotion_allowed: bool = False
    observation_dimension: int = OBSERVATION_DIMENSION
    action_dimension: int = ACTION_DIMENSION
    model_update_rate_hz: int = 100
    diffusion_steps: int = 50
    hidden_dimension: int = 512
    timestep_embedding_dimension: int = 64
    beta_start: float = 1e-4
    beta_end: float = 0.02
    train_epochs: int = 1
    resume_epochs: int = 1
    batch_size: int = 8
    learning_rate: float = 1e-3
    seed: int = 42
    max_vram_fraction: float = 0.849
    deterministic_algorithms_requested: bool = True
    single_worker: bool = True
    blas_openmp_threads: int = 1

    def __post_init__(self) -> None:
        if self.schema != CONTRACT_SCHEMA:
            raise QualificationContractError("qualification contract schema is invalid")
        if not self.fixture_only or self.production_promotion_allowed:
            raise QualificationContractError("qualification contract must remain fixture-only")
        if (self.observation_dimension, self.action_dimension) != (84, 12):
            raise QualificationContractError("qualification contract dimensions are invalid")
        if self.model_update_rate_hz not in DIAGNOSTIC_RATES_HZ or self.diffusion_steps != 50:
            raise QualificationContractError("qualification rate/step contract is invalid")
        if (self.hidden_dimension, self.timestep_embedding_dimension) != (512, 64):
            raise QualificationContractError("qualification must use the full MainlineModelConfig architecture")
        if not 0.0 < self.beta_start < self.beta_end < 1.0:
            raise QualificationContractError("qualification beta schedule is invalid")
        if self.train_epochs <= 0 or self.resume_epochs <= 0 or self.batch_size <= 0:
            raise QualificationContractError("qualification train bounds are invalid")
        if not math.isfinite(self.learning_rate) or self.learning_rate <= 0.0:
            raise QualificationContractError("qualification learning rate is invalid")
        if self.seed < 0 or self.blas_openmp_threads != 1 or not self.single_worker:
            raise QualificationContractError("qualification worker/thread contract is invalid")
        if not math.isfinite(self.max_vram_fraction) or not 0.0 < self.max_vram_fraction < 0.85:
            raise QualificationContractError("qualification VRAM bound must remain below 85 percent")

    def model_config(self) -> MainlineModelConfig:
        return MainlineModelConfig(
            observation_dimension=self.observation_dimension,
            action_dimension=self.action_dimension,
            model_update_rate_hz=self.model_update_rate_hz,
            diffusion_steps=self.diffusion_steps,
            hidden_dimension=self.hidden_dimension,
            timestep_embedding_dimension=self.timestep_embedding_dimension,
            beta_start=self.beta_start,
            beta_end=self.beta_end,
        )

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class FixtureBindings:
    bundle_digest_sha256: str
    dataset_manifest_sha256: str
    dataset_sha256: str
    source_receipt_sha256: str
    split_sha256: str
    split_version: str
    observation_dimension: int
    action_dimension: int
    row_count: int
    episode_group_count: int
    split_row_counts: Mapping[str, int]
    split_episode_counts: Mapping[str, int]
    source_closure: Mapping[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "bundle_digest_sha256": self.bundle_digest_sha256,
            "dataset_manifest_sha256": self.dataset_manifest_sha256,
            "dataset_sha256": self.dataset_sha256,
            "source_receipt_sha256": self.source_receipt_sha256,
            "split_sha256": self.split_sha256,
            "split_version": self.split_version,
            "observation_dimension": self.observation_dimension,
            "action_dimension": self.action_dimension,
            "row_count": self.row_count,
            "episode_group_count": self.episode_group_count,
            "split_row_counts": dict(self.split_row_counts),
            "split_episode_counts": dict(self.split_episode_counts),
            "source_closure": dict(self.source_closure),
        }


@dataclass(frozen=True)
class FixtureDataset:
    observations: np.ndarray
    actions: np.ndarray
    episode_ids: tuple[str, ...]
    splits: tuple[str, ...]
    timestamps_s: np.ndarray
    bindings: FixtureBindings


def _validate_frozen_split(bundle_root: Path) -> tuple[str, str]:
    split_path = bundle_root / "frozen_split.json"
    split = _read_json(split_path)
    if split.get("schema") != "ur10e_tacdiffusion_offline_frozen_split/v1":
        raise QualificationContractError("frozen split schema is invalid")
    supplied = _require_sha256(split.get("split_sha256"), "split_sha256")
    unsigned = dict(split)
    unsigned.pop("split_sha256", None)
    if canonical_sha256(unsigned) != supplied or supplied != EXPECTED_SPLIT_SHA256:
        raise QualificationContractError("frozen split receipt is invalid")
    if split.get("version") != "sha256_episode_group_v1" or split.get("seed") != 42:
        raise QualificationContractError("frozen split policy is invalid")
    episode_split = split.get("episode_split")
    if not isinstance(episode_split, dict) or len(episode_split) != EPISODE_GROUP_COUNT:
        raise QualificationContractError("frozen split does not cover the expected episode groups")
    if any(value not in {"train", "validation", "test"} for value in episode_split.values()):
        raise QualificationContractError("frozen split contains an unsupported split")
    return supplied, "sha256_episode_group_v1"


def _validate_source_closure(source_path: Path, repo_root: Path) -> dict[str, str]:
    source = _read_json(source_path)
    if source.get("schema") != "ur10e_tacdiffusion_offline_source_receipt/v2":
        raise QualificationContractError("campaign source receipt schema is invalid")
    source_receipt_sha = _require_sha256(source.get("source_receipt_sha256"), "source_receipt_sha256")
    if source_receipt_sha != EXPECTED_SOURCE_RECEIPT_SHA256:
        raise QualificationContractError("campaign source receipt digest is not the frozen receipt")
    files = source.get("files")
    if not isinstance(files, list) or not files:
        raise QualificationContractError("campaign source closure is empty")
    closure: dict[str, str] = {}
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise QualificationContractError("campaign source closure entry is invalid")
        relative = item["path"]
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise QualificationContractError("campaign source closure path is not repository-relative")
        expected = _require_sha256(item["sha256"], f"source closure {relative}")
        actual_path = repo_root / relative
        if not actual_path.is_file() or sha256_file(actual_path) != expected:
            raise QualificationContractError(f"source closure drift: {relative}")
        closure[relative] = expected
    unsigned = dict(source)
    unsigned.pop("source_receipt_sha256", None)
    if canonical_sha256(unsigned) != source_receipt_sha:
        raise QualificationContractError("source receipt payload does not match its receipt digest")
    return closure


def _load_fixture_arrays(bundle_root: Path, dataset_manifest: Mapping[str, object]) -> tuple[np.ndarray, np.ndarray, tuple[str, ...], tuple[str, ...], np.ndarray]:
    dataset_path = bundle_root / "dataset.npz"
    try:
        with np.load(dataset_path, allow_pickle=False) as payload:
            expected_fields = {"actions", "episode_artifact_sha256", "episode_ids", "observations", "row_sha256", "sample_indices", "splits", "timestamps_s"}
            if set(payload.files) != expected_fields:
                raise QualificationContractError("fixture dataset fields are missing or extra")
            observations = np.array(payload["observations"], dtype=np.float32, copy=True)
            actions = np.array(payload["actions"], dtype=np.float32, copy=True)
            episode_artifact_sha256 = np.array(payload["episode_artifact_sha256"], copy=True)
            episode_ids_array = np.array(payload["episode_ids"], copy=True)
            row_sha256 = np.array(payload["row_sha256"], copy=True)
            sample_indices = np.array(payload["sample_indices"], copy=True)
            splits_array = np.array(payload["splits"], copy=True)
            timestamps = np.array(payload["timestamps_s"], dtype=np.float64, copy=True)
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, QualificationContractError):
            raise
        raise QualificationContractError("fixture dataset NPZ is invalid") from exc
    if observations.shape != (ROW_COUNT, OBSERVATION_DIMENSION) or actions.shape != (ROW_COUNT, ACTION_DIMENSION):
        raise QualificationContractError("fixture dataset dimensions are not 84D/12D")
    if observations.dtype != np.float32 or actions.dtype != np.float32 or timestamps.shape != (ROW_COUNT,):
        raise QualificationContractError("fixture dataset dtypes/shapes are invalid")
    if episode_ids_array.shape != (ROW_COUNT,) or splits_array.shape != (ROW_COUNT,) or sample_indices.shape != (ROW_COUNT,):
        raise QualificationContractError("fixture dataset row fields have invalid shapes")
    if episode_artifact_sha256.shape != (ROW_COUNT,) or row_sha256.shape != (ROW_COUNT,):
        raise QualificationContractError("fixture dataset row receipts have invalid shapes")
    if not np.isfinite(observations).all() or not np.isfinite(actions).all() or not np.isfinite(timestamps).all():
        raise QualificationContractError("fixture dataset contains non-finite values")
    episode_ids = tuple(str(value) for value in episode_ids_array.tolist())
    splits = tuple(str(value) for value in splits_array.tolist())
    if any(not value.strip() for value in episode_ids) or any(value not in {"train", "validation", "test"} for value in splits):
        raise QualificationContractError("fixture dataset episode/split labels are invalid")
    if not all(isinstance(value, (int, np.integer)) and not isinstance(value, bool) and value >= 0 for value in sample_indices.tolist()):
        raise QualificationContractError("fixture sample indices are invalid")
    if any(_require_sha256(str(value), "episode_artifact_sha256") != str(value) for value in episode_artifact_sha256.tolist()):
        raise QualificationContractError("fixture episode artifact receipts are invalid")
    if any(_require_sha256(str(value), "row_sha256") != str(value) for value in row_sha256.tolist()):
        raise QualificationContractError("fixture row receipts are invalid")
    episode_to_split: dict[str, str] = {}
    episode_to_indices: dict[str, list[int]] = {}
    for row_index, (episode, split) in enumerate(zip(episode_ids, splits)):
        if episode in episode_to_split and episode_to_split[episode] != split:
            raise QualificationContractError("sample-level leakage: an episode crosses splits")
        episode_to_split[episode] = split
        episode_to_indices.setdefault(episode, []).append(row_index)
    for indices in episode_to_indices.values():
        local_indices = [int(sample_indices[index]) for index in indices]
        if local_indices != list(range(len(indices))):
            raise QualificationContractError("fixture episode sample indices are not deterministic")
    expected_episode_split = dataset_manifest.get("episode_split")
    if expected_episode_split != episode_to_split:
        raise QualificationContractError("fixture episode-grouped split differs from the frozen manifest")
    expected_row_counts = dataset_manifest.get("split_row_counts")
    expected_episode_counts = dataset_manifest.get("split_episode_counts")
    row_counts = {split: splits.count(split) for split in ("train", "validation", "test")}
    episode_counts = {split: sum(value == split for value in episode_to_split.values()) for split in ("train", "validation", "test")}
    if expected_row_counts != row_counts or expected_episode_counts != episode_counts:
        raise QualificationContractError("fixture split counts are inconsistent")
    for array in (observations, actions, timestamps, sample_indices, episode_artifact_sha256, row_sha256):
        array.flags.writeable = False
    return observations, actions, episode_ids, splits, timestamps


def validate_fixture_campaign(
    bundle_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    *,
    repo_root: str | Path = REPO_ROOT,
) -> FixtureDataset:
    """Validate current campaign bytes, source closure, dimensions and split."""

    bundle = Path(bundle_root)
    repository = Path(repo_root)
    if not bundle.is_dir():
        raise QualificationContractError("fixture campaign bundle is missing")
    try:
        from .offline_campaign import validate_offline_campaign_bundle

        validate_offline_campaign_bundle(bundle, source_root=repository)
    except QualificationContractError:
        raise
    except Exception as exc:
        raise QualificationContractError("fixture campaign source/dataset closure failed") from exc
    manifest = _read_json(bundle / "bundle.manifest.json")
    if manifest.get("schema") != CAMPAIGN_BUNDLE_SCHEMA or manifest.get("fixture_only") is not True or manifest.get("production_promotion_allowed") is not False:
        raise QualificationContractError("fixture campaign promotion boundary is invalid")
    bundle_digest = _require_sha256(manifest.get("bundle_digest_sha256"), "bundle_digest_sha256")
    if bundle_digest != EXPECTED_BUNDLE_DIGEST:
        raise QualificationContractError("fixture campaign bundle digest differs from the tracked campaign")
    dataset_manifest_path = bundle / "dataset.manifest.json"
    dataset_manifest = _read_json(dataset_manifest_path)
    if dataset_manifest.get("schema") != CAMPAIGN_DATASET_SCHEMA:
        raise QualificationContractError("fixture dataset manifest schema is invalid")
    if sha256_file(dataset_manifest_path) != EXPECTED_DATASET_MANIFEST_SHA256 or dataset_manifest.get("manifest_sha256") != "c7ab3306509632f106bb97de6012cfc18076d215298e01900703181e64817f59":
        raise QualificationContractError("fixture dataset manifest bytes are not frozen")
    if dataset_manifest.get("observation_dimension") != OBSERVATION_DIMENSION or dataset_manifest.get("action_dimension") != ACTION_DIMENSION or dataset_manifest.get("row_count") != ROW_COUNT or dataset_manifest.get("episode_count") != EPISODE_GROUP_COUNT:
        raise QualificationContractError("fixture dataset manifest dimensions/counts are invalid")
    if dataset_manifest.get("observation_shape") != [ROW_COUNT, OBSERVATION_DIMENSION] or dataset_manifest.get("action_shape") != [ROW_COUNT, ACTION_DIMENSION]:
        raise QualificationContractError("fixture dataset manifest shapes are invalid")
    if dataset_manifest.get("fixture_only") is not True or dataset_manifest.get("production_promotion_allowed") is not False or dataset_manifest.get("episode_grouped_split_verified") is not True or dataset_manifest.get("split_frozen_before_training") is not True:
        raise QualificationContractError("fixture dataset training boundary is invalid")
    legacy = dataset_manifest.get("legacy_dimensions_rejected")
    if not isinstance(legacy, dict) or legacy.get("accepted") is not False or legacy.get("condition_36d_action_6d") is not True:
        raise QualificationContractError("legacy 36D/6D rejection is not bound")
    dataset_path = bundle / "dataset.npz"
    dataset_sha = _require_sha256(dataset_manifest.get("dataset_sha256"), "dataset_sha256")
    if dataset_sha != EXPECTED_DATASET_SHA256 or sha256_file(dataset_path) != dataset_sha:
        raise QualificationContractError("fixture dataset bytes are tampered")
    source_receipt_path = bundle / "source_identities.json"
    source_receipt_sha = _require_sha256(manifest.get("source_receipt_sha256"), "bundle source_receipt_sha256")
    if source_receipt_sha != EXPECTED_SOURCE_RECEIPT_SHA256:
        raise QualificationContractError("fixture source receipt binding is invalid")
    closure = _validate_source_closure(source_receipt_path, repository)
    split_sha, split_version = _validate_frozen_split(bundle)
    observations, actions, episode_ids, splits, timestamps = _load_fixture_arrays(bundle, dataset_manifest)
    split_manifest_sha = _require_sha256(dataset_manifest.get("split_sha256"), "dataset split_sha256")
    if split_manifest_sha != split_sha:
        raise QualificationContractError("dataset and frozen split receipts differ")
    bindings = FixtureBindings(
        bundle_digest_sha256=bundle_digest,
        dataset_manifest_sha256=sha256_file(dataset_manifest_path),
        dataset_sha256=dataset_sha,
        source_receipt_sha256=source_receipt_sha,
        split_sha256=split_sha,
        split_version=split_version,
        observation_dimension=OBSERVATION_DIMENSION,
        action_dimension=ACTION_DIMENSION,
        row_count=ROW_COUNT,
        episode_group_count=EPISODE_GROUP_COUNT,
        split_row_counts=dict(dataset_manifest["split_row_counts"]),
        split_episode_counts=dict(dataset_manifest["split_episode_counts"]),
        source_closure=closure,
    )
    return FixtureDataset(observations, actions, episode_ids, splits, timestamps, bindings)


def code_source_hashes(repo_root: str | Path = REPO_ROOT) -> dict[str, str]:
    repository = Path(repo_root)
    paths = (
        "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/mainline_model.py",
        "experiments/ur10e-variable-impedance/ur10e_vic/tacdiffusion/offline_model_qualification.py",
        "experiments/ur10e-variable-impedance/tools/materialize_tacdiffusion_offline_model_qualification.py",
    )
    result: dict[str, str] = {}
    for relative in paths:
        path = repository / relative
        if not path.is_file():
            raise QualificationContractError(f"qualification code source is missing: {relative}")
        result[relative] = _require_sha256(sha256_file(path), f"code source {relative}")
    return result


def build_contract_payload(
    fixture: FixtureDataset,
    *,
    contract: QualificationContract | None = None,
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, object]:
    selected = contract or QualificationContract()
    code_hashes = code_source_hashes(repo_root)
    payload: dict[str, object] = {
        "schema": selected.schema,
        "contract": selected.as_dict(),
        "contract_sha256": canonical_sha256(selected.as_dict()),
        "fixture_binding": fixture.bindings.as_dict(),
        "code_source_hashes": code_hashes,
        "device_policy": {
            "execution": "cuda_only",
            "cpu_fallback": False,
            "single_worker": True,
            "blas_openmp_threads": 1,
            "max_vram_fraction_strictly_below": selected.max_vram_fraction,
            "determinism": "requested_and_record_unavoidable_nondeterminism",
        },
        "checkpoint_policy": {
            "fixture_only": True,
            "formal_checkpoint": False,
            "active_allowed": False,
            "active": False,
            "reproduction_status": "not_claimed",
            "model_rate_selected_hz": None,
        },
    }
    payload["artifact_sha256"] = canonical_sha256(payload)
    return payload


def _validate_output_layout(output_root: Path, allowed_names: frozenset[str], name: str) -> None:
    if not output_root.is_dir():
        raise QualificationContractError(f"{name} output directory is missing")
    actual: set[str] = set()
    for child in output_root.iterdir():
        if child.is_dir():
            raise QualificationContractError(f"{name} output contains an unexpected directory: {child.name}")
        actual.add(child.name)
    if actual != set(allowed_names):
        missing = sorted(set(allowed_names) - actual)
        extra = sorted(actual - set(allowed_names))
        raise QualificationContractError(f"{name} output is partial or mixed; missing={missing}, extra={extra}")


def _contract_file_sha256(contract_path: Path) -> str:
    if contract_path.name != CONTRACT_ARTIFACT_NAME or not contract_path.is_file():
        raise QualificationContractError("qualification contract artifact is missing or has an ambiguous name")
    return _require_sha256(sha256_file(contract_path), "qualification contract file sha256")


def validate_contract_artifact(
    contract_path: str | Path,
    *,
    bundle_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, object]:
    """Validate the actual contract bytes and all current fixture/code bindings."""

    path = Path(contract_path)
    payload = _read_json(path)
    _assert_portable_payload(payload, context="qualification.contract", repo_root=Path(repo_root))
    _require_exact_keys(payload, CONTRACT_FIELDS, "qualification contract")
    supplied_artifact = _require_sha256(payload.get("artifact_sha256"), "contract artifact_sha256")
    unsigned = dict(payload)
    unsigned.pop("artifact_sha256", None)
    if canonical_sha256(unsigned) != supplied_artifact:
        raise QualificationContractError("qualification contract artifact hash mismatch")
    if payload.get("schema") != CONTRACT_SCHEMA:
        raise QualificationContractError("qualification contract schema is invalid")
    contract_payload = payload.get("contract")
    if not isinstance(contract_payload, Mapping) or set(contract_payload) != set(QualificationContract.__dataclass_fields__):
        raise QualificationContractError("qualification contract fields are missing or extra")
    try:
        selected = QualificationContract(**dict(contract_payload))
    except (TypeError, ValueError) as exc:
        raise QualificationContractError("qualification contract values are invalid") from exc
    if selected.as_dict() != QualificationContract().as_dict():
        raise QualificationContractError("qualification contract is not the exact frozen contract")
    if payload.get("contract_sha256") != canonical_sha256(selected.as_dict()):
        raise QualificationContractError("qualification contract payload digest is invalid")
    fixture = validate_fixture_campaign(bundle_root, repo_root=repo_root)
    expected = build_contract_payload(fixture, contract=selected, repo_root=repo_root)
    if payload != expected:
        raise QualificationContractError("qualification contract does not match current fixture/code bindings")
    if _contract_file_sha256(path) != _serialized_json_sha256(expected):
        raise QualificationContractError("qualification contract bytes are not the canonical current artifact")
    return dict(payload)


def _validate_no_checkpoint_output(output_root: Path) -> None:
    if not output_root.exists():
        return
    forbidden = tuple(path for path in output_root.rglob("*") if path.is_file() and path.suffix.lower() in {".pt", ".pth", ".ckpt", ".bin", ".npz"})
    if forbidden:
        raise QualificationContractError("gate-deferred evidence contains a fake or stale checkpoint")


def build_gate_deferred_evidence(
    bundle_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    *,
    repo_root: str | Path = REPO_ROOT,
    output_root: str | Path | None = None,
    contract: QualificationContract | None = None,
) -> dict[str, object]:
    """Build a truthful gate-deferred report without touching CUDA."""

    if output_root is not None:
        _validate_no_checkpoint_output(Path(output_root))
    fixture = validate_fixture_campaign(bundle_root, repo_root=repo_root)
    contract_payload = build_contract_payload(fixture, contract=contract, repo_root=repo_root)
    report: dict[str, object] = {
        "schema": QUALIFICATION_SCHEMA,
        "status": "gate_deferred",
        "cuda_execution_status": CUDA_GATE_DEFERRED,
        "fixture_only": True,
        "production_promotion_allowed": False,
        "formal_checkpoint": False,
        "active_allowed": False,
        "active": False,
        "reproduction_status": "not_claimed",
        "model_rate_selected_hz": None,
        "selection_eligible": False,
        "selected_rate_hz": None,
        "formal_rate_selection": False,
        "qualification_executed": False,
        "no_fake_checkpoint": True,
        "checkpoint_artifacts": [],
        "evaluation_artifacts": [],
        "sampler_smoke": {
            "executed": False,
            "required_diffusion_steps": 50,
            "required_reverse_step_trace": list(REVERSE_STEP_TRACE),
            "observed_trace": None,
        },
        "diagnostics": {
            "status": "external_deferred",
            "rates_hz": list(DIAGNOSTIC_RATES_HZ),
            "selection_eligible": False,
            "selected_rate_hz": None,
            "formal_rate_selection": False,
        },
        "fixture_binding": fixture.bindings.as_dict(),
        "contract_artifact": CONTRACT_ARTIFACT_NAME,
        "contract_artifact_sha256": _serialized_json_sha256(contract_payload),
        "code_source_hashes": dict(contract_payload["code_source_hashes"]),
    }
    return _artifact_with_hash(report)


def validate_gate_deferred_evidence(
    report_path: str | Path,
    *,
    bundle_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, object]:
    path = Path(report_path)
    if path.name != DEFERRED_ARTIFACT_NAME:
        raise QualificationContractError("gate-deferred artifact has an ambiguous filename")
    report = _read_json(path)
    _validate_output_layout(path.parent, DEFERRED_OUTPUT_NAMES, "gate-deferred")
    _require_exact_keys(report, DEFERRED_FIELDS, "gate-deferred artifact")
    _assert_portable_payload(report, context="qualification.gate-deferred", repo_root=Path(repo_root))
    supplied = _require_sha256(report.get("artifact_sha256"), "qualification artifact_sha256")
    unsigned = dict(report)
    unsigned.pop("artifact_sha256", None)
    if canonical_sha256(unsigned) != supplied:
        raise QualificationContractError("qualification deferred artifact hash mismatch")
    fixture = validate_fixture_campaign(bundle_root, repo_root=repo_root)
    contract_path = path.parent / CONTRACT_ARTIFACT_NAME
    contract_payload = validate_contract_artifact(contract_path, bundle_root=bundle_root, repo_root=repo_root)
    if report.get("contract_artifact") != CONTRACT_ARTIFACT_NAME:
        raise QualificationContractError("gate-deferred contract locator is not artifact-local")
    if report.get("contract_artifact_sha256") != _contract_file_sha256(contract_path):
        raise QualificationContractError("gate-deferred report is not bound to current contract bytes")
    expected = build_gate_deferred_evidence(bundle_root, repo_root=repo_root)
    if report != expected:
        raise QualificationContractError("gate-deferred evidence contains a promoted, missing, or altered field")
    if sha256_file(path) != _serialized_json_sha256(expected):
        raise QualificationContractError("gate-deferred artifact bytes are not canonical current evidence")
    if report.get("fixture_binding") != fixture.bindings.as_dict() or report.get("code_source_hashes") != code_source_hashes(repo_root):
        raise QualificationContractError("qualification source/dataset binding drifted")
    if not isinstance(contract_payload, dict) or contract_payload.get("artifact_sha256") != _require_sha256(contract_payload.get("artifact_sha256"), "contract artifact_sha256"):
        raise QualificationContractError("gate-deferred contract payload is invalid")
    sampler = report.get("sampler_smoke")
    if not isinstance(sampler, Mapping) or set(sampler) != {"executed", "required_diffusion_steps", "required_reverse_step_trace", "observed_trace"} or sampler.get("executed") is not False or sampler.get("required_diffusion_steps") != 50 or sampler.get("required_reverse_step_trace") != list(REVERSE_STEP_TRACE) or sampler.get("observed_trace") is not None:
        raise QualificationContractError("gate-deferred evidence cannot contain a fake sampler trace")
    diagnostics = report.get("diagnostics")
    if not isinstance(diagnostics, Mapping) or set(diagnostics) != {"status", "rates_hz", "selection_eligible", "selected_rate_hz", "formal_rate_selection"} or diagnostics != {
        "status": "external_deferred",
        "rates_hz": list(DIAGNOSTIC_RATES_HZ),
        "selection_eligible": False,
        "selected_rate_hz": None,
        "formal_rate_selection": False,
    }:
        raise QualificationContractError("gate-deferred diagnostic state is missing or promoted")
    return report


def _cuda_device_identity() -> dict[str, object]:
    """Query CUDA only from an explicitly opened execution path."""

    if torch is None or not torch.cuda.is_available():
        raise QualificationContractError("CUDA is required and unavailable")
    device_index = int(torch.cuda.current_device())
    properties = torch.cuda.get_device_properties(device_index)
    total_memory = int(properties.total_memory)
    if total_memory <= 0:
        raise QualificationContractError("CUDA device reports invalid total memory")
    return {
        "backend": "cuda",
        "device_index": device_index,
        "device_name": str(properties.name),
        "compute_capability": [int(properties.major), int(properties.minor)],
        "total_memory_bytes": total_memory,
        "torch_version": str(torch.__version__),
        "platform": platform.platform(),
    }


def _configure_cuda_execution(contract: QualificationContract) -> dict[str, object]:
    if torch is None:
        raise QualificationContractError("PyTorch is required for CUDA qualification")
    required_threads = {name: os.environ.get(name) for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")}
    if any(value != "1" for value in required_threads.values()):
        raise QualificationContractError("CUDA qualification requires BLAS/OpenMP thread environment values of 1")
    if not contract.single_worker or contract.blas_openmp_threads != 1:
        raise QualificationContractError("CUDA qualification worker contract is invalid")
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # If another caller initialized the thread pool, retain the observed
        # state in the receipt and fail only if it is not one-threaded below.
        pass
    torch.use_deterministic_algorithms(True)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    identity = _cuda_device_identity()
    device = torch.device("cuda", int(identity["device_index"]))
    if torch.get_num_threads() != 1 or torch.get_num_interop_threads() != 1:
        raise QualificationContractError("CUDA qualification thread pool is not single-threaded")
    return {
        "environment_threads": required_threads,
        "torch_num_threads": int(torch.get_num_threads()),
        "torch_num_interop_threads": int(torch.get_num_interop_threads()),
        "deterministic_algorithms_requested": True,
        "deterministic_algorithms_enabled": bool(torch.are_deterministic_algorithms_enabled()),
        "deterministic_warn_only": bool(torch.is_deterministic_algorithms_warn_only_enabled()),
        "device": identity,
        "device_string": str(device),
        "unavoidable_cuda_nondeterminism": [],
    }


def _vram_receipt(execution: Mapping[str, object], stage: str, contract: QualificationContract) -> dict[str, object]:
    if torch is None:
        raise QualificationContractError("CUDA memory receipt requires PyTorch")
    device_index = int(execution["device"]["device_index"])
    total = int(execution["device"]["total_memory_bytes"])
    allocated = int(torch.cuda.max_memory_allocated(device_index))
    reserved = int(torch.cuda.max_memory_reserved(device_index))
    fraction = max(allocated, reserved) / total
    result = {
        "stage": stage,
        "peak_allocated_bytes": allocated,
        "peak_reserved_bytes": reserved,
        "device_total_memory_bytes": total,
        "peak_fraction": fraction,
        "limit_fraction_strictly_below": contract.max_vram_fraction,
        "below_85_percent": fraction < 0.85,
    }
    if not result["below_85_percent"] or fraction >= contract.max_vram_fraction:
        raise QualificationContractError(f"CUDA VRAM bound exceeded at {stage}")
    return result


def validate_checkpoint_binding(
    checkpoint_path: str | Path,
    expected_binding: Mapping[str, object],
    *,
    require_cuda: bool = True,
) -> dict[str, object]:
    """Validate payload lineage, architecture, device and inactive flags."""

    path = Path(checkpoint_path)
    if not path.is_file():
        raise QualificationContractError("bound checkpoint is missing")
    if torch is None:
        raise QualificationContractError("bound checkpoint requires PyTorch")
    if require_cuda and not torch.cuda.is_available():
        raise QualificationContractError("bound checkpoint requires CUDA")
    try:
        payload = torch.load(path, map_location="cuda" if require_cuda else "cpu", weights_only=False)
    except Exception as exc:
        raise QualificationContractError("bound checkpoint bytes are invalid") from exc
    if not isinstance(payload, dict) or payload.get("checkpoint_binding") != dict(expected_binding):
        raise QualificationContractError("checkpoint binding receipt mismatch")
    config_payload = payload.get("config")
    if not isinstance(config_payload, dict):
        raise QualificationContractError("checkpoint configuration receipt is invalid")
    try:
        config = MainlineModelConfig(**config_payload)
    except (TypeError, ValueError) as exc:
        raise QualificationContractError("checkpoint configuration is invalid") from exc
    if (config.observation_dimension, config.action_dimension, config.diffusion_steps, config.hidden_dimension, config.timestep_embedding_dimension) != (84, 12, 50, 512, 64):
        raise QualificationContractError("checkpoint architecture/step receipt is invalid")
    binding_device = expected_binding.get("device")
    if not isinstance(binding_device, Mapping):
        raise QualificationContractError("checkpoint device receipt is missing")
    if require_cuda:
        current_index = int(torch.cuda.current_device())
        if int(binding_device.get("device_index", -1)) != current_index:
            raise QualificationContractError("checkpoint was bound to the wrong CUDA device")
    for field, expected in (("fixture_only", True), ("formal_checkpoint", False), ("active_allowed", False), ("active", False), ("model_rate_selected_hz", None), ("reproduction_status", "not_claimed")):
        if expected_binding.get(field, payload.get(field)) is not expected and expected_binding.get(field, payload.get(field)) != expected:
            raise QualificationContractError(f"checkpoint inactive boundary is invalid: {field}")
    loaded = load_mainline_checkpoint(path, require_cuda=require_cuda)
    if require_cuda and not str(loaded["device"]).startswith("cuda"):
        raise QualificationContractError("checkpoint loaded outside CUDA")
    return {
        "checkpoint_sha256": _require_sha256(sha256_file(path), "checkpoint_sha256"),
        "device": dict(binding_device),
        "config": config_payload,
        "fixture_only": True,
        "formal_checkpoint": False,
        "active_allowed": False,
        "active": False,
        "reproduction_status": "not_claimed",
        "model_rate_selected_hz": None,
    }


def _checkpoint_receipt(
    path: Path,
    *,
    stage: str,
    fixture: FixtureDataset,
    contract_payload: Mapping[str, object],
    execution: Mapping[str, object],
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": CHECKPOINT_BINDING_SCHEMA,
        "stage": stage,
        "checkpoint_sha256": _require_sha256(sha256_file(path), f"{stage} checkpoint_sha256"),
        "checkpoint_path": path.name,
        "fixture_binding": fixture.bindings.as_dict(),
        "source_receipt_sha256": fixture.bindings.source_receipt_sha256,
        "split_sha256": fixture.bindings.split_sha256,
        "code_source_hashes": dict(contract_payload["code_source_hashes"]),
        "configuration": dict(contract_payload["contract"]),
        "device": dict(execution["device"]),
        "fixture_only": True,
        "formal_checkpoint": False,
        "active_allowed": False,
        "active": False,
        "reproduction_status": "not_claimed",
        "model_rate_selected_hz": None,
    }
    if extra:
        payload.update(_portableize_receipt(dict(extra)))  # type: ignore[arg-type]
    _assert_portable_payload(payload, context=f"{stage} checkpoint receipt")
    return _artifact_with_hash(payload)


def _loaded_model_predictor(model: object, normalization: Mapping[str, object], *, seed: int = 42) -> Callable[[np.ndarray, int], np.ndarray]:
    if torch is None:
        raise QualificationContractError("loaded CUDA diagnostic requires PyTorch")
    device = next(model.parameters()).device
    observation_mean = normalization["observation_mean"].to(device)
    observation_std = normalization["observation_std"].to(device)

    def predict(observation: np.ndarray, tick_seed: int) -> np.ndarray:
        values = np.array(observation, dtype=np.float32, copy=True)
        if values.shape != (OBSERVATION_DIMENSION,) or not np.isfinite(values).all():
            raise QualificationContractError("diagnostic observation is not a finite 84D vector")
        with torch.no_grad():
            tensor = torch.from_numpy(values[None, :]).to(device)
            action = model.sample((tensor - observation_mean) / observation_std, seed=seed + tick_seed)
        return action[0].detach().cpu().numpy().astype(np.float32, copy=False)

    return predict


@dataclass(frozen=True)
class DiagnosticPolicy:
    deadline_fraction: float = 0.8
    max_consecutive_deadline_misses: int = 2
    max_consecutive_stale_ticks: int = 2

    def __post_init__(self) -> None:
        if not 0.0 < self.deadline_fraction <= 1.0 or self.max_consecutive_deadline_misses <= 0 or self.max_consecutive_stale_ticks <= 0:
            raise QualificationContractError("diagnostic stale/deadline policy is invalid")


def _predict_callable(predictor: object) -> Callable[[np.ndarray, int], object]:
    if callable(predictor):
        return predictor  # type: ignore[return-value]
    method = getattr(predictor, "predict", None)
    if not callable(method):
        raise QualificationContractError("diagnostic predictor is not callable")
    return method


def _pace_until(clock: Callable[[], float], sleeper: Callable[[float], None], release_s: float) -> None:
    while True:
        now = float(clock())
        if now >= release_s:
            return
        sleeper(release_s - now)


def _diagnostic_action(value: object) -> tuple[float, ...]:
    array = np.asarray(value, dtype=np.float32)
    if array.shape == (1, ACTION_DIMENSION):
        array = array[0]
    if array.shape != (ACTION_DIMENSION,) or not np.isfinite(array).all():
        raise QualificationContractError("diagnostic predictor returned an invalid 12D action")
    return tuple(float(item) for item in array.tolist())


def run_paced_diagnostics(
    predictor: object,
    observations: np.ndarray,
    *,
    checkpoint_sha256: str,
    dataset_sha256: str,
    source_receipt_sha256: str,
    code_source_hashes_binding: Mapping[str, str],
    sampler_trace: Sequence[int] = REVERSE_STEP_TRACE,
    warmup_samples: int = 1,
    steady_samples: int = 8,
    policy: DiagnosticPolicy = DiagnosticPolicy(),
    rates_hz: Sequence[int] = DIAGNOSTIC_RATES_HZ,
    clock: Callable[[], float] = time.perf_counter,
    sleeper: Callable[[float], None] = time.sleep,
    diagnostic_kind: str = DIAGNOSTIC_KIND_RUNTIME,
    latency_performance_claim: bool = True,
) -> dict[str, object]:
    """Run short 50/100 Hz inference diagnostics, never rate selection."""

    checkpoint_hash = _require_sha256(checkpoint_sha256, "checkpoint_sha256")
    dataset_hash = _require_sha256(dataset_sha256, "dataset_sha256")
    source_hash = _require_sha256(source_receipt_sha256, "source_receipt_sha256")
    observations_array = np.asarray(observations, dtype=np.float32)
    if observations_array.ndim != 2 or observations_array.shape[1] != OBSERVATION_DIMENSION or observations_array.shape[0] == 0 or not np.isfinite(observations_array).all():
        raise QualificationContractError("diagnostic observations must be finite [N,84]")
    if tuple(int(value) for value in sampler_trace) != REVERSE_STEP_TRACE:
        raise QualificationContractError("diagnostic sampler trace is not exactly 50 distinct reverse steps")
    if tuple(int(value) for value in rates_hz) != DIAGNOSTIC_RATES_HZ:
        raise QualificationContractError("diagnostic rates must be exactly 50 and 100 Hz")
    if warmup_samples <= 0 or steady_samples <= 0:
        raise QualificationContractError("diagnostic sample bounds are invalid")
    if diagnostic_kind not in {DIAGNOSTIC_KIND_RUNTIME, DIAGNOSTIC_KIND_POLICY_PROBE}:
        raise QualificationContractError("diagnostic kind is invalid")
    if not isinstance(latency_performance_claim, bool) or latency_performance_claim is not (diagnostic_kind == DIAGNOSTIC_KIND_RUNTIME):
        raise QualificationContractError("diagnostic performance-claim boundary is invalid")
    predict = _predict_callable(predictor)
    rate_results: list[dict[str, object]] = []
    for rate_hz in DIAGNOSTIC_RATES_HZ:
        period_s = 1.0 / rate_hz
        deadline_s = policy.deadline_fraction * period_s
        trial_started = float(clock())
        warmup: list[dict[str, object]] = []
        for index in range(warmup_samples):
            release = trial_started + index * period_s
            _pace_until(clock, sleeper, release)
            started = float(clock())
            value = _diagnostic_action(predict(observations_array[index % len(observations_array)], index))
            ended = float(clock())
            latency_s = ended - started
            if not math.isfinite(started) or not math.isfinite(ended) or not math.isfinite(latency_s) or started < release or ended < started or latency_s < 0.0:
                raise QualificationContractError("diagnostic warmup timing is not finite and monotonic")
            warmup.append(
                {
                    "index": index,
                    "release_s": release,
                    "start_s": started,
                    "end_s": ended,
                    "latency_s": latency_s,
                    "deadline_class": "warmup_excluded",
                    "action": list(value),
                    "action_finite": bool(np.isfinite(value).all()),
                }
            )
        steady: list[dict[str, object]] = []
        last_action = (0.0,) * ACTION_DIMENSION
        consecutive_misses = 0
        consecutive_stale = 0
        transition_index: int | None = None
        governed_stop = False
        stop_reason: str | None = None
        for index in range(steady_samples):
            release = trial_started + (warmup_samples + index) * period_s
            _pace_until(clock, sleeper, release)
            started = float(clock())
            prediction_error: str | None = None
            try:
                candidate = _diagnostic_action(predict(observations_array[index % len(observations_array)], warmup_samples + index))
            except Exception as exc:  # diagnostics convert inference failure into a governed stale action
                candidate = last_action
                prediction_error = type(exc).__name__
            ended = float(clock())
            latency_s = ended - started
            if not math.isfinite(started) or not math.isfinite(ended) or not math.isfinite(latency_s) or started < release or ended < started or latency_s < 0.0:
                raise QualificationContractError("diagnostic steady timing is not finite and monotonic")
            deadline_met = prediction_error is None and latency_s <= deadline_s
            if deadline_met:
                action_state = "fresh"
                action = candidate
                last_action = candidate
                consecutive_misses = 0
                consecutive_stale = 0
            else:
                action_state = "stale"
                action = last_action
                consecutive_misses += 1
                consecutive_stale += 1
                if transition_index is None:
                    transition_index = index
            row: dict[str, object] = {
                "index": index,
                "release_s": release,
                "start_s": started,
                "end_s": ended,
                "latency_s": latency_s,
                "deadline_s": deadline_s,
                "deadline_class": "met" if deadline_met else "missed",
                "action_state": action_state,
                "action": list(action),
                "action_finite": bool(np.isfinite(action).all()),
                "prediction_error": prediction_error,
                "consecutive_deadline_misses": consecutive_misses,
                "consecutive_stale_ticks": consecutive_stale,
            }
            steady.append(row)
            if consecutive_misses >= policy.max_consecutive_deadline_misses or consecutive_stale >= policy.max_consecutive_stale_ticks:
                governed_stop = True
                stop_reason = "bounded_stale_or_deadline_policy"
                break
        deadline_misses = sum(row["deadline_class"] == "missed" for row in steady)
        result = {
            "rate_hz": rate_hz,
            "period_s": period_s,
            "deadline_s": deadline_s,
            "warmup_samples": warmup,
            "steady_samples": steady,
            "steady_sample_count": len(steady),
            "deadline_misses": deadline_misses,
            "deadline_met_count": len(steady) - deadline_misses,
            "stale_action_transition": transition_index is not None,
            "stale_action_transition_index": transition_index,
            "governed_stop": governed_stop,
            "governed_stop_reason": stop_reason,
        }
        rate_results.append(result)
    report: dict[str, object] = {
        "schema": DIAGNOSTIC_SCHEMA,
        "diagnostic_only": True,
        "diagnostic_kind": diagnostic_kind,
        "latency_performance_claim": latency_performance_claim,
        "probe_label": POLICY_PROBE_LABEL if diagnostic_kind == DIAGNOSTIC_KIND_POLICY_PROBE else None,
        "rates_hz": list(DIAGNOSTIC_RATES_HZ),
        "warmup_excluded": True,
        "checkpoint_sha256": checkpoint_hash,
        "dataset_sha256": dataset_hash,
        "source_receipt_sha256": source_hash,
        "code_source_hashes": dict(code_source_hashes_binding),
        "sampler_trace": list(REVERSE_STEP_TRACE),
        "sampler_steps": 50,
        "selection_eligible": False,
        "selected_rate_hz": None,
        "formal_rate_selection": False,
        "active_allowed": False,
        "active": False,
        "fixture_only": True,
        "production_promotion_allowed": False,
        "rate_results": rate_results,
        "policy": asdict(policy),
        "lifecycle_policy_probe": None,
    }
    return _artifact_with_hash(report)


def validate_diagnostic_artifact(
    artifact: Mapping[str, object],
    *,
    expected_checkpoint_sha256: str,
    expected_dataset_sha256: str,
    expected_source_receipt_sha256: str,
    expected_code_source_hashes: Mapping[str, str],
    require_lifecycle_probe: bool = False,
) -> dict[str, object]:
    _require_exact_keys(artifact, DIAGNOSTIC_FIELDS, "diagnostic artifact")
    _assert_portable_payload(artifact, context="diagnostic artifact")
    supplied = _require_sha256(artifact.get("artifact_sha256"), "diagnostic artifact_sha256")
    unsigned = dict(artifact)
    unsigned.pop("artifact_sha256", None)
    if canonical_sha256(unsigned) != supplied:
        raise QualificationContractError("diagnostic artifact hash mismatch")
    if artifact.get("schema") != DIAGNOSTIC_SCHEMA or artifact.get("diagnostic_only") is not True:
        raise QualificationContractError("diagnostic schema/boundary is invalid")
    kind = artifact.get("diagnostic_kind")
    if kind not in {DIAGNOSTIC_KIND_RUNTIME, DIAGNOSTIC_KIND_POLICY_PROBE}:
        raise QualificationContractError("diagnostic kind is invalid")
    if artifact.get("latency_performance_claim") is not (kind == DIAGNOSTIC_KIND_RUNTIME):
        raise QualificationContractError("diagnostic performance-claim boundary is invalid")
    expected_probe_label = POLICY_PROBE_LABEL if kind == DIAGNOSTIC_KIND_POLICY_PROBE else None
    if artifact.get("probe_label") != expected_probe_label:
        raise QualificationContractError("diagnostic probe label is invalid")
    if artifact.get("rates_hz") != list(DIAGNOSTIC_RATES_HZ) or artifact.get("sampler_trace") != list(REVERSE_STEP_TRACE) or artifact.get("sampler_steps") != 50:
        raise QualificationContractError("diagnostic sampler/rate contract is invalid")
    if artifact.get("selection_eligible") is not False or artifact.get("selected_rate_hz") is not None or artifact.get("formal_rate_selection") is not False:
        raise QualificationContractError("diagnostic cannot be promoted by rehashing fields")
    if artifact.get("active_allowed") is not False or artifact.get("active") is not False or artifact.get("fixture_only") is not True or artifact.get("production_promotion_allowed") is not False:
        raise QualificationContractError("diagnostic activation boundary is invalid")
    if _require_sha256(artifact.get("checkpoint_sha256"), "diagnostic checkpoint_sha256") != _require_sha256(expected_checkpoint_sha256, "expected_checkpoint_sha256"):
        raise QualificationContractError("diagnostic checkpoint binding mismatch")
    if _require_sha256(artifact.get("dataset_sha256"), "diagnostic dataset_sha256") != _require_sha256(expected_dataset_sha256, "expected_dataset_sha256"):
        raise QualificationContractError("diagnostic dataset binding mismatch")
    if _require_sha256(artifact.get("source_receipt_sha256"), "diagnostic source_receipt_sha256") != _require_sha256(expected_source_receipt_sha256, "expected_source_receipt_sha256"):
        raise QualificationContractError("diagnostic source receipt binding mismatch")
    if artifact.get("code_source_hashes") != dict(expected_code_source_hashes):
        raise QualificationContractError("diagnostic code source binding mismatch")
    if artifact.get("policy") != asdict(DiagnosticPolicy()):
        raise QualificationContractError("diagnostic policy is not the exact bounded policy")
    results = artifact.get("rate_results")
    if not isinstance(results, list) or len(results) != 2 or [item.get("rate_hz") for item in results if isinstance(item, Mapping)] != list(DIAGNOSTIC_RATES_HZ):
        raise QualificationContractError("diagnostic rate results are incomplete")
    previous_global_end: float | None = None
    policy = DiagnosticPolicy()
    for result_index, item in enumerate(results):
        _require_exact_keys(item, RATE_RESULT_FIELDS, f"diagnostic rate result {result_index}")
        rate_hz = _require_int(item.get("rate_hz"), f"diagnostic rate result {result_index}.rate_hz")
        expected_rate = DIAGNOSTIC_RATES_HZ[result_index]
        if rate_hz != expected_rate:
            raise QualificationContractError("diagnostic rate results are incomplete or out of order")
        period_s = _require_finite(item.get("period_s"), f"diagnostic {rate_hz} period_s", minimum=0.0)
        deadline_s = _require_finite(item.get("deadline_s"), f"diagnostic {rate_hz} deadline_s", minimum=0.0)
        if not math.isclose(period_s, 1.0 / rate_hz, rel_tol=0.0, abs_tol=1e-12) or not math.isclose(deadline_s, policy.deadline_fraction / rate_hz, rel_tol=0.0, abs_tol=1e-12):
            raise QualificationContractError("diagnostic period/deadline arithmetic is invalid")
        warmup = item.get("warmup_samples")
        steady = item.get("steady_samples")
        if not isinstance(warmup, list) or not warmup or not isinstance(steady, list) or not steady:
            raise QualificationContractError("diagnostic warmup/steady rows are missing")
        timeline: list[tuple[float, float, float]] = []
        for row_index, row in enumerate(warmup):
            _require_exact_keys(row, WARMUP_ROW_FIELDS, f"diagnostic {rate_hz} warmup row {row_index}")
            if _require_int(row.get("index"), "diagnostic warmup index") != row_index:
                raise QualificationContractError("diagnostic warmup indices are not deterministic")
            release = _require_finite(row.get("release_s"), "diagnostic warmup release_s", minimum=0.0)
            started = _require_finite(row.get("start_s"), "diagnostic warmup start_s", minimum=0.0)
            ended = _require_finite(row.get("end_s"), "diagnostic warmup end_s", minimum=0.0)
            latency = _require_finite(row.get("latency_s"), "diagnostic warmup latency_s", minimum=0.0)
            if started < release or ended < started or not math.isclose(latency, ended - started, rel_tol=0.0, abs_tol=1e-12) or row.get("deadline_class") != "warmup_excluded":
                raise QualificationContractError("diagnostic warmup timing/classification is inconsistent")
            _require_bool(row.get("action_finite"), "diagnostic warmup action_finite", True)
            _require_finite_vector(row.get("action"), ACTION_DIMENSION, "diagnostic warmup action")
            timeline.append((release, started, ended))
        last_action = (0.0,) * ACTION_DIMENSION
        previous_misses = 0
        previous_stale = 0
        transition_index: int | None = None
        for row_index, row in enumerate(steady):
            _require_exact_keys(row, STEADY_ROW_FIELDS, f"diagnostic {rate_hz} steady row {row_index}")
            if _require_int(row.get("index"), "diagnostic steady index") != row_index:
                raise QualificationContractError("diagnostic steady indices are not deterministic")
            release = _require_finite(row.get("release_s"), "diagnostic steady release_s", minimum=0.0)
            started = _require_finite(row.get("start_s"), "diagnostic steady start_s", minimum=0.0)
            ended = _require_finite(row.get("end_s"), "diagnostic steady end_s", minimum=0.0)
            latency = _require_finite(row.get("latency_s"), "diagnostic steady latency_s", minimum=0.0)
            row_deadline = _require_finite(row.get("deadline_s"), "diagnostic steady deadline_s", minimum=0.0)
            if not math.isclose(row_deadline, deadline_s, rel_tol=0.0, abs_tol=1e-12) or started < release or ended < started or not math.isclose(latency, ended - started, rel_tol=0.0, abs_tol=1e-12):
                raise QualificationContractError("diagnostic steady timing arithmetic is inconsistent")
            _require_bool(row.get("action_finite"), "diagnostic steady action_finite", True)
            action = _require_finite_vector(row.get("action"), ACTION_DIMENSION, "diagnostic steady action")
            prediction_error = row.get("prediction_error")
            if prediction_error is not None and (not isinstance(prediction_error, str) or not prediction_error):
                raise QualificationContractError("diagnostic prediction error classification is invalid")
            expected_met = prediction_error is None and latency <= deadline_s
            deadline_class = row.get("deadline_class")
            action_state = row.get("action_state")
            if deadline_class != ("met" if expected_met else "missed") or action_state != ("fresh" if expected_met else "stale"):
                raise QualificationContractError("diagnostic deadline/action classification is fabricated")
            misses = _require_int(row.get("consecutive_deadline_misses"), "diagnostic consecutive deadline misses", minimum=0)
            stale = _require_int(row.get("consecutive_stale_ticks"), "diagnostic consecutive stale ticks", minimum=0)
            if expected_met:
                if misses != 0 or stale != 0:
                    raise QualificationContractError("diagnostic fresh-row counters are inconsistent")
                last_action = action
            else:
                if misses != previous_misses + 1 or stale != previous_stale + 1 or action != last_action:
                    raise QualificationContractError("diagnostic stale-row transition/counters are inconsistent")
                if transition_index is None:
                    transition_index = row_index
            previous_misses = misses
            previous_stale = stale
            timeline.append((release, started, ended))
        if any(current[0] < previous[0] or current[1] < previous[1] or current[2] < previous[2] for previous, current in zip(timeline, timeline[1:])):
            raise QualificationContractError("diagnostic timestamps are not monotonic")
        reported_count = _require_int(item.get("steady_sample_count"), "diagnostic steady_sample_count", minimum=1)
        if reported_count != len(steady):
            raise QualificationContractError("diagnostic steady sample count is inconsistent")
        reported_misses = _require_int(item.get("deadline_misses"), "diagnostic deadline_misses", minimum=0)
        actual_misses = sum(row.get("deadline_class") == "missed" for row in steady)
        if reported_misses != actual_misses or item.get("deadline_met_count") != len(steady) - actual_misses:
            raise QualificationContractError("diagnostic deadline counts are inconsistent")
        transition = item.get("stale_action_transition")
        _require_bool(transition, "diagnostic stale_action_transition")
        if transition is not (transition_index is not None) or item.get("stale_action_transition_index") != transition_index:
            raise QualificationContractError("diagnostic stale transition receipt is inconsistent")
        governed_stop = item.get("governed_stop")
        _require_bool(governed_stop, "diagnostic governed_stop")
        if governed_stop:
            if item.get("governed_stop_reason") != "bounded_stale_or_deadline_policy" or (previous_misses < policy.max_consecutive_deadline_misses and previous_stale < policy.max_consecutive_stale_ticks):
                raise QualificationContractError("diagnostic governed stop reason/threshold is inconsistent")
        elif item.get("governed_stop_reason") is not None or previous_misses >= policy.max_consecutive_deadline_misses or previous_stale >= policy.max_consecutive_stale_ticks:
            raise QualificationContractError("diagnostic non-stop lifecycle receipt is inconsistent")
        if kind == DIAGNOSTIC_KIND_POLICY_PROBE and (not governed_stop or transition is not True):
            raise QualificationContractError("deterministic lifecycle-policy probe did not exercise governed stale stop")
    probe = artifact.get("lifecycle_policy_probe")
    if probe is not None:
        if not isinstance(probe, Mapping):
            raise QualificationContractError("diagnostic lifecycle-policy probe is invalid")
        validate_diagnostic_artifact(
            probe,
            expected_checkpoint_sha256=expected_checkpoint_sha256,
            expected_dataset_sha256=expected_dataset_sha256,
            expected_source_receipt_sha256=expected_source_receipt_sha256,
            expected_code_source_hashes=expected_code_source_hashes,
            require_lifecycle_probe=False,
        )
        if probe.get("diagnostic_kind") != DIAGNOSTIC_KIND_POLICY_PROBE:
            raise QualificationContractError("diagnostic lifecycle-policy probe is not clearly labeled")
    if require_lifecycle_probe and (not isinstance(probe, Mapping) or probe.get("diagnostic_kind") != DIAGNOSTIC_KIND_POLICY_PROBE):
        raise QualificationContractError("CUDA diagnostic evidence lacks the deterministic lifecycle-policy probe")
    return dict(artifact)


def build_deterministic_lifecycle_policy_probe(
    observations: np.ndarray,
    *,
    checkpoint_sha256: str,
    dataset_sha256: str,
    source_receipt_sha256: str,
    code_source_hashes_binding: Mapping[str, str],
) -> dict[str, object]:
    """Exercise lifecycle stop policy with virtual time, never claim performance."""

    virtual_now = [0.0]

    def clock() -> float:
        return virtual_now[0]

    def sleeper(duration: float) -> None:
        virtual_now[0] += max(0.0, float(duration))

    def deliberately_slow_predictor(_observation: np.ndarray, _seed: int) -> np.ndarray:
        virtual_now[0] += 0.02
        return np.zeros(ACTION_DIMENSION, dtype=np.float32)

    return run_paced_diagnostics(
        deliberately_slow_predictor,
        observations,
        checkpoint_sha256=checkpoint_sha256,
        dataset_sha256=dataset_sha256,
        source_receipt_sha256=source_receipt_sha256,
        code_source_hashes_binding=code_source_hashes_binding,
        warmup_samples=1,
        steady_samples=4,
        clock=clock,
        sleeper=sleeper,
        diagnostic_kind=DIAGNOSTIC_KIND_POLICY_PROBE,
        latency_performance_claim=False,
    )


def _candidate_source_text(source_text: str) -> str:
    replacements = (
        ("local baseline = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]", "local baseline = [800.0, 800.0, 800.0, 30.0, 30.0, 30.0]"),
        ("local fixed_k = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]", "local fixed_k = [800.0, 800.0, 800.0, 30.0, 30.0, 30.0]"),
        ("local fixed_d = [69.2820323, 69.2820323, 69.2820323, 4.898979486, 4.898979486, 4.898979486]", "local fixed_d = [80.0, 80.0, 80.0, 4.898979486, 4.898979486, 4.898979486]"),
        ("local last_stiffness = [600.0, 600.0, 600.0, 30.0, 30.0, 30.0]", "local last_stiffness = [800.0, 800.0, 800.0, 30.0, 30.0, 30.0]"),
    )
    result = source_text
    for old, new in replacements:
        if result.count(old) != 1:
            raise QualificationContractError(f"K=600 source semantic anchor count is not exactly one: {old}")
        result = result.replace(old, new, 1)
    return result


def _parse_source_vector(source_text: str, label: str) -> tuple[float, ...]:
    match = re.search(rf"{re.escape(label)} = \[([^\]]+)\]", source_text)
    if match is None:
        raise QualificationContractError(f"candidate source vector is missing: {label}")
    try:
        values = tuple(float(item.strip()) for item in match.group(1).split(","))
    except ValueError as exc:
        raise QualificationContractError(f"candidate source vector is not numeric: {label}") from exc
    if len(values) != 6 or not all(math.isfinite(value) for value in values):
        raise QualificationContractError(f"candidate source vector is invalid: {label}")
    return values


def build_k800_candidate_payload(
    *,
    repo_root: str | Path = REPO_ROOT,
    source_path: str | Path | None = None,
) -> tuple[dict[str, object], str, str]:
    repository = Path(repo_root)
    source = Path(source_path) if source_path is not None else repository / K600_SOURCE_RELATIVE_PATH
    if not source.is_file():
        raise QualificationContractError("K=600 Direct Torque source is missing")
    source_hash = _require_sha256(sha256_file(source), "K=600 source_sha256")
    if source_hash != EXPECTED_K600_SOURCE_SHA256:
        raise QualificationContractError("immutable K=600 source hash drifted")
    source_text = source.read_text(encoding="utf-8")
    candidate_text = _candidate_source_text(source_text)
    if _parse_source_vector(candidate_text, "local baseline") != K800_STIFFNESS or _parse_source_vector(candidate_text, "local fixed_k") != K800_STIFFNESS or _parse_source_vector(candidate_text, "local last_stiffness") != K800_STIFFNESS:
        raise QualificationContractError("K=800 candidate stiffness vectors are invalid")
    if _parse_source_vector(candidate_text, "local fixed_d") != K800_DAMPING:
        raise QualificationContractError("K=800 candidate derived damping is invalid")
    candidate_source_sha256 = hashlib.sha256(candidate_text.encode("utf-8")).hexdigest()
    candidate_filename = f"candidate-{candidate_source_sha256}.script"
    payload: dict[str, object] = {
        "schema": K800_SCHEMA,
        "candidate_id": "direct_torque_k800_inactive_v1",
        "source_binding": {
            "path": K600_SOURCE_RELATIVE_PATH,
            "sha256": source_hash,
            "content_addressing": "repo_relative_bytes_sha256_v1",
            "immutable": True,
        },
        "candidate_source": {
            "path": candidate_filename,
            "sha256": candidate_source_sha256,
        },
        "semantic_delta": ["translational_stiffness"],
        "baseline_stiffness_6d": list(K600_STIFFNESS),
        "candidate_stiffness_6d": list(K800_STIFFNESS),
        "derived_damping_6d": list(K800_DAMPING),
        "rotational_stiffness_unchanged": True,
        "damping_derivation": EXPECTED_DAMPING_DERIVATION,
        "unchanged_identities": list(EXPECTED_UNCHANGED_IDENTITIES),
        "current": False,
        "active": False,
        "active_allowed": False,
        "deployment_allowed": False,
        "live_qualified": False,
        "real_robot_qualification": "external_deferred",
        "current_pointer_touched": False,
        "fixture_only": True,
        "production_promotion_allowed": False,
        "reproduction_status": "not_claimed",
        "numeric_sanity_artifact": "numeric_sanity.json",
    }
    payload["candidate_sha256"] = canonical_sha256(payload)
    return payload, candidate_text, source_hash


def validate_k800_candidate(
    manifest_path: str | Path,
    *,
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, object]:
    manifest_file = Path(manifest_path)
    manifest = _read_json(manifest_file)
    _assert_portable_payload(manifest, context="K=800 manifest", repo_root=Path(repo_root))
    supplied_artifact = _require_sha256(manifest.get("artifact_sha256"), "K=800 artifact_sha256")
    unsigned = dict(manifest)
    unsigned.pop("artifact_sha256", None)
    if canonical_sha256(unsigned) != supplied_artifact:
        raise QualificationContractError("K=800 candidate manifest hash mismatch")
    expected_manifest_fields = frozenset(
        {
            "schema",
            "candidate_id",
            "source_binding",
            "candidate_source",
            "semantic_delta",
            "baseline_stiffness_6d",
            "candidate_stiffness_6d",
            "derived_damping_6d",
            "rotational_stiffness_unchanged",
            "damping_derivation",
            "unchanged_identities",
            "current",
            "active",
            "active_allowed",
            "deployment_allowed",
            "live_qualified",
            "real_robot_qualification",
            "current_pointer_touched",
            "fixture_only",
            "production_promotion_allowed",
            "reproduction_status",
            "numeric_sanity_artifact",
            "candidate_sha256",
            "artifact_sha256",
        }
    )
    _require_exact_keys(manifest, expected_manifest_fields, "K=800 candidate manifest")
    if manifest.get("schema") != K800_SCHEMA or manifest.get("candidate_id") != "direct_torque_k800_inactive_v1":
        raise QualificationContractError("K=800 candidate schema/identity is invalid")
    for field in ("current", "active", "active_allowed", "deployment_allowed", "live_qualified", "current_pointer_touched", "fixture_only", "production_promotion_allowed"):
        expected = False if field not in {"fixture_only"} else True
        if manifest.get(field) is not expected:
            raise QualificationContractError(f"K=800 candidate activation boundary is invalid: {field}")
    if manifest.get("real_robot_qualification") != "external_deferred" or manifest.get("reproduction_status") != "not_claimed":
        raise QualificationContractError("K=800 external qualification boundary is invalid")
    binding = manifest.get("source_binding")
    if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256", "content_addressing", "immutable"} or binding.get("path") != K600_SOURCE_RELATIVE_PATH or binding.get("content_addressing") != "repo_relative_bytes_sha256_v1" or binding.get("immutable") is not True:
        raise QualificationContractError("K=800 source binding is invalid")
    source_hash = _require_sha256(binding.get("sha256"), "K=800 source_binding.sha256")
    repository = Path(repo_root)
    source_path = repository / K600_SOURCE_RELATIVE_PATH
    if not source_path.is_file() or sha256_file(source_path) != EXPECTED_K600_SOURCE_SHA256 or source_hash != EXPECTED_K600_SOURCE_SHA256:
        raise QualificationContractError("immutable K=600 source was mutated or source hash drifted")
    candidate_info = manifest.get("candidate_source")
    if not isinstance(candidate_info, Mapping) or set(candidate_info) != {"path", "sha256"}:
        raise QualificationContractError("K=800 candidate source locator is invalid")
    candidate_filename = candidate_info.get("path")
    if not isinstance(candidate_filename, str) or re.fullmatch(r"candidate-[0-9a-f]{64}\.script", candidate_filename) is None:
        raise QualificationContractError("K=800 candidate source must be lowercase content-addressed")
    candidate_path = manifest_file.parent / candidate_filename
    candidate_hash = _require_sha256(candidate_info.get("sha256"), "K=800 candidate_source.sha256")
    if candidate_hash != candidate_filename.removeprefix("candidate-").removesuffix(".script") or not candidate_path.is_file() or sha256_file(candidate_path) != candidate_hash:
        raise QualificationContractError("K=800 candidate source content hash mismatch")
    candidate_address_payload = dict(unsigned)
    candidate_address_payload.pop("candidate_sha256", None)
    if manifest.get("candidate_sha256") != canonical_sha256(candidate_address_payload):
        raise QualificationContractError("K=800 candidate content address is invalid")
    expected_text = _candidate_source_text(source_path.read_text(encoding="utf-8"))
    if candidate_path.read_text(encoding="utf-8") != expected_text:
        raise QualificationContractError("K=800 candidate contains a second-variable source drift")
    if manifest.get("semantic_delta") != ["translational_stiffness"] or tuple(manifest.get("baseline_stiffness_6d", ())) != K600_STIFFNESS or tuple(manifest.get("candidate_stiffness_6d", ())) != K800_STIFFNESS or tuple(manifest.get("derived_damping_6d", ())) != K800_DAMPING or manifest.get("rotational_stiffness_unchanged") is not True:
        raise QualificationContractError("K=800 single-variable delta is invalid")
    if manifest.get("numeric_sanity_artifact") != "numeric_sanity.json":
        raise QualificationContractError("K=800 numeric-sanity locator is invalid")
    sanity_path = manifest_file.parent / "numeric_sanity.json"
    sanity = _read_json(sanity_path)
    _assert_portable_payload(sanity, context="K=800 numeric sanity", repo_root=Path(repo_root))
    _require_exact_keys(
        sanity,
        frozenset(
            {
                "schema",
                "candidate_sha256",
                "source_sha256",
                "candidate_source_sha256",
                "finite",
                "only_allowed_delta",
                "rotational_stiffness_unchanged",
                "translational_stiffness_delta",
                "rotational_stiffness",
                "damping_derivation",
                "guards_limits_path_routing_unchanged",
                "activation_or_current_promotion",
                "artifact_sha256",
            }
        ),
        "K=800 numeric-sanity artifact",
    )
    sanity_unsigned = dict(sanity)
    sanity_artifact_sha = _require_sha256(sanity_unsigned.pop("artifact_sha256", None), "K=800 numeric-sanity artifact_sha256")
    if canonical_sha256(sanity_unsigned) != sanity_artifact_sha:
        raise QualificationContractError("K=800 numeric-sanity artifact hash mismatch")
    if sanity.get("schema") != NUMERIC_SANITY_SCHEMA or sanity.get("candidate_sha256") != manifest.get("candidate_sha256") or sanity.get("source_sha256") != source_hash or sanity.get("candidate_source_sha256") != candidate_hash:
        raise QualificationContractError("K=800 numeric-sanity artifact is not source-bound")
    if sanity.get("finite") is not True or sanity.get("only_allowed_delta") is not True or sanity.get("rotational_stiffness_unchanged") is not True or sanity.get("damping_derivation") != EXPECTED_NUMERIC_DAMPING_DERIVATION or sanity.get("guards_limits_path_routing_unchanged") is not True or sanity.get("activation_or_current_promotion") is not False:
        raise QualificationContractError("K=800 numeric sanity failed")
    translational = sanity.get("translational_stiffness_delta")
    if not isinstance(translational, Mapping) or set(translational) != {"before", "after", "units"} or tuple(_require_finite_vector(translational.get("before"), 3, "K=800 numeric before")) != K600_STIFFNESS[:3] or tuple(_require_finite_vector(translational.get("after"), 3, "K=800 numeric after")) != K800_STIFFNESS[:3] or translational.get("units") != "N/m":
        raise QualificationContractError("K=800 translational numeric sanity is invalid")
    rotational = sanity.get("rotational_stiffness")
    if not isinstance(rotational, Mapping) or set(rotational) != {"value", "units"} or tuple(_require_finite_vector(rotational.get("value"), 3, "K=800 numeric rotational")) != K800_STIFFNESS[3:] or rotational.get("units") != "Nm/rad":
        raise QualificationContractError("K=800 rotational numeric sanity is invalid")
    if manifest.get("semantic_delta") != ["translational_stiffness"] or tuple(_require_finite_vector(manifest.get("baseline_stiffness_6d"), 6, "K=800 baseline stiffness")) != K600_STIFFNESS or tuple(_require_finite_vector(manifest.get("candidate_stiffness_6d"), 6, "K=800 candidate stiffness")) != K800_STIFFNESS or tuple(_require_finite_vector(manifest.get("derived_damping_6d"), 6, "K=800 damping")) != K800_DAMPING or manifest.get("rotational_stiffness_unchanged") is not True or manifest.get("damping_derivation") != EXPECTED_DAMPING_DERIVATION or manifest.get("unchanged_identities") != list(EXPECTED_UNCHANGED_IDENTITIES):
        raise QualificationContractError("K=800 canonical semantic fields are invalid")
    _validate_output_layout(
        manifest_file.parent,
        frozenset({manifest_file.name, candidate_filename, "numeric_sanity.json"}),
        "K=800 candidate",
    )
    return manifest


def materialize_k800_candidate(
    output_root: str | Path = DEFAULT_K800_ROOT,
    *,
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, object]:
    output = Path(output_root)
    payload, candidate_text, source_hash = build_k800_candidate_payload(repo_root=repo_root)
    output.mkdir(parents=True, exist_ok=True)
    candidate_filename = str(payload["candidate_source"]["path"])
    for child in output.iterdir():
        if child.is_file() and (child.name == "candidate.script" or re.fullmatch(r"candidate-[0-9a-f]{64}\.script", child.name)) and child.name != candidate_filename:
            child.unlink()
    candidate_path = output / candidate_filename
    _write_bytes_atomic(candidate_path, candidate_text.encode("utf-8"))
    manifest = _artifact_with_hash(payload)
    _write_json_atomic(output / "candidate.manifest.json", manifest)
    sanity = _artifact_with_hash({
        "schema": NUMERIC_SANITY_SCHEMA,
        "candidate_sha256": manifest["candidate_sha256"],
        "source_sha256": source_hash,
        "candidate_source_sha256": payload["candidate_source"]["sha256"],
        "finite": True,
        "only_allowed_delta": True,
        "rotational_stiffness_unchanged": True,
        "translational_stiffness_delta": {"before": list(K600_STIFFNESS[:3]), "after": list(K800_STIFFNESS[:3]), "units": "N/m"},
        "rotational_stiffness": {"value": list(K800_STIFFNESS[3:]), "units": "Nm/rad"},
        "damping_derivation": EXPECTED_NUMERIC_DAMPING_DERIVATION,
        "guards_limits_path_routing_unchanged": True,
        "activation_or_current_promotion": False,
    })
    _write_json_atomic(output / "numeric_sanity.json", sanity)
    return validate_k800_candidate(output / "candidate.manifest.json", repo_root=repo_root)


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _build_checkpoint_binding(
    fixture: FixtureDataset,
    contract_payload: Mapping[str, object],
    device: Mapping[str, object],
) -> dict[str, object]:
    contract_values = contract_payload.get("contract")
    if not isinstance(contract_values, Mapping):
        raise QualificationContractError("contract configuration is missing")
    selected = QualificationContract(**dict(contract_values))
    return {
        "schema": CHECKPOINT_BINDING_SCHEMA,
        "fixture_binding": fixture.bindings.as_dict(),
        "source_receipt_sha256": fixture.bindings.source_receipt_sha256,
        "split_sha256": fixture.bindings.split_sha256,
        "code_source_hashes": dict(contract_payload["code_source_hashes"]),
        "configuration": selected.as_dict(),
        "configuration_sha256": canonical_sha256(selected.as_dict()),
        "device": dict(device),
        "fixture_only": True,
        "formal_checkpoint": False,
        "active_allowed": False,
        "active": False,
        "reproduction_status": "not_claimed",
        "model_rate_selected_hz": None,
    }


def _validate_cuda_execution_receipt(execution: object) -> dict[str, object]:
    _require_exact_keys(execution, EXECUTION_FIELDS, "CUDA execution receipt")
    if not isinstance(execution, Mapping):
        raise QualificationContractError("CUDA execution receipt is invalid")
    threads = execution.get("environment_threads")
    if not isinstance(threads, Mapping) or set(threads) != {"OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"} or any(value != "1" for value in threads.values()):
        raise QualificationContractError("CUDA execution BLAS/OpenMP receipt is invalid")
    for field in ("torch_num_threads", "torch_num_interop_threads"):
        if _require_int(execution.get(field), f"CUDA execution {field}") != 1:
            raise QualificationContractError("CUDA execution is not single-threaded")
    for field in ("deterministic_algorithms_requested", "deterministic_algorithms_enabled"):
        _require_bool(execution.get(field), f"CUDA execution {field}", True)
    _require_bool(execution.get("deterministic_warn_only"), "CUDA execution deterministic_warn_only", False)
    if execution.get("unavoidable_cuda_nondeterminism") != []:
        raise QualificationContractError("CUDA execution contains an unbound nondeterminism claim")
    device = _require_exact_keys(execution.get("device"), DEVICE_FIELDS, "CUDA device receipt")
    if device.get("backend") != "cuda":
        raise QualificationContractError("CUDA evidence is not CUDA-only")
    device_index = _require_int(device.get("device_index"), "CUDA device_index", minimum=0)
    if not isinstance(device.get("device_name"), str) or not device["device_name"].strip() or not isinstance(device.get("torch_version"), str) or not device["torch_version"].strip() or not isinstance(device.get("platform"), str) or not device["platform"].strip():
        raise QualificationContractError("CUDA device identity is incomplete")
    capability = device.get("compute_capability")
    if not isinstance(capability, list) or len(capability) != 2 or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in capability):
        raise QualificationContractError("CUDA compute capability receipt is invalid")
    if _require_int(device.get("total_memory_bytes"), "CUDA device total memory", minimum=1) <= 0:
        raise QualificationContractError("CUDA device total memory is invalid")
    if execution.get("device_string") != f"cuda:{device_index}":
        raise QualificationContractError("CUDA device string receipt is invalid")
    return dict(device)


def _validate_memory_receipt(
    memory: object,
    *,
    expected_stage: str,
    contract: QualificationContract,
    device: Mapping[str, object],
) -> dict[str, object]:
    expected_fields = frozenset(
        {
            "stage",
            "peak_allocated_bytes",
            "peak_reserved_bytes",
            "device_total_memory_bytes",
            "peak_fraction",
            "limit_fraction_strictly_below",
            "below_85_percent",
        }
    )
    receipt = _require_exact_keys(memory, expected_fields, f"{expected_stage} memory receipt")
    if receipt.get("stage") != expected_stage:
        raise QualificationContractError("memory receipt stage is invalid")
    allocated = _require_int(receipt.get("peak_allocated_bytes"), "memory peak_allocated_bytes", minimum=0)
    reserved = _require_int(receipt.get("peak_reserved_bytes"), "memory peak_reserved_bytes", minimum=0)
    total = _require_int(receipt.get("device_total_memory_bytes"), "memory device_total_memory_bytes", minimum=1)
    if total != device.get("total_memory_bytes"):
        raise QualificationContractError("memory receipt is bound to the wrong CUDA device")
    fraction = _require_finite(receipt.get("peak_fraction"), "memory peak_fraction", minimum=0.0)
    expected_fraction = max(allocated, reserved) / total
    if not math.isclose(fraction, expected_fraction, rel_tol=0.0, abs_tol=1e-15) or fraction >= 0.85 or fraction >= contract.max_vram_fraction or receipt.get("limit_fraction_strictly_below") != contract.max_vram_fraction:
        raise QualificationContractError("CUDA VRAM receipt violates the strict bound")
    _require_bool(receipt.get("below_85_percent"), "memory below_85_percent", True)
    return dict(receipt)


def _load_checkpoint_cpu_payload(path: Path) -> dict[str, object]:
    if torch is None:
        raise QualificationContractError("CPU checkpoint metadata validation requires PyTorch")
    try:
        payload = torch.load(path, map_location="cpu", weights_only=False)
    except Exception as exc:
        raise QualificationContractError("checkpoint bytes cannot be loaded on CPU") from exc
    if not isinstance(payload, dict):
        raise QualificationContractError("checkpoint payload is not an object")
    return payload


def _validate_cuda_checkpoint_file(
    path: Path,
    receipt: Mapping[str, object],
    *,
    expected_binding: Mapping[str, object],
    fixture: FixtureDataset,
    contract: QualificationContract,
    device: Mapping[str, object],
    stage: str,
) -> dict[str, object]:
    if not path.is_file() or path.name not in {INITIAL_CHECKPOINT_NAME, RESUMED_CHECKPOINT_NAME}:
        raise QualificationContractError("CUDA checkpoint file is missing or ambiguously named")
    payload = _load_checkpoint_cpu_payload(path)
    expected_payload_fields = {
        "schema",
        "config",
        "model_state_dict",
        "normalization",
        "train_indices",
        "validation_indices",
        "test_indices",
        "losses",
        "validation_loss",
        "checkpoint_binding",
        "optimizer_state_dict",
        "generator_state",
        "epoch_completed",
    }
    if set(payload) != expected_payload_fields or payload.get("schema") != "ur10e_tacdiffusion_checkpoint/v3":
        raise QualificationContractError("CUDA checkpoint payload fields are missing or extra")
    model_config = contract.model_config().__dict__
    if payload.get("config") != model_config or payload.get("checkpoint_binding") != dict(expected_binding):
        raise QualificationContractError("CUDA checkpoint configuration/binding is not current")
    expected_train = [index for index, split in enumerate(fixture.splits) if split == "train"]
    expected_validation = [index for index, split in enumerate(fixture.splits) if split == "validation"]
    expected_test = [index for index, split in enumerate(fixture.splits) if split == "test"]
    if payload.get("train_indices") != expected_train or payload.get("validation_indices") != expected_validation or payload.get("test_indices") != expected_test:
        raise QualificationContractError("CUDA checkpoint split indices are not the frozen fixture split")
    losses = payload.get("losses")
    if not isinstance(losses, list) or not losses or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) for value in losses):
        raise QualificationContractError("CUDA checkpoint losses are invalid")
    expected_epochs = contract.train_epochs if stage == "initial" else contract.train_epochs + contract.resume_epochs
    if len(losses) != expected_epochs or payload.get("epoch_completed") != expected_epochs:
        raise QualificationContractError("CUDA checkpoint train-to-resume epoch state is invalid")
    if not isinstance(payload.get("optimizer_state_dict"), Mapping) or torch is None or not isinstance(payload.get("generator_state"), torch.Tensor):
        raise QualificationContractError("CUDA checkpoint resume state is missing")
    validate_checkpoint_binding(path, expected_binding, require_cuda=False)
    return payload


def _validate_checkpoint_runtime_result(
    result: object,
    *,
    stage: str,
    checkpoint_name: str,
    contract: QualificationContract,
    fixture: FixtureDataset,
    device: Mapping[str, object],
) -> dict[str, object]:
    expected_fields = {
        "checkpoint_path",
        "training_loss",
        "validation_loss",
        "train_count",
        "validation_count",
        "test_count",
        "model_schema",
        "runtime",
        "diffusion_steps",
    }
    if stage == "resumed":
        expected_fields.add("resume")
    payload = _require_exact_keys(result, frozenset(expected_fields), f"{stage} runtime result")
    if payload.get("checkpoint_path") != checkpoint_name or payload.get("model_schema") != "ur10e_tacdiffusion_checkpoint/v3" or payload.get("diffusion_steps") != 50 or not isinstance(payload.get("runtime"), str) or not payload["runtime"].startswith("cuda:"):
        raise QualificationContractError("CUDA runtime result is not CUDA-only or has the wrong checkpoint")
    losses = payload.get("training_loss")
    expected_loss_count = contract.train_epochs if stage == "initial" else contract.resume_epochs
    if not isinstance(losses, list) or len(losses) != expected_loss_count or any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) for value in losses):
        raise QualificationContractError("CUDA runtime training-loss receipt is invalid")
    if not isinstance(payload.get("validation_loss"), (int, float)) or isinstance(payload.get("validation_loss"), bool) or not math.isfinite(float(payload["validation_loss"])):
        raise QualificationContractError("CUDA runtime validation loss is invalid")
    if payload.get("train_count") != fixture.bindings.split_row_counts["train"] or payload.get("validation_count") != fixture.bindings.split_row_counts["validation"] or payload.get("test_count") != fixture.bindings.split_row_counts["test"]:
        raise QualificationContractError("CUDA runtime split counts are invalid")
    if stage == "resumed":
        resume = _require_exact_keys(payload.get("resume"), frozenset({"resumed", "source_checkpoint_path", "optimizer_state_restored", "generator_state_restored", "epoch_completed"}), "resume runtime state")
        if resume.get("resumed") is not True or resume.get("source_checkpoint_path") != INITIAL_CHECKPOINT_NAME or resume.get("optimizer_state_restored") is not True or resume.get("generator_state_restored") is not True or resume.get("epoch_completed") != contract.train_epochs + contract.resume_epochs:
            raise QualificationContractError("CUDA resume runtime state is not an exact continuation")
    return dict(payload)


def _validate_cuda_checkpoint_receipt(
    receipt: object,
    *,
    stage: str,
    path: Path,
    expected_binding: Mapping[str, object],
    fixture: FixtureDataset,
    contract: QualificationContract,
    device: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    expected_fields = set(CHECKPOINT_RECEIPT_BASE_FIELDS)
    expected_fields.add("train_result" if stage == "initial" else "resume_result")
    payload = _require_exact_keys(receipt, frozenset(expected_fields), f"{stage} checkpoint receipt")
    _assert_portable_payload(payload, context=f"{stage} checkpoint receipt")
    supplied = _require_sha256(payload.get("artifact_sha256"), f"{stage} checkpoint receipt artifact_sha256")
    unsigned = dict(payload)
    unsigned.pop("artifact_sha256", None)
    if canonical_sha256(unsigned) != supplied:
        raise QualificationContractError(f"{stage} checkpoint receipt hash mismatch")
    if payload.get("schema") != CHECKPOINT_BINDING_SCHEMA or payload.get("stage") != stage or payload.get("checkpoint_path") != path.name or payload.get("checkpoint_sha256") != sha256_file(path):
        raise QualificationContractError(f"{stage} checkpoint receipt binding is invalid")
    if payload.get("fixture_binding") != fixture.bindings.as_dict() or payload.get("source_receipt_sha256") != fixture.bindings.source_receipt_sha256 or payload.get("split_sha256") != fixture.bindings.split_sha256 or payload.get("code_source_hashes") != expected_binding.get("code_source_hashes") or payload.get("configuration") != contract.as_dict() or payload.get("device") != dict(device):
        raise QualificationContractError(f"{stage} checkpoint receipt lineage is invalid")
    for field, expected in (("fixture_only", True), ("formal_checkpoint", False), ("active_allowed", False), ("active", False), ("reproduction_status", "not_claimed"), ("model_rate_selected_hz", None)):
        if payload.get(field) != expected:
            raise QualificationContractError(f"{stage} checkpoint inactive boundary is invalid")
    runtime_key = "train_result" if stage == "initial" else "resume_result"
    runtime = _validate_checkpoint_runtime_result(
        payload.get(runtime_key),
        stage=stage,
        checkpoint_name=path.name,
        contract=contract,
        fixture=fixture,
        device=device,
    )
    memory = _validate_memory_receipt(payload.get("memory"), expected_stage="initial_train" if stage == "initial" else "resume_train", contract=contract, device=device)
    return runtime, memory


def _validate_cuda_evaluation(
    evaluation: object,
    *,
    resumed_sha256: str,
    expected_binding: Mapping[str, object],
    fixture: FixtureDataset,
    contract_payload: Mapping[str, object],
    contract: QualificationContract,
    device: Mapping[str, object],
) -> dict[str, object]:
    payload = _require_exact_keys(evaluation, EVALUATION_FIELDS, "CUDA evaluation artifact")
    _assert_portable_payload(payload, context="CUDA evaluation")
    supplied = _require_sha256(payload.get("artifact_sha256"), "evaluation artifact_sha256")
    unsigned = dict(payload)
    unsigned.pop("artifact_sha256", None)
    if canonical_sha256(unsigned) != supplied:
        raise QualificationContractError("evaluation artifact hash mismatch")
    if payload.get("schema") != "ur10e_tacdiffusion_evaluation/v2" or payload.get("checkpoint_path") != RESUMED_CHECKPOINT_NAME or payload.get("split") != "test" or payload.get("sample_count") != fixture.bindings.split_row_counts["test"] or payload.get("diffusion_steps") != 50 or payload.get("distinct_reverse_steps") != 50 or payload.get("reverse_step_trace") != list(REVERSE_STEP_TRACE) or payload.get("checkpoint_sha256") != resumed_sha256 or payload.get("dataset_sha256") != fixture.bindings.dataset_sha256 or payload.get("source_receipt_sha256") != fixture.bindings.source_receipt_sha256 or payload.get("code_source_hashes") != contract_payload.get("code_source_hashes"):
        raise QualificationContractError("CUDA evaluation lineage/sampler binding is invalid")
    if not isinstance(payload.get("normalized_action_mse"), (int, float)) or isinstance(payload.get("normalized_action_mse"), bool) or not math.isfinite(float(payload["normalized_action_mse"])) or payload.get("device") != f"cuda:{device['device_index']}" or payload.get("require_cuda") is not True or payload.get("checkpoint_binding") != dict(expected_binding):
        raise QualificationContractError("CUDA evaluation device/binding is invalid")
    for field, expected in (("fixture_only", True), ("formal_checkpoint", False), ("active_allowed", False), ("active", False), ("reproduction_status", "not_claimed"), ("model_rate_selected_hz", None)):
        if payload.get(field) != expected:
            raise QualificationContractError("CUDA evaluation activation boundary is invalid")
    _validate_memory_receipt(payload.get("memory"), expected_stage="evaluate_and_sampler", contract=contract, device=device)
    return dict(payload)


def validate_cuda_qualification_evidence(
    report_path: str | Path,
    *,
    bundle_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    repo_root: str | Path = REPO_ROOT,
) -> dict[str, object]:
    """Validate current CUDA evidence using CPU-only metadata reads."""

    path = Path(report_path)
    if path.name != CUDA_ARTIFACT_NAME:
        raise QualificationContractError("CUDA qualification artifact has an ambiguous filename")
    _validate_output_layout(path.parent, CUDA_OUTPUT_NAMES, "CUDA qualification")
    report = _read_json(path)
    _require_exact_keys(report, CUDA_REPORT_FIELDS, "CUDA qualification artifact")
    _assert_portable_payload(report, context="qualification.cuda", repo_root=Path(repo_root))
    supplied = _require_sha256(report.get("artifact_sha256"), "CUDA qualification artifact_sha256")
    unsigned = dict(report)
    unsigned.pop("artifact_sha256", None)
    if canonical_sha256(unsigned) != supplied:
        raise QualificationContractError("CUDA qualification artifact hash mismatch")
    if report.get("schema") != QUALIFICATION_SCHEMA or report.get("status") != "cuda_qualified_fixture_only" or report.get("cuda_execution_status") != CUDA_GATE_OPEN:
        raise QualificationContractError("CUDA qualification status is invalid")
    for field, expected in (("fixture_only", True), ("production_promotion_allowed", False), ("formal_checkpoint", False), ("active_allowed", False), ("active", False), ("reproduction_status", "not_claimed"), ("model_rate_selected_hz", None), ("selection_eligible", False), ("selected_rate_hz", None), ("formal_rate_selection", False), ("qualification_executed", True), ("no_cpu_fallback", True)):
        if report.get(field) != expected:
            raise QualificationContractError(f"CUDA qualification boundary is invalid: {field}")
    contract_path = path.parent / CONTRACT_ARTIFACT_NAME
    contract_payload = validate_contract_artifact(contract_path, bundle_root=bundle_root, repo_root=repo_root)
    if report.get("contract_artifact") != CONTRACT_ARTIFACT_NAME or report.get("contract_artifact_sha256") != _contract_file_sha256(contract_path):
        raise QualificationContractError("CUDA qualification is not bound to current contract bytes")
    fixture = validate_fixture_campaign(bundle_root, repo_root=repo_root)
    if report.get("fixture_binding") != fixture.bindings.as_dict() or report.get("code_source_hashes") != code_source_hashes(repo_root):
        raise QualificationContractError("CUDA qualification fixture/code binding drifted")
    contract_values = contract_payload.get("contract")
    if not isinstance(contract_values, Mapping):
        raise QualificationContractError("CUDA qualification contract values are missing")
    selected = QualificationContract(**dict(contract_values))
    device = _validate_cuda_execution_receipt(report.get("execution"))
    expected_binding = _build_checkpoint_binding(fixture, contract_payload, device)
    sampler_smoke = _require_exact_keys(report.get("sampler_smoke"), frozenset({"executed", "diffusion_steps", "distinct_reverse_steps", "reverse_step_trace"}), "CUDA sampler smoke")
    if sampler_smoke.get("executed") is not True or sampler_smoke.get("diffusion_steps") != 50 or sampler_smoke.get("distinct_reverse_steps") != 50 or sampler_smoke.get("reverse_step_trace") != list(REVERSE_STEP_TRACE):
        raise QualificationContractError("CUDA sampler smoke is incomplete")
    checkpoint_receipts = report.get("checkpoint_artifacts")
    if not isinstance(checkpoint_receipts, list) or len(checkpoint_receipts) != 2:
        raise QualificationContractError("CUDA qualification checkpoint receipts are incomplete")
    initial_receipt, resumed_receipt = checkpoint_receipts
    initial_path = path.parent / INITIAL_CHECKPOINT_NAME
    resumed_path = path.parent / RESUMED_CHECKPOINT_NAME
    initial_payload = _validate_cuda_checkpoint_file(initial_path, initial_receipt, expected_binding=expected_binding, fixture=fixture, contract=selected, device=device, stage="initial")
    resumed_payload = _validate_cuda_checkpoint_file(resumed_path, resumed_receipt, expected_binding=expected_binding, fixture=fixture, contract=selected, device=device, stage="resumed")
    if resumed_payload.get("losses", [])[: selected.train_epochs] != initial_payload.get("losses"):
        raise QualificationContractError("CUDA resume checkpoint does not preserve the exact train state")
    _validate_cuda_checkpoint_receipt(initial_receipt, stage="initial", path=initial_path, expected_binding=expected_binding, fixture=fixture, contract=selected, device=device)
    _validate_cuda_checkpoint_receipt(resumed_receipt, stage="resumed", path=resumed_path, expected_binding=expected_binding, fixture=fixture, contract=selected, device=device)
    if initial_receipt.get("train_result", {}).get("training_loss") != initial_payload.get("losses") or resumed_receipt.get("resume_result", {}).get("training_loss") != resumed_payload.get("losses", [])[selected.train_epochs:]:
        raise QualificationContractError("CUDA checkpoint receipts do not match train/resume loss state")
    evaluation_path = path.parent / EVALUATION_ARTIFACT_NAME
    evaluation = _read_json(evaluation_path)
    evaluated = _validate_cuda_evaluation(evaluation, resumed_sha256=sha256_file(resumed_path), expected_binding=expected_binding, fixture=fixture, contract_payload=contract_payload, contract=selected, device=device)
    if report.get("evaluation_artifacts") != [evaluated]:
        raise QualificationContractError("CUDA qualification evaluation receipt differs from current evaluation bytes")
    diagnostics_path = path.parent / DIAGNOSTICS_ARTIFACT_NAME
    diagnostics = _read_json(diagnostics_path)
    validated_diagnostics = validate_diagnostic_artifact(
        diagnostics,
        expected_checkpoint_sha256=sha256_file(resumed_path),
        expected_dataset_sha256=fixture.bindings.dataset_sha256,
        expected_source_receipt_sha256=fixture.bindings.source_receipt_sha256,
        expected_code_source_hashes=contract_payload["code_source_hashes"],
        require_lifecycle_probe=True,
    )
    if report.get("diagnostic_artifacts") != [validated_diagnostics]:
        raise QualificationContractError("CUDA qualification diagnostic receipt differs from current diagnostics bytes")
    return report


def run_cuda_qualification(
    bundle_root: str | Path = DEFAULT_CAMPAIGN_ROOT,
    *,
    output_root: str | Path = DEFAULT_QUALIFICATION_ROOT,
    repo_root: str | Path = REPO_ROOT,
    gate_open: bool = False,
    contract: QualificationContract | None = None,
) -> dict[str, object]:
    """Run the real one-worker CUDA cycle only after explicit gate opening."""

    if not gate_open:
        return build_gate_deferred_evidence(bundle_root, repo_root=repo_root, output_root=output_root, contract=contract)
    selected = contract or QualificationContract()
    fixture = validate_fixture_campaign(bundle_root, repo_root=repo_root)
    contract_payload = build_contract_payload(fixture, contract=selected, repo_root=repo_root)
    execution = _configure_cuda_execution(selected)
    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise QualificationContractError("CUDA qualification output must be empty; mixed or stale evidence is refused")
    device = execution["device"]
    if not isinstance(device, Mapping):
        raise QualificationContractError("CUDA execution device receipt is invalid")
    device_index = int(device["device_index"])
    torch.cuda.reset_peak_memory_stats(device_index)
    binding = _build_checkpoint_binding(fixture, contract_payload, device)
    initial_path = output / "fixture_checkpoint.initial.pt"
    resumed_path = output / "fixture_checkpoint.resumed.pt"
    train_result = train_mainline_model(
        fixture.observations,
        fixture.actions,
        fixture.splits,
        checkpoint_path=initial_path,
        config=selected.model_config(),
        epochs=selected.train_epochs,
        batch_size=selected.batch_size,
        learning_rate=selected.learning_rate,
        seed=selected.seed,
        checkpoint_binding=binding,
        require_cuda=True,
    )
    torch.cuda.synchronize(device_index)
    initial_memory = _vram_receipt(execution, "initial_train", selected)
    loaded_initial = load_mainline_checkpoint(initial_path, require_cuda=True)
    validate_checkpoint_binding(initial_path, binding, require_cuda=True)
    initial_receipt = _checkpoint_receipt(initial_path, stage="initial", fixture=fixture, contract_payload=contract_payload, execution=execution, extra={"train_result": train_result, "memory": initial_memory})
    resumed_result = resume_mainline_model(
        fixture.observations,
        fixture.actions,
        fixture.splits,
        checkpoint_path=initial_path,
        resumed_checkpoint_path=resumed_path,
        epochs=selected.resume_epochs,
        batch_size=selected.batch_size,
        learning_rate=selected.learning_rate,
        seed=selected.seed,
        checkpoint_binding=binding,
        require_cuda=True,
    )
    torch.cuda.synchronize(device_index)
    resumed_memory = _vram_receipt(execution, "resume_train", selected)
    loaded_resumed = load_mainline_checkpoint(resumed_path, require_cuda=True)
    validate_checkpoint_binding(resumed_path, binding, require_cuda=True)
    resumed_receipt = _checkpoint_receipt(resumed_path, stage="resumed", fixture=fixture, contract_payload=contract_payload, execution=execution, extra={"resume_result": resumed_result, "memory": resumed_memory})
    evaluation = evaluate_mainline_checkpoint(
        resumed_path,
        fixture.observations,
        fixture.actions,
        fixture.splits,
        split="test",
        require_cuda=True,
        checkpoint_binding=binding,
        seed=selected.seed,
    )
    torch.cuda.synchronize(device_index)
    evaluation_memory = _vram_receipt(execution, "evaluate_and_sampler", selected)
    evaluation["memory"] = evaluation_memory
    evaluation = _portableize_receipt(evaluation)  # type: ignore[assignment]
    if not isinstance(evaluation, dict):
        raise QualificationContractError("evaluation receipt could not be made portable")
    evaluation["checkpoint_sha256"] = resumed_receipt["checkpoint_sha256"]
    evaluation["dataset_sha256"] = fixture.bindings.dataset_sha256
    evaluation["source_receipt_sha256"] = fixture.bindings.source_receipt_sha256
    evaluation["code_source_hashes"] = dict(contract_payload["code_source_hashes"])
    evaluation = _artifact_with_hash(evaluation)
    predictor = _loaded_model_predictor(loaded_resumed["model"], loaded_resumed["normalization"], seed=selected.seed)
    diagnostics = run_paced_diagnostics(
        predictor,
        fixture.observations,
        checkpoint_sha256=str(resumed_receipt["checkpoint_sha256"]),
        dataset_sha256=fixture.bindings.dataset_sha256,
        source_receipt_sha256=fixture.bindings.source_receipt_sha256,
        code_source_hashes_binding=contract_payload["code_source_hashes"],
        sampler_trace=evaluation["reverse_step_trace"],
        warmup_samples=1,
        steady_samples=4,
    )
    lifecycle_probe = build_deterministic_lifecycle_policy_probe(
        fixture.observations,
        checkpoint_sha256=str(resumed_receipt["checkpoint_sha256"]),
        dataset_sha256=fixture.bindings.dataset_sha256,
        source_receipt_sha256=fixture.bindings.source_receipt_sha256,
        code_source_hashes_binding=contract_payload["code_source_hashes"],
    )
    diagnostics["lifecycle_policy_probe"] = lifecycle_probe
    diagnostics = _artifact_with_hash(diagnostics)
    contract_file_sha256 = _serialized_json_sha256(contract_payload)
    report = {
        "schema": QUALIFICATION_SCHEMA,
        "status": "cuda_qualified_fixture_only",
        "cuda_execution_status": CUDA_GATE_OPEN,
        "fixture_only": True,
        "production_promotion_allowed": False,
        "formal_checkpoint": False,
        "active_allowed": False,
        "active": False,
        "reproduction_status": "not_claimed",
        "model_rate_selected_hz": None,
        "selection_eligible": False,
        "selected_rate_hz": None,
        "formal_rate_selection": False,
        "qualification_executed": True,
        "no_cpu_fallback": True,
        "execution": execution,
        "fixture_binding": fixture.bindings.as_dict(),
        "code_source_hashes": dict(contract_payload["code_source_hashes"]),
        "contract_artifact": CONTRACT_ARTIFACT_NAME,
        "contract_artifact_sha256": contract_file_sha256,
        "checkpoint_artifacts": [initial_receipt, resumed_receipt],
        "evaluation_artifacts": [evaluation],
        "diagnostic_artifacts": [diagnostics],
        "sampler_smoke": {
            "executed": True,
            "diffusion_steps": 50,
            "distinct_reverse_steps": 50,
            "reverse_step_trace": list(REVERSE_STEP_TRACE),
        },
    }
    report = _artifact_with_hash(report)
    _write_json_atomic(output / "qualification.contract.json", contract_payload)
    _write_json_atomic(output / "evaluation.json", evaluation)
    _write_json_atomic(output / "diagnostics.json", diagnostics)
    _write_json_atomic(output / "qualification.cuda.json", report)
    return report


__all__ = [
    "ACTION_DIMENSION",
    "CAMPAIGN_DATASET_SCHEMA",
    "CONTRACT_SCHEMA",
    "CONTRACT_ARTIFACT_NAME",
    "CUDA_ARTIFACT_NAME",
    "CUDA_GATE_DEFERRED",
    "DIAGNOSTIC_RATES_HZ",
    "DEFERRED_ARTIFACT_NAME",
    "DiagnosticPolicy",
    "DIAGNOSTICS_ARTIFACT_NAME",
    "EVALUATION_ARTIFACT_NAME",
    "EXPECTED_BUNDLE_DIGEST",
    "EXPECTED_DATASET_SHA256",
    "EXPECTED_K600_SOURCE_SHA256",
    "FixtureBindings",
    "FixtureDataset",
    "K600_SOURCE_RELATIVE_PATH",
    "K600_STIFFNESS",
    "K800_DAMPING",
    "K800_SCHEMA",
    "K800_STIFFNESS",
    "QualificationContract",
    "QualificationContractError",
    "REVERSE_STEP_TRACE",
    "RESUMED_CHECKPOINT_NAME",
    "INITIAL_CHECKPOINT_NAME",
    "build_deterministic_lifecycle_policy_probe",
    "build_contract_payload",
    "build_gate_deferred_evidence",
    "build_k800_candidate_payload",
    "canonical_sha256",
    "code_source_hashes",
    "materialize_k800_candidate",
    "run_cuda_qualification",
    "run_paced_diagnostics",
    "sha256_file",
    "validate_checkpoint_binding",
    "validate_contract_artifact",
    "validate_cuda_qualification_evidence",
    "validate_diagnostic_artifact",
    "validate_fixture_campaign",
    "validate_gate_deferred_evidence",
    "validate_k800_candidate",
]
