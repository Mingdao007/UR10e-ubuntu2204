"""Pure V5 sealed-lifecycle binding and append-only physical admission.

Only an already sealed R013 lifecycle artifact, its cold receipt, and its
cold-verified event bundle are consumed here.  Metric construction is always
derived from the immutable recorder rows; caller-provided bins or closure
booleans are never an authority for admission.  This module deliberately has
no RTDE, sensor, controller, motion, or live-reader dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from types import MappingProxyType
from typing import Any

try:  # Tests may put ``tools`` directly on sys.path.
    from step5d_autotune_v4_r013.lifecycle_trace import (
        FLAG_COMMAND_PRESENT,
        FLAG_INVALID,
        FLAG_OUTPUT_PRESENT,
        FLAG_SENSOR_FRESH,
        FLAG_SENSOR_PRESENT,
        FLAG_TERMINAL,
        load_lifecycle_artifact,
    )
    from step5d_autotune_v4_r004.runtime import TimingGuard
    from step6_figure8_autotune_v1.v5_composition_contract import (
        RolloverCommand,
        V5AttemptKind,
        V5TPState,
    )
    from step6_figure8_autotune_v1.v5_rollover import CandidateIdentityV1
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step5d_autotune_v4_r013.lifecycle_trace import (
        FLAG_COMMAND_PRESENT,
        FLAG_INVALID,
        FLAG_OUTPUT_PRESENT,
        FLAG_SENSOR_FRESH,
        FLAG_SENSOR_PRESENT,
        FLAG_TERMINAL,
        load_lifecycle_artifact,
    )
    from tools.step5d_autotune_v4_r004.runtime import TimingGuard
    from tools.step6_figure8_autotune_v1.v5_composition_contract import (
        RolloverCommand,
        V5AttemptKind,
        V5TPState,
    )
    from tools.step6_figure8_autotune_v1.v5_rollover import CandidateIdentityV1


V5_LIFECYCLE_SCHEMA = "step6.autotune/figure8-v5-sealed-lifecycle-v2"
V5_LIFECYCLE_VERSION = 2
V5_LEDGER_SCHEMA = "step6.autotune/figure8-v5-physical-admission-ledger-v2"
V5_LEDGER_VERSION = 2
V5_RECEIPT_SCHEMA = "step6.autotune/figure8-v5-admission-receipt-v2"
V5_TELL_SCHEMA = "step6.autotune/figure8-v5-tell-state-v2"
V5_EVENT_SCHEMA = "step6.autotune/figure8-v5-lifecycle-event-v2"
V5_EVENT_BUNDLE_SCHEMA = "step6.autotune/figure8-v5-event-bundle-v2"
V5_ARTIFACT_BINDING_SCHEMA = "step6.autotune/figure8-v5-artifact-binding-v2"
V5_GATE_OBSERVATION_SCHEMA = "step6.autotune/figure8-v5-gate-observation-v2"
V5_GATE_OBSERVATION_ARTIFACT_SCHEMA = (
    "step6.autotune/figure8-v5-gate-observation-artifact-v2"
)
V5_GATE_OBSERVATION_BINDING_SCHEMA = (
    "step6.autotune/figure8-v5-gate-observation-binding-v2"
)
V5_GATE_OBSERVATION_VERSION = 2
R013_LIFECYCLE_SCHEMA = "step5d.autotune-v4/r013-force-lifecycle-v1"
R013_LIFECYCLE_RECEIPT_SCHEMA = "step5d.autotune-v4/r013-force-lifecycle-receipt-v1"
GENESIS_SHA256 = "0" * 64
PATH_START_S = 0.0
PATH_END_S = 60.0
TAIL_END_S = 20.0 * math.pi
BIN_WIDTH_S = 0.1
EVIDENCE_BIN_COUNT = 600
FORMAL_BIN_COUNT = 550
METRIC_SIGNAL_NAME = "filtered_normal_n"
MAX_ATTEMPTS_PER_CHAIN = 5
MAX_ROLLOVERS_PER_CHAIN = 4
_METRIC_STATES = frozenset({V5TPState.PATH, V5TPState.ROLLOVER_PREPARED})
_GATE_FAMILY_NAMES = (
    "safety",
    "timing",
    "freshness",
    "tube_cbf",
    "identity",
    "command_envelope",
)


class V5LifecycleLedgerError(RuntimeError):
    """Deterministic fail-closed error for V5 evidence or ledger state."""


class V5AmbiguousTellError(V5LifecycleLedgerError):
    """An optimizer side effect may have happened without a durable receipt."""


class BoundaryMode(str, Enum):
    HOME = "HOME"
    CONTACT_ROLLOVER = "CONTACT_ROLLOVER"


class LedgerRole(str, Enum):
    PRIMARY = "PRIMARY"
    CORRECTION = "CORRECTION"


class MetricWindow(str, Enum):
    EVIDENCE = "evidence_0_60"
    FORMAL = "formal_5_60"


class LifecycleEventKind(str, Enum):
    CHAIN_START = "CHAIN_START"
    CONTACT_ROLLOVER = "CONTACT_ROLLOVER"
    HOME = "HOME"


class TellState(str, Enum):
    PREPARED = "PREPARED"
    AUTHORIZED = "AUTHORIZED"
    AMBIGUOUS = "AMBIGUOUS"
    OPTIMIZER_RECEIPT = "OPTIMIZER_RECEIPT"
    COMMITTED = "COMMITTED"


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V5LifecycleLedgerError("V5 value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_sha(value: Any, role: str) -> str:
    if type(value) is not str or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise V5LifecycleLedgerError(f"{role} must be a lowercase SHA-256")
    return value


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise V5LifecycleLedgerError(f"{role} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V5LifecycleLedgerError(f"{role} must be finite") from exc
    if not math.isfinite(result):
        raise V5LifecycleLedgerError(f"{role} must be finite")
    return result


def _cold_control_start_sample(
    source_rows: Sequence[Mapping[str, Any]],
) -> int:
    """Locate the V5-only source-row marker preceding the first control tick."""

    required = FLAG_OUTPUT_PRESENT | FLAG_SENSOR_PRESENT | FLAG_SENSOR_FRESH
    excluded = FLAG_COMMAND_PRESENT | FLAG_TERMINAL | FLAG_INVALID
    marker_indices = tuple(
        index
        for index, row in enumerate(source_rows[:-1])
        if int(row.get("flags", 0)) & required == required
        and not (int(row.get("flags", 0)) & excluded)
        and row.get("command_mode") is None
        and int(row.get("packet_sequence", -2)) == -1
        and int(row.get("consumed_packet_sequence", -2)) == -1
        and tuple(float(value) for value in row.get("qdot", ())) == (0.0,) * 6
    )
    if len(marker_indices) != 1:
        raise V5LifecycleLedgerError(
            "lifecycle does not contain exactly one V5 CONTROL_START marker"
        )
    control_start = marker_indices[0] + 1
    if not (
        int(source_rows[control_start].get("flags", 0)) & FLAG_COMMAND_PRESENT
    ):
        raise V5LifecycleLedgerError(
            "V5 CONTROL_START marker is not followed by a command tick"
        )
    return control_start


def _integer(value: Any, role: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < 0 or (positive and value == 0):
        raise V5LifecycleLedgerError(f"{role} must be a typed {'positive' if positive else 'non-negative'} integer")
    if value > 2**31 - 1:
        raise V5LifecycleLedgerError(f"{role} exceeds the signed integer range")
    return value


def _freeze(value: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise V5LifecycleLedgerError(f"{role} must be a mapping")
    return MappingProxyType(dict(value))


def _identity_from_dict(value: Mapping[str, Any]) -> CandidateIdentityV1:
    if not isinstance(value, Mapping):
        raise V5LifecycleLedgerError("candidate identity is not a mapping")
    try:
        return CandidateIdentityV1(
            epoch=_integer(value["epoch"], "candidate epoch"),
            ordinal=_integer(value["ordinal"], "candidate ordinal", positive=True),
            attempt_kind=V5AttemptKind(_integer(value["attempt_kind"], "candidate kind")),
            candidate_token=_integer(value["candidate_token"], "candidate token", positive=True),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise V5LifecycleLedgerError("candidate identity is invalid") from exc


def _identity_dict(identity: CandidateIdentityV1) -> dict[str, Any]:
    return identity.as_dict()


_GATE_OBSERVATION_INPUT_KEYS = {
    "sample_index",
    "qdot",
    "gate_receipt_sha256",
    "safety_normal",
    "stop_request",
    "controller_epoch",
    "controller_ordinal",
    "controller_candidate_token",
    "guard_terminal_stop",
    "guard_soft_fail_closed",
    "command_envelope_allowed",
    "host_epoch",
    "host_ordinal",
    "host_candidate_token",
    "rollover_command",
    "rollover_generation",
    "activation_pending",
    "activation_durable",
}
_GATE_OBSERVATION_COLUMNS = (
    "sample_index",
    "qdot",
    "gate_receipt_sha256",
    "safety_normal",
    "stop_request",
    "controller_epoch",
    "controller_ordinal",
    "controller_candidate_token",
    "guard_terminal_stop",
    "guard_soft_fail_closed",
    "command_envelope_allowed",
    "host_epoch",
    "host_ordinal",
    "host_candidate_token",
    "rollover_command",
    "rollover_generation",
    "activation_pending",
    "activation_durable",
)
_GATE_TIMING_COLUMNS = (
    "sample_index",
    "monotonic_s",
    "previous_command_sample_index",
    "actual_dt_s",
)
_GATE_OBSERVATION_BINDING_KEYS = {
    "schema",
    "version",
    "artifact_path",
    "artifact_sha256",
    "artifact_size",
    "source_artifact_sha256",
    "timing_start_sample_index",
    "timing_end_sample_index",
    "timing_count",
    "observation_count",
    "observation_head_sha256",
}

# One exact-hash cache avoids repeatedly parsing and validating the same
# multi-megabyte sidecar while a chain's individual attempt records are cold
# reconstructed.  Every lookup still reads and hashes the bytes first, so a
# changed file or binding can never reuse a cached validation result.
_GATE_OBSERVATION_COLD_CACHE: tuple[tuple[Any, ...], dict[str, Any]] | None = None
_SEALED_ARTIFACT_COLD_CACHE: tuple[tuple[Any, ...], "SealedR013ArtifactV2"] | None = None


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return _canonical(dict(value)) + b"\n"


def _atomic_write_exact(path: Path, payload: bytes, role: str) -> None:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise V5LifecycleLedgerError(f"{role} path is a symlink")
    if target.exists():
        if not target.is_file() or target.read_bytes() != payload:
            raise V5LifecycleLedgerError(f"{role} already exists with different bytes")
        return
    descriptor, temporary_value = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-", dir=target.parent
    )
    temporary = Path(temporary_value)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(
            target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _normalize_runtime_timing_acceptance(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or type(value.get("passed")) is not bool:
        raise V5LifecycleLedgerError("runtime timing acceptance is not typed")
    stop_reason = value.get("stop_reason")
    if not isinstance(stop_reason, str):
        raise V5LifecycleLedgerError("runtime timing stop reason is not typed")
    return {
        "passed": value["passed"],
        "rate_hz": _finite(value.get("rate_hz"), "runtime timing rate"),
        "p99_gap_s": _finite(value.get("p99_gap_s"), "runtime timing p99"),
        "max_gap_s": _finite(value.get("max_gap_s"), "runtime timing maximum"),
        "stop_reason": stop_reason,
    }


def _timing_acceptance_matches(
    runtime: Mapping[str, Any], cold: Mapping[str, Any]
) -> bool:
    return bool(
        runtime["passed"] is cold["passed"]
        and runtime["stop_reason"] == cold["stop_reason"]
        and all(
            math.isclose(
                float(runtime[name]),
                float(cold[name]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            for name in ("rate_hz", "p99_gap_s", "max_gap_s")
        )
    )


def persist_gate_observation_artifact(
    target_path: Path,
    *,
    source_artifact_path: Path,
    observations: Sequence[Mapping[str, Any]],
    timing_sample_indices: Sequence[int],
    control_start_sample_index: int,
    control_end_sample_index: int,
    runtime_timing_acceptance: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically persist the compact raw inputs used by existing V5 gates.

    This is evidence for the already-deployed safety/timing/tube/identity and
    command-envelope families.  It does not define a new gate or a force
    readiness window.
    """

    source = Path(source_artifact_path).resolve()
    if source.is_symlink() or not source.is_file():
        raise V5LifecycleLedgerError("gate observation source artifact is absent")
    source_bytes = source.read_bytes()
    source_sha256 = _sha256(source_bytes)
    try:
        _metadata, source_rows = load_lifecycle_artifact(source)
    except Exception as exc:
        raise V5LifecycleLedgerError(
            "gate observation source artifact cold read failed"
        ) from exc
    if any(
        row.get("sample_index") != expected
        for expected, row in enumerate(source_rows)
    ):
        raise V5LifecycleLedgerError(
            "gate observation source sample indices are not contiguous"
        )

    expected_gate_samples = tuple(
        index
        for index, row in enumerate(source_rows)
        if int(row.get("flags", 0)) & FLAG_COMMAND_PRESENT
        and row.get("command_mode") == 2
    )
    supplied_timing_samples = tuple(
        _integer(value, "gate timing sample index")
        for value in timing_sample_indices
    )
    if (
        len(expected_gate_samples) < 2
        or len(supplied_timing_samples) < 2
        or any(
            current <= previous
            for previous, current in zip(
                supplied_timing_samples, supplied_timing_samples[1:]
            )
        )
        or supplied_timing_samples[-1] >= len(source_rows)
    ):
        raise V5LifecycleLedgerError("gate timing sample sequence is invalid")
    control_start = _integer(
        control_start_sample_index, "control timing start sample"
    )
    control_end = _integer(control_end_sample_index, "control timing end sample")
    cold_control_start = _cold_control_start_sample(source_rows)
    terminal_index = len(source_rows) - 1
    command_samples_before_home = tuple(
        index
        for index, row in enumerate(source_rows[:terminal_index])
        if int(row.get("flags", 0)) & FLAG_COMMAND_PRESENT
    )
    if (
        not command_samples_before_home
        or control_start != cold_control_start
        or supplied_timing_samples[0] != control_start
        or supplied_timing_samples[-1] != control_end
        or control_end != command_samples_before_home[-1]
    ):
        raise V5LifecycleLedgerError(
            "gate timing rows differ from CONTROL_START/CONTROL_END"
        )
    expected_timing_samples = tuple(
        index
        for index in range(
            supplied_timing_samples[0], supplied_timing_samples[-1] + 1
        )
        if int(source_rows[index].get("flags", 0)) & FLAG_COMMAND_PRESENT
    )
    if supplied_timing_samples != expected_timing_samples:
        raise V5LifecycleLedgerError(
            "gate timing rows do not exactly cover runtime command ticks"
        )
    first_gate, last_gate = expected_gate_samples[0], expected_gate_samples[-1]
    command_samples_in_motion = tuple(
        index
        for index in range(first_gate, last_gate + 1)
        if int(source_rows[index].get("flags", 0)) & FLAG_COMMAND_PRESENT
    )
    if command_samples_in_motion != expected_gate_samples:
        raise V5LifecycleLedgerError(
            "non-PATH command interrupts the gate-bearing PATH interval"
        )

    timing_rows: list[list[Any]] = []
    previous_timing_sample: int | None = None
    for sample_index in supplied_timing_samples:
        monotonic_s = _finite(
            source_rows[sample_index].get("monotonic_s"),
            "gate timing monotonic",
        )
        if previous_timing_sample is None:
            previous_index: int | None = None
            actual_dt_s = 0.002
        else:
            previous_index = previous_timing_sample
            previous_monotonic_s = _finite(
                source_rows[previous_index].get("monotonic_s"),
                "gate timing preceding monotonic",
            )
            actual_dt_s = monotonic_s - previous_monotonic_s
        if actual_dt_s <= 0.0:
            raise V5LifecycleLedgerError("gate timing source clock is non-increasing")
        timing_rows.append(
            [sample_index, monotonic_s, previous_index, actual_dt_s]
        )
        previous_timing_sample = sample_index

    runtime_timing = _normalize_runtime_timing_acceptance(
        runtime_timing_acceptance
    )
    cold_timing = _cold_timing_acceptance(
        tuple(float(row[1]) for row in timing_rows)
    )
    if not _timing_acceptance_matches(runtime_timing, cold_timing):
        raise V5LifecycleLedgerError(
            "runtime and cold lifecycle timing acceptance differ"
        )

    previous = GENESIS_SHA256
    rows: list[list[Any]] = []
    observed_samples: list[int] = []
    for sequence_index, raw in enumerate(observations):
        if not isinstance(raw, Mapping) or set(raw) != _GATE_OBSERVATION_INPUT_KEYS:
            raise V5LifecycleLedgerError("gate observation input schema differs")
        normalized = dict(raw)
        sample_index = _integer(
            normalized.get("sample_index"), "gate observation sample index"
        )
        qdot = normalized.get("qdot")
        if (
            not isinstance(qdot, Sequence)
            or isinstance(qdot, (str, bytes))
            or len(qdot) != 6
            or any(not math.isfinite(float(item)) for item in qdot)
        ):
            raise V5LifecycleLedgerError("gate observation qdot is invalid")
        _require_sha(
            normalized.get("gate_receipt_sha256"), "gate receipt hash"
        )
        for name in (
            "safety_normal",
            "stop_request",
            "guard_terminal_stop",
            "guard_soft_fail_closed",
            "command_envelope_allowed",
            "activation_pending",
            "activation_durable",
        ):
            if type(normalized.get(name)) is not bool:
                raise V5LifecycleLedgerError(
                    "gate observation boolean input is untyped"
                )
        for name in (
            "controller_epoch",
            "controller_ordinal",
            "controller_candidate_token",
            "host_epoch",
            "host_ordinal",
            "host_candidate_token",
            "rollover_command",
            "rollover_generation",
        ):
            if type(normalized.get(name)) is not int:
                raise V5LifecycleLedgerError(
                    "gate controller identity echo is invalid"
                )
        if sample_index >= len(source_rows) or tuple(float(item) for item in qdot) != tuple(
            float(item) for item in source_rows[sample_index]["qdot"]
        ):
            raise V5LifecycleLedgerError(
                "gate observation does not bind its lifecycle qdot"
            )
        observed_samples.append(sample_index)
        row_for_hash = {
            "schema": V5_GATE_OBSERVATION_SCHEMA,
            "version": V5_GATE_OBSERVATION_VERSION,
            "sequence_index": sequence_index,
            **normalized,
        }
        row_sha256 = canonical_sha256(row_for_hash)
        previous = canonical_sha256(
            {
                "previous_sha256": previous,
                "observation_row_sha256": row_sha256,
            }
        )
        rows.append([normalized[name] for name in _GATE_OBSERVATION_COLUMNS])
    if tuple(observed_samples) != expected_gate_samples:
        raise V5LifecycleLedgerError(
            "gate observations do not exactly cover lifecycle PATH commands"
        )
    body = {
        "schema": V5_GATE_OBSERVATION_ARTIFACT_SCHEMA,
        "version": V5_GATE_OBSERVATION_VERSION,
        "source_artifact_path": str(source),
        "source_artifact_sha256": source_sha256,
        "source_artifact_size": len(source_bytes),
        "timing_columns": list(_GATE_TIMING_COLUMNS),
        "timing_rows": timing_rows,
        "timing_count": len(timing_rows),
        "runtime_timing_acceptance": runtime_timing,
        "observation_columns": list(_GATE_OBSERVATION_COLUMNS),
        "observations": rows,
        "observation_count": len(rows),
        "observation_head_sha256": previous,
    }
    payload = _canonical_json_bytes(body)
    target = Path(target_path).resolve()
    _atomic_write_exact(target, payload, "gate observation artifact")
    return {
        "schema": V5_GATE_OBSERVATION_BINDING_SCHEMA,
        "version": V5_GATE_OBSERVATION_VERSION,
        "artifact_path": str(target),
        "artifact_sha256": _sha256(payload),
        "artifact_size": len(payload),
        "source_artifact_sha256": source_sha256,
        "timing_start_sample_index": control_start,
        "timing_end_sample_index": control_end,
        "timing_count": len(timing_rows),
        "observation_count": len(rows),
        "observation_head_sha256": previous,
    }


