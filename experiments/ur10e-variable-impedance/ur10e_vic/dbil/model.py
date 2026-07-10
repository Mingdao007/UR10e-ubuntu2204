"""Optional PyTorch diffusion Transformer; importing the package needs no torch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .config import DBILConfig
from .dataset import DatasetArrays, DatasetStats, sha256_file

try:  # The base UR10e environment deliberately does not require PyTorch.
    import torch
    from torch import nn
except ModuleNotFoundError:  # pragma: no cover - branch depends on local environment
    torch = None
    nn = None


def require_torch() -> Any:
    if torch is None:
        raise RuntimeError(
            "PyTorch is not installed in the base UR10e environment; create a separate "
            "DBIL training environment before train/infer"
        )
    return torch


if nn is not None:

    class ConditionalDiffusionTransformer(nn.Module):
        def __init__(self, config: DBILConfig) -> None:
            super().__init__()
            self.config = config
            self.context_projection = nn.Linear(config.context_dim, config.hidden_dim)
            self.noisy_target_projection = nn.Linear(config.target_dim, config.hidden_dim)
            self.positional_encoding = nn.Parameter(
                torch.zeros(1, config.history_window, config.hidden_dim)
            )
            self.timestep_embedding = nn.Embedding(config.denoising_steps, config.hidden_dim)
            self.cross_attention = nn.MultiheadAttention(
                config.hidden_dim, config.attention_heads, batch_first=True
            )
            layer = nn.TransformerEncoderLayer(
                d_model=config.hidden_dim,
                nhead=config.attention_heads,
                dim_feedforward=config.hidden_dim * 4,
                batch_first=True,
                activation="gelu",
            )
            self.transformer = nn.TransformerEncoder(layer, config.transformer_layers)
            self.output = nn.Linear(config.hidden_dim, config.target_dim)

        def forward(self, context: Any, noisy_target: Any, timestep: Any) -> Any:
            context_tokens = self.context_projection(context)
            target_tokens = (
                self.noisy_target_projection(noisy_target) + self.positional_encoding
            )
            time_tokens = self.timestep_embedding(timestep).unsqueeze(1)
            target_tokens = target_tokens + time_tokens
            attended, _ = self.cross_attention(
                query=target_tokens, key=context_tokens, value=context_tokens
            )
            encoded = self.transformer(target_tokens + attended)
            return self.output(encoded)


else:

    class ConditionalDiffusionTransformer:  # type: ignore[no-redef]
        def __init__(self, config: DBILConfig) -> None:
            del config
            require_torch()


def _schedule(config: DBILConfig, device: Any) -> tuple[Any, Any, Any]:
    framework = require_torch()
    beta = framework.linspace(
        config.beta_start, config.beta_end, config.denoising_steps, device=device
    )
    alpha = 1.0 - beta
    alpha_bar = framework.cumprod(alpha, dim=0)
    return beta, alpha, alpha_bar


def training_loss(model: Any, context: Any, target: Any, config: DBILConfig) -> Any:
    framework = require_torch()
    batch = target.shape[0]
    timestep = framework.randint(
        0, config.denoising_steps, (batch,), device=target.device
    )
    _, _, alpha_bar = _schedule(config, target.device)
    noise = framework.randn_like(target)
    weight = alpha_bar[timestep].view(batch, 1, 1)
    noisy = framework.sqrt(weight) * target + framework.sqrt(1.0 - weight) * noise
    predicted_noise = model(context, noisy, timestep)
    return framework.nn.functional.mse_loss(predicted_noise, noise)


def sample_s_zft(
    model: Any,
    context: Any,
    config: DBILConfig,
    *,
    seed: int | None = None,
) -> Any:
    framework = require_torch()
    generator = framework.Generator(device=context.device)
    generator.manual_seed(config.seed if seed is None else seed)
    beta, alpha, alpha_bar = _schedule(config, context.device)
    sample = framework.randn(
        (context.shape[0], config.history_window, config.target_dim),
        generator=generator,
        device=context.device,
    )
    model.eval()
    with framework.no_grad():
        for step in reversed(range(config.denoising_steps)):
            timestep = framework.full(
                (context.shape[0],), step, dtype=framework.long, device=context.device
            )
            predicted_noise = model(context, sample, timestep)
            mean = (
                sample
                - beta[step] / framework.sqrt(1.0 - alpha_bar[step]) * predicted_noise
            ) / framework.sqrt(alpha[step])
            if step > 0:
                noise = framework.randn(
                    sample.shape, generator=generator, device=context.device
                )
                sample = mean + framework.sqrt(beta[step]) * noise
            else:
                sample = mean
    return sample


def train_checkpoint(
    arrays: DatasetArrays,
    stats: DatasetStats,
    dataset_path: Path,
    stats_path: Path,
    checkpoint_path: Path,
    *,
    epochs: int,
    batch_size: int,
    learning_rate: float,
    config: DBILConfig | None = None,
    device: str = "auto",
    max_train_samples: int | None = None,
) -> dict[str, Any]:
    framework = require_torch()
    config = config or DBILConfig()
    if epochs <= 0 or batch_size <= 0 or learning_rate <= 0.0:
        raise ValueError("training parameters must be positive")
    framework.manual_seed(config.seed)
    np.random.seed(config.seed)
    training_arrays = arrays.select_split(0)
    context = (training_arrays.context - np.asarray(stats.context_mean)) / np.asarray(
        stats.context_std
    )
    target = (training_arrays.target_s_zft - np.asarray(stats.target_mean)) / np.asarray(
        stats.target_std
    )
    if max_train_samples is not None:
        if max_train_samples <= 0:
            raise ValueError("max_train_samples must be positive")
        context = context[:max_train_samples]
        target = target[:max_train_samples]
    if device == "auto":
        if framework.backends.mps.is_available():
            device = "mps"
        elif framework.cuda.is_available():
            device = "cuda"
        else:
            device = "cpu"
    compute_device = framework.device(device)
    dataset = framework.utils.data.TensorDataset(
        framework.as_tensor(context, dtype=framework.float32),
        framework.as_tensor(target, dtype=framework.float32),
    )
    generator = framework.Generator().manual_seed(config.seed)
    loader = framework.utils.data.DataLoader(
        dataset, batch_size=batch_size, shuffle=True, generator=generator
    )
    model = ConditionalDiffusionTransformer(config).to(compute_device)
    optimizer = framework.optim.AdamW(model.parameters(), lr=learning_rate)
    history: list[float] = []
    model.train()
    for _ in range(epochs):
        running = 0.0
        count = 0
        for context_batch, target_batch in loader:
            context_batch = context_batch.to(compute_device)
            target_batch = target_batch.to(compute_device)
            optimizer.zero_grad(set_to_none=True)
            loss = training_loss(model, context_batch, target_batch, config)
            loss.backward()
            optimizer.step()
            running += float(loss.detach()) * context_batch.shape[0]
            count += context_batch.shape[0]
        history.append(running / count)
    checkpoint = {
        "schema_version": 1,
        "config": config.to_dict(),
        "model_state": {
            name: value.detach().cpu() for name, value in model.state_dict().items()
        },
        "dataset_sha256": sha256_file(dataset_path),
        "stats_sha256": sha256_file(stats_path),
        "training_loss": history,
        "training_samples": int(context.shape[0]),
        "training_mode": "smoke" if max_train_samples is not None else "full",
        "device": str(compute_device),
        "active_enabled": False,
        "claim_boundary": "public-data training scaffold; shadow inference only",
    }
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = checkpoint_path.with_suffix(checkpoint_path.suffix + ".tmp")
    framework.save(checkpoint, temporary)
    temporary.replace(checkpoint_path)
    return {
        "checkpoint_path": str(checkpoint_path.resolve()),
        "checkpoint_sha256": sha256_file(checkpoint_path),
        "dataset_sha256": checkpoint["dataset_sha256"],
        "stats_sha256": checkpoint["stats_sha256"],
        "training_loss": history,
        "training_samples": int(context.shape[0]),
        "training_mode": checkpoint["training_mode"],
        "device": str(compute_device),
        "active_enabled": False,
    }
