"""Raw-tick paced benchmark and independent model-rate validation."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import platform
import time
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .contracts import MODEL_RATE_CANDIDATES_HZ
from .timing import ModelTimingEvidence, select_highest_accepted_model_rate


BENCHMARK_SCHEMA_VERSION = 1
RAW_CANDIDATE_STATUS = "unvalidated_raw_tick_candidate"
RATE_ORDER_HZ = MODEL_RATE_CANDIDATES_HZ


def _canonical_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_sha256(value: object, name: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256")
    return text


def _percentile_99(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("cannot compute p99 without executed ticks")
    position = 0.99 * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _output_is_nonfinite(result: object) -> bool:
    values = getattr(result, "raw_f_df", result)
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        return True
    return array.shape != (6,) or not np.isfinite(array).all()


def _runtime_binding(predictor: object) -> dict[str, str]:
    try:
        import torch

        torch_version = torch.__version__
    except ImportError:
        torch_version = "unavailable"
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "torch_version": torch_version,
        "device": str(getattr(predictor, "device", "unknown")),
        "predictor_class": type(predictor).__name__,
    }


def _summarize_ticks(
    ticks: Sequence[Mapping[str, object]], *, period_s: float
) -> dict[str, object]:
    latencies: list[float] = []
    deadline_misses = 0
    nonfinite_outputs = 0
    previous_release: float | None = None
    for expected_index, tick in enumerate(ticks):
        if int(tick.get("tick_index", -1)) != expected_index:
            raise ValueError("raw tick indices must be contiguous")
        release = float(tick.get("release_s", math.nan))
        started = float(tick.get("start_s", math.nan))
        ended = float(tick.get("end_s", math.nan))
        nonfinite = tick.get("nonfinite_output")
        if (
            not math.isfinite(release)
            or not math.isfinite(started)
            or not math.isfinite(ended)
            or started < release
            or ended < started
        ):
            raise ValueError("raw tick timing bounds are invalid")
        if previous_release is not None and not math.isclose(
            release - previous_release,
            period_s,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("raw tick release grid drifted")
        previous_release = release
        if not isinstance(nonfinite, bool):
            raise ValueError("raw tick nonfinite_output must be boolean")
        latency = ended - started
        latencies.append(latency)
        deadline_misses += int(ended >= release + period_s)
        nonfinite_outputs += int(nonfinite)
    return {
        "scheduled_ticks": len(ticks),
        "executed_ticks": len(latencies),
        "deadline_misses": deadline_misses,
        "nonfinite_outputs": nonfinite_outputs,
        "latency_p50_s": float(np.median(np.asarray(latencies, dtype=float))),
        "latency_p99_s": _percentile_99(latencies),
        "latency_max_s": max(latencies),
    }


def benchmark_paced_predictor(
    predictor: object,
    condition: Sequence[float],
    *,
    duration_per_rate_s: float,
    checkpoint_sha256: str,
    dataset_sha256: str,
    warmup_iterations: int = 1,
    clock: Callable[[], float] = time.perf_counter,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Produce raw ticks only; the producer is forbidden from self-selecting."""

    if not math.isfinite(duration_per_rate_s) or duration_per_rate_s <= 0.0:
        raise ValueError("duration_per_rate_s must be finite and positive")
    if warmup_iterations < 0:
        raise ValueError("warmup_iterations must be non-negative")
    condition_array = np.asarray(condition, dtype=float)
    if condition_array.shape != (36,) or not np.isfinite(condition_array).all():
        raise ValueError("benchmark condition must contain 36 finite values")
    checkpoint_hash = _require_sha256(checkpoint_sha256, "checkpoint_sha256")
    dataset_hash = _require_sha256(dataset_sha256, "dataset_sha256")
    predict = getattr(predictor, "predict", None)
    if not callable(predict):
        raise ValueError("predictor must expose a callable predict method")
    for iteration in range(warmup_iterations):
        result = predict(condition_array, seed=iteration)
        if _output_is_nonfinite(result):
            raise ValueError("warmup inference produced non-finite output")

    rate_results: list[dict[str, object]] = []
    for rate_hz in RATE_ORDER_HZ:
        period_s = 1.0 / rate_hz
        scheduled_ticks = max(1, int(round(duration_per_rate_s * rate_hz)))
        trial_started_s = clock()
        ticks: list[dict[str, object]] = []
        for tick_index in range(scheduled_ticks):
            release_s = trial_started_s + tick_index * period_s
            while True:
                now = clock()
                if now >= release_s:
                    break
                sleeper(release_s - now)
            started_s = clock()
            result = predict(condition_array, seed=tick_index)
            ended_s = clock()
            ticks.append(
                {
                    "tick_index": tick_index,
                    "release_s": release_s,
                    "start_s": started_s,
                    "end_s": ended_s,
                    "nonfinite_output": _output_is_nonfinite(result),
                }
            )
        summary = _summarize_ticks(ticks, period_s=period_s)
        rate_results.append(
            {
                "rate_hz": rate_hz,
                "period_s": period_s,
                "declared_duration_s": duration_per_rate_s,
                "trial_started_s": trial_started_s,
                "trial_ended_s": clock(),
                "raw_ticks": ticks,
                "producer_summary": summary,
            }
        )
    harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "candidate_status": RAW_CANDIDATE_STATUS,
        "timing_mode": "independent_wall_clock_paced_raw_ticks",
        "duration_per_rate_s": duration_per_rate_s,
        "rates_hz": list(RATE_ORDER_HZ),
        "artifact_bindings": {
            "checkpoint_sha256": checkpoint_hash,
            "dataset_sha256": dataset_hash,
            "harness_source_sha256": harness_sha256,
            "runtime": _runtime_binding(predictor),
        },
        "rate_results": rate_results,
        "producer_selected_rate_hz": None,
        "producer_selection_eligible": False,
        "active_enabled": False,
        "shadow_only": True,
    }


