"""Strict read-only Dashboard observations for Step5d V3."""

from __future__ import annotations

import socket
import time
from typing import Callable, Iterable


_PREFIXES = {
    "PolyscopeVersion": ("URSoftware", "PolyscopeVersion"),
    "get loaded program": ("Loaded program:",),
    "is in remote control": ("true", "false"),
    "programState": ("PLAYING", "PAUSED", "STOPPED"),
    "robotmode": ("Robotmode:",),
    "running": ("Program running:",),
    "safetymode": ("Safetymode:",),
}


class DashboardObservationError(RuntimeError):
    pass


def _matching_line(
    sock: socket.socket,
    prefixes: Iterable[str],
    *,
    timeout_s: float,
) -> str:
    accepted = tuple(prefixes)
    deadline = time.monotonic() + timeout_s
    buffered = b""
    seen: list[str] = []
    sock.settimeout(min(0.05, timeout_s))
    while time.monotonic() < deadline:
        while b"\n" in buffered:
            raw, buffered = buffered.split(b"\n", 1)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            seen.append(line)
            if line.startswith(accepted):
                return line
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            break
        buffered += chunk
    raise DashboardObservationError(
        f"Dashboard response timeout; expected={accepted!r}; seen={seen!r}"
    )


def dashboard_exchange(
    host: str,
    commands: list[str],
    port: int = 29999,
    timeout: float = 3.0,
    *,
    connector: Callable[..., socket.socket] = socket.create_connection,
) -> dict[str, str]:
    if timeout <= 0.0:
        raise ValueError("Dashboard timeout must be positive")
    responses: dict[str, str] = {}
    for command in commands:
        prefixes = _PREFIXES.get(command)
        if prefixes is None:
            raise DashboardObservationError(
                f"unsupported read-only Dashboard command: {command}"
            )
        with connector((host, port), timeout=timeout) as sock:
            sock.sendall((command + "\n").encode("ascii"))
            responses[command] = _matching_line(
                sock,
                prefixes,
                timeout_s=timeout,
            )
    return responses


__all__ = [
    "DashboardObservationError",
    "dashboard_exchange",
]
