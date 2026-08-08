from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from ur10e_vic.tacdiffusion.action import MODEL_MODE_FIXED_K_V1
from ur10e_vic.tacdiffusion.formal_artifacts import (
    load_sealed_formal_predictor,
    train_and_seal_formal_checkpoint,
)
from ur10e_vic.tacdiffusion.formal_model import FormalDatasetManifestV1


def _dataset(tmp_path: Path) -> tuple[Path, Path]:
    observations = np.asarray(
        [[0.001 * row + 0.00001 * column for column in range(84)] for row in range(140)],
        dtype=np.float64,
    )
    targets = np.asarray(
        [[0.01 * (row % 3) for _ in range(6)] for row in range(140)],
        dtype=np.float64,
    )
    episode_ids = np.asarray([f"episode_{index:03d}" for index in range(140)])
    splits = np.asarray(["train"] * 140)
    timestamps = np.asarray([0.002 * index for index in range(140)], dtype=np.float64)
    path = tmp_path / "dataset_84d_6d.npz"
    with path.open("wb") as handle:
        np.savez_compressed(
            handle,
            observations=observations,
            targets=targets,
            episode_ids=episode_ids,
            splits=splits,
            timestamps_s=timestamps,
        )
    manifest = FormalDatasetManifestV1.from_arrays(
        observations,
        targets,
        dataset_id="fixed_k_formal_v4_dataset_test",
        mode=MODEL_MODE_FIXED_K_V1,
        source_hashes={f"episode_{index:03d}": "a" * 64 for index in range(140)},
    )
    manifest_path = tmp_path / "dataset_84d_6d.manifest.json"
    payload = manifest.as_json() | {
        "artifact": path.name,
        "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "episode_count": 140,
        "split_episode_counts": {"train": 140, "validation": 0, "test": 0},
        "mainline_manifest_sha256": "b" * 64,
    }
    manifest_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path, manifest_path


def test_training_checkpoint_roundtrip_is_state_and_dataset_bound(tmp_path: Path) -> None:
    dataset, manifest = _dataset(tmp_path)
    receipt = train_and_seal_formal_checkpoint(
        training_dataset_path=dataset,
        training_manifest_path=manifest,
        output_dir=tmp_path / "training",
        epochs=1,
        seed=7,
        device="cpu",
    )
    assert receipt["model_active"] is False
    predictor = load_sealed_formal_predictor(
        tmp_path / "training" / "training_receipt.json"
    )
    sample = predictor.sample(np.zeros(84), seed=1)
    assert len(sample.values) == 6
    assert sample.steps_executed == 50
    assert sample.active_enabled is False


def test_checkpoint_file_tamper_fails_closed(tmp_path: Path) -> None:
    dataset, manifest = _dataset(tmp_path)
    train_and_seal_formal_checkpoint(
        training_dataset_path=dataset,
        training_manifest_path=manifest,
        output_dir=tmp_path / "training",
        epochs=1,
        seed=7,
    )
    checkpoint = tmp_path / "training" / "checkpoint.bin"
    checkpoint.write_bytes(checkpoint.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        load_sealed_formal_predictor(
            tmp_path / "training" / "training_receipt.json"
        )

