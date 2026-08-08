"""Independent offline V4 DDPM path for the 84D TacDiffusion contract.

This module intentionally does not import or alter the legacy 36D model
implementation.  It provides a small NumPy reference backend and an
optional CUDA-capable backend hook with one fixed predictor API:
``sample(observation_84d, *, seed=None)``.  The sampler always executes the
formal 50 reverse-diffusion steps and returns a typed 6D or 7D model output.

The model is never active.  Checkpoint and dataset manifests are value-bound
and remain shadow/offline evidence only.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .action import (
    MODEL_MODE_FIXED_K_V1,
    MODEL_MODE_VARIABLE_K_V1,
    TypedModelOutputV1,
)
from .contracts import FORMAL_OBSERVATION_DIMENSION, FORMAL_SAMPLER_STEPS


FORMAL_DDPM_SCHEMA_V1 = "ur10e_tacdiffusion_formal_ddpm/v1"
FORMAL_DATASET_MANIFEST_SCHEMA_V1 = "ur10e_tacdiffusion_formal_dataset_manifest/v1"
FORMAL_CHECKPOINT_MANIFEST_SCHEMA_V1 = "ur10e_tacdiffusion_formal_checkpoint_manifest/v1"
FORMAL_MODEL_CONFIG_SCHEMA_V1 = "ur10e_tacdiffusion_formal_model_config/v1"
FORMAL_MODEL_MODES = (MODEL_MODE_FIXED_K_V1, MODEL_MODE_VARIABLE_K_V1)
FORMAL_MODEL_OUTPUT_DIMENSIONS = {
    MODEL_MODE_FIXED_K_V1: 6,
    MODEL_MODE_VARIABLE_K_V1: 7,
}
FORMAL_BETA_START = 1.0e-4
FORMAL_BETA_END = 2.0e-2
FORMAL_DEVICE_DEFAULT = "cpu"
FORMAL_VARIABLE_K_CENTER_N_M = 600.0
FORMAL_VARIABLE_K_HALF_RANGE_N_M = 200.0
_ZERO_SHA256 = "0" * 64


def _require_sha256(value: object, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite_vector(values: Sequence[float], dimension: int, name: str) -> np.ndarray:
    try:
        array = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if array.shape != (dimension,) or not np.isfinite(array).all():
        raise ValueError(f"{name} must contain {dimension} finite values")
    return array


def _finite_training_arrays(
    observations: Sequence[Sequence[float]],
    targets: Sequence[Sequence[float]],
    output_dimension: int,
) -> tuple[np.ndarray, np.ndarray]:
    condition = np.asarray(observations, dtype=np.float64)
    labels = np.asarray(targets, dtype=np.float64)
    if condition.ndim != 2 or condition.shape[1] != FORMAL_OBSERVATION_DIMENSION:
        raise ValueError("formal observations must have shape [N, 84]")
    if labels.shape != (condition.shape[0], output_dimension):
        raise ValueError(f"formal targets must have shape [N, {output_dimension}]")
    if condition.shape[0] == 0 or not np.isfinite(condition).all() or not np.isfinite(labels).all():
        raise ValueError("formal observations and targets must be non-empty and finite")
    return condition, labels


def _alpha_bar_schedule(config: "FormalModelConfigV1") -> np.ndarray:
    """Return the cumulative product of DDPM alphas, never raw betas."""

    betas = np.linspace(
        config.beta_start,
        config.beta_end,
        config.diffusion_steps,
        dtype=np.float64,
    )
    return np.cumprod(1.0 - betas)


def _normalize_training_targets(labels: np.ndarray, *, mode: str) -> np.ndarray:
    """Put learned stiffness on a diffusion-compatible [-1, 1] scale."""

    normalized = np.asarray(labels, dtype=np.float64).copy()
    if mode == MODEL_MODE_VARIABLE_K_V1:
        k_values = normalized[:, -1]
        if np.any(k_values < 400.0) or np.any(k_values > 800.0):
            raise ValueError("Variable-K training labels must stay within 400..800 N/m")
        normalized[:, -1] = (
            k_values - FORMAL_VARIABLE_K_CENTER_N_M
        ) / FORMAL_VARIABLE_K_HALF_RANGE_N_M
    return normalized


def _denormalize_sample(values: np.ndarray, *, mode: str) -> np.ndarray:
    """Map the diffusion coordinate back to the typed physical model output."""

    physical = np.asarray(values, dtype=np.float64).copy()
    if mode == MODEL_MODE_VARIABLE_K_V1:
        normalized_k = float(np.clip(physical[-1], -1.0, 1.0))
        physical[-1] = (
            FORMAL_VARIABLE_K_CENTER_N_M
            + FORMAL_VARIABLE_K_HALF_RANGE_N_M * normalized_k
        )
    return physical


def build_variable_k_training_labels(
    feedforward_wrench_6d: Sequence[Sequence[float]],
    tracking_errors_xyz: Sequence[Sequence[float]],
    kunwei_forces_xyz: Sequence[Sequence[float]],
) -> np.ndarray:
    """Build formal 7D targets with the deterministic Variable-K seventh label."""

    force = np.asarray(feedforward_wrench_6d, dtype=np.float64)
    errors = np.asarray(tracking_errors_xyz, dtype=np.float64)
    kunwei = np.asarray(kunwei_forces_xyz, dtype=np.float64)
    if force.ndim != 2 or force.shape[1] != 6:
        raise ValueError("feedforward_wrench_6d must have shape [N, 6]")
    if errors.shape != (force.shape[0], 3) or kunwei.shape != (force.shape[0], 3):
        raise ValueError("tracking_errors_xyz and kunwei_forces_xyz must have shape [N, 3]")
    if force.shape[0] == 0 or not np.isfinite(force).all() or not np.isfinite(errors).all() or not np.isfinite(kunwei).all():
        raise ValueError("Variable-K training label inputs must be non-empty and finite")
    from .expert import VariableKExpertV1

    expert = VariableKExpertV1()
    labels = np.asarray(
        [
            expert.training_label(
                error,
                sensor_force,
                feedforward_wrench_6d=force_row,
            )
            for force_row, error, sensor_force in zip(force, errors, kunwei)
        ],
        dtype=np.float64,
    )
    if labels.shape != (force.shape[0], 7) or not np.isfinite(labels).all():
        raise ValueError("Variable-K training labels must have shape [N, 7]")
    return labels


@dataclass(frozen=True)
class FormalModelConfigV1:
    """Typed formal model identity shared by train and predict paths."""

    mode: str
    observation_dimension: int = FORMAL_OBSERVATION_DIMENSION
    output_dimension: int | None = None
    diffusion_steps: int = FORMAL_SAMPLER_STEPS
    beta_start: float = FORMAL_BETA_START
    beta_end: float = FORMAL_BETA_END
    device: str = FORMAL_DEVICE_DEFAULT
    active_enabled: bool = False
    shadow_only: bool = True
    schema_version: str = FORMAL_MODEL_CONFIG_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema_version != FORMAL_MODEL_CONFIG_SCHEMA_V1:
            raise ValueError("unsupported formal model config schema")
        if self.mode not in FORMAL_MODEL_MODES:
            raise ValueError("formal model mode must be fixed_k_v1 or variable_k_v1")
        expected = FORMAL_MODEL_OUTPUT_DIMENSIONS[self.mode]
        if self.observation_dimension != FORMAL_OBSERVATION_DIMENSION:
            raise ValueError("formal model observation dimension must be exactly 84")
        if self.output_dimension is None:
            object.__setattr__(self, "output_dimension", expected)
        if self.output_dimension != expected:
            raise ValueError("formal model output dimension does not match typed mode")
        if self.diffusion_steps != FORMAL_SAMPLER_STEPS:
            raise ValueError("formal DDPM must use exactly 50 diffusion steps")
        if not math.isfinite(self.beta_start) or not math.isfinite(self.beta_end):
            raise ValueError("formal beta schedule must be finite")
        if not 0.0 < self.beta_start < self.beta_end < 1.0:
            raise ValueError("formal beta schedule is invalid")
        if not str(self.device).strip():
            raise ValueError("formal model device must be named")
        if self.active_enabled is not False or self.shadow_only is not True:
            raise ValueError("formal model must remain inactive and shadow-only")

    @property
    def cuda_compatible(self) -> bool:
        """The tensor contract is backend-neutral and can run on CUDA."""

        return True

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "observation_dimension": self.observation_dimension,
            "output_dimension": self.output_dimension,
            "diffusion_steps": self.diffusion_steps,
            "beta_start": self.beta_start,
            "beta_end": self.beta_end,
            "device": self.device,
            "cuda_compatible": self.cuda_compatible,
            "active_enabled": self.active_enabled,
            "shadow_only": self.shadow_only,
        }


class _NumpyFormalNoiseModel:
    """Deterministic conditional noise estimator used when PyTorch is absent."""

    def __init__(self, config: FormalModelConfigV1, *, seed: int) -> None:
        self.config = config
        feature_dimension = config.observation_dimension + config.output_dimension + 1
        rng = np.random.default_rng(int(seed))
        self.weights = rng.normal(0.0, 0.02, size=(feature_dimension, config.output_dimension))
        self.bias = np.zeros(config.output_dimension, dtype=np.float64)

    def _features(
        self,
        observation: np.ndarray,
        noisy_output: np.ndarray,
        step_index: int,
    ) -> np.ndarray:
        normalized_step = float(step_index) / float(self.config.diffusion_steps - 1)
        return np.concatenate(
            (observation, noisy_output, np.asarray((normalized_step,), dtype=np.float64))
        )

    def predict(self, observation: np.ndarray, noisy_output: np.ndarray, step_index: int) -> np.ndarray:
        return self._features(observation, noisy_output, step_index) @ self.weights + self.bias

    def train_step(
        self,
        observations: np.ndarray,
        noisy_outputs: np.ndarray,
        step_indices: np.ndarray,
        target_noise: np.ndarray,
        *,
        learning_rate: float,
    ) -> float:
        features = np.asarray(
            [self._features(row, action, int(step)) for row, action, step in zip(observations, noisy_outputs, step_indices)],
            dtype=np.float64,
        )
        predicted = features @ self.weights + self.bias
        error = predicted - target_noise
        self.weights -= learning_rate * (features.T @ error) / float(len(features))
        self.bias -= learning_rate * error.mean(axis=0)
        return float(np.mean(error * error))

    def state_bytes(self) -> bytes:
        return self.weights.tobytes(order="C") + self.bias.tobytes(order="C")


class _TorchFormalNoiseModel:
    """Optional CUDA backend with the same conditional-noise contract."""

    def __init__(self, config: FormalModelConfigV1, *, seed: int) -> None:
        try:
            import torch
            import torch.nn as nn
        except ImportError as exc:
            raise RuntimeError("CUDA formal predictor requires PyTorch") from exc
        self.torch = torch
        self.device = torch.device(config.device)
        torch.manual_seed(int(seed))
        feature_dimension = config.observation_dimension + config.output_dimension + 1
        self.model = nn.Sequential(
            nn.Linear(feature_dimension, 128),
            nn.SiLU(),
            nn.Linear(128, config.output_dimension),
        ).to(self.device)
        self.config = config

    def predict_tensor(self, observation: Any, noisy_output: Any, step_index: int) -> Any:
        normalized_step = self.torch.tensor(
            [float(step_index) / float(self.config.diffusion_steps - 1)],
            device=self.device,
            dtype=self.torch.float32,
        )
        features = self.torch.cat((observation, noisy_output, normalized_step), dim=0)
        return self.model(features)

    def train_step(
        self,
        observations: np.ndarray,
        noisy_outputs: np.ndarray,
        step_indices: np.ndarray,
        target_noise: np.ndarray,
        *,
        learning_rate: float,
    ) -> float:
        torch = self.torch
        self.model.train()
        features = []
        for observation, noisy_output, step_index in zip(observations, noisy_outputs, step_indices):
            features.append(
                np.concatenate(
                    (
                        observation,
                        noisy_output,
                        np.asarray(
                            (float(step_index) / float(self.config.diffusion_steps - 1),),
                            dtype=np.float64,
                        ),
                    )
                )
            )
        feature_tensor = torch.tensor(np.asarray(features), device=self.device, dtype=torch.float32)
        target_tensor = torch.tensor(target_noise, device=self.device, dtype=torch.float32)
        loss = torch.mean((self.model(feature_tensor) - target_tensor) ** 2)
        loss.backward()
        with torch.no_grad():
            for parameter in self.model.parameters():
                parameter -= learning_rate * parameter.grad
                parameter.grad = None
        return float(loss.detach().cpu().item())

    def state_bytes(self) -> bytes:
        return b"".join(
            tensor.detach().cpu().numpy().tobytes(order="C")
            for _, tensor in sorted(self.model.state_dict().items())
        )


@dataclass(frozen=True)
class FormalDatasetManifestV1:
    dataset_id: str
    mode: str
    sample_count: int
    dataset_sha256: str
    source_hashes: Mapping[str, str]
    observation_dimension: int = FORMAL_OBSERVATION_DIMENSION
    output_dimension: int | None = None
    diffusion_steps: int = FORMAL_SAMPLER_STEPS
    active_enabled: bool = False
    shadow_only: bool = True
    schema_version: str = FORMAL_DATASET_MANIFEST_SCHEMA_V1

    def __post_init__(self) -> None:
        config = FormalModelConfigV1(
            mode=self.mode,
            observation_dimension=self.observation_dimension,
            output_dimension=self.output_dimension,
            diffusion_steps=self.diffusion_steps,
        )
        object.__setattr__(self, "output_dimension", config.output_dimension)
        if self.schema_version != FORMAL_DATASET_MANIFEST_SCHEMA_V1:
            raise ValueError("unsupported formal dataset manifest schema")
        if not str(self.dataset_id).strip() or self.sample_count <= 0:
            raise ValueError("formal dataset identity/count is invalid")
        _require_sha256(self.dataset_sha256, "dataset_sha256")
        if not isinstance(self.source_hashes, Mapping) or not self.source_hashes:
            raise ValueError("formal dataset source hashes are required")
        normalized = {str(key): _require_sha256(value, f"source_hashes[{key}]") for key, value in self.source_hashes.items()}
        object.__setattr__(self, "source_hashes", normalized)
        if self.active_enabled is not False or self.shadow_only is not True:
            raise ValueError("formal dataset manifest must remain inactive and shadow-only")

    @classmethod
    def from_arrays(
        cls,
        observations: Sequence[Sequence[float]],
        targets: Sequence[Sequence[float]],
        *,
        dataset_id: str,
        mode: str,
        source_hashes: Mapping[str, str] | None = None,
    ) -> "FormalDatasetManifestV1":
        output_dimension = FORMAL_MODEL_OUTPUT_DIMENSIONS.get(mode)
        if output_dimension is None:
            raise ValueError("unsupported formal dataset mode")
        condition, labels = _finite_training_arrays(observations, targets, output_dimension)
        digest = hashlib.sha256(condition.tobytes(order="C") + labels.tobytes(order="C")).hexdigest()
        return cls(
            dataset_id=dataset_id,
            mode=mode,
            sample_count=int(condition.shape[0]),
            dataset_sha256=digest,
            source_hashes=source_hashes or {"dataset_arrays": digest},
            output_dimension=output_dimension,
        )

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "mode": self.mode,
            "sample_count": self.sample_count,
            "dataset_sha256": self.dataset_sha256,
            "source_hashes": dict(self.source_hashes),
            "observation_dimension": self.observation_dimension,
            "output_dimension": self.output_dimension,
            "diffusion_steps": self.diffusion_steps,
            "active_enabled": self.active_enabled,
            "shadow_only": self.shadow_only,
        }

    @property
    def manifest_sha256(self) -> str:
        return _canonical_sha256(self._payload())

    def as_json(self) -> dict[str, object]:
        payload = self._payload()
        payload["manifest_sha256"] = self.manifest_sha256
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> "FormalDatasetManifestV1":
        raw = dict(payload)
        supplied = raw.pop("manifest_sha256", None)
        result = cls(**raw)
        if supplied != result.manifest_sha256:
            raise ValueError("formal dataset manifest hash mismatch")
        return result


@dataclass(frozen=True)
class FormalCheckpointManifestV1:
    checkpoint_id: str
    mode: str
    checkpoint_sha256: str
    dataset_sha256: str
    source_hashes: Mapping[str, str]
    observation_dimension: int = FORMAL_OBSERVATION_DIMENSION
    output_dimension: int | None = None
    diffusion_steps: int = FORMAL_SAMPLER_STEPS
    active_enabled: bool = False
    shadow_only: bool = True
    schema_version: str = FORMAL_CHECKPOINT_MANIFEST_SCHEMA_V1

    def __post_init__(self) -> None:
        config = FormalModelConfigV1(
            mode=self.mode,
            observation_dimension=self.observation_dimension,
            output_dimension=self.output_dimension,
            diffusion_steps=self.diffusion_steps,
        )
        object.__setattr__(self, "output_dimension", config.output_dimension)
        if self.schema_version != FORMAL_CHECKPOINT_MANIFEST_SCHEMA_V1:
            raise ValueError("unsupported formal checkpoint manifest schema")
        if not str(self.checkpoint_id).strip():
            raise ValueError("formal checkpoint identity is required")
        _require_sha256(self.checkpoint_sha256, "checkpoint_sha256")
        _require_sha256(self.dataset_sha256, "dataset_sha256")
        if not isinstance(self.source_hashes, Mapping) or not self.source_hashes:
            raise ValueError("formal checkpoint source hashes are required")
        object.__setattr__(
            self,
            "source_hashes",
            {str(key): _require_sha256(value, f"source_hashes[{key}]") for key, value in self.source_hashes.items()},
        )
        if self.active_enabled is not False or self.shadow_only is not True:
            raise ValueError("formal checkpoint manifest must remain inactive and shadow-only")

    def _payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "checkpoint_id": self.checkpoint_id,
            "mode": self.mode,
            "checkpoint_sha256": self.checkpoint_sha256,
            "dataset_sha256": self.dataset_sha256,
            "source_hashes": dict(self.source_hashes),
            "observation_dimension": self.observation_dimension,
            "output_dimension": self.output_dimension,
            "diffusion_steps": self.diffusion_steps,
            "active_enabled": self.active_enabled,
            "shadow_only": self.shadow_only,
        }

    @property
    def manifest_sha256(self) -> str:
        return _canonical_sha256(self._payload())

    def as_json(self) -> dict[str, object]:
        payload = self._payload()
        payload["manifest_sha256"] = self.manifest_sha256
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> "FormalCheckpointManifestV1":
        raw = dict(payload)
        supplied = raw.pop("manifest_sha256", None)
        result = cls(**raw)
        if supplied != result.manifest_sha256:
            raise ValueError("formal checkpoint manifest hash mismatch")
        return result


@dataclass(frozen=True)
class FormalDDPMSampleV1:
    mode: str
    values: tuple[float, ...]
    observation_dimension: int
    output_dimension: int
    steps_executed: int
    step_trace: tuple[int, ...]
    device: str
    checkpoint_sha256: str
    dataset_sha256: str
    active_enabled: bool = False
    shadow_only: bool = True
    sampler_api: str = "FormalDDPMPredictorV1.sample"

    def __post_init__(self) -> None:
        typed = TypedModelOutputV1(self.mode, self.values)
        object.__setattr__(self, "values", typed.values)
        if self.observation_dimension != FORMAL_OBSERVATION_DIMENSION:
            raise ValueError("formal sample observation dimension must be 84")
        if self.output_dimension != len(typed.values):
            raise ValueError("formal sample output dimension mismatch")
        if self.steps_executed != FORMAL_SAMPLER_STEPS:
            raise ValueError("formal sample must execute exactly 50 steps")
        if tuple(self.step_trace) != tuple(range(FORMAL_SAMPLER_STEPS - 1, -1, -1)):
            raise ValueError("formal sample step trace does not prove 50 reverse steps")
        if self.sampler_api != "FormalDDPMPredictorV1.sample":
            raise ValueError("formal sample must bind the canonical predictor API")
        if not str(self.device).strip():
            raise ValueError("formal sample device is missing")
        _require_sha256(self.checkpoint_sha256, "checkpoint_sha256")
        _require_sha256(self.dataset_sha256, "dataset_sha256")
        if self.active_enabled is not False or self.shadow_only is not True:
            raise ValueError("formal sample must remain inactive and shadow-only")

    @property
    def raw_action(self) -> tuple[float, ...]:
        return self.values

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": FORMAL_DDPM_SCHEMA_V1,
            "mode": self.mode,
            "values": list(self.values),
            "observation_dimension": self.observation_dimension,
            "output_dimension": self.output_dimension,
            "steps_executed": self.steps_executed,
            "step_trace": list(self.step_trace),
            "device": self.device,
            "checkpoint_sha256": self.checkpoint_sha256,
            "dataset_sha256": self.dataset_sha256,
            "sampler_api": self.sampler_api,
            "active_enabled": self.active_enabled,
            "shadow_only": self.shadow_only,
        }


class FormalDDPMPredictorV1:
    """Actual fixed-contract DDPM predictor used by formal benchmarking."""

    def __init__(
        self,
        *,
        config: FormalModelConfigV1,
        checkpoint_manifest: FormalCheckpointManifestV1 | None = None,
        dataset_manifest: FormalDatasetManifestV1 | None = None,
        seed: int = 42,
        _noise_model: _NumpyFormalNoiseModel | None = None,
    ) -> None:
        if not isinstance(config, FormalModelConfigV1):
            raise TypeError("formal predictor requires FormalModelConfigV1")
        if config.device.startswith("cuda"):
            self._validate_cuda_device(config.device)
        if checkpoint_manifest is not None and checkpoint_manifest.mode != config.mode:
            raise ValueError("formal checkpoint mode does not match predictor")
        if checkpoint_manifest is not None and (
            checkpoint_manifest.observation_dimension != config.observation_dimension
            or checkpoint_manifest.output_dimension != config.output_dimension
            or checkpoint_manifest.diffusion_steps != config.diffusion_steps
        ):
            raise ValueError("formal checkpoint dimensions/steps do not match predictor")
        if dataset_manifest is not None and dataset_manifest.mode != config.mode:
            raise ValueError("formal dataset mode does not match predictor")
        if dataset_manifest is not None and (
            dataset_manifest.observation_dimension != config.observation_dimension
            or dataset_manifest.output_dimension != config.output_dimension
            or dataset_manifest.diffusion_steps != config.diffusion_steps
        ):
            raise ValueError("formal dataset dimensions/steps do not match predictor")
        if checkpoint_manifest is not None and dataset_manifest is not None:
            if checkpoint_manifest.dataset_sha256 != dataset_manifest.dataset_sha256:
                raise ValueError("formal checkpoint/dataset binding mismatch")
        self.config = config
        self.device = config.device
        self.seed = int(seed)
        self._noise_model = _noise_model or (
            _TorchFormalNoiseModel(config, seed=self.seed)
            if config.device.startswith("cuda")
            else _NumpyFormalNoiseModel(config, seed=self.seed)
        )
        self.checkpoint_manifest = checkpoint_manifest
        self.dataset_manifest = dataset_manifest
        checkpoint_sha = (
            checkpoint_manifest.checkpoint_sha256
            if checkpoint_manifest is not None
            else hashlib.sha256(self._noise_model.state_bytes()).hexdigest()
        )
        dataset_sha = (
            dataset_manifest.dataset_sha256
            if dataset_manifest is not None
            else _ZERO_SHA256
        )
        self.checkpoint_sha256 = checkpoint_sha
        self.dataset_sha256 = dataset_sha
        self.last_sample: FormalDDPMSampleV1 | None = None

    @staticmethod
    def _validate_cuda_device(device: str) -> None:
        try:
            import torch  # type: ignore
        except ImportError as exc:
            raise RuntimeError("CUDA formal predictor requires an installed CUDA-capable PyTorch") from exc
        if not bool(torch.cuda.is_available()):
            raise RuntimeError("CUDA formal predictor requested but CUDA is unavailable")

    def sample(self, observation_84d: Sequence[float], *, seed: int | None = None) -> FormalDDPMSampleV1:
        """Run the real 50-step formal sampler API; no step-count probing."""

        observation = _finite_vector(
            observation_84d,
            FORMAL_OBSERVATION_DIMENSION,
            "observation_84d",
        )
        if isinstance(self._noise_model, _TorchFormalNoiseModel):
            return self._sample_torch(observation, seed=seed)
        rng = np.random.default_rng(self.seed if seed is None else int(seed))
        betas = np.linspace(
            self.config.beta_start,
            self.config.beta_end,
            self.config.diffusion_steps,
            dtype=np.float64,
        )
        alphas = 1.0 - betas
        alpha_bar = np.cumprod(alphas)
        action = rng.normal(0.0, 1.0, size=self.config.output_dimension)
        trace: list[int] = []
        for index in range(self.config.diffusion_steps - 1, -1, -1):
            trace.append(index)
            predicted_noise = self._noise_model.predict(observation, action, index)
            mean = (action - betas[index] * predicted_noise / math.sqrt(1.0 - alpha_bar[index])) / math.sqrt(alphas[index])
            if index:
                action = mean + math.sqrt(betas[index]) * rng.normal(size=action.shape)
            else:
                action = mean
        physical_action = _denormalize_sample(action, mode=self.config.mode)
        values = tuple(float(value) for value in physical_action)
        result = FormalDDPMSampleV1(
            mode=self.config.mode,
            values=values,
            observation_dimension=FORMAL_OBSERVATION_DIMENSION,
            output_dimension=self.config.output_dimension,
            steps_executed=len(trace),
            step_trace=tuple(trace),
            device=self.device,
            checkpoint_sha256=self.checkpoint_sha256,
            dataset_sha256=self.dataset_sha256,
        )
        self.last_sample = result
        return result

    def _sample_torch(self, observation: np.ndarray, *, seed: int | None) -> FormalDDPMSampleV1:
        noise_model = self._noise_model
        assert isinstance(noise_model, _TorchFormalNoiseModel)
        torch = noise_model.torch
        generator = torch.Generator(device=noise_model.device)
        generator.manual_seed(self.seed if seed is None else int(seed))
        condition = torch.tensor(observation, device=noise_model.device, dtype=torch.float32)
        betas = torch.linspace(
            self.config.beta_start,
            self.config.beta_end,
            self.config.diffusion_steps,
            device=noise_model.device,
            dtype=torch.float32,
        )
        alphas = 1.0 - betas
        alpha_bar = torch.cumprod(alphas, dim=0)
        action = torch.randn(
            (self.config.output_dimension,),
            generator=generator,
            device=noise_model.device,
            dtype=torch.float32,
        )
        trace: list[int] = []
        with torch.no_grad():
            for index in range(self.config.diffusion_steps - 1, -1, -1):
                trace.append(index)
                predicted_noise = noise_model.predict_tensor(condition, action, index)
                mean = (action - betas[index] * predicted_noise / torch.sqrt(1.0 - alpha_bar[index])) / torch.sqrt(alphas[index])
                if index:
                    action = mean + torch.sqrt(betas[index]) * torch.randn(
                        action.shape,
                        generator=generator,
                        device=noise_model.device,
                        dtype=torch.float32,
                    )
                else:
                    action = mean
        values_array = _denormalize_sample(
            action.detach().cpu().numpy().astype(np.float64),
            mode=self.config.mode,
        )
        result = FormalDDPMSampleV1(
            mode=self.config.mode,
            values=tuple(float(value) for value in values_array),
            observation_dimension=FORMAL_OBSERVATION_DIMENSION,
            output_dimension=self.config.output_dimension,
            steps_executed=len(trace),
            step_trace=tuple(trace),
            device=self.device,
            checkpoint_sha256=self.checkpoint_sha256,
            dataset_sha256=self.dataset_sha256,
        )
        self.last_sample = result
        return result

    # Predictor spelling used by a few offline callers; it delegates to the
    # one fixed sample API and does not introduce a second sampler contract.
    predict = sample


@dataclass(frozen=True)
class FormalTrainingResultV1:
    mode: str
    predictor: FormalDDPMPredictorV1
    dataset_manifest: FormalDatasetManifestV1
    checkpoint_manifest: FormalCheckpointManifestV1
    epochs: int
    final_loss: float
    active_enabled: bool = False
    shadow_only: bool = True

    def __post_init__(self) -> None:
        if self.epochs <= 0 or not math.isfinite(self.final_loss) or self.final_loss < 0.0:
            raise ValueError("formal training result metrics are invalid")
        if self.mode != self.predictor.config.mode:
            raise ValueError("formal training result mode mismatch")
        if self.active_enabled is not False or self.shadow_only is not True:
            raise ValueError("formal training result must remain inactive and shadow-only")

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": FORMAL_DDPM_SCHEMA_V1,
            "mode": self.mode,
            "epochs": self.epochs,
            "final_loss": self.final_loss,
            "dataset_manifest": self.dataset_manifest.as_json(),
            "checkpoint_manifest": self.checkpoint_manifest.as_json(),
            "active_enabled": self.active_enabled,
            "shadow_only": self.shadow_only,
        }


class FormalDDPMTrainerV1:
    """Small deterministic offline trainer for the formal DDPM path."""

    def __init__(self, *, config: FormalModelConfigV1, seed: int = 42) -> None:
        self.config = config
        self.seed = int(seed)

    def train(
        self,
        observations: Sequence[Sequence[float]],
        targets: Sequence[Sequence[float]],
        *,
        dataset_id: str = "formal_v4_dataset",
        source_hashes: Mapping[str, str] | None = None,
        epochs: int = 1,
        learning_rate: float = 1.0e-3,
    ) -> FormalTrainingResultV1:
        if epochs <= 0 or not math.isfinite(learning_rate) or learning_rate <= 0.0:
            raise ValueError("formal training epochs/learning_rate are invalid")
        condition, labels = _finite_training_arrays(
            observations,
            targets,
            int(self.config.output_dimension),
        )
        diffusion_labels = _normalize_training_targets(labels, mode=self.config.mode)
        dataset_manifest = FormalDatasetManifestV1.from_arrays(
            condition,
            labels,
            dataset_id=dataset_id,
            mode=self.config.mode,
            source_hashes=source_hashes,
        )
        noise_model = (
            _TorchFormalNoiseModel(self.config, seed=self.seed)
            if self.config.device.startswith("cuda")
            else _NumpyFormalNoiseModel(self.config, seed=self.seed)
        )
        rng = np.random.default_rng(self.seed)
        losses: list[float] = []
        for _ in range(epochs):
            step_indices = rng.integers(0, FORMAL_SAMPLER_STEPS, size=condition.shape[0])
            noise = rng.normal(size=diffusion_labels.shape)
            alpha_bar = _alpha_bar_schedule(self.config)
            clean_scale = np.sqrt(alpha_bar[step_indices])[:, None]
            noise_scale = np.sqrt(1.0 - alpha_bar[step_indices])[:, None]
            noisy = clean_scale * diffusion_labels + noise_scale * noise
            losses.append(
                noise_model.train_step(
                    condition,
                    noisy,
                    step_indices,
                    noise,
                    learning_rate=learning_rate,
                )
            )
        checkpoint_sha = hashlib.sha256(noise_model.state_bytes()).hexdigest()
        checkpoint_manifest = FormalCheckpointManifestV1(
            checkpoint_id=f"{dataset_id}:{self.config.mode}:shadow",
            mode=self.config.mode,
            checkpoint_sha256=checkpoint_sha,
            dataset_sha256=dataset_manifest.dataset_sha256,
            source_hashes={
                "formal_model_source": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                **dict(dataset_manifest.source_hashes),
            },
            output_dimension=self.config.output_dimension,
        )
        predictor = FormalDDPMPredictorV1(
            config=self.config,
            checkpoint_manifest=checkpoint_manifest,
            dataset_manifest=dataset_manifest,
            seed=self.seed,
            _noise_model=noise_model,
        )
        return FormalTrainingResultV1(
            mode=self.config.mode,
            predictor=predictor,
            dataset_manifest=dataset_manifest,
            checkpoint_manifest=checkpoint_manifest,
            epochs=epochs,
            final_loss=float(losses[-1]),
        )


def train_formal_ddpm_v1(
    observations: Sequence[Sequence[float]],
    targets: Sequence[Sequence[float]],
    *,
    mode: str,
    epochs: int = 1,
    seed: int = 42,
    device: str = FORMAL_DEVICE_DEFAULT,
    dataset_id: str = "formal_v4_dataset",
    source_hashes: Mapping[str, str] | None = None,
) -> FormalTrainingResultV1:
    """Train the separate formal model and return its bound predictor."""

    config = FormalModelConfigV1(mode=mode, device=device)
    return FormalDDPMTrainerV1(config=config, seed=seed).train(
        observations,
        targets,
        dataset_id=dataset_id,
        source_hashes=source_hashes,
        epochs=epochs,
    )


# Concise aliases for callers that describe the object as a sampler rather
# than a predictor.  Both names resolve to the same fixed API.
FormalDDPMSamplerV1 = FormalDDPMPredictorV1
FormalPredictorV1 = FormalDDPMPredictorV1
FormalDDPMConfigV1 = FormalModelConfigV1


__all__ = [
    "FORMAL_BETA_END",
    "FORMAL_BETA_START",
    "FORMAL_CHECKPOINT_MANIFEST_SCHEMA_V1",
    "FORMAL_DATASET_MANIFEST_SCHEMA_V1",
    "FORMAL_DDPM_SCHEMA_V1",
    "FORMAL_MODEL_CONFIG_SCHEMA_V1",
    "FORMAL_MODEL_MODES",
    "FORMAL_MODEL_OUTPUT_DIMENSIONS",
    "FormalCheckpointManifestV1",
    "FormalDatasetManifestV1",
    "FormalDDPMConfigV1",
    "FormalDDPMPredictorV1",
    "FormalDDPMSamplerV1",
    "FormalDDPMSampleV1",
    "FormalDDPMTrainerV1",
    "FormalModelConfigV1",
    "FormalPredictorV1",
    "FormalTrainingResultV1",
    "build_variable_k_training_labels",
    "train_formal_ddpm_v1",
]
