"""Offline contract for the isolated V4 I-saturation/feedforward branch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


V4_ISATURATION_SCHEMA = "step6.autotune/v4-isaturation-validation-plan-v1"


class V4ISaturationPlanError(ValueError):
    """The isolated V4 comparison plan is not bounded or identity-safe."""


@dataclass(frozen=True)
class V4ISaturationValidationPlanV1:
    candidate_n: int = 5
    correction_weights: tuple[float, ...] = (0.0,) * 6
    i_off_label: str = "I_OFF"
    i_on_label: str = "I_ON"
    feedforward_off_label: str = "FEEDFORWARD_OFF"
    feedforward_on_label: str = "FEEDFORWARD_ON"
    phase_target_intervention_label: str = "PHASE_TARGET_CORRECTION_SEPARATE"
    primary_floor_campaign_n: int = 200
    schema: str = V4_ISATURATION_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != V4_ISATURATION_SCHEMA or self.version != 1:
            raise V4ISaturationPlanError("V4 I-saturation plan schema/version differs")
        if self.candidate_n != 5 or self.primary_floor_campaign_n != 200:
            raise V4ISaturationPlanError("V4 I-saturation repeat/campaign count differs")
        if tuple(float(value) for value in self.correction_weights) != (0.0,) * 6:
            raise V4ISaturationPlanError("V4 I-saturation plan must use zero correction")
        labels = (
            self.i_off_label,
            self.i_on_label,
            self.feedforward_off_label,
            self.feedforward_on_label,
            self.phase_target_intervention_label,
        )
        if any(not isinstance(label, str) or not label for label in labels):
            raise V4ISaturationPlanError("V4 I-saturation labels are incomplete")
        if len(set(labels)) != len(labels):
            raise V4ISaturationPlanError("V4 I-saturation identities must be distinct")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "candidate_n": self.candidate_n,
            "correction_weights": list(self.correction_weights),
            "comparisons": {
                "i_saturation": [self.i_off_label, self.i_on_label],
                "path_velocity_feedforward": [
                    self.feedforward_off_label,
                    self.feedforward_on_label,
                ],
                "phase_target_intervention": self.phase_target_intervention_label,
            },
            "primary_floor_campaign_n": self.primary_floor_campaign_n,
            "automatic_promotion": False,
            "live_capability_required_before_start": True,
        }


def load_v4_isaturation_plan(raw: Mapping[str, Any]) -> V4ISaturationValidationPlanV1:
    if not isinstance(raw, Mapping):
        raise V4ISaturationPlanError("V4 I-saturation plan is not an object")
    comparisons = raw.get("comparisons")
    if not isinstance(comparisons, Mapping):
        raise V4ISaturationPlanError("V4 I-saturation comparisons are missing")
    i_saturation = tuple(comparisons.get("i_saturation", ()))
    feedforward = tuple(comparisons.get("path_velocity_feedforward", ()))
    return V4ISaturationValidationPlanV1(
        candidate_n=raw.get("candidate_n"),
        correction_weights=tuple(raw.get("correction_weights", ())),
        i_off_label=i_saturation[0] if len(i_saturation) == 2 else "",
        i_on_label=i_saturation[1] if len(i_saturation) == 2 else "",
        feedforward_off_label=feedforward[0] if len(feedforward) == 2 else "",
        feedforward_on_label=feedforward[1] if len(feedforward) == 2 else "",
        phase_target_intervention_label=str(comparisons.get("phase_target_intervention", "")),
        primary_floor_campaign_n=raw.get("primary_floor_campaign_n"),
        schema=raw.get("schema"),
        version=raw.get("version"),
    )


__all__ = [
    "V4_ISATURATION_SCHEMA",
    "V4ISaturationPlanError",
    "V4ISaturationValidationPlanV1",
    "load_v4_isaturation_plan",
]
