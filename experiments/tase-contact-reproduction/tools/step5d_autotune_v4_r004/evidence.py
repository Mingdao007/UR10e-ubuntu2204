"""Layered path, motion and timing evidence primitives for r004."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .contracts import TARGET_FORCE_N
from .timing import MIN_SUCCESS_RATE_HZ, TimingError, TimingEvidence, TimingEvidenceCollector


class EvidenceError(RuntimeError):
    """Evidence is incomplete, nonfinite, or outside acceptance bounds."""


PATH_DURATION_MIN_S = 60.0
PATH_PHASE_REQUIRED = 6
XY_ERROR_P95_MAX_M = 0.0005
XY_ERROR_MAX_M = 0.001
ENDPOINT_ERROR_MAX_M = 0.001
QD_CORRELATION_MIN = 0.9
QD_LAG_MAX_S = 0.020


def _finite(value: Any, role: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError(f"{role} is not numeric") from exc
    if not math.isfinite(result):
        raise EvidenceError(f"{role} is nonfinite")
    return result


def _json_safe(value: Any) -> Any:
    """Replace nonfinite floats with None so allow_nan=False digests never crash the host."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _vector(value: Any, length: int, role: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EvidenceError(f"{role} is not a sequence")
    if len(value) != length:
        raise EvidenceError(f"{role} must have length {length}")
    result = tuple(_finite(item, f"{role}[{index}]") for index, item in enumerate(value))
    return result


def _coalesce(primary: Any, alias: Any, role: str) -> Any:
    if primary is not None and alias is not None and primary != alias:
        raise EvidenceError(f"{role} aliases disagree")
    return primary if primary is not None else alias


