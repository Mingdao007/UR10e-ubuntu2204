"""Strict durable 84D/12D mainline dataset and frozen episode split manifest."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Mapping, Sequence

import numpy as np


DATASET_SCHEMA = "ur10e_tacdiffusion_dataset/v3"
OBSERVATION_DIMENSION = 84
ACTION_DIMENSION = 12
ALLOWED_SPLITS = frozenset({"train", "validation", "test"})
NPZ_FIELDS = frozenset({"observations", "actions", "episode_ids", "splits", "timestamps_s"})
MANIFEST_FIELDS = frozenset({
    "schema",
    "observation_dimension",
    "action_dimension",
    "dataset_artifact",
    "dataset_sha256",
    "row_count",
    "episode_count",
    "episode_order",
    "episode_split",
    "split_row_counts",
    "split_episode_counts",
    "source_raw_artifact_hashes",
    "surface_calibration_sha256",
    "action_profile_sha256",
    "filter_profile_sha256",
    "normalization_sha256",
    "split_policy",
    "manifest_sha256",
})


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha(value: object, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


def _payload_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _fsync_parent(path: Path) -> None:
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _write_npz_atomic(path: Path, arrays: Mapping[str, np.ndarray]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w+b", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _validate_rows(
    observations: np.ndarray,
    actions: np.ndarray,
    episode_ids: np.ndarray,
    splits: np.ndarray,
    timestamps: np.ndarray,
) -> tuple[dict[str, str], dict[str, int], dict[str, int], list[str]]:
    row_count = observations.shape[0]
    if row_count == 0:
        raise ValueError("mainline dataset must be nonempty")
    if observations.ndim != 2 or observations.shape != (row_count, OBSERVATION_DIMENSION):
        raise ValueError("mainline observations must be [N,84]")
    if actions.ndim != 2 or actions.shape != (row_count, ACTION_DIMENSION):
        raise ValueError("mainline actions must be [N,12]")
    if episode_ids.ndim != 1 or splits.ndim != 1 or timestamps.ndim != 1 or any(array.shape[0] != row_count for array in (episode_ids, splits, timestamps)):
        raise ValueError("dataset row metadata lengths do not match")
    if observations.dtype != np.float32 or actions.dtype != np.float32 or timestamps.dtype != np.float64:
        raise ValueError("dataset dtypes are incompatible with the mainline schema")
    if not np.isfinite(observations).all() or not np.isfinite(actions).all() or not np.isfinite(timestamps).all():
        raise ValueError("dataset contains non-finite values")
    episode_values = [str(value) for value in episode_ids.tolist()]
    split_values = [str(value) for value in splits.tolist()]
    if any(not value.strip() for value in episode_values):
        raise ValueError("episode ids must be non-empty")
    if any(value not in ALLOWED_SPLITS for value in split_values):
        raise ValueError("unsupported dataset split")
    if np.any(timestamps < 0.0):
        raise ValueError("dataset timestamps must be non-negative")
    episode_split: dict[str, str] = {}
    split_row_counts = {name: 0 for name in sorted(ALLOWED_SPLITS)}
    split_episode_sets = {name: set() for name in sorted(ALLOWED_SPLITS)}
    episode_order: list[str] = []
    last_timestamp: dict[str, float] = {}
    for episode, split, timestamp in zip(episode_values, split_values, timestamps.tolist()):
        if episode not in episode_split:
            episode_split[episode] = split
            episode_order.append(episode)
        elif episode_split[episode] != split:
            raise ValueError("episode frame leakage across dataset splits")
        if episode in last_timestamp and float(timestamp) <= last_timestamp[episode]:
            raise ValueError("timestamps must strictly increase within each episode")
        last_timestamp[episode] = float(timestamp)
        split_row_counts[split] += 1
        split_episode_sets[split].add(episode)
    split_episode_counts = {name: len(split_episode_sets[name]) for name in sorted(split_episode_sets)}
    return episode_split, split_row_counts, split_episode_counts, episode_order


def write_mainline_dataset(
    dataset_path: str | Path,
    *,
    observations: Sequence[Sequence[float]],
    actions: Sequence[Sequence[float]],
    episode_ids: Sequence[str],
    splits: Sequence[str],
    timestamps_s: Sequence[float],
    source_raw_artifact_hashes: Mapping[str, str],
    surface_calibration_sha256: str,
    action_profile_sha256: str,
    filter_profile_sha256: str,
    normalization_sha256: str,
) -> dict[str, object]:
    path = Path(dataset_path)
    observations_array = np.asarray(observations, dtype=np.float32)
    actions_array = np.asarray(actions, dtype=np.float32)
    timestamps_array = np.asarray(timestamps_s, dtype=np.float64)
    episode_array = np.asarray([str(value) for value in episode_ids])
    split_array = np.asarray([str(value) for value in splits])
    episode_split, split_row_counts, split_episode_counts, episode_order = _validate_rows(observations_array, actions_array, episode_array, split_array, timestamps_array)
    if not isinstance(source_raw_artifact_hashes, Mapping):
        raise ValueError("source raw artifact hashes must be an episode mapping")
    source_hashes = {str(key): _sha(value, f"source_raw_artifact_hashes[{key}]") for key, value in source_raw_artifact_hashes.items()}
    if set(source_hashes) != set(episode_split):
        raise ValueError("source raw artifact hashes must cover exactly every episode")
    for name, value in (("surface_calibration_sha256", surface_calibration_sha256), ("action_profile_sha256", action_profile_sha256), ("filter_profile_sha256", filter_profile_sha256), ("normalization_sha256", normalization_sha256)):
        _sha(value, name)
    _write_npz_atomic(path, {"observations": observations_array, "actions": actions_array, "episode_ids": episode_array, "splits": split_array, "timestamps_s": timestamps_array})
    manifest: dict[str, object] = {
        "schema": DATASET_SCHEMA,
        "observation_dimension": OBSERVATION_DIMENSION,
        "action_dimension": ACTION_DIMENSION,
        "dataset_artifact": path.name,
        "dataset_sha256": _sha256_file(path),
        "row_count": int(observations_array.shape[0]),
        "episode_count": len(episode_split),
        "episode_order": episode_order,
        "episode_split": episode_split,
        "split_row_counts": split_row_counts,
        "split_episode_counts": split_episode_counts,
        "source_raw_artifact_hashes": source_hashes,
        "surface_calibration_sha256": surface_calibration_sha256,
        "action_profile_sha256": action_profile_sha256,
        "filter_profile_sha256": filter_profile_sha256,
        "normalization_sha256": normalization_sha256,
        "split_policy": "episode_grouped_frozen_before_training",
    }
    manifest["manifest_sha256"] = _payload_hash(manifest)
    return manifest


def write_mainline_manifest(manifest_path: str | Path, manifest: dict[str, object]) -> None:
    path = Path(manifest_path)
    _write_json_atomic(path, manifest)


def validate_mainline_dataset(dataset_path: str | Path, manifest: dict[str, object]) -> dict[str, object]:
    path = Path(dataset_path)
    if set(manifest) != MANIFEST_FIELDS:
        raise ValueError("mainline dataset manifest fields are missing or extra")
    if manifest.get("schema") != DATASET_SCHEMA or manifest.get("observation_dimension") != OBSERVATION_DIMENSION or manifest.get("action_dimension") != ACTION_DIMENSION:
        raise ValueError("incompatible mainline dataset schema")
    if manifest.get("split_policy") != "episode_grouped_frozen_before_training":
        raise ValueError("mainline dataset split policy is invalid")
    if manifest.get("dataset_artifact") != path.name:
        raise ValueError("dataset artifact name mismatch")
    payload = dict(manifest)
    supplied_manifest_hash = payload.pop("manifest_sha256")
    if supplied_manifest_hash != _payload_hash(payload):
        raise ValueError("dataset manifest hash mismatch")
    if manifest.get("dataset_sha256") != _sha256_file(path):
        raise ValueError("dataset artifact hash mismatch")
    for name in ("surface_calibration_sha256", "action_profile_sha256", "filter_profile_sha256", "normalization_sha256"):
        _sha(manifest.get(name), name)
    source_hashes = manifest.get("source_raw_artifact_hashes")
    if not isinstance(source_hashes, dict):
        raise ValueError("source raw artifact hashes are invalid")
    with np.load(path, allow_pickle=False) as data:
        if set(data.files) != NPZ_FIELDS:
            raise ValueError("dataset NPZ fields are missing or extra")
        observations = data["observations"]
        actions = data["actions"]
        episode_ids = data["episode_ids"]
        splits = data["splits"]
        timestamps = data["timestamps_s"]
        episode_split, split_row_counts, split_episode_counts, episode_order = _validate_rows(observations, actions, episode_ids, splits, timestamps)
    if manifest["row_count"] != int(observations.shape[0]) or manifest["episode_count"] != len(episode_split):
        raise ValueError("dataset row or episode count mismatch")
    if manifest["episode_order"] != episode_order or manifest["episode_split"] != episode_split:
        raise ValueError("dataset episode ordering or frozen split mismatch")
    if manifest["split_row_counts"] != split_row_counts or manifest["split_episode_counts"] != split_episode_counts:
        raise ValueError("dataset split counts mismatch")
    if set(source_hashes) != set(episode_split):
        raise ValueError("source raw artifact hashes do not cover dataset episodes")
    for key, value in source_hashes.items():
        _sha(value, f"source_raw_artifact_hashes[{key}]")
    return manifest
