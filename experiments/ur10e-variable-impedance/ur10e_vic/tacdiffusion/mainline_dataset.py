"""84D observation/12D action durable training view and hash manifest."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np


DATASET_SCHEMA = "ur10e_tacdiffusion_dataset/v2"
OBSERVATION_DIMENSION = 84
ACTION_DIMENSION = 12


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def write_mainline_dataset(
    dataset_path: str | Path,
    *,
    observations: Sequence[Sequence[float]],
    actions: Sequence[Sequence[float]],
    episode_ids: Sequence[str],
    splits: Sequence[str],
    timestamps_s: Sequence[float],
    surface_calibration_sha256: str,
    action_profile_sha256: str,
    normalization_sha256: str,
) -> dict[str, object]:
    path = Path(dataset_path)
    observations_array = np.asarray(observations, dtype=np.float32)
    actions_array = np.asarray(actions, dtype=np.float32)
    timestamps_array = np.asarray(timestamps_s, dtype=np.float64)
    if observations_array.ndim != 2 or observations_array.shape[1] != OBSERVATION_DIMENSION:
        raise ValueError("mainline observations must be [N, 84]")
    if actions_array.shape != (observations_array.shape[0], ACTION_DIMENSION):
        raise ValueError("mainline actions must be [N, 12]")
    if len(episode_ids) != len(splits) or len(episode_ids) != observations_array.shape[0] or timestamps_array.shape != (observations_array.shape[0],):
        raise ValueError("dataset row metadata lengths do not match")
    if not np.isfinite(observations_array).all() or not np.isfinite(actions_array).all() or not np.isfinite(timestamps_array).all():
        raise ValueError("dataset contains non-finite values")
    if any(not str(value).strip() for value in episode_ids):
        raise ValueError("episode ids must be non-empty")
    if any(value not in {"train", "validation", "test"} for value in splits):
        raise ValueError("unsupported dataset split")
    grouped = {}
    for episode, split, timestamp in zip(episode_ids, splits, timestamps_array):
        prior = grouped.setdefault(episode, split)
        if prior != split:
            raise ValueError("episode frame leakage across dataset splits")
        if timestamp < 0.0:
            raise ValueError("dataset timestamp must be non-negative")
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, observations=observations_array, actions=actions_array, episode_ids=np.asarray(episode_ids), splits=np.asarray(splits), timestamps_s=timestamps_array)
    manifest: dict[str, object] = {
        "schema": DATASET_SCHEMA,
        "observation_dimension": OBSERVATION_DIMENSION,
        "action_dimension": ACTION_DIMENSION,
        "dataset_artifact": path.name,
        "dataset_sha256": _sha256_file(path),
        "episode_count": len(grouped),
        "row_count": int(observations_array.shape[0]),
        "surface_calibration_sha256": surface_calibration_sha256,
        "action_profile_sha256": action_profile_sha256,
        "normalization_sha256": normalization_sha256,
        "split_policy": "episode_grouped_frozen_before_training",
    }
    manifest["manifest_sha256"] = _payload_hash(manifest)
    return manifest


def write_mainline_manifest(manifest_path: str | Path, manifest: dict[str, object]) -> None:
    path = Path(manifest_path)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def validate_mainline_dataset(dataset_path: str | Path, manifest: dict[str, object]) -> dict[str, object]:
    path = Path(dataset_path)
    if manifest.get("schema") != DATASET_SCHEMA or manifest.get("observation_dimension") != OBSERVATION_DIMENSION or manifest.get("action_dimension") != ACTION_DIMENSION:
        raise ValueError("incompatible mainline dataset schema")
    supplied_hash = manifest.get("dataset_sha256")
    if supplied_hash != _sha256_file(path):
        raise ValueError("dataset artifact hash mismatch")
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    if manifest.get("manifest_sha256") != _payload_hash(payload):
        raise ValueError("dataset manifest hash mismatch")
    with np.load(path, allow_pickle=False) as data:
        if data["observations"].shape[1] != OBSERVATION_DIMENSION or data["actions"].shape[1] != ACTION_DIMENSION:
            raise ValueError("dataset dimensions do not match manifest")
    return manifest
