"""Durable training/checkpoint artifacts for TacDiffusion formal V4."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Mapping

import numpy as np

from .formal_model import (
    FormalCheckpointManifestV1,
    FormalDatasetManifestV1,
    FormalDDPMPredictorV1,
    FormalModelConfigV1,
    _NumpyFormalNoiseModel,
    _TorchFormalNoiseModel,
    train_formal_ddpm_v1,
)


FORMAL_TRAINING_ARTIFACT_SCHEMA_V1 = "ur10e_tacdiffusion_training_artifact/v1"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _write_json_new(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _formal_dataset_manifest(payload: Mapping[str, Any]) -> FormalDatasetManifestV1:
    keys = {
        "schema_version",
        "dataset_id",
        "mode",
        "sample_count",
        "dataset_sha256",
        "source_hashes",
        "observation_dimension",
        "output_dimension",
        "diffusion_steps",
        "active_enabled",
        "shadow_only",
        "manifest_sha256",
    }
    missing = keys - set(payload)
    if missing:
        raise ValueError(f"formal dataset manifest is missing: {sorted(missing)}")
    return FormalDatasetManifestV1.from_json({key: payload[key] for key in keys})


def _checkpoint_bytes(
    predictor: FormalDDPMPredictorV1,
    path: Path,
) -> tuple[str, str]:
    """Write the actual trained state and return backend/state format."""

    noise_model = predictor._noise_model
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        if isinstance(noise_model, _NumpyFormalNoiseModel):
            with NamedTemporaryFile(
                "w+b", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                np.savez(
                    handle,
                    weights=noise_model.weights,
                    bias=noise_model.bias,
                )
                handle.flush()
                os.fsync(handle.fileno())
            backend = "numpy"
            state_format = "numpy_weights_bias_npz_v1"
        elif isinstance(noise_model, _TorchFormalNoiseModel):
            torch = noise_model.torch
            with NamedTemporaryFile(
                "w+b", dir=path.parent, prefix=f".{path.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                torch.save(noise_model.model.state_dict(), handle)
                handle.flush()
                os.fsync(handle.fileno())
            backend = "torch"
            state_format = "torch_state_dict_v1"
        else:
            raise TypeError("unsupported formal trained-noise-model backend")
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return backend, state_format
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def train_and_seal_formal_checkpoint(
    *,
    training_dataset_path: str | Path,
    training_manifest_path: str | Path,
    output_dir: str | Path,
    epochs: int,
    seed: int = 42,
    device: str = "cpu",
) -> dict[str, object]:
    dataset_path = Path(training_dataset_path)
    manifest_path = Path(training_manifest_path)
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest_payload, Mapping):
        raise ValueError("formal training manifest must be an object")
    if manifest_payload.get("artifact") != dataset_path.name:
        raise ValueError("formal training dataset artifact name mismatch")
    if manifest_payload.get("artifact_sha256") != _sha256_file(dataset_path):
        raise ValueError("formal training dataset artifact hash mismatch")
    dataset_manifest = _formal_dataset_manifest(manifest_payload)
    with np.load(dataset_path, allow_pickle=False) as archive:
        required = {"observations", "targets", "episode_ids", "splits", "timestamps_s"}
        if set(archive.files) != required:
            raise ValueError("formal training dataset fields are missing or extra")
        observations = np.asarray(archive["observations"], dtype=np.float64)
        targets = np.asarray(archive["targets"], dtype=np.float64)
        episode_ids = tuple(str(value) for value in archive["episode_ids"].tolist())
        splits = tuple(str(value) for value in archive["splits"].tolist())
    if observations.shape[0] != dataset_manifest.sample_count:
        raise ValueError("formal training dataset row count mismatch")
    rebuilt = FormalDatasetManifestV1.from_arrays(
        observations,
        targets,
        dataset_id=dataset_manifest.dataset_id,
        mode=dataset_manifest.mode,
        source_hashes=dataset_manifest.source_hashes,
    )
    if rebuilt.as_json() != dataset_manifest.as_json():
        raise ValueError("formal training arrays do not match their manifest")
    train_mask = np.asarray([split == "train" for split in splits], dtype=bool)
    if not train_mask.any() or len(set(np.asarray(episode_ids)[train_mask].tolist())) != 140:
        raise ValueError("formal trainer requires the frozen 140-episode training split")
    result = train_formal_ddpm_v1(
        observations[train_mask],
        targets[train_mask],
        mode=dataset_manifest.mode,
        epochs=epochs,
        seed=seed,
        device=device,
        dataset_id=dataset_manifest.dataset_id,
        source_hashes=dataset_manifest.source_hashes,
    )
    # Training uses only the train split, but the public checkpoint remains
    # bound to the complete frozen dataset from which that split was selected.
    checkpoint = FormalCheckpointManifestV1(
        checkpoint_id=result.checkpoint_manifest.checkpoint_id,
        mode=result.checkpoint_manifest.mode,
        checkpoint_sha256=result.checkpoint_manifest.checkpoint_sha256,
        dataset_sha256=dataset_manifest.dataset_sha256,
        source_hashes=result.checkpoint_manifest.source_hashes
        | {"complete_dataset_manifest": dataset_manifest.manifest_sha256},
        observation_dimension=result.checkpoint_manifest.observation_dimension,
        output_dimension=result.checkpoint_manifest.output_dimension,
        diffusion_steps=result.checkpoint_manifest.diffusion_steps,
    )
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=False)
    checkpoint_path = output / "checkpoint.bin"
    backend, state_format = _checkpoint_bytes(result.predictor, checkpoint_path)
    payload: dict[str, object] = {
        "schema_version": FORMAL_TRAINING_ARTIFACT_SCHEMA_V1,
        "mode": dataset_manifest.mode,
        "epochs": epochs,
        "seed": seed,
        "device": device,
        "final_loss": result.final_loss,
        "training_episode_count": 140,
        "training_row_count": int(train_mask.sum()),
        "complete_dataset_manifest": dataset_manifest.as_json(),
        "checkpoint_manifest": checkpoint.as_json(),
        "checkpoint_artifact": checkpoint_path.name,
        "checkpoint_artifact_sha256": _sha256_file(checkpoint_path),
        "checkpoint_backend": backend,
        "checkpoint_state_format": state_format,
        "model_active": False,
        "shadow_only": True,
    }
    payload["training_receipt_sha256"] = _canonical_sha256(payload)
    _write_json_new(output / "training_receipt.json", payload)
    return payload


def load_sealed_formal_predictor(
    training_receipt_path: str | Path,
) -> FormalDDPMPredictorV1:
    receipt_path = Path(training_receipt_path)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or payload.get("schema_version") != FORMAL_TRAINING_ARTIFACT_SCHEMA_V1:
        raise ValueError("unsupported formal training artifact")
    unsigned = dict(payload)
    supplied = unsigned.pop("training_receipt_sha256", None)
    if supplied != _canonical_sha256(unsigned):
        raise ValueError("formal training receipt hash mismatch")
    if payload.get("model_active") is not False or payload.get("shadow_only") is not True:
        raise ValueError("formal checkpoint must remain inactive and shadow-only")
    dataset_payload = payload.get("complete_dataset_manifest")
    checkpoint_payload = payload.get("checkpoint_manifest")
    if not isinstance(dataset_payload, Mapping) or not isinstance(checkpoint_payload, Mapping):
        raise ValueError("formal training artifact manifests are missing")
    dataset = FormalDatasetManifestV1.from_json(dataset_payload)
    checkpoint = FormalCheckpointManifestV1.from_json(checkpoint_payload)
    checkpoint_path = receipt_path.parent / str(payload.get("checkpoint_artifact", ""))
    if _sha256_file(checkpoint_path) != payload.get("checkpoint_artifact_sha256"):
        raise ValueError("formal checkpoint artifact hash mismatch")
    config = FormalModelConfigV1(mode=checkpoint.mode, device=str(payload.get("device", "cpu")))
    if payload.get("checkpoint_backend") == "numpy":
        model = _NumpyFormalNoiseModel(config, seed=int(payload["seed"]))
        with np.load(checkpoint_path, allow_pickle=False) as archive:
            if set(archive.files) != {"weights", "bias"}:
                raise ValueError("formal NumPy checkpoint fields are invalid")
            weights = np.asarray(archive["weights"], dtype=np.float64)
            bias = np.asarray(archive["bias"], dtype=np.float64)
        if weights.shape != model.weights.shape or bias.shape != model.bias.shape:
            raise ValueError("formal NumPy checkpoint shape mismatch")
        model.weights = weights
        model.bias = bias
    elif payload.get("checkpoint_backend") == "torch":
        model = _TorchFormalNoiseModel(config, seed=int(payload["seed"]))
        state = model.torch.load(
            checkpoint_path, map_location=model.device, weights_only=True
        )
        model.model.load_state_dict(state, strict=True)
    else:
        raise ValueError("formal checkpoint backend is unsupported")
    if hashlib.sha256(model.state_bytes()).hexdigest() != checkpoint.checkpoint_sha256:
        raise ValueError("formal checkpoint state identity mismatch")
    return FormalDDPMPredictorV1(
        config=config,
        checkpoint_manifest=checkpoint,
        dataset_manifest=dataset,
        seed=int(payload["seed"]),
        _noise_model=model,
    )


__all__ = [
    "FORMAL_TRAINING_ARTIFACT_SCHEMA_V1",
    "load_sealed_formal_predictor",
    "train_and_seal_formal_checkpoint",
]
