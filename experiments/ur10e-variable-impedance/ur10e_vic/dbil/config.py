"""Pinned first-pass architecture for public-data DBIL preparation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DBILConfig:
    history_window: int = 16
    pose_dim: int = 7
    wrench_dim: int = 6
    target_dim: int = 7
    hidden_dim: int = 512
    attention_heads: int = 4
    transformer_layers: int = 6
    denoising_steps: int = 20
    seed: int = 42
    beta_start: float = 1e-4
    beta_end: float = 2e-2
    diffusion_variant: str = "portable_component_ddpm_v0"
    active_enabled: bool = False

    def __post_init__(self) -> None:
        pinned = {
            "history_window": 16,
            "hidden_dim": 512,
            "attention_heads": 4,
            "transformer_layers": 6,
            "denoising_steps": 20,
            "seed": 42,
        }
        for name, expected in pinned.items():
            if getattr(self, name) != expected:
                raise ValueError(f"first DBIL scaffold pins {name}={expected}")
        if (self.pose_dim, self.wrench_dim, self.target_dim) != (7, 6, 7):
            raise ValueError("DBIL scaffold expects pose7+wrench6 -> sZFT pose7")
        if not 0.0 < self.beta_start < self.beta_end < 1.0:
            raise ValueError("invalid diffusion beta schedule")
        if self.diffusion_variant not in {
            "portable_component_ddpm_v0",
            "upstream_slerp_cross_attention_v1",
        }:
            raise ValueError("unsupported diffusion variant")
        if self.active_enabled:
            raise ValueError("DBIL active mode is intentionally disabled")

    @property
    def context_dim(self) -> int:
        return self.pose_dim + self.wrench_dim

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "DBILConfig":
        return cls(**dict(payload))
