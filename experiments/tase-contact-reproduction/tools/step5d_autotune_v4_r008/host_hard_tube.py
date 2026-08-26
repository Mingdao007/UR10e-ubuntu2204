"""Host-side HardTubeGuard for B3 LiveR004Writer (default OFF).

B3 live does not run through V3 ``apply_step5d_hard_tube_guard``. Progress is
host-owned ``path_time_s`` (fresh by construction); no TP float31 required.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ur10e_experiment_runtime.hard_tube import (
    HARD_TUBE_EVALUATION_DIVISOR,
    HARD_TUBE_RADIUS_M,
    HardTubeGuard,
    HardTubeReason,
    HardTubeResult,
)
from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from ur10e_experiment_runtime.stage_adapters import (
    ACTIVE_STAGE25_CODE,
    Stage25ControllerProgressAdapter,
    TRAJECTORY_PARAMETERS,
)

ENV_FLAG = "R008_HOST_HARD_TUBE"
# Launch-profile moving_sphere_reference_sha256 must match this adapter.
EXPECTED_REFERENCE_SHA256 = "dd5f065a5cecc04098865dda08891e11bfa8c6c11f40bbd964d1b1dcb2b60b86"
PATH_END_OVERRUN_ALLOWANCE_S = 0.020


def env_flag_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_FLAG, "0")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _finite_or_none(value: float | None) -> float | None:
    """JSON-safe float: never emit NaN/Inf (PathEvidence finalize uses allow_nan=False)."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True, slots=True)
class HostHardTubeDecision:
    enabled: bool
    stop: bool
    reason: str
    actual_distance_m: float | None
    remaining_margin_m: float | None
    path_time_s: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "stop": self.stop,
            "reason": self.reason,
            "actual_distance_m": _finite_or_none(self.actual_distance_m),
            "remaining_margin_m": _finite_or_none(self.remaining_margin_m),
            "path_time_s": _finite_or_none(self.path_time_s),
        }


