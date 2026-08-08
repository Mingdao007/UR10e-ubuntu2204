"""r008 state-25 / PATH force+integral sidecar (hard-stop reconstructability).

Observation-only: record per-tick TCP Z, wrench/force, and the live outer-loop
integrator state while TP is in PATH (state 25).  Does not change control,
safety envelope, or reason-61 thresholds.  Exists so a PATH hard-stop leaves a
force/integral trail that open-loop seal artefacts cannot provide.
"""

from __future__ import annotations

import json
import math
import queue
import threading
from collections import deque
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

STATE25_PATH_TRACE_SCHEMA = "step5d.autotune-v4/r008-state25-path-trace-v1"
STATE25_PATH_TRACE_SIDECAR_NAME = "r008-state25-path-trace.jsonl"
STATE25_STOP_DOMINANT_SCHEMA = "step5d.autotune-v4/r008-state25-stop-dominant-v1"
STATE25_STOP_DOMINANT_NAME = "r008-state25-stop-dominant.json"
STATE25_RING_DEFAULT = 256
# Bound async disk backlog so a slow filesystem cannot stall the 500 Hz loop.
# Live 170124: per-tick open/append/close on a 337 MB sidecar caused ~129 ms
# host monotonic gaps (qualification dt gate) while RTDE stayed at 2 ms.
STATE25_WRITE_QUEUE_MAX = 4096
_STATE25_WRITER_SENTINEL = object()

# Required tick fields written by the LIMIT50 live path (v1).
STATE25_PATH_TRACE_REQUIRED_FIELDS: tuple[str, ...] = (
    "schema",
    "monotonic_s",
    "tcp_pose_m_rad",
    "tcp_z_m",
    "normal_load_n",
    "force_norm_n",
    "tp_state",
)

# Optional fields already emitted when the live writer supplies them.
STATE25_PATH_TRACE_OPTIONAL_LIVE_FIELDS: tuple[str, ...] = (
    "wall_time_s",
    "force_integral_n_s",
    "force_integral_limit_n_s",
    "filtered_normal_n",
    "torque_norm_nm",
    "sensor_fresh",
    "wrench",
    "command_mode",
    "packet_sequence",
    "rtde_timestamp_s",
    "attempt_ordinal",
    "attempt_id",
    "session_epoch",
)

# Additive optional fields for future Ki-pocket / integral diagnostics.
# Kept on schema v1 (additive); not wired into a running host tonight.
STATE25_PATH_TRACE_OPTIONAL_DIAG_FIELDS: tuple[str, ...] = (
    "force_i_gain",
    "force_p_gain",
    "force_damping",
    "force_target_n",
    "force_error_n",
    "integral_saturated",
)


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _finite_list(values: Sequence[Any] | None, *, size: int | None = None) -> list[float] | None:
    if values is None:
        return None
    out: list[float] = []
    for value in values:
        number = _finite_or_none(value)
        if number is None:
            return None
        out.append(number)
    if size is not None and len(out) != size:
        return None
    return out


def integral_saturation_fraction(row: Mapping[str, Any]) -> float | None:
    """Return |I| / limit when both are finite and limit > 0; else None."""

    integral = _finite_or_none(row.get("force_integral_n_s"))
    limit = _finite_or_none(row.get("force_integral_limit_n_s"))
    if integral is None or limit is None or limit <= 0.0:
        return None
    return abs(integral) / limit


def is_integral_saturated(
    row: Mapping[str, Any],
    *,
    fraction: float = 0.98,
) -> bool | None:
    """True when |I| is within ``fraction`` of the clamp limit."""

    sat = integral_saturation_fraction(row)
    if sat is None:
        return None
    thresh = _finite_or_none(fraction)
    if thresh is None or thresh <= 0.0:
        raise ValueError("fraction must be a positive finite value")
    return sat >= thresh


