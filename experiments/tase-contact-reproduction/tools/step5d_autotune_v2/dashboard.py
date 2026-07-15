"""Framed, one-command-per-connection UR Dashboard client."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Callable, Iterable


GREETING_PREFIX = "Connected: Universal Robots Dashboard Server"


class DashboardError(RuntimeError):
    """Raised when Dashboard framing or the expected response contract fails."""


@dataclass(frozen=True)
class DashboardResponse:
    command: str
    matched_line: str
    lines: tuple[str, ...]


class DashboardClient:
    def __init__(
        self,
        host: str,
        port: int = 29999,
        *,
        connect_timeout_s: float = 1.0,
        response_timeout_s: float = 2.0,
        socket_factory: Callable[..., socket.socket] = socket.create_connection,
    ) -> None:
        if not host or not (1 <= port <= 65535):
            raise DashboardError("Dashboard endpoint is invalid")
        self.host = host
        self.port = port
        self.connect_timeout_s = connect_timeout_s
        self.response_timeout_s = response_timeout_s
        self.socket_factory = socket_factory

    def command(self, command: str, expected_prefixes: Iterable[str]) -> DashboardResponse:
        if not command or "\n" in command or "\r" in command:
            raise DashboardError("Dashboard command must be one non-empty line")
        prefixes = tuple(expected_prefixes)
        if not prefixes or any(not prefix for prefix in prefixes):
            raise DashboardError("at least one expected response prefix is required")
        deadline = time.monotonic() + self.response_timeout_s
        lines: list[str] = []
        buffer = bytearray()
        try:
            connection = self.socket_factory(
                (self.host, self.port), timeout=self.connect_timeout_s
            )
        except OSError as exc:
            raise DashboardError(f"Dashboard connect failed: {exc}") from exc
        with connection:
            connection.settimeout(max(0.01, min(self.response_timeout_s, 0.25)))
            connection.sendall(command.encode("ascii") + b"\n")
            while time.monotonic() < deadline:
                try:
                    chunk = connection.recv(4096)
                except socket.timeout:
                    continue
                except OSError as exc:
                    raise DashboardError(f"Dashboard receive failed: {exc}") from exc
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    try:
                        line = raw.rstrip(b"\r").decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise DashboardError("Dashboard response is not UTF-8") from exc
                    if not line:
                        continue
                    lines.append(line)
                    if line.startswith(prefixes):
                        return DashboardResponse(command, line, tuple(lines))
                    if line.startswith(GREETING_PREFIX):
                        continue
            if buffer:
                try:
                    line = bytes(buffer).rstrip(b"\r").decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DashboardError("Dashboard response is not UTF-8") from exc
                if line:
                    lines.append(line)
                    if line.startswith(prefixes):
                        return DashboardResponse(command, line, tuple(lines))
        raise DashboardError(
            f"Dashboard response timeout; expected={prefixes!r}; seen={tuple(lines)!r}"
        )

    def snapshot(self) -> dict[str, str]:
        contracts = (
            ("programState", ("PLAYING", "PAUSED", "STOPPED")),
            ("safetymode", ("Safetymode:",)),
            ("robotmode", ("Robotmode:",)),
            ("get loaded program", ("Loaded program:", "No program loaded")),
        )
        return {
            command: self.command(command, prefixes).matched_line
            for command, prefixes in contracts
        }