def _normalize_gate_observation_binding(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _GATE_OBSERVATION_BINDING_KEYS:
        raise V5LifecycleLedgerError("gate observation artifact binding schema differs")
    binding = dict(value)
    if (
        binding.get("schema") != V5_GATE_OBSERVATION_BINDING_SCHEMA
        or binding.get("version") != V5_GATE_OBSERVATION_VERSION
    ):
        raise V5LifecycleLedgerError("gate observation binding version differs")
    artifact_path = binding.get("artifact_path")
    if not isinstance(artifact_path, str) or not artifact_path:
        raise V5LifecycleLedgerError("gate observation artifact path is absent")
    _require_sha(binding.get("artifact_sha256"), "gate observation artifact hash")
    _require_sha(binding.get("source_artifact_sha256"), "gate observation source hash")
    _require_sha(binding.get("observation_head_sha256"), "gate observation head")
    _integer(binding.get("artifact_size"), "gate observation artifact size", positive=True)
    timing_start = _integer(
        binding.get("timing_start_sample_index"),
        "gate timing start sample",
    )
    timing_end = _integer(
        binding.get("timing_end_sample_index"),
        "gate timing end sample",
    )
    if timing_end <= timing_start:
        raise V5LifecycleLedgerError("gate timing boundary is empty")
    _integer(binding.get("timing_count"), "gate timing count", positive=True)
    _integer(binding.get("observation_count"), "gate observation count", positive=True)
    binding["artifact_path"] = str(Path(artifact_path).resolve())
    return binding


def _cold_timing_acceptance(monotonic_s: Sequence[float]) -> dict[str, Any]:
    values = tuple(float(value) for value in monotonic_s)
    if not values or any(not math.isfinite(value) for value in values):
        raise V5LifecycleLedgerError("cold timing clocks are invalid")
    guard = TimingGuard()
    origin = values[0]
    for value in values:
        guard.observe(value - origin)
    return {
        "schema": "step6.autotune/figure8-v5-cold-timing-acceptance-v1",
        "version": 1,
        "sample_count": len(values),
        **guard.acceptance(),
    }


def _gate_family_results(row: Mapping[str, Any]) -> dict[str, bool]:
    echo = row["controller_identity_echo"]
    return {
        "safety": bool(row["safety_normal"] and not row["stop_request"]),
        "timing": bool(0.0 < row["actual_dt_s"] < 0.08),
        "freshness": bool(row["sensor_fresh"]),
        "tube_cbf": bool(
            row["guard_terminal_stop"] is False
            and row["guard_soft_fail_closed"] is False
        ),
        "identity": bool(
            echo["epoch"] == row["expected_epoch"]
            and echo["ordinal"] == row["expected_ordinal"]
            and echo["candidate_token"] == row["expected_candidate_token"]
        ),
        "command_envelope": bool(row["command_envelope_allowed"]),
    }


def _load_gate_observation_artifact(
    binding_value: Mapping[str, Any],
    *,
    source_artifact_path: Path,
    source_artifact_sha256: str,
    source_artifact_size: int,
    source_rows: Sequence[Mapping[str, Any]],
    events: Sequence[LifecycleEventV2],
    switch_gate_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    global _GATE_OBSERVATION_COLD_CACHE

    binding = _normalize_gate_observation_binding(binding_value)
    path = Path(binding["artifact_path"])
    if path.is_symlink() or not path.is_file():
        raise V5LifecycleLedgerError("gate observation artifact is not a regular file")
    payload = path.read_bytes()
    payload_sha256 = _sha256(payload)
    if (
        len(payload) != binding["artifact_size"]
        or payload_sha256 != binding["artifact_sha256"]
    ):
        raise V5LifecycleLedgerError("gate observation artifact bytes differ")
    cache_key = (
        str(path.resolve()),
        payload_sha256,
        source_artifact_sha256,
        source_artifact_size,
        str(Path(source_artifact_path).resolve()),
        canonical_sha256([event.as_dict() for event in events]),
        canonical_sha256([dict(item) for item in switch_gate_receipts]),
    )
    if (
        _GATE_OBSERVATION_COLD_CACHE is not None
        and _GATE_OBSERVATION_COLD_CACHE[0] == cache_key
    ):
        return _GATE_OBSERVATION_COLD_CACHE[1]
    try:
        value = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
        raise V5LifecycleLedgerError("gate observation artifact is unreadable") from exc
    required_body_keys = {
        "schema",
        "version",
        "source_artifact_path",
        "source_artifact_sha256",
        "source_artifact_size",
        "timing_columns",
        "timing_rows",
        "timing_count",
        "runtime_timing_acceptance",
        "observation_columns",
        "observations",
        "observation_count",
        "observation_head_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required_body_keys
        or value.get("schema") != V5_GATE_OBSERVATION_ARTIFACT_SCHEMA
        or value.get("version") != V5_GATE_OBSERVATION_VERSION
        or payload != _canonical_json_bytes(value)
        or Path(str(value.get("source_artifact_path"))).resolve()
        != Path(source_artifact_path).resolve()
        or value.get("source_artifact_sha256") != source_artifact_sha256
        or value.get("source_artifact_size") != source_artifact_size
        or binding["source_artifact_sha256"] != source_artifact_sha256
    ):
        raise V5LifecycleLedgerError("gate observation artifact namespace differs")
    if value.get("timing_columns") != list(_GATE_TIMING_COLUMNS):
        raise V5LifecycleLedgerError("gate timing column schema differs")
    if value.get("observation_columns") != list(_GATE_OBSERVATION_COLUMNS):
        raise V5LifecycleLedgerError("gate observation column schema differs")

    expected_gate_samples = tuple(
        index
        for index, row in enumerate(source_rows)
        if int(row.get("flags", 0)) & FLAG_COMMAND_PRESENT
        and row.get("command_mode") == 2
    )
    if len(expected_gate_samples) < 2:
        raise V5LifecycleLedgerError("lifecycle PATH command coverage is incomplete")
    first_gate, last_gate = expected_gate_samples[0], expected_gate_samples[-1]
    command_samples_in_motion = tuple(
        index
        for index in range(first_gate, last_gate + 1)
        if int(source_rows[index].get("flags", 0)) & FLAG_COMMAND_PRESENT
    )
    if command_samples_in_motion != expected_gate_samples:
        raise V5LifecycleLedgerError(
            "non-PATH command interrupts the gate-bearing PATH interval"
        )

    raw_timing = value.get("timing_rows")
    if (
        not isinstance(raw_timing, Sequence)
        or isinstance(raw_timing, (str, bytes))
        or value.get("timing_count") != len(raw_timing)
        or len(raw_timing) < 2
    ):
        raise V5LifecycleLedgerError("gate timing row count differs")
    cold_control_start = _cold_control_start_sample(source_rows)
    if binding["timing_start_sample_index"] != cold_control_start:
        raise V5LifecycleLedgerError(
            "gate timing rows differ from cold V5 CONTROL_START marker"
        )
    timing: list[float] = []
    timing_monotonic: list[float] = []
    timing_samples: list[int] = []
    previous_timing_sample: int | None = None
    for raw in raw_timing:
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != len(_GATE_TIMING_COLUMNS)
        ):
            raise V5LifecycleLedgerError("gate timing row schema differs")
        sample_index = _integer(raw[0], "gate timing sample index")
        monotonic_s = _finite(raw[1], "gate timing monotonic")
        previous_sample_index = _integer(
            raw[2], "gate timing previous sample index"
        ) if raw[2] is not None else None
        actual_dt_s = _finite(raw[3], "gate timing actual dt")
        expected_previous = previous_timing_sample
        if (
            sample_index >= len(source_rows)
            or (timing_samples and sample_index <= timing_samples[-1])
            or not (int(source_rows[sample_index].get("flags", 0)) & FLAG_COMMAND_PRESENT)
            or previous_sample_index != expected_previous
            or not math.isclose(
                monotonic_s,
                float(source_rows[sample_index]["monotonic_s"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                actual_dt_s,
                0.002
                if expected_previous is None
                else monotonic_s
                - float(source_rows[expected_previous]["monotonic_s"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or actual_dt_s <= 0.0
        ):
            raise V5LifecycleLedgerError(
                "gate timing row is not cold-derived from lifecycle clocks"
            )
        timing.append(actual_dt_s)
        timing_monotonic.append(monotonic_s)
        timing_samples.append(sample_index)
        previous_timing_sample = sample_index
    expected_timing_samples = tuple(
        index
        for index in range(timing_samples[0], timing_samples[-1] + 1)
        if int(source_rows[index].get("flags", 0)) & FLAG_COMMAND_PRESENT
    )
    if tuple(timing_samples) != expected_timing_samples or not set(
        expected_gate_samples
    ).issubset(timing_samples):
        raise V5LifecycleLedgerError(
            "gate timing rows do not exactly cover runtime command ticks"
        )
    terminal_index = len(source_rows) - 1
    command_samples_before_home = tuple(
        index
        for index, row in enumerate(source_rows[:terminal_index])
        if int(row.get("flags", 0)) & FLAG_COMMAND_PRESENT
    )
    if (
        not command_samples_before_home
        or timing_samples[0] != cold_control_start
        or binding["timing_start_sample_index"] != timing_samples[0]
        or binding["timing_end_sample_index"] != timing_samples[-1]
        or binding["timing_count"] != len(timing_samples)
        or timing_samples[-1] != command_samples_before_home[-1]
    ):
        raise V5LifecycleLedgerError(
            "gate timing rows differ from CONTROL_START/CONTROL_END"
        )
    runtime_timing = _normalize_runtime_timing_acceptance(
        value.get("runtime_timing_acceptance")
    )
    cold_timing = _cold_timing_acceptance(timing_monotonic)
    if not _timing_acceptance_matches(runtime_timing, cold_timing):
        raise V5LifecycleLedgerError(
            "runtime and cold lifecycle timing acceptance differ"
        )
    timing_index_by_sample = {
        sample_index: index for index, sample_index in enumerate(timing_samples)
    }

    raw_observations = value.get("observations")
    if (
        not isinstance(raw_observations, Sequence)
        or isinstance(raw_observations, (str, bytes))
        or not raw_observations
        or value.get("observation_count") != len(raw_observations)
        or binding["observation_count"] != len(raw_observations)
        or len(raw_observations) != len(expected_gate_samples)
    ):
        raise V5LifecycleLedgerError("gate observation count differs")

    start_events = tuple(
        event for event in events if event.kind is LifecycleEventKind.CHAIN_START
    )
    rollover_events = tuple(
        event for event in events if event.kind is LifecycleEventKind.CONTACT_ROLLOVER
    )
    if len(start_events) != 1 or any(
        event.next_identity is None for event in rollover_events
    ):
        raise V5LifecycleLedgerError("gate observation lifecycle events are incomplete")
    chain_start = start_events[0]
    if chain_start.sample_index != expected_gate_samples[0]:
        raise V5LifecycleLedgerError(
            "CHAIN_START does not identify the first PATH command"
        )

    previous = GENESIS_SHA256
    observations: list[dict[str, Any]] = []
    active_start = chain_start
    active_identity = chain_start.identity
    rollover_index = 0
    for sequence_index, (expected_sample, raw) in enumerate(
        zip(expected_gate_samples, raw_observations, strict=True)
    ):
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes))
            or len(raw) != len(_GATE_OBSERVATION_COLUMNS)
        ):
            raise V5LifecycleLedgerError("gate observation row schema differs")
        compact = dict(zip(_GATE_OBSERVATION_COLUMNS, raw, strict=True))
        sample_index = _integer(compact.get("sample_index"), "gate sample index")
        if sample_index != expected_sample:
            raise V5LifecycleLedgerError("gate observation order/range differs")
        qdot = compact.get("qdot")
        if (
            not isinstance(qdot, Sequence)
            or isinstance(qdot, (str, bytes))
            or len(qdot) != 6
            or any(not math.isfinite(float(item)) for item in qdot)
        ):
            raise V5LifecycleLedgerError("gate observation qdot is invalid")
        for name in (
            "safety_normal",
            "stop_request",
            "guard_terminal_stop",
            "guard_soft_fail_closed",
            "command_envelope_allowed",
            "activation_pending",
            "activation_durable",
        ):
            if type(compact.get(name)) is not bool:
                raise V5LifecycleLedgerError("gate observation boolean input is untyped")
        for name in (
            "controller_epoch",
            "controller_ordinal",
            "controller_candidate_token",
            "host_epoch",
            "host_ordinal",
            "host_candidate_token",
            "rollover_command",
            "rollover_generation",
        ):
            if type(compact.get(name)) is not int:
                raise V5LifecycleLedgerError("gate controller identity echo is invalid")
        _require_sha(compact.get("gate_receipt_sha256"), "gate receipt hash")

        while (
            rollover_index < len(rollover_events)
            and rollover_events[rollover_index].sample_index < sample_index
        ):
            active_start = rollover_events[rollover_index]
            assert active_start.next_identity is not None
            active_identity = active_start.next_identity
            rollover_index += 1
        seam_event = (
            rollover_events[rollover_index]
            if rollover_index < len(rollover_events)
            and rollover_events[rollover_index].sample_index == sample_index
            else None
        )
        expected_identity = (
            seam_event.next_identity if seam_event is not None else active_identity
        )
        assert expected_identity is not None
        source_row = source_rows[sample_index]
        rtde_timestamp_s = _finite(
            source_row.get("rtde_timestamp_s"), "gate RTDE timestamp"
        )
        path_time_s = (
            TAIL_END_S
            if seam_event is not None
            else min(
                math.nextafter(TAIL_END_S, 0.0),
                max(0.0, rtde_timestamp_s - active_start.rtde_timestamp_s),
            )
        )
        phase = (
            "activation_pending"
            if compact["activation_pending"] and seam_event is None
            else "path" if path_time_s < PATH_END_S else "tail"
        )
        if (
            tuple(float(item) for item in qdot)
            != tuple(float(item) for item in source_row["qdot"])
        ):
            raise V5LifecycleLedgerError("gate observation does not bind its lifecycle row")

        raw_for_hash = {
            "schema": V5_GATE_OBSERVATION_SCHEMA,
            "version": V5_GATE_OBSERVATION_VERSION,
            "sequence_index": sequence_index,
            **compact,
        }
        row_sha256 = canonical_sha256(raw_for_hash)
        previous = canonical_sha256(
            {
                "previous_sha256": previous,
                "observation_row_sha256": row_sha256,
            }
        )
        normalized = {
            **raw_for_hash,
            "timing_index": timing_index_by_sample[sample_index],
            "phase": phase,
            "path_time_s": path_time_s,
            "actual_dt_s": timing[timing_index_by_sample[sample_index]],
            "tp_state": source_row.get("tp_state"),
            "rtde_timestamp_s": rtde_timestamp_s,
            "sensor_fresh": bool(
                int(source_row.get("flags", 0)) & FLAG_SENSOR_FRESH
            ),
            "normal_load_n": float(source_row["normal_load_n"]),
            "force_norm_n": float(source_row["force_norm_n"]),
            "torque_norm_nm": float(source_row["torque_norm_nm"]),
            "filtered_normal_n": float(source_row["filtered_normal_n"]),
            "controller_identity_echo": {
                "epoch": compact["controller_epoch"],
                "ordinal": compact["controller_ordinal"],
                "candidate_token": compact["controller_candidate_token"],
            },
            "expected_epoch": expected_identity.epoch,
            "expected_ordinal": expected_identity.ordinal,
            "expected_candidate_token": expected_identity.candidate_token,
            "row_sha256": row_sha256,
        }
        normalized["family_results"] = _gate_family_results(normalized)
        observations.append(normalized)

    # Each controller seam begins a bounded staged interval.  Until the
    # durability worker has cold-verified COMMIT_ACK+ACTIVATED, the host must
    # retain the old binding and publish only the exact seam seed under a
    # duplicate COMMIT.  The first non-pending command is the durable
    # successor publication and must be NONE/new-bound.
    activation_publications: list[dict[str, Any]] = []
    claimed_pending_samples: list[int] = []
    for event, switch_receipt in zip(
        rollover_events, switch_gate_receipts, strict=True
    ):
        seam_position = next(
            (
                index
                for index, row in enumerate(observations)
                if row["sample_index"] == event.sample_index
            ),
            None,
        )
        if seam_position is None or event.next_identity is None:
            raise V5LifecycleLedgerError(
                "activation-pending seam observation is absent"
            )
        seed = tuple(float(item) for item in switch_receipt["commit_seed_qdot"])
        pending_rows: list[Mapping[str, Any]] = []
        cursor = seam_position
        while cursor < len(observations) and observations[cursor][
            "activation_pending"
        ]:
            pending_rows.append(observations[cursor])
            cursor += 1
        if not pending_rows or cursor >= len(observations):
            raise V5LifecycleLedgerError(
                "activation-pending interval is empty or unterminated"
            )
        for row in pending_rows:
            if (
                row["activation_durable"] is not False
                or row["tp_state"] != int(V5TPState.ROLLOVER_COMMITTED)
                or row["rollover_command"] != int(RolloverCommand.COMMIT)
                or row["rollover_generation"] != event.generation
                or row["host_epoch"] != event.identity.epoch
                or row["host_ordinal"] != event.identity.ordinal
                or row["host_candidate_token"]
                != event.identity.candidate_token
                or row["controller_epoch"] != event.next_identity.epoch
                or row["controller_ordinal"] != event.next_identity.ordinal
                or row["controller_candidate_token"]
                != event.next_identity.candidate_token
                or tuple(float(item) for item in row["qdot"]) != seed
                or not all(row["family_results"].values())
            ):
                raise V5LifecycleLedgerError(
                    "activation-pending authority/gate evidence differs"
                )
        first_successor = observations[cursor]
        if (
            first_successor["activation_pending"] is not False
            or first_successor["activation_durable"] is not True
            or first_successor["rollover_command"] != int(RolloverCommand.NONE)
            or first_successor["host_epoch"] != event.next_identity.epoch
            or first_successor["host_ordinal"] != event.next_identity.ordinal
            or first_successor["host_candidate_token"]
            != event.next_identity.candidate_token
            or first_successor["sample_index"] <= pending_rows[-1]["sample_index"]
        ):
            raise V5LifecycleLedgerError(
                "first durable successor publication differs"
            )
        pending_sample_indices = [
            int(row["sample_index"]) for row in pending_rows
        ]
        claimed_pending_samples.extend(pending_sample_indices)
        activation_publications.append(
            {
                "generation": event.generation,
                "pending_sample_indices": pending_sample_indices,
                "first_successor_sample_index": int(
                    first_successor["sample_index"]
                ),
            }
        )
    observed_pending_samples = [
        int(row["sample_index"])
        for row in observations
        if row["activation_pending"]
    ]
    if claimed_pending_samples != observed_pending_samples:
        raise V5LifecycleLedgerError(
            "activation-pending interval re-entry or orphan evidence differs"
        )
    if (
        value.get("observation_head_sha256") != previous
        or binding["observation_head_sha256"] != previous
    ):
        raise V5LifecycleLedgerError("gate observation head differs")

    if len(rollover_events) != len(switch_gate_receipts):
        raise V5LifecycleLedgerError("gate switch receipt count differs")
    by_sample = {row["sample_index"]: row for row in observations}
    for event, receipt in zip(rollover_events, switch_gate_receipts, strict=True):
        observation = by_sample.get(event.sample_index)
        if observation is None:
            raise V5LifecycleLedgerError("rollover commit lacks a gate observation")
        family_results = observation["family_results"]
        gate_receipt = receipt.get("gate_receipt") if isinstance(receipt, Mapping) else None
        commit_seed = receipt.get("commit_seed_qdot") if isinstance(receipt, Mapping) else None
        if (
            not isinstance(gate_receipt, Mapping)
            or not isinstance(commit_seed, Sequence)
            or isinstance(commit_seed, (str, bytes))
            or len(commit_seed) != 6
        ):
            raise V5LifecycleLedgerError("rollover switch gate receipt is incomplete")
        if (
            observation["phase"] != "tail"
            or event.next_identity is None
            or observation["expected_epoch"] != event.next_identity.epoch
            or observation["expected_ordinal"] != event.next_identity.ordinal
            or observation["expected_candidate_token"]
            != event.next_identity.candidate_token
            or observation["gate_receipt_sha256"]
            != canonical_sha256(dict(gate_receipt))
            or tuple(float(item) for item in observation["qdot"])
            != tuple(float(item) for item in commit_seed)
            or not all(family_results.values())
        ):
            raise V5LifecycleLedgerError("rollover switch gate observation differs")
    result = {
        "binding": binding,
        "timing_rows": tuple(tuple(row) for row in raw_timing),
        "timing_actual_dt_s": tuple(timing),
        "timing_acceptance": cold_timing,
        "observations": tuple(observations),
        "activation_publications": tuple(
            {
                **publication,
                "pending_sample_indices": tuple(
                    publication["pending_sample_indices"]
                ),
            }
            for publication in activation_publications
        ),
    }
    _GATE_OBSERVATION_COLD_CACHE = (cache_key, result)
    return result


def lifecycle_row_evidence_sha256(row: Mapping[str, Any]) -> str:
    if not isinstance(row, Mapping):
        raise V5LifecycleLedgerError("lifecycle row evidence is not a mapping")
    return canonical_sha256(dict(row))


@dataclass(frozen=True)
class LifecycleEventV2:
    kind: LifecycleEventKind
    sample_index: int
    monotonic_s: float
    identity: CandidateIdentityV1
    rtde_timestamp_s: float = 0.0
    next_identity: CandidateIdentityV1 | None = None
    generation: int = 0
    qdot_generation: int = 0
    terminal_state: V5TPState | None = None
    event_evidence_sha256: str = GENESIS_SHA256
    referenced_row_sha256: str = GENESIS_SHA256
    schema: str = V5_EVENT_SCHEMA
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LifecycleEventKind):
            raise V5LifecycleLedgerError("lifecycle event kind is not typed")
        _integer(self.sample_index, "event sample index")
        mono = _finite(self.monotonic_s, "event monotonic time")
        rtde = _finite(self.rtde_timestamp_s, "event RTDE timestamp")
        if rtde < 0.0:
            raise V5LifecycleLedgerError("event RTDE timestamp must be non-negative")
        object.__setattr__(self, "monotonic_s", mono)
        object.__setattr__(self, "rtde_timestamp_s", rtde)
        if not isinstance(self.identity, CandidateIdentityV1):
            raise V5LifecycleLedgerError("event identity is not typed")
        if self.next_identity is not None and not isinstance(self.next_identity, CandidateIdentityV1):
            raise V5LifecycleLedgerError("event next identity is not typed")
        _integer(self.generation, "event generation")
        _integer(self.qdot_generation, "event qdot generation")
        _require_sha(self.event_evidence_sha256, "event evidence hash")
        _require_sha(self.referenced_row_sha256, "referenced lifecycle row hash")
        if self.event_evidence_sha256 == GENESIS_SHA256 or self.referenced_row_sha256 == GENESIS_SHA256:
            raise V5LifecycleLedgerError("lifecycle event evidence is incomplete")
        if self.schema != V5_EVENT_SCHEMA or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("lifecycle event schema/version differs")
        if self.kind is LifecycleEventKind.CHAIN_START:
            if self.next_identity is not None or self.generation or self.qdot_generation or self.terminal_state is not None:
                raise V5LifecycleLedgerError("CHAIN_START carries non-entry fields")
        elif self.kind is LifecycleEventKind.CONTACT_ROLLOVER:
            if self.next_identity is None or self.generation <= 0 or self.qdot_generation != self.generation:
                raise V5LifecycleLedgerError("CONTACT_ROLLOVER identity/generation is incomplete")
            if self.next_identity.epoch != self.identity.epoch or self.next_identity.ordinal <= self.identity.ordinal or self.terminal_state is not None:
                raise V5LifecycleLedgerError("CONTACT_ROLLOVER identity/state is invalid")
        elif self.kind is LifecycleEventKind.HOME:
            if self.next_identity is not None or self.terminal_state is not V5TPState.READY_HOME_NEXT:
                raise V5LifecycleLedgerError("HOME event lacks terminal Home proof")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LifecycleEventV2":
        try:
            next_value = value.get("next_identity")
            terminal = value.get("terminal_state")
            return cls(
                kind=LifecycleEventKind(value["kind"]),
                sample_index=value["sample_index"],
                monotonic_s=value["monotonic_s"],
                identity=_identity_from_dict(value["identity"]),
                rtde_timestamp_s=value.get("rtde_timestamp_s", 0.0),
                next_identity=None if next_value is None else _identity_from_dict(next_value),
                generation=value.get("generation", 0),
                qdot_generation=value.get("qdot_generation", 0),
                terminal_state=None if terminal is None else V5TPState(terminal),
                event_evidence_sha256=value["event_evidence_sha256"],
                referenced_row_sha256=value["referenced_row_sha256"],
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise V5LifecycleLedgerError("lifecycle event mapping is invalid") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "kind": self.kind.value,
            "sample_index": self.sample_index,
            "monotonic_s": self.monotonic_s,
            "rtde_timestamp_s": self.rtde_timestamp_s,
            "identity": _identity_dict(self.identity),
            "next_identity": None if self.next_identity is None else _identity_dict(self.next_identity),
            "generation": self.generation,
            "qdot_generation": self.qdot_generation,
            "terminal_state": None if self.terminal_state is None else int(self.terminal_state),
            "event_evidence_sha256": self.event_evidence_sha256,
            "referenced_row_sha256": self.referenced_row_sha256,
        }


def _normalize_events(events: Mapping[str, Any]) -> tuple[tuple[LifecycleEventV2, ...], Mapping[str, Any]]:
    if not isinstance(events, Mapping) or events.get("cold_verified") is not True or events.get("schema") != V5_EVENT_BUNDLE_SCHEMA or events.get("version") != V5_LIFECYCLE_VERSION:
        raise V5LifecycleLedgerError("event input must be a cold-verified bundle mapping")
    raw_events = events.get("events")
    expected_hash = events.get("events_sha256")
    if not isinstance(raw_events, Sequence) or isinstance(raw_events, (str, bytes)) or not raw_events:
        raise V5LifecycleLedgerError("event bundle has no typed events")
    _require_sha(expected_hash, "event bundle hash")
    normalized = tuple(event if isinstance(event, LifecycleEventV2) else LifecycleEventV2.from_mapping(event) for event in raw_events)
    if canonical_sha256([event.as_dict() for event in normalized]) != expected_hash:
        raise V5LifecycleLedgerError("event bundle bytes differ")
    previous_index = -1
    previous_mono = -math.inf
    previous_rtde = -math.inf
    for event in normalized:
        if event.sample_index <= previous_index or event.monotonic_s < previous_mono or event.rtde_timestamp_s < previous_rtde:
            raise V5LifecycleLedgerError("event clocks/sample indices are not ordered")
        previous_index = event.sample_index
        previous_mono = event.monotonic_s
        previous_rtde = event.rtde_timestamp_s
    entry_hash = events.get("entry_evidence_sha256")
    _require_sha(entry_hash, "entry evidence hash")
    rollover_events = tuple(
        event for event in normalized if event.kind is LifecycleEventKind.CONTACT_ROLLOVER
    )
    switch_receipts = events.get("switch_gate_receipts")
    if not isinstance(switch_receipts, Sequence) or isinstance(switch_receipts, (str, bytes)) or len(switch_receipts) != len(rollover_events):
        raise V5LifecycleLedgerError("event bundle switch receipts differ from rollover events")
    normalized_switch_receipts: list[dict[str, Any]] = []
    required_switch_keys = {
        "schema",
        "version",
        "old_identity",
        "next_identity",
        "generation",
        "qdot_generation",
        "tail_endpoint_s",
        "controller_overlay",
        "commit_seed_qdot",
        "gate_receipt",
        "narrow_force_windows_blocking",
    }
    for event, raw_receipt in zip(rollover_events, switch_receipts, strict=True):
        if not isinstance(raw_receipt, Mapping) or set(raw_receipt) != required_switch_keys:
            raise V5LifecycleLedgerError("switch gate receipt schema keys differ")
        receipt = dict(raw_receipt)
        if (
            receipt.get("schema") != "step6.autotune/figure8-v5-switch-gate-receipt-v1"
            or receipt.get("version") != 1
            or _identity_from_dict(receipt.get("old_identity", {})) != event.identity
            or _identity_from_dict(receipt.get("next_identity", {})) != event.next_identity
            or receipt.get("generation") != event.generation
            or receipt.get("qdot_generation") != event.qdot_generation
            or not math.isclose(_finite(receipt.get("tail_endpoint_s"), "switch tail endpoint"), TAIL_END_S, rel_tol=0.0, abs_tol=1e-12)
            or receipt.get("narrow_force_windows_blocking") is not False
            or not isinstance(receipt.get("controller_overlay"), Mapping)
            or not isinstance(receipt.get("gate_receipt"), Mapping)
            or not isinstance(receipt.get("commit_seed_qdot"), Sequence)
            or isinstance(receipt.get("commit_seed_qdot"), (str, bytes))
            or len(receipt["commit_seed_qdot"]) != 6
            or any(not math.isfinite(float(value)) for value in receipt["commit_seed_qdot"])
            or canonical_sha256(receipt) != event.event_evidence_sha256
        ):
            raise V5LifecycleLedgerError("switch gate receipt content/hash differs")
        normalized_switch_receipts.append(receipt)
    bundle = dict(events)
    bundle["events"] = [event.as_dict() for event in normalized]
    bundle["events_sha256"] = expected_hash
    bundle["entry_evidence_sha256"] = entry_hash
    bundle["switch_gate_receipts"] = normalized_switch_receipts
    bundle["gate_observation_artifact"] = _normalize_gate_observation_binding(
        events.get("gate_observation_artifact")
    )
    return normalized, _freeze(bundle, "event bundle")


@dataclass(frozen=True)
class SealedR013ArtifactV2:
    artifact_path: str
    artifact_sha256: str
    artifact_size: int
    metadata: Mapping[str, Any]
    rows: tuple[Mapping[str, Any], ...]
    receipt: Mapping[str, Any]
    events: tuple[LifecycleEventV2, ...]
    event_bundle: Mapping[str, Any]
    schema: str = V5_LIFECYCLE_SCHEMA
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.artifact_sha256, "sealed artifact hash")
        _integer(self.artifact_size, "sealed artifact size", positive=True)
        if not isinstance(self.metadata, Mapping) or not isinstance(self.receipt, Mapping) or not isinstance(self.event_bundle, Mapping):
            raise V5LifecycleLedgerError("sealed artifact metadata/receipt/bundle is not typed")
        if self.metadata.get("schema") != R013_LIFECYCLE_SCHEMA:
            raise V5LifecycleLedgerError("sealed artifact metadata schema differs")
        if self.receipt.get("schema") != R013_LIFECYCLE_RECEIPT_SCHEMA or self.receipt.get("status") != "complete" or self.receipt.get("artifact_sha256") != self.artifact_sha256 or self.receipt.get("artifact_row_count") != len(self.rows) or self.receipt.get("errors") != []:
            raise V5LifecycleLedgerError("sealed artifact receipt binding differs")
        if not self.rows or any(not isinstance(row, Mapping) for row in self.rows) or not self.events:
            raise V5LifecycleLedgerError("sealed artifact rows/events are incomplete")
        if self.schema != V5_LIFECYCLE_SCHEMA or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("sealed lifecycle schema/version differs")
        object.__setattr__(self, "metadata", _freeze(self.metadata, "artifact metadata"))
        object.__setattr__(self, "receipt", _freeze(self.receipt, "artifact receipt"))
        object.__setattr__(self, "rows", tuple(MappingProxyType(dict(row)) for row in self.rows))
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "event_bundle", _freeze(self.event_bundle, "event bundle"))


