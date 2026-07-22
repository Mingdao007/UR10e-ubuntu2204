"""Strict Dashboard observations and exact-program load for Step5d V3."""

from __future__ import annotations

import socket
import time
from pathlib import PurePosixPath
from typing import Any, Callable, Iterable


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


class DashboardProgramLoadError(DashboardObservationError):
    def __init__(self, observation: dict[str, Any]) -> None:
        super().__init__(
            f"{observation['reason_code']}: {observation.get('detail') or 'program load failed'}"
        )
        self.observation = observation


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


def _exact_program_path(value: str) -> str:
    if not isinstance(value, str) or not value.isascii() or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise ValueError("Dashboard program path must be safe ASCII")
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path.as_posix() != value
        or len(path.parts) < 3
        or path.parts[1] != "programs"
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.lower() != ".urp"
    ):
        raise ValueError("Dashboard program path must be an absolute normalized .urp under /programs")
    return value


def _loaded_program(response: str) -> str:
    prefix = "Loaded program:"
    if not response.startswith(prefix):
        return ""
    return response[len(prefix) :].strip()


def _program_load_observation(
    *,
    ok: bool,
    reason_code: str,
    blocker_class: str | None,
    observed_at_unix_ns: int,
    host: str,
    port: int,
    expected_program: str,
    before: str | None,
    load_attempted: bool,
    load_response: str | None,
    after: str | None,
    detail: str | None,
) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v3/program-load-observation-v1",
        "ok": ok,
        "reason_code": reason_code,
        "blocker_class": blocker_class,
        "observed_at_unix_ns": observed_at_unix_ns,
        "release_binding": None,
        "dashboard_endpoint": {"host": host, "port": port},
        "expected_program": expected_program,
        "before_get_loaded_program": before,
        "load_attempted": load_attempted,
        "load_response": load_response,
        "after_get_loaded_program": after,
        "detail": detail,
    }


def ensure_exact_loaded_program(
    host: str,
    expected_program: str,
    port: int = 29999,
    timeout: float = 3.0,
    *,
    connector: Callable[..., socket.socket] = socket.create_connection,
) -> dict[str, Any]:
    """Load one exact manifest-bound TP program and prove it with a fresh GET.

    This is intentionally narrower than :func:`dashboard_exchange`: callers
    cannot submit an arbitrary Dashboard write command, and this function
    never sends ``play`` or ``stop``.
    """

    observed_at = time.time_ns()
    try:
        expected = _exact_program_path(expected_program)
    except ValueError as exc:
        observation = _program_load_observation(
            ok=False,
            reason_code="PROGRAM_LOAD_BINDING_INVALID",
            blocker_class="INTERNAL",
            observed_at_unix_ns=observed_at,
            host=host,
            port=port,
            expected_program=str(expected_program),
            before=None,
            load_attempted=False,
            load_response=None,
            after=None,
            detail=str(exc),
        )
        raise DashboardProgramLoadError(observation) from exc

    before_response: str | None = None
    before_program: str | None = None
    load_attempted = False
    load_response: str | None = None
    try:
        before_response = dashboard_exchange(
            host,
            ["get loaded program"],
            port=port,
            timeout=timeout,
            connector=connector,
        )["get loaded program"]
        before_program = _loaded_program(before_response)
    except (OSError, DashboardObservationError) as exc:
        observation = _program_load_observation(
            ok=False,
            reason_code="DASHBOARD_UNREACHABLE",
            blocker_class="BLOCKED_EXTERNAL",
            observed_at_unix_ns=observed_at,
            host=host,
            port=port,
            expected_program=expected,
            before=before_response,
            load_attempted=False,
            load_response=None,
            after=None,
            detail=f"initial fresh get loaded program failed: {type(exc).__name__}:{exc}",
        )
        raise DashboardProgramLoadError(observation) from exc

    if before_program != expected:
        load_attempted = True
        command = f"load {expected}"
        try:
            with connector((host, port), timeout=timeout) as sock:
                sock.sendall((command + "\n").encode("ascii"))
                load_response = _matching_line(
                    sock,
                    (
                        "Loading program:",
                        "Error while loading program:",
                        "Failed to load program:",
                        "File not found:",
                        "Cannot load program:",
                        "Command failed:",
                    ),
                    timeout_s=timeout,
                )
        except (OSError, DashboardObservationError) as exc:
            observation = _program_load_observation(
                ok=False,
                reason_code="DASHBOARD_LOAD_RESPONSE_INVALID",
                blocker_class="BLOCKED_EXTERNAL",
                observed_at_unix_ns=observed_at,
                host=host,
                port=port,
                expected_program=expected,
                before=before_response,
                load_attempted=True,
                load_response=load_response,
                after=None,
                detail=f"Dashboard load exchange failed: {type(exc).__name__}:{exc}",
            )
            raise DashboardProgramLoadError(observation) from exc
        if not load_response.startswith("Loading program:"):
            observation = _program_load_observation(
                ok=False,
                reason_code="DASHBOARD_LOAD_REJECTED",
                blocker_class="BLOCKED_EXTERNAL",
                observed_at_unix_ns=observed_at,
                host=host,
                port=port,
                expected_program=expected,
                before=before_response,
                load_attempted=True,
                load_response=load_response,
                after=None,
                detail="controller rejected the exact manifest-bound program load",
            )
            raise DashboardProgramLoadError(observation)

    after_response: str | None = None
    try:
        after_response = dashboard_exchange(
            host,
            ["get loaded program"],
            port=port,
            timeout=timeout,
            connector=connector,
        )["get loaded program"]
    except (OSError, DashboardObservationError) as exc:
        observation = _program_load_observation(
            ok=False,
            reason_code="DASHBOARD_UNREACHABLE",
            blocker_class="BLOCKED_EXTERNAL",
            observed_at_unix_ns=observed_at,
            host=host,
            port=port,
            expected_program=expected,
            before=before_response,
            load_attempted=load_attempted,
            load_response=load_response,
            after=after_response,
            detail=f"post-load fresh get loaded program failed: {type(exc).__name__}:{exc}",
        )
        raise DashboardProgramLoadError(observation) from exc
    if _loaded_program(after_response) != expected:
        observation = _program_load_observation(
            ok=False,
            reason_code="DASHBOARD_POST_LOAD_MISMATCH",
            blocker_class="BLOCKED_EXTERNAL",
            observed_at_unix_ns=observed_at,
            host=host,
            port=port,
            expected_program=expected,
            before=before_response,
            load_attempted=load_attempted,
            load_response=load_response,
            after=after_response,
            detail="fresh get loaded program did not exactly match the release target",
        )
        raise DashboardProgramLoadError(observation)
    return _program_load_observation(
        ok=True,
        reason_code="DASHBOARD_PROGRAM_LOAD_VERIFIED",
        blocker_class=None,
        observed_at_unix_ns=observed_at,
        host=host,
        port=port,
        expected_program=expected,
        before=before_response,
        load_attempted=load_attempted,
        load_response=load_response,
        after=after_response,
        detail=None,
    )


__all__ = [
    "DashboardObservationError",
    "DashboardProgramLoadError",
    "dashboard_exchange",
    "ensure_exact_loaded_program",
]
