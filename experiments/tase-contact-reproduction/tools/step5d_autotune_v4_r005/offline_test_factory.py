"""Explicit synthetic objective factory for offline tests only.

Production adapters must construct :class:`ForceObjective` through
``ForceObjectiveBuilder`` from raw PATH samples.  Keeping this factory in an
offline-named module makes test fixtures unable to masquerade as production
scalar objective input.
"""

from __future__ import annotations

from step5d_force_objective import (
    ForceObjective,
    ForceObjectiveBuilder,
    ForcePathSample,
)


def synthetic_force_objective(mae_n: float) -> ForceObjective:
    """Return a sealed but explicitly non-production fixture.

    The fixture exercises serialization and host-loop failure handling.  Its
    provenance is intentionally not trainable, so it cannot cross an
    ObservationRecord or optimizer eligibility boundary.
    """

    value = float(mae_n)
    if value < 0.0 or value != value or value in {float("inf"), float("-inf")}:
        raise ValueError("synthetic objective must be finite and nonnegative")
    builder = ForceObjectiveBuilder()
    for index in range(600):
        builder.add(
            ForcePathSample(
                path_time_s=index / 10.0,
                path_phase=25,
                filtered_normal_n=5.0 + value,
                source_sequences={"offline": index + 1},
                source_ages_s={"offline": 0.0},
            )
        )
    return builder.finalize(provenance="offline_synthetic_test")


__all__ = ["synthetic_force_objective"]