def _receipt_is_complete(receipt: Mapping[str, Any], path: Path, digest: str, row_count: int) -> None:
    if receipt.get("schema") != R013_LIFECYCLE_RECEIPT_SCHEMA or receipt.get("status") != "complete":
        raise V5LifecycleLedgerError("R013 receipt is not complete")
    receipt_path = receipt.get("artifact_path")
    if not isinstance(receipt_path, str) or Path(receipt_path).resolve() != path.resolve():
        raise V5LifecycleLedgerError("R013 receipt artifact_path differs")
    if receipt.get("artifact_sha256") != digest or receipt.get("artifact_row_count") != row_count:
        raise V5LifecycleLedgerError("R013 receipt artifact binding differs")
    required_true = ("coverage_complete", "home_verified", "path_coverage_complete", "capture_started_before_motion", "terminal_sensor_present")
    if any(receipt.get(name) is not True for name in required_true) or receipt.get("terminal_state") != int(V5TPState.READY_HOME_NEXT):
        raise V5LifecycleLedgerError("R013 receipt lacks complete coverage/Home proof")
    required_zero = ("artifact_sample_index_gap_count", "packet_sequence_gap_count", "packet_sequence_regression_count", "dropped_chunks", "write_errors", "invalid_rows", "observer_errors", "missing_force_rows", "missing_pose_rows")
    if any(receipt.get(name) != 0 for name in required_zero) or receipt.get("errors") != []:
        raise V5LifecycleLedgerError("R013 receipt has gaps, regressions, drops, invalid rows, or errors")
    gc_window = receipt.get("gc_window")
    if (
        not isinstance(gc_window, Mapping)
        or gc_window.get("schema") != "step6.autotune/figure8-v5-gc-window-receipt-v1"
        or gc_window.get("version") != 1
        or gc_window.get("entered") is not True
        or gc_window.get("restored") is not True
        or gc_window.get("restored_after_timing_lease") is not True
        or gc_window.get("pre_enabled") != gc_window.get("post_enabled")
    ):
        raise V5LifecycleLedgerError("V5 receipt lacks a complete GC critical-window proof")
    if receipt.get("path_requested") is not True:
        raise V5LifecycleLedgerError("R013 receipt is not a path capture")
    if "sample_count" in receipt and receipt.get("sample_count") != row_count:
        raise V5LifecycleLedgerError("R013 receipt sample count differs")


