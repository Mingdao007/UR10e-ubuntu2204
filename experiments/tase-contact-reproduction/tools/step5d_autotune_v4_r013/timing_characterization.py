"""Independent, non-BO timing characterization for the R013 live seam.

This lane consumes fresh preparation directories and exercises only the
owner's fixed qualification lifecycle.  It never opens a campaign scheduler,
creates a stage dispatch, calls ``tell_exact``, or changes the controller
program/configuration.  The legacy 0.98 packet-consumption ratio is retained
as a diagnostic while the evidence is classified against the independent
rate/freshness gates.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, Iterable, Mapping, Sequence


SCHEMA = "step5d.autotune-v4/r013-timing-characterization-v2"
VERSION = 2
LEGACY_RATIO = 0.98
COALESCING_RATIO_LOW = 0.95
COALESCING_RATIO_HIGH = 0.98
COALESCING_STABILITY_SPAN = 0.01
MIN_RATE_HZ = 460.0
P99_MAX_S = 0.010
MAX_GAP_S = 0.020
DEFAULT_REPEATS = 10


def _finite(value: Any, role: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{role} must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{role} must be finite")
    return parsed


def _sha(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stats(values: Sequence[float], *, ddof: int = 1) -> dict[str, Any]:
    parsed = tuple(_finite(value, "timing statistic") for value in values)
    if not parsed:
        return {
            "n": 0,
            "ddof": ddof,
            "mean": None,
            "stdev": None,
            "median": None,
            "min": None,
            "max": None,
        }
    deviation = None
    if len(parsed) > ddof:
        deviation = statistics.stdev(parsed) if ddof == 1 else statistics.pstdev(parsed)
    return {
        "n": len(parsed),
        "ddof": ddof,
        "mean": statistics.fmean(parsed),
        "stdev": deviation,
        "median": statistics.median(parsed),
        "min": min(parsed),
        "max": max(parsed),
    }


def _timing_from_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    rates = value.get("layer_rates_hz")
    if not isinstance(rates, Mapping):
        raise ValueError("timing layer_rates_hz is missing")
    writer_hz = _finite(rates.get("writer_publishes"), "writer_hz")
    rtde_hz = _finite(rates.get("rtde_frames"), "rtde_hz")
    tp_hz = _finite(rates.get("tp_consumed_packet_echoes"), "tp_hz")
    kunwei_hz = _finite(rates.get("kunwei_frames"), "kunwei_hz")
    if writer_hz <= 0.0:
        raise ValueError("writer_hz must be positive")
    return {
        "duration_s": _finite(value.get("duration_s"), "timing duration"),
        "writer_hz": writer_hz,
        "rtde_hz": rtde_hz,
        "tp_hz": tp_hz,
        "kunwei_hz": kunwei_hz,
        "tp_writer_ratio": tp_hz / writer_hz,
        "feedback_p99_s": _finite(value.get("feedback_age_p99_s"), "feedback p99"),
        "max_fresh_gap_s": _finite(value.get("max_fresh_gap_s"), "fresh gap"),
        "distinct_writer": int(value.get("successful_writer_publishes", 0)),
        "distinct_rtde": int(value.get("distinct_rtde_frames", 0)),
        "distinct_tp": int(value.get("distinct_tp_consumed_packet_echoes", 0)),
        "distinct_kunwei": int(value.get("distinct_kunwei_frames", 0)),
    }


def classify_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    minimum_observations: int = DEFAULT_REPEATS,
) -> dict[str, Any]:
    """Classify timing evidence without changing any live gate."""

    if type(minimum_observations) is not int or minimum_observations < 3:
        raise ValueError("timing classification requires at least three observations")

    rows = [dict(row) for row in observations]
    timing_rows = [row["timing"] for row in rows if isinstance(row.get("timing"), Mapping)]
    ratios = [float(row["tp_writer_ratio"]) for row in timing_rows]
    hard_failures: list[dict[str, Any]] = []
    legacy_ratio_pass_count = 0
    for row in timing_rows:
        row_failures: list[str] = []
        if row["writer_hz"] < MIN_RATE_HZ:
            row_failures.append("writer_hz_below_460")
        if row["rtde_hz"] < MIN_RATE_HZ:
            row_failures.append("rtde_hz_below_460")
        if row["tp_hz"] < MIN_RATE_HZ:
            row_failures.append("tp_hz_below_460")
        if row["feedback_p99_s"] > P99_MAX_S:
            row_failures.append("feedback_p99_above_10ms")
        if row["max_fresh_gap_s"] >= MAX_GAP_S:
            row_failures.append("fresh_gap_at_or_above_20ms")
        if row["tp_writer_ratio"] >= LEGACY_RATIO:
            legacy_ratio_pass_count += 1
        if row_failures:
            hard_failures.append({"timing": dict(row), "failures": row_failures})
    stable_band = bool(
        ratios
        and all(COALESCING_RATIO_LOW <= value < COALESCING_RATIO_HIGH for value in ratios)
        and max(ratios) - min(ratios) <= COALESCING_STABILITY_SPAN
    )
    if hard_failures:
        classification = "controller_or_host_timing_failure"
    elif len(timing_rows) < minimum_observations:
        classification = "inconclusive_insufficient_observations"
    elif stable_band:
        classification = "coalescing_candidate"
    else:
        classification = "inconclusive_ratio_not_stable"
    metric_values = {
        key: _stats([row[key] for row in timing_rows])
        for key in (
            "writer_hz", "rtde_hz", "tp_hz", "kunwei_hz", "tp_writer_ratio",
            "feedback_p99_s", "max_fresh_gap_s",
        )
    }
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "observation_count": len(rows),
        "timing_observation_count": len(timing_rows),
        "classification_minimum_observations": minimum_observations,
        "statistics_ddof": 1,
        "metrics": metric_values,
        "legacy_ratio": LEGACY_RATIO,
        "legacy_ratio_pass_count": legacy_ratio_pass_count,
        "coalescing_ratio_band": [COALESCING_RATIO_LOW, COALESCING_RATIO_HIGH],
        "coalescing_stability_span": COALESCING_STABILITY_SPAN,
        "hard_gate": {
            "minimum_writer_hz": MIN_RATE_HZ,
            "minimum_rtde_hz": MIN_RATE_HZ,
            "minimum_tp_hz": MIN_RATE_HZ,
            "feedback_p99_max_s": P99_MAX_S,
            "max_fresh_gap_s_exclusive": MAX_GAP_S,
        },
        "hard_failures": hard_failures,
        "classification": classification,
        "legacy_ratio_remains_diagnostic": True,
        "automatic_gate_change": False,
    }


def _lifecycle_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for path in Path(run_dir).glob("*.r013life.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(row, dict) and row.get("kind") == "QUALIFICATION":
            result[str(row.get("execution_id"))] = {**row, "receipt_path": str(path)}
    return result


def _lifecycle_sequence_stats(path: Path) -> dict[str, Any]:
    """Cold-read packet jumps from the immutable lifecycle artifact."""

    if not path.is_file():
        return {
            "packet_sequence_gap_count": 0,
            "packet_sequence_regression_count": 0,
            "sequence_jump_histogram": {},
            "sequence_advance": 0,
            "sample_count": 0,
        }
    from .lifecycle_trace import iter_lifecycle_rows

    packets: list[int] = []
    consumed: list[int] = []
    for row in iter_lifecycle_rows(path):
        packet = int(row.get("packet_sequence", -1))
        consumed_packet = int(row.get("consumed_packet_sequence", -1))
        # ``iter_lifecycle_rows`` exposes terminal TP states but not a
        # synthetic ``terminal`` boolean.  Excluding a nonexistent key made
        # the final Home row look like a packet regression in every receipt.
        terminal = row.get("tp_state") in {78, 90}
        if packet >= 0 and not terminal:
            packets.append(packet)
        if consumed_packet >= 0 and not terminal:
            consumed.append(consumed_packet)
    histogram: Counter[str] = Counter()
    regressions = 0
    gaps = 0
    for previous, current in zip(packets, packets[1:]):
        delta = current - previous
        if delta < 1:
            regressions += 1
        else:
            histogram[str(delta)] += 1
            gaps += max(0, delta - 1)
    sequence_advance = max(0, packets[-1] - packets[0]) if len(packets) >= 2 else 0
    consumed_advance = max(0, consumed[-1] - consumed[0]) if len(consumed) >= 2 else 0
    return {
        "packet_sequence_gap_count": gaps,
        "packet_sequence_regression_count": regressions,
        "sequence_jump_histogram": dict(sorted(histogram.items())),
        "sequence_advance": sequence_advance,
        "consumed_sequence_advance": consumed_advance,
        "sample_count": len(packets),
    }


def load_run_observations(run_dir: Path) -> list[dict[str, Any]]:
    """Load qualification timing rows from one fresh preparation run."""

    run_dir = Path(run_dir).resolve()
    physical = run_dir / "r006-physical-observations.jsonl"
    if not physical.is_file():
        return []
    lifecycle = _lifecycle_index(run_dir)
    observations: list[dict[str, Any]] = []
    for line in physical.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, Mapping) or row.get("kind") != "QUALIFICATION":
            continue
        metrics = row.get("metrics")
        if not isinstance(metrics, Mapping):
            continue
        timing = metrics.get("timing_evidence")
        if not isinstance(timing, Mapping):
            continue
        execution_id = str(metrics.get("execution_id", ""))
        receipt = lifecycle.get(execution_id, {})
        gap_counts = {
            "packet_sequence_gap_count": int(receipt.get("packet_sequence_gap_count", 0)),
            "packet_sequence_regression_count": int(receipt.get("packet_sequence_regression_count", 0)),
            "artifact_sample_index_gap_count": int(receipt.get("artifact_sample_index_gap_count", 0)),
        }
        lifecycle_path = Path(str(receipt.get("artifact_path", "")))
        sequence_stats = _lifecycle_sequence_stats(lifecycle_path)
        observations.append(
            {
                "schema": SCHEMA,
                "run_dir": str(run_dir),
                "attempt_sequence": int(row.get("attempt_sequence", 0)),
                "execution_id": execution_id,
                "candidate": dict(row.get("candidate", {})),
                "timing": _timing_from_mapping(timing),
                "timing_decision": dict(metrics.get("r008_timing_decision", {})),
                "arm_transition_classification": dict(
                    receipt.get("arm_transition_classification", {})
                    if isinstance(receipt.get("arm_transition_classification"), Mapping)
                    else {}
                ),
                "timing_scheduler": dict(
                    receipt.get("timing_scheduler", {})
                    if isinstance(receipt.get("timing_scheduler"), Mapping)
                    else {}
                ),
                "gc_window": dict(
                    receipt.get("gc_window", {})
                    if isinstance(receipt.get("gc_window"), Mapping)
                    else {}
                ),
                "arm_transition_markers": list(
                    receipt.get("arm_transition_markers", ())
                    if isinstance(receipt.get("arm_transition_markers"), list)
                    else ()
                ),
                "packet_sequence": {**gap_counts, **sequence_stats},
                "lifecycle_receipt": str(receipt.get("receipt_path", "")),
                "qualification_passed": bool(row.get("qualification_passed", False)),
                "timing_gate": bool(row.get("timing_gate", False)),
            }
        )
    return observations


def build_receipt(run_dirs: Iterable[Path], *, lane_id: str) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        observations.extend(load_run_observations(Path(run_dir)))
    candidate_tokens = {
        _sha(row.get("candidate", {})) for row in observations if row.get("candidate")
    }
    source_identities: set[str] = set()
    for row in observations:
        prep = Path(row["run_dir"]) / "r013_live_preparation.json"
        try:
            payload = json.loads(prep.read_text(encoding="utf-8"))
            source_identities.add(str(payload["materialized_campaign_fingerprint"]["source_identity"]))
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
            continue
    classification = classify_observations(observations)
    sequence_distribution = Counter()
    sequence_jump_histogram = Counter()
    regression_total = 0
    sample_gap_total = 0
    pooled_writer = 0.0
    pooled_tp = 0.0
    for row in observations:
        gaps = row["packet_sequence"]
        sequence_distribution[str(gaps["packet_sequence_gap_count"])] += 1
        sequence_jump_histogram.update(gaps.get("sequence_jump_histogram", {}))
        regression_total += gaps["packet_sequence_regression_count"]
        sample_gap_total += gaps["artifact_sample_index_gap_count"]
        timing = row.get("timing")
        if isinstance(timing, Mapping):
            pooled_writer += float(timing.get("writer_hz", 0.0)) * float(
                timing.get("duration_s", 0.0)
            )
            pooled_tp += float(timing.get("tp_hz", 0.0)) * float(
                timing.get("duration_s", 0.0)
            )
    return {
        **classification,
        "lane_id": str(lane_id),
        "fixed_candidate_identity_count": len(candidate_tokens),
        "fixed_candidate_identity": next(iter(candidate_tokens), None),
        "source_identity_count": len(source_identities),
        "source_identities": sorted(source_identities),
        "observations": observations,
        "sequence_jump_observation_distribution": dict(sorted(sequence_distribution.items())),
        "sequence_jump_histogram": dict(sorted(sequence_jump_histogram.items())),
        "pooled_ratio": (pooled_tp / pooled_writer) if pooled_writer > 0.0 else None,
        "pooled_writer_count": pooled_writer,
        "pooled_tp_count": pooled_tp,
        "packet_sequence_regression_total": regression_total,
        "artifact_sample_index_gap_total": sample_gap_total,
        "bo_entered": False,
        "tell_exact_called": False,
        "controller_modified": False,
    }


__all__ = [
    "SCHEMA",
    "build_receipt",
    "classify_observations",
    "load_run_observations",
]
