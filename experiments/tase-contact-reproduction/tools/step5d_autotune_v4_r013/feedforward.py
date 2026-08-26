"""Typed feedforward profiles for the R013 demo A/B comparison.

The demo deliberately exposes this as a run-level choice.  It is not an
optimizer parameter and it must not change while a PATH segment is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


FEEDFORWARD_SCHEMA = "step5d.autotune-v4/r013-feedforward-profile-v1"


class FeedforwardMode(str, Enum):
    """Whether the path velocity feedforward is part of the runtime."""

    ON = "on"
    OFF = "off"

    @property
    def enabled(self) -> bool:
        return self is FeedforwardMode.ON


def parse_feedforward_mode(value: Any = None) -> FeedforwardMode:
    """Parse one explicit mode; omitted input preserves the historical ON default."""

    if value is None:
        return FeedforwardMode.ON
    if isinstance(value, FeedforwardMode):
        return value
    if type(value) is not str:
        raise ValueError("feedforward mode must be exactly 'on' or 'off'")
    normalized = value.strip().lower()
    try:
        return FeedforwardMode(normalized)
    except ValueError as exc:
        raise ValueError(
            f"unsupported feedforward mode {value!r}; expected 'on' or 'off'"
        ) from exc


@dataclass(frozen=True)
class FeedforwardProfile:
    """Immutable run identity for the two comparable demo limbs."""

    mode: FeedforwardMode

    @classmethod
    def from_value(cls, value: Any = None) -> "FeedforwardProfile":
        return cls(parse_feedforward_mode(value))

    @property
    def enabled(self) -> bool:
        return self.mode.enabled

    @property
    def profile_id(self) -> str:
        return "r013-feedforward-on-v1" if self.enabled else "r013-feedforward-off-v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": FEEDFORWARD_SCHEMA,
            "profile_id": self.profile_id,
            "mode": self.mode.value,
            "enabled": self.enabled,
            "same_candidate": True,
            "same_path_and_safety": True,
            "disabled_terms": []
            if self.enabled
            else [
                "path_error_desired_velocity_xy_over_motion_kp",
                "outer_loop_xdot_pd_base_desired_velocity_xy",
            ],
            "comparison_role": "historical_demo_ab_only_not_optimizer_dimension",
        }


__all__ = [
    "FEEDFORWARD_SCHEMA",
    "FeedforwardMode",
    "FeedforwardProfile",
    "parse_feedforward_mode",
]

__all__.append("MotionAdmissionProfileV1")


def __getattr__(name: str) -> Any:
    if name == "MotionAdmissionProfileV1":
        from .baseline_policy import MotionAdmissionProfileV1

        return MotionAdmissionProfileV1
    raise AttributeError(name)