def index_sealed_r013_artifact(artifact_path: Path, receipt: Mapping[str, Any], events: Mapping[str, Any]) -> SealedR013ArtifactV2:
    """Cold-index one sealed R013 artifact and its sealed event bundle."""

    global _SEALED_ARTIFACT_COLD_CACHE

    path = Path(artifact_path)
    if path.is_symlink() or not path.is_file():
        raise V5LifecycleLedgerError("R013 artifact is not a regular file")
    if not isinstance(receipt, Mapping):
        raise V5LifecycleLedgerError("R013 receipt is not typed")
    if not isinstance(events, Mapping):
        raise V5LifecycleLedgerError("R013 event bundle is not typed")
    raw_bytes = path.read_bytes()
    digest = _sha256(raw_bytes)
    gate_binding = _normalize_gate_observation_binding(
        events.get("gate_observation_artifact")
    )
    gate_path = Path(gate_binding["artifact_path"])
    if gate_path.is_symlink() or not gate_path.is_file():
        raise V5LifecycleLedgerError("gate observation artifact is not a regular file")
    gate_bytes = gate_path.read_bytes()
    gate_digest = _sha256(gate_bytes)
    if (
        len(gate_bytes) != gate_binding["artifact_size"]
        or gate_digest != gate_binding["artifact_sha256"]
    ):
        raise V5LifecycleLedgerError("gate observation artifact bytes differ")
    cache_key = (
        str(path.resolve()),
        digest,
        len(raw_bytes),
        canonical_sha256(dict(receipt)),
        canonical_sha256(dict(events)),
        str(gate_path.resolve()),
        gate_digest,
        len(gate_bytes),
    )
    if (
        _SEALED_ARTIFACT_COLD_CACHE is not None
        and _SEALED_ARTIFACT_COLD_CACHE[0] == cache_key
    ):
        return _SEALED_ARTIFACT_COLD_CACHE[1]
    try:
        metadata, raw_rows = load_lifecycle_artifact(path)
    except Exception as exc:
        raise V5LifecycleLedgerError("R013 artifact cold read failed") from exc
    _receipt_is_complete(receipt, path, digest, len(raw_rows))
    if metadata.get("schema") != R013_LIFECYCLE_SCHEMA:
        raise V5LifecycleLedgerError("R013 metadata schema differs")
    rows: list[Mapping[str, Any]] = []
    previous_mono = -math.inf
    previous_rtde = -math.inf
    packet_values: list[int] = []
    for expected_index, raw_row in enumerate(raw_rows):
        row = dict(raw_row)
        if row.get("sample_index") != expected_index:
            raise V5LifecycleLedgerError("R013 sample indices are not contiguous")
        for field in ("monotonic_s", "rtde_timestamp_s", "filtered_normal_n", "normal_load_n", "force_norm_n", "torque_norm_nm"):
            _finite(row.get(field), f"R013 row {field}")
        mono = float(row["monotonic_s"])
        rtde = float(row["rtde_timestamp_s"])
        if mono < previous_mono or rtde < previous_rtde:
            raise V5LifecycleLedgerError("R013 host/RTDE clocks regress")
        if int(row.get("flags", 0)) & FLAG_INVALID:
            raise V5LifecycleLedgerError("R013 contains invalid rows")
        packet = int(row.get("packet_sequence", -1))
        if packet >= 0 and not (int(row.get("flags", 0)) & FLAG_TERMINAL):
            packet_values.append(packet)
        rows.append(row)
        previous_mono = mono
        previous_rtde = rtde
    if any(curr - prev < 0 or curr - prev > 1 for prev, curr in zip(packet_values, packet_values[1:])):
        raise V5LifecycleLedgerError("R013 packet sequence regresses or has gaps")
    terminal_indices = [index for index, row in enumerate(rows) if int(row.get("flags", 0)) & FLAG_TERMINAL]
    if terminal_indices != [len(rows) - 1]:
        raise V5LifecycleLedgerError("R013 terminal Home row is not final")
    terminal = rows[-1]
    if terminal.get("tp_state") != int(V5TPState.READY_HOME_NEXT) or terminal.get("phase_code") != int(V5TPState.READY_HOME_NEXT) or not (int(terminal.get("flags", 0)) & FLAG_SENSOR_PRESENT) or not (int(terminal.get("flags", 0)) & FLAG_SENSOR_FRESH):
        raise V5LifecycleLedgerError("R013 final row is not a fresh typed Home")
    normalized_events, bundle = _normalize_events(events)
    if any(event.sample_index >= len(rows) for event in normalized_events):
        raise V5LifecycleLedgerError("event references a row outside the artifact")
    _load_gate_observation_artifact(
        bundle["gate_observation_artifact"],
        source_artifact_path=path,
        source_artifact_sha256=digest,
        source_artifact_size=len(raw_bytes),
        source_rows=rows,
        events=normalized_events,
        switch_gate_receipts=bundle["switch_gate_receipts"],
    )
    artifact = SealedR013ArtifactV2(
        str(path.resolve()),
        digest,
        len(raw_bytes),
        metadata,
        tuple(rows),
        receipt,
        normalized_events,
        bundle,
    )
    _SEALED_ARTIFACT_COLD_CACHE = (cache_key, artifact)
    return artifact


def cold_activation_publication_boundaries(
    artifact: SealedR013ArtifactV2,
) -> tuple[Mapping[str, Any], ...]:
    """Return the sidecar-derived staged activation boundaries."""

    if not isinstance(artifact, SealedR013ArtifactV2):
        raise V5LifecycleLedgerError(
            "activation publication source artifact is untyped"
        )
    cold = _load_gate_observation_artifact(
        artifact.event_bundle["gate_observation_artifact"],
        source_artifact_path=Path(artifact.artifact_path),
        source_artifact_sha256=artifact.artifact_sha256,
        source_artifact_size=artifact.artifact_size,
        source_rows=artifact.rows,
        events=artifact.events,
        switch_gate_receipts=artifact.event_bundle["switch_gate_receipts"],
    )
    return tuple(
        {
            "generation": int(publication["generation"]),
            "pending_sample_indices": tuple(
                int(value)
                for value in publication["pending_sample_indices"]
            ),
            "first_successor_sample_index": int(
                publication["first_successor_sample_index"]
            ),
        }
        for publication in cold["activation_publications"]
    )


def _require_cold_artifact_object(artifact: SealedR013ArtifactV2) -> None:
    """Reject an in-memory artifact object whose rows were caller-mutated."""

    cold = index_sealed_r013_artifact(Path(artifact.artifact_path), artifact.receipt, artifact.event_bundle)
    if (
        cold.artifact_sha256 != artifact.artifact_sha256
        or cold.artifact_size != artifact.artifact_size
        or cold.artifact_path != artifact.artifact_path
        or canonical_sha256(dict(cold.metadata)) != canonical_sha256(dict(artifact.metadata))
        or canonical_sha256(dict(cold.receipt)) != canonical_sha256(dict(artifact.receipt))
        or canonical_sha256(dict(cold.event_bundle)) != canonical_sha256(dict(artifact.event_bundle))
        or canonical_sha256([dict(row) for row in cold.rows]) != canonical_sha256([dict(row) for row in artifact.rows])
        or canonical_sha256([event.as_dict() for event in cold.events]) != canonical_sha256([event.as_dict() for event in artifact.events])
    ):
        raise V5LifecycleLedgerError("sealed artifact object differs from its cold bytes")


@dataclass(frozen=True)
class ArtifactBindingV2:
    artifact_path: str
    artifact_sha256: str
    artifact_size: int
    receipt: Mapping[str, Any]
    events: Mapping[str, Any]
    schema: str = V5_ARTIFACT_BINDING_SCHEMA
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.artifact_sha256, "artifact binding hash")
        _integer(self.artifact_size, "artifact binding size", positive=True)
        if not isinstance(self.artifact_path, str) or Path(self.artifact_path).resolve() != Path(self.artifact_path):
            raise V5LifecycleLedgerError("artifact binding path must be resolved")
        if not isinstance(self.receipt, Mapping) or not isinstance(self.events, Mapping):
            raise V5LifecycleLedgerError("artifact binding receipt/events are incomplete")
        if self.schema != V5_ARTIFACT_BINDING_SCHEMA or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("artifact binding schema/version differs")
        object.__setattr__(self, "receipt", _freeze(self.receipt, "artifact binding receipt"))
        object.__setattr__(self, "events", _freeze(self.events, "artifact binding events"))

    @classmethod
    def from_artifact(cls, artifact: SealedR013ArtifactV2) -> "ArtifactBindingV2":
        if not isinstance(artifact, SealedR013ArtifactV2):
            raise V5LifecycleLedgerError("artifact binding requires a sealed artifact")
        return cls(artifact.artifact_path, artifact.artifact_sha256, artifact.artifact_size, artifact.receipt, artifact.event_bundle)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ArtifactBindingV2":
        try:
            binding = cls(value["artifact_path"], value["artifact_sha256"], value["artifact_size"], value["receipt"], value["events"], schema=value["schema"], version=value["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise V5LifecycleLedgerError("artifact binding mapping is invalid") from exc
        return binding

    def cold_index(self) -> SealedR013ArtifactV2:
        return index_sealed_r013_artifact(Path(self.artifact_path), self.receipt, self.events)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "artifact_path": self.artifact_path, "artifact_sha256": self.artifact_sha256, "artifact_size": self.artifact_size, "receipt": dict(self.receipt), "events": dict(self.events)}


@dataclass(frozen=True)
class MetricBinV1:
    window: MetricWindow
    index: int
    start_s: float
    end_s: float
    filtered_normal_n: float
    raw_signed_normal_n: float
    raw_force_norm_n: float
    raw_torque_norm_nm: float

    def __post_init__(self) -> None:
        if not isinstance(self.window, MetricWindow):
            raise V5LifecycleLedgerError("metric bin window is not typed")
        _integer(self.index, "metric bin index")
        expected_start = (self.index if self.window is MetricWindow.EVIDENCE else self.index + 50) * BIN_WIDTH_S
        if not math.isclose(_finite(self.start_s, "metric bin start"), expected_start, rel_tol=0.0, abs_tol=1e-12) or not math.isclose(_finite(self.end_s, "metric bin end"), expected_start + BIN_WIDTH_S, rel_tol=0.0, abs_tol=1e-12):
            raise V5LifecycleLedgerError("metric bin interval differs from the fixed 0.1 s grid")
        for field in ("filtered_normal_n", "raw_signed_normal_n", "raw_force_norm_n", "raw_torque_norm_nm"):
            object.__setattr__(self, field, _finite(getattr(self, field), f"metric {field}"))
        limit = EVIDENCE_BIN_COUNT if self.window is MetricWindow.EVIDENCE else FORMAL_BIN_COUNT
        if self.index >= limit:
            raise V5LifecycleLedgerError("metric bin index is outside its window")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], window: MetricWindow) -> "MetricBinV1":
        if not isinstance(value, Mapping) or value.get("signal_name", METRIC_SIGNAL_NAME) != METRIC_SIGNAL_NAME or value.get("raw_measured_force_is_metric_input") is True:
            raise V5LifecycleLedgerError("metric bin is not the filtered_normal_n signal")
        try:
            start = value.get("start_s", (value["index"] if window is MetricWindow.EVIDENCE else value["index"] + 50) * BIN_WIDTH_S)
            return cls(window, value["index"], start, value.get("end_s", start + BIN_WIDTH_S), value["filtered_normal_n"], value.get("raw_signed_normal_n", value.get("raw_normal_n")), value.get("raw_force_norm_n", value.get("force_norm_n")), value.get("raw_torque_norm_nm", value.get("torque_norm_nm")))
        except (KeyError, TypeError, ValueError) as exc:
            raise V5LifecycleLedgerError("metric bin mapping is incomplete") from exc

    def as_dict(self) -> dict[str, Any]:
        return {"window": self.window.value, "index": self.index, "start_s": self.start_s, "end_s": self.end_s, "signal_name": METRIC_SIGNAL_NAME, "filtered_normal_n": self.filtered_normal_n, "raw_signed_normal_n": self.raw_signed_normal_n, "raw_force_norm_n": self.raw_force_norm_n, "raw_torque_norm_nm": self.raw_torque_norm_nm}


@dataclass(frozen=True)
class MetricClosureEvidenceV2:
    coverage_complete: bool
    gap_free: bool
    timing_valid: bool
    identity_valid: bool
    schema: str = "step6.autotune/figure8-v5-metric-closure-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.coverage_complete, self.gap_free, self.timing_valid, self.identity_valid)) or self.schema != "step6.autotune/figure8-v5-metric-closure-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("metric closure is not typed/versioned")

    @property
    def sealed(self) -> bool:
        return all((self.coverage_complete, self.gap_free, self.timing_valid, self.identity_valid))

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "coverage_complete": self.coverage_complete, "gap_free": self.gap_free, "timing_valid": self.timing_valid, "identity_valid": self.identity_valid}


