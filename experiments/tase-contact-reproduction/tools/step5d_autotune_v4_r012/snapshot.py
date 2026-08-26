"""Readable R012 live preparation snapshot."""

from __future__ import annotations

import subprocess
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .behavior import R012_LINEAGE, R012_PROGRAM, R012_RUNTIME_PROTOCOL, validate_manual_wave
from .censor import CensorProtocol
from .common import R012ValueError, freeze_tree, json_tree, strict_json_object
from .qlognei import ProductionQLogNEIBinding, ProductionGPConfig
from .runtime_composition import runtime_composition_manifest, validate_runtime_composition
from .safety_filter import SafetyFilterConfig


ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_SCHEMA = "step5d.autotune-v4/r012-live-snapshot-v1"
DEFAULT_WAVE_PATH = ROOT / "config/step5d/autotune_v4_r012_wave_schedule.json"
DEFAULT_NOISE_PATH = ROOT / "config/step5d/autotune_v4_r012_observation_noise.json"
DEFAULT_CENSOR_PATH = ROOT / "config/step5d/autotune_v4_r012_censoring.json"
DEFAULT_SAFETY_PATH = ROOT / "config/step5d/autotune_v4_r012_safety_filter.json"


class LiveSnapshotError(R012ValueError):
    """A persisted R012 live snapshot is malformed or incomplete."""


class CampaignMode(str, Enum):
    """Physical campaign lifecycle selected by the persisted snapshot."""

    ONE_SHOT = "one_shot"
    CONTINUOUS = "continuous"

    @classmethod
    def parse(cls, value: str | "CampaignMode" | None) -> "CampaignMode":
        raw = (
            cls.ONE_SHOT.value
            if value is None
            else value.value
            if isinstance(value, cls)
            else str(value).strip().lower()
        )
        try:
            return cls(raw)
        except ValueError as exc:
            raise LiveSnapshotError(f"unsupported R012 campaign mode {value!r}") from exc


def _git_value(root: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), *args], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise LiveSnapshotError("cannot read Git HEAD for R012 snapshot") from exc


def _working_tree_dirty(root: Path) -> bool:
    return bool(_git_value(root, "status", "--porcelain", "--untracked-files=all"))


def _validate_safety_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LiveSnapshotError("R012 safety filter config must be an object")
    required = {
        "schema", "scope", "dt_s", "ellipse_axes_m", "tightened_axes_m",
        "tracking_error_bound_m", "latency_error_bound_m", "frame_error_bound_m",
        "velocity_min_m_s", "velocity_max_m_s", "acceleration_min_m_s2",
        "acceleration_max_m_s2", "max_state_age_s", "alpha_s_inv",
        "engage_deadband", "force_cbf_claim",
    }
    if set(value) != required:
        raise LiveSnapshotError("R012 safety filter config fields differ")
    bounds = dict(value)
    bounds.pop("tightened_axes_m")
    bounds.pop("scope")
    bounds.pop("force_cbf_claim")
    config = SafetyFilterConfig(**bounds)
    if any(not math.isclose(float(actual), float(expected), rel_tol=0.0, abs_tol=1e-12) for actual, expected in zip(config.tightened_axes_m, value["tightened_axes_m"])):
        raise LiveSnapshotError("R012 safety tightened axes differ")
    return json_tree(value)


@dataclass(frozen=True)
class R012BehaviorConfig:
    """Typed readable values consumed by the R012 runtime."""

    wave: Mapping[str, Any]
    noise: Mapping[str, Any]
    censor: Mapping[str, Any]
    safety: Mapping[str, Any]
    optimizer: Mapping[str, Any]
    runtime: Mapping[str, Any]

    def __post_init__(self) -> None:
        wave = validate_manual_wave(self.wave).as_dict()
        noise = json_tree(self.noise)
        censor = json_tree(self.censor)
        censor["objective_semantics"] = "force-mae-v2-sealed"
        safety = _validate_safety_config(self.safety)
        optimizer = ProductionQLogNEIBinding(**{
            key: self.optimizer[key]
            for key in ("component", "acquisition", "pending_model", "q", "censored_observations_allowed", "theory_shadow_separate", "schema")
            if key in self.optimizer
        }).as_dict()
        if CensorProtocol().as_dict()["denominator_bins"] != censor.get("denominator_bins"):
            raise LiveSnapshotError("R012 censor denominator differs")
        runtime = validate_runtime_composition(self.runtime)
        object.__setattr__(self, "wave", freeze_tree(wave))
        object.__setattr__(self, "noise", freeze_tree(noise))
        object.__setattr__(self, "censor", freeze_tree(censor))
        object.__setattr__(self, "safety", freeze_tree(safety))
        object.__setattr__(self, "optimizer", freeze_tree(optimizer))
        object.__setattr__(self, "runtime", freeze_tree(runtime))

    def as_dict(self) -> dict[str, Any]:
        return {
            "wave": json_tree(self.wave),
            "noise": json_tree(self.noise),
            "censor": json_tree(self.censor),
            "safety": json_tree(self.safety),
            "optimizer": json_tree(self.optimizer),
            "runtime": json_tree(self.runtime),
        }


