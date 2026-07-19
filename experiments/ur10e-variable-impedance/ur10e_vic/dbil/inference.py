"""Hash-bound PyTorch predictor implementing the DBIL shadow protocol."""

from __future__ import annotations

from pathlib import Path
import platform
import sys
import time
from typing import Any, Mapping

import numpy as np

from ..contracts import ImpedanceObservation, PoseSample
from ..policies import DBILPrediction
from .config import DBILConfig
from .dataset import DatasetArrays, DatasetStats, SPLIT_NAMES, sha256_file
from .model import ConditionalDiffusionTransformer, require_torch, sample_s_zft
from .timing import (
    RATE_CANDIDATES_HZ,
    RAW_CANDIDATE_STATUS,
    RAW_PACED_TIMING_SCHEMA_VERSION,
)


class TorchDBILShadowPredictor:
    def __init__(
        self,
        checkpoint_path: Path,
        stats_path: Path,
        *,
        expected_checkpoint_sha256: str,
        expected_stats_sha256: str,
        device: str = "auto",
    ) -> None:
        framework = require_torch()
        if sha256_file(checkpoint_path) != expected_checkpoint_sha256:
            raise ValueError("checkpoint hash mismatch")
        if sha256_file(stats_path) != expected_stats_sha256:
            raise ValueError("statistics hash mismatch")
        checkpoint = framework.load(checkpoint_path, map_location="cpu")
        if checkpoint.get("active_enabled") is not False:
            raise ValueError("checkpoint does not retain active-disabled gate")
        self.config = DBILConfig.from_mapping(checkpoint["config"])
        self.dataset_hash = checkpoint["dataset_sha256"]
        self.training_mode = checkpoint.get("training_mode", "unknown")
        if checkpoint.get("stats_sha256") != expected_stats_sha256:
            raise ValueError("checkpoint is not bound to supplied statistics")
        self.stats = DatasetStats.from_path(stats_path)
        self.model = ConditionalDiffusionTransformer(self.config)
        self.model.load_state_dict(checkpoint["model_state"])
        if device == "auto":
            if framework.backends.mps.is_available():
                device = "mps"
            elif framework.cuda.is_available():
                device = "cuda"
            else:
                device = "cpu"
        self.device = framework.device(device)
        self.model.to(self.device)
        self.model.eval()
        self.model_hash = expected_checkpoint_sha256
        self.stats_hash = expected_stats_sha256

    def predict_window_arrays(
        self, pose_history: np.ndarray, wrench_history: np.ndarray
    ) -> np.ndarray:
        framework = require_torch()
        if pose_history.shape != (16, 7) or wrench_history.shape != (16, 6):
            raise ValueError("inference arrays must have shapes [16,7] and [16,6]")
        context = np.concatenate((pose_history, wrench_history), axis=-1)
        context = (context - np.asarray(self.stats.context_mean)) / np.asarray(
            self.stats.context_std
        )
        tensor = framework.as_tensor(
            context[None, :, :], dtype=framework.float32, device=self.device
        )
        normalized_window = sample_s_zft(self.model, tensor, self.config)[0].cpu().numpy()
        return normalized_window * np.asarray(self.stats.target_std) + np.asarray(
            self.stats.target_mean
        )

    def predict(self, observation: ImpedanceObservation) -> DBILPrediction:
        pose = np.asarray(
            [
                list(sample.position_m) + list(sample.quaternion_wxyz)
                for sample in observation.pose_history
            ],
            dtype=np.float32,
        )
        wrench = np.asarray(observation.wrench_history, dtype=np.float32)
        predicted_window = self.predict_window_arrays(pose, wrench)
        predicted = predicted_window[-1]
        return DBILPrediction(
            s_zft=PoseSample(predicted[:3], predicted[3:7]),
            confidence=1.0,
            model_hash=self.model_hash,
        )


def _synchronize(device: Any) -> None:
    framework = require_torch()
    if device.type == "mps":
        framework.mps.synchronize()
    elif device.type == "cuda":
        framework.cuda.synchronize(device)


def _runtime_binding(device: Any) -> dict[str, str]:
    try:
        framework = require_torch()
    except RuntimeError:
        framework = None
    device_type = str(device.type)
    if device_type == "cuda" and framework is not None:
        device_name = str(framework.cuda.get_device_name(device))
    elif device_type == "mps":
        device_name = "Apple Metal Performance Shaders"
    else:
        device_name = platform.processor() or platform.machine() or "cpu"
    return {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "torch_version": (
            str(framework.__version__) if framework is not None else "unavailable_test_double"
        ),
        "device_type": device_type,
        "device_name": device_name,
        "python_executable": sys.executable,
    }


