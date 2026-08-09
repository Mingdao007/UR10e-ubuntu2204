"""Read-only BO health analysis for admitted historical Autotune evidence.

The report is analysis-only: it cannot import observations, promote an
incumbent, dispatch a candidate, or certify completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .gp_calibration import AdmissionResult, CalibrationRow, admit_phase5_ledger


BO_HEALTH_SCHEMA = "step5d.autotune-v4/r010-bo-health-v1"
BO_HEALTH_POLICY_SCHEMA = "step5d.autotune-v4/r010-bo-health-policy-v1"


class R010BoHealthError(ValueError):
    """Historical BO evidence or policy is invalid."""


@dataclass(frozen=True)
class BoHealthPolicy:
    plateau_min_admitted_after_best: int = 100
    robust_min_repeats: int = 2
    thresholds_n: tuple[float, ...] = (0.2, 0.35, 0.5, 0.6, 0.7, 0.8, 1.0)
    schema: str = BO_HEALTH_POLICY_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != BO_HEALTH_POLICY_SCHEMA:
            raise R010BoHealthError("BO health policy schema differs")
        if (
            isinstance(self.plateau_min_admitted_after_best, bool)
            or self.plateau_min_admitted_after_best <= 0
        ):
            raise R010BoHealthError("plateau window must be a positive int")
        if isinstance(self.robust_min_repeats, bool) or self.robust_min_repeats < 2:
            raise R010BoHealthError("robust incumbent requires at least two repeats")
        previous = -math.inf
        for threshold in self.thresholds_n:
            if not math.isfinite(threshold) or threshold <= previous or threshold <= 0.0:
                raise R010BoHealthError("BO thresholds must be finite, positive, and increasing")
            previous = threshold

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "plateau_min_admitted_after_best": self.plateau_min_admitted_after_best,
            "robust_min_repeats": self.robust_min_repeats,
            "thresholds_n": list(self.thresholds_n),
        }


DEFAULT_BO_HEALTH_POLICY = BoHealthPolicy()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R010BoHealthError(f"BO health report is not canonical JSON: {exc}") from exc


def _row_summary(row: CalibrationRow) -> dict[str, Any]:
    return {
        "attempt_sequence": row.attempt_sequence,
        "kind": row.kind,
        "objective_n": row.objective_n,
        "candidate": dict(row.candidate),
    }


def _kind_summary(rows: Sequence[CalibrationRow]) -> dict[str, Any]:
    values = [row.objective_n for row in rows]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min_n": min(values),
        "median_n": statistics.median(values),
        "mean_n": statistics.fmean(values),
        "max_n": max(values),
    }


def _group_rows(rows: Sequence[CalibrationRow]) -> dict[tuple[Any, ...], list[CalibrationRow]]:
    groups: dict[tuple[Any, ...], list[CalibrationRow]] = {}
    for row in rows:
        groups.setdefault(row.gain_key, []).append(row)
    return groups


def _group_summary(rows: Sequence[CalibrationRow]) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: row.attempt_sequence)
    values = [row.objective_n for row in ordered]
    return {
        "candidate": dict(ordered[0].candidate),
        "attempt_sequences": [row.attempt_sequence for row in ordered],
        "kinds": sorted({row.kind for row in ordered}),
        "repeat_count": len(values),
        "min_n": min(values),
        "median_n": statistics.median(values),
        "mean_n": statistics.fmean(values),
        "sample_stdev_n": statistics.stdev(values) if len(values) > 1 else None,
    }


def _improvement_events(rows: Sequence[CalibrationRow]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    incumbent = math.inf
    for admitted_index, row in enumerate(rows, 1):
        if row.objective_n < incumbent:
            incumbent = row.objective_n
            events.append(
                {
                    "admitted_index": admitted_index,
                    "attempt_sequence": row.attempt_sequence,
                    "kind": row.kind,
                    "objective_n": row.objective_n,
                }
            )
    return events


def build_bo_health_report(
    admission: AdmissionResult,
    *,
    policy: BoHealthPolicy = DEFAULT_BO_HEALTH_POLICY,
) -> dict[str, Any]:
    if not isinstance(admission, AdmissionResult) or not admission.rows:
        raise R010BoHealthError("BO health requires non-empty admitted evidence")
    if not isinstance(policy, BoHealthPolicy):
        raise R010BoHealthError("BO health requires a typed policy")
    rows = tuple(sorted(admission.rows, key=lambda row: row.attempt_sequence))
    by_kind = {
        kind: tuple(row for row in rows if row.kind == kind)
        for kind in sorted({row.kind for row in rows})
    }
    bo_rows = by_kind.get("BO_TRIAL", ())
    anchor_rows = by_kind.get("ANCHOR", ())
    if not bo_rows or not anchor_rows:
        raise R010BoHealthError("BO health requires both ANCHOR and BO_TRIAL evidence")

    groups = _group_rows(rows)
    repeated_groups = [group for group in groups.values() if len(group) >= policy.robust_min_repeats]
    repeated_bo_groups = [
        group
        for group in repeated_groups
        if any(row.kind == "BO_TRIAL" for row in group)
    ]
    one_shot = min(rows, key=lambda row: (row.objective_n, row.attempt_sequence))
    one_shot_group = groups[one_shot.gain_key]
    robust_bo = (
        min(
            repeated_bo_groups,
            key=lambda group: (
                statistics.median(row.objective_n for row in group),
                statistics.fmean(row.objective_n for row in group),
                min(row.attempt_sequence for row in group),
            ),
        )
        if repeated_bo_groups
        else None
    )
    best_index = rows.index(one_shot)
    admitted_after_best = len(rows) - best_index - 1
    anchor_median = statistics.median(row.objective_n for row in anchor_rows)
    bo_median = statistics.median(row.objective_n for row in bo_rows)
    bo_best = min(row.objective_n for row in bo_rows)

    report: dict[str, Any] = {
        "schema": BO_HEALTH_SCHEMA,
        "policy": policy.as_dict(),
        "source": {
            "input_sha256": admission.input_sha256,
            "dataset_sha256": admission.dataset_sha256,
            "campaign_fingerprint": rows[0].campaign_fingerprint,
            "total_observation_rows": admission.total_observation_rows,
            "admitted_rows": len(rows),
            "rejection_counts": dict(admission.rejection_counts),
        },
        "dataset": {
            "unique_gain_keys": len(groups),
            "repeat_group_count": len(repeated_groups),
            "max_repeat_count": max(len(group) for group in groups.values()),
            "kind_summaries": {
                kind: _kind_summary(kind_rows) for kind, kind_rows in by_kind.items()
            },
        },
        "progress": {
            "incumbent_events": _improvement_events(rows),
            "one_shot_incumbent": _row_summary(one_shot),
            "one_shot_incumbent_repeat_evidence": _group_summary(one_shot_group),
            "robust_repeated_bo_incumbent": (
                _group_summary(robust_bo) if robust_bo is not None else None
            ),
            "admitted_after_best": admitted_after_best,
            "plateau_detected": (
                admitted_after_best >= policy.plateau_min_admitted_after_best
            ),
        },
        "improvement": {
            "anchor_median_n": anchor_median,
            "bo_median_n": bo_median,
            "bo_best_n": bo_best,
            "bo_median_reduction_fraction_vs_anchor": 1.0 - bo_median / anchor_median,
            "bo_best_reduction_fraction_vs_anchor": 1.0 - bo_best / anchor_median,
            "historical_bo_improved": bo_median < anchor_median and bo_best < anchor_median,
            "r010_calibration_live_improvement_proven": False,
        },
        "threshold_counts": {
            f"le_{threshold:g}_n": sum(
                1 for row in rows if row.objective_n <= threshold
            )
            for threshold in policy.thresholds_n
        },
        "decision": {
            "historical_training_import_allowed": False,
            "resume_old_ledger_allowed": False,
            "completion_proven": False,
            "single_low_mae_is_robust_incumbent": len(one_shot_group) >= 3,
            "next_evidence": [
                "identity_bound_fixed_anchor_wave7_canary",
                "empty_r010_ledger_cold_start",
                "bounded_sequential_bo_with_repeat_budget",
            ],
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    return report


def analyze_phase5_ledger(
    path: Path,
    *,
    policy: BoHealthPolicy = DEFAULT_BO_HEALTH_POLICY,
    expected_rows: int | None = 567,
    expected_timing_ineligible: int | None = 13,
) -> dict[str, Any]:
    admission = admit_phase5_ledger(
        path,
        expected_rows=expected_rows,
        expected_timing_ineligible=expected_timing_ineligible,
    )
    return build_bo_health_report(admission, policy=policy)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path, help="historical sealed Phase5 JSONL ledger")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    print(json.dumps(analyze_phase5_ledger(args.ledger), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BO_HEALTH_POLICY_SCHEMA",
    "BO_HEALTH_SCHEMA",
    "BoHealthPolicy",
    "DEFAULT_BO_HEALTH_POLICY",
    "R010BoHealthError",
    "analyze_phase5_ledger",
    "build_bo_health_report",
]