def build_state25_row(
    *,
    monotonic_s: float,
    wall_time_s: float | None = None,
    tcp_pose_m_rad: Sequence[float],
    normal_load_n: float,
    force_norm_n: float,
    force_integral_n_s: float | None = None,
    force_integral_limit_n_s: float | None = None,
    filtered_normal_n: float | None = None,
    torque_norm_nm: float | None = None,
    sensor_fresh: bool | None = None,
    wrench: Sequence[float] | None = None,
    tp_state: int = 25,
    command_mode: int | None = None,
    packet_sequence: int | None = None,
    rtde_timestamp_s: float | None = None,
    attempt_ordinal: int | None = None,
    attempt_id: str | None = None,
    session_epoch: int | None = None,
    force_i_gain: float | None = None,
    force_p_gain: float | None = None,
    force_damping: float | None = None,
    force_target_n: float | None = None,
    force_error_n: float | None = None,
    integral_saturated: bool | None = None,
) -> dict[str, Any]:
    """Build one JSON-serializable state-25 tick row.

    Core force + outer-loop I-state fields are enough for PATH hard-stop /
    Ki-pocket reconstruction when joined to the attempt dispatch overlay via
    ``attempt_ordinal``.  Optional diag fields (gains, e_f, saturation flag)
    are additive on schema v1 for a future host wire — callers may omit them.
    """

    pose = _finite_list(tcp_pose_m_rad, size=6)
    if pose is None:
        raise ValueError("tcp_pose_m_rad must be six finite SI values")
    mono = _finite_or_none(monotonic_s)
    if mono is None:
        raise ValueError("monotonic_s must be finite")
    row: dict[str, Any] = {
        "schema": STATE25_PATH_TRACE_SCHEMA,
        "monotonic_s": mono,
        "tcp_pose_m_rad": pose,
        "tcp_z_m": float(pose[2]),
        "normal_load_n": float(normal_load_n),
        "force_norm_n": float(force_norm_n),
        "tp_state": int(tp_state),
    }
    wall = _finite_or_none(wall_time_s)
    if wall is not None:
        row["wall_time_s"] = wall
    integral = _finite_or_none(force_integral_n_s)
    if integral is not None:
        row["force_integral_n_s"] = integral
    limit = _finite_or_none(force_integral_limit_n_s)
    if limit is not None:
        row["force_integral_limit_n_s"] = limit
    filtered = _finite_or_none(filtered_normal_n)
    if filtered is not None:
        row["filtered_normal_n"] = filtered
    torque = _finite_or_none(torque_norm_nm)
    if torque is not None:
        row["torque_norm_nm"] = torque
    if sensor_fresh is not None:
        row["sensor_fresh"] = bool(sensor_fresh)
    wrench_list = _finite_list(wrench, size=6)
    if wrench_list is not None:
        row["wrench"] = wrench_list
    if command_mode is not None:
        row["command_mode"] = int(command_mode)
    if packet_sequence is not None:
        row["packet_sequence"] = int(packet_sequence)
    rtde_ts = _finite_or_none(rtde_timestamp_s)
    if rtde_ts is not None:
        row["rtde_timestamp_s"] = rtde_ts
    if attempt_ordinal is not None:
        row["attempt_ordinal"] = int(attempt_ordinal)
    if attempt_id:
        row["attempt_id"] = str(attempt_id)
    if session_epoch is not None:
        row["session_epoch"] = int(session_epoch)
    for key, value in (
        ("force_i_gain", force_i_gain),
        ("force_p_gain", force_p_gain),
        ("force_damping", force_damping),
        ("force_target_n", force_target_n),
        ("force_error_n", force_error_n),
    ):
        number = _finite_or_none(value)
        if number is not None:
            row[key] = number
    if integral_saturated is None and integral is not None and limit is not None:
        derived = is_integral_saturated(row)
        if derived is not None:
            row["integral_saturated"] = bool(derived)
    elif integral_saturated is not None:
        row["integral_saturated"] = bool(integral_saturated)
    return row