def benchmark_shadow_predictor(
    predictor: TorchDBILShadowPredictor,
    observation: ImpedanceObservation,
    *,
    duration_s: float = 60.0,
    warmup_iterations: int = 5,
) -> dict[str, object]:
    if duration_s < 60.0:
        raise ValueError("DBIL timing evidence must run for at least 60 seconds")
    if warmup_iterations < 1:
        raise ValueError("warmup_iterations must be positive")
    for _ in range(warmup_iterations):
        predictor.predict(observation)
    _synchronize(predictor.device)
    latencies: list[float] = []
    nonfinite_outputs = 0
    started = time.perf_counter()
    while time.perf_counter() - started < duration_s:
        tick_started = time.perf_counter()
        prediction = predictor.predict(observation)
        _synchronize(predictor.device)
        latencies.append(time.perf_counter() - tick_started)
        values = prediction.s_zft.position_m + prediction.s_zft.quaternion_wxyz
        if not np.isfinite(values).all():
            nonfinite_outputs += 1
    actual_duration = time.perf_counter() - started
    latency_array = np.asarray(latencies)
    # One free-running throughput loop is not three independently paced trials.
    # These counters are useful rejection diagnostics only and must never feed
    # rate selection, even if a fast future model happens to meet a threshold.
    diagnostics = [
        {
            "rate_hz": rate,
            "duration_s": actual_duration,
            "p99_latency_s": float(np.quantile(latency_array, 0.99)),
            "hypothetical_deadline_misses": int(
                np.count_nonzero(latency_array >= 1.0 / rate)
            ),
            "selection_eligible": False,
        }
        for rate in RATE_CANDIDATES_HZ
    ]
    return {
        "schema_version": 1,
        "timing_mode": "unpaced_throughput_diagnostic",
        "selection_eligible": False,
        "duration_s": actual_duration,
        "iterations": len(latencies),
        "device": str(predictor.device),
        "latency_first_s": float(latency_array[0]),
        "latency_p50_s": float(np.quantile(latency_array, 0.50)),
        "latency_p99_s": float(np.quantile(latency_array, 0.99)),
        "latency_max_s": float(latency_array.max()),
        "nonfinite_outputs": nonfinite_outputs,
        "rate_diagnostics": diagnostics,
        "selected_rate_hz": None,
        "shadow_only": True,
        "active_enabled": False,
    }


