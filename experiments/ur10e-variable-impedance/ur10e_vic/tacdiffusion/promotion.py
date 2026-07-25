"""Offline replay and shortest virtual-clock shadow promotion gate."""

from __future__ import annotations

from dataclasses import dataclass
import math
import hashlib
import json
import os
from tempfile import NamedTemporaryFile
from pathlib import Path
from typing import Mapping, Sequence

from .raw_artifact import read_raw_frames, raw_artifact_sha256


PROMOTION_MANIFEST_SCHEMA = "ur10e_tacdiffusion_promotion_manifest/v1"
SHADOW_NAMES = ("smooth_low_curvature", "turning_high_curvature")
SHADOW_DURATION_MIN_S = 40.0
SHADOW_DURATION_MAX_S = 50.0
BLOCKED_SHADOW_REASON = "shadow_artifacts_missing"
LIVE_AUTHORIZATION_SCHEMA = "ur10e_live_authorization/v3"
LIVE_AUTHORIZATION_FIELDS = frozenset({
    "schema",
    "explicit_live_authorization",
    "promotion_manifest_relative_path",
    "promotion_manifest_sha256",
    "checkpoint_binding_relative_path",
    "checkpoint_binding_sha256",
    "surface_calibration_sha256",
    "action_profile_sha256",
    "filter_profile_sha256",
    "normalization_sha256",
    "authorization_sha256",
})
PROMOTION_MANIFEST_FIELDS = frozenset({
    "schema",
    "checkpoint_binding_relative_path",
    "checkpoint_binding_sha256",
    "model_checkpoint_relative_path",
    "model_checkpoint_sha256",
    "surface_calibration_sha256",
    "action_profile_sha256",
    "filter_profile_sha256",
    "normalization_sha256",
    "raw_replay_relative_path",
    "raw_replay_artifact_sha256",
    "offline_replay_metrics",
    "representative_names",
    "shadow_artifacts",
    "total_virtual_duration_s",
    "blockers",
    "sleep_calls",
    "active_allowed",
    "manifest_sha256",
})
SHADOW_ENTRY_FIELDS = frozenset({"name", "relative_path", "sha256", "duration_s"})


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


@dataclass(frozen=True)
class LiveAuthorization:
    authorization_path: str
    promotion_manifest_path: str
    checkpoint_binding_path: str
    active_allowed: bool = True


def evaluate_offline_replay(raw_artifact: str | Path, *, max_force_norm_n: float = 20.0) -> OfflineReplayMetrics:
    frames = read_raw_frames(raw_artifact)
    artifact_hash = raw_artifact_sha256(raw_artifact)
    if not frames:
        return OfflineReplayMetrics(artifact_hash, 0, 0.0, 0.0, 0.0, 0.0, False, ("empty_raw_artifact",))
    raw_norms = [math.sqrt(sum(value * value for value in frame.raw_f_df[:3])) for frame in frames]
    filtered_norms = [math.sqrt(sum(value * value for value in frame.filtered_f_ff[:3])) for frame in frames]
    velocities = [max(abs(value) for value in frame.filter_velocity) for frame in frames]
    latencies = sorted(frame.inference_latency_s for frame in frames)
    p99 = latencies[min(len(latencies) - 1, int(0.99 * len(latencies)))]
    blockers = []
    if raw_norms and max(raw_norms) > max_force_norm_n:
        blockers.append("raw_force_norm_guard")
    return OfflineReplayMetrics(artifact_hash, len(frames), max(raw_norms, default=0.0), max(filtered_norms, default=0.0), max(velocities, default=0.0), p99, not blockers, tuple(blockers))


def load_shadow_artifact(path: str | Path, *, expected_name: str) -> ShadowTrace:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    supplied_hash = payload.pop("artifact_sha256", None)
    canonical_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if payload.get("name") != expected_name or supplied_hash != canonical_hash:
        raise ValueError("shadow artifact identity/hash mismatch")
    return ShadowTrace(expected_name, float(payload["duration_s"]), float(payload["max_tracking_error_m"]), float(payload["max_force_norm_n"]), int(payload.get("nonfinite_count", 0)))


