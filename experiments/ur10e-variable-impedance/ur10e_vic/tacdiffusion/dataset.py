"""Portable, episode-grouped NPZ datasets for UR10e force diffusion.

The formal training contract admits only newly recorded UR10e expert
demonstrations with real ``F_ff`` labels.  Legacy v27/v29 traces remain useful
for observation-pipeline replay, but this module deliberately cannot promote
them into labelled training data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import numpy as np

from .contracts import (
    CONDITION_DIMENSION,
    CONTROL_RATE_HZ,
    PERMITTED_PROGRAM_CLAIM,
)
from .expert_data import ExpertTraceManifestV2, validate_episode_manifest_artifacts


DATASET_SCHEMA_VERSION = 2
ACTION_DIMENSION = 6
FORMAL_SOURCE_KIND = "ur10e_expert_demonstration"
LEGACY_PIPELINE_SOURCE_KINDS = frozenset(
    {"legacy_v27_replay", "legacy_v29_replay"}
)
SPLIT_NAMES = ("train", "validation", "test")
CONDITION_ORDER = (
    "current_external_wrench,current_internal_wrench,current_ee_twist,"
    "previous_external_wrench,previous_internal_wrench,previous_ee_twist"
)


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: object, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _scalar(array: np.ndarray, name: str) -> object:
    if array.shape != ():
        raise ValueError(f"{name} must be an NPZ scalar")
    return array.item()


@dataclass(frozen=True)
class ForceDataset:
    condition: np.ndarray
    expert_f_ff: np.ndarray
    episode_id: tuple[str, ...]
    split: tuple[str, ...]
    source_kind: str
    canonical_frame_id: str
    frame_calibration_sha256: str
    trace_manifest_sha256_by_sample: tuple[str, ...]
    dataset_path: Path

    @property
    def sample_count(self) -> int:
        return int(self.condition.shape[0])

    @property
    def episode_count(self) -> int:
        return len(set(self.episode_id))

    def indices_for_split(self, split: str) -> np.ndarray:
        if split not in SPLIT_NAMES:
            raise ValueError("split must be train, validation, or test")
        return np.asarray(
            [index for index, value in enumerate(self.split) if value == split],
            dtype=np.int64,
        )


def assign_episode_grouped_splits(
    episode_ids: Iterable[str],
    *,
    train_fraction: float = 0.7,
    validation_fraction: float = 0.15,
    seed: int = 42,
) -> tuple[str, ...]:
    """Assign a split once per episode using a stable hash ordering."""

    episodes = tuple(str(value) for value in episode_ids)
    if not episodes or any(not value.strip() for value in episodes):
        raise ValueError("episode_ids must be non-empty strings")
    if (
        not math.isfinite(train_fraction)
        or not math.isfinite(validation_fraction)
        or train_fraction <= 0.0
        or validation_fraction < 0.0
        or train_fraction + validation_fraction >= 1.0
    ):
        raise ValueError("split fractions must leave positive train and test shares")
    unique = sorted(set(episodes))
    ordered = sorted(
        unique,
        key=lambda episode: hashlib.sha256(
            f"{seed}:{episode}".encode("utf-8")
        ).digest(),
    )
    count = len(ordered)
    train_count = max(1, int(round(count * train_fraction)))
    validation_count = int(round(count * validation_fraction))
    if count >= 3:
        validation_count = max(1, validation_count)
        train_count = min(train_count, count - validation_count - 1)
    else:
        train_count = min(train_count, count)
        validation_count = min(validation_count, count - train_count)
    by_episode: dict[str, str] = {}
    for index, episode in enumerate(ordered):
        if index < train_count:
            by_episode[episode] = "train"
        elif index < train_count + validation_count:
            by_episode[episode] = "validation"
        else:
            by_episode[episode] = "test"
    return tuple(by_episode[episode] for episode in episodes)


def _validate_arrays(
    condition: np.ndarray,
    expert_f_ff: np.ndarray,
    episode_id: Sequence[object],
    split: Sequence[object],
) -> tuple[np.ndarray, np.ndarray, tuple[str, ...], tuple[str, ...]]:
    conditions = np.asarray(condition, dtype=np.float32)
    actions = np.asarray(expert_f_ff, dtype=np.float32)
    if conditions.ndim != 2 or conditions.shape[1] != CONDITION_DIMENSION:
        raise ValueError("condition must have shape [N, 36]")
    if actions.shape != (conditions.shape[0], ACTION_DIMENSION):
        raise ValueError("expert_f_ff must have shape [N, 6]")
    if conditions.shape[0] == 0:
        raise ValueError("dataset must contain at least one sample")
    if not np.isfinite(conditions).all() or not np.isfinite(actions).all():
        raise ValueError("condition and expert_f_ff must be finite")
    episodes = tuple(str(value) for value in episode_id)
    splits = tuple(str(value) for value in split)
    if len(episodes) != conditions.shape[0] or len(splits) != conditions.shape[0]:
        raise ValueError("episode_id and split must contain N values")
    if any(not value.strip() for value in episodes):
        raise ValueError("episode_id values must be non-empty")
    if any(value not in SPLIT_NAMES for value in splits):
        raise ValueError("split values must be train, validation, or test")
    episode_splits: dict[str, str] = {}
    for episode, sample_split in zip(episodes, splits):
        prior = episode_splits.setdefault(episode, sample_split)
        if prior != sample_split:
            raise ValueError("frame leakage: one episode appears in multiple splits")
    if "train" not in splits:
        raise ValueError("formal dataset requires at least one training episode")
    return conditions, actions, episodes, splits


def write_expert_dataset_npz(
    output_path: str | Path,
    *,
    condition: np.ndarray,
    expert_f_ff: np.ndarray,
    episode_id: Sequence[object],
    split: Sequence[object],
    canonical_frame_id: str,
    frame_calibration_sha256: str,
    expert_trace_manifest_paths: str | Path | Sequence[str | Path],
    source_kind: str = FORMAL_SOURCE_KIND,
) -> Path:
    """Write a formal dataset from independently hash-bound trace manifests."""

    if source_kind in LEGACY_PIPELINE_SOURCE_KINDS:
        raise ValueError("legacy v27/v29 replay cannot generate expert F_ff labels")
    if source_kind != FORMAL_SOURCE_KIND:
        raise ValueError("formal datasets require ur10e_expert_demonstration source")
    if not str(canonical_frame_id).strip():
        raise ValueError("canonical_frame_id must be non-empty")
    calibration = _require_sha256(
        frame_calibration_sha256, "frame_calibration_sha256"
    )
    conditions, actions, episodes, splits = _validate_arrays(
        condition, expert_f_ff, episode_id, split
    )
    trace_artifacts = _load_trace_manifest_artifacts(expert_trace_manifest_paths)
    if set(trace_artifacts) != set(episodes):
        raise ValueError("expert trace manifests must match dataset episode_id values")
    sample_count_by_episode = Counter(episodes)
    trace_hash_by_sample: list[str] = []
    for episode, sample_split in zip(episodes, splits):
        trace_manifest, trace_sha256, _ = trace_artifacts[episode]
        if trace_manifest.dataset_split != sample_split:
            raise ValueError("expert trace manifest split does not match dataset split")
        if trace_manifest.canonical_frame_id != canonical_frame_id:
            raise ValueError("expert trace manifest canonical frame mismatch")
        if trace_manifest.frame_calibration_sha256 != calibration:
            raise ValueError("expert trace manifest calibration mismatch")
        if trace_manifest.sample_count != sample_count_by_episode[episode]:
            raise ValueError("expert trace manifest sample count mismatch")
        trace_hash_by_sample.append(trace_sha256)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle,
            condition=conditions,
            expert_f_ff=actions,
            episode_id=np.asarray(episodes, dtype=np.str_),
            split=np.asarray(splits, dtype=np.str_),
            schema_version=np.asarray(DATASET_SCHEMA_VERSION, dtype=np.int64),
            source_kind=np.asarray(source_kind),
            canonical_frame_id=np.asarray(canonical_frame_id),
            frame_calibration_sha256=np.asarray(calibration),
            trace_manifest_sha256=np.asarray(trace_hash_by_sample, dtype=np.str_),
            condition_order=np.asarray(CONDITION_ORDER),
            control_rate_hz=np.asarray(CONTROL_RATE_HZ, dtype=np.int64),
            expert_labels_verified=np.asarray(True, dtype=np.bool_),
            vision_included=np.asarray(False, dtype=np.bool_),
        )
    temporary.replace(destination)
    return destination


def load_expert_dataset_npz(path: str | Path) -> ForceDataset:
    dataset_path = Path(path)
    try:
        with np.load(dataset_path, allow_pickle=False) as archive:
            required = {
                "condition",
                "expert_f_ff",
                "episode_id",
                "split",
                "schema_version",
                "source_kind",
                "canonical_frame_id",
                "frame_calibration_sha256",
                "trace_manifest_sha256",
                "condition_order",
                "control_rate_hz",
                "expert_labels_verified",
                "vision_included",
            }
            if not required <= set(archive.files):
                missing = sorted(required - set(archive.files))
                raise ValueError(f"dataset is missing required fields: {missing}")
            schema_version = int(_scalar(archive["schema_version"], "schema_version"))
            source_kind = str(_scalar(archive["source_kind"], "source_kind"))
            canonical_frame_id = str(
                _scalar(archive["canonical_frame_id"], "canonical_frame_id")
            )
            calibration = str(
                _scalar(
                    archive["frame_calibration_sha256"],
                    "frame_calibration_sha256",
                )
            )
            trace_manifest_hashes = tuple(
                str(value) for value in archive["trace_manifest_sha256"]
            )
            condition_order = str(
                _scalar(archive["condition_order"], "condition_order")
            )
            control_rate_hz = int(
                _scalar(archive["control_rate_hz"], "control_rate_hz")
            )
            labels_verified = bool(
                _scalar(archive["expert_labels_verified"], "expert_labels_verified")
            )
            vision_included = bool(
                _scalar(archive["vision_included"], "vision_included")
            )
            conditions, actions, episodes, splits = _validate_arrays(
                archive["condition"],
                archive["expert_f_ff"],
                archive["episode_id"],
                archive["split"],
            )
    except (OSError, ValueError) as error:
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"cannot read TacDiffusion dataset: {error}") from error
    if schema_version != DATASET_SCHEMA_VERSION:
        raise ValueError("unsupported TacDiffusion dataset schema")
    if source_kind in LEGACY_PIPELINE_SOURCE_KINDS:
        raise ValueError("legacy v27/v29 replay cannot contain expert F_ff labels")
    if source_kind != FORMAL_SOURCE_KIND or not labels_verified:
        raise ValueError("dataset does not contain verified UR10e expert F_ff labels")
    if not canonical_frame_id.strip():
        raise ValueError("canonical_frame_id must be non-empty")
    _require_sha256(calibration, "frame_calibration_sha256")
    if len(trace_manifest_hashes) != conditions.shape[0]:
        raise ValueError("trace_manifest_sha256 must contain N artifact hashes")
    for trace_hash in trace_manifest_hashes:
        _require_sha256(trace_hash, "trace_manifest_sha256")
    if condition_order != CONDITION_ORDER:
        raise ValueError("condition feature order does not match the 36D contract")
    if control_rate_hz != CONTROL_RATE_HZ:
        raise ValueError("dataset control rate must be 500 Hz")
    if vision_included:
        raise ValueError("force-domain dataset must not contain vision features")
    conditions.setflags(write=False)
    actions.setflags(write=False)
    return ForceDataset(
        condition=conditions,
        expert_f_ff=actions,
        episode_id=episodes,
        split=splits,
        source_kind=source_kind,
        canonical_frame_id=canonical_frame_id,
        frame_calibration_sha256=calibration,
        trace_manifest_sha256_by_sample=trace_manifest_hashes,
        dataset_path=dataset_path,
    )


def dataset_statistics(dataset: ForceDataset) -> dict[str, object]:
    condition = np.asarray(dataset.condition, dtype=np.float64)
    action = np.asarray(dataset.expert_f_ff, dtype=np.float64)
    return {
        "condition_mean": condition.mean(axis=0).tolist(),
        "condition_std": condition.std(axis=0).tolist(),
        "expert_f_ff_mean": action.mean(axis=0).tolist(),
        "expert_f_ff_std": action.std(axis=0).tolist(),
    }


def _load_trace_manifest_artifact(
    path: str | Path,
) -> tuple[ExpertTraceManifestV2, str, Path]:
    artifact_path = Path(path)
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("expert trace manifest must be a JSON object")
    declared_fingerprint = payload.pop("fingerprint_sha256", None)
    if "manifest" in payload:
        if set(payload) != {"manifest"} or not isinstance(payload["manifest"], dict):
            raise ValueError("expert trace manifest wrapper is invalid")
        payload = dict(payload["manifest"])
    manifest = ExpertTraceManifestV2(**payload)
    if declared_fingerprint is not None and declared_fingerprint != manifest.fingerprint_sha256:
        raise ValueError("expert trace manifest fingerprint mismatch")
    validate_episode_manifest_artifacts(manifest, artifact_path)
    return manifest, sha256_file(artifact_path), artifact_path


def _load_trace_manifest_artifacts(
    paths: str | Path | Sequence[str | Path],
) -> dict[str, tuple[ExpertTraceManifestV2, str, Path]]:
    normalized = (paths,) if isinstance(paths, (str, Path)) else tuple(paths)
    if not normalized:
        raise ValueError("at least one expert trace manifest artifact is required")
    result: dict[str, tuple[ExpertTraceManifestV2, str, Path]] = {}
    for path in normalized:
        manifest, artifact_hash, artifact_path = _load_trace_manifest_artifact(path)
        if manifest.trace_id in result:
            raise ValueError("duplicate trace_id in expert trace manifests")
        result[manifest.trace_id] = (manifest, artifact_hash, artifact_path)
    return result


def _validate_bound_trace_artifacts(
    dataset: ForceDataset,
    paths: str | Path | Sequence[str | Path],
) -> list[dict[str, str]]:
    artifacts = _load_trace_manifest_artifacts(paths)
    if set(artifacts) != set(dataset.episode_id):
        raise ValueError("expert trace artifacts do not cover dataset episodes")
    expected_by_episode = {
        episode: trace_hash
        for episode, trace_hash in zip(
            dataset.episode_id, dataset.trace_manifest_sha256_by_sample
        )
    }
    sample_count_by_episode = Counter(dataset.episode_id)
    bindings: list[dict[str, str]] = []
    seen_artifact_names: set[str] = set()
    for episode in sorted(artifacts):
        manifest, artifact_hash, artifact_path = artifacts[episode]
        if artifact_hash != expected_by_episode[episode]:
            raise ValueError("expert trace manifest artifact hash mismatch")
        episode_splits = {
            sample_split
            for sample_episode, sample_split in zip(
                dataset.episode_id, dataset.split
            )
            if sample_episode == episode
        }
        if episode_splits != {manifest.dataset_split}:
            raise ValueError("expert trace manifest split binding mismatch")
        if manifest.canonical_frame_id != dataset.canonical_frame_id:
            raise ValueError("expert trace manifest frame binding mismatch")
        if manifest.frame_calibration_sha256 != dataset.frame_calibration_sha256:
            raise ValueError("expert trace manifest calibration binding mismatch")
        if manifest.sample_count != sample_count_by_episode[episode]:
            raise ValueError("expert trace manifest sample-count binding mismatch")
        if artifact_path.name in seen_artifact_names:
            raise ValueError("trace manifest artifact basenames must be unique")
        seen_artifact_names.add(artifact_path.name)
        bindings.append(
            {
                "artifact": artifact_path.name,
                "sha256": artifact_hash,
                "trace_id": episode,
            }
        )
    return bindings


def build_dataset_manifest(
    dataset_path: str | Path,
    expert_trace_manifest_paths: str | Path | Sequence[str | Path],
) -> dict[str, object]:
    dataset = load_expert_dataset_npz(dataset_path)
    trace_artifact_bindings = _validate_bound_trace_artifacts(
        dataset, expert_trace_manifest_paths
    )
    stats = dataset_statistics(dataset)
    split_sample_counts = {
        name: int(sum(value == name for value in dataset.split)) for name in SPLIT_NAMES
    }
    split_episode_counts = {
        name: len(
            {
                episode
                for episode, sample_split in zip(dataset.episode_id, dataset.split)
                if sample_split == name
            }
        )
        for name in SPLIT_NAMES
    }
    manifest: dict[str, object] = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_artifact": dataset.dataset_path.name,
        "dataset_sha256": sha256_file(dataset.dataset_path),
        "sample_count": dataset.sample_count,
        "episode_count": dataset.episode_count,
        "condition_shape": [dataset.sample_count, CONDITION_DIMENSION],
        "action_shape": [dataset.sample_count, ACTION_DIMENSION],
        "condition_order": CONDITION_ORDER,
        "source_kind": dataset.source_kind,
        "canonical_frame_id": dataset.canonical_frame_id,
        "frame_calibration_sha256": dataset.frame_calibration_sha256,
        "trace_manifest_artifacts": trace_artifact_bindings,
        "control_rate_hz": CONTROL_RATE_HZ,
        "split_sample_counts": split_sample_counts,
        "split_episode_counts": split_episode_counts,
        "episode_grouped_split_verified": True,
        "frame_leakage_detected": False,
        "expert_labels_verified": True,
        "vision_included": False,
        "training_eligible": True,
        "permitted_program_claim": PERMITTED_PROGRAM_CLAIM,
        "statistics": stats,
        "statistics_sha256": _canonical_sha256(stats),
    }
    manifest["manifest_payload_sha256"] = _canonical_sha256(manifest)
    return manifest


def write_dataset_manifest(
    dataset_path: str | Path,
    manifest_path: str | Path,
    expert_trace_manifest_paths: str | Path | Sequence[str | Path],
) -> dict[str, object]:
    manifest = build_dataset_manifest(dataset_path, expert_trace_manifest_paths)
    destination = Path(manifest_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return manifest


def validate_dataset_manifest(
    dataset_path: str | Path,
    manifest_path: str | Path,
    expert_trace_manifest_paths: str | Path | Sequence[str | Path],
) -> dict[str, object]:
    candidate = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    expected = build_dataset_manifest(dataset_path, expert_trace_manifest_paths)
    if candidate != expected:
        raise ValueError("dataset manifest does not match the current NPZ artifact")
    return expected