@dataclass
class HostHardTubeGuard:
    """Wrap Stage25 progress adapter + HardTubeGuard for host PATH ticks."""

    enabled: bool
    adapter: Stage25ControllerProgressAdapter | None = None
    guard: HardTubeGuard | None = None
    _tick_seq: int = 0

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        force_enabled: bool | None = None,
    ) -> HostHardTubeGuard:
        enabled = (
            bool(force_enabled)
            if force_enabled is not None
            else env_flag_enabled(environ)
        )
        if not enabled:
            return cls(enabled=False)
        adapter = Stage25ControllerProgressAdapter(
            physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint,
            allow_tick_gaps=True,
        )
        if adapter.reference_sha256 != EXPECTED_REFERENCE_SHA256:
            raise RuntimeError(
                "host HardTube reference SHA differs from launch profile "
                f"(got {adapter.reference_sha256}, expected {EXPECTED_REFERENCE_SHA256})"
            )
        guard = HardTubeGuard(
            reference_sha256=adapter.reference_sha256,
            radius_m=HARD_TUBE_RADIUS_M,
            evaluation_divisor=HARD_TUBE_EVALUATION_DIVISOR,
        )
        return cls(enabled=True, adapter=adapter, guard=guard)

    def reset(self) -> None:
        self._tick_seq = 0
        if self.adapter is not None:
            self.adapter.reset()
        if self.guard is not None:
            self.guard.reset()

    def evaluate(
        self,
        *,
        tp_state: int,
        path_time_s: float | None,
        tcp_pose_m_rad: Sequence[float],
        monotonic_ns: int | None = None,
        controller_timestamp_s: float | None = None,
    ) -> HostHardTubeDecision:
        if not self.enabled or self.adapter is None or self.guard is None:
            return HostHardTubeDecision(
                enabled=False,
                stop=False,
                reason="disabled",
                actual_distance_m=None,
                remaining_margin_m=None,
                path_time_s=path_time_s,
            )
        # Inactive outside PATH state 25 — never invent progress from baseline.
        if int(tp_state) != 25 or path_time_s is None:
            progress = self.adapter.sample(
                stage=20.0,
                controller_progress_s=None,
                controller_tick_seq=None,
                controller_timestamp_s=None,
                age_ns=-1,
                tcp_z_m=float(tcp_pose_m_rad[2]) if len(tcp_pose_m_rad) > 2 else None,
                tp_protocol_state=None,
            )
            result = self.guard.tick(
                progress=progress,
                tcp_base=tcp_pose_m_rad,
                observed_monotonic_ns=monotonic_ns,
            )
            return _decision(result, path_time_s=path_time_s)

        self._tick_seq += 1
        mono_ns = (
            monotonic_ns
            if isinstance(monotonic_ns, int) and monotonic_ns > 0
            else time.monotonic_ns()
        )
        # Host-owned progress: age_ns=0 (fresh). tick_seq monotonic via allow_tick_gaps.
        # The frozen centerline is defined through exactly duration_s.  A
        # small RTDE/host clock skew can expose state25 just after that
        # endpoint; project only that bounded handoff interval to the finite
        # endpoint and keep the same HardTubeGuard active.
        raw_path_time_s = float(path_time_s)
        duration_s = float(TRAJECTORY_PARAMETERS["duration_s"])
        path_overrun_s = (
            max(0.0, raw_path_time_s - duration_s)
            if math.isfinite(raw_path_time_s)
            else math.inf
        )
        effective_path_time_s = min(raw_path_time_s, duration_s)
        ts_s = (
            float(controller_timestamp_s)
            if controller_timestamp_s is not None and math.isfinite(float(controller_timestamp_s))
            else mono_ns * 1e-9
        )
        progress = self.adapter.sample(
            stage=ACTIVE_STAGE25_CODE,
            controller_progress_s=effective_path_time_s,
            controller_tick_seq=self._tick_seq,
            controller_timestamp_s=ts_s,
            age_ns=0,
            tcp_z_m=float(tcp_pose_m_rad[2]),
            tp_protocol_state=None,
        )
        result = self.guard.tick(
            progress=progress,
            tcp_base=tcp_pose_m_rad,
            observed_monotonic_ns=mono_ns,
        )
        decision = _decision(result, path_time_s=path_time_s)
        if path_overrun_s > 0.0 and not result.stop:
            if path_overrun_s > PATH_END_OVERRUN_ALLOWANCE_S:
                return HostHardTubeDecision(
                    enabled=True,
                    stop=True,
                    reason="PATH_END_OVERRUN",
                    actual_distance_m=decision.actual_distance_m,
                    remaining_margin_m=decision.remaining_margin_m,
                    path_time_s=path_time_s,
                )
            return HostHardTubeDecision(
                enabled=True,
                stop=False,
                reason="PATH_END_OVERRUN_PENDING",
                actual_distance_m=decision.actual_distance_m,
                remaining_margin_m=decision.remaining_margin_m,
                path_time_s=path_time_s,
            )
        return decision


def _decision(result: HardTubeResult, *, path_time_s: float | None) -> HostHardTubeDecision:
    reason = result.reason.name if isinstance(result.reason, HardTubeReason) else str(result.reason)
    return HostHardTubeDecision(
        enabled=True,
        stop=bool(result.stop),
        reason=reason,
        actual_distance_m=_finite_or_none(float(result.actual_distance_m)),
        remaining_margin_m=_finite_or_none(float(result.remaining_margin_m)),
        path_time_s=path_time_s,
    )


__all__ = [
    "ENV_FLAG",
    "EXPECTED_REFERENCE_SHA256",
    "PATH_END_OVERRUN_ALLOWANCE_S",
    "HostHardTubeDecision",
    "HostHardTubeGuard",
    "env_flag_enabled",
]