def _load_eligible_shadow_artifact(path: str | Path, *, expected_name: str, max_error_m: float, max_force_norm_n: float, allow_fixture: bool = False) -> dict[str, object]:
    artifact_path = Path(path)
    try:
        payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("shadow artifact JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("shadow artifact must be an object")
    supplied_hash = payload.get("artifact_sha256")
    unsigned = dict(payload)
    unsigned.pop("artifact_sha256", None)
    canonical_hash = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if supplied_hash != canonical_hash:
        raise ValueError("shadow artifact self-hash mismatch")
    if payload.get("name") != expected_name:
        raise ValueError("shadow artifact name mismatch")
    if payload.get("capture_mode") != "live_shadow" or payload.get("hardware_run") is not True:
        raise ValueError("shadow artifact is not a live-shadow hardware artifact")
    if "nonfinite_count" not in payload or "nonfinite_rows" not in payload:
        raise ValueError("shadow artifact must declare nonfinite row counts")
    if payload.get("fixture") is True and not allow_fixture:
        raise ValueError("synthetic fixture is not eligible for active promotion")
    try:
        duration = float(payload["duration_s"])
        tracking_error = float(payload["max_tracking_error_m"])
        force_norm = float(payload["max_force_norm_n"])
        nonfinite_count = int(payload.get("nonfinite_count", 0))
        nonfinite_rows = int(payload.get("nonfinite_rows", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("shadow artifact metrics are malformed") from exc
    if not all(math.isfinite(value) for value in (duration, tracking_error, force_norm)):
        raise ValueError("shadow artifact metrics are non-finite")
    if not SHADOW_DURATION_MIN_S <= duration <= SHADOW_DURATION_MAX_S:
        raise ValueError("shadow artifact duration is not about 45 seconds")
    if nonfinite_count != 0 or nonfinite_rows != 0:
        raise ValueError("shadow artifact contains nonfinite rows")
    if tracking_error > max_error_m or force_norm > max_force_norm_n:
        raise ValueError("shadow artifact metrics fail thresholds")
    return payload


def _canonical_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _relative_path(path: Path, *, root: Path) -> str:
    return os.path.relpath(path.resolve(), root.resolve())


def _resolve_relative(path: str, *, root: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else root / candidate


def _shadow_path_pair(shadow_artifact_paths: Mapping[str, str | Path] | Sequence[str | Path]) -> tuple[Path, Path]:
    if isinstance(shadow_artifact_paths, Mapping):
        if set(shadow_artifact_paths) != set(SHADOW_NAMES):
            raise ValueError("promotion requires exactly the two named shadow artifacts")
        paths = tuple(Path(shadow_artifact_paths[name]) for name in SHADOW_NAMES)
    else:
        if len(shadow_artifact_paths) != 2:
            raise ValueError("promotion requires exactly two shadow artifact paths")
        paths = tuple(Path(value) for value in shadow_artifact_paths)
    if paths[0].resolve() == paths[1].resolve():
        raise ValueError("shadow artifact paths must be distinct")
    return paths[0], paths[1]


def _offline_metrics_payload(metrics: OfflineReplayMetrics) -> dict[str, object]:
    return {
        "artifact_sha256": metrics.artifact_sha256,
        "row_count": metrics.row_count,
        "max_raw_force_norm_n": metrics.max_raw_force_norm_n,
        "max_filtered_force_norm_n": metrics.max_filtered_force_norm_n,
        "max_filter_velocity": metrics.max_filter_velocity,
        "p99_inference_latency_s": metrics.p99_inference_latency_s,
        "passed": metrics.passed,
        "blockers": list(metrics.blockers),
    }


def _write_atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    data = (json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _build_promotion_manifest(
    *,
    manifest_path: Path,
    checkpoint_binding_path: Path,
    model_checkpoint_path: Path,
    raw_replay_artifact_path: Path,
    shadow_artifact_paths: Mapping[str, str | Path] | Sequence[str | Path],
    max_error_m: float,
    max_force_norm_n: float,
    allow_fixture: bool = False,
) -> dict[str, object]:
    from .checkpoint import validate_checkpoint_binding

    binding = validate_checkpoint_binding(checkpoint_binding_path)
    model_checkpoint_hash = hashlib.sha256(model_checkpoint_path.read_bytes()).hexdigest()
    if model_checkpoint_hash != binding.checkpoint_sha256:
        raise ValueError("model checkpoint hash does not match checkpoint binding")
    replay_metrics = evaluate_offline_replay(raw_replay_artifact_path, max_force_norm_n=max_force_norm_n)
    low_path, high_path = _shadow_path_pair(shadow_artifact_paths)
    shadows = []
    for name, shadow_path in zip(SHADOW_NAMES, (low_path, high_path)):
        payload = _load_eligible_shadow_artifact(shadow_path, expected_name=name, max_error_m=max_error_m, max_force_norm_n=max_force_norm_n, allow_fixture=True)
        shadows.append({
            "name": name,
            "relative_path": _relative_path(shadow_path, root=manifest_path.parent),
            "sha256": hashlib.sha256(shadow_path.read_bytes()).hexdigest(),
            "duration_s": float(payload["duration_s"]),
        })
    total_duration = sum(float(item["duration_s"]) for item in shadows)
    blockers = list(replay_metrics.blockers)
    if not replay_metrics.passed:
        blockers.append("offline_replay_metrics_failed")
    if any(_load_eligible_shadow_artifact(shadow_path, expected_name=name, max_error_m=max_error_m, max_force_norm_n=max_force_norm_n, allow_fixture=True).get("fixture") is True for name, shadow_path in zip(SHADOW_NAMES, (low_path, high_path))):
        blockers.append("synthetic_fixture_not_live_evidence")
    manifest: dict[str, object] = {
        "schema": PROMOTION_MANIFEST_SCHEMA,
        "checkpoint_binding_relative_path": _relative_path(checkpoint_binding_path, root=manifest_path.parent),
        "checkpoint_binding_sha256": hashlib.sha256(checkpoint_binding_path.read_bytes()).hexdigest(),
        "model_checkpoint_relative_path": _relative_path(model_checkpoint_path, root=manifest_path.parent),
        "model_checkpoint_sha256": model_checkpoint_hash,
        "surface_calibration_sha256": binding.surface_calibration_sha256,
        "action_profile_sha256": binding.action_profile_sha256,
        "filter_profile_sha256": binding.filter_profile_sha256,
        "normalization_sha256": binding.normalization_sha256,
        "raw_replay_relative_path": _relative_path(raw_replay_artifact_path, root=manifest_path.parent),
        "raw_replay_artifact_sha256": replay_metrics.artifact_sha256,
        "offline_replay_metrics": _offline_metrics_payload(replay_metrics),
        "representative_names": list(SHADOW_NAMES),
        "shadow_artifacts": shadows,
        "total_virtual_duration_s": total_duration,
        "blockers": blockers,
        "sleep_calls": 0,
        "active_allowed": not blockers,
    }
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    return manifest


def write_promotion_manifest(
    path: str | Path,
    *,
    checkpoint_binding_path: str | Path,
    model_checkpoint_path: str | Path,
    raw_replay_artifact_path: str | Path,
    shadow_artifact_paths: Mapping[str, str | Path] | Sequence[str | Path],
    max_error_m: float = 0.01,
    max_force_norm_n: float = 20.0,
) -> dict[str, object]:
    """Write a fully hash-linked promotion manifest; paths are never inferred from booleans."""

    manifest_path = Path(path)
    manifest = _build_promotion_manifest(
        manifest_path=manifest_path,
        checkpoint_binding_path=Path(checkpoint_binding_path),
        model_checkpoint_path=Path(model_checkpoint_path),
        raw_replay_artifact_path=Path(raw_replay_artifact_path),
        shadow_artifact_paths=shadow_artifact_paths,
        max_error_m=max_error_m,
        max_force_norm_n=max_force_norm_n,
    )
    _write_atomic_json(manifest_path, manifest)
    return manifest


def write_blocked_promotion_manifest(
    path: str | Path,
    *,
    checkpoint_binding_path: str | Path,
    model_checkpoint_path: str | Path,
    raw_replay_artifact_path: str | Path,
    reason: str = BLOCKED_SHADOW_REASON,
    max_force_norm_n: float = 20.0,
) -> dict[str, object]:
    """Record an offline blocked result without fabricating shadow evidence."""

    from .checkpoint import validate_checkpoint_binding

    manifest_path = Path(path)
    binding_path = Path(checkpoint_binding_path)
    model_path = Path(model_checkpoint_path)
    raw_path = Path(raw_replay_artifact_path)
    if reason != BLOCKED_SHADOW_REASON:
        raise ValueError(f"unsupported blocked promotion reason: {reason}")
    binding = validate_checkpoint_binding(binding_path)
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if model_hash != binding.checkpoint_sha256:
        raise ValueError("model checkpoint hash does not match checkpoint binding")
    replay_metrics = evaluate_offline_replay(raw_path, max_force_norm_n=max_force_norm_n)
    blockers = [reason]
    if not replay_metrics.passed:
        blockers.extend(replay_metrics.blockers)
    manifest: dict[str, object] = {
        "schema": PROMOTION_MANIFEST_SCHEMA,
        "checkpoint_binding_relative_path": _relative_path(binding_path, root=manifest_path.parent),
        "checkpoint_binding_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest(),
        "model_checkpoint_relative_path": _relative_path(model_path, root=manifest_path.parent),
        "model_checkpoint_sha256": model_hash,
        "surface_calibration_sha256": binding.surface_calibration_sha256,
        "action_profile_sha256": binding.action_profile_sha256,
        "filter_profile_sha256": binding.filter_profile_sha256,
        "normalization_sha256": binding.normalization_sha256,
        "raw_replay_relative_path": _relative_path(raw_path, root=manifest_path.parent),
        "raw_replay_artifact_sha256": replay_metrics.artifact_sha256,
        "offline_replay_metrics": _offline_metrics_payload(replay_metrics),
        "representative_names": [],
        "shadow_artifacts": [],
        "total_virtual_duration_s": 0.0,
        "blockers": blockers,
        "sleep_calls": 0,
        "active_allowed": False,
    }
    manifest["manifest_sha256"] = _canonical_hash(manifest)
    _write_atomic_json(manifest_path, manifest)
    return manifest


def validate_promotion_manifest(
    path: str | Path,
    *,
    max_error_m: float = 0.01,
    max_force_norm_n: float = 20.0,
) -> dict[str, object]:
    """Recompute every lineage/hash/metric field and reject any tamper."""

    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("promotion manifest JSON is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema") != PROMOTION_MANIFEST_SCHEMA:
        raise ValueError("promotion manifest schema is invalid")
    if set(payload) != PROMOTION_MANIFEST_FIELDS:
        raise ValueError("promotion manifest fields are missing or extra")
    supplied_manifest_hash = payload.get("manifest_sha256")
    unsigned = dict(payload)
    unsigned.pop("manifest_sha256", None)
    if supplied_manifest_hash != _canonical_hash(unsigned):
        raise ValueError("promotion manifest self-hash mismatch")
    if payload.get("sleep_calls") != 0:
        raise ValueError("promotion manifest sleep_calls must be zero")
    if not isinstance(payload.get("active_allowed"), bool) or not isinstance(payload.get("blockers"), list):
        raise ValueError("promotion manifest status fields are invalid")
    from .checkpoint import validate_checkpoint_binding

    binding_path = _resolve_relative(str(payload["checkpoint_binding_relative_path"]), root=manifest_path.parent)
    model_path = _resolve_relative(str(payload["model_checkpoint_relative_path"]), root=manifest_path.parent)
    raw_path = _resolve_relative(str(payload["raw_replay_relative_path"]), root=manifest_path.parent)
    binding = validate_checkpoint_binding(binding_path)
    if hashlib.sha256(binding_path.read_bytes()).hexdigest() != payload["checkpoint_binding_sha256"]:
        raise ValueError("checkpoint binding file hash mismatch")
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    if model_hash != payload["model_checkpoint_sha256"] or model_hash != binding.checkpoint_sha256:
        raise ValueError("model checkpoint hash mismatch")
    for field in ("surface_calibration_sha256", "action_profile_sha256", "filter_profile_sha256", "normalization_sha256"):
        if payload[field] != getattr(binding, field):
            raise ValueError(f"checkpoint lineage mismatch: {field}")
    replay_metrics = evaluate_offline_replay(raw_path, max_force_norm_n=max_force_norm_n)
    if payload["raw_replay_artifact_sha256"] != replay_metrics.artifact_sha256:
        raise ValueError("raw replay artifact hash mismatch")
    if payload["offline_replay_metrics"] != _offline_metrics_payload(replay_metrics):
        raise ValueError("offline replay metrics mismatch")
    shadows = payload.get("shadow_artifacts")
    representative_names = payload.get("representative_names")
    if not isinstance(representative_names, list) or not isinstance(shadows, list):
        raise ValueError("promotion representative fields are invalid")
    expected_blockers = list(replay_metrics.blockers)
    if not replay_metrics.passed and representative_names != []:
        expected_blockers.append("offline_replay_metrics_failed")
    if representative_names == [] and shadows == []:
        if float(payload.get("total_virtual_duration_s")) != 0.0:
            raise ValueError("blocked promotion total duration is invalid")
        expected_blockers.insert(0, BLOCKED_SHADOW_REASON)
    else:
        if representative_names != list(SHADOW_NAMES) or len(shadows) != 2:
            raise ValueError("promotion requires exactly two representative shadows")
        seen_names: set[str] = set()
        seen_paths: set[Path] = set()
        total_duration = 0.0
        fixture_seen = False
        for expected_name, entry in zip(SHADOW_NAMES, shadows):
            if not isinstance(entry, dict) or set(entry) != SHADOW_ENTRY_FIELDS or entry.get("name") != expected_name or expected_name in seen_names:
                raise ValueError("shadow artifact names are duplicate or swapped")
            seen_names.add(expected_name)
            shadow_path = _resolve_relative(str(entry["relative_path"]), root=manifest_path.parent)
            if shadow_path.resolve() in seen_paths:
                raise ValueError("shadow artifact paths must be distinct")
            seen_paths.add(shadow_path.resolve())
            actual_hash = hashlib.sha256(shadow_path.read_bytes()).hexdigest()
            if actual_hash != entry.get("sha256"):
                raise ValueError("shadow artifact file hash mismatch")
            artifact = _load_eligible_shadow_artifact(shadow_path, expected_name=expected_name, max_error_m=max_error_m, max_force_norm_n=max_force_norm_n, allow_fixture=not bool(payload.get("active_allowed")))
            fixture_seen = fixture_seen or artifact.get("fixture") is True
            if float(entry.get("duration_s")) != float(artifact["duration_s"]):
                raise ValueError("shadow artifact duration mismatch")
            total_duration += float(artifact["duration_s"])
        if abs(float(payload["total_virtual_duration_s"]) - total_duration) > 1e-12:
            raise ValueError("shadow virtual duration mismatch")
        if fixture_seen:
            expected_blockers.append("synthetic_fixture_not_live_evidence")
    if payload.get("blockers") != expected_blockers:
        raise ValueError("promotion blockers do not match recomputed status")
    expected_active = not expected_blockers
    if payload.get("active_allowed") != expected_active:
        raise ValueError("promotion active status does not match recomputed status")
    return payload


def validate_live_authorization(path: str | Path) -> LiveAuthorization:
    """Validate the only v3 path that can authorize model-active output."""

    if isinstance(path, bool):
        raise TypeError("live authorization requires a path, not a boolean")
    authorization_path = Path(path)
    try:
        payload = json.loads(authorization_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("live authorization artifact is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != LIVE_AUTHORIZATION_FIELDS:
        raise ValueError("live authorization fields are missing or extra")
    if payload.get("schema") != LIVE_AUTHORIZATION_SCHEMA or payload.get("explicit_live_authorization") is not True:
        raise ValueError("explicit v3 live authorization is required")
    supplied_hash = payload.get("authorization_sha256")
    unsigned = dict(payload)
    unsigned.pop("authorization_sha256", None)
    if supplied_hash != _canonical_hash(unsigned):
        raise ValueError("live authorization self-hash mismatch")
    promotion_relative = Path(str(payload["promotion_manifest_relative_path"]))
    binding_relative = Path(str(payload["checkpoint_binding_relative_path"]))
    if promotion_relative.is_absolute() or binding_relative.is_absolute():
        raise ValueError("live authorization artifact paths must be relative")
    promotion_path = _resolve_relative(str(promotion_relative), root=authorization_path.parent)
    binding_path = _resolve_relative(str(binding_relative), root=authorization_path.parent)
    if not promotion_path.is_file() or not binding_path.is_file():
        raise ValueError("promotion manifest and checkpoint binding artifacts are required")
    if hashlib.sha256(promotion_path.read_bytes()).hexdigest() != payload["promotion_manifest_sha256"]:
        raise ValueError("promotion manifest file hash mismatch")
    if hashlib.sha256(binding_path.read_bytes()).hexdigest() != payload["checkpoint_binding_sha256"]:
        raise ValueError("checkpoint binding file hash mismatch")
    promotion = validate_promotion_manifest(promotion_path)
    if promotion.get("active_allowed") is not True or promotion.get("blockers") != []:
        raise ValueError("promotion manifest is not active-allowed")
    promotion_binding_path = _resolve_relative(str(promotion["checkpoint_binding_relative_path"]), root=promotion_path.parent)
    if promotion_binding_path.resolve() != binding_path.resolve():
        raise ValueError("promotion checkpoint binding path mismatch")
    if promotion["checkpoint_binding_sha256"] != payload["checkpoint_binding_sha256"]:
        raise ValueError("promotion checkpoint binding hash mismatch")
    from .checkpoint import validate_checkpoint_binding

    binding = validate_checkpoint_binding(binding_path)
    for field in ("surface_calibration_sha256", "action_profile_sha256", "filter_profile_sha256", "normalization_sha256"):
        if payload[field] != getattr(binding, field) or promotion[field] != getattr(binding, field):
            raise ValueError(f"live authorization lineage mismatch: {field}")
    return LiveAuthorization(str(authorization_path), str(promotion_path), str(binding_path), True)


def run_offline_shadow_gate(*, offline_replay_passed: bool | None = None, offline_replay_artifact: str | Path | None = None, traces: Sequence[ShadowTrace] = (), shadow_artifact_paths: Sequence[str | Path] = (), max_error_m: float = 0.01, max_force_norm_n: float = 20.0) -> ShadowGateResult:
    if shadow_artifact_paths:
        if len(shadow_artifact_paths) != 2:
            raise ValueError("promotion requires exactly two hash-validated shadow artifact paths")
        try:
            eligible = tuple(
                _load_eligible_shadow_artifact(path, expected_name=name, max_error_m=max_error_m, max_force_norm_n=max_force_norm_n)
                for name, path in zip(SHADOW_NAMES, shadow_artifact_paths)
            )
        except ValueError as exc:
            return ShadowGateResult(False, tuple(), 0.0, False, 0, (f"shadow_artifact_invalid:{exc}",))
        traces = tuple(
            ShadowTrace(name, float(payload["duration_s"]), float(payload["max_tracking_error_m"]), float(payload["max_force_norm_n"]), int(payload.get("nonfinite_count", 0)))
            for name, payload in zip(SHADOW_NAMES, eligible)
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
    return run_offline_shadow_gate(offline_replay_artifact=offline_replay_artifact, shadow_artifact_paths=(low_curvature_artifact, high_curvature_artifact), max_error_m=max_error_m, max_force_norm_n=max_force_norm_n)