@dataclass(frozen=True)
class MetricSnapshotV1:
    source_artifact_sha256: str
    source_artifact_size: int
    sample_start_index: int
    sample_end_index: int
    clock_start_s: float
    clock_end_s: float
    candidate_identity: CandidateIdentityV1
    evidence_bins: tuple[MetricBinV1, ...]
    formal_bins: tuple[MetricBinV1, ...]
    closure: MetricClosureEvidenceV2
    formal_mae_n: float
    metric_rows_sha256: str
    signal_name: str = METRIC_SIGNAL_NAME
    sealed: bool = True
    schema: str = "step6.autotune/figure8-v5-metric-snapshot-v1"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.source_artifact_sha256, "metric source artifact hash")
        _integer(self.source_artifact_size, "metric source artifact size", positive=True)
        _integer(self.sample_start_index, "metric sample start")
        _integer(self.sample_end_index, "metric sample end")
        if self.sample_end_index <= self.sample_start_index or _finite(self.clock_end_s, "metric clock end") <= _finite(self.clock_start_s, "metric clock start"):
            raise V5LifecycleLedgerError("metric bounds are empty")
        if not isinstance(self.candidate_identity, CandidateIdentityV1) or not isinstance(self.closure, MetricClosureEvidenceV2) or not self.closure.sealed or self.signal_name != METRIC_SIGNAL_NAME or self.sealed is not True:
            raise V5LifecycleLedgerError("metric snapshot is not sealed filtered_normal_n evidence")
        _require_sha(self.metric_rows_sha256, "metric rows hash")
        if self.metric_rows_sha256 == GENESIS_SHA256 or self.schema != "step6.autotune/figure8-v5-metric-snapshot-v1" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("metric snapshot schema/hash differs")
        evidence = tuple(self.evidence_bins)
        formal = tuple(self.formal_bins)
        if len(evidence) != EVIDENCE_BIN_COUNT or len(formal) != FORMAL_BIN_COUNT or any(not isinstance(item, MetricBinV1) or item.window is not MetricWindow.EVIDENCE for item in evidence) or any(not isinstance(item, MetricBinV1) or item.window is not MetricWindow.FORMAL for item in formal):
            raise V5LifecycleLedgerError("metric fixed bins are incomplete")
        if {item.index for item in evidence} != set(range(EVIDENCE_BIN_COUNT)) or {item.index for item in formal} != set(range(FORMAL_BIN_COUNT)):
            raise V5LifecycleLedgerError("metric bins are missing or duplicated")
        mae = math.fsum(abs(item.filtered_normal_n - 5.0) for item in formal) / FORMAL_BIN_COUNT
        supplied = _finite(self.formal_mae_n, "formal MAE")
        if not math.isclose(supplied, mae, rel_tol=0.0, abs_tol=1e-12):
            raise V5LifecycleLedgerError("formal MAE differs from filtered bins")
        object.__setattr__(self, "evidence_bins", evidence)
        object.__setattr__(self, "formal_bins", formal)
        object.__setattr__(self, "clock_start_s", _finite(self.clock_start_s, "metric clock start"))
        object.__setattr__(self, "clock_end_s", _finite(self.clock_end_s, "metric clock end"))
        object.__setattr__(self, "formal_mae_n", supplied)

    @classmethod
    def from_artifact_slice(cls, artifact: SealedR013ArtifactV2, *, candidate_identity: CandidateIdentityV1, sample_start_index: int, sample_end_index: int, path_clock_start_s: float) -> "MetricSnapshotV1":
        if not isinstance(artifact, SealedR013ArtifactV2) or not isinstance(candidate_identity, CandidateIdentityV1):
            raise V5LifecycleLedgerError("metric derivation requires typed sealed artifact/identity")
        _integer(sample_start_index, "metric slice start")
        _integer(sample_end_index, "metric slice end")
        if sample_end_index <= sample_start_index or sample_end_index > len(artifact.rows):
            raise V5LifecycleLedgerError("metric slice bounds are outside artifact")
        path_start = _finite(path_clock_start_s, "attempt path clock start")
        grouped: dict[int, list[Mapping[str, Any]]] = {index: [] for index in range(EVIDENCE_BIN_COUNT)}
        selected: list[Mapping[str, Any]] = []
        for row in artifact.rows[sample_start_index:sample_end_index]:
            local_time = _finite(row.get("monotonic_s"), "metric row monotonic") - path_start
            if local_time < 0.0:
                raise V5LifecycleLedgerError("metric row precedes attempt clock")
            if local_time >= PATH_END_S:
                continue
            try:
                state = V5TPState(int(row.get("tp_state")))
            except (TypeError, ValueError):
                continue
            if state not in _METRIC_STATES:
                continue
            phase = int(row.get("phase_code", 0))
            if (state is V5TPState.PATH and phase != int(V5TPState.PATH)) or (state in (V5TPState.ROLLOVER_PREPARED, V5TPState.ROLLOVER_COMMITTED) and phase != 0):
                continue
            flags = int(row.get("flags", 0))
            if not (flags & FLAG_SENSOR_PRESENT and flags & FLAG_SENSOR_FRESH):
                continue
            for field in ("filtered_normal_n", "normal_load_n", "force_norm_n", "torque_norm_nm"):
                _finite(row.get(field), f"metric row {field}")
            bin_index = int(math.floor((local_time + 1e-9) / BIN_WIDTH_S))
            if not 0 <= bin_index < EVIDENCE_BIN_COUNT:
                continue
            grouped[bin_index].append(row)
            selected.append(row)
        missing = [index for index, values in grouped.items() if not values]
        if missing:
            raise V5LifecycleLedgerError("metric fixed grid has missing bins: " + ",".join(map(str, missing)))
        evidence = tuple(MetricBinV1(MetricWindow.EVIDENCE, index, index * BIN_WIDTH_S, (index + 1) * BIN_WIDTH_S, math.fsum(float(row["filtered_normal_n"]) for row in values) / len(values), math.fsum(float(row["normal_load_n"]) for row in values) / len(values), math.fsum(float(row["force_norm_n"]) for row in values) / len(values), math.fsum(float(row["torque_norm_nm"]) for row in values) / len(values)) for index, values in grouped.items())
        formal = tuple(MetricBinV1(MetricWindow.FORMAL, index - 50, index * BIN_WIDTH_S, (index + 1) * BIN_WIDTH_S, evidence[index].filtered_normal_n, evidence[index].raw_signed_normal_n, evidence[index].raw_force_norm_n, evidence[index].raw_torque_norm_nm) for index in range(50, EVIDENCE_BIN_COUNT))
        return cls(artifact.artifact_sha256, artifact.artifact_size, min(int(row["sample_index"]) for row in selected), max(int(row["sample_index"]) for row in selected) + 1, path_start, path_start + PATH_END_S, candidate_identity, evidence, formal, MetricClosureEvidenceV2(True, True, True, True), math.fsum(abs(item.filtered_normal_n - 5.0) for item in formal) / FORMAL_BIN_COUNT, canonical_sha256([dict(row) for row in selected]))

    @property
    def content_hash(self) -> str:
        value = self.as_dict(include_hash=False)
        value.pop("source_artifact_sha256", None)
        value.pop("source_artifact_size", None)
        return canonical_sha256(value)

    @property
    def snapshot_sha256(self) -> str:
        return self.content_hash

    @property
    def primary_mae_n(self) -> float:
        """Full [0,60) objective; formal_mae_n remains the [5,60) comparator."""

        return math.fsum(
            abs(item.filtered_normal_n - 5.0) for item in self.evidence_bins
        ) / EVIDENCE_BIN_COUNT

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {"schema": self.schema, "version": self.version, "source_artifact_sha256": self.source_artifact_sha256, "source_artifact_size": self.source_artifact_size, "sample_start_index": self.sample_start_index, "sample_end_index": self.sample_end_index, "clock_start_s": self.clock_start_s, "clock_end_s": self.clock_end_s, "candidate_identity": _identity_dict(self.candidate_identity), "evidence_bins": [item.as_dict() for item in self.evidence_bins], "formal_bins": [item.as_dict() for item in self.formal_bins], "closure": self.closure.as_dict(), "formal_mae_n": self.formal_mae_n, "metric_rows_sha256": self.metric_rows_sha256, "signal_name": self.signal_name, "sealed": self.sealed}
        if include_hash:
            value["snapshot_sha256"] = self.content_hash
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MetricSnapshotV1":
        try:
            closure_value = value["closure"]
            snapshot = cls(value["source_artifact_sha256"], value["source_artifact_size"], value["sample_start_index"], value["sample_end_index"], value["clock_start_s"], value["clock_end_s"], _identity_from_dict(value["candidate_identity"]), tuple(MetricBinV1.from_mapping(item, MetricWindow.EVIDENCE) for item in value["evidence_bins"]), tuple(MetricBinV1.from_mapping(item, MetricWindow.FORMAL) for item in value["formal_bins"]), MetricClosureEvidenceV2(closure_value["coverage_complete"], closure_value["gap_free"], closure_value["timing_valid"], closure_value["identity_valid"]), value["formal_mae_n"], value["metric_rows_sha256"], value["signal_name"], value["sealed"])
            if value.get("snapshot_sha256") is not None and value["snapshot_sha256"] != snapshot.content_hash:
                raise V5LifecycleLedgerError("metric snapshot hash differs")
            return snapshot
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, V5LifecycleLedgerError):
                raise
            raise V5LifecycleLedgerError("metric snapshot mapping is invalid") from exc


@dataclass(frozen=True)
class HomeBoundaryV2:
    terminal_state: V5TPState
    sample_index: int
    monotonic_s: float
    home_evidence_sha256: str
    rtde_timestamp_s: float = 0.0
    schema: str = "step6.autotune/figure8-v5-home-boundary-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if self.terminal_state is not V5TPState.READY_HOME_NEXT:
            raise V5LifecycleLedgerError("HOME boundary requires READY_HOME_NEXT")
        _integer(self.sample_index, "HOME sample index")
        _finite(self.monotonic_s, "HOME monotonic time")
        rtde = _finite(self.rtde_timestamp_s, "HOME RTDE timestamp")
        if rtde < 0.0:
            raise V5LifecycleLedgerError("HOME RTDE timestamp must be non-negative")
        _require_sha(self.home_evidence_sha256, "HOME evidence hash")
        if self.home_evidence_sha256 == GENESIS_SHA256 or self.schema != "step6.autotune/figure8-v5-home-boundary-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("HOME boundary evidence/schema differs")
        object.__setattr__(self, "rtde_timestamp_s", rtde)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "mode": BoundaryMode.HOME.value, "terminal_state": int(self.terminal_state), "sample_index": self.sample_index, "monotonic_s": self.monotonic_s, "rtde_timestamp_s": self.rtde_timestamp_s, "home_evidence_sha256": self.home_evidence_sha256}


@dataclass(frozen=True)
class ControllerCommitAckV2:
    active_identity: CandidateIdentityV1
    generation: int
    qdot_generation: int
    sample_index: int
    monotonic_s: float
    rtde_timestamp_s: float = 0.0
    schema: str = "step6.autotune/figure8-v5-controller-commit-ack-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.active_identity, CandidateIdentityV1):
            raise V5LifecycleLedgerError("COMMIT ACK identity is not typed")
        _integer(self.generation, "COMMIT ACK generation", positive=True)
        if self.qdot_generation != self.generation:
            raise V5LifecycleLedgerError("COMMIT ACK qdot generation differs")
        _integer(self.sample_index, "COMMIT ACK sample index")
        _finite(self.monotonic_s, "COMMIT ACK monotonic time")
        rtde = _finite(self.rtde_timestamp_s, "COMMIT ACK RTDE timestamp")
        if rtde < 0.0 or self.schema != "step6.autotune/figure8-v5-controller-commit-ack-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("COMMIT ACK schema/time differs")
        object.__setattr__(self, "rtde_timestamp_s", rtde)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "active_identity": _identity_dict(self.active_identity), "generation": self.generation, "qdot_generation": self.qdot_generation, "sample_index": self.sample_index, "monotonic_s": self.monotonic_s, "rtde_timestamp_s": self.rtde_timestamp_s}


@dataclass(frozen=True)
class ContactRolloverBoundaryV2:
    old_identity: CandidateIdentityV1
    prepared_identity: CandidateIdentityV1
    generation: int
    qdot_generation: int
    tail_endpoint_s: float
    switch_gate_receipt_sha256: str
    controller_commit_ack: ControllerCommitAckV2
    commit_sample_index: int
    commit_monotonic_s: float
    commit_rtde_timestamp_s: float = 0.0
    schema: str = "step6.autotune/figure8-v5-contact-rollover-boundary-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.old_identity, CandidateIdentityV1) or not isinstance(self.prepared_identity, CandidateIdentityV1) or self.prepared_identity.epoch != self.old_identity.epoch or self.prepared_identity.ordinal <= self.old_identity.ordinal:
            raise V5LifecycleLedgerError("rollover identities are not strictly newer")
        _integer(self.generation, "rollover generation", positive=True)
        if self.qdot_generation != self.generation or not math.isclose(_finite(self.tail_endpoint_s, "tail endpoint"), TAIL_END_S, rel_tol=0.0, abs_tol=1e-12):
            raise V5LifecycleLedgerError("rollover endpoint/generation differs")
        _require_sha(self.switch_gate_receipt_sha256, "switch gate receipt hash")
        if self.switch_gate_receipt_sha256 == GENESIS_SHA256 or not isinstance(self.controller_commit_ack, ControllerCommitAckV2) or self.controller_commit_ack.active_identity != self.prepared_identity or self.controller_commit_ack.generation != self.generation or self.controller_commit_ack.qdot_generation != self.qdot_generation:
            raise V5LifecycleLedgerError("rollover COMMIT ACK differs")
        _integer(self.commit_sample_index, "rollover commit sample")
        commit_mono = _finite(self.commit_monotonic_s, "rollover commit monotonic")
        commit_rtde = _finite(self.commit_rtde_timestamp_s, "rollover commit RTDE")
        if commit_rtde < 0.0 or self.controller_commit_ack.sample_index != self.commit_sample_index or not math.isclose(self.controller_commit_ack.monotonic_s, commit_mono, rel_tol=0.0, abs_tol=1e-12) or not math.isclose(self.controller_commit_ack.rtde_timestamp_s, commit_rtde, rel_tol=0.0, abs_tol=1e-12) or self.schema != "step6.autotune/figure8-v5-contact-rollover-boundary-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("rollover ACK clocks/schema differ")
        object.__setattr__(self, "tail_endpoint_s", TAIL_END_S)
        object.__setattr__(self, "commit_monotonic_s", commit_mono)
        object.__setattr__(self, "commit_rtde_timestamp_s", commit_rtde)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "mode": BoundaryMode.CONTACT_ROLLOVER.value, "old_identity": _identity_dict(self.old_identity), "prepared_identity": _identity_dict(self.prepared_identity), "generation": self.generation, "qdot_generation": self.qdot_generation, "tail_endpoint_s": self.tail_endpoint_s, "switch_gate_receipt_sha256": self.switch_gate_receipt_sha256, "controller_commit_ack": self.controller_commit_ack.as_dict(), "commit_sample_index": self.commit_sample_index, "commit_monotonic_s": self.commit_monotonic_s, "commit_rtde_timestamp_s": self.commit_rtde_timestamp_s}


BoundaryV2 = HomeBoundaryV2 | ContactRolloverBoundaryV2


@dataclass(frozen=True)
class TrialBoundaryReceiptV2:
    boundary: BoundaryV2
    schema: str = "step6.autotune/figure8-v5-trial-boundary-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.boundary, (HomeBoundaryV2, ContactRolloverBoundaryV2)) or self.schema != "step6.autotune/figure8-v5-trial-boundary-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("trial boundary union/schema differs")

    @property
    def mode(self) -> BoundaryMode:
        return BoundaryMode.HOME if isinstance(self.boundary, HomeBoundaryV2) else BoundaryMode.CONTACT_ROLLOVER

    @property
    def safe_return(self) -> bool:
        return self.mode is BoundaryMode.HOME

    @property
    def return_gate(self) -> bool:
        return self.mode is BoundaryMode.HOME

    @property
    def boundary_sha256(self) -> str:
        return canonical_sha256(self.as_dict(include_hash=False))

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {"schema": self.schema, "version": self.version, "mode": self.mode.value, "proof": self.boundary.as_dict()}
        if include_hash:
            value["boundary_sha256"] = canonical_sha256(value)
        return value


@dataclass(frozen=True)
class GateFamiliesV2:
    safety: bool
    timing: bool
    freshness: bool
    tube_cbf: bool
    identity: bool
    command_envelope: bool
    schema: str = "step6.autotune/figure8-v5-gate-families-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.safety, self.timing, self.freshness, self.tube_cbf, self.identity, self.command_envelope)) or self.schema != "step6.autotune/figure8-v5-gate-families-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("gate families are not typed/versioned")

    @property
    def all_passed(self) -> bool:
        return all((self.safety, self.timing, self.freshness, self.tube_cbf, self.identity, self.command_envelope))

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "safety": self.safety, "timing": self.timing, "freshness": self.freshness, "tube_cbf": self.tube_cbf, "identity": self.identity, "command_envelope": self.command_envelope}


