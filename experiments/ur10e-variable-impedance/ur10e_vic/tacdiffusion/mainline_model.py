"""Shared conditional 84D-to-12D DDPM model for offline/runtime use.

The production model is a real conditional denoiser: every prediction sees
the noisy 12D action, the 84D temporal observation, and a timestep embedding.
Training samples noise and timesteps from a deterministic 50-step schedule;
sampling traverses all 50 distinct reverse timesteps.  There is no task-ID
input and no observation-only or random-linear production fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import time
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class MainlineModelConfig:
    schema_version: str = "ur10e_tacdiffusion_model/v3"
    observation_dimension: int = 84
    action_dimension: int = 12
    model_update_rate_hz: int = 100
    diffusion_steps: int = 50
    hidden_dimension: int = 512
    timestep_embedding_dimension: int = 64
    beta_start: float = 1e-4
    beta_end: float = 0.02

    def __post_init__(self) -> None:
        if self.observation_dimension != 84 or self.action_dimension != 12:
            raise ValueError("mainline model must use 84D observation and 12D action")
        if self.model_update_rate_hz not in {50, 100} or self.diffusion_steps != 50:
            raise ValueError("mainline model rate/denoising contract is incompatible")
        if not 0.0 < self.beta_start < self.beta_end < 1.0:
            raise ValueError("diffusion beta schedule is invalid")


def _schedule_numpy(config: MainlineModelConfig) -> tuple[np.ndarray, ...]:
    beta = np.linspace(config.beta_start, config.beta_end, config.diffusion_steps, dtype=np.float32)
    alpha = 1.0 - beta
    alpha_bar = np.cumprod(alpha)
    return beta, alpha, alpha_bar


def _sinusoidal_timestep(timestep, dimension: int, *, torch_module=None):
    """Deterministic sinusoidal embedding shared by torch and CPU fallback."""

    if torch_module is not None:
        torch = torch_module
        half = dimension // 2
        scale = math.log(10000.0) / max(1, half - 1)
        frequency = torch.exp(torch.arange(half, device=timestep.device, dtype=torch.float32) * -scale)
        phase = timestep.to(torch.float32).reshape(-1, 1) * frequency.reshape(1, -1)
        embedding = torch.cat((torch.sin(phase), torch.cos(phase)), dim=1)
        return embedding[:, :dimension]
    values = np.asarray(timestep, dtype=np.float32).reshape(-1, 1)
    half = dimension // 2
    frequency = np.exp(-math.log(10000.0) * np.arange(half, dtype=np.float32) / max(1, half - 1))
    phase = values * frequency.reshape(1, -1)
    return np.concatenate((np.sin(phase), np.cos(phase)), axis=1)[:, :dimension]


try:
    import torch
    from torch import nn
except ImportError:  # pragma: no cover - exercised only on minimal hosts
    torch = None
    nn = None


if nn is not None:
    class ConditionalActionModel(nn.Module):
        """Shared conditional epsilon denoiser; no discrete task identity."""

        def __init__(self, config: MainlineModelConfig = MainlineModelConfig()) -> None:
            super().__init__()
            self.config = config
            hidden = config.hidden_dimension
            self.observation_projection = nn.Linear(config.observation_dimension, hidden)
            self.action_projection = nn.Linear(config.action_dimension, hidden)
            self.timestep_projection = nn.Sequential(
                nn.Linear(config.timestep_embedding_dimension, hidden),
                nn.GELU(),
                nn.Linear(hidden, hidden),
            )
            self.network = nn.Sequential(
                nn.LayerNorm(hidden),
                nn.GELU(),
                nn.Linear(hidden, hidden),
                nn.GELU(),
                nn.Linear(hidden, config.action_dimension),
            )

        def forward(self, noisy_action, observation, timestep):
            if noisy_action.shape[-1] != 12 or observation.shape[-1] != 84:
                raise ValueError("denoiser requires noisy 12D action and 84D observation")
            time_embedding = _sinusoidal_timestep(
                timestep, self.config.timestep_embedding_dimension, torch_module=torch
            )
            hidden = (
                self.observation_projection(observation)
                + self.action_projection(noisy_action)
                + self.timestep_projection(time_embedding)
            )
            return self.network(hidden)

        def sample(
            self,
            observation,
            *,
            seed: int = 42,
            return_trace: bool = False,
        ):
            device = observation.device
            generator = torch.Generator(device=device).manual_seed(seed)
            action = torch.randn(
                (observation.shape[0], self.config.action_dimension),
                generator=generator,
                device=device,
            )
            beta = torch.linspace(self.config.beta_start, self.config.beta_end, self.config.diffusion_steps, device=device)
            alpha = 1.0 - beta
            alpha_bar = torch.cumprod(alpha, dim=0)
            trace: list[int] = []
            for timestep_index in range(self.config.diffusion_steps - 1, -1, -1):
                trace.append(timestep_index)
                timestep = torch.full(
                    (observation.shape[0],), timestep_index, dtype=torch.long, device=device
                )
                predicted_noise = self.forward(action, observation, timestep)
                beta_t = beta[timestep_index]
                alpha_t = alpha[timestep_index]
                alpha_bar_t = alpha_bar[timestep_index]
                mean = (action - beta_t * predicted_noise / torch.sqrt(1.0 - alpha_bar_t)) / torch.sqrt(alpha_t)
                if timestep_index:
                    action = mean + torch.sqrt(beta_t) * torch.randn(action.shape, generator=generator, device=device)
                else:
                    action = mean
            return (action, tuple(trace)) if return_trace else action
else:
    class ConditionalActionModel:
        """Deterministic CPU epsilon denoiser fallback with trainable weights."""

        def __init__(self, config: MainlineModelConfig = MainlineModelConfig(), *, seed: int = 42) -> None:
            self.config = config
            rng = np.random.default_rng(seed)
            hidden = config.hidden_dimension
            input_dimension = 84 + 12 + config.timestep_embedding_dimension
            self.w1 = rng.standard_normal((input_dimension, hidden), dtype=np.float32) / math.sqrt(input_dimension)
            self.b1 = np.zeros(hidden, dtype=np.float32)
            self.w2 = rng.standard_normal((hidden, 12), dtype=np.float32) / math.sqrt(hidden)
            self.b2 = np.zeros(12, dtype=np.float32)

        def forward(self, noisy_action: np.ndarray, observation: np.ndarray, timestep: np.ndarray) -> np.ndarray:
            noisy = np.asarray(noisy_action, dtype=np.float32)
            obs = np.asarray(observation, dtype=np.float32)
            if noisy.shape[-1] != 12 or obs.shape[-1] != 84:
                raise ValueError("denoiser requires noisy 12D action and 84D observation")
            features = np.concatenate((noisy, obs, _sinusoidal_timestep(timestep, self.config.timestep_embedding_dimension)), axis=1)
            hidden = np.tanh(features @ self.w1 + self.b1)
            return hidden @ self.w2 + self.b2

        __call__ = forward

        def fit(self, observations: np.ndarray, actions: np.ndarray, *, epochs: int, learning_rate: float, seed: int) -> list[float]:
            beta, alpha, alpha_bar = _schedule_numpy(self.config)
            rng = np.random.default_rng(seed)
            losses: list[float] = []
            for _ in range(epochs):
                timestep = rng.integers(0, self.config.diffusion_steps, size=observations.shape[0], dtype=np.int64)
                noise = rng.standard_normal(actions.shape, dtype=np.float32)
                noisy = np.sqrt(alpha_bar[timestep, None]) * actions + np.sqrt(1.0 - alpha_bar[timestep, None]) * noise
                features = np.concatenate((noisy, observations, _sinusoidal_timestep(timestep, self.config.timestep_embedding_dimension)), axis=1)
                hidden_pre = features @ self.w1 + self.b1
                hidden = np.tanh(hidden_pre)
                prediction = hidden @ self.w2 + self.b2
                error = prediction - noise
                losses.append(float(np.mean(error * error)))
                count = max(1, observations.shape[0])
                grad_output = 2.0 * error / count
                grad_w2 = hidden.T @ grad_output
                grad_b2 = grad_output.sum(axis=0)
                grad_hidden = (grad_output @ self.w2.T) * (1.0 - hidden * hidden)
                self.w1 -= learning_rate * (features.T @ grad_hidden)
                self.b1 -= learning_rate * grad_hidden.sum(axis=0)
                self.w2 -= learning_rate * grad_w2
                self.b2 -= learning_rate * grad_b2
            return losses

        def sample(self, observation: np.ndarray, *, seed: int = 42, return_trace: bool = False):
            obs = np.asarray(observation, dtype=np.float32)
            if obs.ndim == 1:
                obs = obs[None, :]
            rng = np.random.default_rng(seed)
            action = rng.standard_normal((obs.shape[0], 12), dtype=np.float32)
            beta, alpha, alpha_bar = _schedule_numpy(self.config)
            trace: list[int] = []
            for timestep_index in range(self.config.diffusion_steps - 1, -1, -1):
                trace.append(timestep_index)
                timestep = np.full((obs.shape[0],), timestep_index, dtype=np.int64)
                predicted_noise = self.forward(action, obs, timestep)
                mean = (action - beta[timestep_index] * predicted_noise / np.sqrt(1.0 - alpha_bar[timestep_index])) / np.sqrt(alpha[timestep_index])
                action = mean if timestep_index == 0 else mean + np.sqrt(beta[timestep_index]) * rng.standard_normal(action.shape, dtype=np.float32)
            return (action, tuple(trace)) if return_trace else action


class DeterministicMainlinePredictor:
    """Test-only deterministic linear predictor; never used by training/runtime."""

    def __init__(self, config: MainlineModelConfig = MainlineModelConfig(), *, seed: int = 42) -> None:
        self.config = config
        rng = np.random.default_rng(seed)
        self._weights = rng.standard_normal((config.observation_dimension, config.action_dimension), dtype=np.float32) / math.sqrt(config.observation_dimension)

    def predict(self, observation: Sequence[float]) -> tuple[float, ...]:
        vector = np.asarray(observation, dtype=np.float32)
        if vector.shape != (self.config.observation_dimension,) or not np.isfinite(vector).all():
            raise ValueError("model observation is not a finite 84D vector")
        return tuple(float(value) for value in np.tanh(vector @ self._weights))


def _normalized_arrays(observations: np.ndarray, actions: np.ndarray, train_idx: np.ndarray):
    x_train = observations[train_idx].astype(np.float32)
    y_train = actions[train_idx].astype(np.float32)
    x_mean = x_train.mean(axis=0)
    x_std = np.maximum(x_train.std(axis=0), 1e-6)
    y_mean = y_train.mean(axis=0)
    y_std = np.maximum(y_train.std(axis=0), 1e-6)
    return x_mean, x_std, y_mean, y_std


def _fsync_parent(path: Path) -> None:
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_numpy_checkpoint(path: Path, **arrays: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w+b", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            np.savez(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_torch_checkpoint(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("w+b", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            torch.save(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def train_mainline_model(
    observations: np.ndarray,
    actions: np.ndarray,
    splits: Sequence[str],
    *,
    checkpoint_path: str | Path,
    config: MainlineModelConfig = MainlineModelConfig(),
    epochs: int = 2,
    batch_size: int = 64,
    learning_rate: float = 1e-3,
    seed: int = 42,
    checkpoint_binding: dict[str, object] | None = None,
    require_cuda: bool = False,
) -> dict[str, object]:
    if observations.ndim != 2 or actions.ndim != 2 or observations.shape[1] != 84 or actions.shape[1] != 12 or len(splits) != observations.shape[0]:
        raise ValueError("train_mainline_model requires [N,84], [N,12], and matching splits")
    if not np.isfinite(observations).all() or not np.isfinite(actions).all() or any(split not in {"train", "validation", "test"} for split in splits):
        raise ValueError("training data contains non-finite values or unsupported splits")
    if require_cuda and (torch is None or not torch.cuda.is_available()):
        raise RuntimeError("CUDA is required for this qualification path")
    train_idx = np.asarray([i for i, split in enumerate(splits) if split == "train"], dtype=np.int64)
    validation_idx = np.asarray([i for i, split in enumerate(splits) if split == "validation"], dtype=np.int64)
    test_idx = np.asarray([i for i, split in enumerate(splits) if split == "test"], dtype=np.int64)
    if train_idx.size == 0 or validation_idx.size == 0:
        raise ValueError("frozen train/validation split must both be non-empty")
    x_mean, x_std, y_mean, y_std = _normalized_arrays(observations, actions, train_idx)
    checkpoint = Path(checkpoint_path)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    if torch is None:
        model = ConditionalActionModel(config, seed=seed)
        x_train = (observations[train_idx].astype(np.float32) - x_mean) / x_std
        y_train = (actions[train_idx].astype(np.float32) - y_mean) / y_std
        losses = model.fit(x_train, y_train, epochs=epochs, learning_rate=learning_rate, seed=seed)
        validation_prediction = model.sample((observations[validation_idx].astype(np.float32) - x_mean) / x_std, seed=seed)
        validation_target = (actions[validation_idx].astype(np.float32) - y_mean) / y_std
        validation_loss = float(np.mean((validation_prediction - validation_target) ** 2))
        _atomic_numpy_checkpoint(checkpoint, schema="ur10e_tacdiffusion_checkpoint/v3", config=json.dumps(config.__dict__, sort_keys=True), w1=model.w1, b1=model.b1, w2=model.w2, b2=model.b2, observation_mean=x_mean, observation_std=x_std, action_mean=y_mean, action_std=y_std, train_indices=train_idx, validation_indices=validation_idx, test_indices=test_idx, losses=np.asarray(losses), validation_loss=validation_loss, checkpoint_binding=json.dumps(checkpoint_binding or {}, sort_keys=True))
        return {"checkpoint_path": str(checkpoint), "training_loss": losses, "validation_loss": validation_loss, "train_count": int(train_idx.size), "validation_count": int(validation_idx.size), "test_count": int(test_idx.size), "model_schema": "ur10e_tacdiffusion_checkpoint/v3", "runtime": "numpy_cpu_fallback", "diffusion_steps": config.diffusion_steps}

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    use_cuda = require_cuda or torch.cuda.is_available()
    device = torch.device("cuda", int(torch.cuda.current_device())) if use_cuda else torch.device("cpu")
    model = ConditionalActionModel(config).to(device)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    x = torch.from_numpy(observations.astype(np.float32)).to(device)
    y = torch.from_numpy(actions.astype(np.float32)).to(device)
    train_index_tensor = torch.from_numpy(train_idx).to(device)
    validation_index_tensor = torch.from_numpy(validation_idx).to(device)
    mean = torch.from_numpy(x_mean).to(device)
    std = torch.from_numpy(x_std).to(device)
    action_mean = torch.from_numpy(y_mean).to(device)
    action_std = torch.from_numpy(y_std).to(device)
    beta = torch.linspace(config.beta_start, config.beta_end, config.diffusion_steps, device=device)
    alpha_bar = torch.cumprod(1.0 - beta, dim=0)
    generator = torch.Generator(device=device).manual_seed(seed)
    losses: list[float] = []
    model.train()
    for _ in range(epochs):
        permutation = train_index_tensor[torch.randperm(train_idx.size, generator=generator, device=device)]
        epoch_loss = 0.0
        batches = 0
        for start in range(0, len(permutation), batch_size):
            batch = permutation[start : start + batch_size]
            timestep = torch.randint(0, config.diffusion_steps, (batch.numel(),), generator=generator, device=device)
            noise = torch.randn((batch.numel(), 12), generator=generator, device=device)
            clean = (y[batch] - action_mean) / action_std
            noisy = torch.sqrt(alpha_bar[timestep, None]) * clean + torch.sqrt(1.0 - alpha_bar[timestep, None]) * noise
            prediction = model(noisy, (x[batch] - mean) / std, timestep)
            loss = torch.mean((prediction - noise) ** 2)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.detach().cpu())
            batches += 1
        losses.append(epoch_loss / max(1, batches))
    model.eval()
    with torch.no_grad():
        validation_batch = validation_index_tensor
        validation_timestep = torch.zeros((validation_batch.numel(),), dtype=torch.long, device=device)
        validation_noise = torch.zeros((validation_batch.numel(), 12), device=device)
        validation_clean = (y[validation_batch] - action_mean) / action_std
        validation_noisy = torch.sqrt(alpha_bar[validation_timestep, None]) * validation_clean
        validation_prediction = model(validation_noisy, (x[validation_batch] - mean) / std, validation_timestep)
        validation_loss = float(torch.mean((validation_prediction - validation_noise) ** 2).detach().cpu())
    payload = {
        "schema": "ur10e_tacdiffusion_checkpoint/v3",
        "config": config.__dict__,
        "model_state_dict": model.state_dict(),
        "normalization": {
            "observation_mean": mean.detach().cpu(),
            "observation_std": std.detach().cpu(),
            "action_mean": action_mean.detach().cpu(),
            "action_std": action_std.detach().cpu(),
        },
        "train_indices": train_idx.tolist(),
        "validation_indices": validation_idx.tolist(),
        "test_indices": test_idx.tolist(),
        "losses": losses,
        "validation_loss": validation_loss,
        "checkpoint_binding": checkpoint_binding or {},
        "optimizer_state_dict": optimizer.state_dict(),
        "generator_state": generator.get_state(),
        "epoch_completed": int(epochs),
    }
    _atomic_torch_checkpoint(checkpoint, payload)
    return {"checkpoint_path": str(checkpoint), "training_loss": losses, "validation_loss": validation_loss, "train_count": int(train_idx.size), "validation_count": int(validation_idx.size), "test_count": int(test_idx.size), "model_schema": payload["schema"], "runtime": str(device), "diffusion_steps": config.diffusion_steps}


def load_mainline_checkpoint(checkpoint_path: str | Path, *, require_cuda: bool = False) -> dict[str, object]:
    """Strictly load a v3 trained artifact and return its loaded model bundle."""

    checkpoint = Path(checkpoint_path)
    if not checkpoint.is_file():
        raise ValueError("mainline checkpoint is missing")
    expected_config_fields = set(MainlineModelConfig.__dataclass_fields__)
    if require_cuda and (torch is None or not torch.cuda.is_available()):
        raise RuntimeError("CUDA is required for this publication benchmark")

    def validate_training_metadata(payload: dict[str, object]) -> None:
        if not isinstance(payload["checkpoint_binding"], dict):
            raise ValueError("mainline checkpoint binding metadata is invalid")
        train_indices = payload["train_indices"]
        validation_indices = payload["validation_indices"]
        test_indices = payload.get("test_indices", [])
        if not isinstance(train_indices, (list, tuple)) or not isinstance(validation_indices, (list, tuple)):
            raise ValueError("mainline checkpoint split indices are invalid")
        if not isinstance(test_indices, (list, tuple)):
            raise ValueError("mainline checkpoint split indices are invalid")
        all_indices = (*train_indices, *validation_indices, *test_indices)
        if not train_indices or not validation_indices or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in all_indices):
            raise ValueError("mainline checkpoint split indices are invalid")
        if (
            set(train_indices) & set(validation_indices)
            or set(train_indices) & set(test_indices)
            or set(validation_indices) & set(test_indices)
        ):
            raise ValueError("mainline checkpoint train/validation split overlaps")
        if sorted(all_indices) != list(range(max(all_indices) + 1)):
            raise ValueError("mainline checkpoint split indices are outside a coherent sample range")
        losses = payload["losses"]
        validation_loss = payload["validation_loss"]
        if not isinstance(losses, (list, tuple)) or not losses or any(not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in losses):
            raise ValueError("mainline checkpoint losses are invalid")
        if not isinstance(validation_loss, (int, float)) or not math.isfinite(float(validation_loss)):
            raise ValueError("mainline checkpoint validation loss is invalid")

    if torch is not None:
        try:
            payload = torch.load(checkpoint, map_location="cuda" if require_cuda else "cpu", weights_only=False)
        except Exception as exc:
            raise ValueError("mainline torch checkpoint is invalid") from exc
        required_fields = {"schema", "config", "model_state_dict", "normalization", "train_indices", "validation_indices", "losses", "validation_loss", "checkpoint_binding"}
        optional_fields = {"optimizer_state_dict", "generator_state", "epoch_completed", "test_indices"}
        if not isinstance(payload, dict) or not required_fields <= set(payload) or not set(payload) <= required_fields | optional_fields:
            raise ValueError("mainline checkpoint fields are missing or extra")
        if payload["schema"] != "ur10e_tacdiffusion_checkpoint/v3" or not isinstance(payload["config"], dict) or set(payload["config"]) != expected_config_fields:
            raise ValueError("mainline checkpoint schema/config is invalid")
        try:
            config = MainlineModelConfig(**payload["config"])
        except (TypeError, ValueError) as exc:
            raise ValueError("mainline checkpoint config is invalid") from exc
        validate_training_metadata(payload)
        if "epoch_completed" in payload and (
            isinstance(payload["epoch_completed"], bool)
            or not isinstance(payload["epoch_completed"], int)
            or payload["epoch_completed"] < 0
        ):
            raise ValueError("mainline checkpoint epoch metadata is invalid")
        device = torch.device("cuda", int(torch.cuda.current_device())) if require_cuda else torch.device("cpu")
        model = ConditionalActionModel(config).to(device)
        try:
            state_dict = payload["model_state_dict"]
            if not isinstance(state_dict, dict) or any(not isinstance(value, torch.Tensor) or not torch.isfinite(value).all() for value in state_dict.values()):
                raise ValueError("mainline checkpoint model state is non-finite")
            model.load_state_dict(state_dict, strict=True)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("mainline checkpoint model state is invalid") from exc
        normalization = payload["normalization"]
        if not isinstance(normalization, dict) or set(normalization) != {"observation_mean", "observation_std", "action_mean", "action_std"}:
            raise ValueError("mainline checkpoint normalization is invalid")
        for key, dimension in (("observation_mean", 84), ("observation_std", 84), ("action_mean", 12), ("action_std", 12)):
            value = normalization[key]
            if not isinstance(value, torch.Tensor) or tuple(value.shape) != (dimension,) or not torch.isfinite(value).all():
                raise ValueError("mainline checkpoint normalization dimensions/values are invalid")
            if key.endswith("_std") and not torch.all(value > 0):
                raise ValueError("mainline checkpoint normalization std must be strictly positive")
    else:
        if require_cuda:
            raise RuntimeError("CUDA is required for this publication benchmark")
        try:
            with np.load(checkpoint, allow_pickle=False) as payload:
                required_fields = {"schema", "config", "w1", "b1", "w2", "b2", "observation_mean", "observation_std", "action_mean", "action_std", "train_indices", "validation_indices", "losses", "validation_loss", "checkpoint_binding"}
                optional_fields = {"test_indices"}
                if not required_fields <= set(payload.files) or not set(payload.files) <= required_fields | optional_fields:
                    raise ValueError("mainline checkpoint fields are missing or extra")
                if str(payload["schema"]) != "ur10e_tacdiffusion_checkpoint/v3":
                    raise ValueError("mainline checkpoint schema is invalid")
                config = MainlineModelConfig(**json.loads(str(payload["config"])))
                model = ConditionalActionModel(config, seed=0)
                model.w1 = np.asarray(payload["w1"], dtype=np.float32)
                model.b1 = np.asarray(payload["b1"], dtype=np.float32)
                model.w2 = np.asarray(payload["w2"], dtype=np.float32)
                model.b2 = np.asarray(payload["b2"], dtype=np.float32)
                normalization = {key: np.asarray(payload[key]) for key in ("observation_mean", "observation_std", "action_mean", "action_std")}
                expected_shapes = {
                    "w1": (84 + 12 + config.timestep_embedding_dimension, config.hidden_dimension),
                    "b1": (config.hidden_dimension,),
                    "w2": (config.hidden_dimension, 12),
                    "b2": (12,),
                }
                for key, shape in expected_shapes.items():
                    if getattr(model, key).shape != shape or not np.isfinite(getattr(model, key)).all():
                        raise ValueError("mainline CPU checkpoint model state is invalid")
                for key, dimension in (("observation_mean", 84), ("observation_std", 84), ("action_mean", 12), ("action_std", 12)):
                    if normalization[key].shape != (dimension,) or not np.isfinite(normalization[key]).all():
                        raise ValueError("mainline CPU checkpoint normalization is invalid")
                    if key.endswith("_std") and not np.all(normalization[key] > 0):
                        raise ValueError("mainline CPU checkpoint normalization std must be strictly positive")
                parsed_metadata = {
                    "checkpoint_binding": json.loads(str(payload["checkpoint_binding"])),
                    "train_indices": np.asarray(payload["train_indices"]).tolist(),
                    "validation_indices": np.asarray(payload["validation_indices"]).tolist(),
                    "test_indices": np.asarray(payload["test_indices"]).tolist() if "test_indices" in payload.files else [],
                    "losses": np.asarray(payload["losses"]).tolist(),
                    "validation_loss": float(payload["validation_loss"]),
                }
                validate_training_metadata(parsed_metadata)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if isinstance(exc, ValueError):
                raise
            raise ValueError("mainline CPU checkpoint is invalid") from exc
        device = "cpu"
    model._mainline_checkpoint_loaded = True
    if torch is not None:
        model.eval()
    return {
        "model": model,
        "config": config,
        "normalization": normalization,
        "schema": "ur10e_tacdiffusion_checkpoint/v3",
        "device": str(device),
        "checkpoint_path": str(checkpoint),
        "epoch_completed": int(payload.get("epoch_completed", len(payload["losses"]))) if isinstance(payload, dict) else 0,
    }


def resume_mainline_model(
    observations: np.ndarray,
    actions: np.ndarray,
    splits: Sequence[str],
    *,
    checkpoint_path: str | Path,
    resumed_checkpoint_path: str | Path,
    epochs: int = 1,
    batch_size: int = 64,
    learning_rate: float = 1e-3,
    seed: int = 42,
    checkpoint_binding: dict[str, object] | None = None,
    require_cuda: bool = False,
) -> dict[str, object]:
    """Continue a loaded v3 checkpoint without changing its architecture.

    The qualification lane uses this helper with ``require_cuda=True``.  A
    checkpoint without the optional optimizer/generator state is still
    loadable for backwards compatibility, but its receipt reports that the
    optimizer state was not restored rather than silently claiming a resume.
    """

    if torch is None:
        raise RuntimeError("PyTorch is required to resume a mainline checkpoint")
    if require_cuda and not torch.cuda.is_available():
        raise RuntimeError("CUDA is required for this qualification path")
    if epochs <= 0 or batch_size <= 0:
        raise ValueError("resume epochs and batch_size must be positive")
    observations = np.asarray(observations, dtype=np.float32)
    actions = np.asarray(actions, dtype=np.float32)
    if observations.ndim != 2 or actions.ndim != 2 or observations.shape[1] != 84 or actions.shape[1] != 12 or observations.shape[0] != actions.shape[0] or len(splits) != observations.shape[0]:
        raise ValueError("resume_mainline_model requires [N,84], [N,12], and matching splits")
    if not np.isfinite(observations).all() or not np.isfinite(actions).all() or any(split not in {"train", "validation", "test"} for split in splits):
        raise ValueError("resume data contains non-finite values or unsupported splits")
    train_idx = np.asarray([i for i, split in enumerate(splits) if split == "train"], dtype=np.int64)
    validation_idx = np.asarray([i for i, split in enumerate(splits) if split == "validation"], dtype=np.int64)
    test_idx = np.asarray([i for i, split in enumerate(splits) if split == "test"], dtype=np.int64)
    if train_idx.size == 0 or validation_idx.size == 0:
        raise ValueError("resume train/validation split must both be non-empty")

    checkpoint = Path(checkpoint_path)
    if not checkpoint.is_file():
        raise ValueError("resume source checkpoint is missing")
    map_location = "cuda" if require_cuda else "cpu"
    try:
        raw_payload = torch.load(checkpoint, map_location=map_location, weights_only=False)
    except Exception as exc:
        raise ValueError("resume source checkpoint is invalid") from exc
    if not isinstance(raw_payload, dict):
        raise ValueError("resume source checkpoint is invalid")
    loaded = load_mainline_checkpoint(checkpoint, require_cuda=require_cuda)
    config = loaded["config"]
    model = loaded["model"]
    device = next(model.parameters()).device
    if checkpoint_binding is not None and raw_payload.get("checkpoint_binding") != checkpoint_binding:
        raise ValueError("resume source checkpoint binding mismatch")

    saved_train = tuple(int(value) for value in raw_payload["train_indices"])
    saved_validation = tuple(int(value) for value in raw_payload["validation_indices"])
    saved_test = tuple(int(value) for value in raw_payload.get("test_indices", []))
    if (saved_train, saved_validation, saved_test) != (tuple(train_idx.tolist()), tuple(validation_idx.tolist()), tuple(test_idx.tolist())):
        raise ValueError("resume source checkpoint split mismatch")
    normalization = loaded["normalization"]
    mean = normalization["observation_mean"].to(device)
    std = normalization["observation_std"].to(device)
    action_mean = normalization["action_mean"].to(device)
    action_std = normalization["action_std"].to(device)
    # Fixture arrays may be mmap-backed and read-only.  Materialize writable
    # float32 copies before exposing their storage to PyTorch.
    x = torch.from_numpy(np.array(observations, dtype=np.float32, copy=True)).to(device)
    y = torch.from_numpy(np.array(actions, dtype=np.float32, copy=True)).to(device)
    train_index_tensor = torch.from_numpy(train_idx).to(device)
    validation_index_tensor = torch.from_numpy(validation_idx).to(device)
    beta = torch.linspace(config.beta_start, config.beta_end, config.diffusion_steps, device=device)
    alpha_bar = torch.cumprod(1.0 - beta, dim=0)
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    optimizer_state_restored = isinstance(raw_payload.get("optimizer_state_dict"), dict)
    if optimizer_state_restored:
        try:
            optimizer.load_state_dict(raw_payload["optimizer_state_dict"])
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("resume optimizer state is invalid") from exc
    generator = torch.Generator(device=device)
    generator_state_restored = isinstance(raw_payload.get("generator_state"), torch.Tensor)
    if generator_state_restored:
        try:
            # ``map_location="cuda"`` moves every tensor in the checkpoint,
            # while Generator.set_state still requires a CPU ByteTensor.
            generator.set_state(raw_payload["generator_state"].detach().cpu())
        except (RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("resume generator state is invalid") from exc
    else:
        generator.manual_seed(seed)

    previous_losses = [float(value) for value in raw_payload["losses"]]
    losses: list[float] = []
    model.train()
    for _ in range(epochs):
        permutation = train_index_tensor[torch.randperm(train_idx.size, generator=generator, device=device)]
        epoch_loss = 0.0
        batches = 0
        for start in range(0, len(permutation), batch_size):
            batch = permutation[start : start + batch_size]
            timestep = torch.randint(0, config.diffusion_steps, (batch.numel(),), generator=generator, device=device)
            noise = torch.randn((batch.numel(), 12), generator=generator, device=device)
            clean = (y[batch] - action_mean) / action_std
            noisy = torch.sqrt(alpha_bar[timestep, None]) * clean + torch.sqrt(1.0 - alpha_bar[timestep, None]) * noise
            prediction = model(noisy, (x[batch] - mean) / std, timestep)
            loss = torch.mean((prediction - noise) ** 2)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.detach().cpu())
            batches += 1
        losses.append(epoch_loss / max(1, batches))
    model.eval()
    with torch.no_grad():
        validation_batch = validation_index_tensor
        validation_timestep = torch.zeros((validation_batch.numel(),), dtype=torch.long, device=device)
        validation_noise = torch.zeros((validation_batch.numel(), 12), device=device)
        validation_clean = (y[validation_batch] - action_mean) / action_std
        validation_noisy = torch.sqrt(alpha_bar[validation_timestep, None]) * validation_clean
        validation_prediction = model(validation_noisy, (x[validation_batch] - mean) / std, validation_timestep)
        validation_loss = float(torch.mean((validation_prediction - validation_noise) ** 2).detach().cpu())

    epoch_completed = int(raw_payload.get("epoch_completed", len(previous_losses))) + epochs
    payload = {
        "schema": "ur10e_tacdiffusion_checkpoint/v3",
        "config": config.__dict__,
        "model_state_dict": model.state_dict(),
        "normalization": {
            "observation_mean": mean.detach().cpu(),
            "observation_std": std.detach().cpu(),
            "action_mean": action_mean.detach().cpu(),
            "action_std": action_std.detach().cpu(),
        },
        "train_indices": train_idx.tolist(),
        "validation_indices": validation_idx.tolist(),
        "test_indices": test_idx.tolist(),
        "losses": previous_losses + losses,
        "validation_loss": validation_loss,
        "checkpoint_binding": checkpoint_binding if checkpoint_binding is not None else raw_payload.get("checkpoint_binding", {}),
        "optimizer_state_dict": optimizer.state_dict(),
        "generator_state": generator.get_state(),
        "epoch_completed": epoch_completed,
    }
    resumed_checkpoint = Path(resumed_checkpoint_path)
    _atomic_torch_checkpoint(resumed_checkpoint, payload)
    return {
        "checkpoint_path": str(resumed_checkpoint),
        "training_loss": losses,
        "validation_loss": validation_loss,
        "train_count": int(train_idx.size),
        "validation_count": int(validation_idx.size),
        "test_count": int(test_idx.size),
        "model_schema": payload["schema"],
        "runtime": str(device),
        "diffusion_steps": config.diffusion_steps,
        "resume": {
            "resumed": True,
            "source_checkpoint_path": str(checkpoint),
            "optimizer_state_restored": optimizer_state_restored,
            "generator_state_restored": generator_state_restored,
            "epoch_completed": epoch_completed,
        },
    }


def evaluate_mainline_checkpoint(
    checkpoint_path: str | Path,
    observations: np.ndarray,
    actions: np.ndarray,
    splits: Sequence[str],
    *,
    split: str = "test",
    require_cuda: bool = False,
    checkpoint_binding: dict[str, object] | None = None,
    seed: int = 42,
) -> dict[str, object]:
    """Evaluate a loaded 84D/12D checkpoint and prove its 50-step sampler."""

    if split not in {"train", "validation", "test"}:
        raise ValueError("evaluation split is invalid")
    observations = np.asarray(observations, dtype=np.float32)
    actions = np.asarray(actions, dtype=np.float32)
    if observations.ndim != 2 or actions.shape != (observations.shape[0], 12) or observations.shape[1] != 84 or len(splits) != observations.shape[0]:
        raise ValueError("evaluation requires [N,84], [N,12], and matching splits")
    if any(value not in {"train", "validation", "test"} for value in splits):
        raise ValueError("evaluation split labels are invalid")
    indices = np.asarray([index for index, value in enumerate(splits) if value == split], dtype=np.int64)
    if indices.size == 0:
        raise ValueError("evaluation split is empty")
    loaded = load_mainline_checkpoint(checkpoint_path, require_cuda=require_cuda)
    if checkpoint_binding is not None and loaded["model"].config is not None:
        loaded_binding = None
        if torch is not None:
            payload = torch.load(Path(checkpoint_path), map_location="cuda" if require_cuda else "cpu", weights_only=False)
            loaded_binding = payload.get("checkpoint_binding") if isinstance(payload, dict) else None
        if loaded_binding != checkpoint_binding:
            raise ValueError("evaluation checkpoint binding mismatch")
    model = loaded["model"]
    config = loaded["config"]
    normalization = loaded["normalization"]
    model_device = next(model.parameters()).device if torch is not None else "cpu"
    if torch is None:
        normalized_observations = (observations[indices] - normalization["observation_mean"]) / normalization["observation_std"]
        normalized_actions = (actions[indices] - normalization["action_mean"]) / normalization["action_std"]
        predicted, trace = model.sample(normalized_observations, seed=seed, return_trace=True)
        loss = float(np.mean((predicted - normalized_actions) ** 2))
    else:
        observation_tensor = torch.from_numpy(observations[indices]).to(model_device)
        action_tensor = torch.from_numpy(actions[indices]).to(model_device)
        mean = normalization["observation_mean"].to(model_device)
        std = normalization["observation_std"].to(model_device)
        action_mean = normalization["action_mean"].to(model_device)
        action_std = normalization["action_std"].to(model_device)
        with torch.no_grad():
            prediction, trace = model.sample((observation_tensor - mean) / std, seed=seed, return_trace=True)
        normalized_actions = (action_tensor - action_mean) / action_std
        loss = float(torch.mean((prediction - normalized_actions) ** 2).detach().cpu())
    if tuple(trace) != tuple(range(49, -1, -1)) or len(set(trace)) != 50:
        raise RuntimeError("mainline evaluation sampler did not execute all 50 distinct reverse steps")
    return {
        "schema": "ur10e_tacdiffusion_evaluation/v2",
        "checkpoint_path": str(checkpoint_path),
        "split": split,
        "sample_count": int(indices.size),
        "diffusion_steps": config.diffusion_steps,
        "distinct_reverse_steps": len(set(trace)),
        "reverse_step_trace": list(trace),
        "normalized_action_mse": loss,
        "device": str(model_device),
        "require_cuda": require_cuda,
        "checkpoint_binding": checkpoint_binding or {},
        "fixture_only": True,
        "formal_checkpoint": False,
        "active_allowed": False,
        "active": False,
        "reproduction_status": "not_claimed",
        "model_rate_selected_hz": None,
    }


def benchmark_runtime(model: "ConditionalActionModel", *, iterations: int = 8, warmup_iterations: int = 2, seed: int = 42, diffusion_steps: int = 50, require_cuda: bool = False) -> dict[str, object]:
    if model is None or diffusion_steps != 50:
        raise ValueError("runtime benchmark requires a model and exactly 50 denoising steps")
    if not getattr(model, "_mainline_checkpoint_loaded", False):
        raise ValueError("runtime benchmark requires a trained loaded checkpoint")
    if iterations <= 0 or warmup_iterations < 0:
        raise ValueError("benchmark iterations are invalid")
    if torch is None:
        if require_cuda:
            raise RuntimeError("CUDA is required for this publication benchmark")
        observation = np.zeros((1, 84), dtype=np.float32)
        timings = []
        for index in range(warmup_iterations):
            model.sample(observation, seed=seed + index, return_trace=True)
        for _ in range(iterations):
            start = time.perf_counter()
            _, trace = model.sample(observation, seed=seed, return_trace=True)
            timings.append(time.perf_counter() - start)
        runtime = "numpy_cpu_fallback"
    else:
        device = next(model.parameters()).device
        if require_cuda and (device.type != "cuda" or not torch.cuda.is_available()):
            raise RuntimeError("CUDA is required for this publication benchmark")
        observation = torch.zeros((1, 84), dtype=torch.float32, device=device)
        timings = []
        model.eval()
        with torch.no_grad():
            for index in range(warmup_iterations):
                model.sample(observation, seed=seed + index, return_trace=True)
            if device.type == "cuda":
                torch.cuda.synchronize(device)
            for _ in range(iterations):
                if device.type == "cuda":
                    torch.cuda.synchronize(device)
                start = time.perf_counter()
                _, trace = model.sample(observation, seed=seed, return_trace=True)
                if device.type == "cuda":
                    torch.cuda.synchronize(device)
                timings.append(time.perf_counter() - start)
        runtime = str(device)
    if tuple(trace) != tuple(range(49, -1, -1)):
        raise RuntimeError("benchmark sampler did not execute all distinct reverse timesteps")
    timings.sort()
    p99 = timings[min(len(timings) - 1, int(0.99 * len(timings)))]
    accepted = {rate: p99 <= 0.8 / rate for rate in (50, 100)}
    return {"iterations": iterations, "warmup_iterations": warmup_iterations, "warmup_excluded": True, "diffusion_steps": len(trace), "distinct_timesteps": len(set(trace)), "p99_latency_s": p99, "accepted_rates_hz": [rate for rate, ok in accepted.items() if ok], "selected_rate_hz": 100 if accepted[100] else 50 if accepted[50] else None, "sleep_calls": 0, "runtime": runtime, "cuda_required": require_cuda}


def export_torchscript(checkpoint_path: str | Path, output_path: str | Path) -> str:
    if torch is None:
        with np.load(checkpoint_path, allow_pickle=False) as payload:
            with Path(output_path).open("wb") as handle:
                np.savez(handle, **{key: payload[key] for key in payload.files})
        return str(output_path)
    payload = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    config = MainlineModelConfig(**payload["config"])
    model = ConditionalActionModel(config)
    model.load_state_dict(payload["model_state_dict"])
    model.eval()
    scripted = torch.jit.trace(
        model,
        (
            torch.zeros((1, 12), dtype=torch.float32),
            torch.zeros((1, 84), dtype=torch.float32),
            torch.zeros((1,), dtype=torch.long),
        ),
    )
    scripted.save(str(output_path))
    return str(output_path)
