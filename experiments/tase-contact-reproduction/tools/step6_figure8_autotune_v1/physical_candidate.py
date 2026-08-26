"""Typed continuous-domain candidate accepted by the mature R006 control seam."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping

from step5d_autotune_v4_r006.lattice import IMode
from step5d_autotune_v4_r006.live_adapter import (
    R006Candidate,
    R006LiveAdapterError,
)
from step6_figure8_autotune_v1.v5_design_domain import (
    DAMPING_BOUNDS,
    I_OVER_P_BOUNDS,
    KO_BOUNDS,
    MOTION_KP_BOUNDS,
    P_OVER_D_BOUNDS,
    TAU_BOUNDS,
)

TARGET_FORCE_N = 5.0


def _inside(value: float, bounds: tuple[float, float], role: str) -> float:
    number = float(value)
    if (
        not math.isfinite(number)
        or number < bounds[0] - 1e-12
        or number > bounds[1] + 1e-12
    ):
        raise R006LiveAdapterError(
            f"Figure-eight {role} is outside [{bounds[0]},{bounds[1]}]"
        )
    return number


@dataclass(frozen=True)
class FigureEightPhysicalCandidateV1(R006Candidate):
    """R006-compatible DTO with the frozen Figure-eight transformed box.

    The mature control implementation only requires the physical fields and
    ``R006Candidate`` type capability.  Its historical quarter-octave graph is
    an optimizer policy, not a controller safety requirement, so this subtype
    replaces that graph with the independently bounded Figure-eight domain.
    """

    i_mode: IMode = IMode.ON

    def __post_init__(self) -> None:
        values = {
            "force_p_gain": self._finite_positive(self.force_p_gain, "P"),
            "force_i_gain": self._finite_positive(self.force_i_gain, "I"),
            "force_damping": self._finite_positive(self.force_damping, "D"),
            "normal_filter_tau_s": self._finite_positive(
                self.normal_filter_tau_s, "tau"
            ),
            "orientation_ko": self._finite_positive(self.orientation_ko, "Ko"),
            "motion_kp": self._finite_positive(self.motion_kp, "Kp"),
            "target_force_n": self._finite_positive(
                self.target_force_n, "target_force_n"
            ),
        }
        if self.i_mode is not IMode.ON:
            raise R006LiveAdapterError("Figure-eight campaign requires I-on")
        if not math.isclose(
            values["target_force_n"], TARGET_FORCE_N, rel_tol=0.0, abs_tol=1e-12
        ):
            raise R006LiveAdapterError("Figure-eight target force must remain 5 N")
        damping = _inside(
            values["force_damping"], DAMPING_BOUNDS, "force damping"
        )
        _inside(
            values["force_p_gain"] / damping,
            P_OVER_D_BOUNDS,
            "P/D",
        )
        _inside(values["normal_filter_tau_s"], TAU_BOUNDS, "tau")
        _inside(values["orientation_ko"], KO_BOUNDS, "Ko")
        _inside(values["motion_kp"], MOTION_KP_BOUNDS, "motion Kp")
        _inside(
            values["force_i_gain"] / values["force_p_gain"],
            I_OVER_P_BOUNDS,
            "I/P",
        )
        for name, value in values.items():
            object.__setattr__(self, name, value)

    @classmethod
    def from_canonical(
        cls, payload: Mapping[str, Any]
    ) -> "FigureEightPhysicalCandidateV1":
        required = {
            "force_p_gain",
            "force_i_gain",
            "force_damping",
            "normal_filter_tau_s",
            "orientation_ko",
            "motion_kp",
            "target_force_n",
            "i_off",
        }
        if not isinstance(payload, Mapping) or set(payload) != required:
            raise R006LiveAdapterError(
                "Figure-eight physical candidate canonical fields differ"
            )
        if payload.get("i_off") is not False:
            raise R006LiveAdapterError("Figure-eight physical candidate requires i_off=false")
        return cls(
            force_p_gain=payload["force_p_gain"],
            force_i_gain=payload["force_i_gain"],
            force_damping=payload["force_damping"],
            normal_filter_tau_s=payload["normal_filter_tau_s"],
            orientation_ko=payload["orientation_ko"],
            motion_kp=payload["motion_kp"],
            target_force_n=payload["target_force_n"],
            i_mode=IMode.ON,
        )

    @property
    def as_point(self) -> Any:
        raise R006LiveAdapterError(
            "Figure-eight continuous candidate has no historical R006 lattice point"
        )

    @property
    def candidate_uid(self) -> str:
        encoded = json.dumps(
            self.canonical,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def physical_candidate_domain_receipt() -> dict[str, Any]:
    return {
        "schema": "step6.autotune/figure8-physical-candidate-domain-v1",
        "version": 1,
        "p_over_d": list(P_OVER_D_BOUNDS),
        "damping": list(DAMPING_BOUNDS),
        "tau_s": list(TAU_BOUNDS),
        "orientation_ko": list(KO_BOUNDS),
        "motion_kp": list(MOTION_KP_BOUNDS),
        "i_over_p": list(I_OVER_P_BOUNDS),
        "target_force_n": TARGET_FORCE_N,
        "i_off": False,
        "historical_r006_quarter_octave_graph_applies": False,
    }


__all__ = [
    "DAMPING_BOUNDS",
    "I_OVER_P_BOUNDS",
    "KO_BOUNDS",
    "MOTION_KP_BOUNDS",
    "P_OVER_D_BOUNDS",
    "TAU_BOUNDS",
    "FigureEightPhysicalCandidateV1",
    "physical_candidate_domain_receipt",
]
