from __future__ import annotations

import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_physics_soft_prior import (  # noqa: E402
    PhysicsSoftPrior,
    effective_damping_ratio,
    joint_physics_log_weight,
    physics_log_weight,
    rank_degraded_bootstrap_candidates,
)


def test_default_seed_matches_butterworth_ratio_at_implied_stiffness() -> None:
    candidate = ForceCandidate()
    implied_stiffness = candidate.force_damping**2 / (
        2.0 * candidate.force_p_gain
    )
    zeta = effective_damping_ratio(
        candidate,
        contact_stiffness_n_m=implied_stiffness,
    )
    assert zeta == pytest.approx(1.0 / math.sqrt(2.0))


def test_prior_is_soft_finite_and_prefers_target_ratio() -> None:
    prior = PhysicsSoftPrior(contact_stiffness_n_m=24_500.0)
    target = ForceCandidate()
    near_motion_target = ForceCandidate.from_log2(
        p=0.0,
        damping=0.0,
        motion=1.5,
    )
    low_damping = ForceCandidate.from_log2(
        p=0.0,
        damping=-6.0,
        i=0.0,
    )

    assert physics_log_weight(near_motion_target, prior) > physics_log_weight(
        target, prior
    )
    assert math.isfinite(physics_log_weight(low_damping, prior))
    assert physics_log_weight(low_damping, prior) < 0.0


def test_disabled_prior_has_no_ranking_effect() -> None:
    prior = PhysicsSoftPrior(enabled=False)
    candidates = (
        ForceCandidate(),
        ForceCandidate.from_log2(p=0.0, damping=-6.0, i=0.0),
    )
    assert joint_physics_log_weight(candidates, prior) == 0.0


def test_prior_mapping_rejects_unknown_or_nonpositive_values() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        PhysicsSoftPrior.from_mapping({"mystery": 1})
    with pytest.raises(ValueError, match="finite and positive"):
        PhysicsSoftPrior.from_mapping({"strength": 0.0})


def test_degraded_bootstrap_starts_at_baseline_then_uses_prior_within_local_shell(
) -> None:
    prior = PhysicsSoftPrior(contact_stiffness_n_m=32_000.0)
    anchor = ForceCandidate()
    local_p_up = ForceCandidate.from_log2(p=0.25, damping=0.0, i=0.0)
    local_d_up = ForceCandidate.from_log2(p=0.0, damping=0.25, i=0.0)
    far_aggressive = ForceCandidate.from_log2(p=-0.75, damping=-2.0, i=0.0)

    ranked = rank_degraded_bootstrap_candidates(
        (far_aggressive, local_p_up, anchor, local_d_up),
        prior,
    )

    assert ranked[0] == anchor
    assert ranked[-1] == far_aggressive
    assert physics_log_weight(ranked[1], prior) >= physics_log_weight(
        ranked[2], prior
    )
