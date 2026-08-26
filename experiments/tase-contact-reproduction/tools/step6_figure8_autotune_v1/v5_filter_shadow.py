"""Artifact-only force-filter diagnostics for Autotuner V5.

This module never opens a sensor, RTDE connection, or controller transport.
It cold-indexes an already sealed ``.r013life`` artifact and writes an
observation-only comparison of the deployed actual-dt one-pole filter with
same-cutoff two-pole Bessel and Butterworth candidates.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import time
from types import MappingProxyType
from typing import Any, Iterator, Mapping

from .v5_lifecycle_ledger import V5LifecycleLedgerError, index_sealed_r013_artifact


FILTER_SHADOW_SCHEMA = "step6.autotune/force-filter-shadow-v1"
FILTER_SHADOW_VERSION = 1
FILTER_JOB_SCHEMA = "step6.autotune/force-filter-shadow-job-v1"
FILTER_JOB_VERSION = 1
FILTER_QUEUE_SCHEMA = "step6.autotune/force-filter-shadow-queue-receipt-v1"
FILTER_QUEUE_VERSION = 1
DEFAULT_TAU_S = 0.04375
QUEUE_CAPACITY = 2
GENESIS_SHA256 = "0" * 64
FORBIDDEN_CONSUMERS = (
    "controller",
    "arm",
    "censor",
    "tell_exact",
    "gp_training",
    "promotion",
)


class V5FilterShadowError(RuntimeError):
    """The artifact-only filter-shadow contract is invalid."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha(value: Any) -> str:
    return _sha_bytes(_canonical(value))


