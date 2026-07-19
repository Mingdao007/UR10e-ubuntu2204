"""Path-independent NPZ dataset contract and hash/statistics manifests."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np

from .config import DBILConfig


REQUIRED_ARRAYS = ("pose_history", "wrench_history", "target_s_zft", "split")
SPLIT_NAMES = {0: "train", 1: "validation", 2: "test", 3: "application"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class DatasetArrays:
    pose_history: np.ndarray
    wrench_history: np.ndarray
    target_s_zft: np.ndarray
    split: np.ndarray

    def __post_init__(self) -> None:
        count = self.pose_history.shape[0]
        if self.pose_history.shape != (count, 16, 7):
            raise ValueError("pose_history must have shape [N,16,7]")
        if self.wrench_history.shape != (count, 16, 6):
            raise ValueError("wrench_history must have shape [N,16,6]")
        if self.target_s_zft.shape != (count, 16, 7):
            raise ValueError("target_s_zft must have shape [N,16,7]")
        if self.split.shape != (count,):
            raise ValueError("split must have shape [N]")
        if not np.isin(self.split, tuple(SPLIT_NAMES)).all():
            raise ValueError("split values must be train/validation/test/application ids 0..3")
        if count <= 0:
            raise ValueError("dataset must contain at least one sample")
        if not all(
            np.isfinite(array).all()
            for array in (self.pose_history, self.wrench_history, self.target_s_zft)
        ):
            raise ValueError("dataset arrays must be finite")
        quaternion_norms = np.linalg.norm(self.pose_history[:, :, 3:7], axis=-1)
        target_quaternion_norms = np.linalg.norm(self.target_s_zft[:, :, 3:7], axis=-1)
        if np.any(np.abs(quaternion_norms - 1.0) > 1e-3) or np.any(
            np.abs(target_quaternion_norms - 1.0) > 1e-3
        ):
            raise ValueError("pose quaternions must be unit norm within 1e-3")

    @property
    def context(self) -> np.ndarray:
        return np.concatenate((self.pose_history, self.wrench_history), axis=-1)

    def __len__(self) -> int:
        return int(self.pose_history.shape[0])

    def select_split(self, split_id: int) -> "DatasetArrays":
        mask = self.split == split_id
        if not mask.any():
            raise ValueError(f"dataset has no {SPLIT_NAMES.get(split_id, split_id)} samples")
        return DatasetArrays(
            self.pose_history[mask],
            self.wrench_history[mask],
            self.target_s_zft[mask],
            self.split[mask],
        )


def load_dataset(path: Path, config: DBILConfig | None = None) -> DatasetArrays:
    config = config or DBILConfig()
    if config != DBILConfig():
        # Construction already enforces the pinned architecture; retain an explicit guard.
        raise ValueError("dataset loader only supports the pinned first-pass DBIL config")
    with np.load(path, allow_pickle=False) as archive:
        missing = [name for name in REQUIRED_ARRAYS if name not in archive]
        if missing:
            raise ValueError(f"dataset is missing arrays: {', '.join(missing)}")
        arrays = DatasetArrays(
            pose_history=np.asarray(archive["pose_history"], dtype=np.float32),
            wrench_history=np.asarray(archive["wrench_history"], dtype=np.float32),
            target_s_zft=np.asarray(archive["target_s_zft"], dtype=np.float32),
            split=np.asarray(archive["split"], dtype=np.int8),
        )
    return arrays


@dataclass(frozen=True)
class DatasetStats:
    context_mean: tuple[float, ...]
    context_std: tuple[float, ...]
    target_mean: tuple[float, ...]
    target_std: tuple[float, ...]

    def __post_init__(self) -> None:
        if not all(
            len(values) == expected
            for values, expected in (
                (self.context_mean, 13),
                (self.context_std, 13),
                (self.target_mean, 7),
                (self.target_std, 7),
            )
        ):
            raise ValueError("statistics dimensions do not match DBIL config")
        if any(value <= 0.0 for value in self.context_std + self.target_std):
            raise ValueError("standard deviations must be positive")
        if not all(
            np.isfinite(value)
            for values in (
                self.context_mean,
                self.context_std,
                self.target_mean,
                self.target_std,
            )
            for value in values
        ):
            raise ValueError("statistics must be finite")

    @classmethod
    def fit(cls, arrays: DatasetArrays, *, split_id: int = 0) -> "DatasetStats":
        selected = arrays.select_split(split_id)
        context = selected.context.reshape(-1, 13)
        target = selected.target_s_zft.reshape(-1, 7)
        context_std = np.maximum(context.std(axis=0), 1e-6)
        target_std = np.maximum(target.std(axis=0), 1e-6)
        return cls(
            context_mean=tuple(float(value) for value in context.mean(axis=0)),
            context_std=tuple(float(value) for value in context_std),
            target_mean=tuple(float(value) for value in target.mean(axis=0)),
            target_std=tuple(float(value) for value in target_std),
        )

    def to_dict(self) -> dict[str, list[float]]:
        return {
            "context_mean": list(self.context_mean),
            "context_std": list(self.context_std),
            "target_mean": list(self.target_mean),
            "target_std": list(self.target_std),
        }

    @classmethod
    def from_path(cls, path: Path) -> "DatasetStats":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(**{name: tuple(values) for name, values in payload.items()})


def write_dataset_manifest(
    dataset_path: Path,
    output_path: Path,
    *,
    stats_output_path: Path | None = None,
) -> dict[str, Any]:
    arrays = load_dataset(dataset_path)
    stats = DatasetStats.fit(arrays)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "dataset_artifact": dataset_path.name,
        "path_semantics": "artifact names are relative to the relocated evidence bundle",
        "dataset_sha256": sha256_file(dataset_path),
        "sample_count": len(arrays),
        "array_shapes": {
            "pose_history": list(arrays.pose_history.shape),
            "wrench_history": list(arrays.wrench_history.shape),
            "target_s_zft": list(arrays.target_s_zft.shape),
            "split": list(arrays.split.shape),
        },
        "split_counts": {
            name: int(np.count_nonzero(arrays.split == split_id))
            for split_id, name in SPLIT_NAMES.items()
        },
        "dbil_config": DBILConfig().to_dict(),
        "claim_boundary": "public-data pipeline preparation; no hardware reproduction claim",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if stats_output_path is not None:
        stats_output_path.parent.mkdir(parents=True, exist_ok=True)
        stats_output_path.write_text(
            json.dumps(stats.to_dict(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return payload