def _mapping(value: Any, role: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{role} is not a mapping")
    result = dict(value)
    if any(not isinstance(key, str) or not key for key in result):
        raise EvidenceError(f"{role} has an invalid key")
    return result


def _source_value(mapping: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _p_quantile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return float("inf")
    ordered = sorted(float(value) for value in values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


@dataclass(frozen=True)
class PathSample:
    """One host-observed PATH sample.

    The first seven fields preserve the r003/r004 force-observation seam.  The
    optional fields are deliberately explicit so a force bin cannot be used as
    a proxy for physical motion.
    """

    observed_at_s: float
    filtered_normal_n: float
    force_norm_n: float
    torque_norm_nm: float
    sensor_fresh: bool
    state: int
    safety_normal: bool
    desired_xy_m: Sequence[float] | None = None
    actual_xy_m: Sequence[float] | None = None
    path_time_s: float | None = None
    path_phase: int | None = None
    desired_velocity_m_s: Sequence[float] | None = None
    actual_velocity_m_s: Sequence[float] | None = None
    qdot: Sequence[float] | None = None
    actual_qd: Sequence[float] | None = None
    source_ages_s: Mapping[str, float] = field(default_factory=dict)
    source_sequences: Mapping[str, Any] = field(default_factory=dict)
    qd_lag_s: float | None = None
    # Keyword aliases used by some adapters and reports.
    desired_xy: Sequence[float] | None = None
    actual_xy: Sequence[float] | None = None
    path_time: float | None = None
    phase: int | None = None
    desired_velocity: Sequence[float] | None = None
    actual_velocity: Sequence[float] | None = None
    qdot_rad_s: Sequence[float] | None = None
    actual_qd_rad_s: Sequence[float] | None = None
    source_ages: Mapping[str, float] | None = None
    source_sequence: Mapping[str, Any] | None = None
    actual_qd_lag_s: float | None = None
    # Optional Tube+CBF / host HardTube telemetry (default None = absent).
    tube_cbf: Mapping[str, Any] | None = None
    host_hard_tube: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        values = (
            self.observed_at_s,
            self.filtered_normal_n,
            self.force_norm_n,
            self.torque_norm_nm,
        )
        if not all(math.isfinite(float(value)) for value in values):
            raise EvidenceError("path sample contains a nonfinite value")
        if self.force_norm_n < 0.0 or self.torque_norm_nm < 0.0:
            raise EvidenceError("path sample norms are negative")
        if not isinstance(self.sensor_fresh, bool) or not isinstance(self.safety_normal, bool):
            raise EvidenceError("path sample flags are not typed")
        if isinstance(self.state, bool) or not isinstance(self.state, int):
            raise EvidenceError("path sample state is not an int")

        desired_xy = _coalesce(self.desired_xy_m, self.desired_xy, "desired XY")
        actual_xy = _coalesce(self.actual_xy_m, self.actual_xy, "actual XY")
        path_time = _coalesce(self.path_time_s, self.path_time, "path time")
        path_phase = _coalesce(self.path_phase, self.phase, "path phase")
        desired_velocity = _coalesce(
            self.desired_velocity_m_s,
            self.desired_velocity,
            "desired velocity",
        )
        actual_velocity = _coalesce(
            self.actual_velocity_m_s,
            self.actual_velocity,
            "actual velocity",
        )
        qdot = _coalesce(self.qdot, self.qdot_rad_s, "qdot")
        actual_qd = _coalesce(self.actual_qd, self.actual_qd_rad_s, "actual qd")
        ages_input = self.source_ages_s
        if self.source_ages is not None:
            if ages_input and dict(ages_input) != dict(self.source_ages):
                raise EvidenceError("source age aliases disagree")
            ages_input = self.source_ages
        sequences_input = self.source_sequences
        if self.source_sequence is not None:
            if sequences_input and dict(sequences_input) != dict(self.source_sequence):
                raise EvidenceError("source sequence aliases disagree")
            sequences_input = self.source_sequence

        if desired_xy is not None:
            desired_xy = _vector(desired_xy, 2, "desired XY")
        if actual_xy is not None:
            actual_xy = _vector(actual_xy, 2, "actual XY")
        if desired_velocity is not None:
            desired_velocity = _vector(desired_velocity, 2, "desired velocity")
        if actual_velocity is not None:
            actual_velocity = _vector(actual_velocity, 2, "actual velocity")
        if qdot is not None:
            qdot = _vector(qdot, 6, "qdot")
        if actual_qd is not None:
            actual_qd = _vector(actual_qd, 6, "actual qd")
        if path_time is not None:
            path_time = _finite(path_time, "path time")
            if path_time < 0.0:
                raise EvidenceError("path time is negative")
        if path_phase is not None:
            if isinstance(path_phase, bool) or not isinstance(path_phase, int) or not 0 <= path_phase <= PATH_PHASE_REQUIRED:
                raise EvidenceError("path phase is invalid")
        lag = _coalesce(self.qd_lag_s, self.actual_qd_lag_s, "qdot lag")
        if lag is not None and _finite(lag, "qdot lag") < 0.0:
            raise EvidenceError("qdot lag is negative")

        ages = _mapping(ages_input, "source ages")
        for key, value in ages.items():
            numeric = _finite(value, f"source age {key}")
            if numeric < 0.0:
                raise EvidenceError(f"source age {key} is negative")
            ages[key] = numeric
        sequences = _mapping(sequences_input, "source sequences")
        for key, value in sequences.items():
            if isinstance(value, bool) or value is None:
                raise EvidenceError(f"source sequence {key} is invalid")

        object.__setattr__(self, "desired_xy_m", desired_xy)
        object.__setattr__(self, "actual_xy_m", actual_xy)
        object.__setattr__(self, "path_time_s", path_time)
        object.__setattr__(self, "path_phase", path_phase)
        object.__setattr__(self, "desired_velocity_m_s", desired_velocity)
        object.__setattr__(self, "actual_velocity_m_s", actual_velocity)
        object.__setattr__(self, "qdot", qdot)
        object.__setattr__(self, "actual_qd", actual_qd)
        object.__setattr__(self, "source_ages_s", ages)
        object.__setattr__(self, "source_sequences", sequences)
        object.__setattr__(self, "qd_lag_s", None if lag is None else float(lag))
        # Make aliases read back as the normalized values as well.
        object.__setattr__(self, "desired_xy", desired_xy)
        object.__setattr__(self, "actual_xy", actual_xy)
        object.__setattr__(self, "path_time", path_time)
        object.__setattr__(self, "phase", path_phase)
        object.__setattr__(self, "desired_velocity", desired_velocity)
        object.__setattr__(self, "actual_velocity", actual_velocity)
        object.__setattr__(self, "qdot_rad_s", qdot)
        object.__setattr__(self, "actual_qd_rad_s", actual_qd)
        object.__setattr__(self, "source_ages", ages)
        object.__setattr__(self, "source_sequence", sequences)
        object.__setattr__(self, "actual_qd_lag_s", None if lag is None else float(lag))

    @property
    def motion_fields_complete(self) -> bool:
        required_sources = (
            ("writer", "publish", "writer_publish", "writer_sequence"),
            ("rtde", "rtde_frame", "controller", "actual_qd"),
            ("kunwei", "kunwei_frame", "wrench", "sensor"),
            ("tp", "tp_echo", "tp_consumed", "packet_echo"),
        )
        return bool(
            self.desired_xy_m is not None
            and self.actual_xy_m is not None
            and self.path_time_s is not None
            and self.path_phase is not None
            and self.desired_velocity_m_s is not None
            and self.actual_velocity_m_s is not None
            and self.qdot is not None
            and self.actual_qd is not None
            and self.source_ages_s
            and self.source_sequences
            and all(_source_value(self.source_ages_s, names) is not None for names in required_sources)
            and all(_source_value(self.source_sequences, names) is not None for names in required_sources)
        )


@dataclass(frozen=True)
class AttemptEvidence:
    complete_bins: int
    effective_rate_hz: float
    p99_packet_interval_s: float
    max_packet_interval_s: float
    mae_n: float
    objective: float
    safety_gate_passed: bool
    contact_gate_passed: bool
    return_gate_passed: bool
    home_proof: Mapping[str, Any]
    path_samples: int
    path_bin_ids: tuple[int, ...]
    evidence_sha256: str
    metrics: Mapping[str, Any] = field(default_factory=dict)
    path_duration_s: float | None = None
    path_phase: int | None = None
    xy_error_p95_m: float | None = None
    xy_error_max_m: float | None = None
    endpoint_error_max_m: float | None = None
    qd_correlation: float | None = None
    qd_lag_s: float | None = None
    timing_evidence: TimingEvidence | None = None

    def __post_init__(self) -> None:
        if self.complete_bins != 550 or set(self.path_bin_ids) != set(range(550)):
            raise EvidenceError("r004 path coverage is not exactly 550 bins")
        for role, value in (
            ("effective rate", self.effective_rate_hz),
            ("p99 packet interval", self.p99_packet_interval_s),
            ("max packet interval", self.max_packet_interval_s),
            ("MAE", self.mae_n),
            ("objective", self.objective),
        ):
            if not math.isfinite(float(value)) or float(value) < 0.0:
                raise EvidenceError(f"{role} is invalid")
        if not all(
            isinstance(value, bool)
            for value in (self.safety_gate_passed, self.contact_gate_passed, self.return_gate_passed)
        ):
            raise EvidenceError("attempt gate values are not typed")
        if not isinstance(self.path_samples, int) or isinstance(self.path_samples, bool) or self.path_samples < 550:
            raise EvidenceError("path sample count is invalid")
        if len(self.evidence_sha256) != 64 or any(char not in "0123456789abcdef" for char in self.evidence_sha256):
            raise EvidenceError("attempt evidence SHA is invalid")
        if self.path_phase is not None and (isinstance(self.path_phase, bool) or not isinstance(self.path_phase, int)):
            raise EvidenceError("attempt path phase is not an int")
        for role, value in (
            ("path duration", self.path_duration_s),
            ("XY p95", self.xy_error_p95_m),
            ("XY max", self.xy_error_max_m),
            ("endpoint error", self.endpoint_error_max_m),
            ("qd correlation", self.qd_correlation),
            ("qd lag", self.qd_lag_s),
        ):
            if value is not None and (not math.isfinite(float(value)) or float(value) < 0.0):
                raise EvidenceError(f"{role} is invalid")
        if self.qd_correlation is not None and self.qd_correlation > 1.0:
            raise EvidenceError("qd correlation is above one")
        if self.timing_evidence is not None and not isinstance(self.timing_evidence, TimingEvidence):
            raise EvidenceError("timing evidence is not typed")

    def _timing(self) -> TimingEvidence | None:
        if self.timing_evidence is not None:
            return self.timing_evidence
        raw = self.metrics.get("timing_evidence") if isinstance(self.metrics, Mapping) else None
        if isinstance(raw, TimingEvidence):
            return raw
        if isinstance(raw, Mapping):
            try:
                return TimingEvidence.from_mapping(raw)
            except TimingError:
                return None
        return None

    @property
    def timing_gate_passed(self) -> bool:
        timing = self._timing()
        return bool(timing is not None and timing.successful)

    @property
    def motion_gate_passed(self) -> bool:
        return bool(
            self.path_duration_s is not None
            and self.path_duration_s >= PATH_DURATION_MIN_S
            and self.path_phase == PATH_PHASE_REQUIRED
            and self.xy_error_p95_m is not None
            and self.xy_error_p95_m <= XY_ERROR_P95_MAX_M
            and self.xy_error_max_m is not None
            and self.xy_error_max_m <= XY_ERROR_MAX_M
            and self.endpoint_error_max_m is not None
            and self.endpoint_error_max_m <= ENDPOINT_ERROR_MAX_M
            and self.qd_correlation is not None
            and self.qd_correlation >= QD_CORRELATION_MIN
            and self.qd_lag_s is not None
            and self.qd_lag_s <= QD_LAG_MAX_S
        )

    @property
    def path_motion_passed(self) -> bool:
        return self.motion_gate_passed

    @property
    def eligible(self) -> bool:
        return bool(
            self.safety_gate_passed
            and self.contact_gate_passed
            and self.return_gate_passed
            and self.motion_gate_passed
            and self.timing_gate_passed
        )


@dataclass(frozen=True)
class QualificationSample:
    observed_at_s: float
    filtered_normal_n: float
    internal_setpoint_n: float
    sensor_fresh: bool
    safety_normal: bool
    state: int
    command_mode: int
    sticky_one_newton_latched: int

    def __post_init__(self) -> None:
        values = (self.observed_at_s, self.filtered_normal_n, self.internal_setpoint_n)
        if not all(math.isfinite(float(value)) for value in values):
            raise EvidenceError("qualification sample contains a nonfinite value")
        if not 1.0 <= float(self.internal_setpoint_n) <= TARGET_FORCE_N:
            raise EvidenceError("qualification setpoint is outside 1-to-5 N")
        if not isinstance(self.sensor_fresh, bool) or not isinstance(self.safety_normal, bool):
            raise EvidenceError("qualification sample flags are not typed")
        if self.command_mode not in {0, 1, 3} or self.sticky_one_newton_latched not in {0, 1}:
            raise EvidenceError("qualification wire state is invalid")


@dataclass(frozen=True)
class QualificationEvidence:
    qualification_passed: bool
    effective_rate_hz: float
    p99_packet_interval_s: float
    max_packet_interval_s: float
    safety_gate_passed: bool
    contact_gate_passed: bool
    return_gate_passed: bool
    fresh_sensor_gate_passed: bool
    sample_count: int
    observed_states: tuple[int, ...]
    home_proof: Mapping[str, Any]
    evidence_sha256: str
    metrics: Mapping[str, Any] = field(default_factory=dict)
    timing_evidence: TimingEvidence | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, bool)
            for value in (
                self.qualification_passed,
                self.safety_gate_passed,
                self.contact_gate_passed,
                self.return_gate_passed,
                self.fresh_sensor_gate_passed,
            )
        ):
            raise EvidenceError("qualification gate values are not typed")
        if self.sample_count < 2 or not {20, 21, 78}.issubset(self.observed_states):
            raise EvidenceError("qualification state/sample evidence is incomplete")
        for role, value in (
            ("effective rate", self.effective_rate_hz),
            ("p99 packet interval", self.p99_packet_interval_s),
            ("max packet interval", self.max_packet_interval_s),
        ):
            if not math.isfinite(float(value)) or float(value) < 0.0:
                raise EvidenceError(f"qualification {role} is invalid")
        if len(self.evidence_sha256) != 64 or any(char not in "0123456789abcdef" for char in self.evidence_sha256):
            raise EvidenceError("qualification evidence SHA is invalid")
        if self.timing_evidence is not None and not isinstance(self.timing_evidence, TimingEvidence):
            raise EvidenceError("qualification timing evidence is not typed")

    @property
    def timing_gate_passed(self) -> bool:
        return bool(self.timing_evidence is not None and self.timing_evidence.successful)

    @property
    def eligible(self) -> bool:
        return bool(self.qualification_passed and self.timing_gate_passed)


class QualificationEvidenceCollector:
    """Collect genuine contact/baseline/return evidence without path bins."""

    def __init__(self) -> None:
        self._samples: list[QualificationSample] = []
        self._states: set[int] = set()

    def observe(self, sample: QualificationSample) -> None:
        if self._samples and sample.observed_at_s <= self._samples[-1].observed_at_s:
            raise EvidenceError("qualification timestamps are not strictly increasing")
        self._samples.append(sample)
        self._states.add(sample.state)

    @staticmethod
    def _p99(intervals: list[float]) -> float:
        return _p_quantile(intervals, 0.99)

    def finalize(
        self,
        *,
        return_gate_passed: bool,
        home_proof: Mapping[str, Any],
        timing_evidence: TimingEvidence | None = None,
    ) -> QualificationEvidence:
        if len(self._samples) < 2 or not {20, 21, 78}.issubset(self._states):
            raise EvidenceError("qualification did not observe contact, baseline, and return")
        intervals = [
            right.observed_at_s - left.observed_at_s
            for left, right in zip(self._samples, self._samples[1:])
        ]
        if any(not math.isfinite(value) or value <= 0.0 for value in intervals):
            raise EvidenceError("qualification timing is invalid")
        duration = self._samples[-1].observed_at_s - self._samples[0].observed_at_s
        rate = (len(self._samples) - 1) / duration if duration > 0.0 else 0.0
        baseline_samples = [sample for sample in self._samples if sample.state == 21]
        sensor_gate = bool(baseline_samples) and all(sample.sensor_fresh for sample in baseline_samples)
        safety_gate = all(sample.safety_normal for sample in self._samples)
        contact_gate = {20, 21}.issubset(self._states)
        saw_baseline = any(sample.command_mode == 1 for sample in baseline_samples)
        saw_retract = any(sample.command_mode == 3 for sample in baseline_samples)
        saw_latch = any(sample.sticky_one_newton_latched == 1 for sample in baseline_samples)
        timing_gate = (
            rate >= MIN_SUCCESS_RATE_HZ
            and self._p99(intervals) <= 0.010
            and max(intervals) < 0.020
        )
        passed = bool(
            return_gate_passed
            and sensor_gate
            and safety_gate
            and contact_gate
            and saw_baseline
            and saw_retract
            and saw_latch
            and timing_gate
        )
        material = _json_safe(
            {
                "samples": [sample.__dict__ for sample in self._samples],
                "states": sorted(self._states),
                "home_proof": dict(home_proof),
                "return_gate_passed": bool(return_gate_passed),
                "timing_evidence": None if timing_evidence is None else timing_evidence.as_dict(),
            }
        )
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        metrics = {
            "baseline_samples": len(baseline_samples),
            "saw_baseline": saw_baseline,
            "saw_retract": saw_retract,
            "saw_latch": saw_latch,
            "timing_gate_passed": timing_gate,
            "setpoint_min_n": min(sample.internal_setpoint_n for sample in baseline_samples),
            "setpoint_max_n": max(sample.internal_setpoint_n for sample in baseline_samples),
        }
        if timing_evidence is not None:
            metrics["timing_evidence"] = timing_evidence.as_dict()
        return QualificationEvidence(
            qualification_passed=passed,
            effective_rate_hz=rate,
            p99_packet_interval_s=self._p99(intervals),
            max_packet_interval_s=max(intervals),
            safety_gate_passed=safety_gate,
            contact_gate_passed=contact_gate,
            return_gate_passed=bool(return_gate_passed),
            fresh_sensor_gate_passed=sensor_gate,
            sample_count=len(self._samples),
            observed_states=tuple(sorted(self._states)),
            home_proof=dict(home_proof),
            evidence_sha256=digest,
            metrics=metrics,
            timing_evidence=timing_evidence,
        )


class PathEvidenceCollector:
    """Collect force, motion and timing evidence without synthesizing bins."""

    BIN_WIDTH_S = 0.1
    REQUIRED_BINS = 550
    REQUIRED_DURATION_S = PATH_DURATION_MIN_S
    REQUIRED_PHASE = PATH_PHASE_REQUIRED

    def __init__(
        self,
        timing: TimingEvidenceCollector | None = None,
        *,
        require_path_boundary: bool = False,
    ) -> None:
        if not isinstance(require_path_boundary, bool):
            raise EvidenceError("r004 PATH boundary policy is not typed")
        self.path_started_at_s: float | None = None
        self._path_time_origin_s: float | None = None
        self._bins: dict[int, list[float]] = {}
        self._path_samples: list[PathSample] = []
        self._states: set[int] = set()
        self._safety_all = True
        self._timing = timing or TimingEvidenceCollector()
        self._timing_observation_error: str | None = None
        self._seen_path_identities: set[tuple[tuple[str, str], ...]] = set()
        self._path_intervals_s: list[float] = []
        self._rtde_intervals_s: list[float] = []
        self._last_common_clock: tuple[float, int] | None = None
        self._path_start_observed_at_s: float | None = None
        self._path_start_rtde_timestamp_s: float | None = None
        self._path_start_tp_sequence: int | None = None
        self._require_path_boundary = require_path_boundary
        # The source clock is already a binary64 value.  Retain only the
        # largest source-timestamp ULP needed to bound cancellation; raw
        # samples remain the evidence owner and are not duplicated here.
        self._max_duration_source_ulp_s = 0.0

    def mark_path_start(
        self,
        *,
        observed_at_s: float,
        rtde_timestamp_s: float,
        tp_sequence: int,
    ) -> None:
        """Bind PATH coverage to one TP echo and its RTDE controller clock."""

        observed = _finite(observed_at_s, "PATH boundary observed time")
        rtde_timestamp = _finite(rtde_timestamp_s, "PATH boundary RTDE timestamp")
        if rtde_timestamp < 0.0:
            raise EvidenceError("PATH boundary RTDE timestamp is negative")
        if (
            isinstance(tp_sequence, bool)
            or not isinstance(tp_sequence, int)
            or tp_sequence < 0
        ):
            raise EvidenceError("PATH boundary TP sequence is invalid")
        if self._path_start_rtde_timestamp_s is not None:
            if (
                not math.isclose(
                    self._path_start_rtde_timestamp_s,
                    rtde_timestamp,
                    rel_tol=0.0,
                    abs_tol=0.0,
                )
                or self._path_start_tp_sequence != tp_sequence
            ):
                raise EvidenceError("r004 PATH boundary changed after initialization")
            return
        self._path_start_observed_at_s = observed
        self._path_start_rtde_timestamp_s = rtde_timestamp
        self._path_start_tp_sequence = tp_sequence
        self._max_duration_source_ulp_s = max(
            self._max_duration_source_ulp_s,
            math.ulp(abs(rtde_timestamp)),
        )

    @staticmethod
    def _common_clock(sample: PathSample) -> tuple[float, int] | None:
        rtde_raw = _source_value(
            sample.source_sequences,
            ("rtde", "rtde_frame", "controller", "actual_qd"),
        )
        tp_raw = _source_value(
            sample.source_sequences,
            ("tp", "tp_echo", "tp_consumed", "packet_echo"),
        )
        if rtde_raw is None or tp_raw is None:
            return None
        rtde_timestamp = _finite(rtde_raw, "PATH RTDE timestamp")
        if (
            isinstance(tp_raw, bool)
            or not isinstance(tp_raw, int)
            or tp_raw < 0
        ):
            raise EvidenceError("PATH TP consumed sequence is invalid")
        return rtde_timestamp, tp_raw

    @staticmethod
    def _path_identity(sample: PathSample) -> tuple[tuple[str, str], ...] | None:
        common_clock = PathEvidenceCollector._common_clock(sample)
        if common_clock is not None:
            return (
                ("rtde", repr(common_clock[0])),
                ("tp", repr(common_clock[1])),
            )
        if not sample.source_sequences:
            return None
        return tuple(sorted((key, repr(value)) for key, value in sample.source_sequences.items()))

    def observe(self, sample: PathSample) -> bool:
        self._states.add(sample.state)
        self._safety_all = self._safety_all and sample.safety_normal
        if sample.state != 25:
            return False
        if sample.path_time_s is not None:
            self._max_duration_source_ulp_s = max(
                self._max_duration_source_ulp_s,
                math.ulp(abs(sample.path_time_s)),
            )
        if self._path_samples and sample.observed_at_s < self._path_samples[-1].observed_at_s:
            raise EvidenceError("path timestamp regressed")
        identity = self._path_identity(sample)
        if identity is not None and identity in self._seen_path_identities:
            return False
        common_clock = self._common_clock(sample)
        if common_clock is not None:
            self._max_duration_source_ulp_s = max(
                self._max_duration_source_ulp_s,
                math.ulp(abs(common_clock[0])),
            )
        if self._path_start_rtde_timestamp_s is not None and common_clock is None:
            raise EvidenceError("r004 PATH sample lacks the common TP/RTDE clock")
        if common_clock is not None:
            if self._last_common_clock is not None:
                previous_rtde, previous_tp = self._last_common_clock
                current_rtde, current_tp = common_clock
                if current_rtde < previous_rtde or current_tp < previous_tp:
                    raise EvidenceError("r004 common TP/RTDE clock regressed")
                # A new host/Kunwei observation cannot stand in for a new
                # physical control tick when either joined source is cached.
                if current_rtde == previous_rtde or current_tp == previous_tp:
                    return False
            if self._path_start_rtde_timestamp_s is not None:
                start_rtde = self._path_start_rtde_timestamp_s
                start_tp = self._path_start_tp_sequence
                if start_tp is None:
                    raise EvidenceError("r004 PATH TP boundary sequence is missing")
                if common_clock[0] < start_rtde or common_clock[1] < start_tp:
                    raise EvidenceError("r004 PATH sample precedes its TP/RTDE boundary")
                if sample.path_time_s != common_clock[0] - start_rtde:
                    raise EvidenceError("r004 PATH time is not bound to the RTDE controller clock")
                if self._last_common_clock is None and common_clock != (start_rtde, start_tp):
                    # The first accepted sample must advance both sides of
                    # the joined clock.  A controller timestamp with the old
                    # TP echo (or vice versa) is a cached join, not a control
                    # tick that can contribute to physical coverage.
                    if common_clock[0] == start_rtde or common_clock[1] == start_tp:
                        return False
            if self._last_common_clock is not None:
                interval = common_clock[0] - self._last_common_clock[0]
                if not math.isfinite(interval) or interval <= 0.0:
                    raise EvidenceError("r004 common TP/RTDE cadence is invalid")
                self._rtde_intervals_s.append(interval)
            self._last_common_clock = common_clock
        if identity is not None:
            self._seen_path_identities.add(identity)
        if sample.path_time_s is not None and sample.path_time_s >= PATH_DURATION_MIN_S:
            raise EvidenceError("r004 PATH sample must remain strictly before 60 s")
        if self._path_samples and sample.path_time_s is not None:
            previous_path_time = self._path_samples[-1].path_time_s
            if previous_path_time is not None:
                interval = sample.path_time_s - previous_path_time
                if not math.isfinite(interval) or interval <= 0.0:
                    raise EvidenceError("r004 PATH cadence is not strictly increasing")
                self._path_intervals_s.append(interval)
        if self.path_started_at_s is None:
            self.path_started_at_s = sample.observed_at_s
            self._path_time_origin_s = sample.path_time_s
        if sample.path_time_s is not None and self._path_time_origin_s is not None:
            relative = sample.path_time_s - self._path_time_origin_s
        else:
            relative = sample.observed_at_s - self.path_started_at_s
        if relative < 0.0:
            raise EvidenceError("path time regressed")
        index = int(relative / self.BIN_WIDTH_S)
        if 0 <= index < self.REQUIRED_BINS:
            self._bins.setdefault(index, []).append(sample.filtered_normal_n)
        self._path_samples.append(sample)
        try:
            self._timing.observe_layered_sample(
                sample.observed_at_s,
                source_sequences=sample.source_sequences,
                source_ages_s=sample.source_ages_s,
            )
        except TimingError as exc:
            self._timing_observation_error = str(exc)
        return True

    def _validated_path_coverage_interval_s(self) -> float:
        """Return the observed cadence interval owned by the last sample."""

        intervals = self._rtde_intervals_s if self._path_start_rtde_timestamp_s is not None else self._path_intervals_s
        if not intervals:
            raise EvidenceError("r004 PATH cadence evidence is missing")
        interval = statistics.median(intervals)
        if not math.isfinite(interval) or interval <= 0.0:
            raise EvidenceError("r004 PATH cadence evidence is invalid")
        return interval

    def _duration_rounding_bound(
        self,
        *,
        physical_span: float,
        coverage_interval_s: float,
        duration: float,
    ) -> float:
        """Bound source-clock cancellation and the final duration arithmetic.

        Each physical span and cadence interval is a difference of two
        already-rounded source timestamps.  Two source-ULP terms bound those
        input representations.  The remaining terms account, separately,
        for the physical subtraction, the interval subtraction/median, and
        the final addition.  This is an operation/source-magnitude bound, not
        a time or cadence tolerance.
        """

        return (
            2.0 * self._max_duration_source_ulp_s
            + math.ulp(abs(physical_span))
            + 2.0 * math.ulp(abs(coverage_interval_s))
            + math.ulp(abs(duration))
        )

    def _canonicalize_full_duration(
        self,
        duration: float,
        *,
        coverage_interval_s: float,
        rounding_bound_s: float,
    ) -> float:
        """Canonicalize a proven endpoint, never a cadence-sized shortfall.

        The endpoint-exclusive V3 rule is that the last strict-before-end
        sample owns one validated following control interval.  If the
        computed deficit is no larger than the derived binary64 error bound,
        and twice that bound is strictly smaller than the interval itself, the
        endpoint is numerically distinguishable from a one-frame-short run
        and may be published as exact ``60``.
        Otherwise the original duration remains fail-closed.
        """

        required = self.REQUIRED_DURATION_S
        if duration >= required:
            return duration
        deficit_s = required - duration
        if (
            math.isfinite(rounding_bound_s)
            and 2.0 * rounding_bound_s < coverage_interval_s
            and deficit_s <= rounding_bound_s
        ):
            return required
        return duration

    @property
    def safety_gate_passed(self) -> bool:
        return self._safety_all

    @property
    def observed_states(self) -> frozenset[int]:
        return frozenset(self._states)

    @property
    def path_samples(self) -> tuple[PathSample, ...]:
        return tuple(self._path_samples)

    @staticmethod
    def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
        if len(left) != len(right) or not left:
            return 0.0
        left_mean = statistics.fmean(left)
        right_mean = statistics.fmean(right)
        left_centered = [value - left_mean for value in left]
        right_centered = [value - right_mean for value in right]
        left_norm = math.sqrt(sum(value * value for value in left_centered))
        right_norm = math.sqrt(sum(value * value for value in right_centered))
        if left_norm <= 1e-12 and right_norm <= 1e-12:
            return 1.0 if all(math.isclose(a, b, abs_tol=1e-12) for a, b in zip(left, right)) else 0.0
        if left_norm <= 1e-12 or right_norm <= 1e-12:
            return 0.0
        return sum(a * b for a, b in zip(left_centered, right_centered)) / (left_norm * right_norm)

    def _motion_metrics(self) -> dict[str, Any]:
        if not self._path_samples:
            raise EvidenceError("r004 path has no PATH samples")
        missing = [
            role
            for role, predicate in (
                ("desired XY", lambda sample: sample.desired_xy_m is not None),
                ("actual XY", lambda sample: sample.actual_xy_m is not None),
                ("path time", lambda sample: sample.path_time_s is not None),
                ("path phase", lambda sample: sample.path_phase is not None),
                ("desired velocity", lambda sample: sample.desired_velocity_m_s is not None),
                ("actual velocity", lambda sample: sample.actual_velocity_m_s is not None),
                ("qdot", lambda sample: sample.qdot is not None),
                ("actual qd", lambda sample: sample.actual_qd is not None),
                ("source ages", lambda sample: bool(sample.source_ages_s)),
                ("source sequences", lambda sample: bool(sample.source_sequences)),
            )
            if not all(predicate(sample) for sample in self._path_samples)
        ]
        if missing:
            raise EvidenceError(
                "force bins alone cannot prove motion; missing PATH evidence: " + ", ".join(missing)
            )
        if self._require_path_boundary and self._path_start_rtde_timestamp_s is None:
            raise EvidenceError("r004 PATH TP/RTDE boundary evidence is missing")
        # A strictly-before-end sample owns the following validated control
        # interval.  In the live path, the start is the first TP/RTDE boundary
        # and the duration is measured on the RTDE controller clock, not on the
        # delayed host callback clock.
        coverage_interval_s = self._validated_path_coverage_interval_s()
        observed_span = (
            self._path_samples[-1].path_time_s
            - self._path_samples[0].path_time_s
        )
        if self._path_start_rtde_timestamp_s is not None:
            if self._last_common_clock is None:
                raise EvidenceError("r004 PATH common TP/RTDE clock is missing")
            physical_span = self._last_common_clock[0] - self._path_start_rtde_timestamp_s
            if not math.isfinite(physical_span) or physical_span < 0.0:
                raise EvidenceError("r004 PATH physical clock regressed")
            duration = physical_span + coverage_interval_s
            duration_basis = "tp_consumed_sequence+rtde_controller_timestamp"
        else:
            physical_span = observed_span
            duration = observed_span + coverage_interval_s
            duration_basis = "path_time_sample_clock"
        duration_rounding_bound_s = self._duration_rounding_bound(
            physical_span=physical_span,
            coverage_interval_s=coverage_interval_s,
            duration=duration,
        )
        canonical_duration = self._canonicalize_full_duration(
            duration,
            coverage_interval_s=coverage_interval_s,
            rounding_bound_s=duration_rounding_bound_s,
        )
        if canonical_duration < self.REQUIRED_DURATION_S:
            raise EvidenceError(
                f"r004 path duration is {duration:.6f} s, below 60 s "
                f"(observed_span={observed_span:.6f}, "
                f"coverage_interval={coverage_interval_s:.6f}, "
                f"physical_span={physical_span:.6f})"
            )
        # Keep the accepted ULP-only endpoint exact so AttemptEvidence,
        # campaign promotion, and any later duration_s >= 60.0 check cannot
        # turn the same valid physical run into SAFE_NONTRAINABLE.
        duration = canonical_duration
        if self._path_samples[-1].path_phase != self.REQUIRED_PHASE:
            raise EvidenceError("r004 path did not reach phase 6")

        xy_errors = [
            math.sqrt(sum((actual - desired) ** 2 for actual, desired in zip(sample.actual_xy_m, sample.desired_xy_m)))
            for sample in self._path_samples
        ]
        velocity_errors = [
            math.sqrt(
                sum(
                    (actual - desired) ** 2
                    for actual, desired in zip(sample.actual_velocity_m_s, sample.desired_velocity_m_s)
                )
            )
            for sample in self._path_samples
        ]
        endpoint_error = max(xy_errors[0], xy_errors[-1])
        desired_qd = list(zip(*(sample.qdot for sample in self._path_samples)))
        actual_qd = list(zip(*(sample.actual_qd for sample in self._path_samples)))
        per_joint_correlation = tuple(
            self._pearson(left, right) for left, right in zip(desired_qd, actual_qd)
        )
        # The acceptance contract is the correlation of the commanded and
        # measured six-joint streams.  Requiring the minimum correlation of
        # every individual joint makes an intentionally stationary/noise-only
        # joint veto otherwise good tracking.
        qd_correlation = max(
            -1.0,
            min(
                1.0,
                self._pearson(
                    [value for sample in self._path_samples for value in sample.qdot],
                    [value for sample in self._path_samples for value in sample.actual_qd],
                ),
            ),
        )
        lag_values: list[float] = []
        for sample in self._path_samples:
            if sample.qd_lag_s is not None:
                lag_values.append(sample.qd_lag_s)
            else:
                lag_values.extend(float(value) for value in sample.source_ages_s.values())
        if not lag_values:
            raise EvidenceError("qdot/actual_qd lag evidence is missing")
        return {
            "path_duration_s": duration,
            "path_observed_span_s": observed_span,
            "path_physical_span_s": physical_span,
            "path_coverage_interval_s": coverage_interval_s,
            "path_duration_rounding_bound_s": duration_rounding_bound_s,
            "path_cadence_hz": 1.0 / coverage_interval_s,
            "path_duration_basis": duration_basis,
            "path_phase": self._path_samples[-1].path_phase,
            "xy_error_p95_m": _p_quantile(xy_errors, 0.95),
            "xy_error_max_m": max(xy_errors),
            "endpoint_error_max_m": endpoint_error,
            "velocity_error_p95_m_s": _p_quantile(velocity_errors, 0.95),
            "qd_correlation": qd_correlation,
            "qd_joint_correlations": per_joint_correlation,
            "qd_lag_s": max(lag_values),
            "motion_gate_passed": bool(
                _p_quantile(xy_errors, 0.95) <= XY_ERROR_P95_MAX_M
                and max(xy_errors) <= XY_ERROR_MAX_M
                and endpoint_error <= ENDPOINT_ERROR_MAX_M
                and qd_correlation >= QD_CORRELATION_MIN
                and max(lag_values) <= QD_LAG_MAX_S
            ),
        }

    def finalize(
        self,
        *,
        return_gate_passed: bool,
        home_proof: Mapping[str, Any],
        contact_gate_passed: bool | None = None,
        mae_n: float | None = None,
        objective: float | None = None,
        timing_evidence: TimingEvidence | None = None,
    ) -> AttemptEvidence:
        if self.path_started_at_s is None or len(self._bins) != self.REQUIRED_BINS:
            raise EvidenceError(f"r004 path has {len(self._bins)} of 550 bins")
        if len(self._path_samples) < 2:
            raise EvidenceError("r004 path has insufficient timing samples")
        timestamps = [sample.observed_at_s for sample in self._path_samples]
        intervals = [right - left for left, right in zip(timestamps, timestamps[1:])]
        if any(not math.isfinite(value) or value <= 0.0 for value in intervals):
            raise EvidenceError("r004 path timing is not strictly increasing")
        motion = self._motion_metrics()
        means = [statistics.fmean(self._bins[index]) for index in range(self.REQUIRED_BINS)]
        errors = [value - TARGET_FORCE_N for value in means]
        calculated_mae = statistics.fmean(abs(value) for value in errors)
        duration = motion["path_duration_s"]
        if timing_evidence is None:
            try:
                timing_evidence = self._timing.finalize(duration_s=duration)
            except TimingError as exc:
                self._timing_observation_error = str(exc)
                timing_evidence = None
        elif not isinstance(timing_evidence, TimingEvidence):
            raise EvidenceError("timing evidence is not typed")
        complete_contact = (
            contact_gate_passed
            if contact_gate_passed is not None
            else {20, 21, 25}.issubset(self._states)
        )
        metrics: dict[str, Any] = {
            "state_codes": sorted(self._states),
            "path_duration_s": duration,
            "path_observed_span_s": motion["path_observed_span_s"],
            "path_physical_span_s": motion["path_physical_span_s"],
            "path_coverage_interval_s": motion["path_coverage_interval_s"],
            "path_duration_rounding_bound_s": motion["path_duration_rounding_bound_s"],
            "path_cadence_hz": motion["path_cadence_hz"],
            "path_duration_basis": motion["path_duration_basis"],
            "path_phase": motion["path_phase"],
            "xy_error_p95_m": motion["xy_error_p95_m"],
            "xy_error_max_m": motion["xy_error_max_m"],
            "endpoint_error_max_m": motion["endpoint_error_max_m"],
            "velocity_error_p95_m_s": motion["velocity_error_p95_m_s"],
            "qd_correlation": motion["qd_correlation"],
            "qd_joint_correlations": motion["qd_joint_correlations"],
            "qd_lag_s": motion["qd_lag_s"],
            "motion_gate_passed": motion["motion_gate_passed"],
        }
        if timing_evidence is not None:
            metrics["timing_evidence"] = timing_evidence.as_dict()
            metrics["timing_gate_passed"] = timing_evidence.successful
        else:
            metrics["timing_gate_passed"] = False
        if self._timing_observation_error:
            metrics["timing_observation_error"] = self._timing_observation_error
        evidence_material = _json_safe(
            {
                "bins": {str(index): self._bins[index] for index in range(self.REQUIRED_BINS)},
                "samples": [sample.__dict__ for sample in self._path_samples],
                "states": sorted(self._states),
                "home_proof": dict(home_proof),
                "path_start": {
                    "observed_at_s": self._path_start_observed_at_s,
                    "rtde_timestamp_s": self._path_start_rtde_timestamp_s,
                    "tp_sequence": self._path_start_tp_sequence,
                },
                "timing_evidence": None if timing_evidence is None else timing_evidence.as_dict(),
            }
        )
        evidence_sha = hashlib.sha256(
            json.dumps(evidence_material, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        ).hexdigest()
        return AttemptEvidence(
            complete_bins=self.REQUIRED_BINS,
            effective_rate_hz=len(self._path_samples) / duration if duration > 0.0 else 0.0,
            p99_packet_interval_s=_p_quantile(intervals, 0.99),
            max_packet_interval_s=max(intervals),
            mae_n=calculated_mae if mae_n is None else float(mae_n),
            objective=calculated_mae if objective is None else float(objective),
            safety_gate_passed=self._safety_all,
            contact_gate_passed=bool(complete_contact),
            return_gate_passed=bool(return_gate_passed),
            home_proof=dict(home_proof),
            path_samples=len(self._path_samples),
            path_bin_ids=tuple(range(self.REQUIRED_BINS)),
            evidence_sha256=evidence_sha,
            metrics=metrics,
            path_duration_s=motion["path_duration_s"],
            path_phase=motion["path_phase"],
            xy_error_p95_m=motion["xy_error_p95_m"],
            xy_error_max_m=motion["xy_error_max_m"],
            endpoint_error_max_m=motion["endpoint_error_max_m"],
            qd_correlation=motion["qd_correlation"],
            qd_lag_s=motion["qd_lag_s"],
            timing_evidence=timing_evidence,
        )


__all__ = [
    "AttemptEvidence",
    "ENDPOINT_ERROR_MAX_M",
    "EvidenceError",
    "PATH_DURATION_MIN_S",
    "PATH_PHASE_REQUIRED",
    "PathEvidenceCollector",
    "PathSample",
    "QD_CORRELATION_MIN",
    "QD_LAG_MAX_S",
    "QualificationEvidence",
    "QualificationEvidenceCollector",
    "QualificationSample",
    "XY_ERROR_MAX_M",
    "XY_ERROR_P95_MAX_M",
]