def _require_sha(value: Any, name: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise V5FilterShadowError(f"{name} is not a lowercase SHA-256")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise V5FilterShadowError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V5FilterShadowError(f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise V5FilterShadowError(f"{name} must be finite")
    return result


def _resolved_regular_input(path: Path | str, *, suffix: str | None = None) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise V5FilterShadowError("filter-shadow input must be a regular file")
    resolved = candidate.resolve(strict=True)
    if suffix is not None and resolved.suffix != suffix:
        raise V5FilterShadowError(f"filter-shadow input must end with {suffix}")
    return resolved


def _atomic_json(path: Path, value: Mapping[str, Any]) -> tuple[str, int]:
    target = Path(path)
    if target.is_symlink() or target.parent.is_symlink():
        raise V5FilterShadowError("filter-shadow output must not use symlinks")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = _canonical(dict(value)) + b"\n"
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise V5FilterShadowError("filter-shadow temporary output already exists")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        descriptor = os.open(str(target.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
    return _sha_bytes(encoded), len(encoded)


@dataclass
class _SecondOrderState:
    output: float
    velocity: float = 0.0

    def advance(self, sample: float, dt_s: float, *, omega_n: float, damping_coefficient: float) -> float:
        if dt_s <= 0.0:
            return self.output
        alpha = 0.5 * damping_coefficient * omega_n
        beta_squared = omega_n * omega_n - alpha * alpha
        if beta_squared <= 0.0:
            raise V5FilterShadowError("second-order candidate is not underdamped")
        beta = math.sqrt(beta_squared)
        displacement = self.output - sample
        decay = math.exp(-alpha * dt_s)
        cosine = math.cos(beta * dt_s)
        sine = math.sin(beta * dt_s)
        next_displacement = decay * (
            displacement * cosine
            + (self.velocity + alpha * displacement) * sine / beta
        )
        next_velocity = decay * (
            self.velocity * cosine
            - (alpha * self.velocity + omega_n * omega_n * displacement) * sine / beta
        )
        self.output = sample + next_displacement
        self.velocity = next_velocity
        return self.output


def _second_order_parameters(tau_s: float, family: str) -> tuple[float, float]:
    cutoff_rad_s = 1.0 / tau_s
    if family == "butterworth2":
        return cutoff_rad_s, math.sqrt(2.0)
    if family == "bessel2":
        damping = math.sqrt(3.0)
        cutoff_ratio_squared = (math.sqrt(5.0) - 1.0) / 2.0
        return cutoff_rad_s / math.sqrt(cutoff_ratio_squared), damping
    raise V5FilterShadowError("unknown second-order filter family")


def _summary(values: tuple[float, ...]) -> dict[str, float]:
    if not values:
        raise V5FilterShadowError("filter-shadow series is empty")
    return {
        "min_n": min(values),
        "max_n": max(values),
        "mean_n": math.fsum(values) / len(values),
        "rms_n": math.sqrt(math.fsum(value * value for value in values) / len(values)),
    }


def build_force_filter_shadow(
    artifact_path: Path | str,
    receipt: Mapping[str, Any],
    event_bundle: Mapping[str, Any],
    output_path: Path | str,
    *,
    tau_s: float = DEFAULT_TAU_S,
) -> dict[str, Any]:
    """Cold-read one sealed lifecycle artifact and write shadow diagnostics."""

    source = _resolved_regular_input(artifact_path, suffix=".r013life")
    tau = _finite(tau_s, "filter tau")
    if not 0.001 <= tau <= 1.0:
        raise V5FilterShadowError("filter tau is outside the bounded offline domain")
    try:
        artifact = index_sealed_r013_artifact(source, receipt, event_bundle)
    except V5LifecycleLedgerError as exc:
        raise V5FilterShadowError("sealed R013 artifact verification failed") from exc

    rows: list[dict[str, Any]] = []
    raw_values: list[float] = []
    deployed_values: list[float] = []
    one_pole_values: list[float] = []
    bessel_values: list[float] = []
    butterworth_values: list[float] = []
    positive_dt: list[float] = []
    previous_time: float | None = None
    one_pole = 0.0
    bessel: _SecondOrderState | None = None
    butterworth: _SecondOrderState | None = None
    bessel_omega, bessel_damping = _second_order_parameters(tau, "bessel2")
    butter_omega, butter_damping = _second_order_parameters(tau, "butterworth2")

    for source_row in artifact.rows:
        monotonic_s = _finite(source_row["monotonic_s"], "row monotonic time")
        raw = _finite(source_row["normal_load_n"], "raw signed normal")
        deployed = _finite(source_row["filtered_normal_n"], "deployed filtered normal")
        dt_s = 0.0 if previous_time is None else monotonic_s - previous_time
        if dt_s < 0.0:
            raise V5FilterShadowError("artifact monotonic clock regressed")
        if previous_time is None:
            one_pole = raw
            bessel = _SecondOrderState(raw)
            butterworth = _SecondOrderState(raw)
        else:
            if dt_s > 0.0:
                positive_dt.append(dt_s)
            alpha = 1.0 - math.exp(-dt_s / tau)
            one_pole = (1.0 - alpha) * one_pole + alpha * raw
        assert bessel is not None and butterworth is not None
        bessel_value = bessel.advance(raw, dt_s, omega_n=bessel_omega, damping_coefficient=bessel_damping)
        butter_value = butterworth.advance(raw, dt_s, omega_n=butter_omega, damping_coefficient=butter_damping)
        row = {
            "sample_index": int(source_row["sample_index"]),
            "monotonic_s": monotonic_s,
            "rtde_timestamp_s": _finite(source_row["rtde_timestamp_s"], "row RTDE time"),
            "raw_signed_normal_n": raw,
            "deployed_filtered_normal_n": deployed,
            "actual_dt_one_pole_n": one_pole,
            "same_cutoff_bessel2_n": bessel_value,
            "same_cutoff_butterworth2_n": butter_value,
        }
        rows.append(row)
        raw_values.append(raw)
        deployed_values.append(deployed)
        one_pole_values.append(one_pole)
        bessel_values.append(bessel_value)
        butterworth_values.append(butter_value)
        previous_time = monotonic_s

    if not rows:
        raise V5FilterShadowError("sealed artifact contains no rows")
    deployed_error = tuple(deployed - recomputed for deployed, recomputed in zip(deployed_values, one_pole_values, strict=True))
    content: dict[str, Any] = {
        "schema": FILTER_SHADOW_SCHEMA,
        "version": FILTER_SHADOW_VERSION,
        "source": {
            "artifact_path": artifact.artifact_path,
            "artifact_sha256": artifact.artifact_sha256,
            "artifact_size": artifact.artifact_size,
            "row_count": len(artifact.rows),
            "event_bundle_sha256": _sha(dict(artifact.event_bundle)),
        },
        "contract": {
            "input_authority": "sealed_r013life_artifact_only",
            "raw_signal": "normal_load_n",
            "deployed_signal": "filtered_normal_n",
            "deployed_filter_unchanged": True,
            "tau_s": tau,
            "cutoff_rad_s": 1.0 / tau,
            "actual_dt": True,
            "notch_50_hz_enabled": False,
            "authority": "observation_only",
            "raw_signal_authority": ["safety", "evidence"],
            "forbidden_consumers": list(FORBIDDEN_CONSUMERS),
        },
        "clock": {
            "sample_count": len(rows),
            "monotonic_start_s": rows[0]["monotonic_s"],
            "monotonic_end_s": rows[-1]["monotonic_s"],
            "positive_dt_count": len(positive_dt),
            "positive_dt_min_s": None if not positive_dt else min(positive_dt),
            "positive_dt_median_s": None if not positive_dt else statistics.median(positive_dt),
            "positive_dt_max_s": None if not positive_dt else max(positive_dt),
        },
        "summary": {
            "raw_signed_normal": _summary(tuple(raw_values)),
            "deployed_filtered": _summary(tuple(deployed_values)),
            "actual_dt_one_pole": _summary(tuple(one_pole_values)),
            "same_cutoff_bessel2": _summary(tuple(bessel_values)),
            "same_cutoff_butterworth2": _summary(tuple(butterworth_values)),
            "deployed_vs_recomputed_one_pole_rmse_n": math.sqrt(math.fsum(value * value for value in deployed_error) / len(deployed_error)),
            "deployed_vs_recomputed_one_pole_max_abs_n": max(abs(value) for value in deployed_error),
        },
        "rows": rows,
    }
    content["content_sha256"] = _sha(content)
    output = Path(output_path)
    output_sha256, output_size = _atomic_json(output, content)
    return {
        "schema": "step6.autotune/force-filter-shadow-build-receipt-v1",
        "version": 1,
        "status": "COMPLETE",
        "source_artifact_sha256": artifact.artifact_sha256,
        "output_path": str(output.resolve(strict=True)),
        "output_sha256": output_sha256,
        "output_size": output_size,
        "content_sha256": content["content_sha256"],
        "sample_count": len(rows),
        "authority": "observation_only",
    }


def cold_verify_force_filter_shadow(
    output_path: Path | str,
    build_receipt: Mapping[str, Any],
    *,
    expected_source_artifact_sha256: str | None = None,
) -> Mapping[str, Any]:
    """Cold-verify shadow bytes and all authority-critical fields."""

    output = _resolved_regular_input(output_path)
    encoded = output.read_bytes()
    expected_receipt_keys = {
        "schema",
        "version",
        "status",
        "source_artifact_sha256",
        "output_path",
        "output_sha256",
        "output_size",
        "content_sha256",
        "sample_count",
        "authority",
    }
    if not isinstance(build_receipt, Mapping) or set(build_receipt) != expected_receipt_keys:
        raise V5FilterShadowError("filter-shadow build receipt schema differs")
    if build_receipt.get("schema") != "step6.autotune/force-filter-shadow-build-receipt-v1" or build_receipt.get("version") != 1 or build_receipt.get("status") != "COMPLETE" or build_receipt.get("authority") != "observation_only":
        raise V5FilterShadowError("filter-shadow build receipt state differs")
    if Path(str(build_receipt.get("output_path"))).resolve() != output or build_receipt.get("output_size") != len(encoded) or build_receipt.get("output_sha256") != _sha_bytes(encoded):
        raise V5FilterShadowError("filter-shadow output bytes differ from receipt")
    source_sha = _require_sha(build_receipt.get("source_artifact_sha256"), "filter-shadow source artifact hash")
    if expected_source_artifact_sha256 is not None and source_sha != _require_sha(expected_source_artifact_sha256, "expected source artifact hash"):
        raise V5FilterShadowError("filter-shadow source artifact identity differs")
    try:
        value = json.loads(encoded)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise V5FilterShadowError("filter-shadow output is invalid JSON") from exc
    expected_keys = {"schema", "version", "source", "contract", "clock", "summary", "rows", "content_sha256"}
    if not isinstance(value, dict) or set(value) != expected_keys or value.get("schema") != FILTER_SHADOW_SCHEMA or value.get("version") != FILTER_SHADOW_VERSION:
        raise V5FilterShadowError("filter-shadow output schema differs")
    content_hash = value.pop("content_sha256")
    if content_hash != _sha(value) or content_hash != build_receipt.get("content_sha256"):
        raise V5FilterShadowError("filter-shadow content hash differs")
    value["content_sha256"] = content_hash
    source = value.get("source")
    contract = value.get("contract")
    rows = value.get("rows")
    if not isinstance(source, Mapping) or source.get("artifact_sha256") != source_sha or not isinstance(contract, Mapping) or not isinstance(rows, list):
        raise V5FilterShadowError("filter-shadow source/contract/rows differ")
    if contract.get("input_authority") != "sealed_r013life_artifact_only" or contract.get("notch_50_hz_enabled") is not False or contract.get("authority") != "observation_only" or tuple(contract.get("forbidden_consumers", ())) != FORBIDDEN_CONSUMERS:
        raise V5FilterShadowError("filter-shadow authority contract differs")
    if build_receipt.get("sample_count") != len(rows) or source.get("row_count") != len(rows) or not rows:
        raise V5FilterShadowError("filter-shadow row count differs")
    previous_time = -math.inf
    expected_row_keys = {"sample_index", "monotonic_s", "rtde_timestamp_s", "raw_signed_normal_n", "deployed_filtered_normal_n", "actual_dt_one_pole_n", "same_cutoff_bessel2_n", "same_cutoff_butterworth2_n"}
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != expected_row_keys or row.get("sample_index") != index:
            raise V5FilterShadowError("filter-shadow row schema/index differs")
        current_time = _finite(row.get("monotonic_s"), "filter-shadow row monotonic time")
        if current_time < previous_time:
            raise V5FilterShadowError("filter-shadow row clock regressed")
        for field in expected_row_keys - {"sample_index", "monotonic_s"}:
            _finite(row.get(field), f"filter-shadow row {field}")
        previous_time = current_time
    return MappingProxyType(value)


@dataclass(frozen=True)
class FilterShadowJobV1:
    artifact_path: str
    receipt: Mapping[str, Any]
    event_bundle: Mapping[str, Any]
    output_path: str
    tau_s: float = DEFAULT_TAU_S
    schema: str = FILTER_JOB_SCHEMA
    version: int = FILTER_JOB_VERSION

    def __post_init__(self) -> None:
        if self.schema != FILTER_JOB_SCHEMA or self.version != FILTER_JOB_VERSION:
            raise V5FilterShadowError("filter-shadow job schema/version differs")
        if not isinstance(self.receipt, Mapping) or not isinstance(self.event_bundle, Mapping):
            raise V5FilterShadowError("filter-shadow job evidence is not typed")
        tau = _finite(self.tau_s, "filter-shadow job tau")
        if not 0.001 <= tau <= 1.0:
            raise V5FilterShadowError("filter-shadow job tau is outside bounds")
        object.__setattr__(self, "tau_s", tau)
        object.__setattr__(self, "receipt", dict(self.receipt))
        object.__setattr__(self, "event_bundle", dict(self.event_bundle))

    @property
    def job_id(self) -> str:
        return _sha({
            "artifact_path": str(Path(self.artifact_path).resolve()),
            "artifact_sha256": self.receipt.get("artifact_sha256"),
            "output_path": str(Path(self.output_path).resolve()),
            "tau_s": self.tau_s,
        })

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "job_id": self.job_id,
            "artifact_path": str(Path(self.artifact_path).resolve()),
            "receipt": dict(self.receipt),
            "event_bundle": dict(self.event_bundle),
            "output_path": str(Path(self.output_path).resolve()),
            "tau_s": self.tau_s,
            "authority": "observation_only",
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "FilterShadowJobV1":
        expected = {"schema", "version", "job_id", "artifact_path", "receipt", "event_bundle", "output_path", "tau_s", "authority"}
        if not isinstance(value, Mapping) or set(value) != expected or value.get("authority") != "observation_only":
            raise V5FilterShadowError("filter-shadow job mapping differs")
        try:
            job = cls(
                str(value["artifact_path"]),
                value["receipt"],
                value["event_bundle"],
                str(value["output_path"]),
                value["tau_s"],
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5FilterShadowError("filter-shadow job mapping is invalid") from exc
        if value.get("job_id") != job.job_id:
            raise V5FilterShadowError("filter-shadow job identity differs")
        return job


class FilterShadowJobQueueV1:
    """Capacity-two filesystem inbox for an isolated post-Home worker."""

    def __init__(self, root: Path | str, *, capacity: int = QUEUE_CAPACITY) -> None:
        if type(capacity) is not int or capacity != QUEUE_CAPACITY:
            raise V5FilterShadowError("filter-shadow queue capacity must remain two")
        self.root = Path(root)
        if self.root.is_symlink():
            raise V5FilterShadowError("filter-shadow queue root must not be a symlink")
        self.capacity = capacity
        self.pending = self.root / "pending"
        self.inflight = self.root / "inflight"
        self.completed = self.root / "completed"
        self.failed = self.root / "failed"
        for directory in (self.pending, self.inflight, self.completed, self.failed):
            if directory.is_symlink():
                raise V5FilterShadowError("filter-shadow queue directory must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".queue.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _matches(self, job_id: str) -> tuple[Path, ...]:
        return tuple(directory / f"{job_id}.json" for directory in (self.pending, self.inflight, self.completed, self.failed))

    def submit(self, job: FilterShadowJobV1) -> dict[str, Any]:
        """Submit without reading the artifact or waiting for the worker."""

        if not isinstance(job, FilterShadowJobV1):
            raise V5FilterShadowError("filter-shadow submit requires a typed job")
        with self._lock():
            existing = next((path for path in self._matches(job.job_id) if path.exists()), None)
            if existing is not None:
                disposition = "DUPLICATE"
                accepted = True
            elif len(tuple(self.pending.glob("*.json"))) + len(tuple(self.inflight.glob("*.json"))) >= self.capacity:
                disposition = "DROP_NEWEST"
                accepted = False
            else:
                target = self.pending / f"{job.job_id}.json"
                _atomic_json(target, {**job.as_dict(), "submitted_monotonic_ns": time.monotonic_ns()})
                disposition = "ENQUEUED"
                accepted = True
        return {
            "schema": FILTER_QUEUE_SCHEMA,
            "version": FILTER_QUEUE_VERSION,
            "job_id": job.job_id,
            "accepted": accepted,
            "disposition": disposition,
            "capacity": self.capacity,
            "overflow_policy": "drop_newest",
            "physical_campaign_dependency": False,
        }

    def run_one(self) -> dict[str, Any] | None:
        """Process one job; failures become receipts and never escape."""

        with self._lock():
            candidates = sorted(self.pending.glob("*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
            if not candidates:
                return None
            pending = candidates[0]
            inflight = self.inflight / pending.name
            os.replace(pending, inflight)
        job_id = inflight.stem
        started = time.monotonic_ns()
        try:
            value = json.loads(inflight.read_text(encoding="utf-8"))
            expected = set(FilterShadowJobV1.from_mapping({key: item for key, item in value.items() if key != "submitted_monotonic_ns"}).as_dict()) | {"submitted_monotonic_ns"}
            if set(value) != expected or type(value["submitted_monotonic_ns"]) is not int:
                raise V5FilterShadowError("queued filter-shadow job schema differs")
            job = FilterShadowJobV1.from_mapping({key: item for key, item in value.items() if key != "submitted_monotonic_ns"})
            build = build_force_filter_shadow(job.artifact_path, job.receipt, job.event_bundle, job.output_path, tau_s=job.tau_s)
            status = {
                "schema": "step6.autotune/force-filter-shadow-job-status-v1",
                "version": 1,
                "job_id": job_id,
                "status": "COMPLETE",
                "build": build,
                "error_type": None,
                "error": None,
                "started_monotonic_ns": started,
                "ended_monotonic_ns": time.monotonic_ns(),
                "physical_campaign_dependency": False,
            }
            destination = self.completed / inflight.name
        except Exception as exc:  # noqa: BLE001 -- isolation boundary is intentional
            status = {
                "schema": "step6.autotune/force-filter-shadow-job-status-v1",
                "version": 1,
                "job_id": job_id,
                "status": "FAILED",
                "build": None,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "started_monotonic_ns": started,
                "ended_monotonic_ns": time.monotonic_ns(),
                "physical_campaign_dependency": False,
            }
            destination = self.failed / inflight.name
        _atomic_json(destination, status)
        if inflight.exists() and not inflight.is_symlink():
            inflight.unlink()
        return status


__all__ = [
    "DEFAULT_TAU_S",
    "FILTER_SHADOW_SCHEMA",
    "FilterShadowJobQueueV1",
    "FilterShadowJobV1",
    "QUEUE_CAPACITY",
    "V5FilterShadowError",
    "build_force_filter_shadow",
    "cold_verify_force_filter_shadow",
]