def append_state25_sidecar(run_dir: Path, row: Mapping[str, Any]) -> Path:
    """Append one JSONL tick next to the run ledger (synchronous helper).

    Live ``State25PathTrace.observe`` must not call this on the motion thread;
    it enqueues through ``_State25SidecarWriter`` instead.
    """

    path = Path(run_dir) / STATE25_PATH_TRACE_SIDECAR_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
    return path


class _State25SidecarWriter:
    """Background JSONL writer: motion thread only enqueues; never opens the file."""

    def __init__(
        self,
        path: Path,
        *,
        maxsize: int = STATE25_WRITE_QUEUE_MAX,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=max(1, int(maxsize)))
        self._dropped = 0
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="r008-state25-sidecar-writer",
            daemon=True,
        )
        self._thread.start()

    @property
    def dropped(self) -> int:
        return int(self._dropped)

    def enqueue(self, row: Mapping[str, Any]) -> None:
        if self._closed:
            return
        payload = dict(row)
        while True:
            try:
                self._queue.put_nowait(payload)
                return
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._dropped += 1
                    self._queue.task_done()
                except queue.Empty:
                    pass

    def flush(self, timeout_s: float = 30.0) -> None:
        """Block until all currently queued rows are on disk (or timeout)."""

        if self._closed:
            return
        done = threading.Event()

        def _mark() -> None:
            done.set()

        self._queue.put(_mark)
        if not done.wait(timeout=float(timeout_s)):
            raise TimeoutError("state25 sidecar writer flush timed out")

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(_STATE25_WRITER_SENTINEL)
        self._thread.join(timeout=30.0)

    def _run(self) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            while True:
                item = self._queue.get()
                try:
                    if item is _STATE25_WRITER_SENTINEL:
                        handle.flush()
                        return
                    if callable(item):
                        handle.flush()
                        item()
                        continue
                    handle.write(
                        json.dumps(item, sort_keys=True, allow_nan=False) + "\n"
                    )
                finally:
                    self._queue.task_done()


def build_state25_stop_dominant_context(
    *,
    reason_code: int,
    reason: str,
    wrench: Sequence[float] | None,
    tp_state: int | None,
    sensor_fresh: bool | None,
    command_mode: int | None,
    normal_load_n: float | None = None,
    force_norm_n: float | None = None,
    torque_norm_nm: float | None = None,
    filtered_normal_n: float | None = None,
    force_integral_n_s: float | None = None,
    force_integral_limit_n_s: float | None = None,
    abs_force_integral_max_n_s: float | None = None,
    last_state25_samples: Sequence[Mapping[str, Any]] | None = None,
    sidecar_path: str | None = None,
) -> dict[str, Any]:
    """Compact PATH stop-dominant diagnostic with integrator evidence."""

    payload: dict[str, Any] = {
        "schema": STATE25_STOP_DOMINANT_SCHEMA,
        "reason_code": int(reason_code),
        "reason": str(reason),
        "ring_source": "tp_state_25",
    }
    if tp_state is not None:
        payload["tp_state"] = int(tp_state)
    if sensor_fresh is not None:
        payload["sensor_fresh"] = bool(sensor_fresh)
    if command_mode is not None:
        payload["command_mode"] = int(command_mode)
    wrench_list = _finite_list(wrench, size=6)
    if wrench_list is not None:
        payload["wrench"] = wrench_list
    for key, value in (
        ("normal_load_n", normal_load_n),
        ("force_norm_n", force_norm_n),
        ("torque_norm_nm", torque_norm_nm),
        ("filtered_normal_n", filtered_normal_n),
        ("force_integral_n_s", force_integral_n_s),
        ("force_integral_limit_n_s", force_integral_limit_n_s),
        ("abs_force_integral_max_n_s", abs_force_integral_max_n_s),
    ):
        number = _finite_or_none(value)
        if number is not None:
            payload[key] = number
    if last_state25_samples:
        payload["last_state25_samples"] = [dict(sample) for sample in last_state25_samples]
        payload["state25_sample_count_in_ring"] = len(payload["last_state25_samples"])
    if sidecar_path:
        payload["sidecar_path"] = str(sidecar_path)
    return payload


