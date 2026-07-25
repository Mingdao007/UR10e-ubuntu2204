"""Offline replay and shortest virtual-clock shadow promotion gate."""

from __future__ import annotations

from dataclasses import dataclass
import math
import hashlib
import json
from pathlib import Path
from typing import Sequence

from .raw_artifact import read_raw_frames, raw_artifact_sha256


@dataclass(frozen=True)
class ShadowTrace:
    name: str
    duration_s: float
    max_tracking_error_m: float
    max_force_norm_n: float
    nonfinite_count: int = 0


@dataclass(frozen=True)
class ShadowGateResult:
    offline_replay_passed: bool
    representative_traces: tuple[str, ...]
    total_virtual_duration_s: float
    active_allowed: bool
    sleep_calls: int
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class OfflineReplayMetrics:
    artifact_sha256: str
    row_count: int
    max_raw_force_norm_n: float
    max_filtered_force_norm_n: float
    max_filter_velocity: float
    p99_inference_latency_s: float
    passed: bool
    blockers: tuple[str, ...]


def evaluate_offline_replay(raw_artifact: str | Path, *, max_force_norm_n: float = 20.0) -> OfflineReplayMetrics:
    frames = read_raw_frames(raw_artifact)
    raw_norms = [math.sqrt(sum(value * value for value in frame.raw_f_df[:3])) for frame in frames]
    filtered_norms = [math.sqrt(sum(value * value for value in frame.filtered_f_ff[:3])) for frame in frames]
    velocities = [max(abs(value) for value in frame.filter_velocity) for frame in frames]
    latencies = sorted(frame.inference_latency_s for frame in frames)
    p99 = latencies[min(len(latencies) - 1, int(0.99 * len(latencies)))]
    blockers = []
    if not frames:
        blockers.append("empty_raw_artifact")
    if raw_norms and max(raw_norms) > max_force_norm_n:
        blockers.append("raw_force_norm_guard")
    return OfflineReplayMetrics(raw_artifact_sha256(raw_artifact), len(frames), max(raw_norms, default=0.0), max(filtered_norms, default=0.0), max(velocities, default=0.0), p99, not blockers, tuple(blockers))


def load_shadow_artifact(path: str | Path, *, expected_name: str) -> ShadowTrace:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    supplied_hash = payload.pop("artifact_sha256", None)
    canonical_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if payload.get("name") != expected_name or supplied_hash != canonical_hash:
        raise ValueError("shadow artifact identity/hash mismatch")
    return ShadowTrace(expected_name, float(payload["duration_s"]), float(payload["max_tracking_error_m"]), float(payload["max_force_norm_n"]), int(payload.get("nonfinite_count", 0)))


def run_offline_shadow_gate(*, offline_replay_passed: bool | None = None, offline_replay_artifact: str | Path | None = None, traces: Sequence[ShadowTrace] = (), shadow_artifact_paths: Sequence[str | Path] = (), max_error_m: float = 0.01, max_force_norm_n: float = 20.0) -> ShadowGateResult:
    if shadow_artifact_paths:
        if len(shadow_artifact_paths) != 2:
            raise ValueError("promotion requires exactly two hash-validated shadow artifact paths")
        traces = (
            load_shadow_artifact(shadow_artifact_paths[0], expected_name="smooth_low_curvature"),
            load_shadow_artifact(shadow_artifact_paths[1], expected_name="turning_high_curvature"),
        )
    elif traces:
        # In-memory values are useful for diagnostics but can never authorize
        # promotion; only the artifact-path entry point proves lineage.
        blockers = ["shadow_artifact_paths_missing"]
        if offline_replay_artifact is None:
            blockers.append("offline_replay_artifact_missing")
        return ShadowGateResult(False, tuple(trace.name for trace in traces), sum(trace.duration_s for trace in traces), False, 0, tuple(blockers))
    by_name = {trace.name: trace for trace in traces}
    blockers: list[str] = []
    replay_passed = False
    if offline_replay_artifact is not None:
        replay_passed = evaluate_offline_replay(offline_replay_artifact, max_force_norm_n=max_force_norm_n).passed
        if not replay_passed:
            blockers.append("offline_replay_metrics_failed")
    else:
        blockers.append("offline_replay_artifact_missing")
    for required in ("smooth_low_curvature", "turning_high_curvature"):
        if required not in by_name:
            blockers.append(f"missing_{required}")
    for trace in traces:
        if trace.duration_s <= 0.0 or not math.isfinite(trace.duration_s):
            blockers.append(f"invalid_duration:{trace.name}")
        if trace.max_tracking_error_m > max_error_m or trace.max_force_norm_n > max_force_norm_n or trace.nonfinite_count:
            blockers.append(f"metric_failed:{trace.name}")
    total_duration = sum(trace.duration_s for trace in traces)
    if not 80.0 <= total_duration <= 100.0:
        blockers.append("shadow_duration_not_about_90s")
    if offline_replay_artifact is None and offline_replay_passed is False:
        blockers.append("offline_replay_failed")
    return ShadowGateResult(replay_passed, tuple(by_name), total_duration, not blockers, 0, tuple(blockers))


def run_offline_shadow_artifact_gate(*, offline_replay_artifact: str | Path, low_curvature_artifact: str | Path, high_curvature_artifact: str | Path, max_error_m: float = 0.01, max_force_norm_n: float = 20.0) -> ShadowGateResult:
    traces = (
        load_shadow_artifact(low_curvature_artifact, expected_name="smooth_low_curvature"),
        load_shadow_artifact(high_curvature_artifact, expected_name="turning_high_curvature"),
    )
    return run_offline_shadow_gate(offline_replay_artifact=offline_replay_artifact, shadow_artifact_paths=(low_curvature_artifact, high_curvature_artifact), max_error_m=max_error_m, max_force_norm_n=max_force_norm_n)
