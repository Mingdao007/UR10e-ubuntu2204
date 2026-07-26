"""Portable extraction of the pinned upstream DBIL mathematical core.

This module mirrors the pinned repository's trajectory/noise topology:
pose-query + wrench cross-attention, component position noising, quaternion
SLERP noising, relative-quaternion targets, the composite quaternion loss, and
iterative subtract/inverse-multiply reconstruction.  It deliberately does not
load the upstream path or shared-memory glue.

No checkpoint produced by ``portable_component_ddpm_v0`` is compatible with
this core.  The first checked-in implementation is integration-tested only and
does not claim a trained upstream-faithful reproduction.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import DBILConfig
from .model import require_torch

try:
    import torch
    from torch import nn
    from torch.nn import functional as F
except ModuleNotFoundError:  # pragma: no cover - environment dependent
    torch = None
    nn = None
    F = None


UPSTREAM_COMMIT = "8c05a4d4aca8012927a9fdf5bfcd8313247f6a30"
UPSTREAM_VARIANT = "upstream_slerp_cross_attention_v1"


def _normalise_quaternion(value: Any) -> Any:
    framework = require_torch()
    return value / framework.linalg.vector_norm(
        value, dim=-1, keepdim=True
    ).clamp(min=1e-8)


def quaternion_multiply(first: Any, second: Any) -> Any:
    framework = require_torch()
    w1, x1, y1, z1 = framework.unbind(first, dim=-1)
    w2, x2, y2, z2 = framework.unbind(second, dim=-1)
    result = framework.stack(
        (
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ),
        dim=-1,
    )
    return _normalise_quaternion(result)


def quaternion_inverse(value: Any) -> Any:
    framework = require_torch()
    norm_squared = framework.sum(value * value, dim=-1, keepdim=True).clamp(
        min=1e-12
    )
    return framework.cat((value[..., :1], -value[..., 1:]), dim=-1) / norm_squared


def quaternion_slerp(first: Any, second: Any, weight: Any) -> Any:
    framework = require_torch()
    first = _normalise_quaternion(first)
    second = _normalise_quaternion(second)
    dot = framework.sum(first * second, dim=-1, keepdim=True)
    second = framework.where(dot < 0.0, -second, second)
    dot = framework.abs(dot).clamp(-1.0, 1.0)
    theta = framework.acos(dot)
    sin_theta = framework.sin(theta)
    weight = framework.as_tensor(weight, dtype=first.dtype, device=first.device)
    while weight.ndim < dot.ndim:
        weight = weight.unsqueeze(-1)
    near_zero = framework.abs(sin_theta) < 1e-6
    first_weight = framework.where(
        near_zero,
        1.0 - weight,
        framework.sin((1.0 - weight) * theta) / sin_theta.clamp(min=1e-8),
    )
    second_weight = framework.where(
        near_zero,
        weight,
        framework.sin(weight * theta) / sin_theta.clamp(min=1e-8),
    )
    return _normalise_quaternion(first_weight * first + second_weight * second)


def quaternion_loss(predicted: Any, target: Any) -> Any:
    """Mirror the pinned upstream composite geodesic/theta loss."""

    framework = require_torch()
    predicted = _normalise_quaternion(predicted)
    target = _normalise_quaternion(target)
    dot_signed = framework.sum(predicted * target, dim=-1, keepdim=True)
    predicted = framework.where(dot_signed < 0.0, -predicted, predicted)
    dot = framework.abs(dot_signed).squeeze(-1).clamp(0.0, 1.0 - 1e-6)
    loss_q = framework.mean((1.0 - dot) ** 2)
    angle = 2.0 * framework.acos(dot)
    loss_q = loss_q + framework.mean(angle**2)
    unit_loss = framework.mean(
        (framework.linalg.vector_norm(predicted, dim=-1) - 1.0) ** 2
    )
    theta_loss = framework.mean(angle**2)
    return 5.0 * theta_loss + loss_q + unit_loss


if nn is not None:

    class UpstreamNoisePredictor(nn.Module):
        """Path-free equivalent of the pinned upstream Transformer topology."""

        def __init__(self, config: DBILConfig) -> None:
            super().__init__()
            if config.diffusion_variant != UPSTREAM_VARIANT:
                raise ValueError(f"UpstreamNoisePredictor requires {UPSTREAM_VARIANT}")
            self.config = config
            self.trajectory_embedding = nn.Linear(7, config.hidden_dim)
            self.positional_encoding = nn.Parameter(
                torch.zeros(1, config.history_window, config.hidden_dim)
            )
            self.time_embedding = nn.Embedding(
                config.denoising_steps, config.hidden_dim
            )
            self.condition_projection = nn.Linear(6, config.hidden_dim)
            self.cross_attention = nn.MultiheadAttention(
                config.hidden_dim, config.attention_heads, batch_first=True
            )
            layer = nn.TransformerEncoderLayer(
                d_model=config.hidden_dim,
                nhead=config.attention_heads,
                batch_first=True,
            )
            self.transformer = nn.TransformerEncoder(
                layer, num_layers=config.transformer_layers
            )
            self.first_output = nn.Linear(config.hidden_dim, config.hidden_dim // 2)
            self.second_output = nn.Linear(config.hidden_dim // 2, 7)

        def forward(
            self,
            noisy_position: Any,
            noisy_quaternion: Any,
            timestep: Any,
            wrench: Any,
        ) -> Any:
            tokens = self.trajectory_embedding(
                torch.cat((noisy_position, noisy_quaternion), dim=-1)
            ) + self.positional_encoding
            timestep = torch.as_tensor(
                timestep, dtype=torch.long, device=tokens.device
            )
            if timestep.ndim == 0:
                timestep = timestep.expand(tokens.shape[0])
            tokens = tokens + self.time_embedding(timestep).unsqueeze(1)
            condition = self.condition_projection(wrench)
            attended, _ = self.cross_attention(
                query=tokens, key=condition, value=condition
            )
            encoded = self.transformer(tokens + attended)
            return self.second_output(torch.relu(self.first_output(encoded)))


else:

    class UpstreamNoisePredictor:  # type: ignore[no-redef]
        def __init__(self, config: DBILConfig) -> None:
            del config
            require_torch()


def add_upstream_noise(
    clean_pose: Any,
    complete_noisy_pose: Any,
    config: DBILConfig,
    *,
    noiseadding_steps: int,
    timestep: int,
) -> tuple[Any, Any, Any, Any]:
    """Deterministic form of upstream ``add_noise`` for testing/training."""

    framework = require_torch()
    if config.diffusion_variant != UPSTREAM_VARIANT:
        raise ValueError(f"noise function requires {UPSTREAM_VARIANT}")
    if not 1 <= noiseadding_steps <= config.denoising_steps:
        raise ValueError("noiseadding_steps is outside configured schedule")
    if not 0 <= timestep < noiseadding_steps:
        raise ValueError("timestep is outside the selected noise schedule")
    beta = framework.linspace(
        config.beta_start,
        config.beta_end,
        noiseadding_steps,
        dtype=clean_pose.dtype,
        device=clean_pose.device,
    )
    alpha_bar = framework.cumprod(1.0 - beta, dim=0)
    sqrt_alpha = framework.sqrt(alpha_bar[timestep])
    actual_position_noise = complete_noisy_pose[..., :3] - clean_pose[..., :3]
    noisy_position = (
        sqrt_alpha * clean_pose[..., :3]
        + framework.sqrt(1.0 - alpha_bar[timestep]) * actual_position_noise
    )
    noisy_quaternion = quaternion_slerp(
        clean_pose[..., 3:7], complete_noisy_pose[..., 3:7], sqrt_alpha
    )
    noise_scale = 1.0 / sqrt_alpha
    return noisy_position, noisy_quaternion, noise_scale, framework.as_tensor(
        timestep, dtype=framework.long, device=clean_pose.device
    )


def upstream_training_loss(
    model: Any,
    clean_pose: Any,
    complete_noisy_pose: Any,
    wrench: Any,
    config: DBILConfig,
    *,
    noiseadding_steps: int,
    timestep: int,
) -> Any:
    framework = require_torch()
    noisy_position, noisy_quaternion, noise_scale, step = add_upstream_noise(
        clean_pose,
        complete_noisy_pose,
        config,
        noiseadding_steps=noiseadding_steps,
        timestep=timestep,
    )
    actual_position_noise = noisy_position - clean_pose[..., :3]
    actual_quaternion_noise = quaternion_multiply(
        noisy_quaternion, quaternion_inverse(clean_pose[..., 3:7])
    )
    prediction = model(noisy_position, noisy_quaternion, step, wrench)
    position_loss = framework.nn.functional.smooth_l1_loss(
        prediction[..., :3], actual_position_noise
    )
    return (
        position_loss
        + 4.0 * quaternion_loss(prediction[..., 3:7], actual_quaternion_noise)
    ) / framework.clamp(noise_scale, min=1e-6) * 10000.0


def reconstruct_upstream(
    model: Any,
    observed_pose: Any,
    wrench: Any,
    config: DBILConfig,
) -> Any:
    """Mirror upstream iterative subtract/inverse-multiply reconstruction."""

    framework = require_torch()
    position = observed_pose[..., :3].clone()
    quaternion = _normalise_quaternion(observed_pose[..., 3:7].clone())
    model.eval()
    with framework.no_grad():
        # The pinned upstream test path iterates 0..N-1, not reverse DDPM time.
        for timestep in range(config.denoising_steps):
            predicted = model(position, quaternion, timestep, wrench)
            position = position - predicted[..., :3]
            quaternion = quaternion_multiply(
                quaternion, quaternion_inverse(predicted[..., 3:7])
            )
    return framework.cat((position, _normalise_quaternion(quaternion)), dim=-1)


def upstream_stiffness_estimate(
    orientation_axis: np.ndarray,
    linear_error: np.ndarray,
    linear_velocity: np.ndarray,
    rotation_error: np.ndarray,
    angular_velocity: np.ndarray,
    force: np.ndarray,
    moment: np.ndarray,
    gamma: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Portable numerical extraction of upstream per-window stiffness estimate."""

    arrays = tuple(
        np.asarray(value, dtype=float)
        for value in (
            orientation_axis,
            linear_error,
            linear_velocity,
            rotation_error,
            angular_velocity,
            force,
            moment,
        )
    )
    if any(value.ndim != 2 or value.shape[1] != 3 for value in arrays):
        raise ValueError("stiffness estimator inputs must have shape [T,3]")
    if len({value.shape[0] for value in arrays}) != 1 or not all(
        np.isfinite(value).all() for value in arrays
    ):
        raise ValueError("stiffness estimator windows must align and be finite")
    if not math.isfinite(gamma):
        raise ValueError("gamma must be finite")
    axis, error, velocity, rotation, omega, force_values, moment_values = arrays
    epsilon = 1e-6
    trans_norm = np.linalg.norm(error, axis=0)
    rot_norm = np.linalg.norm(axis, axis=0)
    trans_importance = trans_norm / (np.sum(trans_norm) + epsilon)
    rot_importance = rot_norm / (np.sum(rot_norm) + epsilon)
    translational = np.empty(3)
    rotational = np.empty(3)
    for index in range(3):
        e_value = error[:, index] - gamma * velocity[:, index]
        if np.max(np.abs(force_values[:, index])) < 0.2:
            translational[index] = 800.0
        else:
            drop = abs(np.dot(force_values[:, index], e_value)) / (
                np.dot(e_value, e_value) + epsilon
            )
            translational[index] = np.clip(
                800.0 - 10.0 * (1.0 - trans_importance[index]) * drop,
                0.0,
                800.0,
            )
        r_value = rotation[:, index] - gamma * omega[:, index]
        if np.max(np.abs(moment_values[:, index])) < 1.0:
            rotational[index] = 150.0
        else:
            drop = abs(np.dot(moment_values[:, index], r_value)) / (
                np.dot(r_value, r_value) + epsilon
            )
            rotational[index] = np.clip(
                150.0 - 2.0 * (1.0 - rot_importance[index]) * drop,
                0.0,
                150.0,
            )
    return translational, rotational
