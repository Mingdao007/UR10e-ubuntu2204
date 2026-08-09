"""R011 binding for the production qLogNEI path, kept separate from theory TS."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .censor import CensoredObservation, ExactObservation, Observation, require_exact_for_legacy_gp, validate_observation
from .common import R011ValueError, digest, json_tree, require_digest


QLOGNEI_SCHEMA = "step5d.autotune-v4/r011-production-qlognei-binding-v1"


class QLogNEIReuseError(R011ValueError):
    """Production qLogNEI binding is invalid or received shadow-only data."""


@dataclass(frozen=True)
class ProductionQLogNEIBinding:
    reused_r010_component: str
    acquisition: str = "qLogNEI"
    pending_model: str = "production_qlognei"
    censored_observations_allowed: bool = False
    theory_shadow_separate: bool = True
    schema: str = QLOGNEI_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != QLOGNEI_SCHEMA or self.acquisition != "qLogNEI":
            raise QLogNEIReuseError("production acquisition must remain qLogNEI")
        if self.censored_observations_allowed or not self.theory_shadow_separate:
            raise QLogNEIReuseError("qLogNEI/shadow separation differs")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "reused_r010_component": self.reused_r010_component,
            "acquisition": self.acquisition,
            "pending_model": self.pending_model,
            "censored_observations_allowed": False,
            "theory_shadow_separate": True,
        }


def production_qlognei_observations(observations: Sequence[Observation | Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for observation in observations:
        row = validate_observation(observation)
        if isinstance(row, CensoredObservation):
            raise QLogNEIReuseError("censored observations are not exact qLogNEI training rows")
        rows.append(require_exact_for_legacy_gp(row).as_dict())
    return tuple(rows)


def validate_production_binding(value: Mapping[str, Any]) -> ProductionQLogNEIBinding:
    if not isinstance(value, Mapping):
        raise QLogNEIReuseError("qLogNEI binding must be an object")
    return ProductionQLogNEIBinding(
        reused_r010_component=value["reused_r010_component"],
        acquisition=value["acquisition"],
        pending_model=value["pending_model"],
        censored_observations_allowed=value["censored_observations_allowed"],
        theory_shadow_separate=value["theory_shadow_separate"],
        schema=value["schema"],
    )


__all__ = [
    "ProductionQLogNEIBinding", "QLOGNEI_SCHEMA", "QLogNEIReuseError",
    "production_qlognei_observations", "validate_production_binding",
]
