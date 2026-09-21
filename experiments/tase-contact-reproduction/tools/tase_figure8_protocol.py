"""Explicit 60 s TASE Figure-eight compatibility protocol.

The existing :mod:`contact_yield_protocol` describes the 62.831853 s
full-period task.  This module is deliberately separate: the historical
R013-compatible window is a 60 s measurement protocol and must never be
silently relabelled as a full period.

This module is offline and side-effect free.  It is used by the live adapter
only after the request identity has passed the normal writer admission gates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Iterable, Mapping


PROTOCOL_ID = "figure8_window60_r013_compat_v1"
SCHEMA = "tase.figure8-window60-r013-compat-v1"
DURATION_S = 60.0
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
FORMAL_WINDOW_S = (FORMAL_START_S, FORMAL_END_S)
BIN_WIDTH_S = 0.1
REQUIRED_BINS = 550
ENTRY_DURATION_S = 1.0
TARGET_FORCE_N = 5.0
OMEGA_RAD_S = 0.1
X_AMPLITUDE_M = 0.04
Y_AMPLITUDE_M = 0.01
SEAM_CONTINUATION_S = 0.004


class TaseFigure8ProtocolError(ValueError):
    """A protocol identity, sample, or metric contract is invalid."""


@dataclass(frozen=True)
class Figure8Window60Task:
    """Historical R013-compatible Figure-eight reference."""

    protocol_id: str = PROTOCOL_ID
    duration_s: float = DURATION_S
    entry_duration_s: float = ENTRY_DURATION_S
    target_force_n: float = TARGET_FORCE_N
    omega_rad_s: float = OMEGA_RAD_S
    x_amplitude_m: float = X_AMPLITUDE_M
    y_amplitude_m: float = Y_AMPLITUDE_M

    def __post_init__(self) -> None:
        if self.protocol_id != PROTOCOL_ID:
            raise TaseFigure8ProtocolError("60 s task protocol identity differs")
        if self.duration_s != DURATION_S or self.entry_duration_s != ENTRY_DURATION_S:
            raise TaseFigure8ProtocolError("60 s task duration is immutable")
        if self.target_force_n != TARGET_FORCE_N or self.omega_rad_s != OMEGA_RAD_S:
            raise TaseFigure8ProtocolError("60 s task target or frequency is immutable")
        if self.x_amplitude_m != X_AMPLITUDE_M or self.y_amplitude_m != Y_AMPLITUDE_M:
            raise TaseFigure8ProtocolError("60 s task amplitudes are immutable")

    def reference(self, time_s: float, *, allow_seam: bool = False) -> dict[str, Any]:
        t = float(time_s)
        limit = DURATION_S + (SEAM_CONTINUATION_S if allow_seam else 0.0)
        if not math.isfinite(t) or t < 0.0 or t > limit + 1e-12:
            raise TaseFigure8ProtocolError("60 s reference clock is outside the task")
        w = self.omega_rad_s
        return {
            "position_m": (
                self.x_amplitude_m * math.sin(w * t),
                self.y_amplitude_m * math.sin(2.0 * w * t),
                0.0,
            ),
            "velocity_m_s": (
                self.x_amplitude_m * w * math.cos(w * t),
                2.0 * self.y_amplitude_m * w * math.cos(2.0 * w * t),
                0.0,
            ),
            "acceleration_m_s2": (
                -self.x_amplitude_m * w * w * math.sin(w * t),
                -4.0 * self.y_amplitude_m * w * w * math.sin(2.0 * w * t),
                0.0,
            ),
            "reference_force_n": self.target_force_n,
            "protocol_id": self.protocol_id,
        }

    def entry_reference(self, time_s: float) -> dict[str, Any]:
        t = float(time_s)
        if not math.isfinite(t) or not 0.0 <= t <= ENTRY_DURATION_S:
            raise TaseFigure8ProtocolError("entry clock is outside the 1 s entry")
        # The entry is a zero-velocity quintic ramp; the formal task clock
        # starts only after this separate stage.
        s = t / ENTRY_DURATION_S
        h = -4.0 * s**3 + 7.0 * s**4 - 3.0 * s**5
        dh = (-12.0 * s**2 + 28.0 * s**3 - 15.0 * s**4) / ENTRY_DURATION_S
        task = self.reference(0.0)
        return {
            "position_m": tuple(h * value for value in task["position_m"]),
            "velocity_m_s": tuple(dh * value for value in task["position_m"]),
            "reference_force_n": self.target_force_n,
            "protocol_id": self.protocol_id,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "protocol_id": self.protocol_id,
            "formal_window_s": list(FORMAL_WINDOW_S),
            "duration_s": self.duration_s,
            "entry_duration_s": self.entry_duration_s,
            "target_force_n": self.target_force_n,
            "omega_rad_s": self.omega_rad_s,
            "x_amplitude_m": self.x_amplitude_m,
            "y_amplitude_m": self.y_amplitude_m,
            "bin_width_s": BIN_WIDTH_S,
            "required_bins": REQUIRED_BINS,
            "full_period_protocol": False,
            "historical_compatibility": "R013",
        }

    @property
    def identity_sha256(self) -> str:
        payload = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def protocol_manifest() -> dict[str, Any]:
    task = Figure8Window60Task()
    return {**task.as_dict(), "identity_sha256": task.identity_sha256}


def _row_value(row: Any, *names: str) -> float | None:
    for name in names:
        if isinstance(row, Mapping) and name in row:
            value = row[name]
        else:
            value = getattr(row, name, None)
        if value is not None:
            try:
                result = float(value)
            except (TypeError, ValueError):
                raise TaseFigure8ProtocolError(f"sample field {name!r} is not numeric")
            if not math.isfinite(result):
                raise TaseFigure8ProtocolError(f"sample field {name!r} is not finite")
            return result
    return None


def score_formal_samples(
    samples: Iterable[Any],
    *,
    stage: str = "PATH",
    protocol_id: str = PROTOCOL_ID,
    target_force_n: float = TARGET_FORCE_N,
) -> dict[str, Any]:
    """Score both complete and incomplete attempts without inventing coverage.

    Rows may use ``time_s``/``path_time_s`` and
    ``normal_force_n``/``filtered_normal_n``/``force_n``.  Metrics are
    calculated over observed rows in ``[5, 60)``.  ``complete`` is true only
    when all 550 bins are present and the observed interval reaches 60 s;
    incomplete or qualification rows still receive diagnostic metrics with
    explicit coverage and stage labels.
    """
    if protocol_id != PROTOCOL_ID:
        raise TaseFigure8ProtocolError("metric protocol identity differs")
    if not math.isfinite(float(target_force_n)):
        raise TaseFigure8ProtocolError("metric target is not finite")
    parsed: list[tuple[float, float]] = []
    for row in samples:
        t = _row_value(row, "path_time_s", "time_s", "elapsed_s", "timestamp_s")
        force = _row_value(row, "filtered_normal_n", "normal_force_n", "force_n", "normal_load_n")
        if t is None or force is None or not FORMAL_START_S <= t < FORMAL_END_S:
            continue
        parsed.append((t, force))
    parsed.sort(key=lambda pair: pair[0])
    # Add a tiny, derived binary64 boundary guard so a timestamp represented
    # as 5.1 does not land in the preceding bin because 0.1 is inexact.
    bins = {
        int(math.floor((t - FORMAL_START_S) / BIN_WIDTH_S + 1e-9))
        for t, _ in parsed
    }
    bins.intersection_update(range(REQUIRED_BINS))
    values = [force - target_force_n for _, force in parsed]
    if values:
        mae = sum(abs(value) for value in values) / len(values)
        rmse = math.sqrt(sum(value * value for value in values) / len(values))
        bias = sum(values) / len(values)
        sorted_abs = sorted(abs(value) for value in values)
        tail = sorted_abs[max(0, int(math.ceil(0.95 * len(sorted_abs))) - 1)]
        low_contact_fraction = sum(force < 1.0 for _, force in parsed) / len(parsed)
    else:
        mae = rmse = bias = tail = low_contact_fraction = None
    coverage_start = parsed[0][0] if parsed else None
    coverage_end = parsed[-1][0] if parsed else None
    coverage_s = (
        max(0.0, min(FORMAL_END_S, coverage_end + BIN_WIDTH_S) - max(FORMAL_START_S, coverage_start))
        if parsed else 0.0
    )
    complete = (
        stage == "PATH"
        and len(bins) == REQUIRED_BINS
        and coverage_end is not None
        and coverage_end + BIN_WIDTH_S >= FORMAL_END_S
    )
    return {
        "protocol_id": protocol_id,
        "stage": str(stage),
        "target_force_n": float(target_force_n),
        "formal_window_s": list(FORMAL_WINDOW_S),
        "metric_basis": "observed normal-force rows in [5,60), unweighted diagnostic samples",
        "sample_count": len(parsed),
        "complete": bool(complete),
        "complete_bins": len(bins),
        "required_bins": REQUIRED_BINS,
        "coverage_start_s": coverage_start,
        "coverage_end_s": coverage_end,
        "coverage_s": coverage_s,
        "mae_n": mae,
        "rmse_n": rmse,
        "bias_n": bias,
        "tail_abs_error_p95_n": tail,
        "low_contact_fraction_below_1n": low_contact_fraction,
    }


__all__ = [
    "BIN_WIDTH_S",
    "DURATION_S",
    "ENTRY_DURATION_S",
    "FORMAL_END_S",
    "FORMAL_START_S",
    "FORMAL_WINDOW_S",
    "Figure8Window60Task",
    "PROTOCOL_ID",
    "REQUIRED_BINS",
    "SCHEMA",
    "SEAM_CONTINUATION_S",
    "TaseFigure8ProtocolError",
    "protocol_manifest",
    "score_formal_samples",
]
