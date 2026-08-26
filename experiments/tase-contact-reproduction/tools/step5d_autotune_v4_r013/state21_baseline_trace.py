"""R013 State21 baseline force sidecar.

The binary ``.r013life`` artifact is the authoritative full-rate trajectory.
This sidecar is the bounded human/diagnostic view of the BASELINE segment and
is written asynchronously so it cannot become a control-loop dependency.
"""

from __future__ import annotations

import json
import math
import queue
import threading
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Sequence


STATE21_BASELINE_TRACE_SCHEMA = "step5d.autotune-v4/r013-state21-baseline-trace-v1"
STATE21_BASELINE_TRACE_SIDECAR_NAME = "r013-state21-baseline-trace.jsonl"
STATE21_RING_DEFAULT = 256
STATE21_ATTEMPT_ROWS_MAX = 8192
STATE21_WRITE_QUEUE_MAX = 4096
_SENTINEL = object()


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _finite_list(values: Sequence[Any] | None, *, size: int) -> list[float] | None:
    if values is None or len(values) != size:
        return None
    result = [_finite(value) for value in values]
    if any(value is None for value in result):
        return None
    return [float(value) for value in result]


def build_state21_row(
    *,
    monotonic_s: float,
    tcp_pose_m_rad: Sequence[float],
    tcp_speed_m_s_rad_s: Sequence[float],
    normal_load_n: float,
    force_norm_n: float,
    filtered_normal_n: float,
    torque_norm_nm: float,
    sensor_fresh: bool,
    wrench: Sequence[float],
    command_mode: int | None = None,
    packet_sequence: int | None = None,
    rtde_timestamp_s: float | None = None,
    attempt_ordinal: int | None = None,
    attempt_id: str | None = None,
    session_epoch: int | None = None,
    setpoint_n: float | None = None,
    qdot: Sequence[float] | None = None,
    force_integral_n_s: float | None = None,
    force_integral_limit_n_s: float | None = None,
    force_error_n: float | None = None,
    handoff_policy: str | None = None,
    baseline_transition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    mono = _finite(monotonic_s)
    pose = _finite_list(tcp_pose_m_rad, size=6)
    speed = _finite_list(tcp_speed_m_s_rad_s, size=6)
    wrench_values = _finite_list(wrench, size=6)
    if mono is None or pose is None or speed is None or wrench_values is None:
        raise ValueError("State21 row contains non-finite or incomplete vectors")
    scalar_values = {
        "normal_load_n": _finite(normal_load_n),
        "force_norm_n": _finite(force_norm_n),
        "filtered_normal_n": _finite(filtered_normal_n),
        "torque_norm_nm": _finite(torque_norm_nm),
    }
    if any(value is None for value in scalar_values.values()):
        raise ValueError("State21 row contains non-finite force scalars")
    row: dict[str, Any] = {
        "schema": STATE21_BASELINE_TRACE_SCHEMA,
        "monotonic_s": mono,
        "tcp_pose_m_rad": pose,
        "tcp_speed_m_s_rad_s": speed,
        "normal_load_n": scalar_values["normal_load_n"],
        "force_norm_n": scalar_values["force_norm_n"],
        "filtered_normal_n": scalar_values["filtered_normal_n"],
        "torque_norm_nm": scalar_values["torque_norm_nm"],
        "sensor_fresh": bool(sensor_fresh),
        "wrench": wrench_values,
        "tp_state": 21,
    }
    for key, value in (
        ("command_mode", command_mode),
        ("packet_sequence", packet_sequence),
        ("attempt_ordinal", attempt_ordinal),
        ("session_epoch", session_epoch),
    ):
        if value is not None:
            row[key] = int(value)
    if attempt_id:
        row["attempt_id"] = str(attempt_id)
    for key, value in (
        ("rtde_timestamp_s", rtde_timestamp_s),
        ("setpoint_n", setpoint_n),
        ("force_integral_n_s", force_integral_n_s),
        ("force_integral_limit_n_s", force_integral_limit_n_s),
        ("force_error_n", force_error_n),
    ):
        number = _finite(value)
        if number is not None:
            row[key] = number
    if handoff_policy:
        row["handoff_policy"] = str(handoff_policy)
    if baseline_transition is not None:
        if not isinstance(baseline_transition, Mapping):
            raise ValueError("State21 baseline transition diagnostic is not a mapping")
        profile_id = baseline_transition.get("profile_id")
        reason = baseline_transition.get("reason")
        if not isinstance(profile_id, str) or not profile_id or not isinstance(reason, str):
            raise ValueError("State21 baseline transition identity/reason is invalid")
        flags: dict[str, bool] = {}
        for key in (
            "path_request_allowed",
            "ramp_complete",
            "sensor_fresh",
            "timing_gate_passed",
            "safety_normal",
            "hard_limits_passed",
            "narrow_readiness_passed",
            "narrow_path_release_opened",
        ):
            value = baseline_transition.get(key)
            if not isinstance(value, bool):
                raise ValueError(
                    f"State21 baseline transition flag {key!r} is not typed"
                )
            flags[key] = value
        row["baseline_transition"] = {
            "profile_id": profile_id,
            **flags,
            "reason": reason,
        }
    qdot_values = _finite_list(qdot, size=6)
    if qdot_values is not None:
        row["qdot"] = qdot_values
    return row


class _Writer:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.queue: queue.Queue[Any] = queue.Queue(maxsize=STATE21_WRITE_QUEUE_MAX)
        self.dropped = 0
        self.closed = False
        self.thread = threading.Thread(target=self._run, name="r013-state21-sidecar", daemon=True)
        self.thread.start()

    def enqueue(self, row: Mapping[str, Any]) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(dict(row))
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout_s: float = 30.0) -> None:
        if self.closed:
            return
        done = threading.Event()
        self.queue.put(done)
        if not done.wait(timeout=float(timeout_s)):
            raise TimeoutError("state21 sidecar writer flush timed out")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.queue.put(_SENTINEL)
        self.thread.join(timeout=30.0)

    def _run(self) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            while True:
                item = self.queue.get()
                try:
                    if item is _SENTINEL:
                        handle.flush()
                        return
                    if isinstance(item, threading.Event):
                        handle.flush()
                        item.set()
                        continue
                    handle.write(json.dumps(item, sort_keys=True, allow_nan=False) + "\n")
                finally:
                    self.queue.task_done()