def dump_state25_stop_dominant_json(run_dir: Path, context: Mapping[str, Any]) -> Path:
    """Write/overwrite the PATH stop-dominant dump beside the state-25 sidecar."""

    path = Path(run_dir) / STATE25_STOP_DOMINANT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(context), sort_keys=True, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def read_live_force_integral(owner: Any) -> tuple[float | None, float | None]:
    """Best-effort read of outer-loop integrator + limit from a live writer."""

    control = getattr(owner, "_qualification_control", None)
    runtime = getattr(control, "_runtime", None) if control is not None else None
    if runtime is None:
        # Some stacks expose the calibrated runtime directly.
        runtime = getattr(owner, "_runtime", None)
    if runtime is None:
        return None, None
    state = getattr(runtime, "_outer_state", None)
    integral = _finite_or_none(getattr(state, "force_integral_n_s", None))
    limit = _finite_or_none(getattr(runtime, "force_integral_limit_n_s", None))
    if limit is None:
        limit = _finite_or_none(getattr(owner, "force_integral_limit_n_s", None))
    return integral, limit


class State25PathTrace:
    """In-process ring + optional run-dir sidecar for state-25 PATH ticks."""

    def __init__(
        self,
        run_dir: Path | None = None,
        *,
        ring_size: int = STATE25_RING_DEFAULT,
        write_queue_max: int = STATE25_WRITE_QUEUE_MAX,
    ) -> None:
        if isinstance(ring_size, bool) or not isinstance(ring_size, int) or ring_size < 1:
            raise ValueError("ring_size must be a positive integer")
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._sidecar_path: Path | None = None
        self._rows_written = 0
        self._abs_force_integral_max_n_s: float | None = None
        self._active_attempt_ordinal: int | None = None
        self._writer: _State25SidecarWriter | None = None
        if self.run_dir is not None:
            self._sidecar_path = self.run_dir / STATE25_PATH_TRACE_SIDECAR_NAME
            self._writer = _State25SidecarWriter(
                self._sidecar_path, maxsize=write_queue_max
            )

    @property
    def rows_written(self) -> int:
        return self._rows_written

    @property
    def sidecar_path(self) -> Path | None:
        return self._sidecar_path

    @property
    def abs_force_integral_max_n_s(self) -> float | None:
        return self._abs_force_integral_max_n_s

    @property
    def sidecar_dropped(self) -> int:
        if self._writer is None:
            return 0
        return int(self._writer.dropped)

    def recent_rows(self, n: int | None = None) -> list[dict[str, Any]]:
        rows = list(self._ring)
        if n is None or n >= len(rows):
            return rows
        return rows[-n:]

    def observe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Record one state-25 tick; enqueue sidecar write off the motion thread."""

        payload = dict(row)
        if payload.get("schema") != STATE25_PATH_TRACE_SCHEMA:
            payload["schema"] = STATE25_PATH_TRACE_SCHEMA
        ordinal = payload.get("attempt_ordinal")
        if isinstance(ordinal, int) and not isinstance(ordinal, bool):
            if self._active_attempt_ordinal is not None and ordinal != self._active_attempt_ordinal:
                self.reset_attempt()
            self._active_attempt_ordinal = int(ordinal)
        integral = _finite_or_none(payload.get("force_integral_n_s"))
        if integral is not None:
            abs_i = abs(integral)
            if self._abs_force_integral_max_n_s is None or abs_i > self._abs_force_integral_max_n_s:
                self._abs_force_integral_max_n_s = abs_i
        self._ring.append(payload)
        if self._writer is not None:
            self._writer.enqueue(payload)
            self._rows_written += 1
        return payload

    def flush(self, timeout_s: float = 30.0) -> None:
        """Drain the async sidecar writer (between attempts / before stop dump)."""

        if self._writer is not None:
            self._writer.flush(timeout_s=timeout_s)

    def close(self) -> None:
        """Flush and stop the background writer."""

        if self._writer is not None:
            try:
                self._writer.flush(timeout_s=30.0)
            except TimeoutError:
                pass
            self._writer.close()
            self._writer = None

    def stop_dominant_context(self, **kwargs: Any) -> dict[str, Any]:
        self.flush()
        sidecar = str(self._sidecar_path) if self._sidecar_path is not None else None
        if sidecar is None and self.run_dir is not None:
            sidecar = str(self.run_dir / STATE25_PATH_TRACE_SIDECAR_NAME)
        if "abs_force_integral_max_n_s" not in kwargs:
            kwargs["abs_force_integral_max_n_s"] = self._abs_force_integral_max_n_s
        return build_state25_stop_dominant_context(
            last_state25_samples=self.recent_rows(),
            sidecar_path=sidecar,
            **kwargs,
        )

    def dump_stop_dominant(self, context: Mapping[str, Any]) -> Path | None:
        if self.run_dir is None:
            return None
        self.flush()
        return dump_state25_stop_dominant_json(self.run_dir, context)

    def reset_attempt(self) -> None:
        """Flush pending writes, then clear in-memory ring/stats (sidecar retained)."""

        self.flush()
        self._ring.clear()
        self._abs_force_integral_max_n_s = None
        self._active_attempt_ordinal = None


def attach_state25_trace(owner: Any, run_dir: Path | None) -> State25PathTrace:
    """Bind a fresh PATH trace onto a live writer (r008 host plumbing)."""

    prior = getattr(owner, "_state25_trace", None)
    if isinstance(prior, State25PathTrace):
        prior.close()
    trace = State25PathTrace(run_dir)
    setattr(owner, "_state25_trace", trace)
    return trace


def merge_state25_into_exception_detail(
    detail: str,
    *,
    trace: State25PathTrace | None,
    extras: MutableMapping[str, Any] | None = None,
) -> str:
    """Append state-25 tail metadata onto an execute_attempt failure detail."""

    payload: dict[str, Any] = {}
    if extras:
        payload.update(extras)
    if trace is not None:
        payload["state25_rows_written"] = int(trace.rows_written)
        payload["state25_ring_tail"] = trace.recent_rows(8)
        if trace.abs_force_integral_max_n_s is not None:
            payload["abs_force_integral_max_n_s"] = float(trace.abs_force_integral_max_n_s)
        if trace.sidecar_path is not None:
            payload["state25_sidecar"] = str(trace.sidecar_path)
    if not payload:
        return detail
    return f"{detail}; state25_diag={json.dumps(payload, sort_keys=True, allow_nan=False)}"


__all__ = [
    "STATE25_PATH_TRACE_OPTIONAL_DIAG_FIELDS",
    "STATE25_PATH_TRACE_OPTIONAL_LIVE_FIELDS",
    "STATE25_PATH_TRACE_REQUIRED_FIELDS",
    "STATE25_PATH_TRACE_SCHEMA",
    "STATE25_PATH_TRACE_SIDECAR_NAME",
    "STATE25_RING_DEFAULT",
    "STATE25_STOP_DOMINANT_NAME",
    "STATE25_STOP_DOMINANT_SCHEMA",
    "State25PathTrace",
    "append_state25_sidecar",
    "attach_state25_trace",
    "build_state25_row",
    "build_state25_stop_dominant_context",
    "dump_state25_stop_dominant_json",
    "integral_saturation_fraction",
    "is_integral_saturated",
    "merge_state25_into_exception_detail",
    "read_live_force_integral",
]
