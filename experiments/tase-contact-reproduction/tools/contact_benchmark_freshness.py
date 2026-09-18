"""Offline freshness replay for the six-controller contact benchmark.

The replay deliberately does not invent samples.  For each cutoff it retains
only rows whose measured observation age is below that cutoff, reports the
discarded rows as censored, and admits a controller ranking only when every
row for that controller survives the cutoff.  This makes the 20/40/60/80-ms
sensitivity check useful without turning a timing policy into a controller
claim.
"""

from __future__ import annotations

from collections import defaultdict
import math
from typing import Any, Iterable, Mapping

from contact_benchmark_protocol import (
    FRESH_AGE_S,
    FRESHNESS_SENSITIVITY_CUTOFFS_S,
    STALE_AGE_S,
    classify_sensor_age,
)


def _number(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{role} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{role} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{role} must be finite")
    return number


def _mean(values: list[float]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _rms(values: list[float]) -> float | None:
    return math.sqrt(math.fsum(value * value for value in values) / len(values)) if values else None


def _row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise ValueError("freshness replay rows must be mappings")
    if "observation_age_s" not in row:
        raise ValueError("freshness replay row lacks observation_age_s")
    age = _number(row["observation_age_s"], "observation_age_s")
    if age < 0.0:
        raise ValueError("observation_age_s must be non-negative")
    normalized = dict(row)
    normalized["observation_age_s"] = age
    normalized["controller"] = str(row.get("controller", "all"))
    for name in ("force_error_n", "path_error_m", "vibration_metric"):
        if name in normalized and normalized[name] is not None:
            normalized[name] = _number(normalized[name], name)
    return normalized


def replay_freshness_sensitivity(
    rows: Iterable[Mapping[str, Any]],
    *,
    cutoffs_s: Iterable[float] = FRESHNESS_SENSITIVITY_CUTOFFS_S,
) -> dict[str, Any]:
    """Return deterministic metrics for each freshness cutoff.

    Required input is ``controller`` and ``observation_age_s``.  Optional
    ``force_error_n``, ``path_error_m`` and ``vibration_metric`` columns are
    summarized when present.  Rows at exactly a cutoff are censored, matching
    the strict fresh/held/stale boundary used by the runtime.
    """
    normalized = [_row(row) for row in rows]
    cutoffs = tuple(_number(value, "freshness cutoff") for value in cutoffs_s)
    if not cutoffs or any(value <= 0.0 for value in cutoffs):
        raise ValueError("freshness cutoffs must be positive")
    if any(right <= left for left, right in zip(cutoffs, cutoffs[1:])):
        raise ValueError("freshness cutoffs must be strictly increasing")

    result: dict[str, Any] = {
        "schema": "ur10e.contact-benchmark-freshness-sensitivity-v1",
        "fresh_age_s": FRESH_AGE_S,
        "stale_age_s": STALE_AGE_S,
        "cutoffs_s": list(cutoffs),
        "rows": len(normalized),
        "comparisons": [],
        "claim_scope": "offline replay only; no controller or physical acceptance",
    }
    for cutoff in cutoffs:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in normalized:
            grouped[item["controller"]].append(item)
        controllers = []
        ranking_candidates = []
        for controller in sorted(grouped):
            controller_rows = grouped[controller]
            accepted = [item for item in controller_rows if item["observation_age_s"] < cutoff]
            censored = len(accepted) != len(controller_rows)
            force_errors = [item["force_error_n"] for item in accepted if "force_error_n" in item]
            path_errors = [item["path_error_m"] for item in accepted if "path_error_m" in item]
            vibration = [item["vibration_metric"] for item in accepted if "vibration_metric" in item]
            held = [item for item in accepted if item["observation_age_s"] >= FRESH_AGE_S]
            entry = {
                "controller": controller,
                "total_rows": len(controller_rows),
                "accepted_rows": len(accepted),
                "censored_rows": len(controller_rows) - len(accepted),
                "censored": censored,
                "objective_eligible": not censored and bool(accepted),
                "held_fraction": len(held) / len(accepted) if accepted else 0.0,
                "force_mae_n": _mean([abs(value) for value in force_errors]),
                "force_rmse_n": _rms(force_errors),
                "path_rms_m": _rms(path_errors),
                "vibration_rms": _rms(vibration),
            }
            controllers.append(entry)
            if entry["objective_eligible"] and entry["force_rmse_n"] is not None:
                ranking_candidates.append(entry)
        ranking_candidates.sort(key=lambda entry: (entry["force_rmse_n"], entry["controller"]))
        result["comparisons"].append({
            "cutoff_s": cutoff,
            "controllers": controllers,
            "ranking_by_force_rmse": [entry["controller"] for entry in ranking_candidates],
            "ranking_is_complete": len(ranking_candidates) == len(controllers) and bool(controllers),
        })
    return result


__all__ = ["replay_freshness_sensitivity"]