def benchmark_paced_shadow_predictor(
    predictor: TorchDBILShadowPredictor,
    observation: ImpedanceObservation,
    *,
    duration_per_rate_s: float = 60.0,
    warmup_iterations: int = 5,
    artifact_bindings: Mapping[str, Any],
) -> dict[str, object]:
    """Produce raw 200/100/50 Hz timing; never self-authorize selection."""

    if duration_per_rate_s < 60.0:
        raise ValueError("each paced DBIL rate trial must run at least 60 seconds")
    if warmup_iterations < 1:
        raise ValueError("warmup_iterations must be positive")
    rate_results: list[dict[str, object]] = []
    for rate in RATE_CANDIDATES_HZ:
        for _ in range(warmup_iterations):
            predictor.predict(observation)
        _synchronize(predictor.device)
        period = 1.0 / rate
        latencies: list[float] = []
        deadline_misses = 0
        skipped_releases = 0
        nonfinite_outputs = 0
        raw_ticks: list[dict[str, object]] = []
        started = time.perf_counter()
        ends = started + duration_per_rate_s
        tick = 0
        while time.perf_counter() < ends:
            release = started + tick * period
            now = time.perf_counter()
            if now < release:
                time.sleep(release - now)
                now = time.perf_counter()
            elif now >= release + period:
                skipped = int((now - release) // period)
                for skipped_index in range(tick, tick + skipped):
                    raw_ticks.append(
                        {
                            "tick_index": skipped_index,
                            "release_s": started + skipped_index * period,
                            "start_s": None,
                            "end_s": None,
                            "nonfinite_output": None,
                        }
                    )
                skipped_releases += skipped
                deadline_misses += skipped
                tick += skipped
                release = started + tick * period
            tick_started = time.perf_counter()
            if tick_started < release:
                time.sleep(release - tick_started)
                tick_started = time.perf_counter()
            prediction = predictor.predict(observation)
            _synchronize(predictor.device)
            finished = time.perf_counter()
            latencies.append(finished - tick_started)
            deadline_misses += int(finished >= release + period)
            values = prediction.s_zft.position_m + prediction.s_zft.quaternion_wxyz
            nonfinite = bool(not np.isfinite(values).all())
            nonfinite_outputs += int(nonfinite)
            raw_ticks.append(
                {
                    "tick_index": tick,
                    "release_s": release,
                    "start_s": tick_started,
                    "end_s": finished,
                    "nonfinite_output": nonfinite,
                }
            )
            tick += 1
        ended = time.perf_counter()
        actual_duration = ended - started
        latency_array = np.asarray(latencies, dtype=float)
        p99 = float(np.quantile(latency_array, 0.99))
        rate_results.append(
            {
                "rate_hz": rate,
                "period_s": period,
                "trial_started_s": started,
                "trial_ended_s": ended,
                "raw_ticks": raw_ticks,
                "producer_summary": {
                    "rate_hz": rate,
                    "duration_s": actual_duration,
                    "period_s": period,
                    "scheduled_ticks": tick,
                    "executed_ticks": len(latencies),
                    "skipped_releases": skipped_releases,
                    "deadline_misses": deadline_misses,
                    "nonfinite_outputs": nonfinite_outputs,
                    "latency_first_s": float(latency_array[0]),
                    "latency_p50_s": float(np.quantile(latency_array, 0.50)),
                    "latency_p99_s": p99,
                    "latency_max_s": float(latency_array.max()),
                    "accepted": (
                        deadline_misses == 0
                        and p99 <= 0.8 * period
                        and nonfinite_outputs == 0
                    ),
                },
            }
        )
    bindings = dict(artifact_bindings)
    bindings["runtime"] = _runtime_binding(predictor.device)
    return {
        "schema_version": RAW_PACED_TIMING_SCHEMA_VERSION,
        "timing_mode": "independent_wall_clock_paced_trials",
        "candidate_status": RAW_CANDIDATE_STATUS,
        "duration_per_rate_s": duration_per_rate_s,
        "device": str(predictor.device),
        "artifact_bindings": bindings,
        "rate_results": rate_results,
        "selected_rate_hz": None,
        "selection_eligible": False,
        "shadow_only": True,
        "active_enabled": False,
    }


def evaluate_shadow_predictor(
    predictor: TorchDBILShadowPredictor,
    arrays: DatasetArrays,
    *,
    dataset_path: Path,
    split_id: int = 2,
    max_samples: int | None = None,
) -> dict[str, object]:
    if sha256_file(dataset_path) != predictor.dataset_hash:
        raise ValueError("checkpoint is not bound to the supplied evaluation dataset")
    selected = arrays.select_split(split_id)
    count = len(selected) if max_samples is None else min(len(selected), max_samples)
    if count <= 0:
        raise ValueError("evaluation requires at least one sample")
    position_errors: list[np.ndarray] = []
    orientation_errors_deg: list[np.ndarray] = []
    for index in range(count):
        predicted = predictor.predict_window_arrays(
            selected.pose_history[index], selected.wrench_history[index]
        )
        target = selected.target_s_zft[index]
        position_errors.append(np.linalg.norm(predicted[:, :3] - target[:, :3], axis=1))
        predicted_q = predicted[:, 3:7]
        target_q = target[:, 3:7]
        predicted_q = predicted_q / np.maximum(
            np.linalg.norm(predicted_q, axis=1, keepdims=True), 1e-12
        )
        target_q = target_q / np.maximum(
            np.linalg.norm(target_q, axis=1, keepdims=True), 1e-12
        )
        dot = np.clip(np.abs(np.sum(predicted_q * target_q, axis=1)), 0.0, 1.0)
        orientation_errors_deg.append(np.degrees(2.0 * np.arccos(dot)))
    position = np.concatenate(position_errors)
    orientation = np.concatenate(orientation_errors_deg)
    return {
        "split": SPLIT_NAMES[split_id],
        "samples": count,
        "position_rmse_m": float(np.sqrt(np.mean(position * position))),
        "position_mean_m": float(np.mean(position)),
        "position_max_m": float(np.max(position)),
        "orientation_rmse_deg": float(
            np.sqrt(np.mean(orientation * orientation))
        ),
        "orientation_mean_deg": float(np.mean(orientation)),
        "orientation_max_deg": float(np.max(orientation)),
        "checkpoint_training_mode": predictor.training_mode,
        "claim_boundary": (
            "public-data subset metric only; not full-paper or hardware reproduction"
        ),
        "active_enabled": False,
    }
