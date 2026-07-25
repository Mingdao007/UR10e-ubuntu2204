"""Checkpoint binding across all source, schema, split, and environment inputs."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
from typing import Mapping, Sequence


def _sha(value: str, name: str) -> str:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be lowercase SHA-256")
    return value


@dataclass(frozen=True)
class CheckpointBinding:
    schema_version: str
    surface_calibration_sha256: str
    action_profile_sha256: str
    filter_profile_sha256: str
    normalization_sha256: str
    split_sha256: str
    source_hashes: tuple[str, ...]
    dataset_sha256: str
    model_config: Mapping[str, object]
    environment_fingerprint: str
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_hashes", tuple(self.source_hashes))
        if self.schema_version != "ur10e_tacdiffusion_checkpoint/v2":
            raise ValueError("unsupported checkpoint schema")
        for name in ("surface_calibration_sha256", "action_profile_sha256", "filter_profile_sha256", "normalization_sha256", "split_sha256", "dataset_sha256", "environment_fingerprint", "checkpoint_sha256"):
            _sha(getattr(self, name), name)
        if not self.source_hashes or any(len(value) != 64 for value in self.source_hashes):
            raise ValueError("checkpoint source hashes are incomplete")
        if self.model_config.get("observation_dimension") != 84 or self.model_config.get("action_dimension") != 12:
            raise ValueError("checkpoint model configuration is not 84D/12D")

    def payload(self) -> dict[str, object]:
        return asdict(self)


def write_checkpoint_binding(path: str | Path, binding: CheckpointBinding) -> None:
    Path(path).write_text(json.dumps(binding.payload(), indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def validate_checkpoint_binding(path: str | Path) -> CheckpointBinding:
    return CheckpointBinding(**json.loads(Path(path).read_text(encoding="utf-8")))
