"""Deterministic, receipt-backed kernel freeze gate for V5.

The first 24 exact global observations are the only calibration view.  A
challenger kernel is admissible only when every declared replay metric is no
worse than the incumbent and NLPD improves strictly.  This helper does not
fit a model or make a statistical claim; it validates the externally produced
fit/replay receipt before a live worker is allowed to select a challenger.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


KERNEL_SELECTION_SCHEMA = "step6.autotune/figure8-v5-kernel-selection-v1"
KERNEL_SELECTION_VERSION = 1
CALIBRATION_OBSERVATION_COUNT = 24
INCUMBENT_KERNEL = "matern52_ard"
CHALLENGER_KERNELS = ("rbf_ard", "matern32_ard")
METRIC_NAMES = ("nlpd", "coverage", "low_tail_recall", "false_optimism", "replay_regret")


class KernelSelectionError(ValueError):
    """A kernel-selection receipt is incomplete or violates the freeze gate."""


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise KernelSelectionError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise KernelSelectionError(f"{name} must be finite") from exc
    if not math.isfinite(parsed):
        raise KernelSelectionError(f"{name} must be finite")
    return parsed


@dataclass(frozen=True)
class KernelSelectionReceiptV1:
    selected_kernel: str
    calibration_observation_count: int
    incumbent_metrics: Mapping[str, float]
    challenger_metrics: Mapping[str, Mapping[str, float]]
    decision: str
    source: str = "offline_replay"
    schema: str = KERNEL_SELECTION_SCHEMA
    version: int = KERNEL_SELECTION_VERSION

    def __post_init__(self) -> None:
        if self.schema != KERNEL_SELECTION_SCHEMA or self.version != KERNEL_SELECTION_VERSION:
            raise KernelSelectionError("kernel-selection schema/version differs")
        if self.calibration_observation_count != CALIBRATION_OBSERVATION_COUNT:
            raise KernelSelectionError("kernel selection requires exactly 24 observations")
        if self.selected_kernel not in (INCUMBENT_KERNEL, *CHALLENGER_KERNELS):
            raise KernelSelectionError("unknown selected kernel")
        if self.decision not in {"retain_incumbent", "freeze_challenger"}:
            raise KernelSelectionError("unknown kernel selection decision")
        incumbent = _metric_map(self.incumbent_metrics, "incumbent")
        challengers = {
            str(name): _metric_map(metrics, f"challenger:{name}")
            for name, metrics in self.challenger_metrics.items()
        }
        if set(challengers) != set(CHALLENGER_KERNELS):
            raise KernelSelectionError("challenger metric set differs")
        if self.selected_kernel == INCUMBENT_KERNEL and self.decision != "retain_incumbent":
            raise KernelSelectionError("incumbent selection decision differs")
        if self.selected_kernel != INCUMBENT_KERNEL and self.decision != "freeze_challenger":
            raise KernelSelectionError("challenger selection decision differs")
        object.__setattr__(self, "incumbent_metrics", incumbent)
        object.__setattr__(self, "challenger_metrics", challengers)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "selected_kernel": self.selected_kernel,
            "calibration_observation_count": self.calibration_observation_count,
            "incumbent_metrics": dict(self.incumbent_metrics),
            "challenger_metrics": {
                name: dict(metrics) for name, metrics in self.challenger_metrics.items()
            },
            "decision": self.decision,
            "source": self.source,
        }


def _metric_map(value: Mapping[str, Any], role: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != set(METRIC_NAMES):
        raise KernelSelectionError(f"{role} metric names differ")
    result = {name: _finite(value[name], f"{role}:{name}") for name in METRIC_NAMES}
    if result["coverage"] < 0.0 or result["coverage"] > 1.0:
        raise KernelSelectionError(f"{role}:coverage is outside [0,1]")
    if result["low_tail_recall"] < 0.0 or result["low_tail_recall"] > 1.0:
        raise KernelSelectionError(f"{role}:low_tail_recall is outside [0,1]")
    return result


def select_kernel(
    *,
    incumbent_metrics: Mapping[str, Any],
    challenger_metrics: Mapping[str, Mapping[str, Any]],
    calibration_observation_count: int = CALIBRATION_OBSERVATION_COUNT,
    source: str = "offline_replay",
) -> KernelSelectionReceiptV1:
    """Apply the strict challenger gate and return the frozen kernel receipt."""

    incumbent = _metric_map(incumbent_metrics, "incumbent")
    challengers = {
        str(name): _metric_map(metrics, f"challenger:{name}")
        for name, metrics in challenger_metrics.items()
    }
    if set(challengers) != set(CHALLENGER_KERNELS):
        raise KernelSelectionError("challenger metric set differs")
    selected = INCUMBENT_KERNEL
    decision = "retain_incumbent"
    # Lower is better for NLPD, false optimism, and replay regret.  Higher is
    # better for coverage and low-tail recall; equality is admissible.
    for name in CHALLENGER_KERNELS:
        candidate = challengers[name]
        improves_nlpd = candidate["nlpd"] < incumbent["nlpd"]
        no_worse = (
            candidate["coverage"] >= incumbent["coverage"]
            and candidate["low_tail_recall"] >= incumbent["low_tail_recall"]
            and candidate["false_optimism"] <= incumbent["false_optimism"]
            and candidate["replay_regret"] <= incumbent["replay_regret"]
        )
        if improves_nlpd and no_worse:
            if selected == INCUMBENT_KERNEL or candidate["nlpd"] < challengers[selected]["nlpd"]:
                selected = name
                decision = "freeze_challenger"
    return KernelSelectionReceiptV1(
        selected_kernel=selected,
        calibration_observation_count=calibration_observation_count,
        incumbent_metrics=incumbent,
        challenger_metrics=challengers,
        decision=decision,
        source=str(source),
    )


__all__ = [
    "CALIBRATION_OBSERVATION_COUNT",
    "CHALLENGER_KERNELS",
    "INCUMBENT_KERNEL",
    "KERNEL_SELECTION_SCHEMA",
    "KernelSelectionError",
    "KernelSelectionReceiptV1",
    "select_kernel",
]