@dataclass(frozen=True)
class GateClosureEvidenceV2:
    source_artifact_sha256: str
    source_artifact_size: int
    attempt_id: str
    trial_id: str
    candidate_identity: CandidateIdentityV1
    sample_start_index: int
    sample_end_index: int
    source_rows_sha256: str
    path_summary: Mapping[str, Any]
    tail_summary: Mapping[str, Any]
    timing_acceptance: Mapping[str, Any]
    schema: str = "step6.autotune/figure8-v5-gate-closure-evidence-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.source_artifact_sha256, "gate evidence artifact hash")
        _integer(self.source_artifact_size, "gate evidence artifact size", positive=True)
        if not self.attempt_id or not self.trial_id or not isinstance(self.candidate_identity, CandidateIdentityV1):
            raise V5LifecycleLedgerError("gate evidence identity is incomplete")
        _integer(self.sample_start_index, "gate evidence sample start")
        _integer(self.sample_end_index, "gate evidence sample end")
        if self.sample_end_index <= self.sample_start_index:
            raise V5LifecycleLedgerError("gate evidence sample bounds are empty")
        _require_sha(self.source_rows_sha256, "gate evidence source rows hash")
        if self.source_rows_sha256 == GENESIS_SHA256:
            raise V5LifecycleLedgerError("gate evidence source rows hash is empty")

        def normalize_phase(value: Mapping[str, Any], role: str) -> Mapping[str, Any]:
            if not isinstance(value, Mapping) or set(value) != {
                "observed_count",
                "failure_counts",
                "observation_chain_sha256",
            }:
                raise V5LifecycleLedgerError(f"{role} gate summary schema differs")
            count = _integer(value.get("observed_count"), f"{role} observed count", positive=True)
            failures = value.get("failure_counts")
            if not isinstance(failures, Mapping) or set(failures) != set(_GATE_FAMILY_NAMES):
                raise V5LifecycleLedgerError(f"{role} gate failure families differ")
            normalized_failures = {
                name: _integer(failures[name], f"{role} {name} failures")
                for name in _GATE_FAMILY_NAMES
            }
            if any(value > count for value in normalized_failures.values()):
                raise V5LifecycleLedgerError(f"{role} gate failures exceed observations")
            digest = _require_sha(value.get("observation_chain_sha256"), f"{role} gate observation hash")
            if digest == GENESIS_SHA256:
                raise V5LifecycleLedgerError(f"{role} gate observation hash is empty")
            return _freeze(
                {
                    "observed_count": count,
                    "failure_counts": normalized_failures,
                    "observation_chain_sha256": digest,
                },
                f"{role} gate summary",
            )

        path_summary = normalize_phase(self.path_summary, "PATH")
        tail_summary = normalize_phase(self.tail_summary, "tail")
        if not isinstance(self.timing_acceptance, Mapping) or type(self.timing_acceptance.get("passed")) is not bool:
            raise V5LifecycleLedgerError("gate timing acceptance is not typed")
        if self.schema != "step6.autotune/figure8-v5-gate-closure-evidence-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("gate closure evidence schema/version differs")
        object.__setattr__(self, "path_summary", path_summary)
        object.__setattr__(self, "tail_summary", tail_summary)
        object.__setattr__(self, "timing_acceptance", _freeze(self.timing_acceptance, "gate timing acceptance"))

    def _families(self, summary: Mapping[str, Any]) -> GateFamiliesV2:
        failures = summary["failure_counts"]
        values = {name: failures[name] == 0 for name in _GATE_FAMILY_NAMES}
        values["timing"] = bool(values["timing"] and self.timing_acceptance["passed"])
        return GateFamiliesV2(**values)

    @property
    def path(self) -> GateFamiliesV2:
        return self._families(self.path_summary)

    @property
    def tail(self) -> GateFamiliesV2:
        return self._families(self.tail_summary)

    @property
    def evidence_sha256(self) -> str:
        return canonical_sha256(self.as_dict(include_hash=False))

    def verify_binding(self, artifact: SealedR013ArtifactV2, attempt: "TrialSliceV2") -> None:
        if (
            self.source_artifact_sha256 != artifact.artifact_sha256
            or self.source_artifact_size != artifact.artifact_size
            or self.attempt_id != attempt.attempt_id
            or self.trial_id != attempt.trial_id
            or self.candidate_identity != attempt.candidate_identity
            or self.sample_start_index != attempt.sample_start_index
            or self.sample_end_index != attempt.sample_end_index
            or self.source_rows_sha256
            != canonical_sha256(
                [
                    dict(row)
                    for row in artifact.rows[
                        attempt.sample_start_index:attempt.sample_end_index
                    ]
                ]
            )
        ):
            raise V5LifecycleLedgerError("gate closure evidence is bound to another artifact slice")
        derived = self._cold_summaries(artifact, attempt)
        if (
            dict(self.path_summary) != derived["path"]
            or dict(self.tail_summary) != derived["tail"]
            or dict(self.timing_acceptance) != derived["timing_acceptance"]
        ):
            raise V5LifecycleLedgerError(
                "gate closure evidence is not cold-derived from observations"
            )

    @staticmethod
    def _cold_summaries(
        artifact: SealedR013ArtifactV2,
        attempt: "TrialSliceV2",
    ) -> dict[str, Any]:
        cold = _load_gate_observation_artifact(
            artifact.event_bundle["gate_observation_artifact"],
            source_artifact_path=Path(artifact.artifact_path),
            source_artifact_sha256=artifact.artifact_sha256,
            source_artifact_size=artifact.artifact_size,
            source_rows=artifact.rows,
            events=artifact.events,
            switch_gate_receipts=artifact.event_bundle["switch_gate_receipts"],
        )
        predecessor_seams = {
            event.sample_index
            for event in artifact.events
            if event.kind is LifecycleEventKind.CONTACT_ROLLOVER
            and event.next_identity == attempt.candidate_identity
            and event.sample_index == attempt.sample_start_index
        }
        upper_inclusive = attempt.boundary.mode is BoundaryMode.CONTACT_ROLLOVER
        selected = tuple(
            observation
            for observation in cold["observations"]
            if observation["sample_index"] >= attempt.sample_start_index
            and (
                observation["sample_index"] <= attempt.sample_end_index
                if upper_inclusive
                else observation["sample_index"] < attempt.sample_end_index
            )
            and observation["sample_index"] not in predecessor_seams
        )
        if not selected:
            raise V5LifecycleLedgerError("trial has no cold gate observations")
        for observation in selected:
            expected = attempt.candidate_identity
            if (
                upper_inclusive
                and observation["sample_index"] == attempt.sample_end_index
            ):
                expected = attempt.boundary.boundary.prepared_identity
            if (
                observation["expected_epoch"] != expected.epoch
                or observation["expected_ordinal"] != expected.ordinal
                or observation["expected_candidate_token"]
                != expected.candidate_token
            ):
                raise V5LifecycleLedgerError(
                    "gate observation expected identity differs from trial boundary"
                )

        def summarize(phase: str) -> dict[str, Any]:
            phase_rows = tuple(row for row in selected if row["phase"] == phase)
            if not phase_rows:
                raise V5LifecycleLedgerError(
                    f"trial has no {phase} gate observations"
                )
            failures = {name: 0 for name in _GATE_FAMILY_NAMES}
            previous = GENESIS_SHA256
            for row in phase_rows:
                for name, passed in row["family_results"].items():
                    if not passed:
                        failures[name] += 1
                previous = canonical_sha256(
                    {
                        "previous_sha256": previous,
                        "observation_row_sha256": row["row_sha256"],
                    }
                )
            return {
                "observed_count": len(phase_rows),
                "failure_counts": failures,
                "observation_chain_sha256": previous,
            }

        return {
            "path": summarize("path"),
            "tail": summarize("tail"),
            "timing_acceptance": dict(cold["timing_acceptance"]),
        }

    @classmethod
    def from_artifact(
        cls,
        artifact: SealedR013ArtifactV2,
        attempt: "TrialSliceV2",
    ) -> "GateClosureEvidenceV2":
        summary = cls._cold_summaries(artifact, attempt)
        evidence = cls(
            artifact.artifact_sha256,
            artifact.artifact_size,
            attempt.attempt_id,
            attempt.trial_id,
            attempt.candidate_identity,
            attempt.sample_start_index,
            attempt.sample_end_index,
            canonical_sha256(
                [
                    dict(row)
                    for row in artifact.rows[
                        attempt.sample_start_index:attempt.sample_end_index
                    ]
                ]
            ),
            summary["path"],
            summary["tail"],
            summary["timing_acceptance"],
        )
        evidence.verify_binding(artifact, attempt)
        return evidence

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "GateClosureEvidenceV2":
        try:
            evidence = cls(
                value["source_artifact_sha256"],
                value["source_artifact_size"],
                value["attempt_id"],
                value["trial_id"],
                _identity_from_dict(value["candidate_identity"]),
                value["sample_start_index"],
                value["sample_end_index"],
                value["source_rows_sha256"],
                value["path_summary"],
                value["tail_summary"],
                value["timing_acceptance"],
                schema=value["schema"],
                version=value["version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, V5LifecycleLedgerError):
                raise
            raise V5LifecycleLedgerError("gate closure evidence mapping is invalid") from exc
        if value.get("evidence_sha256") != evidence.evidence_sha256:
            raise V5LifecycleLedgerError("gate closure evidence hash differs")
        return evidence

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "version": self.version,
            "source_artifact_sha256": self.source_artifact_sha256,
            "source_artifact_size": self.source_artifact_size,
            "attempt_id": self.attempt_id,
            "trial_id": self.trial_id,
            "candidate_identity": _identity_dict(self.candidate_identity),
            "sample_start_index": self.sample_start_index,
            "sample_end_index": self.sample_end_index,
            "source_rows_sha256": self.source_rows_sha256,
            "path_summary": dict(self.path_summary),
            "tail_summary": dict(self.tail_summary),
            "timing_acceptance": dict(self.timing_acceptance),
        }
        if include_hash:
            value["evidence_sha256"] = canonical_sha256(value)
        return value


@dataclass(frozen=True)
class PathTailClosureV2:
    path: GateFamiliesV2
    tail: GateFamiliesV2
    evidence: GateClosureEvidenceV2 | None = None
    schema: str = "step6.autotune/figure8-v5-path-tail-closure-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.path, GateFamiliesV2) or not isinstance(self.tail, GateFamiliesV2) or (self.evidence is not None and not isinstance(self.evidence, GateClosureEvidenceV2)) or self.schema != "step6.autotune/figure8-v5-path-tail-closure-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("PATH/tail closure is not typed/versioned")
        if self.evidence is not None and (self.path != self.evidence.path or self.tail != self.evidence.tail):
            raise V5LifecycleLedgerError("PATH/tail closure differs from typed evidence")

    @property
    def all_passed(self) -> bool:
        return self.evidence is not None and self.path.all_passed and self.tail.all_passed

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "path": self.path.as_dict(), "tail": self.tail.as_dict(), "evidence": None if self.evidence is None else self.evidence.as_dict()}


@dataclass(frozen=True)
class TrialSliceV2:
    attempt_id: str
    trial_id: str
    candidate_identity: CandidateIdentityV1
    sample_start_index: int
    sample_end_index: int
    clock_start_s: float
    clock_end_s: float
    metric_snapshot: MetricSnapshotV1
    boundary: TrialBoundaryReceiptV2
    schema: str = "step6.autotune/figure8-v5-trial-slice-v2"
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        if not self.attempt_id or not self.trial_id or any(ch in self.attempt_id + self.trial_id for ch in "\r\n") or not isinstance(self.candidate_identity, CandidateIdentityV1) or not isinstance(self.metric_snapshot, MetricSnapshotV1) or not isinstance(self.boundary, TrialBoundaryReceiptV2):
            raise V5LifecycleLedgerError("trial slice identity/evidence is incomplete")
        _integer(self.sample_start_index, "trial sample start")
        _integer(self.sample_end_index, "trial sample end")
        if self.sample_end_index <= self.sample_start_index:
            raise V5LifecycleLedgerError("trial slice sample bounds are empty")
        start = _finite(self.clock_start_s, "trial clock start")
        end = _finite(self.clock_end_s, "trial clock end")
        if end <= start or self.metric_snapshot.candidate_identity != self.candidate_identity or self.metric_snapshot.sample_start_index < self.sample_start_index or self.metric_snapshot.sample_end_index > self.sample_end_index or not math.isclose(self.metric_snapshot.clock_start_s, start, rel_tol=0.0, abs_tol=1e-12) or self.metric_snapshot.clock_end_s > end + 1e-12:
            raise V5LifecycleLedgerError("trial slice does not bind metric bounds")
        if self.boundary.mode is BoundaryMode.CONTACT_ROLLOVER:
            proof = self.boundary.boundary
            if proof.old_identity != self.candidate_identity or proof.commit_sample_index != self.sample_end_index or not math.isclose(proof.commit_monotonic_s, end, rel_tol=0.0, abs_tol=1e-12):
                raise V5LifecycleLedgerError("rollover boundary is not the half-open old-slice seam")
        else:
            proof = self.boundary.boundary
            if proof.sample_index != self.sample_end_index - 1 or not math.isclose(proof.monotonic_s, end, rel_tol=0.0, abs_tol=1e-12):
                raise V5LifecycleLedgerError("HOME boundary does not close the terminal slice")
        if self.schema != "step6.autotune/figure8-v5-trial-slice-v2" or self.version != V5_LIFECYCLE_VERSION:
            raise V5LifecycleLedgerError("trial slice schema/version differs")
        object.__setattr__(self, "clock_start_s", start)
        object.__setattr__(self, "clock_end_s", end)

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "attempt_id": self.attempt_id, "trial_id": self.trial_id, "candidate_identity": _identity_dict(self.candidate_identity), "sample_start_index": self.sample_start_index, "sample_end_index": self.sample_end_index, "clock_start_s": self.clock_start_s, "clock_end_s": self.clock_end_s, "metric_snapshot": self.metric_snapshot.as_dict(), "boundary": self.boundary.as_dict()}


def _check_event_row(event: LifecycleEventV2, row: Mapping[str, Any], *, kind: LifecycleEventKind) -> None:
    if event.kind is not kind or event.sample_index != int(row["sample_index"]) or not math.isclose(event.monotonic_s, float(row["monotonic_s"]), rel_tol=0.0, abs_tol=1e-12) or not math.isclose(event.rtde_timestamp_s, float(row["rtde_timestamp_s"]), rel_tol=0.0, abs_tol=1e-12) or event.referenced_row_sha256 != lifecycle_row_evidence_sha256(row):
        raise V5LifecycleLedgerError("lifecycle event does not bind its referenced row")


def _check_fresh(row: Mapping[str, Any]) -> None:
    flags = int(row.get("flags", 0))
    if not flags & FLAG_SENSOR_PRESENT or not flags & FLAG_SENSOR_FRESH:
        raise V5LifecycleLedgerError("referenced lifecycle row lacks fresh sensor evidence")


def _check_attempt_boundary(artifact: SealedR013ArtifactV2, attempt: TrialSliceV2) -> None:
    if attempt.sample_end_index > len(artifact.rows):
        raise V5LifecycleLedgerError("trial slice exceeds artifact")
    if attempt.boundary.mode is BoundaryMode.CONTACT_ROLLOVER:
        proof = attempt.boundary.boundary
        if not 0 <= proof.commit_sample_index < len(artifact.rows) or proof.commit_sample_index != attempt.sample_end_index:
            raise V5LifecycleLedgerError("rollover commit sample is outside the typed old half-open slice")
        seam = artifact.rows[proof.commit_sample_index]
        matches = [event for event in artifact.events if event.kind is LifecycleEventKind.CONTACT_ROLLOVER and event.sample_index == proof.commit_sample_index]
        if not matches or seam.get("tp_state") != int(V5TPState.ROLLOVER_COMMITTED) or seam.get("phase_code") != 0:
            raise V5LifecycleLedgerError("rollover boundary does not reference a real state28 seam")
        _check_fresh(seam)
        event = matches[0]
        _check_event_row(event, seam, kind=LifecycleEventKind.CONTACT_ROLLOVER)
        if event.identity != proof.old_identity or event.next_identity != proof.prepared_identity or event.generation != proof.generation or event.qdot_generation != proof.qdot_generation or event.event_evidence_sha256 != proof.switch_gate_receipt_sha256 or not math.isclose(proof.commit_monotonic_s, float(seam["monotonic_s"]), rel_tol=0.0, abs_tol=1e-12) or not math.isclose(event.rtde_timestamp_s, float(seam["rtde_timestamp_s"]), rel_tol=0.0, abs_tol=1e-12) or not math.isclose(proof.commit_rtde_timestamp_s, float(seam["rtde_timestamp_s"]), rel_tol=0.0, abs_tol=1e-12) or not math.isclose(proof.controller_commit_ack.rtde_timestamp_s, float(seam["rtde_timestamp_s"]), rel_tol=0.0, abs_tol=1e-12):
            raise V5LifecycleLedgerError("rollover boundary/event identity differs")
    else:
        proof = attempt.boundary.boundary
        if proof.sample_index >= len(artifact.rows):
            raise V5LifecycleLedgerError("HOME boundary is outside artifact")
        home = artifact.rows[proof.sample_index]
        matches = [event for event in artifact.events if event.kind is LifecycleEventKind.HOME and event.sample_index == proof.sample_index]
        if not matches or home.get("tp_state") != int(V5TPState.READY_HOME_NEXT) or home.get("phase_code") != int(V5TPState.READY_HOME_NEXT) or not int(home.get("flags", 0)) & FLAG_TERMINAL:
            raise V5LifecycleLedgerError("HOME boundary does not reference the final typed Home row")
        _check_fresh(home)
        event = matches[0]
        _check_event_row(event, home, kind=LifecycleEventKind.HOME)
        if event.identity != attempt.candidate_identity or event.terminal_state is not V5TPState.READY_HOME_NEXT or event.event_evidence_sha256 != proof.home_evidence_sha256 or not math.isclose(proof.monotonic_s, float(home["monotonic_s"]), rel_tol=0.0, abs_tol=1e-12) or not math.isclose(proof.rtde_timestamp_s, float(home["rtde_timestamp_s"]), rel_tol=0.0, abs_tol=1e-12):
            raise V5LifecycleLedgerError("HOME boundary/event evidence differs")


