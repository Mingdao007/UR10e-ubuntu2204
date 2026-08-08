"""Gated EMA bias estimator used only by the offline STARS shadow."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Sequence

from .contact_gate import (
    BIAS_ESTIMATE_FIELDS,
    BIAS_RATE_ESTIMATE_FIELDS,
    GateDecision,
    GateMode,
)


Wrench6 = tuple[float, float, float, float, float, float]


def _valid_wrench(values: Sequence[float] | None) -> Wrench6 | None:
    if values is None or not isinstance(values, (list, tuple)) or len(values) != 6:
        return None
    result: list[float] = []
    for value in values:
        if isinstance(value, bool):
            return None
        if not isinstance(value, (int, float)):
            return None
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(number):
            return None
        result.append(number)
    return tuple(result)  # type: ignore[return-value]


@dataclass
class GatedEmaBiasEstimator:
    """EMA state whose clocks advance only by credited contiguous time."""

    ema_tau_s: float = 30.0
    rate_finite_diff_min_dt_s: float = 1.0
    bias: Wrench6 = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    rate: Wrench6 = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    _initialized: bool = False
    _credited_elapsed_s: float = 0.0
    _last_bias_for_rate: Wrench6 | None = None
    _last_bias_elapsed_s: float | None = None
    update_count: int = 0
    freeze_count: int = 0
    predict_count: int = 0
    invalid_input_count: int = 0
    update_time_s: float = 0.0
    freeze_time_s: float = 0.0
    predict_time_s: float = 0.0
    reason_counts: dict[str, int] = field(default_factory=dict)

    def _count_reason(self, reason: str) -> None:
        self.reason_counts[reason] = self.reason_counts.get(reason, 0) + 1

    def step(
        self,
        *,
        t_s: float,
        residual: Sequence[float] | None,
        decision: GateDecision,
        dt_s: float | None,
    ) -> dict[str, float | bool | str | None]:
        """Apply one gate result without ever sanitizing invalid evidence.

        ``t_s`` is retained in the API for compatibility and diagnostics.  It
        is not used as a clock: only ``dt_s`` (already credited by the adapter)
        can advance estimator time or finite-difference rate time.
        """

        del t_s
        dt = 0.0
        if dt_s is not None and isinstance(dt_s, (int, float)) and not isinstance(dt_s, bool):
            try:
                candidate_dt = float(dt_s)
            except (TypeError, ValueError, OverflowError):
                candidate_dt = 0.0
            if math.isfinite(candidate_dt) and candidate_dt > 0.0:
                dt = candidate_dt

        valid_residual = _valid_wrench(residual)
        effective_mode = decision.mode
        effective_reason = decision.bias_contact_reason
        effective_input_valid = decision.input_valid
        if (
            decision.mode is GateMode.UPDATE
            and (
                not decision.update_allowed
                or not decision.input_valid
                or valid_residual is None
                or dt <= 0.0
            )
        ):
            # This protects the estimator even if a caller manually constructs a
            # contradictory GateDecision instead of using decide_gate().
            effective_mode = GateMode.PREDICT_ONLY
            effective_reason = "update_contract_invalid"
            effective_input_valid = False

        self._count_reason(effective_reason)
        if not effective_input_valid:
            self.invalid_input_count += 1

        self._credited_elapsed_s += dt
        if effective_mode is GateMode.UPDATE:
            assert valid_residual is not None
            if not self._initialized:
                self.bias = valid_residual
                self._initialized = True
                self._last_bias_for_rate = self.bias
                self._last_bias_elapsed_s = self._credited_elapsed_s
            else:
                tau = float(self.ema_tau_s)
                if not math.isfinite(tau) or tau <= 0.0:
                    # A typed config validator normally prevents this branch;
                    # preserving the old bias is safer than a zero/NaN update.
                    effective_mode = GateMode.PREDICT_ONLY
                    effective_reason = "estimator_config_invalid"
                    self._count_reason(effective_reason)
                    self.invalid_input_count += 1
                else:
                    alpha = 1.0 - math.exp(-dt / tau) if dt > 0.0 else 0.0
                    self.bias = tuple(
                        (1.0 - alpha) * old + alpha * observed
                        for old, observed in zip(self.bias, valid_residual, strict=True)
                    )  # type: ignore[assignment]
                    if (
                        self._last_bias_elapsed_s is not None
                        and self._last_bias_for_rate is not None
                        and self._credited_elapsed_s - self._last_bias_elapsed_s
                        >= float(self.rate_finite_diff_min_dt_s)
                    ):
                        elapsed = self._credited_elapsed_s - self._last_bias_elapsed_s
                        self.rate = tuple(
                            (current - previous) / elapsed
                            for current, previous in zip(
                                self.bias, self._last_bias_for_rate, strict=True
                            )
                        )  # type: ignore[assignment]
                        self._last_bias_elapsed_s = self._credited_elapsed_s
                        self._last_bias_for_rate = self.bias

            if effective_mode is GateMode.UPDATE:
                self.update_count += 1
                self.update_time_s += dt
        if effective_mode is GateMode.FREEZE:
            self.freeze_count += 1
            self.freeze_time_s += dt
        elif effective_mode is GateMode.PREDICT_ONLY:
            self.predict_count += 1
            self.predict_time_s += dt

        row: dict[str, float | bool | str | None] = {
            "gate": effective_mode.value,
            "update_allowed": effective_mode is GateMode.UPDATE,
            "input_valid": effective_input_valid,
            "bias_estimation_contact_mask": bool(
                decision.bias_estimation_contact_mask
                if effective_mode is not GateMode.PREDICT_ONLY
                else False
            ),
            "bias_contact_reason": effective_reason,
            "contact_mask": bool(
                decision.bias_estimation_contact_mask
                if effective_mode is not GateMode.PREDICT_ONLY
                else False
            ),
        }
        for name, value in zip(BIAS_ESTIMATE_FIELDS, self.bias, strict=True):
            row[name] = float(value)
        for name, value in zip(BIAS_RATE_ESTIMATE_FIELDS, self.rate, strict=True):
            row[name] = float(value)
        return row

    def summary_counts(self) -> dict[str, Any]:
        return {
            "update_count": self.update_count,
            "freeze_count": self.freeze_count,
            "predict_count": self.predict_count,
            "invalid_input_count": self.invalid_input_count,
            "update_time_s": self.update_time_s,
            "freeze_time_s": self.freeze_time_s,
            "predict_time_s": self.predict_time_s,
            "reason_counts": dict(sorted(self.reason_counts.items())),
        }


__all__ = ["GatedEmaBiasEstimator"]
