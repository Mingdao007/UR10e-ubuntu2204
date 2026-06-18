from __future__ import annotations

import argparse
import json
import math
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .kunwei_force_gate import FORCE_KG_TO_N, FIELDS, MOMENT_KG_M_TO_NM, START_STREAM, STOP_STREAM, parse_frame, pop_frames


@dataclass(frozen=True)
class KunweiMonitorConfig:
    sensor_ip: str = "192.168.50.25"
    sensor_port: int = 5152
    connect_timeout_s: float = 3.0
    recv_timeout_s: float = 0.25
    window_s: float = 0.5
    latest_max_age_s: float = 0.25
    min_recent_samples: int = 20
    max_force_delta_n: float = 8.0
    send_start_command: bool = True
    send_stop_command: bool = True
    raw_frames_path: Path | None = None


class KunweiPersistentMonitor:
    def __init__(self, config: KunweiMonitorConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self._raw_handle = None
        self._samples: deque[tuple[float, tuple[float, ...]]] = deque(maxlen=20000)
        self._baseline: tuple[float, ...] | None = None
        self._max_force_delta_n = 0.0
        self._status = "not_started"
        self._failure_reason: str | None = None
        self._bytes_received = 0
        self._packets_received = 0
        self._dropped_sync_bytes = 0
        self._parse_errors = 0
        self._stream_start_command_sent = False
        self._stream_stop_command_sent = False

    def start(self) -> None:
        if self._thread is not None:
            return
        if self.config.raw_frames_path is not None:
            self.config.raw_frames_path.parent.mkdir(parents=True, exist_ok=True)
            self._raw_handle = self.config.raw_frames_path.open("wb")
        self._thread = threading.Thread(target=self._run, name="kunwei-persistent-monitor", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            snapshot = self.snapshot()
            if snapshot["ok"]:
                self.freeze_baseline_from_recent()
                return True
            if snapshot["status"] in {"connect_or_socket_error", "socket_closed"}:
                return False
            time.sleep(0.05)
        return self.snapshot()["ok"]

    def freeze_baseline_from_recent(self) -> None:
        with self._lock:
            if self._baseline is not None:
                return
            recent = self._recent_locked(time.monotonic())
            if recent:
                self._baseline = _mean_tuple([sample for _, sample in recent])

    def reset_baseline_from_recent(self) -> None:
        with self._lock:
            recent = self._recent_locked(time.monotonic())
            if not recent:
                raise RuntimeError("Kunwei monitor has no recent samples for software baseline reset")
            self._baseline = _mean_tuple([sample for _, sample in recent])
            self._max_force_delta_n = 0.0

    def force_delta_n(self) -> float:
        with self._lock:
            if self._baseline is None or not self._samples:
                return 0.0
            delta = _force_delta_n(self._samples[-1][1], self._baseline)
            self._max_force_delta_n = max(self._max_force_delta_n, delta)
            return delta

    def assert_fresh_and_within_force_delta(self) -> None:
        snapshot = self.snapshot()
        if not snapshot["ok"]:
            raise RuntimeError(f"Kunwei monitor stale or not ready: {snapshot['status']}")
        force_delta = self.force_delta_n()
        if force_delta > self.config.max_force_delta_n:
            raise RuntimeError(
                f"Kunwei force delta exceeded limit: {force_delta:.3f} N > {self.config.max_force_delta_n:.3f} N"
            )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._sock is not None:
            if self.config.send_stop_command:
                try:
                    self._sock.sendall(STOP_STREAM)
                    self._stream_stop_command_sent = True
                except OSError:
                    pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._raw_handle is not None:
            self._raw_handle.close()
            self._raw_handle = None
        with self._lock:
            if self._status not in {"connect_or_socket_error", "socket_closed"}:
                self._status = "stopped"

    def write_summary(self, path: Path, extra: dict[str, Any] | None = None) -> None:
        payload = self.snapshot()
        if extra:
            payload.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            recent = self._recent_locked(now)
            latest_time, latest = self._samples[-1] if self._samples else (None, None)
            latest_age_s = None if latest_time is None else now - latest_time
            sample_window_s = recent[-1][0] - recent[0][0] if len(recent) > 1 else 0.0
            recent_rate_hz = (len(recent) - 1) / sample_window_s if sample_window_s > 0.0 and len(recent) > 1 else None
            status = self._status
            ok = (
                len(recent) >= self.config.min_recent_samples
                and latest_age_s is not None
                and latest_age_s <= self.config.latest_max_age_s
                and self._failure_reason is None
            )
            if self._failure_reason is not None:
                status = self._status
            elif self._status == "streaming" and not ok:
                status = "waiting_for_recent_window"
            elif self._status == "streaming" and ok:
                status = "kunwei_persistent_ready"
            force_delta = 0.0
            if latest is not None and self._baseline is not None:
                force_delta = _force_delta_n(latest, self._baseline)
                self._max_force_delta_n = max(self._max_force_delta_n, force_delta)
            return {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "gate": "kunwei_kwr75b_tcp_persistent_force_monitor",
                "role": "primary_step5a_force_source_persistent_monitor",
                "ok": ok,
                "status": status,
                "failure_reason": self._failure_reason,
                "sensor_endpoint": f"{self.config.sensor_ip}:{self.config.sensor_port}",
                "transport": "tcp-client",
                "bridge_process_status": "direct_tcp_no_bridge_process",
                "manual_basis": {
                    "frame_bytes": 28,
                    "stream_command_hex": START_STREAM.hex(" ").upper(),
                    "stop_command_hex": STOP_STREAM.hex(" ").upper(),
                    "raw_force_units": "Kg as written in Kunwei manual",
                    "raw_moment_units": "Kg*m as written in Kunwei manual",
                    "configuration_write_not_used": True,
                },
                "window_s": self.config.window_s,
                "latest_max_age_s": self.config.latest_max_age_s,
                "min_recent_samples": self.config.min_recent_samples,
                "max_force_delta_n": self.config.max_force_delta_n,
                "stream_start_command_sent": self._stream_start_command_sent,
                "stream_stop_command_sent": self._stream_stop_command_sent,
                "bytes_received": self._bytes_received,
                "packets_received": self._packets_received,
                "dropped_sync_bytes": self._dropped_sync_bytes,
                "parse_errors": self._parse_errors,
                "samples": len(self._samples),
                "recent_sample_count": len(recent),
                "recent_window_s": sample_window_s,
                "recent_rate_hz": recent_rate_hz,
                "latest_sample_age_s": latest_age_s,
                "latest_manual_units": None if latest is None else dict(zip(FIELDS, latest)),
                "latest_si_units": None if latest is None else _si_payload(latest),
                "baseline_manual_units": None if self._baseline is None else dict(zip(FIELDS, self._baseline)),
                "baseline_si_units": None if self._baseline is None else _si_payload(self._baseline),
                "current_force_delta_n": force_delta,
                "max_observed_force_delta_n": self._max_force_delta_n,
                "raw_frames_path": None if self.config.raw_frames_path is None else str(self.config.raw_frames_path),
            }

    def _run(self) -> None:
        buffer = bytearray()
        try:
            self._sock = socket.create_connection(
                (self.config.sensor_ip, self.config.sensor_port),
                timeout=self.config.connect_timeout_s,
            )
            self._sock.settimeout(self.config.recv_timeout_s)
            if self.config.send_start_command:
                self._sock.sendall(START_STREAM)
                self._stream_start_command_sent = True
            with self._lock:
                self._status = "streaming"
            while not self._stop_event.is_set():
                try:
                    chunk = self._sock.recv(8192)
                except socket.timeout:
                    continue
                if not chunk:
                    with self._lock:
                        self._status = "socket_closed"
                        self._failure_reason = "socket_closed"
                    return
                buffer.extend(chunk)
                frames, dropped = pop_frames(buffer)
                self._bytes_received += len(chunk)
                self._packets_received += 1
                self._dropped_sync_bytes += dropped
                for frame in frames:
                    try:
                        values = parse_frame(frame)
                    except ValueError:
                        self._parse_errors += 1
                        continue
                    if self._raw_handle is not None:
                        self._raw_handle.write(frame)
                    with self._lock:
                        self._samples.append((time.monotonic(), values))
        except OSError as exc:
            with self._lock:
                self._status = "connect_or_socket_error"
                self._failure_reason = f"{type(exc).__name__}: {exc}"

    def _recent_locked(self, now: float) -> list[tuple[float, tuple[float, ...]]]:
        earliest = now - self.config.window_s
        return [entry for entry in self._samples if entry[0] >= earliest]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a persistent no-motion Kunwei KWR75B monitor gate.")
    parser.add_argument("--sensor-ip", default="192.168.50.25")
    parser.add_argument("--sensor-port", type=int, default=5152)
    parser.add_argument("--duration-s", type=float, default=2.0)
    parser.add_argument("--ready-timeout-s", type=float, default=3.0)
    parser.add_argument("--window-s", type=float, default=0.5)
    parser.add_argument("--latest-max-age-s", type=float, default=0.25)
    parser.add_argument("--min-recent-samples", type=int, default=20)
    parser.add_argument("--max-force-delta-n", type=float, default=8.0)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--raw-frames", type=Path, default=None)
    args = parser.parse_args(argv)

    monitor = KunweiPersistentMonitor(
        KunweiMonitorConfig(
            sensor_ip=args.sensor_ip,
            sensor_port=args.sensor_port,
            window_s=args.window_s,
            latest_max_age_s=args.latest_max_age_s,
            min_recent_samples=args.min_recent_samples,
            max_force_delta_n=args.max_force_delta_n,
            raw_frames_path=args.raw_frames,
        )
    )
    ok = False
    try:
        monitor.start()
        ok = monitor.wait_ready(args.ready_timeout_s)
        if ok and args.duration_s > 0.0:
            deadline = time.monotonic() + args.duration_s
            while time.monotonic() < deadline:
                monitor.assert_fresh_and_within_force_delta()
                time.sleep(0.05)
    finally:
        monitor.stop()
        monitor.write_summary(args.summary, {"live_motion": False, "sent_goal": False})
    print(json.dumps(monitor.snapshot(), indent=2, sort_keys=True))
    return 0 if ok else 3


def _mean_tuple(samples: list[tuple[float, ...]]) -> tuple[float, ...]:
    width = float(len(samples))
    return tuple(sum(sample[index] for sample in samples) / width for index in range(len(FIELDS)))


def _force_delta_n(sample: tuple[float, ...], baseline: tuple[float, ...]) -> float:
    deltas = [(sample[index] - baseline[index]) * FORCE_KG_TO_N for index in range(3)]
    return math.sqrt(sum(delta * delta for delta in deltas))


def _si_payload(values: tuple[float, ...]) -> dict[str, float]:
    force_n = [value * FORCE_KG_TO_N for value in values[:3]]
    moment_nm = [value * MOMENT_KG_M_TO_NM for value in values[3:]]
    return {
        "Fx_N": force_n[0],
        "Fy_N": force_n[1],
        "Fz_N": force_n[2],
        "Mx_Nm": moment_nm[0],
        "My_Nm": moment_nm[1],
        "Mz_Nm": moment_nm[2],
        "force_norm_N": math.sqrt(sum(value * value for value in force_n)),
    }


if __name__ == "__main__":
    raise SystemExit(main())