@dataclass(frozen=True)
class V5ChainBindingV2:
    campaign_fingerprint: str
    release_identity_sha256: str
    role: LedgerRole
    chain_id: str
    artifact: SealedR013ArtifactV2
    attempts: tuple[TrialSliceV2, ...]
    schema: str = V5_LIFECYCLE_SCHEMA
    version: int = V5_LIFECYCLE_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.campaign_fingerprint, "chain campaign fingerprint")
        _require_sha(self.release_identity_sha256, "chain release identity")
        if not isinstance(self.role, LedgerRole) or not self.chain_id or not isinstance(self.artifact, SealedR013ArtifactV2):
            raise V5LifecycleLedgerError("chain identity/artifact is incomplete")
        _require_cold_artifact_object(self.artifact)
        attempts = tuple(self.attempts)
        if not 1 <= len(attempts) <= MAX_ATTEMPTS_PER_CHAIN:
            raise V5LifecycleLedgerError("chain must contain one to five attempts")
        if len(attempts) - 1 > MAX_ROLLOVERS_PER_CHAIN:
            raise V5LifecycleLedgerError("chain exceeds four rollovers")
        start_events = [event for event in self.artifact.events if event.kind is LifecycleEventKind.CHAIN_START]
        rollover_events = [event for event in self.artifact.events if event.kind is LifecycleEventKind.CONTACT_ROLLOVER]
        home_events = [event for event in self.artifact.events if event.kind is LifecycleEventKind.HOME]
        if len(start_events) != 1 or len(rollover_events) != len(attempts) - 1 or len(home_events) != 1:
            raise V5LifecycleLedgerError("chain event counts differ")
        start_event = start_events[0]
        first = attempts[0]
        if first.sample_start_index != start_event.sample_index or not math.isclose(first.clock_start_s, start_event.monotonic_s, rel_tol=0.0, abs_tol=1e-12):
            raise V5LifecycleLedgerError("first trial does not start at CHAIN_START")
        first_row = self.artifact.rows[start_event.sample_index]
        _check_event_row(start_event, first_row, kind=LifecycleEventKind.CHAIN_START)
        _check_fresh(first_row)
        if first_row.get("tp_state") != int(V5TPState.PATH) or first_row.get("phase_code") != int(V5TPState.PATH) or start_event.identity != first.candidate_identity or start_event.event_evidence_sha256 != self.artifact.event_bundle.get("entry_evidence_sha256"):
            raise V5LifecycleLedgerError("CHAIN_START entry evidence differs")
        identifiers: set[str] = set()
        previous: TrialSliceV2 | None = None
        for index, attempt in enumerate(attempts):
            if attempt.attempt_id in identifiers or attempt.trial_id in identifiers:
                raise V5LifecycleLedgerError("chain attempt/trial IDs repeat")
            identifiers.update((attempt.attempt_id, attempt.trial_id))
            if attempt.metric_snapshot.source_artifact_sha256 != self.artifact.artifact_sha256 or attempt.metric_snapshot.source_artifact_size != self.artifact.artifact_size:
                raise V5LifecycleLedgerError("trial metric is bound to another artifact")
            if previous is not None:
                if attempt.candidate_identity.epoch != previous.candidate_identity.epoch or attempt.candidate_identity.ordinal <= previous.candidate_identity.ordinal or attempt.sample_start_index != previous.sample_end_index or not math.isclose(attempt.clock_start_s, previous.clock_end_s, rel_tol=0.0, abs_tol=1e-12):
                    raise V5LifecycleLedgerError("chain slices are not contiguous at the typed seam")
                if previous.boundary.mode is not BoundaryMode.CONTACT_ROLLOVER or previous.boundary.boundary.prepared_identity != attempt.candidate_identity:
                    raise V5LifecycleLedgerError("chain next identity does not match rollover")
            row = self.artifact.rows[attempt.sample_start_index]
            if not math.isclose(float(row["monotonic_s"]), attempt.clock_start_s, rel_tol=0.0, abs_tol=1e-12):
                raise V5LifecycleLedgerError("trial start clock does not bind row")
            _check_attempt_boundary(self.artifact, attempt)
            derived = MetricSnapshotV1.from_artifact_slice(self.artifact, candidate_identity=attempt.candidate_identity, sample_start_index=attempt.sample_start_index, sample_end_index=attempt.sample_end_index, path_clock_start_s=attempt.clock_start_s)
            if derived.as_dict(include_hash=False) != attempt.metric_snapshot.as_dict(include_hash=False):
                raise V5LifecycleLedgerError("trial metric is not cold-derived")
            previous = attempt
        if attempts[-1].boundary.mode is not BoundaryMode.HOME or any(attempt.boundary.mode is BoundaryMode.HOME for attempt in attempts[:-1]):
            raise V5LifecycleLedgerError("only the terminal attempt may carry HOME")
        terminal_home = home_events[0]
        if terminal_home.sample_index != attempts[-1].boundary.boundary.sample_index or terminal_home.sample_index != len(self.artifact.rows) - 1:
            raise V5LifecycleLedgerError("HOME event is not the final artifact row")
        object.__setattr__(self, "attempts", attempts)

    @property
    def chain_sha256(self) -> str:
        return canonical_sha256(self.as_dict(include_hash=False))

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {"schema": self.schema, "version": self.version, "campaign_fingerprint": self.campaign_fingerprint, "release_identity_sha256": self.release_identity_sha256, "role": self.role.value, "chain_id": self.chain_id, "artifact_sha256": self.artifact.artifact_sha256, "artifact_size": self.artifact.artifact_size, "attempts": [attempt.as_dict() for attempt in self.attempts]}
        if include_hash:
            value["chain_sha256"] = canonical_sha256(value)
        return value


def bind_v5_chain(artifact: SealedR013ArtifactV2, attempts: Sequence[TrialSliceV2], *, campaign_fingerprint: str, release_identity_sha256: str, role: LedgerRole, chain_id: str) -> V5ChainBindingV2:
    return V5ChainBindingV2(campaign_fingerprint, release_identity_sha256, role, chain_id, artifact, tuple(attempts))


@dataclass(frozen=True)
class FigureEightPhysicalRecordV2:
    campaign_fingerprint: str
    release_identity_sha256: str
    role: LedgerRole
    chain_id: str
    attempt_id: str
    trial_id: str
    candidate_identity: CandidateIdentityV1
    metric_snapshot: MetricSnapshotV1
    boundary: TrialBoundaryReceiptV2
    trial_slice: TrialSliceV2
    source_artifact: SealedR013ArtifactV2
    closure: PathTailClosureV2
    censored: bool = False
    optional_telemetry: Mapping[str, Any] = MappingProxyType({})
    schema: str = V5_LEDGER_SCHEMA
    version: int = V5_LEDGER_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.campaign_fingerprint, "record campaign fingerprint")
        _require_sha(self.release_identity_sha256, "record release identity")
        if not isinstance(self.role, LedgerRole) or not all(isinstance(value, str) and value for value in (self.chain_id, self.attempt_id, self.trial_id)) or not isinstance(self.candidate_identity, CandidateIdentityV1) or not isinstance(self.metric_snapshot, MetricSnapshotV1) or not isinstance(self.boundary, TrialBoundaryReceiptV2) or not isinstance(self.trial_slice, TrialSliceV2) or not isinstance(self.source_artifact, SealedR013ArtifactV2) or not isinstance(self.closure, PathTailClosureV2):
            raise V5LifecycleLedgerError("physical record typed fields are incomplete")
        _require_cold_artifact_object(self.source_artifact)
        if self.metric_snapshot.candidate_identity != self.candidate_identity or self.trial_slice.candidate_identity != self.candidate_identity or self.trial_slice.metric_snapshot.as_dict(include_hash=False) != self.metric_snapshot.as_dict(include_hash=False) or self.trial_slice.boundary != self.boundary or self.trial_slice.trial_id != self.trial_id or self.trial_slice.attempt_id != self.attempt_id or self.metric_snapshot.source_artifact_sha256 != self.source_artifact.artifact_sha256 or self.metric_snapshot.source_artifact_size != self.source_artifact.artifact_size:
            raise V5LifecycleLedgerError("physical record identity/evidence differs")
        _check_attempt_boundary(self.source_artifact, self.trial_slice)
        if self.closure.evidence is not None:
            self.closure.evidence.verify_binding(
                self.source_artifact,
                self.trial_slice,
            )
        derived = MetricSnapshotV1.from_artifact_slice(self.source_artifact, candidate_identity=self.candidate_identity, sample_start_index=self.trial_slice.sample_start_index, sample_end_index=self.trial_slice.sample_end_index, path_clock_start_s=self.trial_slice.clock_start_s)
        if derived.as_dict(include_hash=False) != self.metric_snapshot.as_dict(include_hash=False):
            raise V5LifecycleLedgerError("physical record metric is not cold-derived")
        if type(self.censored) is not bool or not isinstance(self.optional_telemetry, Mapping) or self.schema != V5_LEDGER_SCHEMA or self.version != V5_LEDGER_VERSION:
            raise V5LifecycleLedgerError("physical record state/schema differs")
        object.__setattr__(self, "optional_telemetry", _freeze(self.optional_telemetry, "optional telemetry"))

    @property
    def artifact_binding(self) -> ArtifactBindingV2:
        return ArtifactBindingV2.from_artifact(self.source_artifact)

    @property
    def admission_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.censored:
            reasons.append("censored")
        if not self.metric_snapshot.closure.sealed:
            reasons.append("metric_not_sealed")
        if self.closure.evidence is None:
            reasons.append("gate_closure_evidence_missing")
        if not self.closure.all_passed:
            reasons.append("path_tail_closure_failed")
        if self.boundary.mode is BoundaryMode.CONTACT_ROLLOVER and not math.isclose(self.boundary.boundary.tail_endpoint_s, TAIL_END_S, rel_tol=0.0, abs_tol=1e-12):
            reasons.append("rollover_endpoint_mismatch")
        if self.boundary.mode not in (BoundaryMode.HOME, BoundaryMode.CONTACT_ROLLOVER):
            reasons.append("boundary_missing")
        return tuple(reasons)

    @property
    def eligible(self) -> bool:
        return not self.admission_reasons

    @property
    def record_sha256(self) -> str:
        return canonical_sha256(self.as_dict(include_hash=False))

    def tell_token(self) -> str | None:
        if not self.eligible:
            return None
        return canonical_sha256({"schema": V5_TELL_SCHEMA, "role": self.role.value, "campaign_fingerprint": self.campaign_fingerprint, "trial_id": self.trial_id, "record_sha256": self.record_sha256})

    @classmethod
    def from_attempt(cls, chain: V5ChainBindingV2, attempt: TrialSliceV2, closure: PathTailClosureV2, *, censored: bool = False, optional_telemetry: Mapping[str, Any] | None = None) -> "FigureEightPhysicalRecordV2":
        if attempt not in chain.attempts:
            raise V5LifecycleLedgerError("attempt is not part of the bound chain")
        return cls(chain.campaign_fingerprint, chain.release_identity_sha256, chain.role, chain.chain_id, attempt.attempt_id, attempt.trial_id, attempt.candidate_identity, attempt.metric_snapshot, attempt.boundary, attempt, chain.artifact, closure, censored, {} if optional_telemetry is None else optional_telemetry)

    def as_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        value = {"schema": self.schema, "version": self.version, "campaign_fingerprint": self.campaign_fingerprint, "release_identity_sha256": self.release_identity_sha256, "role": self.role.value, "chain_id": self.chain_id, "attempt_id": self.attempt_id, "trial_id": self.trial_id, "candidate_identity": _identity_dict(self.candidate_identity), "artifact_binding": self.artifact_binding.as_dict(), "trial_slice": self.trial_slice.as_dict(), "metric_snapshot": self.metric_snapshot.as_dict(), "boundary": self.boundary.as_dict(), "closure": self.closure.as_dict(), "censored": self.censored, "optional_telemetry": dict(self.optional_telemetry), "eligible": self.eligible, "admission_reasons": list(self.admission_reasons)}
        if include_hash:
            value["record_sha256"] = canonical_sha256(value)
        return value


@dataclass(frozen=True)
class PhysicalAdmissionReceiptV2:
    trial_id: str
    record_sha256: str
    ledger_row_sha256: str
    eligible: bool
    reason: str
    tell_token: str | None
    campaign_fingerprint: str
    release_identity_sha256: str
    role: LedgerRole
    cold_verified_head_sha256: str
    schema: str = V5_RECEIPT_SCHEMA
    version: int = V5_LEDGER_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.record_sha256, "admission record hash")
        _require_sha(self.ledger_row_sha256, "admission row hash")
        _require_sha(self.campaign_fingerprint, "admission campaign fingerprint")
        _require_sha(self.release_identity_sha256, "admission release identity")
        _require_sha(self.cold_verified_head_sha256, "cold ledger head")
        if not isinstance(self.role, LedgerRole) or not self.trial_id or not self.reason or type(self.eligible) is not bool or self.eligible != (self.tell_token is not None) or self.schema != V5_RECEIPT_SCHEMA or self.version != V5_LEDGER_VERSION:
            raise V5LifecycleLedgerError("admission receipt is incomplete")
        if self.tell_token is not None:
            _require_sha(self.tell_token, "tell token")

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "trial_id": self.trial_id, "record_sha256": self.record_sha256, "ledger_row_sha256": self.ledger_row_sha256, "eligible": self.eligible, "reason": self.reason, "tell_token": self.tell_token, "campaign_fingerprint": self.campaign_fingerprint, "release_identity_sha256": self.release_identity_sha256, "role": self.role.value, "cold_verified_head_sha256": self.cold_verified_head_sha256}


@dataclass(frozen=True)
class OptimizerReceiptV2:
    trial_id: str
    tell_token: str
    record_sha256: str
    optimizer_receipt_id: str
    schema: str = "step6.autotune/figure8-v5-optimizer-receipt-v2"
    version: int = V5_LEDGER_VERSION

    def __post_init__(self) -> None:
        if not self.trial_id or not self.optimizer_receipt_id or self.schema != "step6.autotune/figure8-v5-optimizer-receipt-v2" or self.version != V5_LEDGER_VERSION:
            raise V5LifecycleLedgerError("optimizer receipt identity/schema is incomplete")
        _require_sha(self.tell_token, "optimizer tell token")
        _require_sha(self.record_sha256, "optimizer record hash")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "OptimizerReceiptV2":
        try:
            return cls(value["trial_id"], value["tell_token"], value["record_sha256"], value["optimizer_receipt_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise V5LifecycleLedgerError("optimizer receipt mapping is invalid") from exc

    def as_dict(self) -> dict[str, Any]:
        return {"schema": self.schema, "version": self.version, "trial_id": self.trial_id, "tell_token": self.tell_token, "record_sha256": self.record_sha256, "optimizer_receipt_id": self.optimizer_receipt_id}


@dataclass(frozen=True)
class TellAuthorizationV2:
    trial_id: str
    record_sha256: str
    tell_token: str
    state: TellState
    ledger_head_sha256: str
    schema: str = V5_TELL_SCHEMA
    version: int = V5_LEDGER_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.record_sha256, "tell record hash")
        _require_sha(self.tell_token, "tell token")
        _require_sha(self.ledger_head_sha256, "tell ledger head")
        if not self.trial_id or not isinstance(self.state, TellState) or self.schema != V5_TELL_SCHEMA or self.version != V5_LEDGER_VERSION:
            raise V5LifecycleLedgerError("tell authorization is incomplete")


@dataclass(frozen=True)
class TellReconciliationV2:
    trial_id: str
    tell_token: str
    state: TellState
    ledger_head_sha256: str
    optimizer_receipt_id: str | None = None
    schema: str = V5_TELL_SCHEMA
    version: int = V5_LEDGER_VERSION

    def __post_init__(self) -> None:
        _require_sha(self.tell_token, "reconciliation tell token")
        _require_sha(self.ledger_head_sha256, "reconciliation ledger head")
        if not self.trial_id or not isinstance(self.state, TellState) or self.schema != V5_TELL_SCHEMA or self.version != V5_LEDGER_VERSION:
            raise V5LifecycleLedgerError("tell reconciliation is incomplete")


def _record_from_dict(value: Mapping[str, Any]) -> FigureEightPhysicalRecordV2:
    try:
        if value.get("schema") != V5_LEDGER_SCHEMA or value.get("version") != V5_LEDGER_VERSION:
            raise V5LifecycleLedgerError("physical record schema/version differs")
        binding = ArtifactBindingV2.from_mapping(value["artifact_binding"])
        artifact = binding.cold_index()
        if binding.artifact_sha256 != artifact.artifact_sha256 or binding.artifact_size != artifact.artifact_size:
            raise V5LifecycleLedgerError("artifact binding bytes differ")
        metric = MetricSnapshotV1.from_dict(value["metric_snapshot"])
        boundary_value = value["boundary"]
        proof = boundary_value["proof"]
        if boundary_value["mode"] == BoundaryMode.HOME.value:
            boundary = TrialBoundaryReceiptV2(HomeBoundaryV2(V5TPState(proof["terminal_state"]), proof["sample_index"], proof["monotonic_s"], proof["home_evidence_sha256"], proof.get("rtde_timestamp_s", 0.0)))
        elif boundary_value["mode"] == BoundaryMode.CONTACT_ROLLOVER.value:
            ack_value = proof["controller_commit_ack"]
            ack = ControllerCommitAckV2(_identity_from_dict(ack_value["active_identity"]), ack_value["generation"], ack_value["qdot_generation"], ack_value["sample_index"], ack_value["monotonic_s"], ack_value.get("rtde_timestamp_s", 0.0))
            boundary = TrialBoundaryReceiptV2(ContactRolloverBoundaryV2(_identity_from_dict(proof["old_identity"]), _identity_from_dict(proof["prepared_identity"]), proof["generation"], proof["qdot_generation"], proof["tail_endpoint_s"], proof["switch_gate_receipt_sha256"], ack, proof["commit_sample_index"], proof["commit_monotonic_s"], proof.get("commit_rtde_timestamp_s", 0.0)))
        else:
            raise V5LifecycleLedgerError("physical boundary mode is unknown")
        closure_value = value["closure"]
        def gates(item: Mapping[str, Any]) -> GateFamiliesV2:
            return GateFamiliesV2(item["safety"], item["timing"], item["freshness"], item["tube_cbf"], item["identity"], item["command_envelope"])
        evidence_value = closure_value.get("evidence")
        closure = PathTailClosureV2(
            gates(closure_value["path"]),
            gates(closure_value["tail"]),
            None if evidence_value is None else GateClosureEvidenceV2.from_mapping(evidence_value),
        )
        slice_value = value["trial_slice"]
        trial_slice = TrialSliceV2(value["attempt_id"], value["trial_id"], _identity_from_dict(slice_value["candidate_identity"]), slice_value["sample_start_index"], slice_value["sample_end_index"], slice_value["clock_start_s"], slice_value["clock_end_s"], metric, boundary)
        record = FigureEightPhysicalRecordV2(value["campaign_fingerprint"], value["release_identity_sha256"], LedgerRole(value["role"]), value["chain_id"], value["attempt_id"], value["trial_id"], _identity_from_dict(value["candidate_identity"]), metric, boundary, trial_slice, artifact, closure, value["censored"], value.get("optional_telemetry", {}))
        if value.get("eligible") is not record.eligible or value.get("admission_reasons") != list(record.admission_reasons) or value.get("record_sha256") not in (None, record.record_sha256):
            raise V5LifecycleLedgerError("persisted physical record admission/hash differs")
        return record
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, V5LifecycleLedgerError):
            raise
        raise V5LifecycleLedgerError("physical record mapping is invalid") from exc


