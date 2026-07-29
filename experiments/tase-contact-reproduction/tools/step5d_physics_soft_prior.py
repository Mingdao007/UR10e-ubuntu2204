#!/usr/bin/env python3
"""Pure physics-informed soft-prior primitives for Step5d BO.

The prior is deliberately advisory: it changes candidate acquisition ranking
without changing the force-MAE objective, candidate feasibility, runtime
acceptance, or any controller safety gate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable, Mapping

from step5d_autotune_contract import ForceCandidate


@dataclass(frozen=True)
class PhysicsSoftPrior:
    """Gaussian log-weight around a target effective damping ratio."""

    enabled: bool = True
    contact_stiffness_n_m: float = 32_000.0
    target_damping_ratio: float = 1.0 / math.sqrt(2.0)
    log2_sigma_octaves: float = 2.0
    strength: float = 0.35
    target_motion_kp: float = 4.0
    motion_log2_sigma_octaves: float = 1.0
    motion_strength: float = 0.20

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("physics soft prior enabled must be boolean")
        for name in (
            "contact_stiffness_n_m",
            "target_damping_ratio",
            "log2_sigma_octaves",
            "strength",
            "target_motion_kp",
            "motion_log2_sigma_octaves",
            "motion_strength",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"physics soft prior {name} must be numeric")
            number = float(value)
            if not math.isfinite(number) or number <= 0.0:
                raise ValueError(
                    f"physics soft prior {name} must be finite and positive"
                )
            object.__setattr__(self, name, number)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any] | None) -> "PhysicsSoftPrior":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise ValueError("physics_soft_prior must be an object")
        allowed = {
            "enabled",
            "contact_stiffness_n_m",
            "target_damping_ratio",
            "log2_sigma_octaves",
            "strength",
            "target_motion_kp",
            "motion_log2_sigma_octaves",
            "motion_strength",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(
                "physics_soft_prior has unknown fields: " + ", ".join(sorted(unknown))
            )
        return cls(**dict(payload))

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def effective_damping_ratio(
    candidate: ForceCandidate,
    *,
    contact_stiffness_n_m: float,
) -> float:
    """Return zeta = D / (2*sqrt(P*K_e)) for the linearized force channel."""

    stiffness = float(contact_stiffness_n_m)
    if not math.isfinite(stiffness) or stiffness <= 0.0:
        raise ValueError("contact_stiffness_n_m must be finite and positive")
    return candidate.force_damping / (
        2.0 * math.sqrt(candidate.force_p_gain * stiffness)
    )


def physics_log_weight(
    candidate: ForceCandidate,
    prior: PhysicsSoftPrior,
) -> float:
    """Return an additive qLogNEI weight; zero means no preference.

    The finite Gaussian penalty is soft for every positive candidate.  Even a
    candidate far from the target remains selectable when its data-driven
    acquisition improvement is sufficiently large.
    """

    if not prior.enabled:
        return 0.0
    ratio = effective_damping_ratio(
        candidate,
        contact_stiffness_n_m=prior.contact_stiffness_n_m,
    )
    normalized = (
        math.log2(ratio / prior.target_damping_ratio)
        / prior.log2_sigma_octaves
    )
    motion_normalized = (
        math.log2(candidate.motion_kp / prior.target_motion_kp)
        / prior.motion_log2_sigma_octaves
    )
    return (
        -0.5 * prior.strength * normalized * normalized
        -0.5 * prior.motion_strength * motion_normalized * motion_normalized
    )


def joint_physics_log_weight(
    candidates: tuple[ForceCandidate, ...],
    prior: PhysicsSoftPrior,
) -> float:
    """Return the additive prior weight for one joint q-candidate set."""

    return sum(physics_log_weight(candidate, prior) for candidate in candidates)


def _bootstrap_lattice_distance(
    candidate: ForceCandidate,
    anchor: ForceCandidate,
) -> float:
    """Return a dimensionless log-lattice distance from a proven anchor."""

    continuous = (
        abs(candidate.log2_p - anchor.log2_p),
        abs(candidate.log2_damping - anchor.log2_damping),
        abs(candidate.log2_filter_tau - anchor.log2_filter_tau),
        abs(candidate.log2_orientation_ko - anchor.log2_orientation_ko),
        abs(candidate.log2_motion_kp - anchor.log2_motion_kp),
    )
    i_distance = 1.0
    if candidate.i_mode == anchor.i_mode:
        i_distance = (
            0.0
            if candidate.i_mode == "off"
            else abs(candidate.log2_i - anchor.log2_i)
        )
    return sum((*continuous, i_distance))


def rank_degraded_bootstrap_candidates(
    candidates: Iterable[ForceCandidate],
    prior: PhysicsSoftPrior,
    *,
    anchor: ForceCandidate | None = None,
) -> tuple[ForceCandidate, ...]:
    """Rank optimizer-free bootstrap rows locally, then by the soft prior.

    This is deliberately a producer policy, not an acceptance boundary.  A
    candidate at any finite positive damping remains eligible.  Distance from
    the proven anchor prevents a cold-start fallback from jumping across many
    log-lattice steps; the physics prior orders candidates within each equally
    local shell.
    """

    selected_anchor = anchor or ForceCandidate()
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                _bootstrap_lattice_distance(candidate, selected_anchor),
                -physics_log_weight(candidate, prior),
                candidate.candidate_uid,
            ),
        )
    )


__all__ = [
    "PhysicsSoftPrior",
    "effective_damping_ratio",
    "joint_physics_log_weight",
    "physics_log_weight",
    "rank_degraded_bootstrap_candidates",
]
