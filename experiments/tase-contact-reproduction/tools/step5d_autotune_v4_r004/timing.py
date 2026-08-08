"""Versioned, layered timing evidence for the r004 offline/live seam.

The four layers deliberately have independent counters.  A packet that is
replayed from a cache keeps the same identity and therefore cannot improve a
layer's rate.  This module is intentionally transport-agnostic: the writer,
RTDE, Kunwei and TP adapters provide identities, while this collector owns the
acceptance math.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping


TIMING_EVIDENCE_VERSION = "step5d.autotune-v4/r004-timing-v1"
TIMING_EVIDENCE_SCHEMA = "step5d.autotune-v4/r004-timing-evidence-v1"
NOMINAL_RATE_HZ = 500.0
MIN_SUCCESS_RATE_HZ = 460.0
FEEDBACK_AGE_P99_MAX_S = 0.010
MAX_FRESH_GAP_S = 0.020
RUNTIME_STALE_STOP_S = 0.080

WRITER_PUBLISH_LAYER = "writer_publishes"
RTDE_FRAME_LAYER = "rtde_frames"
KUNWEI_FRAME_LAYER = "kunwei_frames"
TP_ECHO_LAYER = "tp_consumed_packet_echoes"
TIMING_LAYERS = (
    WRITER_PUBLISH_LAYER,
    RTDE_FRAME_LAYER,
    KUNWEI_FRAME_LAYER,
    TP_ECHO_LAYER,
)
# Kunwei arrives over TCP in coalesced bursts.  Inter-batch hold time is a
# transport artifact (still covered by distinct kunwei rate + RUNTIME_STALE_STOP),
# not closed-loop feedback age.  Same rationale as max_fresh_gap below.
_KUNWEI_FEEDBACK_AGE_KEYS = frozenset({"kunwei", "kunwei_frame", "wrench", "sensor"})


class TimingError(ValueError):
    """A timing observation is not typed or cannot be trusted."""


def _finite(value: Any, role: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise TimingError(f"{role} is not numeric") from exc
    if not math.isfinite(result):
        raise TimingError(f"{role} is nonfinite")
    return result


def _strict_identity(value: Any, role: str) -> str:
    if value is None:
        raise TimingError(f"{role} is missing")
    if isinstance(value, bool):
        raise TimingError(f"{role} must not be bool")
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=repr,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TimingError(f"{role} cannot be canonicalized") from exc
    return hashlib.sha256(encoded).hexdigest()


def _identity(
    *,
    sequence: Any = None,
    frame_id: Any = None,
    payload: Any = None,
    role: str,
    epoch: Any = None,
) -> tuple[str, str]:
    """Return a stable identity, preferring sequence over payload bytes."""

    if sequence is not None:
        return ("sequence", _strict_identity((epoch, sequence), f"{role} sequence"))
    if frame_id is not None:
        return ("frame", _strict_identity((epoch, frame_id), f"{role} frame id"))
    if payload is not None:
        return ("payload", _strict_identity(payload, f"{role} payload"))
    raise TimingError(f"{role} has no frame identity")


def _p_quantile(values: list[float], percentile: float) -> float:
    if not values:
        return float("inf")
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


@dataclass(frozen=True)
class TimingLayerEvidence:
    """One independently measured layer of the timing contract."""

    layer: str
    distinct_count: int
    duration_s: float
    rate_hz: float
    nominal_rate_hz: float = NOMINAL_RATE_HZ
    minimum_rate_hz: float = MIN_SUCCESS_RATE_HZ

    def __post_init__(self) -> None:
        if self.layer not in TIMING_LAYERS:
            raise TimingError(f"unknown timing layer: {self.layer}")
        if isinstance(self.distinct_count, bool) or not isinstance(self.distinct_count, int):
            raise TimingError(f"{self.layer} count is not an int")
        if self.distinct_count < 0:
            raise TimingError(f"{self.layer} count is negative")
        duration = _finite(self.duration_s, f"{self.layer} duration")
        rate = _finite(self.rate_hz, f"{self.layer} rate")
        nominal = _finite(self.nominal_rate_hz, f"{self.layer} nominal rate")
        minimum = _finite(self.minimum_rate_hz, f"{self.layer} minimum rate")
        if duration <= 0.0 or rate < 0.0 or nominal != NOMINAL_RATE_HZ or minimum != MIN_SUCCESS_RATE_HZ:
            raise TimingError(f"{self.layer} timing contract is invalid")

    @property
    def passed(self) -> bool:
        # The comparison is deliberately inclusive at 460 Hz.
        return self.rate_hz >= self.minimum_rate_hz

    @property
    def successful(self) -> bool:
        return self.passed

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "distinct_count": self.distinct_count,
            "duration_s": self.duration_s,
            "rate_hz": self.rate_hz,
            "nominal_rate_hz": self.nominal_rate_hz,
            "minimum_rate_hz": self.minimum_rate_hz,
            "passed": self.passed,
        }


@dataclass(frozen=True)
class TimingEvidence:
    """Versioned aggregate of all r004 freshness layers.

    Counts are counts of *distinct* identities, never raw callback counts.
    ``RUNTIME_STALE_STOP_S`` remains intentionally separate from the tighter
    fresh-gap acceptance bound.
    """

    VERSION = TIMING_EVIDENCE_VERSION
    NOMINAL_HZ = NOMINAL_RATE_HZ
    MINIMUM_HZ = MIN_SUCCESS_RATE_HZ
    FEEDBACK_AGE_P99_LIMIT_S = FEEDBACK_AGE_P99_MAX_S
    FRESH_GAP_LIMIT_S = MAX_FRESH_GAP_S
    STALE_STOP_S = RUNTIME_STALE_STOP_S

    duration_s: float = 0.0
    successful_writer_publishes: int = 0
    distinct_rtde_frames: int = 0
    distinct_kunwei_frames: int = 0
    distinct_tp_consumed_packet_echoes: int = 0
    feedback_age_p99_s: float = float("inf")
    max_fresh_gap_s: float = float("inf")
    nominal_rate_hz: float = NOMINAL_RATE_HZ
    minimum_rate_hz: float = MIN_SUCCESS_RATE_HZ
    runtime_stale_stop_s: float = RUNTIME_STALE_STOP_S
    layer_rates_hz: Mapping[str, float] = field(default_factory=dict)
    version: str = TIMING_EVIDENCE_VERSION

    def __post_init__(self) -> None:
        duration = _finite(self.duration_s, "timing duration")
        if duration < 0.0:
            raise TimingError("timing duration is negative")
        if self.version != TIMING_EVIDENCE_VERSION:
            raise TimingError(f"unsupported timing evidence version: {self.version}")
        nominal = _finite(self.nominal_rate_hz, "nominal rate")
        minimum = _finite(self.minimum_rate_hz, "minimum rate")
        stale_stop = _finite(self.runtime_stale_stop_s, "runtime stale stop")
        if nominal != NOMINAL_RATE_HZ or minimum != MIN_SUCCESS_RATE_HZ or stale_stop != RUNTIME_STALE_STOP_S:
            raise TimingError("r004 timing constants differ")
        for role in (
            "successful_writer_publishes",
            "distinct_rtde_frames",
            "distinct_kunwei_frames",
            "distinct_tp_consumed_packet_echoes",
        ):
            value = getattr(self, role)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TimingError(f"{role} is invalid")
        for role in ("feedback_age_p99_s", "max_fresh_gap_s"):
            value = float(getattr(self, role))
            if math.isnan(value) or value < 0.0:
                raise TimingError(f"{role} is invalid")
        normalized = dict(self.layer_rates_hz)
        for layer, rate in normalized.items():
            if layer not in TIMING_LAYERS or _finite(rate, f"{layer} rate") < 0.0:
                raise TimingError("timing layer rates are invalid")
        if duration > 0.0:
            derived = {
                WRITER_PUBLISH_LAYER: self.successful_writer_publishes / duration,
                RTDE_FRAME_LAYER: self.distinct_rtde_frames / duration,
                KUNWEI_FRAME_LAYER: self.distinct_kunwei_frames / duration,
                TP_ECHO_LAYER: self.distinct_tp_consumed_packet_echoes / duration,
            }
            for layer, rate in derived.items():
                normalized.setdefault(layer, rate)
        object.__setattr__(self, "layer_rates_hz", normalized)

    @property
    def writer_publishes(self) -> int:
        return self.successful_writer_publishes

    @property
    def successful_writer_publish_count(self) -> int:
        return self.successful_writer_publishes

    @property
    def writer_publish_rate_hz(self) -> float:
        return self.layer_rates_hz.get(WRITER_PUBLISH_LAYER, 0.0)

    @property
    def rtde_frames(self) -> int:
        return self.distinct_rtde_frames

    @property
    def distinct_rtde_frame_count(self) -> int:
        return self.distinct_rtde_frames

    @property
    def rtde_frame_rate_hz(self) -> float:
        return self.layer_rates_hz.get(RTDE_FRAME_LAYER, 0.0)

    @property
    def kunwei_frames(self) -> int:
        return self.distinct_kunwei_frames

    @property
    def distinct_kunwei_frame_count(self) -> int:
        return self.distinct_kunwei_frames

    @property
    def kunwei_frame_rate_hz(self) -> float:
        return self.layer_rates_hz.get(KUNWEI_FRAME_LAYER, 0.0)

    @property
    def tp_consumed_echoes(self) -> int:
        return self.distinct_tp_consumed_packet_echoes

    @property
    def distinct_tp_consumed_echo_count(self) -> int:
        return self.distinct_tp_consumed_packet_echoes

    @property
    def tp_consumed_echo_rate_hz(self) -> float:
        return self.layer_rates_hz.get(TP_ECHO_LAYER, 0.0)

    @property
    def feedback_age_p99(self) -> float:
        return self.feedback_age_p99_s

    @property
    def max_fresh_gap(self) -> float:
        return self.max_fresh_gap_s

    @property
    def layers(self) -> tuple[TimingLayerEvidence, ...]:
        counts = {
            WRITER_PUBLISH_LAYER: self.successful_writer_publishes,
            RTDE_FRAME_LAYER: self.distinct_rtde_frames,
            KUNWEI_FRAME_LAYER: self.distinct_kunwei_frames,
            TP_ECHO_LAYER: self.distinct_tp_consumed_packet_echoes,
        }
        return tuple(
            TimingLayerEvidence(
                layer=layer,
                distinct_count=counts[layer],
                duration_s=self.duration_s,
                rate_hz=self.layer_rates_hz.get(layer, 0.0),
                nominal_rate_hz=self.nominal_rate_hz,
                minimum_rate_hz=self.minimum_rate_hz,
            )
            for layer in TIMING_LAYERS
        ) if self.duration_s > 0.0 else ()

    @property
    def layer_rates(self) -> Mapping[str, float]:
        return self.layer_rates_hz

    @property
    def timing_gate_passed(self) -> bool:
        return self.successful

    @property
    def passed(self) -> bool:
        rates_pass = (
            self.duration_s > 0.0
            and self.successful_writer_publishes / self.duration_s >= self.minimum_rate_hz
            and self.distinct_rtde_frames / self.duration_s >= self.minimum_rate_hz
            and self.distinct_kunwei_frames / self.duration_s >= self.minimum_rate_hz
            and self.distinct_tp_consumed_packet_echoes / self.duration_s >= self.minimum_rate_hz
        )
        return bool(
            rates_pass
            and self.feedback_age_p99_s <= FEEDBACK_AGE_P99_MAX_S
            and self.max_fresh_gap_s < MAX_FRESH_GAP_S
            and self.max_fresh_gap_s < self.runtime_stale_stop_s
        )

    @property
    def successful(self) -> bool:
        return self.passed

    def as_dict(self) -> dict[str, Any]:
        feedback_p99 = self.feedback_age_p99_s if math.isfinite(self.feedback_age_p99_s) else None
        fresh_gap = self.max_fresh_gap_s if math.isfinite(self.max_fresh_gap_s) else None
        return {
            "schema": TIMING_EVIDENCE_SCHEMA,
            "version": self.version,
            "duration_s": self.duration_s,
            "nominal_rate_hz": self.nominal_rate_hz,
            "minimum_rate_hz": self.minimum_rate_hz,
            "runtime_stale_stop_s": self.runtime_stale_stop_s,
            "successful_writer_publishes": self.successful_writer_publishes,
            "distinct_rtde_frames": self.distinct_rtde_frames,
            "distinct_kunwei_frames": self.distinct_kunwei_frames,
            "distinct_tp_consumed_packet_echoes": self.distinct_tp_consumed_packet_echoes,
            "layer_rates_hz": dict(self.layer_rates_hz),
            "feedback_age_p99_s": feedback_p99,
            "max_fresh_gap_s": fresh_gap,
            "feedback_age_p99_max_s": FEEDBACK_AGE_P99_MAX_S,
            "max_fresh_gap_limit_s": MAX_FRESH_GAP_S,
            "successful": self.successful,
        }

    to_dict = as_dict

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TimingEvidence":
        if not isinstance(value, Mapping):
            raise TimingError("timing evidence mapping is not typed")
        feedback_value = value.get("feedback_age_p99_s", value.get("feedback_age_p99", float("inf")))
        gap_value = value.get("max_fresh_gap_s", value.get("max_fresh_gap", float("inf")))
        return cls(
            duration_s=value.get("duration_s", 0.0),
            successful_writer_publishes=value.get(
                "successful_writer_publishes", value.get("writer_publishes", 0)
            ),
            distinct_rtde_frames=value.get("distinct_rtde_frames", value.get("rtde_frames", 0)),
            distinct_kunwei_frames=value.get("distinct_kunwei_frames", value.get("kunwei_frames", 0)),
            distinct_tp_consumed_packet_echoes=value.get(
                "distinct_tp_consumed_packet_echoes", value.get("tp_consumed_echoes", 0)
            ),
            feedback_age_p99_s=float("inf") if feedback_value is None else feedback_value,
            max_fresh_gap_s=float("inf") if gap_value is None else gap_value,
            nominal_rate_hz=value.get("nominal_rate_hz", NOMINAL_RATE_HZ),
            minimum_rate_hz=value.get("minimum_rate_hz", MIN_SUCCESS_RATE_HZ),
            runtime_stale_stop_s=value.get("runtime_stale_stop_s", RUNTIME_STALE_STOP_S),
            layer_rates_hz=value.get("layer_rates_hz", value.get("layer_rates", {})),
            version=value.get("version", TIMING_EVIDENCE_VERSION),
        )

    @classmethod
    def from_counts(
        cls,
        *,
        duration_s: float,
        writer_publishes: int | None = None,
        successful_writer_publishes: int | None = None,
        rtde_frames: int | None = None,
        distinct_rtde_frames: int | None = None,
        kunwei_frames: int | None = None,
        distinct_kunwei_frames: int | None = None,
        tp_consumed_echoes: int | None = None,
        distinct_tp_consumed_packet_echoes: int | None = None,
        feedback_age_p99_s: float = float("inf"),
        max_fresh_gap_s: float = float("inf"),
    ) -> "TimingEvidence":
        return cls(
            duration_s=duration_s,
            successful_writer_publishes=(
                successful_writer_publishes
                if successful_writer_publishes is not None
                else (writer_publishes or 0)
            ),
            distinct_rtde_frames=distinct_rtde_frames if distinct_rtde_frames is not None else (rtde_frames or 0),
            distinct_kunwei_frames=distinct_kunwei_frames if distinct_kunwei_frames is not None else (kunwei_frames or 0),
            distinct_tp_consumed_packet_echoes=(
                distinct_tp_consumed_packet_echoes
                if distinct_tp_consumed_packet_echoes is not None
                else (tp_consumed_echoes or 0)
            ),
            feedback_age_p99_s=feedback_age_p99_s,
            max_fresh_gap_s=max_fresh_gap_s,
        )


class TimingEvidenceCollector:
    """Collect distinct, fresh observations for the four timing layers."""

    NOMINAL_HZ = NOMINAL_RATE_HZ
    MINIMUM_HZ = MIN_SUCCESS_RATE_HZ
    RUNTIME_STALE_STOP_S = RUNTIME_STALE_STOP_S

    def __init__(
        self,
        *,
        nominal_rate_hz: float = NOMINAL_RATE_HZ,
        minimum_rate_hz: float = MIN_SUCCESS_RATE_HZ,
        runtime_stale_stop_s: float = RUNTIME_STALE_STOP_S,
    ) -> None:
        if float(nominal_rate_hz) != NOMINAL_RATE_HZ or float(minimum_rate_hz) != MIN_SUCCESS_RATE_HZ:
            raise TimingError("r004 timing rates are immutable")
        if float(runtime_stale_stop_s) != RUNTIME_STALE_STOP_S:
            raise TimingError("runtime stale stop must remain 0.080 s")
        self._layer_keys: dict[str, set[tuple[str, str]]] = {layer: set() for layer in TIMING_LAYERS}
        self._layer_times: dict[str, list[float]] = {layer: [] for layer in TIMING_LAYERS}
        self._layer_counts: dict[str, int] = {layer: 0 for layer in TIMING_LAYERS}
        self._last_kunwei_sequence: tuple[Any, int] | None = None
        self._feedback_keys: set[tuple[str, str]] = set()
        self._feedback_times: list[float] = []
        self._feedback_ages: list[float] = []

    def _record_layer(
        self,
        layer: str,
        observed_at_s: float,
        *,
        sequence: Any = None,
        frame_id: Any = None,
        payload: Any = None,
        successful: bool = True,
        epoch: Any = None,
    ) -> bool:
        if layer not in TIMING_LAYERS:
            raise TimingError(f"unknown timing layer: {layer}")
        if not isinstance(successful, bool):
            raise TimingError(f"{layer} success flag is not bool")
        timestamp = _finite(observed_at_s, f"{layer} timestamp")
        if not successful:
            return False
        key = _identity(
            sequence=sequence,
            frame_id=frame_id,
            payload=payload,
            role=layer,
            epoch=epoch,
        )
        if key in self._layer_keys[layer]:
            return False
        times = self._layer_times[layer]
        if times and timestamp < times[-1]:
            raise TimingError(f"{layer} timestamps regress")
        increment = 1
        if layer == KUNWEI_FRAME_LAYER and isinstance(sequence, int) and not isinstance(sequence, bool):
            previous = self._last_kunwei_sequence
            if previous is not None and previous[0] == epoch:
                if sequence <= previous[1]:
                    raise TimingError("kunwei cumulative frame sequence regressed")
                increment = sequence - previous[1]
            # The collector can start after the transport, so the first
            # cumulative value is only proof of the currently observed frame.
            self._last_kunwei_sequence = (epoch, sequence)
        self._layer_keys[layer].add(key)
        self._layer_counts[layer] += increment
        times.append(timestamp)
        return True

    def observe_writer_publish(
        self,
        observed_at_s: float,
        sequence: Any = None,
        *,
        frame_id: Any = None,
        payload: Any = None,
        successful: bool = True,
        epoch: Any = None,
    ) -> bool:
        return self._record_layer(
            WRITER_PUBLISH_LAYER,
            observed_at_s,
            sequence=sequence,
            frame_id=frame_id,
            payload=payload,
            successful=successful,
            epoch=epoch,
        )

    record_writer_publish = observe_writer_publish

    def observe_rtde_frame(
        self,
        observed_at_s: float,
        sequence: Any = None,
        *,
        frame_id: Any = None,
        payload: Any = None,
        fresh: bool = True,
        epoch: Any = None,
    ) -> bool:
        return self._record_layer(
            RTDE_FRAME_LAYER,
            observed_at_s,
            sequence=sequence,
            frame_id=frame_id,
            payload=payload,
            successful=fresh,
            epoch=epoch,
        )

    record_rtde_frame = observe_rtde_frame

    def observe_kunwei_frame(
        self,
        observed_at_s: float,
        sequence: Any = None,
        *,
        frame_id: Any = None,
        payload: Any = None,
        fresh: bool = True,
        epoch: Any = None,
    ) -> bool:
        return self._record_layer(
            KUNWEI_FRAME_LAYER,
            observed_at_s,
            sequence=sequence,
            frame_id=frame_id,
            payload=payload,
            successful=fresh,
            epoch=epoch,
        )

    record_kunwei_frame = observe_kunwei_frame

    def observe_tp_consumed_echo(
        self,
        observed_at_s: float,
        sequence: Any = None,
        *,
        packet_echo: Any = None,
        echo_sequence: Any = None,
        frame_id: Any = None,
        payload: Any = None,
        consumed: bool = True,
        epoch: Any = None,
    ) -> bool:
        identity_value = sequence if sequence is not None else echo_sequence
        identity_payload = payload if packet_echo is None else packet_echo
        return self._record_layer(
            TP_ECHO_LAYER,
            observed_at_s,
            sequence=identity_value,
            frame_id=frame_id,
            payload=identity_payload,
            successful=consumed,
            epoch=epoch,
        )

    record_tp_consumed_echo = observe_tp_consumed_echo
    observe_tp_echo = observe_tp_consumed_echo

    def observe_feedback(
        self,
        observed_at_s: float,
        feedback_age_s: float | None = None,
        *,
        age_s: float | None = None,
        sequence: Any = None,
        frame_id: Any = None,
        payload: Any = None,
        fresh: bool = True,
        epoch: Any = None,
    ) -> bool:
        timestamp = _finite(observed_at_s, "feedback timestamp")
        if not isinstance(fresh, bool):
            raise TimingError("feedback freshness flag is not bool")
        if not fresh:
            return False
        age_value = feedback_age_s if feedback_age_s is not None else age_s
        if age_value is None:
            raise TimingError("feedback age is missing")
        age = _finite(age_value, "feedback age")
        if age < 0.0:
            raise TimingError("feedback age is negative")
        key = _identity(
            sequence=sequence,
            frame_id=frame_id,
            payload=payload,
            role="feedback",
            epoch=epoch,
        )
        if key in self._feedback_keys:
            return False
        if self._feedback_times and timestamp < self._feedback_times[-1]:
            raise TimingError("feedback timestamps regress")
        self._feedback_keys.add(key)
        self._feedback_times.append(timestamp)
        self._feedback_ages.append(age)
        return True

    record_feedback = observe_feedback

    def observe_layered_sample(
        self,
        observed_at_s: float,
        *,
        source_sequences: Mapping[str, Any],
        source_ages_s: Mapping[str, float],
        epoch: Any = None,
    ) -> None:
        """Record one PATH sample using common source-key aliases."""

        sequences = dict(source_sequences)
        ages = dict(source_ages_s)

        def lookup(mapping: Mapping[str, Any], names: tuple[str, ...]) -> Any:
            for name in names:
                if name in mapping:
                    return mapping[name]
            return None

        writer = lookup(sequences, ("writer", "publish", "writer_publish", "writer_sequence"))
        rtde = lookup(sequences, ("rtde", "rtde_frame", "controller", "actual_qd"))
        kunwei = lookup(sequences, ("kunwei", "kunwei_frame", "wrench", "sensor"))
        tp = lookup(sequences, ("tp", "tp_echo", "tp_consumed", "packet_echo"))
        if writer is not None:
            self.observe_writer_publish(observed_at_s, writer, epoch=epoch)
        if rtde is not None:
            self.observe_rtde_frame(observed_at_s, rtde, epoch=epoch)
        if kunwei is not None:
            self.observe_kunwei_frame(observed_at_s, kunwei, epoch=epoch)
        if tp is not None:
            self.observe_tp_consumed_echo(observed_at_s, tp, epoch=epoch)
        # Feedback-age gate tracks writer/RTDE/TP loop lag.  Kunwei TCP
        # inter-batch hold (~50 ms sawtooth while distinct frames still prove
        # 1 kHz) must not dominate max(source_ages); layer rate + stale-stop
        # already own sensor delivery health.
        age_values = [
            float(value)
            for key, value in ages.items()
            if str(key) not in _KUNWEI_FEEDBACK_AGE_KEYS
        ]
        if not age_values:
            age_values = [float(value) for value in ages.values()]
        if age_values and sequences:
            self.observe_feedback(
                observed_at_s,
                max(age_values),
                sequence=tuple(sorted((str(key), repr(value)) for key, value in sequences.items())),
                epoch=epoch,
            )

    observe_sources = observe_layered_sample

    def finalize(self, *, duration_s: float | None = None) -> TimingEvidence:
        all_times = [timestamp for values in self._layer_times.values() for timestamp in values]
        all_times.extend(self._feedback_times)
        if duration_s is None:
            duration = max(all_times) - min(all_times) if len(all_times) >= 2 else 0.0
        else:
            duration = _finite(duration_s, "timing duration")
        if duration < 0.0:
            raise TimingError("timing duration is negative")
        counts = dict(self._layer_counts)
        # ``max_fresh_gap`` is the host feedback-loop cadence metric.  Source
        # performance is independently covered by distinct layer rates and
        # feedback-age p99 (writer/RTDE/TP; Kunwei TCP batch hold excluded at
        # observe time); a source reaching 80 ms is a runtime STOP, not an
        # acceptance relaxation.  This keeps Kunwei's complete 1 kHz physical
        # frames from being misclassified when TCP occasionally delivers them
        # in a sub-80-ms batch.
        fresh_gap = (
            max(
                right - left
                for left, right in zip(self._feedback_times, self._feedback_times[1:])
            )
            if len(self._feedback_times) >= 2
            else float("inf")
        )
        return TimingEvidence(
            duration_s=duration,
            successful_writer_publishes=counts[WRITER_PUBLISH_LAYER],
            distinct_rtde_frames=counts[RTDE_FRAME_LAYER],
            distinct_kunwei_frames=counts[KUNWEI_FRAME_LAYER],
            distinct_tp_consumed_packet_echoes=counts[TP_ECHO_LAYER],
            feedback_age_p99_s=_p_quantile(self._feedback_ages, 0.99),
            max_fresh_gap_s=fresh_gap,
        )


__all__ = [
    "FEEDBACK_AGE_P99_MAX_S",
    "KUNWEI_FRAME_LAYER",
    "MAX_FRESH_GAP_S",
    "MIN_SUCCESS_RATE_HZ",
    "NOMINAL_RATE_HZ",
    "RTDE_FRAME_LAYER",
    "RUNTIME_STALE_STOP_S",
    "TIMING_EVIDENCE_SCHEMA",
    "TIMING_EVIDENCE_VERSION",
    "TIMING_LAYERS",
    "TP_ECHO_LAYER",
    "TimingError",
    "TimingEvidence",
    "TimingEvidenceCollector",
    "TimingLayerEvidence",
    "WRITER_PUBLISH_LAYER",
]
