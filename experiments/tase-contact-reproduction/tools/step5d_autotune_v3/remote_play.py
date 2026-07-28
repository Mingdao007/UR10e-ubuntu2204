"""Governed one-shot Dashboard Play for the active Step5d V3 Remote route."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import time
from typing import Any, Callable, Mapping

from .atomic_io import atomic_bytes
from .dashboard import dashboard_exchange


REMOTE_PLAY_SCHEMA = "step5d.autotune-v3/remote-play-receipt-v1"
STATUS_SCHEMA = "step5d.bridge/governed-status-v3"
STATUS_MAX_AGE_NS = 1_000_000_000

_REQUIRED_TRUE_PREDICATES = (
    "bench_ready",
    "bridge_heartbeat_fresh",
    "bridge_process_alive",
    "canonical_attempt_bound",
    "controller_fresh",
    "controller_fresh_get",
    "controller_triplet_verified",
    "kunwei_fresh",
    "lease_valid",
    "loaded_program_verified",
    "mailbox_clean",
    "next_arm_published",
    "play_identity_rechecked",
    "play_prompt_ready",
    "release_contract_proven",
    "release_current",
    "rtde_fresh",
    "rtde_recipe_capable",
    "single_writer",
    "tp_runtime_identity_verified",
    "uploaded_identity_verified",
)

_DASHBOARD_OBSERVATION_COMMANDS = [
    "is in remote control",
    "safetymode",
    "robotmode",
    "running",
    "programState",
    "get loaded program",
]


class RemotePlayError(RuntimeError):
    """The governed Remote Play primitive failed closed."""


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _dashboard_write(
    host: str,
    command: str,
    *,
    port: int = 29999,
    timeout_s: float = 3.0,
    connector: Callable[..., socket.socket] = socket.create_connection,
) -> str:
    expected_prefixes = {
        "play": ("Starting program",),
        "stop": ("Stopped",),
    }
    prefixes = expected_prefixes.get(command)
    if prefixes is None:
        raise RemotePlayError(f"unsupported Dashboard write: {command}")
    deadline = time.monotonic() + timeout_s
    buffered = b""
    seen: list[str] = []
    with connector((host, port), timeout=timeout_s) as sock:
        sock.settimeout(min(0.05, timeout_s))
        sock.sendall((command + "\n").encode("ascii"))
        while time.monotonic() < deadline:
            while b"\n" in buffered:
                raw, buffered = buffered.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                seen.append(line)
                if line.startswith(prefixes):
                    return line
                if line.lower().startswith(("failed", "cannot", "not allowed")):
                    raise RemotePlayError(
                        f"Dashboard {command} refused: {line}"
                    )
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            buffered += chunk
    raise RemotePlayError(
        f"Dashboard {command} response timeout; expected={prefixes!r}; seen={seen!r}"
    )


def _expected_program(status: Mapping[str, Any]) -> str:
    controller = status.get("controller")
    if not isinstance(controller, Mapping):
        raise RemotePlayError("governed status lacks controller evidence")
    loaded = controller.get("loaded")
    if not isinstance(loaded, Mapping) or loaded.get("verified") is not True:
        raise RemotePlayError("loaded-program identity is not verified")
    expected = loaded.get("expected")
    observed = loaded.get("observed")
    if not isinstance(expected, str) or expected != observed:
        raise RemotePlayError("loaded-program identity differs")
    return expected


def _validate_status(
    status: Mapping[str, Any],
    *,
    now_ns: int,
) -> tuple[str, str, str]:
    if status.get("schema") != STATUS_SCHEMA:
        raise RemotePlayError("governed status schema differs")
    if status.get("compatibility_phase") != "WAITING_FOR_PLAY":
        raise RemotePlayError("bridge is not waiting for Play")
    if status.get("state") != "BENCH_READY":
        raise RemotePlayError("bench is not ready")
    generated = status.get("generated_at_unix_ns")
    if (
        isinstance(generated, bool)
        or not isinstance(generated, int)
        or not 0 <= now_ns - generated <= STATUS_MAX_AGE_NS
    ):
        raise RemotePlayError("governed status is stale")
    predicates = status.get("predicates")
    if not isinstance(predicates, Mapping):
        raise RemotePlayError("governed predicates are missing")
    missing = [
        name for name in _REQUIRED_TRUE_PREDICATES
        if predicates.get(name) is not True
    ]
    if missing:
        raise RemotePlayError(
            "governed Play predicates are false: " + ",".join(missing)
        )
    if predicates.get("play_observed") is not False:
        raise RemotePlayError("Play was already observed")
    blocker = status.get("blocker")
    if (
        not isinstance(blocker, Mapping)
        or blocker.get("class") is not None
        or blocker.get("reason_codes") != []
    ):
        raise RemotePlayError("governed status contains a blocker")
    bridge = status.get("bridge")
    if (
        not isinstance(bridge, Mapping)
        or bridge.get("alive") is not True
        or bridge.get("heartbeat_fresh") is not True
    ):
        raise RemotePlayError("bridge liveness differs")
    lease = status.get("campaign_lease")
    if not isinstance(lease, Mapping) or lease.get("valid") is not True:
        raise RemotePlayError("campaign lease is not valid")
    release = status.get("release")
    if (
        not isinstance(release, Mapping)
        or release.get("valid") is not True
        or not isinstance(release.get("sha256"), str)
    ):
        raise RemotePlayError("current release is not valid")
    attempt = status.get("launch_attempt")
    if (
        not isinstance(attempt, Mapping)
        or attempt.get("present") is not True
        or attempt.get("state") != "STARTED"
        or not isinstance(attempt.get("attempt_id"), str)
    ):
        raise RemotePlayError("canonical launch attempt is not active")
    return (
        _expected_program(status),
        str(attempt["attempt_id"]),
        str(release["sha256"]),
    )


def _validate_pre_play_observation(
    dashboard: Mapping[str, str],
    *,
    expected_program: str,
) -> None:
    if dashboard.get("is in remote control", "").strip().lower() != "true":
        raise RemotePlayError("controller is not in Remote Control")
    if "NORMAL" not in dashboard.get("safetymode", ""):
        raise RemotePlayError("Dashboard safety is not NORMAL")
    if "RUNNING" not in dashboard.get("robotmode", ""):
        raise RemotePlayError("robot mode is not RUNNING")
    if dashboard.get("running", "").strip().lower() != "program running: false":
        raise RemotePlayError("another program is already running")
    if not dashboard.get("programState", "").startswith("STOPPED"):
        raise RemotePlayError("loaded program is not stopped")
    if dashboard.get("get loaded program") != f"Loaded program: {expected_program}":
        raise RemotePlayError("fresh Dashboard loaded program differs")


def _post_play_observed(
    dashboard: Mapping[str, str],
    *,
    expected_program: str,
) -> bool:
    return (
        dashboard.get("is in remote control", "").strip().lower() == "true"
        and "NORMAL" in dashboard.get("safetymode", "")
        and dashboard.get("running", "").strip().lower() == "program running: true"
        and dashboard.get("programState", "").startswith("PLAYING")
        and dashboard.get("get loaded program") == f"Loaded program: {expected_program}"
    )


def governed_remote_play(
    experiment_root: Path,
    *,
    robot_host: str,
    evidence_output: Path | None = None,
    dashboard_port: int = 29999,
    dashboard_timeout_s: float = 3.0,
    observe_timeout_s: float = 3.0,
    status_resolver: Callable[[Path], Mapping[str, Any]],
    dashboard_observer: Callable[..., dict[str, str]] = dashboard_exchange,
    dashboard_writer: Callable[..., str] = _dashboard_write,
    now_ns: Callable[[], int] = time.time_ns,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Validate the active attempt, send one Play, and persist its observation."""

    root = experiment_root.expanduser().resolve(strict=True)
    status = dict(status_resolver(root))
    observed_at = now_ns()
    expected_program, attempt_id, release_sha = _validate_status(
        status,
        now_ns=observed_at,
    )
    before = dashboard_observer(
        robot_host,
        _DASHBOARD_OBSERVATION_COMMANDS,
        port=dashboard_port,
        timeout=dashboard_timeout_s,
    )
    _validate_pre_play_observation(before, expected_program=expected_program)

    response = dashboard_writer(
        robot_host,
        "play",
        port=dashboard_port,
        timeout_s=dashboard_timeout_s,
    )
    deadline = monotonic() + observe_timeout_s
    after: dict[str, str] | None = None
    post_error: Exception | None = None
    while monotonic() < deadline:
        try:
            candidate = dashboard_observer(
                robot_host,
                _DASHBOARD_OBSERVATION_COMMANDS,
                port=dashboard_port,
                timeout=dashboard_timeout_s,
            )
            after = candidate
            if _post_play_observed(candidate, expected_program=expected_program):
                break
        except Exception as exc:  # preserve fail-closed compensation below
            post_error = exc
        sleeper(0.02)
    if after is None or not _post_play_observed(
        after,
        expected_program=expected_program,
    ):
        stop_result = "not_attempted"
        try:
            stop_result = dashboard_writer(
                robot_host,
                "stop",
                port=dashboard_port,
                timeout_s=dashboard_timeout_s,
            )
        except Exception as exc:
            stop_result = f"stop_failed:{type(exc).__name__}:{exc}"
        detail = (
            f"; last_error={type(post_error).__name__}:{post_error}"
            if post_error is not None
            else ""
        )
        raise RemotePlayError(
            f"Play was not confirmed; compensation={stop_result}; after={after!r}{detail}"
        )

    bindings = status["launch_attempt"].get("bindings")
    if not isinstance(bindings, Mapping):
        raise RemotePlayError("launch attempt bindings are missing")
    output_root = bindings.get("output_root")
    if evidence_output is None:
        if not isinstance(output_root, str):
            raise RemotePlayError("launch output root is missing")
        evidence_output = Path(output_root) / "remote-play-receipt.json"
    receipt = {
        "schema": REMOTE_PLAY_SCHEMA,
        "status": "play_observed",
        "attempt_id": attempt_id,
        "release_manifest_sha256": release_sha,
        "expected_program": expected_program,
        "governed_status_sha256": hashlib.sha256(
            _canonical_json_bytes(status)
        ).hexdigest(),
        "observed_at_unix_ns": observed_at,
        "play_observed_at_unix_ns": now_ns(),
        "dashboard_before": before,
        "dashboard_write_response": response,
        "dashboard_after": after,
        "evidence_output": str(evidence_output),
    }
    atomic_bytes(
        evidence_output,
        json.dumps(
            receipt,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8") + b"\n",
    )
    return receipt


__all__ = [
    "REMOTE_PLAY_SCHEMA",
    "RemotePlayError",
    "governed_remote_play",
]
