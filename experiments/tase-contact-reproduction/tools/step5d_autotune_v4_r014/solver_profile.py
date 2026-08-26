"""Typed, bounded and versioned strict-RNN solver selections."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .common import R014Error, sha256_value


SOLVER_PROFILE_SCHEMA = "step5d.autotuner-r014/solver-profile-v1"


@dataclass(frozen=True)
class SolverProfile:
    profile_id: str
    r: float
    epsilon: float
    iterations: int
    backend: str
    qdot_limit_rad_s: float
    preconstruct_outside_tick: bool

    def __post_init__(self) -> None:
        if self.profile_id not in {"finite-time-r08", "legacy-r1"}:
            raise R014Error("unknown strict-RNN solver profile")
        if not math.isfinite(self.r) or not 0.0 < self.r <= 1.0:
            raise R014Error("strict-RNN r must be in (0,1]")
        if not math.isfinite(self.epsilon) or self.epsilon <= 0.0:
            raise R014Error("strict-RNN epsilon must be positive")
        if type(self.iterations) is not int or self.iterations < 1:
            raise R014Error("strict-RNN iterations must be a positive integer")
        if self.backend not in {"numpy", "cupy"}:
            raise R014Error("strict-RNN backend must be numpy or cupy")
        if not math.isfinite(self.qdot_limit_rad_s) or not 0.0 < self.qdot_limit_rad_s <= 0.15:
            raise R014Error("strict-RNN qdot limit is outside (0,0.15]")
        exact = (
            self.r,
            self.epsilon,
            self.iterations,
            self.backend,
            self.qdot_limit_rad_s,
            self.preconstruct_outside_tick,
        )
        expected = (
            (0.8, 0.01, 512, "cupy", 0.05, True)
            if self.profile_id == "finite-time-r08"
            else (1.0, 0.022, 1, "numpy", 0.15, False)
        )
        if exact != expected:
            raise R014Error(f"solver profile fields differ from {self.profile_id}")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SolverProfile":
        return cls(
            profile_id=str(value.get("id", "")),
            r=float(value.get("r")),
            epsilon=float(value.get("epsilon")),
            iterations=int(value.get("iterations")),
            backend=str(value.get("backend", "")),
            qdot_limit_rad_s=float(value.get("qdot_limit_rad_s")),
            preconstruct_outside_tick=value.get("preconstruct_outside_tick") is True,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SOLVER_PROFILE_SCHEMA,
            "version": 1,
            "id": self.profile_id,
            "r": self.r,
            "epsilon": self.epsilon,
            "iterations": self.iterations,
            "backend": self.backend,
            "qdot_limit_rad_s": self.qdot_limit_rad_s,
            "preconstruct_outside_tick": self.preconstruct_outside_tick,
        }

    @property
    def sha256(self) -> str:
        return sha256_value(self.as_dict())


FINITE_TIME_R08 = SolverProfile(
    profile_id="finite-time-r08",
    r=0.8,
    epsilon=0.01,
    iterations=512,
    backend="cupy",
    qdot_limit_rad_s=0.05,
    preconstruct_outside_tick=True,
)
LEGACY_R1 = SolverProfile(
    profile_id="legacy-r1",
    r=1.0,
    epsilon=0.022,
    iterations=1,
    backend="numpy",
    qdot_limit_rad_s=0.15,
    preconstruct_outside_tick=False,
)


def strict_rnn_config(profile: SolverProfile, *, paper_truth_path: Any, motion_qdot_limit_rad_s: float) -> Any:
    """Create the existing solver config without allowing an implicit fallback."""

    if not isinstance(profile, SolverProfile):
        raise R014Error("strict_rnn_config requires a typed SolverProfile")
    motion_limit = float(motion_qdot_limit_rad_s)
    if not math.isfinite(motion_limit) or motion_limit <= 0.0:
        raise R014Error("motion qdot limit must be positive")
    from step5c_strict_rnn import StrictRnnConfig

    return StrictRnnConfig(
        paper_truth_path=paper_truth_path,
        qdot_limit_rad_s=min(motion_limit, profile.qdot_limit_rad_s),
        epsilon=profile.epsilon,
        sigr_exponent_r=profile.r,
        inner_iterations=profile.iterations,
        backend=profile.backend,
    )
