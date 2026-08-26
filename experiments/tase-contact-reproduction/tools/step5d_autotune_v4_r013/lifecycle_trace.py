"""Loss-aware, full-attempt force lifecycle recording for R013.

The RTDE/control hot path only packs a fixed-size record into memory and
queues a bytes chunk.  JSON, filesystem metadata, fsync, and cold validation
run after the motion segment (or on the writer thread).  A queue overflow is
never silently repaired: the receipt is marked incomplete and the physical
admission gate must reject that attempt.

This is deliberately a separate artifact from the 10 Hz State20/State25
diagnostic sidecars.  The latter remain useful for human-readable diagnostics;
this artifact is the canonical answer to "from the first move until HOME".
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import struct
import threading
import time
from typing import Any, Iterator, Mapping, Sequence


LIFECYCLE_SCHEMA = "step5d.autotune-v4/r013-force-lifecycle-v1"
LIFECYCLE_RECEIPT_SCHEMA = "step5d.autotune-v4/r013-force-lifecycle-receipt-v1"
LIFECYCLE_MAGIC = b"R013LIFE"
LIFECYCLE_VERSION = 1
LIFECYCLE_RECEIPT_SUFFIX = ".r013life.json"
LIFECYCLE_DATA_SUFFIX = ".r013life"
LIFECYCLE_PART_SUFFIX = ".r013life.part"
DEFAULT_PATH_MIN_DURATION_S = 59.5
LIFECYCLE_COVERAGE_PROFILE_R013_BASELINE = "r013_baseline_v1"
LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH = "v5_immediate_path_v1"
_LIFECYCLE_COVERAGE_PROFILES = frozenset(
    {
        LIFECYCLE_COVERAGE_PROFILE_R013_BASELINE,
        LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
    }
)

# ARM/dispatch, TP-owned contact search, baseline, 60 s path, return, HOME,
# and stopped are the states needed to explain the complete force trajectory.
PHASE_CODES: dict[int, int] = {
    11: 11,
    20: 20,
    21: 21,
    25: 25,
    40: 40,
    78: 78,
    80: 80,
    90: 90,
}
PHASE_NAMES: dict[int, str] = {
    11: "ARM_ENTRY",
    20: "CONTACT_SEARCH",
    21: "BASELINE",
    25: "PATH",
    40: "RETURN",
    78: "HOME",
    80: "COMPLETE",
    90: "STOPPED",
}

FLAG_SENSOR_PRESENT = 1 << 0
FLAG_SENSOR_FRESH = 1 << 1
FLAG_OUTPUT_PRESENT = 1 << 2
FLAG_COMMAND_PRESENT = 1 << 3
FLAG_TERMINAL = 1 << 4
FLAG_INVALID = 1 << 5

_HEADER = struct.Struct("<8sIII")
# sample, monotonic, RTDE timestamp, observation timestamp, state, phase,
# command mode, flags, packet sequence, consumed sequence, four force scalar
# fields, six wrench fields, pose, TCP speed, qd, and proposed qdot.
_RECORD = struct.Struct("<QdddihhIqq" + "d" * (4 + 6 + 6 + 6 + 6 + 6 + 1))
RECORD_SIZE = _RECORD.size
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class LifecycleTraceError(RuntimeError):
    """The lifecycle artifact could not be created or cold-verified."""


def _finite(value: Any, default: float = math.nan) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def _vector(value: Any, size: int = 6) -> tuple[float, ...]:
    if isinstance(value, (tuple, list)) and len(value) == size:
        return tuple(_finite(item) for item in value)
    return (math.nan,) * size


def _safe_name(value: str) -> str:
    result = _SAFE_NAME.sub("_", str(value)).strip("._")
    return result or "attempt"


def _phase_code(tp_state: int | None) -> int:
    try:
        state = int(tp_state)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0
    return PHASE_CODES.get(state, 0)


def _phase_name(code: int) -> str:
    return PHASE_NAMES.get(int(code), "UNKNOWN")


def _required_phases(path_requested: bool, coverage_profile: str) -> set[int]:
    phases = {11, 20, 21, 40, 78}
    if coverage_profile == LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH:
        # V5 intentionally transitions from the fresh stable 1 N latch to
        # PATH on the same controller tick.  State21 may consequently have no
        # observable RTDE frame; requiring it would reintroduce the legacy
        # stationary release phase that V5 explicitly removed.
        phases.remove(21)
    if path_requested:
        phases.add(25)
    return phases


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


@dataclass
class _Attempt:
    ordinal: int
    execution_id: str
    kind: str
    epoch: int | None
    path_requested: bool
    part_path: Path
    data_path: Path
    receipt_path: Path
    file: Any
    queue: queue.Queue[bytes | None]
    thread: threading.Thread
    sample_count: int = 0
    dropped_chunks: int = 0
    write_errors: int = 0
    invalid_rows: int = 0
    first_monotonic_s: float | None = None
    last_monotonic_s: float | None = None
    phases: set[int] | None = None
    terminal_state: int | None = None
    home_verified: bool = False
    terminal_sensor_present: bool = False
    last_sample_index: int = -1
    writer_error: str | None = None
    capture_started_before_motion: bool = True
    path_first_monotonic_s: float | None = None
    path_last_monotonic_s: float | None = None
    path_sample_count: int = 0
    missing_force_rows: int = 0
    missing_pose_rows: int = 0
    phase_sample_counts: dict[int, int] | None = None
    observer_errors: int = 0
    path_end_requested: bool = False
    path_end_request_monotonic_s: float | None = None

    def __post_init__(self) -> None:
        if self.phases is None:
            self.phases = set()
        if self.phase_sample_counts is None:
            self.phase_sample_counts = {}


def _write_chunks(attempt: _Attempt) -> None:
    """Write queued chunks without touching control-thread state."""

    while True:
        chunk = attempt.queue.get()
        try:
            if chunk is None:
                return
            try:
                attempt.file.write(chunk)
            except OSError as exc:
                attempt.write_errors += 1
                attempt.writer_error = str(exc)
        finally:
            attempt.queue.task_done()


def _cold_validate_artifact(
    path: Path,
    *,
    expected_sample_count: int,
) -> dict[str, Any]:
    """Validate one immutable artifact after the motion writer has stopped.

    This deliberately does not run from ``observe_tick``.  The hot path only
    records fixed-size rows; all temporal and packet-continuity accounting is
    performed once, after fsync, so evidence validation cannot reduce the
    control-loop cadence.
    """

    base: dict[str, Any] = {
        "artifact_row_count": 0,
        "row_count_matches": False,
        "sample_index_gap_count": 0,
        "max_gap_s": 0.0,
        "max_nonterminal_gap_s": 0.0,
        "max_rtde_gap_s": 0.0,
        "terminal_observation_gap_s": None,
        "terminal_rtde_gap_s": None,
        "packet_sequence_gap_count": 0,
        "packet_sequence_regression_count": 0,
        "errors": [],
    }
    try:
        rows = list(iter_lifecycle_rows(path))
    except (OSError, LifecycleTraceError) as exc:
        base["errors"] = [f"cold lifecycle artifact read failed: {exc}"]
        return base

    errors: list[str] = []
    base["artifact_row_count"] = len(rows)
    base["row_count_matches"] = len(rows) == int(expected_sample_count)
    if not base["row_count_matches"]:
        errors.append(
            "lifecycle artifact row count differs: "
            f"expected={int(expected_sample_count)} observed={len(rows)}"
        )

    terminal_indices = [
        index
        for index, row in enumerate(rows)
        if int(row.get("flags", 0)) & FLAG_TERMINAL
    ]
    if len(terminal_indices) > 1:
        errors.append("lifecycle artifact contains multiple terminal rows")
    if terminal_indices and terminal_indices[-1] != len(rows) - 1:
        errors.append("lifecycle terminal row is not the final artifact row")

    previous_monotonic: float | None = None
    previous_rtde: float | None = None
    for index, row in enumerate(rows):
        if int(row.get("sample_index", -1)) != index:
            base["sample_index_gap_count"] += 1
        if int(row.get("flags", 0)) & FLAG_INVALID:
            errors.append("lifecycle artifact contains invalid rows")
            break

    packet_sequences: list[int] = []
    for row in rows:
        flags = int(row.get("flags", 0))
        monotonic = _finite(row.get("monotonic_s"))
        rtde = _finite(row.get("rtde_timestamp_s"))
        if not math.isfinite(monotonic) or not math.isfinite(rtde):
            errors.append("lifecycle artifact contains nonfinite timestamps")
            continue
        if previous_monotonic is not None:
            host_gap = monotonic - previous_monotonic
            if not math.isfinite(host_gap) or host_gap < 0.0:
                errors.append("lifecycle artifact monotonic timestamps regress")
            else:
                base["max_gap_s"] = max(float(base["max_gap_s"]), host_gap)
                if not flags & FLAG_TERMINAL:
                    base["max_nonterminal_gap_s"] = max(
                        float(base["max_nonterminal_gap_s"]), host_gap
                    )
        if previous_rtde is not None:
            rtde_gap = rtde - previous_rtde
            if not math.isfinite(rtde_gap) or rtde_gap < 0.0:
                errors.append("lifecycle artifact RTDE timestamps regress")
            elif flags & FLAG_TERMINAL:
                base["terminal_rtde_gap_s"] = rtde_gap
            else:
                base["max_rtde_gap_s"] = max(float(base["max_rtde_gap_s"]), rtde_gap)
        if flags & FLAG_TERMINAL and previous_monotonic is not None:
            base["terminal_observation_gap_s"] = monotonic - previous_monotonic
        if not flags & FLAG_TERMINAL and int(row.get("packet_sequence", -1)) >= 0:
            packet_sequences.append(int(row["packet_sequence"]))
        previous_monotonic = monotonic
        previous_rtde = rtde

    for previous, current in zip(packet_sequences, packet_sequences[1:]):
        delta = current - previous
        if delta < 0:
            base["packet_sequence_regression_count"] += 1
        elif delta > 1:
            base["packet_sequence_gap_count"] += delta - 1

    if base["sample_index_gap_count"]:
        errors.append(
            "lifecycle artifact sample indices are not contiguous: "
            f"{base['sample_index_gap_count']}"
        )
    if base["packet_sequence_gap_count"]:
        errors.append(
            "lifecycle artifact packet sequence gaps: "
            f"{base['packet_sequence_gap_count']}"
        )
    if base["packet_sequence_regression_count"]:
        errors.append("lifecycle artifact packet sequence regression")
    base["errors"] = list(dict.fromkeys(errors))
    return base


class LifecycleTrace:
    """One sequential, loss-aware lifecycle artifact per physical attempt."""

    def __init__(
        self,
        run_dir: Path,
        *,
        chunk_records: int = 4096,
        max_queue_chunks: int = 0,
        max_gap_s: float = 0.010,
        path_min_duration_s: float = DEFAULT_PATH_MIN_DURATION_S,
        coverage_profile: str = LIFECYCLE_COVERAGE_PROFILE_R013_BASELINE,
        clock: Any = time.monotonic,
    ) -> None:
        if (
            chunk_records <= 0
            or max_queue_chunks < 0
            or max_gap_s <= 0.0
            or path_min_duration_s <= 0.0
        ):
            raise ValueError("invalid lifecycle trace bounds")
        if coverage_profile not in _LIFECYCLE_COVERAGE_PROFILES:
            raise ValueError("invalid lifecycle coverage profile")
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.chunk_records = int(chunk_records)
        self.max_queue_chunks = int(max_queue_chunks)
        self.max_gap_s = float(max_gap_s)
        self.path_min_duration_s = float(path_min_duration_s)
        self.coverage_profile = str(coverage_profile)
        self.clock = clock
        self._active: _Attempt | None = None
        self._buffer = bytearray()
        self._closed = False
        self._lock = threading.Lock()
        self.receipts: list[dict[str, Any]] = []

    @property
    def active(self) -> bool:
        return self._active is not None

    @property
    def active_execution_id(self) -> str | None:
        return None if self._active is None else self._active.execution_id

    @property
    def active_data_path(self) -> Path | None:
        """Path reserved for the current attempt before it is sealed."""

        return None if self._active is None else self._active.data_path

    def begin_attempt(
        self,
        ordinal: int,
        execution_id: str,
        kind: str,
        *,
        epoch: int | None = None,
        path_requested: bool = True,
        capture_started_before_motion: bool = True,
    ) -> None:
        if self._closed:
            raise LifecycleTraceError("lifecycle trace is closed")
        if self._active is not None:
            raise LifecycleTraceError("previous lifecycle attempt is not finalized")
        if int(ordinal) <= 0 or not str(execution_id) or not str(kind):
            raise LifecycleTraceError("lifecycle attempt identity is incomplete")
        stem = f"{int(ordinal):06d}-{_safe_name(str(execution_id))}"
        part_path = self.run_dir / f"{stem}{LIFECYCLE_PART_SUFFIX}"
        data_path = self.run_dir / f"{stem}{LIFECYCLE_DATA_SUFFIX}"
        receipt_path = self.run_dir / f"{stem}{LIFECYCLE_RECEIPT_SUFFIX}"
        metadata = {
            "schema": LIFECYCLE_SCHEMA,
            "version": LIFECYCLE_VERSION,
            "ordinal": int(ordinal),
            "execution_id": str(execution_id),
            "kind": str(kind),
            "epoch": None if epoch is None else int(epoch),
            "path_requested": bool(path_requested),
            "capture_started_before_motion": bool(capture_started_before_motion),
            "coverage_profile": self.coverage_profile,
            "record_size": RECORD_SIZE,
            "started_monotonic_s": _finite(self.clock()),
        }
        try:
            file = part_path.open("wb")
            encoded = json.dumps(_jsonable(metadata), sort_keys=True, separators=(",", ":")).encode()
            file.write(_HEADER.pack(LIFECYCLE_MAGIC, LIFECYCLE_VERSION, RECORD_SIZE, len(encoded)))
            file.write(encoded)
            file.flush()
        except OSError as exc:
            raise LifecycleTraceError(f"cannot open lifecycle artifact: {part_path}") from exc
        q: queue.Queue[bytes | None]
        q = queue.Queue(maxsize=self.max_queue_chunks)
        attempt = _Attempt(
            ordinal=int(ordinal),
            execution_id=str(execution_id),
            kind=str(kind),
            epoch=None if epoch is None else int(epoch),
            path_requested=bool(path_requested),
            part_path=part_path,
            data_path=data_path,
            receipt_path=receipt_path,
            file=file,
            queue=q,
            thread=threading.Thread(target=lambda: _write_chunks(attempt), daemon=True),
            capture_started_before_motion=bool(capture_started_before_motion),
        )
        # The lambda closes over the object after construction; start only
        # after _active is set so the worker cannot observe a half-built state.
        self._active = attempt
        self._buffer.clear()
        attempt.thread.start()

    def _enqueue_chunk(self) -> None:
        attempt = self._active
        if attempt is None or not self._buffer:
            return
        chunk = bytes(self._buffer)
        self._buffer.clear()
        try:
            attempt.queue.put_nowait(chunk)
        except queue.Full:
            # Never evict an older chunk and never pretend the trajectory is
            # complete.  The owner will fail closed from this receipt.
            attempt.dropped_chunks += 1

    def _pack_row(
        self,
        *,
        monotonic_s: float,
        rtde_timestamp_s: float,
        observed_at_s: float,
        tp_state: int | None,
        command_mode: int | None,
        flags: int,
        packet_sequence: int,
        consumed_packet_sequence: int,
        normal_load_n: float,
        force_norm_n: float,
        filtered_normal_n: float,
        torque_norm_nm: float,
        wrench: Sequence[float],
        pose: Sequence[float],
        tcp_speed: Sequence[float],
        qd: Sequence[float],
        qdot: Sequence[float],
        setpoint_n: float,
    ) -> bytes:
        attempt = self._active
        if attempt is None:
            raise LifecycleTraceError("no active lifecycle attempt")
        state = int(tp_state) if tp_state is not None else -1
        phase = _phase_code(tp_state)
        mode = int(command_mode) if command_mode is not None else -1
        values = (
            attempt.sample_count,
            _finite(monotonic_s),
            _finite(rtde_timestamp_s),
            _finite(observed_at_s),
            state,
            phase,
            mode,
            int(flags),
            int(packet_sequence),
            int(consumed_packet_sequence),
            _finite(normal_load_n),
            _finite(force_norm_n),
            _finite(filtered_normal_n),
            _finite(torque_norm_nm),
            *_vector(wrench),
            *_vector(pose),
            *_vector(tcp_speed),
            *_vector(qd),
            *_vector(qdot),
            _finite(setpoint_n),
        )
        return _RECORD.pack(*values)

    def observe_tick(
        self,
        *,
        monotonic_s: float,
        output: Any | None,
        sensor: Any | None,
        tp_state: int | None,
        command_mode: int | None = None,
        qdot: Sequence[float] = (0.0,) * 6,
        setpoint_n: float = math.nan,
        packet_sequence: int = -1,
        consumed_packet_sequence: int = -1,
        terminal: bool = False,
        path_end_requested: bool = False,
    ) -> bool:
        """Record one control observation; return False only when inactive."""

        attempt = self._active
        if attempt is None:
            return False
        if not isinstance(path_end_requested, bool):
            raise LifecycleTraceError("path-end request marker is not boolean")
        if path_end_requested and not attempt.path_end_requested:
            attempt.path_end_requested = True
            attempt.path_end_request_monotonic_s = float(monotonic_s)
        flags = 0
        if output is not None:
            flags |= FLAG_OUTPUT_PRESENT
        if sensor is not None:
            flags |= FLAG_SENSOR_PRESENT
            if bool(getattr(sensor, "sensor_fresh", False)):
                flags |= FLAG_SENSOR_FRESH
        if command_mode is not None:
            flags |= FLAG_COMMAND_PRESENT
        if terminal:
            flags |= FLAG_TERMINAL
        state = None if tp_state is None else int(tp_state)
        rtde_timestamp_s = getattr(output, "timestamp", math.nan)
        observed_at_s = getattr(output, "observed_at_s", monotonic_s)
        row_flags = flags
        values_to_check = [monotonic_s, rtde_timestamp_s, observed_at_s]
        if not all(math.isfinite(_finite(value)) for value in values_to_check):
            row_flags |= FLAG_INVALID
        if output is None and sensor is None:
            row_flags |= FLAG_INVALID
        current = _finite(monotonic_s)
        required_state = state in {11, 20, 21, 25, 40, 78}
        force_values = (
            getattr(sensor, "normal_load_n", math.nan),
            getattr(sensor, "force_norm_n", math.nan),
            getattr(sensor, "filtered_normal_n", math.nan),
        )
        pose_values = (
            *_vector(getattr(output, "tcp_pose_m_rad", None)),
            *_vector(getattr(output, "tcp_speed_m_s_rad_s", None)),
        )
        if required_state and not all(math.isfinite(_finite(value)) for value in force_values):
            attempt.missing_force_rows += 1
            row_flags |= FLAG_INVALID
        if required_state and not all(math.isfinite(_finite(value)) for value in pose_values):
            attempt.missing_pose_rows += 1
            row_flags |= FLAG_INVALID
        # A normal R013 PATH-end request is sent on the final live PATH tick.
        # The TP keeps reporting its previous state=25 while stopj()/return
        # executes, so those stale rows must not extend the measured PATH
        # span or masquerade as additional force bins.
        if state == 25 and not attempt.path_end_requested:
            if attempt.path_first_monotonic_s is None:
                attempt.path_first_monotonic_s = current
            attempt.path_last_monotonic_s = current
            attempt.path_sample_count += 1
        if row_flags & FLAG_INVALID:
            attempt.invalid_rows += 1
        try:
            raw = self._pack_row(
                monotonic_s=monotonic_s,
                rtde_timestamp_s=rtde_timestamp_s,
                observed_at_s=observed_at_s,
                tp_state=state,
                command_mode=command_mode,
                flags=row_flags,
                packet_sequence=packet_sequence,
                consumed_packet_sequence=consumed_packet_sequence,
                normal_load_n=getattr(sensor, "normal_load_n", math.nan),
                force_norm_n=getattr(sensor, "force_norm_n", math.nan),
                filtered_normal_n=getattr(sensor, "filtered_normal_n", math.nan),
                torque_norm_nm=getattr(sensor, "torque_norm_nm", math.nan),
                wrench=getattr(sensor, "wrench", None),
                pose=getattr(output, "tcp_pose_m_rad", None),
                tcp_speed=getattr(output, "tcp_speed_m_s_rad_s", None),
                qd=getattr(output, "qd_rad_s", None),
                qdot=qdot,
                setpoint_n=setpoint_n,
            )
        except (struct.error, TypeError, ValueError, OverflowError) as exc:
            attempt.invalid_rows += 1
            attempt.writer_error = str(exc)
            return False
        if attempt.first_monotonic_s is None:
            attempt.first_monotonic_s = current
        attempt.last_monotonic_s = current
        if state in PHASE_CODES:
            assert attempt.phases is not None
            attempt.phases.add(state)
            assert attempt.phase_sample_counts is not None
            attempt.phase_sample_counts[state] = (
                attempt.phase_sample_counts.get(state, 0) + 1
            )
        attempt.last_sample_index = attempt.sample_count
        attempt.sample_count += 1
        self._buffer.extend(raw)
        if len(self._buffer) >= self.chunk_records * RECORD_SIZE:
            self._enqueue_chunk()
        return True

    def observe_terminal(
        self,
        *,
        monotonic_s: float | None = None,
        output: Any | None,
        sensor: Any | None = None,
        tp_state: int,
        command_mode: int | None = None,
        packet_sequence: int = -1,
        consumed_packet_sequence: int = -1,
    ) -> bool:
        """Append the terminal TP observation, including state 78/90."""

        attempt = self._active
        if attempt is None:
            return False
        mono = self.clock() if monotonic_s is None else monotonic_s
        recorded = self.observe_tick(
            monotonic_s=float(mono),
            output=output,
            sensor=sensor,
            tp_state=int(tp_state),
            command_mode=command_mode,
            packet_sequence=packet_sequence,
            consumed_packet_sequence=consumed_packet_sequence,
            terminal=True,
        )
        attempt.terminal_sensor_present = sensor is not None
        attempt.terminal_state = int(tp_state)
        attempt.home_verified = int(tp_state) == 78
        return recorded

    def mark_observer_error(self, error: Any) -> None:
        """Retain a control-thread observer failure for the fail-closed gate."""

        attempt = self._active
        if attempt is None:
            return
        attempt.observer_errors += 1
        attempt.invalid_rows += 1
        if not attempt.writer_error:
            attempt.writer_error = f"lifecycle observer: {error}"

    def finalize_attempt(
        self,
        *,
        terminal_state: int | None = None,
        home_verified: bool | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        attempt = self._active
        if attempt is None:
            raise LifecycleTraceError("no active lifecycle attempt to finalize")
        if terminal_state is not None:
            attempt.terminal_state = int(terminal_state)
        if home_verified is not None:
            attempt.home_verified = bool(home_verified)
        if error:
            attempt.writer_error = str(error)
        self._enqueue_chunk()
        # The sentinel is ordered after every successfully enqueued chunk.
        try:
            attempt.queue.put(None)
        except (OSError, RuntimeError):
            attempt.write_errors += 1
            attempt.writer_error = attempt.writer_error or "lifecycle sentinel enqueue failed"
        attempt.queue.join()
        attempt.thread.join(timeout=5.0)
        if attempt.thread.is_alive():
            attempt.write_errors += 1
            attempt.writer_error = attempt.writer_error or "lifecycle writer thread did not join"
        try:
            attempt.file.flush()
            os.fsync(attempt.file.fileno())
            attempt.file.close()
        except OSError as exc:
            attempt.write_errors += 1
            attempt.writer_error = attempt.writer_error or str(exc)
            try:
                attempt.file.close()
            except OSError:
                pass
        try:
            os.replace(attempt.part_path, attempt.data_path)
        except OSError as exc:
            attempt.write_errors += 1
            attempt.writer_error = attempt.writer_error or str(exc)
        receipt = self._build_receipt(attempt)
        try:
            temporary_receipt = attempt.receipt_path.with_suffix(attempt.receipt_path.suffix + ".part")
            temporary_receipt.write_text(
                json.dumps(_jsonable(receipt), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary_receipt, attempt.receipt_path)
        except OSError as exc:
            receipt["status"] = "incomplete"
            receipt["errors"] = [*(receipt.get("errors") or []), f"receipt_write: {exc}"]
        self._active = None
        self._buffer.clear()
        self.receipts.append(receipt)
        return receipt


    def _build_receipt(self, attempt: _Attempt) -> dict[str, Any]:
        cold = _cold_validate_artifact(
            attempt.data_path,
            expected_sample_count=attempt.sample_count,
        )
        phases = sorted(attempt.phases or set())
        required = sorted(
            _required_phases(attempt.path_requested, self.coverage_profile)
        )
        missing = sorted(set(required) - set(phases))
        terminal_ok = attempt.terminal_state == 78 and attempt.home_verified
        path_duration = (
            None
            if attempt.path_first_monotonic_s is None or attempt.path_last_monotonic_s is None
            else attempt.path_last_monotonic_s - attempt.path_first_monotonic_s
        )
        path_ok = bool(
            not attempt.path_requested
            or (
                attempt.path_sample_count > 0
                and path_duration is not None
                and path_duration >= self.path_min_duration_s
            )
        )
        complete = bool(
            attempt.sample_count > 0
            and not missing
            and terminal_ok
            and attempt.terminal_sensor_present
            and attempt.dropped_chunks == 0
            and attempt.write_errors == 0
            and attempt.invalid_rows == 0
            and attempt.observer_errors == 0
            and attempt.capture_started_before_motion
            and attempt.missing_force_rows == 0
            and attempt.missing_pose_rows == 0
            and attempt.first_monotonic_s is not None
            and attempt.last_monotonic_s is not None
            and cold["row_count_matches"]
            and cold["sample_index_gap_count"] == 0
            and cold["terminal_rtde_gap_s"] is not None
            and cold["terminal_rtde_gap_s"] <= self.max_gap_s
            and cold["packet_sequence_gap_count"] == 0
            and cold["packet_sequence_regression_count"] == 0
            and not cold["errors"]
            and path_ok
        )
        digest = None
        try:
            digest = hashlib.sha256(attempt.data_path.read_bytes()).hexdigest()
        except OSError:
            pass
        errors = []
        if attempt.writer_error:
            errors.append(attempt.writer_error)
        if attempt.invalid_rows:
            errors.append("invalid lifecycle rows")
        if attempt.observer_errors:
            errors.append("lifecycle observer errors")
        if not attempt.capture_started_before_motion:
            errors.append("lifecycle capture did not start before motion")
        if attempt.missing_force_rows:
            errors.append("lifecycle rows missing force scalars")
        if attempt.missing_pose_rows:
            errors.append("lifecycle rows missing pose or TCP speed")
        if attempt.dropped_chunks:
            errors.append("lifecycle queue overflow")
        errors.extend(cold["errors"])
        if cold["terminal_rtde_gap_s"] is None:
            errors.append("terminal RTDE timestamp gap is missing")
        elif cold["terminal_rtde_gap_s"] > self.max_gap_s:
            errors.append(
                "terminal RTDE gap "
                f"{cold['terminal_rtde_gap_s']:.6f}s exceeds {self.max_gap_s:.6f}s"
            )
        if cold["packet_sequence_gap_count"]:
            errors.append(
                "lifecycle packet sequence gaps: "
                f"{cold['packet_sequence_gap_count']}"
            )
        if cold["packet_sequence_regression_count"]:
            errors.append("lifecycle packet sequence regression")
        if missing:
            errors.append("missing phases: " + ", ".join(_phase_name(code) for code in missing))
        if not terminal_ok:
            errors.append("terminal HOME state 78 was not verified")
        if not attempt.terminal_sensor_present:
            errors.append("terminal Home force sensor snapshot is missing")
        if attempt.path_requested and (
            attempt.path_sample_count <= 0
            or path_duration is None
            or path_duration < self.path_min_duration_s
        ):
            errors.append(
                "PATH coverage is shorter than the required "
                f"{self.path_min_duration_s:.3f}s"
            )
        warnings = []
        if cold["max_gap_s"] > self.max_gap_s:
            warnings.append(
                "host observation gap is diagnostic-only: "
                f"{cold['max_gap_s']:.6f}s exceeds {self.max_gap_s:.6f}s"
            )
        if cold["max_rtde_gap_s"] > self.max_gap_s:
            warnings.append(
                "non-terminal RTDE timestamp gap is diagnostic-only: "
                f"{cold['max_rtde_gap_s']:.6f}s exceeds {self.max_gap_s:.6f}s"
            )
        if (
            cold["terminal_observation_gap_s"] is not None
            and cold["terminal_observation_gap_s"] > self.max_gap_s
        ):
            warnings.append(
                "terminal host observation gap is diagnostic-only: "
                f"{cold['terminal_observation_gap_s']:.6f}s exceeds {self.max_gap_s:.6f}s"
            )
        return {
            "schema": LIFECYCLE_RECEIPT_SCHEMA,
            "status": "complete" if complete else "incomplete",
            "ordinal": attempt.ordinal,
            "execution_id": attempt.execution_id,
            "kind": attempt.kind,
            "epoch": attempt.epoch,
            "path_requested": attempt.path_requested,
            "coverage_profile": self.coverage_profile,
            "artifact_path": str(attempt.data_path),
            "receipt_path": str(attempt.receipt_path),
            "artifact_sha256": digest,
            "record_size": RECORD_SIZE,
            "sample_count": attempt.sample_count,
            "first_monotonic_s": attempt.first_monotonic_s,
            "last_monotonic_s": attempt.last_monotonic_s,
            "duration_s": (
                None
                if attempt.first_monotonic_s is None or attempt.last_monotonic_s is None
                else attempt.last_monotonic_s - attempt.first_monotonic_s
            ),
            "max_gap_s": cold["max_gap_s"],
            "max_nonterminal_gap_s": cold["max_nonterminal_gap_s"],
            "max_gap_limit_s": self.max_gap_s,
            "max_rtde_gap_s": cold["max_rtde_gap_s"],
            "terminal_observation_gap_s": cold["terminal_observation_gap_s"],
            "terminal_rtde_gap_s": cold["terminal_rtde_gap_s"],
            "packet_sequence_gap_count": cold["packet_sequence_gap_count"],
            "packet_sequence_regression_count": cold["packet_sequence_regression_count"],
            "artifact_row_count": cold["artifact_row_count"],
            "artifact_sample_index_gap_count": cold["sample_index_gap_count"],
            "path_min_duration_s": self.path_min_duration_s,
            "path_first_monotonic_s": attempt.path_first_monotonic_s,
            "path_last_monotonic_s": attempt.path_last_monotonic_s,
            "path_duration_s": path_duration,
            "path_sample_count": attempt.path_sample_count,
            "path_end_requested": attempt.path_end_requested,
            "path_end_request_monotonic_s": attempt.path_end_request_monotonic_s,
            "path_coverage_complete": path_ok,
            "capture_started_before_motion": attempt.capture_started_before_motion,
            "missing_force_rows": attempt.missing_force_rows,
            "missing_pose_rows": attempt.missing_pose_rows,
            "dropped_chunks": attempt.dropped_chunks,
            "write_errors": attempt.write_errors,
            "invalid_rows": attempt.invalid_rows,
            "observer_errors": attempt.observer_errors,
            "phases": [{"code": code, "name": _phase_name(code)} for code in phases],
            "phase_sample_counts": [
                {
                    "code": code,
                    "name": _phase_name(code),
                    "sample_count": int((attempt.phase_sample_counts or {}).get(code, 0)),
                }
                for code in sorted((attempt.phase_sample_counts or {}).keys())
            ],
            "required_phases": [{"code": code, "name": _phase_name(code)} for code in required],
            "missing_phases": [{"code": code, "name": _phase_name(code)} for code in missing],
            "terminal_state": attempt.terminal_state,
            "home_verified": attempt.home_verified,
            "terminal_sensor_present": attempt.terminal_sensor_present,
            "coverage_complete": complete,
            "warnings": warnings,
            "errors": errors,
        }

    def close(self) -> list[dict[str, Any]]:
        if self._closed:
            return list(self.receipts)
        if self._active is not None:
            self.finalize_attempt(error="trace closed before terminal HOME")
        self._closed = True
        return list(self.receipts)


def attach_lifecycle_trace(
    writer: Any,
    run_dir: Path,
    *,
    chunk_records: int = 4096,
    max_queue_chunks: int = 0,
    max_gap_s: float = 0.010,
    path_min_duration_s: float = DEFAULT_PATH_MIN_DURATION_S,
    coverage_profile: str = LIFECYCLE_COVERAGE_PROFILE_R013_BASELINE,
) -> LifecycleTrace:
    trace = LifecycleTrace(
        run_dir,
        chunk_records=chunk_records,
        max_queue_chunks=max_queue_chunks,
        max_gap_s=max_gap_s,
        path_min_duration_s=path_min_duration_s,
        coverage_profile=coverage_profile,
    )
    setattr(writer, "_r013_lifecycle_trace", trace)
    return trace


def _read_header(file: Any) -> dict[str, Any]:
    header = file.read(_HEADER.size)
    if len(header) != _HEADER.size:
        raise LifecycleTraceError("lifecycle artifact header is truncated")
    magic, version, record_size, metadata_size = _HEADER.unpack(header)
    if magic != LIFECYCLE_MAGIC or version != LIFECYCLE_VERSION or record_size != RECORD_SIZE:
        raise LifecycleTraceError("lifecycle artifact header identity differs")
    raw_metadata = file.read(metadata_size)
    if len(raw_metadata) != metadata_size:
        raise LifecycleTraceError("lifecycle artifact metadata is truncated")
    try:
        metadata = json.loads(raw_metadata.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleTraceError("lifecycle artifact metadata is invalid") from exc
    if not isinstance(metadata, dict) or metadata.get("schema") != LIFECYCLE_SCHEMA:
        raise LifecycleTraceError("lifecycle artifact schema differs")
    return metadata


def iter_lifecycle_rows(path: Path) -> Iterator[dict[str, Any]]:
    """Cold-read rows without inventing points across missing intervals."""

    with Path(path).open("rb") as file:
        metadata = _read_header(file)
        while True:
            raw = file.read(RECORD_SIZE)
            if not raw:
                break
            if len(raw) != RECORD_SIZE:
                raise LifecycleTraceError("lifecycle record is truncated")
            values = _RECORD.unpack(raw)
            (
                sample_index,
                monotonic_s,
                rtde_timestamp_s,
                observed_at_s,
                tp_state,
                phase,
                command_mode,
                flags,
                packet_sequence,
                consumed_packet_sequence,
                normal_load_n,
                force_norm_n,
                filtered_normal_n,
                torque_norm_nm,
                *vectors,
            ) = values
            row = {
                "sample_index": int(sample_index),
                "monotonic_s": monotonic_s,
                "rtde_timestamp_s": rtde_timestamp_s,
                "observed_at_s": observed_at_s,
                "tp_state": None if tp_state < 0 else int(tp_state),
                "phase_code": int(phase),
                "phase": _phase_name(phase),
                "command_mode": None if command_mode < 0 else int(command_mode),
                "flags": int(flags),
                "packet_sequence": int(packet_sequence),
                "consumed_packet_sequence": int(consumed_packet_sequence),
                "normal_load_n": normal_load_n,
                "force_norm_n": force_norm_n,
                "filtered_normal_n": filtered_normal_n,
                "torque_norm_nm": torque_norm_nm,
                "wrench": tuple(vectors[0:6]),
                "pose": tuple(vectors[6:12]),
                "tcp_speed": tuple(vectors[12:18]),
                "qd": tuple(vectors[18:24]),
                "qdot": tuple(vectors[24:30]),
                "setpoint_n": vectors[30],
            }
            yield row


def load_lifecycle_artifact(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with Path(path).open("rb") as file:
        metadata = _read_header(file)
        rows: list[dict[str, Any]] = []
        while True:
            raw = file.read(RECORD_SIZE)
            if not raw:
                break
            if len(raw) != RECORD_SIZE:
                raise LifecycleTraceError("lifecycle record is truncated")
            values = _RECORD.unpack(raw)
            (
                sample_index,
                monotonic_s,
                rtde_timestamp_s,
                observed_at_s,
                tp_state,
                phase,
                command_mode,
                flags,
                packet_sequence,
                consumed_packet_sequence,
                normal_load_n,
                force_norm_n,
                filtered_normal_n,
                torque_norm_nm,
                *vectors,
            ) = values
            rows.append(
                {
                    "sample_index": int(sample_index),
                    "monotonic_s": monotonic_s,
                    "rtde_timestamp_s": rtde_timestamp_s,
                    "observed_at_s": observed_at_s,
                    "tp_state": None if tp_state < 0 else int(tp_state),
                    "phase_code": int(phase),
                    "phase": _phase_name(phase),
                    "command_mode": None if command_mode < 0 else int(command_mode),
                    "flags": int(flags),
                    "packet_sequence": int(packet_sequence),
                    "consumed_packet_sequence": int(consumed_packet_sequence),
                    "normal_load_n": normal_load_n,
                    "force_norm_n": force_norm_n,
                    "filtered_normal_n": filtered_normal_n,
                    "torque_norm_nm": torque_norm_nm,
                    "wrench": tuple(vectors[0:6]),
                    "pose": tuple(vectors[6:12]),
                    "tcp_speed": tuple(vectors[12:18]),
                    "qd": tuple(vectors[18:24]),
                    "qdot": tuple(vectors[24:30]),
                    "setpoint_n": vectors[30],
                }
            )
    return metadata, rows


__all__ = [
    "FLAG_COMMAND_PRESENT",
    "FLAG_INVALID",
    "FLAG_OUTPUT_PRESENT",
    "FLAG_SENSOR_FRESH",
    "FLAG_SENSOR_PRESENT",
    "FLAG_TERMINAL",
    "LIFECYCLE_DATA_SUFFIX",
    "LIFECYCLE_RECEIPT_SCHEMA",
    "LIFECYCLE_RECEIPT_SUFFIX",
    "LIFECYCLE_SCHEMA",
    "LifecycleTrace",
    "LifecycleTraceError",
    "PHASE_CODES",
    "PHASE_NAMES",
    "RECORD_SIZE",
    "attach_lifecycle_trace",
    "iter_lifecycle_rows",
    "load_lifecycle_artifact",
]
