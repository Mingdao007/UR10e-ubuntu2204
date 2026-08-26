"""R013 candidate-to-bridge binding used by the live owner.

The binding is deliberately small: it describes the exact candidate and the
fixed anti-windup arguments that the live owner must publish.  Controller
upload/read-back and physical acceptance remain separate receipt gates; the
activation map below records that this binding is a live-capable route, not
that a particular controller session has already produced evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .campaign import ANTI_WINDUP_CONTRACT, candidate_token
from .domain import physical_candidate_key


RUNTIME_BINDING_SCHEMA = "step5d.autotune-v4/r013-bridge-binding-v1"


class R013RuntimeContractError(ValueError):
    """R013 bridge candidate or fixed anti-windup binding differs."""


@dataclass(frozen=True)
class BridgeBinding:
    candidate: Mapping[str, Any]
    physical_candidate_token: str
    integral_policy: str = "conditional-double-clamp-v1"
    integral_state_limit_n_s: float = 1.0
    i_term_authority_error_n: float = 0.5
    back_calculation: bool = False
    schema: str = RUNTIME_BINDING_SCHEMA

    def __post_init__(self) -> None:
        physical_candidate_key(self.candidate)
        if self.physical_candidate_token != candidate_token(self.candidate):
            raise R013RuntimeContractError("R013 bridge physical identity differs")
        if (
            self.schema != RUNTIME_BINDING_SCHEMA
            or self.integral_policy != "conditional-double-clamp-v1"
            or self.integral_state_limit_n_s != 1.0
            or self.i_term_authority_error_n != 0.5
            or self.back_calculation
        ):
            raise R013RuntimeContractError("R013 bridge anti-windup contract differs")

    @classmethod
    def from_candidate(cls, candidate: Mapping[str, Any]) -> "BridgeBinding":
        return cls(dict(candidate), candidate_token(candidate))

    def bridge_argv(self) -> tuple[str, ...]:
        candidate = self.candidate
        return (
            "--bridge-integral-policy", self.integral_policy,
            "--bridge-integral-limit-n-s", "1.0",
            "--step5d-autotune-force-p", repr(float(candidate["force_p_gain"])),
            "--step5d-autotune-force-i", repr(float(candidate["force_i_gain"])),
            "--step5d-autotune-force-damping", repr(float(candidate["force_damping"])),
            "--step5d-autotune-normal-filter-tau", repr(float(candidate["normal_filter_tau_s"])),
            "--step5d-autotune-orientation-ko", repr(float(candidate["orientation_ko"])),
            "--step5d-autotune-motion-kp", repr(float(candidate["motion_kp"])),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "candidate": dict(self.candidate),
            "physical_candidate_token": self.physical_candidate_token,
            "anti_windup": dict(ANTI_WINDUP_CONTRACT),
            "bridge_argv": list(self.bridge_argv()),
            "activation": {
                "network": True,
                "upload": True,
                "readback": True,
                "load": True,
                "play": True,
                "bridge": True,
                "motion": True,
                "contact": True,
            },
            "activation_boundary": "live_owner_receipts_required",
        }


def validate_bridge_binding(value: Mapping[str, Any]) -> BridgeBinding:
    if not isinstance(value, Mapping) or value.get("schema") != RUNTIME_BINDING_SCHEMA:
        raise R013RuntimeContractError("R013 bridge binding schema differs")
    candidate = value.get("candidate")
    if not isinstance(candidate, Mapping):
        raise R013RuntimeContractError("R013 bridge binding candidate is invalid")
    binding = BridgeBinding(dict(candidate), str(value.get("physical_candidate_token", "")))
    if binding.as_dict() != dict(value):
        raise R013RuntimeContractError("R013 bridge binding payload differs")
    return binding


__all__ = [
    "BridgeBinding", "R013RuntimeContractError", "RUNTIME_BINDING_SCHEMA",
    "validate_bridge_binding",
]
