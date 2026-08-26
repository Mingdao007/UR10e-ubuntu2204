"""Explicit, analysis-only admission for the existing offline STARS sidecar."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import R012ValueError, freeze_tree, json_tree
from .offline_checksums import digest


STARS_ADMISSION_SCHEMA = "step5d.autotune-v4/r011-stars-idle-admission-v1"
STARS_SIDECAR_SCHEMA = "stars_ft_bias_shadow/overnight_v0"


class StarsAdmissionError(R012ValueError):
    """STARS replay admission request is invalid or refused."""


def _strict_bool(value: Any, role: str) -> bool:
    if not isinstance(value, bool): raise StarsAdmissionError(f"{role} must be boolean")
    return value


@dataclass(frozen=True)
class StarsAdmissionRequest:
    input_paths: tuple[Path, ...]
    sealed_inputs: Mapping[str, bool]
    explicit_batch_replay: bool
    live_writer_lease: bool
    active_attempt: bool
    gpu_requested: bool
    concurrent_worker: bool
    worker_count: int = 1
    cpu_count: int = 1
    nice: int = 19
    io_mode: str = "idle"
    campaign_id: str = "r012-stars"
    run_id: str = "offline-replay"
    attempt_id: str = "a001"

    def __post_init__(self) -> None:
        if not isinstance(self.input_paths, tuple) or any(not isinstance(path, Path) for path in self.input_paths): raise StarsAdmissionError("input_paths must be a tuple of Path")
        if not isinstance(self.sealed_inputs, Mapping) or any(not isinstance(key, str) or not isinstance(value, bool) for key, value in self.sealed_inputs.items()): raise StarsAdmissionError("sealed_inputs must map strings to strict booleans")
        for name in ("explicit_batch_replay", "live_writer_lease", "active_attempt", "gpu_requested", "concurrent_worker"): _strict_bool(getattr(self, name), name)
        for name in ("worker_count", "cpu_count", "nice"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int): raise StarsAdmissionError(f"{name} must be an integer")
        if not isinstance(self.io_mode, str): raise StarsAdmissionError("io_mode must be a string")
        if not all(isinstance(value, str) and value for value in (self.campaign_id, self.run_id, self.attempt_id)):
            raise StarsAdmissionError("STARS campaign/run/attempt identity is invalid")


@dataclass(frozen=True)
class StarsAdmissionDecision:
    admitted: bool
    reasons: tuple[str, ...]
    job: Mapping[str, Any]
    campaign_id: str
    run_id: str
    attempt_id: str
    input_ids: Mapping[str, str]
    completion_id: str
    campaign_member: bool = False
    gp_observation: bool = False
    force_correction: bool = False
    completion_certificate: bool = False
    analysis_only: bool = True
    schema: str = STARS_ADMISSION_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != STARS_ADMISSION_SCHEMA or not self.analysis_only or self.campaign_member or self.gp_observation or self.force_correction or self.completion_certificate: raise StarsAdmissionError("STARS admission crossed the analysis-only boundary")
        if not all(isinstance(value, str) and value for value in (self.campaign_id, self.run_id, self.attempt_id, self.completion_id)): raise StarsAdmissionError("STARS decision identity is invalid")
        object.__setattr__(self, "job", freeze_tree(json_tree(self.job))); object.__setattr__(self, "input_ids", freeze_tree(json_tree(self.input_ids)))

    @property
    def allowed(self) -> bool: return self.admitted

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "admitted": self.admitted, "reasons": list(self.reasons), "job": json_tree(self.job), "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id, "input_ids": json_tree(self.input_ids), "completion_id": self.completion_id, "analysis_only": True, "campaign_member": False, "gp_observation": False, "force_correction": False, "completion_certificate": False}


def _file_id(path: Path) -> str:
    return digest(path.read_bytes().hex())


def admit_stars_batch_replay(request: StarsAdmissionRequest) -> StarsAdmissionDecision:
    if not isinstance(request, StarsAdmissionRequest): raise StarsAdmissionError("STARS admission requires a typed request")
    reasons: list[str] = []
    if not request.explicit_batch_replay: reasons.append("explicit_batch_replay_required")
    if request.live_writer_lease: reasons.append("live_writer_lease_present")
    if request.active_attempt: reasons.append("active_attempt_present")
    if request.gpu_requested: reasons.append("gpu_forbidden")
    if request.concurrent_worker: reasons.append("concurrent_worker_present")
    if request.worker_count != 1: reasons.append("one_worker_required")
    if request.cpu_count != 1: reasons.append("one_cpu_required")
    if request.nice != 19: reasons.append("nice_19_required")
    if request.io_mode != "idle": reasons.append("idle_io_required")
    input_ids: dict[str, str] = {}
    if not request.input_paths: reasons.append("sealed_inputs_required")
    for path in request.input_paths:
        if path.is_symlink() or not path.is_file(): reasons.append(f"input_not_regular:{path}")
        else: input_ids[str(path)] = _file_id(path)
        if request.sealed_inputs.get(str(path), False) is not True: reasons.append(f"input_not_sealed:{path}")
    job = {"sidecar_schema": STARS_SIDECAR_SCHEMA, "worker_count": 1, "cpu_count": 1, "gpu": False, "nice": 19, "io_mode": "idle", "auto_launch": False, "input_paths": [str(path) for path in request.input_paths], "authority": "analysis_only"}
    receipt = digest({"schema": STARS_ADMISSION_SCHEMA, "admitted": not reasons, "campaign_id": request.campaign_id, "run_id": request.run_id, "attempt_id": request.attempt_id, "input_ids": input_ids, "job": job})
    return StarsAdmissionDecision(not reasons, tuple(reasons), job, request.campaign_id, request.run_id, request.attempt_id, input_ids, receipt)


__all__ = ["STARS_ADMISSION_SCHEMA", "STARS_SIDECAR_SCHEMA", "StarsAdmissionDecision", "StarsAdmissionError", "StarsAdmissionRequest", "admit_stars_batch_replay"]