def validate_paced_benchmark_candidate(
    candidate: Mapping[str, object],
    *,
    expected_checkpoint_sha256: str,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    """Recompute raw ticks and select only from complete 60 s evidence."""

    if candidate.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported paced benchmark schema")
    if candidate.get("candidate_status") != RAW_CANDIDATE_STATUS:
        raise ValueError("benchmark candidate status is invalid")
    if candidate.get("timing_mode") != "independent_wall_clock_paced_raw_ticks":
        raise ValueError("benchmark candidate is not paced raw-tick evidence")
    if candidate.get("producer_selected_rate_hz") is not None:
        raise ValueError("benchmark producer may not self-select a model rate")
    if candidate.get("producer_selection_eligible") is not False:
        raise ValueError("benchmark producer may not claim selection eligibility")
    if candidate.get("active_enabled") is not False or candidate.get("shadow_only") is not True:
        raise ValueError("benchmark candidate must remain inactive and shadow-only")
    duration = float(candidate.get("duration_per_rate_s", math.nan))
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError("benchmark duration is invalid")
    bindings = candidate.get("artifact_bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("benchmark artifact bindings are missing")
    checkpoint_hash = _require_sha256(
        bindings.get("checkpoint_sha256"), "checkpoint_sha256"
    )
    dataset_hash = _require_sha256(bindings.get("dataset_sha256"), "dataset_sha256")
    if checkpoint_hash != _require_sha256(
        expected_checkpoint_sha256, "expected_checkpoint_sha256"
    ):
        raise ValueError("benchmark checkpoint binding mismatch")
    if dataset_hash != _require_sha256(
        expected_dataset_sha256, "expected_dataset_sha256"
    ):
        raise ValueError("benchmark dataset binding mismatch")
    harness_hash = _require_sha256(
        bindings.get("harness_source_sha256"), "harness_source_sha256"
    )
    current_harness_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if harness_hash != current_harness_hash:
        raise ValueError("benchmark harness source binding mismatch")
    runtime = bindings.get("runtime")
    if not isinstance(runtime, Mapping) or not {
        "python_version",
        "platform",
        "torch_version",
        "device",
        "predictor_class",
    } <= set(runtime):
        raise ValueError("benchmark runtime binding is incomplete")
    if not all(str(runtime[name]).strip() for name in runtime):
        raise ValueError("benchmark runtime binding contains an empty value")
    results = candidate.get("rate_results")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise ValueError("benchmark rate_results are missing")
    if candidate.get("rates_hz") != list(RATE_ORDER_HZ):
        raise ValueError("benchmark rate order must be 500/200/100/50 Hz")
    recomputed: list[dict[str, object]] = []
    timing_evidence: list[ModelTimingEvidence] = []
    seen_rates: set[int] = set()
    for raw_result in results:
        if not isinstance(raw_result, Mapping):
            raise ValueError("benchmark rate result must be an object")
        rate_hz = int(raw_result.get("rate_hz", 0))
        if rate_hz not in RATE_ORDER_HZ or rate_hz in seen_rates:
            raise ValueError("benchmark contains invalid or duplicate model rate")
        seen_rates.add(rate_hz)
        period_s = float(raw_result.get("period_s", math.nan))
        if not math.isclose(
            period_s, 1.0 / rate_hz, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("benchmark rate period mismatch")
        if not math.isclose(
            float(raw_result.get("declared_duration_s", math.nan)),
            duration,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("benchmark rate duration mismatch")
        ticks = raw_result.get("raw_ticks")
        if not isinstance(ticks, Sequence) or isinstance(ticks, (str, bytes)):
            raise ValueError("benchmark raw ticks are missing")
        expected_ticks = max(1, int(round(duration * rate_hz)))
        if len(ticks) != expected_ticks:
            raise ValueError("benchmark raw tick count does not cover declared duration")
        trial_started_s = float(raw_result.get("trial_started_s", math.nan))
        trial_ended_s = float(raw_result.get("trial_ended_s", math.nan))
        if (
            not math.isfinite(trial_started_s)
            or not math.isfinite(trial_ended_s)
            or trial_ended_s < trial_started_s
            or not math.isclose(
                float(ticks[0].get("release_s", math.nan)),
                trial_started_s,
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or trial_ended_s < float(ticks[-1].get("end_s", math.nan))
        ):
            raise ValueError("benchmark trial bounds do not bind the raw ticks")
        summary = _summarize_ticks(ticks, period_s=period_s)
        if raw_result.get("producer_summary") != summary:
            raise ValueError("benchmark producer summary mismatch")
        accepted = (
            duration >= 60.0
            and summary["deadline_misses"] == 0
            and summary["nonfinite_outputs"] == 0
            and float(summary["latency_p99_s"]) <= 0.8 * period_s
        )
        recomputed.append(
            {
                "rate_hz": rate_hz,
                **summary,
                "accepted": accepted,
            }
        )
        if duration >= 60.0:
            timing_evidence.append(
                ModelTimingEvidence(
                    rate_hz=rate_hz,
                    duration_s=duration,
                    p99_latency_s=float(summary["latency_p99_s"]),
                    deadline_misses=int(summary["deadline_misses"]),
                    nonfinite_outputs=int(summary["nonfinite_outputs"]),
                )
            )
    complete_rates = seen_rates == set(RATE_ORDER_HZ)
    selection_eligible = duration >= 60.0 and complete_rates
    selected_rate_hz: int | None = None
    timing_evidence_sha256: str | None = None
    if selection_eligible:
        selection = select_highest_accepted_model_rate(timing_evidence)
        selected_rate_hz = selection.selected_rate_hz
        timing_evidence_sha256 = selection.evidence_sha256
    candidate_sha256 = _canonical_sha256(candidate)
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "validation_status": (
            "validated_selection_manifest"
            if selection_eligible
            else "validated_short_diagnostic_only"
        ),
        "selection_eligible": selection_eligible,
        "selected_rate_hz": selected_rate_hz,
        "duration_per_rate_s": duration,
        "all_rates_tested": complete_rates,
        "candidate_sha256": candidate_sha256,
        "timing_evidence_sha256": timing_evidence_sha256,
        "artifact_bindings": dict(bindings),
        "recomputed_rate_results": sorted(
            recomputed, key=lambda item: RATE_ORDER_HZ.index(int(item["rate_hz"]))
        ),
        "active_enabled": False,
        "shadow_only": True,
    }
