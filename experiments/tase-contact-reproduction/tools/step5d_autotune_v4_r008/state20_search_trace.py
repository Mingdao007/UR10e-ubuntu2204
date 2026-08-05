"""r008 state-20 travel-vs-force sidecar (B3 Wave 1 observation).

Pure plumbing: record per-tick TCP pose + wrench while TP is in contact
search (state 20).  Does not change control decisions, safety envelope, or
reason-61 thresholds.  The frozen live writer calls into this helper only to
append / dump observation rows.
"""

from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path
from typing import Any, Mapping, MutableMapping, Sequence

STATE20_SEARCH_TRACE_SCHEMA = "step5d.autotune-v4/r008-state20-search-trace-v1"
STATE20_SEARCH_TRACE_SIDECAR_NAME = "r008-state20-search-trace.jsonl"
STATE20_STOP_DOMINANT_SCHEMA = "step5d.autotune-v4/r008-state20-stop-dominant-v1"
STATE20_STOP_DOMINANT_NAME = "r008-state20-stop-dominant.json"
STATE20_RING_DEFAULT = 64


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


def build_state20_row(
    *,
    monotonic_s: float,
    wall_time_s: float | None = None,
    tcp_pose_m_rad: Sequence[float],
    normal_load_n: float,
    force_norm_n: float,
    filtered_normal_n: float | None = None,
    torque_norm_nm: float | None = None,
    sensor_fresh: bool | None = None,
    wrench: Sequence[float] | None = None,
    tp_state: int = 20,
    command_mode: int | None = None,
    packet_sequence: int | None = None,
    rtde_timestamp_s: float | None = None,
    attempt_ordinal: int | None = None,
    attempt_id: str | None = None,
    session_epoch: int | None = None,
) -> dict[str, Any]:
    """Build one JSON-serializable state-20 tick row (new fields only)."""

    pose = _finite_list(tcp_pose_m_rad, size=6)
    if pose is None:
        raise ValueError("tcp_pose_m_rad must be six finite SI values")
    mono = _finite_or_none(monotonic_s)
    if mono is None:
        raise ValueError("monotonic_s must be finite")
    row: dict[str, Any] = {
        "schema": STATE20_SEARCH_TRACE_SCHEMA,
        "monotonic_s": mono,
        "tcp_pose_m_rad": pose,
        "normal_load_n": float(normal_load_n),
        "force_norm_n": float(force_norm_n),
        "tp_state": int(tp_state),
    }
    wall = _finite_or_none(wall_time_s)
    if wall is not None:
        row["wall_time_s"] = wall
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
    return row


def append_state20_sidecar(run_dir: Path, row: Mapping[str, Any]) -> Path:
    """Append one JSONL tick next to the run ledger."""

    path = Path(run_dir) / STATE20_SEARCH_TRACE_SIDECAR_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row), sort_keys=True, allow_nan=False) + "\n")
    return path


def build_stop_dominant_context(
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
    last_state20_samples: Sequence[Mapping[str, Any]] | None = None,
    sidecar_path: str | None = None,
) -> dict[str, Any]:
    """Compact stop-dominant diagnostic payload for exception text / JSON dump."""

    payload: dict[str, Any] = {
        "schema": STATE20_STOP_DOMINANT_SCHEMA,
        "reason_code": int(reason_code),
        "reason": str(reason),
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
    ):
        number = _finite_or_none(value)
        if number is not None:
            payload[key] = number
    if last_state20_samples:
        payload["last_state20_samples"] = [dict(sample) for sample in last_state20_samples]
        payload["state20_sample_count_in_ring"] = len(payload["last_state20_samples"])
    if sidecar_path:
        payload["sidecar_path"] = str(sidecar_path)
    return payload


def dump_stop_dominant_json(run_dir: Path, context: Mapping[str, Any]) -> Path:
    """Write/overwrite the stop-dominant dump beside the state-20 sidecar."""

    path = Path(run_dir) / STATE20_STOP_DOMINANT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(context), sort_keys=True, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