class State21BaselineTrace:
    """Attempt-scoped State21 ring with asynchronous 10 Hz persistence."""

    def __init__(
        self,
        run_dir: Path | None = None,
        *,
        persistence_stride: int = 50,
        ring_size: int = STATE21_RING_DEFAULT,
        attempt_rows_max: int = STATE21_ATTEMPT_ROWS_MAX,
    ) -> None:
        if persistence_stride < 1 or ring_size < 1 or attempt_rows_max < 1:
            raise ValueError("State21 trace bounds must be positive")
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self.sidecar_path = (
            self.run_dir / STATE21_BASELINE_TRACE_SIDECAR_NAME
            if self.run_dir is not None
            else None
        )
        self._ring: deque[dict[str, Any]] = deque(maxlen=int(ring_size))
        self._attempt_rows: deque[dict[str, Any]] = deque(maxlen=int(attempt_rows_max))
        self._persistence_stride = int(persistence_stride)
        self._attempt_ordinal: int | None = None
        self._observation_count = 0
        self._rows_written = 0
        self._writer = _Writer(self.sidecar_path) if self.sidecar_path is not None else None

    @property
    def rows_written(self) -> int:
        return self._rows_written

    @property
    def sidecar_dropped(self) -> int:
        return 0 if self._writer is None else int(self._writer.dropped)

    def begin_attempt(self, ordinal: int) -> None:
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
            raise ValueError("State21 attempt ordinal must be positive")
        if self._attempt_ordinal == ordinal:
            return
        self.flush()
        self._ring.clear()
        self._attempt_rows.clear()
        self._observation_count = 0
        self._attempt_ordinal = int(ordinal)

    def observe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(row)
        payload["schema"] = STATE21_BASELINE_TRACE_SCHEMA
        ordinal = payload.get("attempt_ordinal")
        if ordinal is not None and int(ordinal) != self._attempt_ordinal:
            raise ValueError("State21 trace attempt changed without begin_attempt")
        self._ring.append(payload)
        self._attempt_rows.append(payload)
        self._observation_count += 1
        if self._observation_count == 1 or self._observation_count % self._persistence_stride == 0:
            if self._writer is not None:
                self._writer.enqueue(payload)
                self._rows_written += 1
        return payload

    def recent_rows(self, n: int | None = None) -> list[dict[str, Any]]:
        rows = list(self._ring)
        return rows if n is None or n >= len(rows) else rows[-n:]

    def attempt_rows(self, ordinal: int | None = None) -> list[dict[str, Any]]:
        if ordinal is not None and self._attempt_ordinal != int(ordinal):
            return []
        return list(self._attempt_rows)

    def flush(self, timeout_s: float = 30.0) -> None:
        if self._writer is not None:
            self._writer.flush(timeout_s=timeout_s)

    def close(self) -> None:
        if self._writer is None:
            return
        try:
            self.flush()
        finally:
            self._writer.close()
            self._writer = None


def attach_state21_trace(
    owner: Any,
    run_dir: Path | None,
    *,
    persistence_stride: int = 50,
) -> State21BaselineTrace:
    prior = getattr(owner, "_state21_trace", None)
    if isinstance(prior, State21BaselineTrace):
        prior.close()
    trace = State21BaselineTrace(run_dir, persistence_stride=persistence_stride)
    setattr(owner, "_state21_trace", trace)
    return trace


__all__ = [
    "STATE21_BASELINE_TRACE_SCHEMA",
    "STATE21_BASELINE_TRACE_SIDECAR_NAME",
    "State21BaselineTrace",
    "attach_state21_trace",
    "build_state21_row",
]