def physical_record_from_mapping(
    value: Mapping[str, Any],
) -> FigureEightPhysicalRecordV2:
    """Cold-rebuild a record from one canonical persisted mapping.

    Rebuilding re-indexes and hashes the referenced sealed lifecycle artifact;
    caller-provided metric or admission fields are never trusted directly.
    """

    if not isinstance(value, Mapping):
        raise V5LifecycleLedgerError("physical record mapping is not typed")
    return _record_from_dict(value)


@dataclass(frozen=True)
class LedgerVerificationV2:
    record_count: int
    tell_count: int
    head_sha256: str
    schema: str = "step6.autotune/figure8-v5-ledger-verification-v2"
    version: int = V5_LEDGER_VERSION

    def __post_init__(self) -> None:
        _integer(self.record_count, "verified record count")
        _integer(self.tell_count, "verified tell count")
        _require_sha(self.head_sha256, "verified ledger head")


class V5PhysicalAdmissionLedgerV2:
    """One role/fingerprint namespace spanning multiple sealed artifacts."""

    def __init__(self, path: Path, *, campaign_fingerprint: str, release_identity_sha256: str, role: LedgerRole) -> None:
        self.path = Path(path)
        self.campaign_fingerprint = _require_sha(campaign_fingerprint, "ledger campaign fingerprint")
        self.release_identity_sha256 = _require_sha(release_identity_sha256, "ledger release identity")
        if not isinstance(role, LedgerRole):
            raise V5LifecycleLedgerError("ledger role is not typed")
        self.role = role
        if self.path.is_symlink():
            raise V5LifecycleLedgerError("ledger path must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._create_header()
        self._records: dict[str, FigureEightPhysicalRecordV2] = {}
        self._tell_states: dict[str, tuple[TellState, str, str, OptimizerReceiptV2 | None]] = {}
        self._head_sha256 = GENESIS_SHA256
        self.fresh_process_verify()

    @property
    def head_sha256(self) -> str:
        return self._head_sha256

    @property
    def records(self) -> tuple[FigureEightPhysicalRecordV2, ...]:
        return tuple(self._records.values())

    def _header(self) -> dict[str, Any]:
        return {"schema": V5_LEDGER_SCHEMA, "version": V5_LEDGER_VERSION, "record_type": "header", "campaign_fingerprint": self.campaign_fingerprint, "release_identity_sha256": self.release_identity_sha256, "role": self.role.value, "metric_signal": METRIC_SIGNAL_NAME, "genesis_sha256": GENESIS_SHA256}

    def _create_header(self) -> None:
        try:
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(_canonical(self._header()).decode() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            pass

    def _read_rows(self) -> list[dict[str, Any]]:
        if self.path.is_symlink() or not self.path.is_file():
            raise V5LifecycleLedgerError("ledger is not a regular file")
        rows: list[dict[str, Any]] = []
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                raise V5LifecycleLedgerError(f"ledger has an empty row at {line_number}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise V5LifecycleLedgerError(f"ledger row {line_number} is invalid JSON") from exc
            if not isinstance(row, dict):
                raise V5LifecycleLedgerError(f"ledger row {line_number} is not an object")
            rows.append(row)
        return rows

    @staticmethod
    def _row_hash(row: Mapping[str, Any]) -> str:
        return canonical_sha256({key: value for key, value in row.items() if key != "row_sha256"})

    def fresh_process_verify(self) -> LedgerVerificationV2:
        rows = self._read_rows()
        if not rows or rows[0].get("schema") == "step6.autotune/figure8-physical-ledger-v1" or rows[0] != self._header():
            raise V5LifecycleLedgerError("V1/old-state or wrong V5 ledger header")
        previous = GENESIS_SHA256
        records: dict[str, FigureEightPhysicalRecordV2] = {}
        tells: dict[str, tuple[TellState, str, str, OptimizerReceiptV2 | None]] = {}
        attempt_ids: set[str] = set()
        for index, row in enumerate(rows[1:], 1):
            if row.get("schema") != V5_LEDGER_SCHEMA or row.get("version") != V5_LEDGER_VERSION or row.get("campaign_fingerprint") != self.campaign_fingerprint or row.get("release_identity_sha256") != self.release_identity_sha256 or row.get("role") != self.role.value or row.get("previous_sha256") != previous or row.get("row_sha256") != self._row_hash(row):
                raise V5LifecycleLedgerError(f"ledger hash/namespace differs at row {index}")
            if row.get("record_type") == "physical_record":
                record = _record_from_dict(row.get("record", {}))
                if record.campaign_fingerprint != self.campaign_fingerprint or record.release_identity_sha256 != self.release_identity_sha256 or record.role is not self.role or record.trial_id in records or record.attempt_id in attempt_ids or row.get("record_sha256") != record.record_sha256:
                    raise V5LifecycleLedgerError("physical record namespace/identity/hash differs")
                records[record.trial_id] = record
                attempt_ids.add(record.attempt_id)
            elif row.get("record_type") == "tell_state":
                trial_id = row.get("trial_id")
                if trial_id not in records:
                    raise V5LifecycleLedgerError("tell state has no physical record")
                record = records[trial_id]
                try:
                    state = TellState(row.get("state"))
                except ValueError as exc:
                    raise V5LifecycleLedgerError("tell state is unknown") from exc
                token = row.get("tell_token")
                if token != record.tell_token() or not record.eligible:
                    raise V5LifecycleLedgerError("tell state token/eligibility differs")
                prior = tells.get(trial_id)
                if prior is None:
                    if state is not TellState.PREPARED:
                        raise V5LifecycleLedgerError("tell state must begin at PREPARED")
                else:
                    allowed = {TellState.PREPARED: {TellState.AUTHORIZED}, TellState.AUTHORIZED: {TellState.AMBIGUOUS, TellState.OPTIMIZER_RECEIPT}, TellState.AMBIGUOUS: {TellState.OPTIMIZER_RECEIPT}, TellState.OPTIMIZER_RECEIPT: {TellState.COMMITTED}, TellState.COMMITTED: set()}[prior[0]]
                    if state not in allowed:
                        raise V5LifecycleLedgerError("tell state transition is not exactly-once")
                optimizer_receipt = None
                receipt_value = row.get("optimizer_receipt")
                if state in {TellState.PREPARED, TellState.AUTHORIZED, TellState.AMBIGUOUS}:
                    if receipt_value is not None:
                        raise V5LifecycleLedgerError("pre-receipt tell state carries an optimizer receipt")
                else:
                    if receipt_value is None:
                        raise V5LifecycleLedgerError("post-receipt tell state lacks an optimizer receipt")
                    optimizer_receipt = OptimizerReceiptV2.from_mapping(row.get("optimizer_receipt", {}))
                    if optimizer_receipt.trial_id != trial_id or optimizer_receipt.tell_token != token or optimizer_receipt.record_sha256 != record.record_sha256:
                        raise V5LifecycleLedgerError("optimizer receipt differs")
                tells[trial_id] = (state, token, record.record_sha256, optimizer_receipt)
            else:
                raise V5LifecycleLedgerError("ledger row type is unknown")
            previous = row["row_sha256"]
        self._records = records
        self._tell_states = tells
        self._head_sha256 = previous
        return LedgerVerificationV2(len(records), len(tells), previous)

    def _append(self, payload: Mapping[str, Any]) -> str:
        row = {"schema": V5_LEDGER_SCHEMA, "version": V5_LEDGER_VERSION, "record_type": payload["record_type"], "campaign_fingerprint": self.campaign_fingerprint, "release_identity_sha256": self.release_identity_sha256, "role": self.role.value, "previous_sha256": self._head_sha256, **{key: value for key, value in payload.items() if key != "record_type"}}
        row["row_sha256"] = self._row_hash(row)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(_canonical(row).decode() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._head_sha256 = row["row_sha256"]
        return self._head_sha256

    def append_record(self, record: FigureEightPhysicalRecordV2) -> PhysicalAdmissionReceiptV2:
        self.fresh_process_verify()
        if not isinstance(record, FigureEightPhysicalRecordV2) or record.campaign_fingerprint != self.campaign_fingerprint or record.release_identity_sha256 != self.release_identity_sha256 or record.role is not self.role:
            raise V5LifecycleLedgerError("physical record crosses ledger namespace")
        cold = record.artifact_binding.cold_index()
        if cold.artifact_sha256 != record.source_artifact.artifact_sha256 or cold.artifact_size != record.source_artifact.artifact_size:
            raise V5LifecycleLedgerError("physical record artifact changed before append")
        _require_cold_artifact_object(record.source_artifact)
        if record.trial_id in self._records or any(record.attempt_id == existing.attempt_id for existing in self._records.values()):
            raise V5LifecycleLedgerError("physical record trial/attempt ID repeats")
        row_hash = self._append({"record_type": "physical_record", "record_sha256": record.record_sha256, "record": record.as_dict()})
        self._records[record.trial_id] = record
        return PhysicalAdmissionReceiptV2(record.trial_id, record.record_sha256, row_hash, record.eligible, "eligible" if record.eligible else record.admission_reasons[0], record.tell_token(), self.campaign_fingerprint, self.release_identity_sha256, self.role, self._head_sha256)

    def _record(self, trial_id: str) -> FigureEightPhysicalRecordV2:
        self.fresh_process_verify()
        try:
            return self._records[trial_id]
        except KeyError as exc:
            raise V5LifecycleLedgerError("trial ID is absent") from exc

    def _authorization(self, record: FigureEightPhysicalRecordV2, state: TellState) -> TellAuthorizationV2:
        token = record.tell_token()
        if token is None:
            raise V5LifecycleLedgerError("ineligible trial has no tell authorization")
        return TellAuthorizationV2(record.trial_id, record.record_sha256, token, state, self._head_sha256)

    def prepare_tell(self, record_or_trial_id: FigureEightPhysicalRecordV2 | str) -> TellAuthorizationV2:
        record = record_or_trial_id if isinstance(record_or_trial_id, FigureEightPhysicalRecordV2) else self._record(record_or_trial_id)
        self.fresh_process_verify()
        if record.trial_id not in self._records or self._records[record.trial_id].record_sha256 != record.record_sha256:
            raise V5LifecycleLedgerError("tell preparation record differs from cold ledger record")
        if not record.eligible:
            raise V5LifecycleLedgerError("ineligible/censored trial cannot prepare tell")
        existing = self._tell_states.get(record.trial_id)
        if existing is None:
            token = record.tell_token()
            self._append({"record_type": "tell_state", "trial_id": record.trial_id, "tell_token": token, "state": TellState.PREPARED.value})
            self._tell_states[record.trial_id] = (TellState.PREPARED, token or "", record.record_sha256, None)
            existing = self._tell_states[record.trial_id]
        return self._authorization(record, existing[0])

    def authorize_tell(self, trial_id: str) -> TellAuthorizationV2:
        record = self._record(trial_id)
        if not record.eligible:
            raise V5LifecycleLedgerError("ineligible/censored trial cannot authorize tell")
        existing = self._tell_states.get(trial_id)
        if existing is None:
            raise V5LifecycleLedgerError("tell authorization requires durable PREPARED")
        if existing[0] is TellState.PREPARED:
            self._append({"record_type": "tell_state", "trial_id": trial_id, "tell_token": existing[1], "state": TellState.AUTHORIZED.value})
            self._tell_states[trial_id] = (TellState.AUTHORIZED, existing[1], existing[2], None)
        return self._authorization(record, self._tell_states[trial_id][0])

    @staticmethod
    def _optimizer_receipt(record: FigureEightPhysicalRecordV2, value: OptimizerReceiptV2 | Mapping[str, Any]) -> OptimizerReceiptV2:
        receipt = value if isinstance(value, OptimizerReceiptV2) else OptimizerReceiptV2.from_mapping(value)
        if receipt.trial_id != record.trial_id or receipt.tell_token != record.tell_token() or receipt.record_sha256 != record.record_sha256:
            raise V5LifecycleLedgerError("optimizer receipt is not the exact durable tell")
        return receipt

    def reconcile_tell(self, trial_id: str, optimizer_receipt: OptimizerReceiptV2 | Mapping[str, Any] | None = None) -> TellReconciliationV2:
        record = self._record(trial_id)
        existing = self._tell_states.get(trial_id)
        if existing is None:
            raise V5LifecycleLedgerError("tell reconciliation has no durable state")
        state, token, record_sha, durable_receipt = existing
        if state is TellState.COMMITTED:
            if optimizer_receipt is not None and self._optimizer_receipt(record, optimizer_receipt) != durable_receipt:
                raise V5LifecycleLedgerError("committed optimizer receipt differs")
            return TellReconciliationV2(trial_id, token, state, self._head_sha256, None if durable_receipt is None else durable_receipt.optimizer_receipt_id)
        if state is TellState.PREPARED:
            raise V5LifecycleLedgerError("tell is prepared but not authorized")
        if optimizer_receipt is None:
            if state is TellState.OPTIMIZER_RECEIPT and durable_receipt is not None:
                return TellReconciliationV2(trial_id, token, state, self._head_sha256, durable_receipt.optimizer_receipt_id)
            if state is TellState.AUTHORIZED:
                self._append({"record_type": "tell_state", "trial_id": trial_id, "tell_token": token, "state": TellState.AMBIGUOUS.value})
                self._tell_states[trial_id] = (TellState.AMBIGUOUS, token, record_sha, None)
            raise V5AmbiguousTellError("matching optimizer receipt is required; tell will not be repeated")
        receipt = self._optimizer_receipt(record, optimizer_receipt)
        if state in (TellState.AUTHORIZED, TellState.AMBIGUOUS):
            self._append({"record_type": "tell_state", "trial_id": trial_id, "tell_token": token, "state": TellState.OPTIMIZER_RECEIPT.value, "optimizer_receipt": receipt.as_dict()})
            self._tell_states[trial_id] = (TellState.OPTIMIZER_RECEIPT, token, record_sha, receipt)
            state = TellState.OPTIMIZER_RECEIPT
        elif state is TellState.OPTIMIZER_RECEIPT and durable_receipt != receipt:
            raise V5LifecycleLedgerError("optimizer receipt differs from durable receipt")
        return TellReconciliationV2(trial_id, token, state, self._head_sha256, receipt.optimizer_receipt_id)

    def commit_tell(self, trial_id: str, optimizer_receipt: OptimizerReceiptV2 | Mapping[str, Any]) -> TellReconciliationV2:
        record = self._record(trial_id)
        receipt = self._optimizer_receipt(record, optimizer_receipt)
        existing = self._tell_states.get(trial_id)
        if existing is None:
            raise V5LifecycleLedgerError("tell commit has no durable state")
        if existing[0] is TellState.COMMITTED:
            if existing[3] != receipt:
                raise V5LifecycleLedgerError("committed tell receipt differs")
            return TellReconciliationV2(trial_id, existing[1], TellState.COMMITTED, self._head_sha256, receipt.optimizer_receipt_id)
        if existing[0] is not TellState.OPTIMIZER_RECEIPT or existing[3] != receipt:
            raise V5LifecycleLedgerError("tell commit requires matching optimizer receipt")
        self._append({"record_type": "tell_state", "trial_id": trial_id, "tell_token": existing[1], "state": TellState.COMMITTED.value, "optimizer_receipt": receipt.as_dict()})
        self._tell_states[trial_id] = (TellState.COMMITTED, existing[1], existing[2], receipt)
        return TellReconciliationV2(trial_id, existing[1], TellState.COMMITTED, self._head_sha256, receipt.optimizer_receipt_id)


__all__ = [
    "ArtifactBindingV2", "BoundaryMode", "BoundaryV2", "ContactRolloverBoundaryV2", "ControllerCommitAckV2", "EVIDENCE_BIN_COUNT", "FORMAL_BIN_COUNT", "FigureEightPhysicalRecordV2", "GateClosureEvidenceV2", "GateFamiliesV2", "GENESIS_SHA256", "HomeBoundaryV2", "LedgerRole", "LedgerVerificationV2", "LifecycleEventKind", "LifecycleEventV2", "MetricBinV1", "MetricClosureEvidenceV2", "MetricSnapshotV1", "MetricWindow", "OptimizerReceiptV2", "PathTailClosureV2", "PhysicalAdmissionReceiptV2", "SealedR013ArtifactV2", "TellAuthorizationV2", "TellReconciliationV2", "TellState", "TrialBoundaryReceiptV2", "TrialSliceV2", "V5AmbiguousTellError", "V5ChainBindingV2", "V5_EVENT_BUNDLE_SCHEMA", "V5_LEDGER_SCHEMA", "V5_LIFECYCLE_SCHEMA", "V5LifecycleLedgerError", "V5PhysicalAdmissionLedgerV2", "bind_v5_chain", "canonical_sha256", "cold_activation_publication_boundaries", "index_sealed_r013_artifact", "lifecycle_row_evidence_sha256", "physical_record_from_mapping",
]