class State20SearchTrace:
    """In-process ring + optional run-dir sidecar for state-20 ticks."""

    def __init__(
        self,
        run_dir: Path | None = None,
        *,
        ring_size: int = STATE20_RING_DEFAULT,
    ) -> None:
        if isinstance(ring_size, bool) or not isinstance(ring_size, int) or ring_size < 1:
            raise ValueError("ring_size must be a positive integer")
        self.run_dir = Path(run_dir) if run_dir is not None else None
        self._ring: deque[dict[str, Any]] = deque(maxlen=ring_size)
        self._sidecar_path: Path | None = None
        self._rows_written = 0

    @property
    def rows_written(self) -> int:
        return self._rows_written

    @property
    def sidecar_path(self) -> Path | None:
        return self._sidecar_path

    def recent_rows(self, n: int | None = None) -> list[dict[str, Any]]:
        rows = list(self._ring)
        if n is None or n >= len(rows):
            return rows
        return rows[-n:]

    def observe(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Record one state-20 tick; persist when ``run_dir`` is set."""

        payload = dict(row)
        if payload.get("schema") != STATE20_SEARCH_TRACE_SCHEMA:
            payload["schema"] = STATE20_SEARCH_TRACE_SCHEMA
        self._ring.append(payload)
        if self.run_dir is not None:
            self._sidecar_path = append_state20_sidecar(self.run_dir, payload)
            self._rows_written += 1
        return payload

    def stop_dominant_context(self, **kwargs: Any) -> dict[str, Any]:
        sidecar = str(self._sidecar_path) if self._sidecar_path is not None else None
        if sidecar is None and self.run_dir is not None:
            sidecar = str(self.run_dir / STATE20_SEARCH_TRACE_SIDECAR_NAME)
        return build_stop_dominant_context(
            last_state20_samples=self.recent_rows(),
            sidecar_path=sidecar,
            **kwargs,
        )

    def dump_stop_dominant(self, context: Mapping[str, Any]) -> Path | None:
        if self.run_dir is None:
            return None
        return dump_stop_dominant_json(self.run_dir, context)


def attach_state20_trace(owner: Any, run_dir: Path | None) -> State20SearchTrace:
    """Bind a fresh trace onto a live writer (r008 host plumbing)."""

    trace = State20SearchTrace(run_dir)
    setattr(owner, "_state20_trace", trace)
    return trace


def format_stop_dominant_error(
    *,
    prefix: str,
    reason_code: int,
    reason: str,
    context: Mapping[str, Any],
) -> str:
    """Keep the historical ``reason_code=… reason=…`` substring matchable."""

    compact = {
        key: context[key]
        for key in (
            "tp_state",
            "sensor_fresh",
            "command_mode",
            "wrench",
            "normal_load_n",
            "force_norm_n",
            "torque_norm_nm",
            "filtered_normal_n",
            "state20_sample_count_in_ring",
            "sidecar_path",
        )
        if key in context
    }
    if "last_state20_samples" in context:
        # Cap exception text: keep last few only.
        samples = list(context["last_state20_samples"])[-8:]
        compact["last_state20_samples"] = samples
    return (
        f"{prefix}"
        f"reason_code={int(reason_code)} reason={reason}; "
        f"stop_diag={json.dumps(compact, sort_keys=True, allow_nan=False)}"
    )


def merge_state20_into_exception_detail(
    detail: str,
    *,
    trace: State20SearchTrace | None,
    extras: MutableMapping[str, Any] | None = None,
) -> str:
    """Append state-20 tail metadata onto an execute_attempt failure detail."""

    payload: dict[str, Any] = {}
    if extras:
        payload.update(extras)
    if trace is not None:
        payload["state20_rows_written"] = int(trace.rows_written)
        payload["state20_ring_tail"] = trace.recent_rows(8)
        if trace.sidecar_path is not None:
            payload["state20_sidecar"] = str(trace.sidecar_path)
    if not payload:
        return detail
    return f"{detail}; state20_diag={json.dumps(payload, sort_keys=True, allow_nan=False)}"


__all__ = [
    "STATE20_RING_DEFAULT",
    "STATE20_SEARCH_TRACE_SCHEMA",
    "STATE20_SEARCH_TRACE_SIDECAR_NAME",
    "STATE20_STOP_DOMINANT_NAME",
    "STATE20_STOP_DOMINANT_SCHEMA",
    "State20SearchTrace",
    "append_state20_sidecar",
    "attach_state20_trace",
    "build_state20_row",
    "build_stop_dominant_context",
    "dump_stop_dominant_json",
    "format_stop_dominant_error",
    "merge_state20_into_exception_detail",
]
