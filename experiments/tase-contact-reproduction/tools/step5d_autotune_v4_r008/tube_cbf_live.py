"""Live PATH-only Tube CBF-QP seam (default OFF).

Hooks after ``desired_twist`` and before ``command()`` in r004 qualification.
Does not touch seal / HOME / search / PATH60. Soft layer only; HardTube is
separate (host-side).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from step5d_autotune_v4_r004.path_reference import (
    PATH_STAGE_ID,
    R004_PATH_FRAME_SNAPSHOT,
    step5_path_reference,
)
from step5d_autotune_v4_r008.tube_cbf import (
    DEFAULT_ALPHA_S,
    DEFAULT_ENGAGE_RHO,
    TubeCbfQp,
    TubeCbfResult,
)

ENV_MODE = "R008_TUBE_CBF_MODE"
DEFAULT_SOFT_A_M = 0.035
DEFAULT_SOFT_B_M = 0.035
_MODE_OFF = "off"
_MODE_SHADOW = "shadow"
_MODE_ACTIVE = "active"
_VALID_MODES = frozenset({_MODE_OFF, _MODE_SHADOW, _MODE_ACTIVE})


@dataclass(frozen=True, slots=True)
class TubeCbfLiveConfig:
    mode: str = _MODE_OFF
    a_m: float = DEFAULT_SOFT_A_M
    b_m: float = DEFAULT_SOFT_B_M
    alpha: float = DEFAULT_ALPHA_S
    engage_rho: float = DEFAULT_ENGAGE_RHO

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> TubeCbfLiveConfig:
        env = os.environ if environ is None else environ
        raw = str(env.get(ENV_MODE, _MODE_OFF)).strip().lower()
        mode = raw if raw in _VALID_MODES else _MODE_OFF
        return cls(mode=mode)

    @property
    def armed(self) -> bool:
        return self.mode in {_MODE_SHADOW, _MODE_ACTIVE}


@dataclass(frozen=True, slots=True)
class TubeCbfLiveOutcome:
    mode: str
    applied: bool
    desired_twist: tuple[float, float, float, float, float, float]
    result: TubeCbfResult | None
    u_m: float
    v_m: float

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "applied": self.applied,
            "u_m": self.u_m,
            "v_m": self.v_m,
        }
        if self.result is None:
            out["soft"] = None
            return out
        out["soft"] = {
            "fired": self.result.fired,
            "infeasible": self.result.infeasible,
            "engaged": self.result.engaged,
            "h": self.result.h,
            "phi": self.result.phi,
            "rho": self.result.rho,
            "lambda_step": self.result.lambda_step,
            "constraint_nom": self.result.constraint_nom,
        }
        return out


def path_frame_uv(
    pose_xy: Sequence[float],
    path_time_s: float,
) -> tuple[float, float]:
    """PATH-frame (u along, v lateral) offset of TCP from the cycloid reference."""

    ref = step5_path_reference(
        PATH_STAGE_ID,
        (float(pose_xy[0]), float(pose_xy[1])),
        float(path_time_s),
    )
    err = ref["path_error_xy"]  # desired - pose (base XY)
    dx = -float(err[0])
    dy = -float(err[1])
    u_along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    p_lat = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]
    u = dx * float(u_along[0]) + dy * float(u_along[1])
    v = dx * float(p_lat[0]) + dy * float(p_lat[1])
    return (u, v)


def _base_twist_to_path(vx: float, vy: float) -> tuple[float, float]:
    u_along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    p_lat = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]
    vu = vx * float(u_along[0]) + vy * float(u_along[1])
    vv = vx * float(p_lat[0]) + vy * float(p_lat[1])
    return (vu, vv)


def _path_twist_to_base(vu: float, vv: float) -> tuple[float, float]:
    u_along = R004_PATH_FRAME_SNAPSHOT["basis"]["u_along_xy"]
    p_lat = R004_PATH_FRAME_SNAPSHOT["basis"]["p_lateral_xy"]
    vx = vu * float(u_along[0]) + vv * float(p_lat[0])
    vy = vu * float(u_along[1]) + vv * float(p_lat[1])
    return (vx, vy)


@dataclass
class TubeCbfLiveFilter:
    """Stateful PATH CBF filter; no-op when mode is off or non-path."""

    config: TubeCbfLiveConfig
    _qp: TubeCbfQp | None = None

    def __post_init__(self) -> None:
        if self.config.armed:
            self._qp = TubeCbfQp.from_semi_axes(self.config.a_m, self.config.b_m)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> TubeCbfLiveFilter:
        return cls(config=TubeCbfLiveConfig.from_environ(environ))

    def apply(
        self,
        desired_twist: Sequence[float],
        *,
        mode: str,
        actual_tcp_pose: Sequence[float],
        path_time_s: float,
    ) -> TubeCbfLiveOutcome:
        twist6 = tuple(float(desired_twist[i]) for i in range(6))
        if not self.config.armed or str(mode).strip().lower() != "path":
            return TubeCbfLiveOutcome(
                mode=self.config.mode,
                applied=False,
                desired_twist=twist6,  # type: ignore[arg-type]
                result=None,
                u_m=0.0,
                v_m=0.0,
            )
        assert self._qp is not None
        if len(actual_tcp_pose) < 2 or not math.isfinite(float(path_time_s)):
            return TubeCbfLiveOutcome(
                mode=self.config.mode,
                applied=False,
                desired_twist=twist6,  # type: ignore[arg-type]
                result=None,
                u_m=0.0,
                v_m=0.0,
            )
        u_m, v_m = path_frame_uv(actual_tcp_pose[:2], path_time_s)
        vu, vv = _base_twist_to_path(twist6[0], twist6[1])
        result = self._qp.filter_twist_xy(
            (vu, vv, twist6[2]),
            (u_m, v_m),
            alpha=self.config.alpha,
            engage_rho=self.config.engage_rho,
        )
        if self.config.mode == _MODE_SHADOW or result.infeasible:
            return TubeCbfLiveOutcome(
                mode=self.config.mode,
                applied=False,
                desired_twist=twist6,  # type: ignore[arg-type]
                result=result,
                u_m=u_m,
                v_m=v_m,
            )
        vx, vy = _path_twist_to_base(result.v_xyz[0], result.v_xyz[1])
        out = (vx, vy, twist6[2], twist6[3], twist6[4], twist6[5])
        return TubeCbfLiveOutcome(
            mode=self.config.mode,
            applied=bool(result.fired),
            desired_twist=out,  # type: ignore[arg-type]
            result=result,
            u_m=u_m,
            v_m=v_m,
        )


__all__ = [
    "DEFAULT_SOFT_A_M",
    "DEFAULT_SOFT_B_M",
    "ENV_MODE",
    "TubeCbfLiveConfig",
    "TubeCbfLiveFilter",
    "TubeCbfLiveOutcome",
    "path_frame_uv",
]
