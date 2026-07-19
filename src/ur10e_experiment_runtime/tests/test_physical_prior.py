from __future__ import annotations

import math

from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR


def test_step5d_v3_prior_is_self_consistent_and_stable() -> None:
    prior = STEP5D_V3_PHYSICAL_PRIOR
    assert prior.reaction_normal_b == (-0.043955267, 0.020079909, 0.998831683)
    assert prior.approach_axis_b == (0.043955267, -0.020079909, -0.998831683)
    assert prior.precontact_rotvec_rad == (3.120752062, 0.0, 0.068626833)
    assert prior.load_gate_n == 8.0
    assert prior.load_gate_dwell_s == 0.10
    assert prior.normal_rate_limit_rad_s == 0.05
    assert prior.fingerprint == (
        "c8019aee2c293746e1edb23097aeab1d7dfb1b8dee09df10ce568fb634f47c9f"
    )
    legacy_gravity_down = (0.0, 0.0, -1.0)
    separation = math.acos(
        max(
            -1.0,
            min(
                1.0,
                sum(
                    value * target
                    for value, target in zip(
                        prior.approach_axis_b,
                        legacy_gravity_down,
                        strict=True,
                    )
                ),
            ),
        )
    )
    assert separation > math.radians(2.0)
