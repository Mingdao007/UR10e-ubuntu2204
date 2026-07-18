"""Clean-room optional-PyTorch DDPM for the UR10e force-domain adaptation.

This module implements the published dimensions and diffusion configuration
from first principles.  It does not copy the upstream TacDiffusion repository,
its Franka controller, or its non-commercial implementation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
import random
from typing import Any, Mapping, Sequence

import numpy as np

from .contracts import CONDITION_DIMENSION, PERMITTED_PROGRAM_CLAIM
from .dataset import (
    ACTION_DIMENSION,
    load_expert_dataset_npz,
    sha256_file,
    validate_dataset_manifest,
)


try:  # Optional by design: dataset validation must work without PyTorch.
    import torch
    from torch import nn
except ImportError:  # pragma: no cover - exercised through the public guard.
    torch = None
    nn = None


CHECKPOINT_SCHEMA_VERSION = 1
MODEL_VARIANT = "clean_room_conditional_ddpm_mlp"


def torch_available() -> bool:
    return torch is not None and nn is not None


def require_torch() -> Any:
    if not torch_available():
        raise RuntimeError(
            "TacDiffusion model commands require optional PyTorch; "
            "dataset and manifest validation remain available without it"
        )
    return torch


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class TacDiffusionDDPMConfig:
    observation_steps: int = 2
    per_observation_dim: int = 18
    condition_dim: int = CONDITION_DIMENSION
    action_dim: int = ACTION_DIMENSION
    hidden_dim: int = 512
    time_embedding_dim: int = 32
    diffusion_steps: int = 50
    beta_start: float = 1e-4
    beta_end: float = 0.02
    seed: int = 42
    active_enabled: bool = False

    def __post_init__(self) -> None:
        pinned = (
            self.observation_steps,
            self.per_observation_dim,
            self.condition_dim,
            self.action_dim,
            self.hidden_dim,
            self.time_embedding_dim,
            self.diffusion_steps,
            self.seed,
        )
        if pinned != (2, 18, 36, 6, 512, 32, 50, 42):
            raise ValueError("TacDiffusion DDPM dimensions/T/seed are pinned")
        if not math.isclose(self.beta_start, 1e-4, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("DDPM beta_start is pinned to 1e-4")
        if not math.isclose(self.beta_end, 0.02, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("DDPM beta_end is pinned to 0.02")
        if self.active_enabled:
            raise ValueError("TacDiffusion active mode remains disabled offline")


if torch_available():

    class ConditionalNoiseMLP(nn.Module):
        """A clean-room 512-wide conditional noise estimator."""

        def __init__(self, config: TacDiffusionDDPMConfig | None = None) -> None:
            super().__init__()
            self.config = config or TacDiffusionDDPMConfig()
            half = self.config.time_embedding_dim // 2
            frequencies = torch.exp(
                -math.log(10000.0)
                * torch.arange(half, dtype=torch.float32)
                / float(max(1, half - 1))
            )
            self.register_buffer("time_frequencies", frequencies)
            input_dim = (
                self.config.condition_dim
                + self.config.action_dim
                + self.config.time_embedding_dim
            )
            hidden = self.config.hidden_dim
            self.network = nn.Sequential(
                nn.Linear(input_dim, hidden),
                nn.SiLU(),
                nn.Linear(hidden, hidden),
                nn.SiLU(),
                nn.Linear(hidden, hidden),
                nn.SiLU(),
                nn.Linear(hidden, self.config.action_dim),
            )

        def _time_embedding(self, step: Any) -> Any:
            normalized = step.float() / float(self.config.diffusion_steps - 1)
            angles = normalized[:, None] * self.time_frequencies[None, :]
            return torch.cat((torch.sin(angles), torch.cos(angles)), dim=-1)

        def forward(self, condition: Any, noisy_action: Any, step: Any) -> Any:
            if condition.ndim != 2 or condition.shape[1] != self.config.condition_dim:
                raise ValueError("condition tensor must have shape [B, 36]")
            if noisy_action.shape != (condition.shape[0], self.config.action_dim):
                raise ValueError("noisy_action tensor must have shape [B, 6]")
            if step.shape != (condition.shape[0],):
                raise ValueError("diffusion step tensor must have shape [B]")
            features = torch.cat(
                (condition, noisy_action, self._time_embedding(step)), dim=-1
            )
            return self.network(features)


    class ConditionalDDPM(nn.Module):
        def __init__(self, config: TacDiffusionDDPMConfig | None = None) -> None:
            super().__init__()
            self.config = config or TacDiffusionDDPMConfig()
            self.noise_estimator = ConditionalNoiseMLP(self.config)
            betas = torch.linspace(
                self.config.beta_start,
                self.config.beta_end,
                self.config.diffusion_steps,
                dtype=torch.float32,
            )
            alphas = 1.0 - betas
            alpha_bar = torch.cumprod(alphas, dim=0)
            self.register_buffer("betas", betas)
            self.register_buffer("alphas", alphas)
            self.register_buffer("alpha_bar", alpha_bar)

        def training_loss(self, condition: Any, clean_action: Any) -> Any:
            batch = condition.shape[0]
            step = torch.randint(
                0,
                self.config.diffusion_steps,
                (batch,),
                device=condition.device,
            )
            noise = torch.randn_like(clean_action)
            alpha_bar = self.alpha_bar[step, None]
            noisy = torch.sqrt(alpha_bar) * clean_action + torch.sqrt(
                1.0 - alpha_bar
            ) * noise
            predicted = self.noise_estimator(condition, noisy, step)
            return torch.mean((predicted - noise) ** 2)

        @torch.no_grad()
        def sample(self, condition: Any, *, seed: int = 42) -> Any:
            if condition.ndim == 1:
                condition = condition[None, :]
            if condition.ndim != 2 or condition.shape[1] != self.config.condition_dim:
                raise ValueError("condition tensor must have shape [B, 36]")
            generator = torch.Generator(device=condition.device)
            generator.manual_seed(int(seed))
            action = torch.randn(
                (condition.shape[0], self.config.action_dim),
                generator=generator,
                device=condition.device,
                dtype=condition.dtype,
            )
            for index in range(self.config.diffusion_steps - 1, -1, -1):
                step = torch.full(
                    (condition.shape[0],),
                    index,
                    device=condition.device,
                    dtype=torch.long,
                )
                predicted_noise = self.noise_estimator(condition, action, step)
                beta = self.betas[index]
                alpha = self.alphas[index]
                alpha_bar = self.alpha_bar[index]
                mean = (action - beta * predicted_noise / torch.sqrt(1.0 - alpha_bar)) / torch.sqrt(alpha)
                if index > 0:
                    noise = torch.randn(
                        action.shape,
                        generator=generator,
                        device=condition.device,
                        dtype=condition.dtype,
                    )
                    action = mean + torch.sqrt(beta) * noise
                else:
                    action = mean
            return action

else:

    class ConditionalNoiseMLP:  # type: ignore[no-redef]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            require_torch()


    class ConditionalDDPM:  # type: ignore[no-redef]
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            require_torch()


@dataclass(frozen=True)
class InferenceResult:
    raw_f_df: tuple[float, ...]
    checkpoint_sha256: str
    dataset_sha256: str
    seed: int


class TacDiffusionPredictor:
    def __init__(
        self,
        *,
        model: Any,
        condition_mean: Sequence[float],
        condition_std: Sequence[float],
        action_mean: Sequence[float],
        action_std: Sequence[float],
        checkpoint_sha256: str,
        dataset_sha256: str,
        device: str,
        config: TacDiffusionDDPMConfig,
    ) -> None:
        module = require_torch()
        self.model = model
        self.condition_mean = module.tensor(
            condition_mean, dtype=module.float32, device=device
        )
        self.condition_std = module.tensor(
            condition_std, dtype=module.float32, device=device
        )
        self.action_mean = module.tensor(
            action_mean, dtype=module.float32, device=device
        )
        self.action_std = module.tensor(
            action_std, dtype=module.float32, device=device
        )
        self.checkpoint_sha256 = checkpoint_sha256
        self.dataset_sha256 = dataset_sha256
        self.device = device
        self.config = config

    def predict(self, condition: Sequence[float], *, seed: int | None = None) -> InferenceResult:
        module = require_torch()
        values = np.asarray(condition, dtype=np.float32)
        if values.shape != (CONDITION_DIMENSION,) or not np.isfinite(values).all():
            raise ValueError("condition must contain 36 finite values")
        tensor = module.tensor(values, dtype=module.float32, device=self.device)
        normalized = (tensor - self.condition_mean) / self.condition_std
        chosen_seed = self.config.seed if seed is None else int(seed)
        sampled = self.model.sample(normalized, seed=chosen_seed)[0]
        action = sampled * self.action_std + self.action_mean
        raw = tuple(float(value) for value in action.detach().cpu().numpy())
        if not all(math.isfinite(value) for value in raw):
            raise ValueError("TacDiffusion inference produced non-finite F_df")
        return InferenceResult(
            raw_f_df=raw,
            checkpoint_sha256=self.checkpoint_sha256,
            dataset_sha256=self.dataset_sha256,
            seed=chosen_seed,
        )


def _device_name(requested: str) -> str:
    module = require_torch()
    if requested == "auto":
        return "cuda" if module.cuda.is_available() else "cpu"
    if requested.startswith("cuda") and not module.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    return requested


def _normalization(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mean = np.asarray(values, dtype=np.float64).mean(axis=0)
    std = np.asarray(values, dtype=np.float64).std(axis=0)
    std = np.maximum(std, 1e-6)
    return mean.astype(np.float32), std.astype(np.float32)


def train_ddpm(
    *,
    dataset_path: str | Path,
    dataset_manifest_path: str | Path,
    expert_trace_manifest_paths: str | Path | Sequence[str | Path],
    checkpoint_path: str | Path,
    checkpoint_manifest_path: str | Path,
    epochs: int = 100,
    batch_size: int = 256,
    learning_rate: float = 1e-4,
    device: str = "auto",
) -> dict[str, object]:
    module = require_torch()
    if epochs <= 0 or batch_size <= 0:
        raise ValueError("epochs and batch_size must be positive")
    if not math.isfinite(learning_rate) or learning_rate <= 0.0:
        raise ValueError("learning_rate must be finite and positive")
    dataset_manifest = validate_dataset_manifest(
        dataset_path, dataset_manifest_path, expert_trace_manifest_paths
    )
    dataset = load_expert_dataset_npz(dataset_path)
    train_indices = dataset.indices_for_split("train")
    if train_indices.size == 0:
        raise ValueError("dataset has no training samples")
    config = TacDiffusionDDPMConfig()
    chosen_device = _device_name(device)
    random.seed(config.seed)
    np.random.seed(config.seed)
    module.manual_seed(config.seed)
    if module.cuda.is_available():
        module.cuda.manual_seed_all(config.seed)
    condition_mean, condition_std = _normalization(dataset.condition[train_indices])
    action_mean, action_std = _normalization(dataset.expert_f_ff[train_indices])
    condition = module.tensor(
        (dataset.condition[train_indices] - condition_mean) / condition_std,
        dtype=module.float32,
        device=chosen_device,
    )
    action = module.tensor(
        (dataset.expert_f_ff[train_indices] - action_mean) / action_std,
        dtype=module.float32,
        device=chosen_device,
    )
    model = ConditionalDDPM(config).to(chosen_device)
    optimizer = module.optim.Adam(model.parameters(), lr=learning_rate)
    permutation_generator = module.Generator(device="cpu")
    permutation_generator.manual_seed(config.seed)
    loss_curve: list[float] = []
    model.train()
    for _epoch in range(epochs):
        permutation = module.randperm(
            condition.shape[0], generator=permutation_generator, device="cpu"
        )
        epoch_loss = 0.0
        batches = 0
        for start in range(0, condition.shape[0], batch_size):
            indices = permutation[start : start + batch_size].to(chosen_device)
            optimizer.zero_grad(set_to_none=True)
            loss = model.training_loss(condition[indices], action[indices])
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.detach().cpu())
            batches += 1
        loss_curve.append(epoch_loss / max(1, batches))
    if not all(math.isfinite(value) for value in loss_curve):
        raise ValueError("training produced a non-finite loss")

    checkpoint = Path(checkpoint_path)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary_checkpoint = checkpoint.with_name(f".{checkpoint.name}.tmp")
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "model_variant": MODEL_VARIANT,
        "config": asdict(config),
        "model_state_dict": model.state_dict(),
        "condition_mean": condition_mean.tolist(),
        "condition_std": condition_std.tolist(),
        "action_mean": action_mean.tolist(),
        "action_std": action_std.tolist(),
        "dataset_sha256": dataset_manifest["dataset_sha256"],
        "dataset_manifest_payload_sha256": dataset_manifest[
            "manifest_payload_sha256"
        ],
        "loss_curve": loss_curve,
        "active_enabled": False,
    }
    module.save(payload, temporary_checkpoint)
    temporary_checkpoint.replace(checkpoint)
    checkpoint_sha256 = sha256_file(checkpoint)
    manifest: dict[str, object] = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "model_variant": MODEL_VARIANT,
        "checkpoint_artifact": checkpoint.name,
        "checkpoint_sha256": checkpoint_sha256,
        "dataset_artifact": Path(dataset_path).name,
        "dataset_sha256": dataset_manifest["dataset_sha256"],
        "dataset_manifest_payload_sha256": dataset_manifest[
            "manifest_payload_sha256"
        ],
        "config": asdict(config),
        "epochs": epochs,
        "batch_size": batch_size,
        "learning_rate": learning_rate,
        "loss_curve": loss_curve,
        "runtime": {
            "python_version": platform.python_version(),
            "torch_version": module.__version__,
            "device": chosen_device,
        },
        "training_status": "completed",
        "active_enabled": False,
        "permitted_program_claim": PERMITTED_PROGRAM_CLAIM,
    }
    manifest["manifest_payload_sha256"] = _canonical_sha256(manifest)
    manifest_path = Path(checkpoint_manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_manifest = manifest_path.with_name(f".{manifest_path.name}.tmp")
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary_manifest.replace(manifest_path)
    return manifest


def validate_checkpoint_manifest(
    checkpoint_path: str | Path,
    checkpoint_manifest_path: str | Path,
    *,
    expected_dataset_sha256: str | None = None,
) -> dict[str, object]:
    manifest = json.loads(Path(checkpoint_manifest_path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("checkpoint manifest must be a JSON object")
    payload_hash = manifest.pop("manifest_payload_sha256", None)
    if payload_hash != _canonical_sha256(manifest):
        raise ValueError("checkpoint manifest payload hash mismatch")
    manifest["manifest_payload_sha256"] = payload_hash
    if manifest.get("checkpoint_sha256") != sha256_file(checkpoint_path):
        raise ValueError("checkpoint artifact hash mismatch")
    if manifest.get("model_variant") != MODEL_VARIANT:
        raise ValueError("checkpoint model variant mismatch")
    config_payload = manifest.get("config")
    if not isinstance(config_payload, dict):
        raise ValueError("checkpoint manifest config is missing")
    TacDiffusionDDPMConfig(**config_payload)
    if manifest.get("active_enabled") is not False:
        raise ValueError("checkpoint manifest may not enable active control")
    if (
        expected_dataset_sha256 is not None
        and manifest.get("dataset_sha256") != expected_dataset_sha256
    ):
        raise ValueError("checkpoint dataset hash mismatch")
    return manifest


def load_predictor(
    checkpoint_path: str | Path,
    *,
    expected_checkpoint_sha256: str,
    expected_dataset_sha256: str,
    device: str = "auto",
) -> TacDiffusionPredictor:
    module = require_torch()
    checkpoint_sha256 = sha256_file(checkpoint_path)
    if checkpoint_sha256 != expected_checkpoint_sha256:
        raise ValueError("checkpoint hash does not match expected binding")
    try:
        payload = module.load(checkpoint_path, map_location="cpu", weights_only=True)
    except TypeError as error:
        raise RuntimeError(
            "installed PyTorch lacks safe weights_only checkpoint loading"
        ) from error
    if not isinstance(payload, dict):
        raise ValueError("checkpoint payload must be a mapping")
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("unsupported checkpoint schema")
    if payload.get("model_variant") != MODEL_VARIANT:
        raise ValueError("unsupported checkpoint model variant")
    if payload.get("active_enabled") is not False:
        raise ValueError("checkpoint attempts to enable active control")
    dataset_sha256 = str(payload.get("dataset_sha256", ""))
    if dataset_sha256 != expected_dataset_sha256:
        raise ValueError("checkpoint dataset hash does not match expected binding")
    config_payload = payload.get("config")
    if not isinstance(config_payload, dict):
        raise ValueError("checkpoint config is missing")
    config = TacDiffusionDDPMConfig(**config_payload)
    chosen_device = _device_name(device)
    model = ConditionalDDPM(config).to(chosen_device)
    model.load_state_dict(payload["model_state_dict"], strict=True)
    model.eval()
    return TacDiffusionPredictor(
        model=model,
        condition_mean=payload["condition_mean"],
        condition_std=payload["condition_std"],
        action_mean=payload["action_mean"],
        action_std=payload["action_std"],
        checkpoint_sha256=checkpoint_sha256,
        dataset_sha256=dataset_sha256,
        device=chosen_device,
        config=config,
    )
