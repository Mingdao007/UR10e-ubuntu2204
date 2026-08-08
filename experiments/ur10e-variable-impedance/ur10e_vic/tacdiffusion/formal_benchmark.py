"""Paced raw-tick benchmark for the formal TacDiffusion V4 sampler."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import time
from typing import Callable, Mapping, Sequence

import numpy as np

from .benchmark import (
    _canonical_sha256,
    _require_sha256,
    _runtime_binding,
    _summarize_ticks,
)
from .contracts import (
    FORMAL_MODEL_RATE_CANDIDATES_HZ,
    FORMAL_OBSERVATION_DIMENSION,
    FORMAL_SAMPLER_STEPS,
)
from .formal_timing import FormalModelTimingEvidence, select_formal_model_rate
from .formal_model import FormalDDPMPredictorV1, FormalDDPMSampleV1


FORMAL_BENCHMARK_SCHEMA_VERSION = 1
FORMAL_RAW_CANDIDATE_STATUS = "formal_v4_raw_tick_candidate"
FORMAL_RATE_ORDER_HZ = FORMAL_MODEL_RATE_CANDIDATES_HZ
FORMAL_TIMING_MODE = "formal_v4_paced_raw_ticks_50_step_sampler"


def _formal_output_is_nonfinite(result: object) -> bool:
    values = getattr(result, "raw_action", result)
    values = getattr(values, "raw_f_df", values)
    try:
        array = np.asarray(values, dtype=float)
    except (ValueError, OverflowError):
        return True
    return array.size == 0 or not np.isfinite(array).all()


def _call_formal_predictor(
    predictor: FormalDDPMPredictorV1,
    observation: np.ndarray,
    tick_index: int,
) -> FormalDDPMSampleV1:
    """Call the canonical formal predictor API exactly once per raw tick."""

    if not isinstance(predictor, FormalDDPMPredictorV1):
        raise ValueError("formal benchmark requires FormalDDPMPredictorV1")
    result = predictor.sample(observation, seed=tick_index)
    if not isinstance(result, FormalDDPMSampleV1):
        raise ValueError("formal predictor returned an untyped sample")
    if result.steps_executed != FORMAL_SAMPLER_STEPS:
        raise ValueError("formal predictor did not execute exactly 50 steps")
    if len(result.step_trace) != FORMAL_SAMPLER_STEPS:
        raise ValueError("formal predictor step trace does not prove 50 steps")
    return result


def benchmark_formal_50_step_sampler(
    predictor: FormalDDPMPredictorV1,
    observation: Sequence[float],
    *,
    duration_per_rate_s: float,
    checkpoint_sha256: str,
    dataset_sha256: str,
    clock: Callable[[], float] = time.perf_counter,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    """Benchmark actual 50-step inference on paced 100/50 Hz raw ticks."""

    if not math.isfinite(duration_per_rate_s) or duration_per_rate_s <= 0.0:
        raise ValueError("formal duration_per_rate_s must be finite and positive")
    condition_array = np.asarray(observation, dtype=float)
    if (
        condition_array.shape != (FORMAL_OBSERVATION_DIMENSION,)
        or not np.isfinite(condition_array).all()
    ):
        raise ValueError("formal benchmark observation must contain 84 finite values")
    checkpoint_hash = _require_sha256(checkpoint_sha256, "checkpoint_sha256")
    dataset_hash = _require_sha256(dataset_sha256, "dataset_sha256")

    rate_results: list[dict[str, object]] = []
    for rate_hz in FORMAL_RATE_ORDER_HZ:
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
                before_sleep = now
                sleeper(release_s - now)
                if clock() <= before_sleep:
                    raise RuntimeError("formal benchmark clock did not advance during pacing")
            started_s = clock()
            result = _call_formal_predictor(predictor, condition_array, tick_index)
            ended_s = clock()
            ticks.append(
                {
                    "tick_index": tick_index,
                    "release_s": release_s,
                    "start_s": started_s,
                    "end_s": ended_s,
                    "nonfinite_output": _formal_output_is_nonfinite(result),
                    "sampler_steps_executed": result.steps_executed,
                    "sampler_step_trace_length": len(result.step_trace),
                    "sampler_api": result.sampler_api,
                }
            )
        summary = _summarize_ticks(ticks, period_s=period_s)
        rate_results.append(
            {
                "rate_hz": rate_hz,
                "period_s": period_s,
                "declared_duration_s": duration_per_rate_s,
                "sampler_steps": FORMAL_SAMPLER_STEPS,
                "paced_raw_ticks": True,
                "trial_started_s": trial_started_s,
                "trial_ended_s": clock(),
                "raw_ticks": ticks,
                "producer_summary": summary,
            }
        )

    harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "schema_version": FORMAL_BENCHMARK_SCHEMA_VERSION,
        "candidate_status": FORMAL_RAW_CANDIDATE_STATUS,
        "timing_mode": FORMAL_TIMING_MODE,
        "observation_dimension": FORMAL_OBSERVATION_DIMENSION,
        "sampler_steps": FORMAL_SAMPLER_STEPS,
        "formal_model_mode": predictor.config.mode,
        "formal_output_dimension": predictor.config.output_dimension,
        "formal_sampler_api": "FormalDDPMPredictorV1.sample",
        "duration_per_rate_s": duration_per_rate_s,
        "rates_hz": list(FORMAL_RATE_ORDER_HZ),
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


def validate_formal_50_step_sampler_candidate(
    candidate: Mapping[str, object],
    *,
    expected_checkpoint_sha256: str,
    expected_dataset_sha256: str,
) -> dict[str, object]:
    """Independently validate the formal rates and return a shadow selection."""

    if candidate.get("schema_version") != FORMAL_BENCHMARK_SCHEMA_VERSION:
        raise ValueError("unsupported formal sampler benchmark schema")
    if candidate.get("candidate_status") != FORMAL_RAW_CANDIDATE_STATUS:
        raise ValueError("formal sampler candidate status is invalid")
    if candidate.get("timing_mode") != FORMAL_TIMING_MODE:
        raise ValueError("formal sampler candidate is not paced raw-tick evidence")
    if candidate.get("observation_dimension") != FORMAL_OBSERVATION_DIMENSION:
        raise ValueError("formal sampler candidate observation dimension must be 84")
    if candidate.get("sampler_steps") != FORMAL_SAMPLER_STEPS:
        raise ValueError("formal sampler candidate must use exactly 50 steps")
    if candidate.get("formal_sampler_api") != "FormalDDPMPredictorV1.sample":
        raise ValueError("formal sampler candidate must bind the formal predictor API")
    if candidate.get("formal_model_mode") not in {"fixed_k_v1", "variable_k_v1"}:
        raise ValueError("formal sampler candidate mode is invalid")
    expected_output_dimension = 6 if candidate.get("formal_model_mode") == "fixed_k_v1" else 7
    if candidate.get("formal_output_dimension") != expected_output_dimension:
        raise ValueError("formal sampler candidate output dimension is invalid")
    if candidate.get("producer_selected_rate_hz") is not None:
        raise ValueError("formal sampler producer may not self-select a rate")
    if candidate.get("producer_selection_eligible") is not False:
        raise ValueError("formal sampler producer may not claim selection eligibility")
    if candidate.get("active_enabled") is not False or candidate.get("shadow_only") is not True:
        raise ValueError("formal sampler candidate must remain inactive and shadow-only")
    duration = float(candidate.get("duration_per_rate_s", math.nan))
    if not math.isfinite(duration) or duration <= 0.0:
        raise ValueError("formal sampler duration is invalid")
    bindings = candidate.get("artifact_bindings")
    if not isinstance(bindings, Mapping):
        raise ValueError("formal sampler artifact bindings are missing")
    if _require_sha256(bindings.get("checkpoint_sha256"), "checkpoint_sha256") != _require_sha256(
        expected_checkpoint_sha256, "expected_checkpoint_sha256"
    ):
        raise ValueError("formal sampler checkpoint binding mismatch")
    if _require_sha256(bindings.get("dataset_sha256"), "dataset_sha256") != _require_sha256(
        expected_dataset_sha256, "expected_dataset_sha256"
    ):
        raise ValueError("formal sampler dataset binding mismatch")
    harness_hash = _require_sha256(bindings.get("harness_source_sha256"), "harness_source_sha256")
    if harness_hash != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("formal sampler harness source binding mismatch")
    runtime = bindings.get("runtime")
    required_runtime = {"python_version", "platform", "torch_version", "device", "predictor_class"}
    if not isinstance(runtime, Mapping) or not required_runtime <= set(runtime):
        raise ValueError("formal sampler runtime binding is incomplete")
    if not all(str(runtime[name]).strip() for name in required_runtime):
        raise ValueError("formal sampler runtime binding contains an empty value")

    results = candidate.get("rate_results")
    if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
        raise ValueError("formal sampler rate_results are missing")
    if candidate.get("rates_hz") != list(FORMAL_RATE_ORDER_HZ):
        raise ValueError("formal sampler rates must be exactly 100 and 50 Hz")
    recomputed: list[dict[str, object]] = []
    evidence: list[FormalModelTimingEvidence] = []
    seen_rates: set[int] = set()
    for raw_result in results:
        if not isinstance(raw_result, Mapping):
            raise ValueError("formal sampler rate result must be an object")
        rate_hz = int(raw_result.get("rate_hz", 0))
        if rate_hz not in FORMAL_RATE_ORDER_HZ or rate_hz in seen_rates:
            raise ValueError("formal sampler contains invalid or duplicate rate")
        seen_rates.add(rate_hz)
        period_s = float(raw_result.get("period_s", math.nan))
        if not math.isclose(period_s, 1.0 / rate_hz, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("formal sampler rate period mismatch")
        if not math.isclose(
            float(raw_result.get("declared_duration_s", math.nan)),
            duration,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError("formal sampler rate duration mismatch")
        if (
            raw_result.get("sampler_steps") != FORMAL_SAMPLER_STEPS
            or raw_result.get("paced_raw_ticks") is not True
        ):
            raise ValueError("formal sampler rate result is not a paced 50-step result")
        ticks = raw_result.get("raw_ticks")
        if not isinstance(ticks, Sequence) or isinstance(ticks, (str, bytes)):
            raise ValueError("formal sampler raw ticks are missing")
        expected_ticks = max(1, int(round(duration * rate_hz)))
        if len(ticks) != expected_ticks:
            raise ValueError("formal sampler raw tick count does not cover declared duration")
        trial_started_s = float(raw_result.get("trial_started_s", math.nan))
        trial_ended_s = float(raw_result.get("trial_ended_s", math.nan))
        if (
            not math.isfinite(trial_started_s)
            or not math.isfinite(trial_ended_s)
            or trial_ended_s < trial_started_s
        ):
            raise ValueError("formal sampler trial bounds are invalid")
        if not math.isclose(
            float(ticks[0].get("release_s", math.nan)),
            trial_started_s,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            raise ValueError("formal sampler first release is not trial-bound")
        if trial_ended_s < float(ticks[-1].get("end_s", math.nan)):
            raise ValueError("formal sampler trial end does not bind raw ticks")
        summary = _summarize_ticks(ticks, period_s=period_s)
        for tick in ticks:
            if (
                tick.get("sampler_steps_executed") != FORMAL_SAMPLER_STEPS
                or tick.get("sampler_step_trace_length") != FORMAL_SAMPLER_STEPS
                or tick.get("sampler_api") != "FormalDDPMPredictorV1.sample"
            ):
                raise ValueError("formal sampler tick does not prove the actual 50-step API")
        if raw_result.get("producer_summary") != summary:
            raise ValueError("formal sampler producer summary mismatch")
        timing = FormalModelTimingEvidence(
            rate_hz=rate_hz,
            duration_s=duration,
            p99_latency_s=float(summary["latency_p99_s"]),
            deadline_misses=int(summary["deadline_misses"]),
            nonfinite_outputs=int(summary["nonfinite_outputs"]),
        )
        evidence.append(timing)
        recomputed.append({"rate_hz": rate_hz, **summary, "accepted": timing.accepted})

    all_rates_tested = seen_rates == set(FORMAL_RATE_ORDER_HZ)
    selection = select_formal_model_rate(evidence)
    return {
        "schema_version": FORMAL_BENCHMARK_SCHEMA_VERSION,
        "validation_status": (
            "validated_formal_timing_manifest"
            if all_rates_tested
            else "validated_incomplete_formal_diagnostic"
        ),
        "selection_eligible": all_rates_tested,
        "selected_rate_hz": selection.selected_rate_hz if all_rates_tested else None,
        "duration_per_rate_s": duration,
        "all_rates_tested": all_rates_tested,
        "candidate_sha256": _canonical_sha256(candidate),
        "timing_evidence_sha256": selection.evidence_sha256,
        "artifact_bindings": dict(bindings),
        "recomputed_rate_results": sorted(
            recomputed,
            key=lambda item: FORMAL_RATE_ORDER_HZ.index(int(item["rate_hz"])),
        ),
        "active_enabled": False,
        "shadow_only": True,
    }


benchmark_actual_50_step_sampler = benchmark_formal_50_step_sampler
validate_formal_sampler_candidate = validate_formal_50_step_sampler_candidate


__all__ = [
    "FORMAL_BENCHMARK_SCHEMA_VERSION",
    "FORMAL_RATE_ORDER_HZ",
    "FORMAL_RAW_CANDIDATE_STATUS",
    "FORMAL_TIMING_MODE",
    "benchmark_actual_50_step_sampler",
    "benchmark_formal_50_step_sampler",
    "validate_formal_50_step_sampler_candidate",
    "validate_formal_sampler_candidate",
]
