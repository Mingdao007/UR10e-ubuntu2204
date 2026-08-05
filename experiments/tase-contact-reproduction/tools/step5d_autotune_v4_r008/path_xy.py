"""r008 PATH XY excursion telemetry (ops visibility for slow cycloid).

Formal PATH is a 15 mm-amplitude cycloid (~94 mm along / ~30 mm lateral over
60 s at ≤3 mm/s).  Tracking error metrics alone look like \"sub-mm motion\" and
QUAL is intentionally zero-qdot, so this helper emits along/lateral peaks in
millimetres after each sealed formal trial.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r004.path_reference import (
    PATH_AMPLITUDE_M,
    PATH_DURATION_S,
    PATH_OMEGA_RAD_S,
    PATH_STAGE_ID,
    R004_PATH_FRAME_SNAPSHOT,
    step5_path_reference,
)

FORMAL_KINDS = frozenset({"STAIRCASE", "SPACEFILL", "BO_TRIAL", "BO", "RETEST", "WARM_START_1", "WARM_START_2", "ROUTE"})


def theory_xy_excursion_mm() -> dict[str, float]:
    """Bound cycloid peaks in the path frame (independent of a sealed row)."""

    origin = R004_PATH_FRAME_SNAPSHOT["basis"]["origin_xy_m"]
    u_along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    p_lateral = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]

    def _project(xy: tuple[float, float]) -> tuple[float, float]:
        dx = xy[0] - origin[0]
        dy = xy[1] - origin[1]
        return (
            dx * u_along[0] + dy * u_along[1],
            dx * p_lateral[0] + dy * p_lateral[1],
        )

    along: list[float] = []
    lateral: list[float] = []
    steps = 601
    for index in range(steps):
        time_s = PATH_DURATION_S * index / (steps - 1)
        ref = step5_path_reference(PATH_STAGE_ID, origin, time_s)
        a_m, l_m = _project(tuple(ref["desired_xy"]))
        along.append(a_m)
        lateral.append(l_m)
    return {
        "theory_along_end_mm": along[-1] * 1000.0,
        "theory_lateral_peak_mm": max(abs(value) for value in lateral) * 1000.0,
        "theory_amplitude_mm": PATH_AMPLITUDE_M * 1000.0,
        "theory_omega_rad_s": PATH_OMEGA_RAD_S,
        "theory_max_along_speed_mm_s": PATH_AMPLITUDE_M * PATH_OMEGA_RAD_S * 2.0 * 1000.0,
        "theory_max_lateral_speed_mm_s": PATH_AMPLITUDE_M * PATH_OMEGA_RAD_S * 1000.0,
    }


def path_xy_summary_from_record(record: Any) -> dict[str, Any] | None:
    """Build a millimetre XY summary for a sealed formal observation."""

    kind = getattr(record, "kind", None)
    if kind not in FORMAL_KINDS:
        return None
    metrics = getattr(record, "metrics", None)
    if not isinstance(metrics, Mapping):
        metrics = {}
    theory = theory_xy_excursion_mm()
    xy_p95 = metrics.get("xy_error_p95_m")
    xy_max = metrics.get("xy_error_max_m")
    xy_p95_mm = float(xy_p95) * 1000.0 if isinstance(xy_p95, (int, float)) and math.isfinite(float(xy_p95)) else None
    xy_max_mm = float(xy_max) * 1000.0 if isinstance(xy_max, (int, float)) and math.isfinite(float(xy_max)) else None
    summary: dict[str, Any] = {
        "schema": "step5d.autotune-v4/r008-path-xy-v1",
        "attempt_sequence": int(getattr(record, "attempt_sequence", 0) or 0),
        "kind": str(kind),
        "mae_n": getattr(record, "mae_n", None),
        "motion_gate": bool(getattr(record, "motion_gate", False)),
        **theory,
        "xy_error_p95_mm": xy_p95_mm,
        "xy_error_max_mm": xy_max_mm,
        "note": "xy_error is tracking error vs cycloid reference, not path amplitude",
    }
    if xy_max_mm is not None:
        summary["actual_along_lb_mm"] = theory["theory_along_end_mm"] - xy_max_mm
        summary["actual_lateral_lb_mm"] = theory["theory_lateral_peak_mm"] - xy_max_mm
    return summary


def format_path_xy_event(summary: Mapping[str, Any]) -> str:
    """Compact host event line for the live event tail."""

    parts = [
        f"seq={summary.get('attempt_sequence')}",
        f"kind={summary.get('kind')}",
        f"along_mm={float(summary['theory_along_end_mm']):.1f}",
        f"lat_peak_mm={float(summary['theory_lateral_peak_mm']):.1f}",
    ]
    if summary.get("xy_error_p95_mm") is not None:
        parts.append(f"xy_err_p95_mm={float(summary['xy_error_p95_mm']):.2f}")
    if summary.get("actual_along_lb_mm") is not None:
        parts.append(f"actual_along_lb_mm={float(summary['actual_along_lb_mm']):.1f}")
    if summary.get("actual_lateral_lb_mm") is not None:
        parts.append(f"actual_lat_lb_mm={float(summary['actual_lateral_lb_mm']):.1f}")
    return "R008_PATH_XY:" + ":".join(parts)


def append_path_xy_sidecar(run_dir: Path, summary: Mapping[str, Any]) -> Path:
    """Append one JSONL row next to the run ledger for ops grepping."""

    path = Path(run_dir) / "r008-path-xy.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(summary), sort_keys=True, allow_nan=False) + "\n")
    return path


__all__ = [
    "FORMAL_KINDS",
    "append_path_xy_sidecar",
    "format_path_xy_event",
    "path_xy_summary_from_record",
    "theory_xy_excursion_mm",
]
