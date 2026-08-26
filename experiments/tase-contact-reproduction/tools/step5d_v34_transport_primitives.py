"""Import-safe RTDE and deadline primitives retained from the Step5d v34 bridge.

The production ``kunwei_rtde_bridge.py`` module also imports the complete
Pinocchio/RNN stack.  Direct Torque needs its proven RTDE framing, drain, and
no-burst deadline behavior, but must not acquire those unrelated runtime
dependencies.  This module is a narrow compatibility extraction of those
primitives; it performs no network I/O at import time.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import select
import socket
import struct
import threading
import time
from typing import Any, Sequence

from _ur_common import RTDEClient


def runtime_scheduler_metadata() -> dict[str, Any]:
    """Return the v35-compatible process scheduler identity."""

    policy_value = os.sched_getscheduler(0)
    names = {
        getattr(os, "SCHED_OTHER", -1): "SCHED_OTHER",
        getattr(os, "SCHED_FIFO", -2): "SCHED_FIFO",
        getattr(os, "SCHED_RR", -3): "SCHED_RR",
    }
    return {
        "policy": names.get(policy_value, f"UNKNOWN_{policy_value}"),
        "policy_value": policy_value,
        "priority": os.sched_getparam(0).sched_priority,
    }


def runtime_thread_scheduler_snapshot() -> dict[str, Any]:
    """Copy the v35 per-thread scheduler inspection without changing policy."""

    current_tid = threading.get_native_id()
    threads: list[dict[str, Any]] = []
    for task_dir in sorted(
        Path("/proc/self/task").iterdir(), key=lambda path: int(path.name)
    ):
        try:
            tid = int(task_dir.name)
            policy_value = os.sched_getscheduler(tid)
            priority = os.sched_getparam(tid).sched_priority
        except (FileNotFoundError, ProcessLookupError):
            continue
        policy_name = {
            getattr(os, "SCHED_OTHER", -1): "SCHED_OTHER",
            getattr(os, "SCHED_FIFO", -2): "SCHED_FIFO",
            getattr(os, "SCHED_RR", -3): "SCHED_RR",
        }.get(policy_value, f"UNKNOWN_{policy_value}")
        threads.append(
            {
                "tid": tid,
                "is_control_thread": tid == current_tid,
                "policy": policy_name,
                "policy_value": policy_value,
                "priority": priority,
            }
        )
    counts: dict[str, int] = {}
    for row in threads:
        key = f"{row['policy']}/{row['priority']}"
        counts[key] = counts.get(key, 0) + 1
    return {"threads": threads, "counts": counts}


def require_v35_sched_other() -> dict[str, Any]:
    """Fail closed unless the process and every current thread are OTHER/0."""

    process = runtime_scheduler_metadata()
    threads = runtime_thread_scheduler_snapshot()
    expected = {
        "policy": "SCHED_OTHER",
        "policy_value": os.SCHED_OTHER,
        "priority": 0,
    }
    non_other = [
        row
        for row in threads["threads"]
        if row["policy"] != "SCHED_OTHER"
        or row["policy_value"] != os.SCHED_OTHER
        or row["priority"] != 0
    ]
    if process != expected or non_other:
        raise RuntimeError(
            "v35_sched_other_required:"
            f"process={process},non_other_threads={non_other}"
        )
    return {
        "mode": "v35_quota_safe_sched_other",
        "process": process,
        "threads": threads,
    }


def rtde_struct_format(type_name: str) -> str:
    mapping = {
        "DOUBLE": "d",
        "VECTOR3D": "3d",
        "VECTOR6D": "6d",
        "VECTOR6INT32": "6i",
        "VECTOR6UINT32": "6I",
        "UINT32": "I",
        "UINT64": "Q",
        "INT32": "i",
        "BOOL": "?",
    }
    if type_name not in mapping:
        raise RuntimeError(f"unsupported RTDE type: {type_name}")
    return mapping[type_name]


def pack_rtde_value(type_name: str, value: Any) -> bytes:
    fmt = rtde_struct_format(type_name)
    if fmt in {"3d", "6d", "6i", "6I"}:
        return struct.pack("!" + fmt, *value)
    return struct.pack("!" + fmt, value)


class RTDEBridgeClient(RTDEClient):
    """Generic v34-compatible RTDE input/output client.

    ``receive_available`` preserves every decoded output packet for diagnostic
    and dataset capture.  ``recv_latest_available_sample`` retains the mature
    bridge's drain-latest behavior for control decisions.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._started = False
        self._output_fields: list[str] = []

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.pause(best_effort=True)
        super().__exit__(exc_type, exc, tb)

    def setup_inputs(self, fields: list[str]) -> tuple[int, list[str]]:
        self._send_packet("I", ",".join(fields).encode())
        packet_type, payload = self._recv_packet()
        if packet_type != ord("I"):
            raise RuntimeError(
                f"unexpected RTDE input setup response type {packet_type}"
            )
        recipe_id = payload[0]
        type_names = payload[1:].decode("ascii", errors="replace").split(",")
        if recipe_id == 0 or any(name == "NOT_FOUND" for name in type_names):
            raise RuntimeError(
                f"invalid RTDE input recipe: id={recipe_id} types={type_names}"
            )
        return recipe_id, type_names

    def send_input_sample(
        self,
        recipe_id: int,
        type_names: list[str],
        values: Sequence[Any],
    ) -> None:
        if len(type_names) != len(values):
            raise ValueError("RTDE input type/value length mismatch")
        payload = bytearray([recipe_id])
        for type_name, value in zip(type_names, values):
            payload.extend(pack_rtde_value(type_name, value))
        self._send_packet("U", bytes(payload))

    def send_inputs(
        self,
        recipe_id: int,
        type_names: list[str],
        values: Sequence[Any],
    ) -> None:
        """Compatibility spelling used by the Direct Torque runner."""

        self.send_input_sample(recipe_id, type_names, values)

    def setup_outputs(
        self,
        frequency: float,
        fields: list[str],
    ) -> tuple[int, list[str]]:
        result = super().setup_outputs(frequency, fields)
        self._output_fields = list(fields)
        return result

    def start(self) -> None:
        super().start()
        self._started = True

    def pause(self, best_effort: bool = False) -> bool:
        if self.sock is None or not self._started:
            return False
        try:
            self._send_packet("P")
            packet_type, payload = self._recv_packet()
            ok = packet_type == ord("P") and payload == b"\x01"
            if not ok and not best_effort:
                raise RuntimeError(
                    f"RTDE pause failed: type={packet_type} payload={payload!r}"
                )
            self._started = False
            return ok
        except (OSError, RuntimeError, socket.timeout):
            if not best_effort:
                raise
            return False

    def _decode_output_sample(
        self,
        payload: bytes,
        type_names: list[str],
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        cursor = 1
        values: list[Any] = []
        for type_name in type_names:
            fmt = rtde_struct_format(type_name)
            width = struct.calcsize("!" + fmt)
            if len(payload) < cursor + width:
                raise RuntimeError("RTDE output payload truncated")
            unpacked = struct.unpack("!" + fmt, payload[cursor : cursor + width])
            cursor += width
            values.append(unpacked[0] if len(unpacked) == 1 else tuple(unpacked))
        if cursor != len(payload):
            raise RuntimeError("RTDE output payload has trailing bytes")
        output_fields = self._output_fields if fields is None else fields
        return {field: value for field, value in zip(output_fields, values)}

    def receive_available(
        self,
        recipe_id: int,
        type_names: list[str],
        fields: list[str],
        timeout_s: float = 0.0,
    ) -> list[dict[str, Any]]:
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], timeout_s)
        if not ready:
            return []
        samples: list[dict[str, Any]] = []
        while True:
            packet_type, payload = self._recv_packet()
            if (
                packet_type == ord("U")
                and payload
                and payload[0] == recipe_id
            ):
                samples.append(
                    self._decode_output_sample(payload, type_names, fields)
                )
            ready, _, _ = select.select([self.sock], [], [], 0.0)
            if not ready:
                return samples

    def receive_available_bounded(
        self,
        recipe_id: int,
        type_names: list[str],
        fields: list[str],
        timeout_s: float = 0.0,
        *,
        max_samples: int = 4,
        max_wall_s: float = 0.004,
    ) -> list[dict[str, Any]]:
        """Drain a bounded RTDE batch so a hot socket cannot starve writes."""

        if max_samples <= 0 or max_wall_s <= 0.0:
            raise ValueError("bounded RTDE receive limits must be positive")
        assert self.sock is not None
        ready, _, _ = select.select([self.sock], [], [], timeout_s)
        if not ready:
            return []
        deadline = time.monotonic() + max_wall_s
        samples: list[dict[str, Any]] = []
        while len(samples) < max_samples and time.monotonic() < deadline:
            packet_type, payload = self._recv_packet()
            if (
                packet_type == ord("U")
                and payload
                and payload[0] == recipe_id
            ):
                samples.append(
                    self._decode_output_sample(payload, type_names, fields)
                )
            ready, _, _ = select.select([self.sock], [], [], 0.0)
            if not ready:
                break
        return samples

    def receive_latest(
        self,
        recipe_id: int,
        type_names: list[str],
        fields: list[str],
        timeout_s: float = 0.0,
    ) -> dict[str, Any] | None:
        samples = self.receive_available(
            recipe_id, type_names, fields, timeout_s
        )
        return samples[-1] if samples else None

    def recv_latest_available_sample(
        self,
        recipe_id: int,
        type_names: list[str],
        timeout_s: float = 0.0,
    ) -> tuple[dict[str, Any] | None, int]:
        samples = self.receive_available(
            recipe_id,
            type_names,
            self._output_fields,
            timeout_s,
        )
        return (samples[-1] if samples else None), len(samples)


def advance_periodic_deadline(
    deadline: float,
    now: float,
    period: float,
) -> tuple[float, int, float]:
    """Advance to the first future slot without burst catch-up."""

    if period <= 0.0:
        raise ValueError("period must be positive")
    lateness = max(0.0, now - deadline)
    missed_slots = max(0, int(math.floor(lateness / period)))
    return deadline + (missed_slots + 1) * period, missed_slots, lateness


def wait_for_rtde_or_deadline(
    rtde: RTDEBridgeClient,
    *,
    next_write_s: float,
    now_s: float,
) -> float:
    """Wait on RTDE output until the next absolute command release."""

    timeout_s = max(0.0, float(next_write_s) - float(now_s))
    if timeout_s <= 0.0 or rtde.sock is None:
        return 0.0
    wait_start = time.perf_counter()
    try:
        select.select([rtde.sock], [], [], timeout_s)
    except InterruptedError:
        pass
    return time.perf_counter() - wait_start