@dataclass(frozen=True)
class LiveSnapshot:
    schema: str
    revision: int
    program: str
    lineage: str
    parent_revision: str
    campaign_id: str
    run_id: str
    attempt_id: str
    git_revision: str
    working_tree_dirty: bool
    controller_programs: Mapping[str, Mapping[str, str]]
    behavior_config: R012BehaviorConfig
    runtime_protocol: int
    created_at_utc: str
    campaign_mode: str = CampaignMode.ONE_SHOT.value

    def __post_init__(self) -> None:
        if self.schema != SNAPSHOT_SCHEMA or self.revision != 12:
            raise LiveSnapshotError("R012 snapshot schema/revision differs")
        if self.program != R012_PROGRAM or self.lineage != R012_LINEAGE or self.parent_revision != "r011":
            raise LiveSnapshotError("R012 snapshot lineage differs")
        for role in ("script", "txt", "urp"):
            entry = self.controller_programs.get(role)
            if not isinstance(entry, Mapping) or set(entry) != {"path", "basename"}:
                raise LiveSnapshotError("R012 controller program entries differ")
            path = Path(str(entry["path"]))
            if path.name != entry["basename"] or not path.is_file() or path.is_symlink():
                raise LiveSnapshotError(f"R012 controller program path is unavailable: {path}")
        for name, value in (("campaign_id", self.campaign_id), ("run_id", self.run_id), ("attempt_id", self.attempt_id), ("git_revision", self.git_revision), ("created_at_utc", self.created_at_utc)):
            if not isinstance(value, str) or not value:
                raise LiveSnapshotError(f"R012 snapshot {name} is invalid")
        if not isinstance(self.working_tree_dirty, bool) or self.runtime_protocol != R012_RUNTIME_PROTOCOL:
            raise LiveSnapshotError("R012 snapshot runtime values differ")
        object.__setattr__(self, "campaign_mode", CampaignMode.parse(self.campaign_mode).value)
        object.__setattr__(self, "controller_programs", freeze_tree(json_tree(self.controller_programs)))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "revision": self.revision,
            "program": self.program,
            "lineage": self.lineage,
            "parent_revision": self.parent_revision,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "git_revision": self.git_revision,
            "working_tree_dirty": self.working_tree_dirty,
            "controller_programs": json_tree(self.controller_programs),
            "behavior_config": self.behavior_config.as_dict(),
            "runtime_protocol": self.runtime_protocol,
            "created_at_utc": self.created_at_utc,
            "campaign_mode": self.campaign_mode,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LiveSnapshot":
        required = {
            "schema", "revision", "program", "lineage", "parent_revision", "campaign_id", "run_id", "attempt_id",
            "git_revision", "working_tree_dirty", "controller_programs", "behavior_config", "runtime_protocol", "created_at_utc",
        }
        allowed = required | {"campaign_mode"}
        if not isinstance(value, Mapping) or set(value) not in (required, allowed):
            raise LiveSnapshotError("R012 snapshot fields differ")
        config = value["behavior_config"]
        if not isinstance(config, Mapping) or set(config) != {"wave", "noise", "censor", "safety", "optimizer", "runtime"}:
            raise LiveSnapshotError("R012 typed behavior config fields differ")
        return cls(
            schema=value["schema"], revision=value["revision"], program=value["program"], lineage=value["lineage"],
            parent_revision=value["parent_revision"], campaign_id=value["campaign_id"], run_id=value["run_id"], attempt_id=value["attempt_id"],
            git_revision=value["git_revision"], working_tree_dirty=value["working_tree_dirty"], controller_programs=value["controller_programs"],
            behavior_config=R012BehaviorConfig(**config), runtime_protocol=value["runtime_protocol"], created_at_utc=value["created_at_utc"], campaign_mode=value.get("campaign_mode", CampaignMode.ONE_SHOT.value),
        )


def load_live_snapshot(path: Path) -> LiveSnapshot:
    return LiveSnapshot.from_mapping(strict_json_object(Path(path), "R012 live snapshot"))


def build_behavior_config(*, root: Path = ROOT) -> R012BehaviorConfig:
    safety = strict_json_object(DEFAULT_SAFETY_PATH, "R012 safety filter")
    return R012BehaviorConfig(
        wave=strict_json_object(DEFAULT_WAVE_PATH, "R012 wave schedule"),
        noise=strict_json_object(DEFAULT_NOISE_PATH, "R012 observation noise"),
        censor=strict_json_object(DEFAULT_CENSOR_PATH, "R012 censor policy"),
        safety=safety,
        optimizer=ProductionQLogNEIBinding().as_dict(),
        runtime=runtime_composition_manifest(),
    )


def build_live_snapshot(*, campaign_id: str, run_id: str, attempt_id: str, controller_programs: Mapping[str, Mapping[str, str]], root: Path = ROOT, created_at_utc: str | None = None, campaign_mode: str | CampaignMode = CampaignMode.ONE_SHOT) -> LiveSnapshot:
    return LiveSnapshot(
        schema=SNAPSHOT_SCHEMA,
        revision=12,
        program=R012_PROGRAM,
        lineage=R012_LINEAGE,
        parent_revision="r011",
        campaign_id=campaign_id,
        run_id=run_id,
        attempt_id=attempt_id,
        git_revision=_git_value(root, "rev-parse", "HEAD"),
        working_tree_dirty=_working_tree_dirty(root),
        controller_programs=controller_programs,
        behavior_config=build_behavior_config(root=root),
        runtime_protocol=R012_RUNTIME_PROTOCOL,
        created_at_utc=created_at_utc or datetime.now(timezone.utc).isoformat(),
        campaign_mode=CampaignMode.parse(campaign_mode).value,
    )


__all__ = ["CampaignMode", "LiveSnapshot", "LiveSnapshotError", "R012BehaviorConfig", "SNAPSHOT_SCHEMA", "build_live_snapshot", "load_live_snapshot"]
