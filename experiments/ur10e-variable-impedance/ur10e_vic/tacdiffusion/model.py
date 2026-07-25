"""Clean-room, TacDiffusion-only DDPM for UR10e expert force labels.

The architecture reproduces the pinned upstream behavioral contract without
copying its source: separate current/previous observation embeddings, a noisy
action embedding, a TimeSiren embedding, 512-wide BatchNorm/GELU residual
blocks, and a 50-step linear-beta DDPM.  Checkpoints remain shadow-only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
from typing import Any, Mapping, Sequence

import numpy as np

from .contracts import CONDITION_DIMENSION, PERMITTED_PROGRAM_CLAIM
from .dataset import (
    LEGACY_ACTION_DIMENSION,
    load_expert_dataset_npz,
    sha256_file,
    validate_dataset_manifest,
)

# This module is the retained v2 six-force replay implementation.  It is not
# the mainline trainer; importing its dimension explicitly prevents accidental
# 6D promotion after the dataset contract moved to 12D.
ACTION_DIMENSION = LEGACY_ACTION_DIMENSION


try:  # Optional: lineage/dataset validation works without PyTorch.
    import torch
    from torch import nn
except ImportError:  # pragma: no cover - exercised through the public guard.
    torch = None
    nn = None


CHECKPOINT_SCHEMA_VERSION = 2
LEGACY_CHECKPOINT_SCHEMA_VERSION = 1
MODEL_VARIANT = "clean_room_tacdiffusion_force_ddpm_v2"
PINNED_UPSTREAM_COMMIT = "6a5567c829c54b7d03164cf40779d2451de4099e"
EVALUATION_SCHEMA = "ur10e_tacdiffusion_evaluation/v1"


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
        payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _indices_sha256(indices: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(indices, dtype="<i8").tobytes()).hexdigest()


@dataclass(frozen=True)
class TacDiffusionDDPMConfig:
    observation_steps: int = 2
    per_observation_dim: int = 18
    condition_dim: int = CONDITION_DIMENSION
    action_dim: int = ACTION_DIMENSION
    embedding_dim: int = 128
    hidden_dim: int = 512
    residual_blocks: int = 3
    diffusion_steps: int = 50
    beta_start: float = 1e-4
    beta_end: float = 0.02
    dropout: float = 0.0
    guidance_weight: float = 0.0
    seed: int = 42
    active_enabled: bool = False

    def __post_init__(self) -> None:
        pinned = (
            self.observation_steps,
            self.per_observation_dim,
            self.condition_dim,
            self.action_dim,
            self.embedding_dim,
            self.hidden_dim,
            self.residual_blocks,
            self.diffusion_steps,
            self.seed,
        )
        if pinned != (2, 18, 36, 6, 128, 512, 3, 50, 42):
            raise ValueError("TacDiffusion architecture dimensions/T/seed are pinned")
        if not math.isclose(self.beta_start, 1e-4, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("DDPM beta_start is pinned to 1e-4")
        if not math.isclose(self.beta_end, 0.02, rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("DDPM beta_end is pinned to 0.02")
        if self.dropout != 0.0 or self.guidance_weight != 0.0:
            raise ValueError("TacDiffusion dropout and guidance weight are pinned to zero")
        if self.active_enabled:
            raise ValueError("TacDiffusion active mode remains disabled offline")


if torch_available():

    class FeatureBlock(nn.Module):
        def __init__(self, input_dim: int, output_dim: int) -> None:
            super().__init__()
            self.layers = nn.Sequential(
                nn.Linear(input_dim, output_dim),
                nn.BatchNorm1d(output_dim),
                nn.GELU(),
            )

        def forward(self, values: Any) -> Any:
            return self.layers(values)


    class TimeSiren(nn.Module):
        def __init__(self, embedding_dim: int) -> None:
            super().__init__()
            self.input = nn.Linear(1, embedding_dim)
            self.output = nn.Linear(embedding_dim, embedding_dim)

        def forward(self, normalized_step: Any) -> Any:
            return self.output(torch.sin(self.input(normalized_step[:, None])))


    class ResidualConditionedBlock(nn.Module):
        def __init__(self, hidden_dim: int, embedding_dim: int) -> None:
            super().__init__()
            self.layers = nn.Sequential(
                nn.Linear(hidden_dim + 2 * embedding_dim, hidden_dim),
                nn.BatchNorm1d(hidden_dim),
                nn.GELU(),
                nn.Linear(hidden_dim, hidden_dim),
                nn.BatchNorm1d(hidden_dim),
                nn.GELU(),
            )

        def forward(self, hidden: Any, action_embedding: Any, time_embedding: Any) -> Any:
            update = self.layers(torch.cat((hidden, action_embedding, time_embedding), dim=-1))
            return hidden + update


    class ConditionalNoiseMLP(nn.Module):
        """Pinned clean-room realization of the TacDiffusion denoiser contract."""

        def __init__(self, config: TacDiffusionDDPMConfig | None = None) -> None:
            super().__init__()
            self.config = config or TacDiffusionDDPMConfig()
            embed = self.config.embedding_dim
            self.current_observation_embedding = FeatureBlock(
                self.config.per_observation_dim, embed
            )
            self.previous_observation_embedding = FeatureBlock(
                self.config.per_observation_dim, embed
            )
            self.noisy_action_embedding = FeatureBlock(self.config.action_dim, embed)
            self.time_embedding = TimeSiren(embed)
            self.input_projection = FeatureBlock(4 * embed, self.config.hidden_dim)
            self.residual_layers = nn.ModuleList(
                ResidualConditionedBlock(self.config.hidden_dim, embed)
                for _ in range(self.config.residual_blocks)
            )
            self.output = nn.Linear(self.config.hidden_dim, self.config.action_dim)

        def forward(self, condition: Any, noisy_action: Any, step: Any) -> Any:
            if condition.ndim != 2 or condition.shape[1] != self.config.condition_dim:
                raise ValueError("condition tensor must have shape [B, 36]")
            if noisy_action.shape != (condition.shape[0], self.config.action_dim):
                raise ValueError("noisy_action tensor must have shape [B, 6]")
            if step.shape != (condition.shape[0],):
                raise ValueError("diffusion step tensor must have shape [B]")
            current = self.current_observation_embedding(
                condition[:, : self.config.per_observation_dim]
            )
            previous = self.previous_observation_embedding(
                condition[:, self.config.per_observation_dim :]
            )
            action = self.noisy_action_embedding(noisy_action)
            normalized_step = step.float() / float(self.config.diffusion_steps - 1)
            time = self.time_embedding(normalized_step)
            hidden = self.input_projection(torch.cat((current, previous, action, time), dim=-1))
            for layer in self.residual_layers:
                hidden = layer(hidden, action, time)
            return self.output(hidden)


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
            self.register_buffer("betas", betas)
            self.register_buffer("alphas", alphas)
            self.register_buffer("alpha_bar", torch.cumprod(alphas, dim=0))

        def training_loss(self, condition: Any, clean_action: Any) -> Any:
            batch = condition.shape[0]
            step = torch.randint(
                0, self.config.diffusion_steps, (batch,), device=condition.device
            )
            noise = torch.randn_like(clean_action)
            alpha_bar = self.alpha_bar[step, None]
            noisy = torch.sqrt(alpha_bar) * clean_action + torch.sqrt(1.0 - alpha_bar) * noise
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
                    (condition.shape[0],), index, device=condition.device, dtype=torch.long
                )
                predicted_noise = self.noise_estimator(condition, action, step)
                beta = self.betas[index]
                alpha = self.alphas[index]
                alpha_bar = self.alpha_bar[index]
                mean = (
                    action - beta * predicted_noise / torch.sqrt(1.0 - alpha_bar)
                ) / torch.sqrt(alpha)
                if index:
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
        self.condition_mean = module.tensor(condition_mean, dtype=module.float32, device=device)
        self.condition_std = module.tensor(condition_std, dtype=module.float32, device=device)
        self.action_mean = module.tensor(action_mean, dtype=module.float32, device=device)
        self.action_std = module.tensor(action_std, dtype=module.float32, device=device)
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
            raise ValueError("TacDiffusion inference produced non-finite F_ff")
        return InferenceResult(raw, self.checkpoint_sha256, self.dataset_sha256, chosen_seed)


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
    return mean.astype(np.float32), np.maximum(std, 1e-6).astype(np.float32)


def _safe_torch_load(path: str | Path) -> Mapping[str, Any]:
    module = require_torch()
    try:
        payload = module.load(path, map_location="cpu", weights_only=True)
    except TypeError as error:
        raise RuntimeError("installed PyTorch lacks safe weights_only checkpoint loading") from error
    if not isinstance(payload, Mapping):
        raise ValueError("checkpoint payload must be a mapping")
    return payload


def _faithfulness_contract() -> dict[str, object]:
    return {
        "upstream_commit": PINNED_UPSTREAM_COMMIT,
        "source_copying": False,
        "observation_embeddings": "separate_current_previous_18d",
        "noisy_action_embedding": True,
        "time_embedding": "TimeSiren_128d",
        "hidden_width": 512,
        "normalization_layers": "BatchNorm1d",
        "activation": "GELU",
        "diffusion_steps": 50,
        "beta_schedule": "linear_1e-4_to_0.02",
        "optimizer": "Adam",
        "scheduler": "CosineAnnealingLR",
        "default_epochs": 1500,
        "default_batch_size": 4096,
        "default_learning_rate": 1e-3,
        "ur10e_delta": "36D force-only condition to 6D pre-filter TCP F_ff",
    }


def train_ddpm(
    *,
    dataset_path: str | Path,
    dataset_manifest_path: str | Path,
    expert_trace_manifest_paths: str | Path | Sequence[str | Path],
    checkpoint_path: str | Path,
    checkpoint_manifest_path: str | Path,
    epochs: int = 1500,
    batch_size: int = 4096,
    learning_rate: float = 1e-3,
    device: str = "auto",
    resume_checkpoint_path: str | Path | None = None,
    evidence_scope: str = "offline_training",
) -> dict[str, object]:
    module = require_torch()
    if epochs <= 0 or batch_size <= 1:
        raise ValueError("epochs must be positive and batch_size must exceed one for BatchNorm")
    if not math.isfinite(learning_rate) or learning_rate <= 0.0:
        raise ValueError("learning_rate must be finite and positive")
    if evidence_scope not in {"offline_training", "fixture_only"}:
        raise ValueError("evidence_scope must be offline_training or fixture_only")
    dataset_manifest = validate_dataset_manifest(
        dataset_path, dataset_manifest_path, expert_trace_manifest_paths
    )
    dataset = load_expert_dataset_npz(dataset_path)
    train_indices = dataset.indices_for_split("train")
    if train_indices.size < 2:
        raise ValueError("dataset requires at least two training samples for BatchNorm")
    config = TacDiffusionDDPMConfig()
    chosen_device = _device_name(device)
    module.manual_seed(config.seed)
    if module.cuda.is_available():
        module.cuda.manual_seed_all(config.seed)
    condition_mean, condition_std = _normalization(dataset.condition[train_indices])
    action_mean, action_std = _normalization(dataset.expert_f_ff[train_indices])
    normalization = {
        "scope": "train_split_only",
        "train_indices_sha256": _indices_sha256(train_indices),
        "condition_mean": condition_mean.tolist(),
        "condition_std": condition_std.tolist(),
        "action_mean": action_mean.tolist(),
        "action_std": action_std.tolist(),
    }
    normalization["payload_sha256"] = _canonical_sha256(normalization)
    training_contract = {
        "batch_size": batch_size,
        "initial_learning_rate": learning_rate,
        "optimizer": "Adam",
        "scheduler": "CosineAnnealingLR",
        "scheduler_t_max": 1500,
    }
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
    scheduler = module.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=1500)
    permutation_generator = module.Generator(device="cpu")
    permutation_generator.manual_seed(config.seed)
    loss_curve: list[float] = []
    start_epoch = 0
    resumed_from_sha256: str | None = None
    if resume_checkpoint_path is not None:
        prior = _safe_torch_load(resume_checkpoint_path)
        if prior.get("schema_version") != CHECKPOINT_SCHEMA_VERSION or prior.get("model_variant") != MODEL_VARIANT:
            raise ValueError("resume checkpoint is legacy or incompatible")
        if prior.get("dataset_sha256") != dataset_manifest["dataset_sha256"]:
            raise ValueError("resume checkpoint dataset binding mismatch")
        if prior.get("config") != asdict(config):
            raise ValueError("resume checkpoint configuration mismatch")
        if prior.get("normalization") != normalization:
            raise ValueError("resume checkpoint normalization mismatch")
        if prior.get("training_contract") != training_contract:
            raise ValueError("resume checkpoint training contract mismatch")
        model.load_state_dict(prior["model_state_dict"], strict=True)
        optimizer.load_state_dict(prior["optimizer_state_dict"])
        scheduler.load_state_dict(prior["scheduler_state_dict"])
        module.set_rng_state(prior["torch_rng_state"])
        if chosen_device.startswith("cuda"):
            module.cuda.set_rng_state_all(prior["cuda_rng_state_all"])
        permutation_generator.set_state(prior["permutation_generator_state"])
        loss_curve = [float(value) for value in prior["loss_curve"]]
        start_epoch = int(prior["epoch_completed"])
        resumed_from_sha256 = sha256_file(resume_checkpoint_path)

    model.train()
    for _epoch in range(epochs):
        permutation = module.randperm(
            condition.shape[0], generator=permutation_generator, device="cpu"
        )
        epoch_loss = 0.0
        batches = 0
        for start in range(0, condition.shape[0], batch_size):
            indices = permutation[start : start + batch_size].to(chosen_device)
            if indices.numel() < 2:
                continue
            optimizer.zero_grad(set_to_none=True)
            loss = model.training_loss(condition[indices], action[indices])
            loss.backward()
            optimizer.step()
            epoch_loss += float(loss.detach().cpu())
            batches += 1
        if batches == 0:
            raise ValueError("training produced no BatchNorm-safe batches")
        loss_curve.append(epoch_loss / batches)
        scheduler.step()
    if not all(math.isfinite(value) for value in loss_curve):
        raise ValueError("training produced a non-finite loss")

    checkpoint = Path(checkpoint_path)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    temporary_checkpoint = checkpoint.with_name(f".{checkpoint.name}.tmp")
    payload = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "model_variant": MODEL_VARIANT,
        "config": asdict(config),
        "faithfulness_contract": _faithfulness_contract(),
        "model_state_dict": model.state_dict(),
        "optimizer_state_dict": optimizer.state_dict(),
        "scheduler_state_dict": scheduler.state_dict(),
        "torch_rng_state": module.get_rng_state(),
        "permutation_generator_state": permutation_generator.get_state(),
        "cuda_rng_state_all": module.cuda.get_rng_state_all() if chosen_device.startswith("cuda") else [],
        "normalization": normalization,
        "training_contract": training_contract,
        "dataset_sha256": dataset_manifest["dataset_sha256"],
        "dataset_manifest_payload_sha256": dataset_manifest["manifest_payload_sha256"],
        "loss_curve": loss_curve,
        "epoch_completed": start_epoch + epochs,
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
        "dataset_manifest_payload_sha256": dataset_manifest["manifest_payload_sha256"],
        "config": asdict(config),
        "faithfulness_contract": _faithfulness_contract(),
        "normalization": normalization,
        "epoch_completed": start_epoch + epochs,
        "epochs_this_run": epochs,
        "batch_size": batch_size,
        "learning_rate": learning_rate,
        "scheduler": {"name": "CosineAnnealingLR", "t_max": 1500},
        "resume": {
            "resumed": resumed_from_sha256 is not None,
            "source_checkpoint_sha256": resumed_from_sha256,
            "optimizer_state_restored": resumed_from_sha256 is not None,
            "scheduler_state_restored": resumed_from_sha256 is not None,
            "rng_state_restored": resumed_from_sha256 is not None,
        },
        "loss_curve": loss_curve,
        "runtime": {
            "python_version": platform.python_version(),
            "torch_version": module.__version__,
            "device": chosen_device,
        },
        "training_status": "completed",
        "evidence_scope": evidence_scope,
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
    if manifest.get("schema_version") == LEGACY_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("legacy checkpoint schema v1 is nonfaithful and unsupported")
    payload_hash = manifest.pop("manifest_payload_sha256", None)
    if payload_hash != _canonical_sha256(manifest):
        raise ValueError("checkpoint manifest payload hash mismatch")
    manifest["manifest_payload_sha256"] = payload_hash
    if manifest.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("unsupported checkpoint schema")
    if manifest.get("checkpoint_sha256") != sha256_file(checkpoint_path):
        raise ValueError("checkpoint artifact hash mismatch")
    if manifest.get("model_variant") != MODEL_VARIANT:
        raise ValueError("checkpoint model variant mismatch")
    config_payload = manifest.get("config")
    if not isinstance(config_payload, dict):
        raise ValueError("checkpoint manifest config is missing")
    TacDiffusionDDPMConfig(**config_payload)
    if manifest.get("faithfulness_contract") != _faithfulness_contract():
        raise ValueError("checkpoint faithfulness contract mismatch")
    normalization = manifest.get("normalization", {})
    if not isinstance(normalization, dict) or normalization.get("scope") != "train_split_only":
        raise ValueError("checkpoint normalization is not train-split-only")
    normalization_hash = normalization.get("payload_sha256")
    normalization_without_hash = dict(normalization)
    normalization_without_hash.pop("payload_sha256", None)
    if normalization_hash != _canonical_sha256(normalization_without_hash):
        raise ValueError("checkpoint normalization hash mismatch")
    if manifest.get("active_enabled") is not False:
        raise ValueError("checkpoint manifest may not enable active control")
    if expected_dataset_sha256 is not None and manifest.get("dataset_sha256") != expected_dataset_sha256:
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
    payload = _safe_torch_load(checkpoint_path)
    if payload.get("schema_version") == LEGACY_CHECKPOINT_SCHEMA_VERSION:
        raise ValueError("legacy checkpoint schema v1 is nonfaithful and unsupported")
    if payload.get("schema_version") != CHECKPOINT_SCHEMA_VERSION or payload.get("model_variant") != MODEL_VARIANT:
        raise ValueError("unsupported checkpoint schema or model variant")
    if payload.get("active_enabled") is not False:
        raise ValueError("checkpoint attempts to enable active control")
    dataset_sha256 = str(payload.get("dataset_sha256", ""))
    if dataset_sha256 != expected_dataset_sha256:
        raise ValueError("checkpoint dataset hash does not match expected binding")
    if payload.get("faithfulness_contract") != _faithfulness_contract():
        raise ValueError("checkpoint faithfulness contract mismatch")
    config_payload = payload.get("config")
    if not isinstance(config_payload, dict):
        raise ValueError("checkpoint config is missing")
    config = TacDiffusionDDPMConfig(**config_payload)
    normalization = payload.get("normalization")
    if not isinstance(normalization, Mapping) or normalization.get("scope") != "train_split_only":
        raise ValueError("checkpoint normalization is invalid")
    chosen_device = _device_name(device)
    model = ConditionalDDPM(config).to(chosen_device)
    model.load_state_dict(payload["model_state_dict"], strict=True)
    model.eval()
    return TacDiffusionPredictor(
        model=model,
        condition_mean=normalization["condition_mean"],
        condition_std=normalization["condition_std"],
        action_mean=normalization["action_mean"],
        action_std=normalization["action_std"],
        checkpoint_sha256=checkpoint_sha256,
        dataset_sha256=dataset_sha256,
        device=chosen_device,
        config=config,
    )


def evaluate_checkpoint(
    *,
    checkpoint_path: str | Path,
    checkpoint_manifest_path: str | Path,
    dataset_path: str | Path,
    dataset_manifest_path: str | Path,
    expert_trace_manifest_paths: str | Path | Sequence[str | Path],
    split: str = "validation",
    max_samples: int | None = None,
    device: str = "auto",
) -> dict[str, object]:
    if split not in {"validation", "test"}:
        raise ValueError("evaluation split must be validation or test")
    dataset_manifest = validate_dataset_manifest(
        dataset_path, dataset_manifest_path, expert_trace_manifest_paths
    )
    checkpoint_manifest = validate_checkpoint_manifest(
        checkpoint_path,
        checkpoint_manifest_path,
        expected_dataset_sha256=str(dataset_manifest["dataset_sha256"]),
    )
    dataset = load_expert_dataset_npz(dataset_path)
    indices = dataset.indices_for_split(split)
    if max_samples is not None:
        if max_samples <= 0:
            raise ValueError("max_samples must be positive")
        indices = indices[:max_samples]
    if indices.size == 0:
        raise ValueError("evaluation split is empty")
    predictor = load_predictor(
        checkpoint_path,
        expected_checkpoint_sha256=str(checkpoint_manifest["checkpoint_sha256"]),
        expected_dataset_sha256=str(dataset_manifest["dataset_sha256"]),
        device=device,
    )
    predictions = np.asarray(
        [
            predictor.predict(dataset.condition[index], seed=42 + int(index)).raw_f_df
            for index in indices
        ],
        dtype=np.float64,
    )
    targets = np.asarray(dataset.expert_f_ff[indices], dtype=np.float64)
    error = predictions - targets
    metrics = {
        "rmse": float(np.sqrt(np.mean(error * error))),
        "mae": float(np.mean(np.abs(error))),
        "component_rmse": np.sqrt(np.mean(error * error, axis=0)).tolist(),
        "nonfinite_outputs": int((~np.isfinite(predictions)).sum()),
    }
    result: dict[str, object] = {
        "schema": EVALUATION_SCHEMA,
        "model_variant": MODEL_VARIANT,
        "checkpoint_sha256": checkpoint_manifest["checkpoint_sha256"],
        "dataset_sha256": dataset_manifest["dataset_sha256"],
        "split": split,
        "sample_count": int(indices.size),
        "sample_indices_sha256": _indices_sha256(indices),
        "metrics": metrics,
        "evidence_scope": checkpoint_manifest["evidence_scope"],
        "simulation_run": False,
        "hardware_run": False,
        "active_enabled": False,
    }
    result["payload_sha256"] = _canonical_sha256(result)
    return result
