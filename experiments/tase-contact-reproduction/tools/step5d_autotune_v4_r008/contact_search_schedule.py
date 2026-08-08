"""r008-owned two-stage contact search schedule (B3 Wave-1 observation).

Wave 1 is a conservative placeholder / observation schedule (NOT speed-
optimization): v_far=0.0008 > v_near=0.0005, with a longer near band
(d_near_m=0.0215 → d_near_start_travel_m=0.0035). This module never mutates
r004–r007 hashed motion constants.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEDULE_PATH = ROOT / "config/step5d/autotune_v4_r008_contact_search_schedule.json"
SCHEDULE_SCHEMA = "step5d.autotune-v4/r008-contact-search-schedule-v1"
PROGRAM_B3 = "step5d_strict_rnn_autotune_v4_r008_b3_two_stage"
MAINLINE_FINGERPRINT = (
    "1db4f9bf587826e7562dccef7040f627e660e540f847a2e8b92f63879870cc4a"
)
FAR_SPEED_M_S = 0.0008
NEAR_SPEED_M_S = 0.0005
MAX_TRAVEL_M = 0.025
D_NEAR_M = 0.0215
D_NEAR_START_TRAVEL_M = 0.0035  # max_travel - d_near
F_FAR_N = 0.3
MAX_FORCE_FUSE_N = 50.0
FORCE_FUSE_REASON = 75


class ContactSearchScheduleError(ValueError):
    """Contact search schedule is invalid or unsafe for B3."""


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContactSearchScheduleError(f"{role} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ContactSearchScheduleError(f"{role} must be finite")
    return result


def _positive(value: Any, role: str) -> float:
    result = _finite(value, role)
    if result <= 0.0:
        raise ContactSearchScheduleError(f"{role} must be positive")
    return result


@dataclass(frozen=True)
class ContactSearchSchedule:
    """Validated FAR/NEAR contact search schedule for the B3 canary TP."""

    schema: str
    version: str
    program: str
    parent_campaign_fingerprint: str
    max_travel_m: float
    timeout_s: float
    v_far_m_s: float
    v_near_m_s: float
    far_acceleration_m_s2: float
    near_acceleration_m_s2: float
    d_near_m: float
    F_far_n: float
    force_fuse_n: float
    force_fuse_reason: int
    confirm_normal_n: float
    confirm_force_norm_n: float
    confirm_hold_s: float
    raw: Mapping[str, Any]

    @property
    def d_near_start_travel_m(self) -> float:
        """Travel depth where FAR ends and NEAR begins (max_travel - d_near)."""

        return self.max_travel_m - self.d_near_m

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "program": self.program,
            "parent_campaign_fingerprint": self.parent_campaign_fingerprint,
            "max_travel_m": self.max_travel_m,
            "timeout_s": self.timeout_s,
            "v_far_m_s": self.v_far_m_s,
            "v_near_m_s": self.v_near_m_s,
            "far_acceleration_m_s2": self.far_acceleration_m_s2,
            "near_acceleration_m_s2": self.near_acceleration_m_s2,
            "d_near_m": self.d_near_m,
            "F_far_n": self.F_far_n,
            "force_fuse_n": self.force_fuse_n,
            "force_fuse_reason": self.force_fuse_reason,
            "confirm_normal_n": self.confirm_normal_n,
            "confirm_force_norm_n": self.confirm_force_norm_n,
            "confirm_hold_s": self.confirm_hold_s,
            "d_near_start_travel_m": self.d_near_start_travel_m,
        }


def validate_schedule(document: Mapping[str, Any]) -> ContactSearchSchedule:
    if not isinstance(document, Mapping):
        raise ContactSearchScheduleError("schedule must be an object")
    if document.get("schema") != SCHEDULE_SCHEMA:
        raise ContactSearchScheduleError("schedule schema differs")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise ContactSearchScheduleError("schedule version is invalid")
    program = document.get("program")
    if program != PROGRAM_B3:
        raise ContactSearchScheduleError("schedule program must be B3 canary identity")
    parent_fp = document.get("parent_campaign_fingerprint")
    if parent_fp != MAINLINE_FINGERPRINT:
        raise ContactSearchScheduleError("parent_campaign_fingerprint must be mainline 1db4f9bf")

    max_travel_m = _positive(document.get("max_travel_m"), "max_travel_m")
    if not math.isclose(max_travel_m, MAX_TRAVEL_M, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"max_travel_m must be exactly {MAX_TRAVEL_M} (got {max_travel_m})"
        )
    timeout_s = _positive(document.get("timeout_s"), "timeout_s")
    v_far_m_s = _positive(document.get("v_far_m_s"), "v_far_m_s")
    if not math.isclose(v_far_m_s, FAR_SPEED_M_S, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"v_far_m_s must be exactly {FAR_SPEED_M_S} (got {v_far_m_s})"
        )
    v_near_m_s = _finite(document.get("v_near_m_s"), "v_near_m_s")
    if not math.isclose(v_near_m_s, NEAR_SPEED_M_S, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"v_near_m_s must be exactly {NEAR_SPEED_M_S} (got {v_near_m_s})"
        )
    if v_far_m_s <= v_near_m_s:
        raise ContactSearchScheduleError("v_far_m_s must be greater than v_near_m_s")

    far_accel = _positive(document.get("far_acceleration_m_s2"), "far_acceleration_m_s2")
    near_accel = _positive(document.get("near_acceleration_m_s2"), "near_acceleration_m_s2")
    d_near_m = _positive(document.get("d_near_m"), "d_near_m")
    if not math.isclose(d_near_m, D_NEAR_M, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"d_near_m must be exactly {D_NEAR_M} (got {d_near_m})"
        )
    if d_near_m >= max_travel_m:
        raise ContactSearchScheduleError("d_near_m must be < max_travel_m")
    d_near_start = max_travel_m - d_near_m
    if not math.isclose(d_near_start, D_NEAR_START_TRAVEL_M, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"d_near_start_travel_m must be {D_NEAR_START_TRAVEL_M} "
            f"(got {d_near_start})"
        )

    f_far_n = _positive(document.get("F_far_n"), "F_far_n")
    if not math.isclose(f_far_n, F_FAR_N, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"F_far_n must be exactly {F_FAR_N} (got {f_far_n})"
        )
    force_fuse_n = _positive(document.get("force_fuse_n"), "force_fuse_n")
    if force_fuse_n > MAX_FORCE_FUSE_N:
        raise ContactSearchScheduleError(
            f"force_fuse_n must be <= {MAX_FORCE_FUSE_N} (got {force_fuse_n})"
        )
    if f_far_n >= force_fuse_n:
        raise ContactSearchScheduleError("F_far_n must be < force_fuse_n")

    fuse_reason = document.get("force_fuse_reason")
    if isinstance(fuse_reason, bool) or not isinstance(fuse_reason, int) or fuse_reason < 1:
        raise ContactSearchScheduleError("force_fuse_reason must be a positive int")
    if int(fuse_reason) != FORCE_FUSE_REASON:
        raise ContactSearchScheduleError(
            f"force_fuse_reason must be {FORCE_FUSE_REASON} (got {fuse_reason})"
        )

    confirm_normal_n = _positive(document.get("confirm_normal_n"), "confirm_normal_n")
    confirm_force_norm_n = _positive(
        document.get("confirm_force_norm_n"), "confirm_force_norm_n"
    )
    confirm_hold_s = _positive(document.get("confirm_hold_s"), "confirm_hold_s")
    if f_far_n >= confirm_normal_n:
        raise ContactSearchScheduleError("F_far_n must be < confirm_normal_n")

    return ContactSearchSchedule(
        schema=SCHEDULE_SCHEMA,
        version=version,
        program=PROGRAM_B3,
        parent_campaign_fingerprint=MAINLINE_FINGERPRINT,
        max_travel_m=max_travel_m,
        timeout_s=timeout_s,
        v_far_m_s=v_far_m_s,
        v_near_m_s=v_near_m_s,
        far_acceleration_m_s2=far_accel,
        near_acceleration_m_s2=near_accel,
        d_near_m=d_near_m,
        F_far_n=f_far_n,
        force_fuse_n=force_fuse_n,
        force_fuse_reason=int(fuse_reason),
        confirm_normal_n=confirm_normal_n,
        confirm_force_norm_n=confirm_force_norm_n,
        confirm_hold_s=confirm_hold_s,
        raw=dict(document),
    )


def load_schedule(path: Path | None = None) -> ContactSearchSchedule:
    schedule_path = DEFAULT_SCHEDULE_PATH if path is None else Path(path)
    if schedule_path.is_symlink() or not schedule_path.is_file():
        raise ContactSearchScheduleError(f"schedule missing or unsafe: {schedule_path}")
    try:
        document = json.loads(schedule_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContactSearchScheduleError(f"schedule is not strict JSON: {exc}") from exc
    version = document.get("version") if isinstance(document, Mapping) else None
    if isinstance(version, str) and version.startswith("b3-geometry-planned"):
        return validate_planned_schedule(document)
    return validate_schedule(document)


# Bounds for geometry-planned (speed-opt) schedules. Wave-1 `validate_schedule`
# remains pinned to the observation constants above.
PLANNED_V_NEAR_MIN_M_S = 0.0002
PLANNED_V_NEAR_MAX_M_S = 0.0007
PLANNED_V_FAR_MAX_M_S = 0.006
PLANNED_NEAR_ACCEL_M_S2 = 0.005
PLANNED_FAR_ACCEL_MAX_M_S2 = 0.05
PLANNED_CONFIRM_NORMAL_N = 0.5
PLANNED_CONFIRM_FORCE_NORM_N = 0.7
PLANNED_CONFIRM_HOLD_S = 0.08


def validate_planned_schedule(document: Mapping[str, Any]) -> ContactSearchSchedule:
    """Validate a geometry-planned schedule (variable v_far / d_near within caps)."""

    if not isinstance(document, Mapping):
        raise ContactSearchScheduleError("schedule must be an object")
    if document.get("schema") != SCHEDULE_SCHEMA:
        raise ContactSearchScheduleError("schedule schema differs")
    version = document.get("version")
    if not isinstance(version, str) or not version:
        raise ContactSearchScheduleError("schedule version is invalid")
    if not str(version).startswith("b3-geometry-planned"):
        raise ContactSearchScheduleError(
            "planned schedule version must start with b3-geometry-planned"
        )
    if document.get("program") != PROGRAM_B3:
        raise ContactSearchScheduleError("schedule program must be B3 canary identity")
    if document.get("parent_campaign_fingerprint") != MAINLINE_FINGERPRINT:
        raise ContactSearchScheduleError("parent_campaign_fingerprint must be mainline 1db4f9bf")

    max_travel_m = _positive(document.get("max_travel_m"), "max_travel_m")
    if not math.isclose(max_travel_m, MAX_TRAVEL_M, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"max_travel_m must be exactly {MAX_TRAVEL_M} (got {max_travel_m})"
        )
    timeout_s = _positive(document.get("timeout_s"), "timeout_s")
    v_far_m_s = _positive(document.get("v_far_m_s"), "v_far_m_s")
    v_near_m_s = _positive(document.get("v_near_m_s"), "v_near_m_s")
    if v_near_m_s < PLANNED_V_NEAR_MIN_M_S or v_near_m_s > PLANNED_V_NEAR_MAX_M_S:
        raise ContactSearchScheduleError(
            f"v_near_m_s must be in [{PLANNED_V_NEAR_MIN_M_S}, {PLANNED_V_NEAR_MAX_M_S}]"
        )
    if v_far_m_s > PLANNED_V_FAR_MAX_M_S:
        raise ContactSearchScheduleError(
            f"v_far_m_s must be <= {PLANNED_V_FAR_MAX_M_S} (got {v_far_m_s})"
        )
    if v_far_m_s <= v_near_m_s:
        raise ContactSearchScheduleError("v_far_m_s must be greater than v_near_m_s")

    far_accel = _positive(document.get("far_acceleration_m_s2"), "far_acceleration_m_s2")
    near_accel = _positive(document.get("near_acceleration_m_s2"), "near_acceleration_m_s2")
    if far_accel > PLANNED_FAR_ACCEL_MAX_M_S2:
        raise ContactSearchScheduleError(
            f"far_acceleration_m_s2 must be <= {PLANNED_FAR_ACCEL_MAX_M_S2}"
        )
    if not math.isclose(near_accel, PLANNED_NEAR_ACCEL_M_S2, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"near_acceleration_m_s2 must be {PLANNED_NEAR_ACCEL_M_S2}"
        )

    d_near_m = _positive(document.get("d_near_m"), "d_near_m")
    if d_near_m >= max_travel_m:
        raise ContactSearchScheduleError("d_near_m must be < max_travel_m")
    d_near_start = max_travel_m - d_near_m
    if d_near_start < 0.002 - 1e-12:
        raise ContactSearchScheduleError("d_near_start_travel_m must be >= 0.002")

    f_far_n = _positive(document.get("F_far_n"), "F_far_n")
    if not math.isclose(f_far_n, F_FAR_N, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(f"F_far_n must be exactly {F_FAR_N}")
    force_fuse_n = _positive(document.get("force_fuse_n"), "force_fuse_n")
    if force_fuse_n > MAX_FORCE_FUSE_N:
        raise ContactSearchScheduleError(
            f"force_fuse_n must be <= {MAX_FORCE_FUSE_N} (got {force_fuse_n})"
        )
    if f_far_n >= force_fuse_n:
        raise ContactSearchScheduleError("F_far_n must be < force_fuse_n")

    fuse_reason = document.get("force_fuse_reason")
    if isinstance(fuse_reason, bool) or not isinstance(fuse_reason, int) or fuse_reason < 1:
        raise ContactSearchScheduleError("force_fuse_reason must be a positive int")
    if int(fuse_reason) != FORCE_FUSE_REASON:
        raise ContactSearchScheduleError(
            f"force_fuse_reason must be {FORCE_FUSE_REASON} (got {fuse_reason})"
        )

    confirm_normal_n = _positive(document.get("confirm_normal_n"), "confirm_normal_n")
    confirm_force_norm_n = _positive(
        document.get("confirm_force_norm_n"), "confirm_force_norm_n"
    )
    confirm_hold_s = _positive(document.get("confirm_hold_s"), "confirm_hold_s")
    if not math.isclose(confirm_normal_n, PLANNED_CONFIRM_NORMAL_N, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(
            f"confirm_normal_n must be {PLANNED_CONFIRM_NORMAL_N}"
        )
    if not math.isclose(
        confirm_force_norm_n, PLANNED_CONFIRM_FORCE_NORM_N, rel_tol=0.0, abs_tol=1e-12
    ):
        raise ContactSearchScheduleError(
            f"confirm_force_norm_n must be {PLANNED_CONFIRM_FORCE_NORM_N}"
        )
    if not math.isclose(confirm_hold_s, PLANNED_CONFIRM_HOLD_S, rel_tol=0.0, abs_tol=1e-12):
        raise ContactSearchScheduleError(f"confirm_hold_s must be {PLANNED_CONFIRM_HOLD_S}")
    if f_far_n >= confirm_normal_n:
        raise ContactSearchScheduleError("F_far_n must be < confirm_normal_n")

    return ContactSearchSchedule(
        schema=SCHEDULE_SCHEMA,
        version=version,
        program=PROGRAM_B3,
        parent_campaign_fingerprint=MAINLINE_FINGERPRINT,
        max_travel_m=max_travel_m,
        timeout_s=timeout_s,
        v_far_m_s=v_far_m_s,
        v_near_m_s=v_near_m_s,
        far_acceleration_m_s2=far_accel,
        near_acceleration_m_s2=near_accel,
        d_near_m=d_near_m,
        F_far_n=f_far_n,
        force_fuse_n=force_fuse_n,
        force_fuse_reason=int(fuse_reason),
        confirm_normal_n=confirm_normal_n,
        confirm_force_norm_n=confirm_force_norm_n,
        confirm_hold_s=confirm_hold_s,
        raw=dict(document),
    )


__all__ = [
    "ContactSearchSchedule",
    "ContactSearchScheduleError",
    "DEFAULT_SCHEDULE_PATH",
    "D_NEAR_M",
    "D_NEAR_START_TRAVEL_M",
    "FAR_SPEED_M_S",
    "FORCE_FUSE_REASON",
    "F_FAR_N",
    "MAINLINE_FINGERPRINT",
    "MAX_TRAVEL_M",
    "NEAR_SPEED_M_S",
    "PLANNED_CONFIRM_FORCE_NORM_N",
    "PLANNED_CONFIRM_HOLD_S",
    "PLANNED_CONFIRM_NORMAL_N",
    "PLANNED_FAR_ACCEL_MAX_M_S2",
    "PLANNED_NEAR_ACCEL_M_S2",
    "PLANNED_V_FAR_MAX_M_S",
    "PLANNED_V_NEAR_MAX_M_S",
    "PLANNED_V_NEAR_MIN_M_S",
    "PROGRAM_B3",
    "SCHEDULE_SCHEMA",
    "load_schedule",
    "validate_planned_schedule",
    "validate_schedule",
]
