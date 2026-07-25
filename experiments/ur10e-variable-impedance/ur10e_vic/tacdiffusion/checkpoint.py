"""Checkpoint binding across all source, schema, split, and environment inputs."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import json
import os
from tempfile import NamedTemporaryFile
from pathlib import Path
from typing import Mapping, Sequence


CHECKPOINT_SCHEMA = "ur10e_tacdiffusion_checkpoint/v3"
LINEAGE_FIELDS = frozenset({
    "schema_version",
    "surface_calibration_sha256",
    "action_profile_sha256",
    "filter_profile_sha256",
    "normalization_sha256",
    "split_sha256",
    "source_hashes",
    "dataset_sha256",
    "model_config",
    "environment_fingerprint",
    "checkpoint_sha256",
})


def _sha(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
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
        if self.schema_version != CHECKPOINT_SCHEMA:
            raise ValueError("unsupported checkpoint schema")
        for name in ("surface_calibration_sha256", "action_profile_sha256", "filter_profile_sha256", "normalization_sha256", "split_sha256", "dataset_sha256", "environment_fingerprint", "checkpoint_sha256"):
            _sha(getattr(self, name), name)
        if not self.source_hashes or any(not isinstance(value, str) for value in self.source_hashes):
            raise ValueError("checkpoint source hashes are incomplete")
        for index, value in enumerate(self.source_hashes):
            _sha(value, f"source_hashes[{index}]")
        if not isinstance(self.model_config, Mapping):
            raise ValueError("checkpoint model configuration is invalid")
        if self.model_config.get("observation_dimension") != 84 or self.model_config.get("action_dimension") != 12:
            raise ValueError("checkpoint model configuration is not 84D/12D")
        if self.model_config.get("diffusion_steps") != 50:
            raise ValueError("checkpoint model configuration must use 50 diffusion steps")
        if self.model_config.get("model_update_rate_hz") not in {50, 100}:
            raise ValueError("checkpoint model update rate must be 50 or 100 Hz")

    def payload(self) -> dict[str, object]:
        return asdict(self)


def write_checkpoint_binding(path: str | Path, binding: CheckpointBinding) -> None:
    if not isinstance(binding, CheckpointBinding):
        raise TypeError("binding must be CheckpointBinding")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(binding.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("wb", dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def validate_checkpoint_binding(path: str | Path) -> CheckpointBinding:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("checkpoint binding JSON is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != LINEAGE_FIELDS:
        raise ValueError("checkpoint binding lineage fields are missing or extra")
    try:
        return CheckpointBinding(**payload)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"checkpoint binding is invalid: {exc}") from exc
