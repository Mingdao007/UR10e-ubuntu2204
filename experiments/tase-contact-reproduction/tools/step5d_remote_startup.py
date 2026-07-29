#!/usr/bin/env python3
"""Remote Control startup primitives for the Step5d campaign handoff.

The classes in this module are deliberately narrow consumers of the handoff
contracts.  Dashboard writes are allow-listed per exact program identity,
Home observation uses RTDE output recipes only, and the bridge/campaign seams
delegate readiness and Play admission to the existing canonical primitives.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
import signal
import socket
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence

from step5d_autotune_v3.dashboard import dashboard_exchange
from step5d_autotune_v3.governance import read_proc_starttime_ticks
from step5d_autotune_v3.remote_play import REMOTE_PLAY_SCHEMA, governed_remote_play
from step5d_autotune_v3.rtde_client import RTDEClient
from step5d_bridge_status import readiness_claim, resolve_status, verify_readiness_claim
from step5d_no_tube_handoff import (
    HandoffError,
    HandoffManifest,
    SCRIPT1_PROGRAM,
    build_home_verified_receipt,
    validate_release_binding,
)


HOME_RTDE_FIELDS = (
    "timestamp",
    "actual_TCP_pose",
    "actual_TCP_speed",
    "actual_q",
    "actual_qd",
)
_DASHBOARD_COMMANDS = [
    "is in remote control",
    "safetymode",
    "robotmode",
    "running",
    "programState",
    "get loaded program",
]
_DASHBOARD_SUCCESS_PREFIXES = {
    "play": ("Starting program",),
    "stop": ("Stopped",),
}
_LOAD_SUCCESS_PREFIX = "Loading program"


class RemoteStartupError(HandoffError):
    """A Remote Control startup primitive failed closed."""


class RemoteStartupPending(RemoteStartupError):
    """The bounded startup observation is not ready yet."""


class RemoteStartupTerminal(RemoteStartupError):
    """The current attempt violated an immutable startup contract."""


@dataclass(frozen=True)
class DashboardWriteOutcome:
    """Receipt for one allow-listed Dashboard write."""

    command: str
    response: str
    command_sent: bool


class DashboardWriteError(RemoteStartupError):
    """A Dashboard write failed with an explicit send boundary."""

    def __init__(
        self,
        message: str,
        *,
        command: str,
        command_sent: bool,
        response: str | None = None,
    ) -> None:
        super().__init__(message)
        self.command = command
        self.command_sent = command_sent
        self.response = response


def _loaded_target(observation: Mapping[str, Any]) -> str:
    value = observation.get("get loaded program")
    if not isinstance(value, str) or not value.startswith("Loaded program: "):
        raise RemoteStartupError("Dashboard loaded-program response is malformed")
    return value.removeprefix("Loaded program: ").strip()


def _program_state(observation: Mapping[str, Any]) -> str:
    value = observation.get("programState")
    if not isinstance(value, str) or not value:
        raise RemoteStartupError("Dashboard programState response is malformed")
    return value


def _require_remote_safety_robot(observation: Mapping[str, Any]) -> None:
    if observation.get("is in remote control", "").strip().lower() != "true":
        raise RemoteStartupError("controller is not in Remote Control")
    if "NORMAL" not in str(observation.get("safetymode", "")):
        raise RemoteStartupError("Dashboard safety is not NORMAL")
    if "RUNNING" not in str(observation.get("robotmode", "")):
        raise RemoteStartupError("robot mode is not RUNNING")


def _program_running(observation: Mapping[str, Any]) -> bool:
    value = observation.get("running")
    if not isinstance(value, str) or not value.lower().startswith("program running:"):
        raise RemoteStartupError("Dashboard running response is malformed")
    normalized = value.split(":", 1)[1].strip().lower()
    if normalized not in {"true", "false"}:
        raise RemoteStartupError("Dashboard running response is not boolean")
    return normalized == "true"


def _dashboard_contract(
    observation: Mapping[str, Any],
    *,
    expected_target: str,
    expected_running: bool | None = None,
    expected_stopped: bool | None = None,
) -> dict[str, Any]:
    _require_remote_safety_robot(observation)
    loaded = _loaded_target(observation)
    if loaded != expected_target:
        raise RemoteStartupError(
            f"Dashboard loaded program differs: expected={expected_target!r} observed={loaded!r}"
        )
    running = _program_running(observation)
    state = _program_state(observation)
    if expected_running is not None and running is not expected_running:
        raise RemoteStartupError(
            f"Dashboard running state differs: expected={expected_running} observed={running}"
        )
    stopped = state.startswith("STOPPED")
    if expected_stopped is not None and stopped is not expected_stopped:
        raise RemoteStartupError(
            f"Dashboard program state differs: expected_stopped={expected_stopped} observed={state!r}"
        )
    return {
        "remote_control": True,
        "safety_mode": "NORMAL",
        "robot_mode": "RUNNING",
        "program_running": running,
        "program_state": state,
        "controller_target": loaded,
        "dashboard": dict(observation),
    }


class RemoteDashboardWriter:
    """Allow-list Dashboard writes for one exact startup binding.

    The generic ``write`` method is intentionally the only write seam.  It
    accepts ``play`` and bounded ``stop`` compensation, plus ``load`` for the
    one exact target supplied at construction.  It cannot issue arbitrary
    Dashboard commands or load a different program.
    """

    def __init__(
        self,
        host: str,
        *,
        load_target: str,
        port: int = 29999,
        timeout_s: float = 3.0,
        connector: Callable[..., socket.socket] = socket.create_connection,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not host:
            raise ValueError("Remote Control robot host is required")
        if not load_target.startswith("/programs/") or not load_target.endswith(".urp"):
            raise ValueError("Dashboard load target must be an exact controller .urp path")
        if port <= 0 or timeout_s <= 0.0:
            raise ValueError("Dashboard writer bounds must be positive")
        self.host = host
        self.load_target = load_target
        self.port = port
        self.timeout_s = timeout_s
        self.connector = connector
        self.monotonic = monotonic
        self.last_command_sent: str | None = None

    def _response_prefixes(self, command: str) -> tuple[str, ...]:
        if command == f"load {self.load_target}":
            return (_LOAD_SUCCESS_PREFIX,)
        prefixes = _DASHBOARD_SUCCESS_PREFIXES.get(command)
        if prefixes is None:
            raise RemoteStartupError(f"unsupported Dashboard write: {command}")
        return prefixes

    def write(self, command: str) -> DashboardWriteOutcome:
        if not isinstance(command, str):
            raise RemoteStartupError("Dashboard write command must be a string")
        self.last_command_sent = None
        prefixes = self._response_prefixes(command)
        deadline = self.monotonic() + self.timeout_s
        buffered = b""
        seen: list[str] = []
        command_sent = False
        try:
            with self.connector((self.host, self.port), timeout=self.timeout_s) as sock:
                sock.settimeout(min(0.05, self.timeout_s))
                try:
                    sock.sendall((command + "\n").encode("ascii"))
                except Exception as exc:
                    raise DashboardWriteError(
                        f"Dashboard write could not send command={command!r}: {exc}",
                        command=command,
                        command_sent=False,
                    ) from exc
                command_sent = True
                self.last_command_sent = command
                while self.monotonic() < deadline:
                    while b"\n" in buffered:
                        raw, buffered = buffered.split(b"\n", 1)
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        seen.append(line)
                        if line.startswith(prefixes):
                            return DashboardWriteOutcome(
                                command=command,
                                response=line,
                                command_sent=True,
                            )
                        if line.lower().startswith(("failed", "cannot", "not allowed")):
                            raise DashboardWriteError(
                                f"Dashboard write refused: command={command!r} response={line!r}",
                                command=command,
                                command_sent=True,
                                response=line,
                            )
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    buffered += chunk
            raise DashboardWriteError(
                f"Dashboard write response timeout; command={command!r} expected={prefixes!r} seen={seen!r}",
                command=command,
                command_sent=command_sent,
            )
        except DashboardWriteError:
            raise
        except Exception as exc:
            raise DashboardWriteError(
                f"Dashboard write failed: command={command!r}: {exc}",
                command=command,
                command_sent=command_sent,
            ) from exc


class _ExactLoadAdapter:
    def __init__(
        self,
        *,
        host: str,
        target: str,
        program_id: str,
        dashboard_observer: Callable[..., dict[str, str]],
        writer: RemoteDashboardWriter,
        dashboard_port: int,
        dashboard_timeout_s: float,
        observe_timeout_s: float,
        poll_interval_s: float,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> None:
        self.host = host
        self.target = target
        self.program_id = program_id
        self.dashboard_observer = dashboard_observer
        self.writer = writer
        self.dashboard_port = dashboard_port
        self.dashboard_timeout_s = dashboard_timeout_s
        self.observe_timeout_s = observe_timeout_s
        self.poll_interval_s = poll_interval_s
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.play_issued = False

    def _observe(self) -> dict[str, str]:
        return self.dashboard_observer(
            self.host,
            _DASHBOARD_COMMANDS,
            port=self.dashboard_port,
            timeout=self.dashboard_timeout_s,
        )

    def _wait(
        self,
        *,
        expected_running: bool,
        expected_stopped: bool,
    ) -> dict[str, Any]:
        deadline = self.monotonic() + self.observe_timeout_s
        last_error: Exception | None = None
        while self.monotonic() < deadline:
            try:
                observation = self._observe()
                # Identity and safety failures are hard blockers.  A
                # running/stopped mismatch is a bounded transition and is
                # therefore polled until the target state is observed.
                contract = _dashboard_contract(
                    observation,
                    expected_target=self.target,
                )
                if (
                    contract["program_running"] is expected_running
                    and contract["program_state"].startswith("STOPPED") is expected_stopped
                ):
                    return {**contract, "dashboard": dict(observation)}
                last_error = RemoteStartupError(
                    f"Dashboard target state pending: running={contract['program_running']} "
                    f"state={contract['program_state']!r}"
                )
            except Exception as exc:
                if isinstance(exc, RemoteStartupError) and any(
                    phrase in str(exc)
                    for phrase in (
                        "not in Remote Control",
                        "safety is not NORMAL",
                        "robot mode is not RUNNING",
                        "loaded program differs",
                    )
                ):
                    raise
                last_error = exc
            self.sleeper(self.poll_interval_s)
        detail = "" if last_error is None else f" last_error={type(last_error).__name__}:{last_error}"
        raise RemoteStartupError(
            f"Dashboard state was not observed for {self.target!r} before timeout.{detail}"
        )

    def load(self) -> dict[str, Any]:
        before = self._observe()
        before_contract = _dashboard_contract(
            before,
            expected_target=_loaded_target(before),
            expected_running=False,
        )
        write_outcome = self.writer.write(f"load {self.target}")
        if not isinstance(write_outcome, DashboardWriteOutcome):
            raise RemoteStartupError("Dashboard load writer returned a malformed outcome")
        after = self._wait(expected_running=False, expected_stopped=True)
        return {
            "action": "LOAD",
            "accepted": True,
            "program_id": self.program_id,
            "controller_target": self.target,
            "safety_mode": "NORMAL",
            "program_running": False,
            "remote_control": True,
            "robot_mode": "RUNNING",
            "dashboard_before": before_contract,
            "dashboard_write_response": write_outcome.response,
            "dashboard_after": after,
        }

    def play(self) -> dict[str, Any]:
        self.play_issued = False
        before = self._observe()
        _dashboard_contract(
            before,
            expected_target=self.target,
            expected_running=False,
            expected_stopped=True,
        )
        try:
            write_outcome = self.writer.write("play")
            if not isinstance(write_outcome, DashboardWriteOutcome):
                raise RemoteStartupError("Dashboard Play writer returned a malformed outcome")
            self.play_issued = write_outcome.command_sent
            after = self._wait(expected_running=True, expected_stopped=False)
        except DashboardWriteError as exc:
            self.play_issued = exc.command == "play" and exc.command_sent
            compensation = "not_attempted"
            if self.play_issued:
                try:
                    compensation = self.writer.write("stop").response
                except Exception as stop_exc:
                    compensation = f"stop_failed:{type(stop_exc).__name__}:{stop_exc}"
            raise RemoteStartupError(
                f"Script 1 Play was not confirmed; compensation={compensation}; "
                f"cause={type(exc).__name__}:{exc}"
            ) from exc
        except Exception as exc:
            compensation = "not_attempted"
            # A post-send observation failure is compensatable.  The writer's
            # explicit outcome has already set this observable before _wait;
            # a connect/sendall failure leaves it false.
            if self.play_issued:
                try:
                    compensation = self.writer.write("stop").response
                except Exception as stop_exc:
                    compensation = f"stop_failed:{type(stop_exc).__name__}:{stop_exc}"
            raise RemoteStartupError(
                f"Script 1 Play was not confirmed; compensation={compensation}; "
                f"cause={type(exc).__name__}:{exc}"
            ) from exc
        return {
            "action": "PLAY",
            "accepted": True,
            "program_id": self.program_id,
            "controller_target": self.target,
            "safety_mode": "NORMAL",
            "program_running": True,
            "remote_control": True,
            "robot_mode": "RUNNING",
            "dashboard_before": dict(before),
            "dashboard_write_response": write_outcome.response,
            "dashboard_after": after,
        }

    def stop(self) -> str:
        return self.writer.write("stop").response


class RemoteScript1Trigger:
    """Governed Remote Load/Play/stop for the exact Script 1 package."""

    def __init__(
        self,
        *,
        robot_host: str,
        dashboard_observer: Callable[..., dict[str, str]] = dashboard_exchange,
        writer: RemoteDashboardWriter,
        dashboard_port: int = 29999,
        dashboard_timeout_s: float = 3.0,
        observe_timeout_s: float = 10.0,
        poll_interval_s: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.robot_host = robot_host
        self.dashboard_observer = dashboard_observer
        self.writer = writer
        self.dashboard_port = dashboard_port
        self.dashboard_timeout_s = dashboard_timeout_s
        self.observe_timeout_s = observe_timeout_s
        self.poll_interval_s = poll_interval_s
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._adapter: _ExactLoadAdapter | None = None
        self.play_issued = False

    def _adapter_for(self, manifest: HandoffManifest) -> _ExactLoadAdapter:
        target = manifest.payload["script1"]["controller_target"]
        if self.writer.load_target != target:
            raise RemoteStartupError("Script 1 writer binding differs from manifest")
        self._adapter = _ExactLoadAdapter(
            host=self.robot_host,
            target=target,
            program_id=SCRIPT1_PROGRAM,
            dashboard_observer=self.dashboard_observer,
            writer=self.writer,
            dashboard_port=self.dashboard_port,
            dashboard_timeout_s=self.dashboard_timeout_s,
            observe_timeout_s=self.observe_timeout_s,
            poll_interval_s=self.poll_interval_s,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )
        return self._adapter

    def load(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        self.play_issued = False
        return self._adapter_for(manifest).load()

    def play(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        adapter = self._adapter_for(manifest)
        self.play_issued = False
        try:
            return adapter.play()
        finally:
            self.play_issued = adapter.play_issued

    def stop(self) -> str:
        return self.writer.write("stop").response


class RemoteHomeObserver:
    """Read-only RTDE observer for exact Script 1 Home and stationary dwell."""

    def __init__(
        self,
        *,
        robot_host: str,
        dashboard_observer: Callable[..., dict[str, str]] = dashboard_exchange,
        rtde_factory: Callable[..., Any] = RTDEClient,
        rtde_port: int = 30004,
        dashboard_port: int = 29999,
        dashboard_timeout_s: float = 3.0,
        frequency_hz: float = 10.0,
        timeout_s: float = 30.0,
        poll_interval_s: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.robot_host = robot_host
        self.dashboard_observer = dashboard_observer
        self.rtde_factory = rtde_factory
        self.rtde_port = rtde_port
        self.dashboard_port = dashboard_port
        self.dashboard_timeout_s = dashboard_timeout_s
        self.frequency_hz = frequency_hz
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.monotonic = monotonic
        self.monotonic_ns = monotonic_ns
        self.sleeper = sleeper

    def _dashboard(self, target: str) -> dict[str, str]:
        return self.dashboard_observer(
            self.robot_host,
            _DASHBOARD_COMMANDS,
            port=self.dashboard_port,
            timeout=self.dashboard_timeout_s,
        )

    def observe(self, manifest: HandoffManifest) -> Sequence[Mapping[str, Any]]:
        target = manifest.payload["script1"]["controller_target"]
        deadline = self.monotonic() + self.timeout_s
        samples: list[dict[str, Any]] = []
        previous_controller_ns: int | None = None
        previous_observed_ns: int | None = None
        last_error: Exception | None = None
        try:
            with self.rtde_factory(
                self.robot_host,
                port=self.rtde_port,
                timeout=self.dashboard_timeout_s,
            ) as client:
                client.negotiate(version=2)
                recipe_id, type_names = client.setup_outputs(
                    self.frequency_hz,
                    HOME_RTDE_FIELDS,
                )
                client.start()
                while self.monotonic() < deadline:
                    values = client.recv_recipe_sample(recipe_id, type_names)
                    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) != len(HOME_RTDE_FIELDS):
                        raise RemoteStartupError("RTDE Home sample shape differs")
                    timestamp_s = float(values[0])
                    if not math.isfinite(timestamp_s) or timestamp_s <= 0.0:
                        raise RemoteStartupError("RTDE controller timestamp is non-finite or stale")
                    controller_ns = int(round(timestamp_s * 1_000_000_000.0))
                    observed_ns = int(self.monotonic_ns())
                    if controller_ns <= 0 or observed_ns <= 0:
                        raise RemoteStartupError("RTDE Home timestamps are invalid")
                    if previous_controller_ns is not None and controller_ns <= previous_controller_ns:
                        raise RemoteStartupError("RTDE controller timestamps are not fresh and ordered")
                    if previous_observed_ns is not None and observed_ns <= previous_observed_ns:
                        raise RemoteStartupError("Home observer timestamps are not sequential")
                    for label, vector in zip(
                        ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd"),
                        values[1:],
                    ):
                        if (
                            not isinstance(vector, Sequence)
                            or isinstance(vector, (str, bytes))
                            or len(vector) != 6
                            or not all(math.isfinite(float(item)) for item in vector)
                        ):
                            raise RemoteStartupError(
                                f"RTDE Home {label} contains a non-finite or malformed vector"
                            )
                    previous_controller_ns = controller_ns
                    previous_observed_ns = observed_ns
                    dashboard = self._dashboard(target)
                    contract = _dashboard_contract(
                        dashboard,
                        expected_target=target,
                    )
                    running = bool(contract["program_running"])
                    state = str(contract["program_state"])
                    row = {
                        "program_id": SCRIPT1_PROGRAM,
                        "program_running": running,
                        "program_state": state,
                        "safety_mode": "NORMAL",
                        "remote_control": True,
                        "robot_mode": "RUNNING",
                        "fresh": True,
                        "observed_monotonic_ns": observed_ns,
                        "controller_timestamp_ns": controller_ns,
                        "tcp_pose": values[1],
                        "tcp_speed": values[2],
                        "actual_q": values[3],
                        "qdot": values[4],
                    }
                    # Samples during Script 1 motion are observations, not
                    # Home evidence.  Wait for the controller to report a
                    # stopped program, then let the canonical builder enforce
                    # pose, speed, qdot, and dwell together.
                    if running or not state.startswith("STOPPED"):
                        self.sleeper(self.poll_interval_s)
                        continue
                    max_gap_ns = int(
                        round(
                            float(manifest.stationary["max_sample_gap_s"])
                            * 1_000_000_000.0
                        )
                    )
                    if samples:
                        previous = samples[-1]
                        observed_gap_ns = (
                            observed_ns - int(previous["observed_monotonic_ns"])
                        )
                        controller_gap_ns = (
                            controller_ns - int(previous["controller_timestamp_ns"])
                        )
                        if (
                            observed_gap_ns > max_gap_ns
                            or controller_gap_ns > max_gap_ns
                        ):
                            # A dropped stream cannot contribute to continuous
                            # dwell.  Start a new stationary window at this
                            # fresh sample instead of carrying the old window
                            # across the hole.
                            samples.clear()
                    samples.append(row)
                    try:
                        build_home_verified_receipt(manifest, samples)
                    except HandoffError as exc:
                        last_error = exc
                        self.sleeper(self.poll_interval_s)
                        continue
                    return samples
        except RemoteStartupError:
            raise
        except HandoffError as exc:
            raise RemoteStartupError(str(exc)) from exc
        except Exception as exc:
            raise RemoteStartupError(
                f"read-only RTDE Home observation failed: {type(exc).__name__}:{exc}"
            ) from exc
        detail = "" if last_error is None else f" last_error={type(last_error).__name__}:{last_error}"
        raise RemoteStartupError(f"HOME_VERIFIED was not observed before timeout.{detail}")


class RemoteR026Loader:
    """Load only the exact r026 target after HOME_VERIFIED."""

    def __init__(
        self,
        *,
        robot_host: str,
        dashboard_observer: Callable[..., dict[str, str]] = dashboard_exchange,
        writer: RemoteDashboardWriter,
        dashboard_port: int = 29999,
        dashboard_timeout_s: float = 3.0,
        observe_timeout_s: float = 10.0,
        poll_interval_s: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.robot_host = robot_host
        self.dashboard_observer = dashboard_observer
        self.writer = writer
        self.dashboard_port = dashboard_port
        self.dashboard_timeout_s = dashboard_timeout_s
        self.observe_timeout_s = observe_timeout_s
        self.poll_interval_s = poll_interval_s
        self.monotonic = monotonic
        self.sleeper = sleeper

    def load(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        target = manifest.payload["script2"]["controller_target"]
        if self.writer.load_target != target:
            raise RemoteStartupError("r026 writer binding differs from manifest")
        adapter = _ExactLoadAdapter(
            host=self.robot_host,
            target=target,
            program_id=manifest.payload["script2"]["program_id"],
            dashboard_observer=self.dashboard_observer,
            writer=self.writer,
            dashboard_port=self.dashboard_port,
            dashboard_timeout_s=self.dashboard_timeout_s,
            observe_timeout_s=self.observe_timeout_s,
            poll_interval_s=self.poll_interval_s,
            monotonic=self.monotonic,
            sleeper=self.sleeper,
        )
        return adapter.load()


class RemoteR026IdentityObserver:
    """Read-only r026 identity, Safety, and release binding observation."""

    def __init__(
        self,
        *,
        robot_host: str,
        dashboard_observer: Callable[..., dict[str, str]] = dashboard_exchange,
        dashboard_port: int = 29999,
        dashboard_timeout_s: float = 3.0,
        release_validator: Callable[[HandoffManifest], Mapping[str, Any]] = validate_release_binding,
    ) -> None:
        self.robot_host = robot_host
        self.dashboard_observer = dashboard_observer
        self.dashboard_port = dashboard_port
        self.dashboard_timeout_s = dashboard_timeout_s
        self.release_validator = release_validator

    def observe(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        binding = self.release_validator(manifest)
        target = manifest.payload["script2"]["controller_target"]
        observation = self.dashboard_observer(
            self.robot_host,
            _DASHBOARD_COMMANDS,
            port=self.dashboard_port,
            timeout=self.dashboard_timeout_s,
        )
        contract = _dashboard_contract(
            observation,
            expected_target=target,
            expected_running=False,
            expected_stopped=True,
        )
        return {
            "program_id": manifest.payload["script2"]["program_id"],
            "controller_target": target,
            "execution_profile_id": manifest.payload["script2"]["execution_profile_id"],
            "release_manifest_sha256": manifest.payload["script2"]["release_manifest_sha256"],
            "triplet_sha256": dict(manifest.payload["script2"]["sha256"]),
            "remote_control": True,
            "robot_mode": "RUNNING",
            "safety_mode": "NORMAL",
            "program_running": False,
            "release_binding": dict(binding),
            "dashboard_response": {**contract, "dashboard": dict(observation)},
        }


class RemoteBridgeStarter:
    """Start the canonical bridge launcher and wait for its readiness claim."""

    def __init__(
        self,
        *,
        experiment_root: Path,
        robot_host: str,
        output_root: Path,
        campaign_root: Path,
        shell_path: Path | None = None,
        status_resolver: Callable[[Path], Mapping[str, Any]] = resolve_status,
        readiness_claim_builder: Callable[[Mapping[str, Any], str], Mapping[str, Any]] = readiness_claim,
        readiness_claim_verifier: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]] = verify_readiness_claim,
        dashboard_observer: Callable[..., dict[str, str]] = dashboard_exchange,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        owner_starttime_reader: Callable[[int], int | None] = read_proc_starttime_ticks,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.experiment_root = experiment_root.expanduser().resolve()
        self.robot_host = robot_host
        self.output_root = output_root.expanduser().absolute()
        self.campaign_root = campaign_root.expanduser().absolute()
        self.shell_path = shell_path or self.experiment_root / "scripts/step5d-autotune-v3.sh"
        self.status_resolver = status_resolver
        self.readiness_claim_builder = readiness_claim_builder
        self.readiness_claim_verifier = readiness_claim_verifier
        self.dashboard_observer = dashboard_observer
        self.popen_factory = popen_factory
        self.owner_starttime_reader = owner_starttime_reader
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.process: Any | None = None
        self.bridge_id: str | None = None
        self.started_this_attempt = False
        self._pre_attempt_id: str | None = None
        self._bridge_output_root: Path | None = None
        self._log_handle: Any | None = None

    @staticmethod
    def _attempt(status: Mapping[str, Any]) -> Mapping[str, Any] | None:
        attempt = status.get("launch_attempt")
        return attempt if isinstance(attempt, Mapping) else None

    def _capture_pre_attempt(self) -> None:
        self._pre_attempt_id = None
        try:
            status = self.status_resolver(self.experiment_root)
        except Exception:
            return
        attempt = self._attempt(status)
        attempt_id = attempt.get("attempt_id") if attempt is not None else None
        if isinstance(attempt_id, str) and attempt_id:
            self._pre_attempt_id = attempt_id

    def _require_fresh_attempt(self, status: Mapping[str, Any]) -> str:
        attempt = self._attempt(status)
        if attempt is None or attempt.get("present") is False:
            raise RemoteStartupPending("canonical launch attempt is not visible yet")
        attempt_id = attempt.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            raise RemoteStartupPending("canonical launch attempt identity is missing")
        if self._pre_attempt_id is not None and attempt_id == self._pre_attempt_id:
            raise RemoteStartupPending("readiness status still refers to the prior launch attempt")
        if attempt.get("state") != "STARTED":
            raise RemoteStartupPending("fresh canonical launch attempt is not STARTED")
        bindings = attempt.get("bindings")
        if not isinstance(bindings, Mapping):
            raise RemoteStartupPending("fresh launch attempt bindings are not visible")
        owner = bindings.get("resource_owner")
        process = self.process
        process_pid = getattr(process, "pid", None)
        if not isinstance(owner, Mapping) or not isinstance(process_pid, int):
            raise RemoteStartupPending("fresh launch owner binding is not visible")
        if owner.get("pid") != process_pid:
            raise RemoteStartupPending("readiness status belongs to a different launcher child")
        if self._bridge_output_root is None:
            raise RemoteStartupPending("bridge output binding is not initialized")
        try:
            bound_output = Path(str(bindings["output_root"])).expanduser().absolute()
            bound_campaign = Path(str(bindings["campaign_root"])).expanduser().absolute()
        except (KeyError, TypeError, ValueError) as exc:
            raise RemoteStartupPending("fresh launch path bindings are malformed") from exc
        if bound_output != self._bridge_output_root or bound_campaign != self.campaign_root:
            raise RemoteStartupPending("readiness status output/campaign binding differs from this attempt")
        owner_start = owner.get("starttime_ticks")
        observed_start = self.owner_starttime_reader(process_pid)
        if (
            isinstance(owner_start, bool)
            or not isinstance(owner_start, int)
            or not isinstance(observed_start, int)
            or owner_start != observed_start
        ):
            raise RemoteStartupPending("readiness status launcher starttime differs from this child")
        return attempt_id

    def _validate_ready_status(
        self,
        manifest: HandoffManifest,
        status: Mapping[str, Any],
        claim: Mapping[str, Any],
    ) -> dict[str, Any]:
        attempt_id = self._require_fresh_attempt(status)
        if claim.get("attempt_id") != attempt_id:
            raise RemoteStartupPending(
                "readiness claim is not bound to the fresh launch attempt"
            )
        if status.get("schema") != "step5d.bridge/governed-status-v3":
            raise RemoteStartupPending("bridge status schema is not visible yet")
        if status.get("state") != "BENCH_READY" or status.get("compatibility_phase") != "WAITING_FOR_PLAY":
            raise RemoteStartupPending("fresh bridge attempt has not reached WAITING_FOR_PLAY")
        expected_target = manifest.payload["script2"]["controller_target"]
        controller = status.get("controller")
        loaded = controller.get("loaded") if isinstance(controller, Mapping) else None
        if not isinstance(loaded, Mapping):
            raise RemoteStartupPending("fresh bridge loaded-program evidence is not visible")
        if (
            loaded.get("verified") is not True
            or loaded.get("expected") != expected_target
            or loaded.get("observed") != expected_target
        ):
            raise RemoteStartupTerminal("fresh bridge loaded r026 identity differs")
        release = status.get("release")
        if not isinstance(release, Mapping):
            raise RemoteStartupPending("fresh bridge release evidence is not visible")
        if release.get("sha256") != manifest.payload["script2"]["release_manifest_sha256"]:
            raise RemoteStartupTerminal("fresh bridge release identity differs")
        predicates = status.get("predicates")
        if not isinstance(predicates, Mapping):
            raise RemoteStartupPending("fresh bridge readiness predicates are missing")
        if predicates.get("single_writer") is False:
            raise RemoteStartupTerminal("fresh bridge single-writer predicate is false")
        if predicates.get("loaded_program_verified") is False:
            raise RemoteStartupTerminal("fresh bridge loaded-program predicate is false")
        if predicates.get("safety_normal") is False:
            raise RemoteStartupTerminal("fresh bridge safety predicate is false")
        for key in ("bridge_heartbeat_fresh", "single_writer", "lease_valid"):
            if predicates.get(key) is not True:
                raise RemoteStartupPending(f"fresh bridge readiness predicate is pending: {key}")
        bridge = status.get("bridge")
        lease = status.get("campaign_lease")
        if not isinstance(bridge, Mapping) or not isinstance(lease, Mapping):
            raise RemoteStartupPending("fresh bridge liveness/lease evidence is missing")
        if lease.get("valid") is False:
            raise RemoteStartupTerminal("fresh bridge lease predicate is false")
        if bridge.get("alive") is not True or bridge.get("heartbeat_fresh") is not True:
            raise RemoteStartupPending("fresh bridge heartbeat is pending")
        try:
            observation = self.dashboard_observer(
                self.robot_host,
                _DASHBOARD_COMMANDS,
                port=manifest.remote_startup["dashboard_port"],
                timeout=manifest.remote_startup["dashboard_timeout_s"],
            )
            if observation.get("is in remote control", "").strip().lower() != "true":
                raise RemoteStartupTerminal("fresh bridge controller is not in Remote Control")
            if "NORMAL" not in str(observation.get("safetymode", "")):
                raise RemoteStartupTerminal("fresh bridge Dashboard safety is not NORMAL")
            if _loaded_target(observation) != expected_target:
                raise RemoteStartupTerminal("fresh bridge Dashboard loaded identity differs")
            if "RUNNING" not in str(observation.get("robotmode", "")):
                raise RemoteStartupPending("fresh bridge robot mode is not RUNNING")
            dashboard = _dashboard_contract(
                observation,
                expected_target=expected_target,
                expected_running=False,
                expected_stopped=True,
            )
        except RemoteStartupTerminal:
            raise
        except RemoteStartupError as exc:
            raise RemoteStartupPending(str(exc)) from exc
        except Exception as exc:
            raise RemoteStartupPending(
                f"fresh bridge Dashboard evidence is pending: {type(exc).__name__}:{exc}"
            ) from exc
        return {
            "bridge_id": f"canonical:{attempt_id}",
            "controller_program_id": manifest.payload["script2"]["program_id"],
            "release_manifest_sha256": manifest.payload["script2"]["release_manifest_sha256"],
            "heartbeat_fresh": True,
            "lease_valid": True,
            "single_writer": True,
            "safety_normal": dashboard["safety_mode"] == "NORMAL",
            "status": dict(status),
            "readiness_claim": dict(claim),
            "dashboard": dashboard,
        }

    def start(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        if manifest.remote_startup.get("route") != "remote_control":
            raise RemoteStartupError("campaign startup route is not Remote Control")
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.campaign_root.mkdir(parents=True, exist_ok=True)
        self.started_this_attempt = False
        self.process = None
        self.bridge_id = None
        self._capture_pre_attempt()
        log_path = self.output_root / "campaign-start-bridge.log"
        environment = os.environ.copy()
        environment[manifest.remote_startup["robot_host_env"]] = self.robot_host
        command = [
            str(self.shell_path),
            "bridge-live",
            "--output-root",
            str(self.output_root / "bridge-live"),
            "--campaign-root",
            str(self.campaign_root),
        ]
        self._bridge_output_root = (self.output_root / "bridge-live").absolute()
        self._log_handle = log_path.open("ab")
        try:
            self.process = self.popen_factory(
                command,
                cwd=self.experiment_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            self.started_this_attempt = True
            deadline = self.monotonic() + manifest.remote_startup["bridge_ready_timeout_s"]
            last_error: RemoteStartupError | None = None
            while self.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RemoteStartupTerminal(
                        f"canonical bridge exited before readiness rc={self.process.returncode}"
                    )
                try:
                    status = dict(self.status_resolver(self.experiment_root))
                    claim = dict(self.readiness_claim_builder(status, "WAITING_FOR_PLAY"))
                    self.readiness_claim_verifier(status, claim)
                    response = self._validate_ready_status(manifest, status, claim)
                    self.bridge_id = str(response["bridge_id"])
                    return response
                except RemoteStartupTerminal:
                    raise
                except RemoteStartupPending as exc:
                    last_error = exc
                except Exception as exc:
                    last_error = RemoteStartupPending(
                        f"fresh bridge readiness is pending: {type(exc).__name__}:{exc}"
                    )
                self.sleeper(manifest.remote_startup["poll_interval_s"])
            detail = "" if last_error is None else f" last_error={type(last_error).__name__}:{last_error}"
            raise RemoteStartupPending(f"canonical bridge readiness timed out.{detail}")
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=0.5)
            except Exception:
                try:
                    process.terminate()
                    process.wait(timeout=0.5)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None


class RemoteCampaignPlayer:
    """Delegate Script 2 Play to the existing governed Remote Play primitive."""

    def __init__(
        self,
        *,
        experiment_root: Path,
        robot_host: str,
        output_root: Path,
        bridge: RemoteBridgeStarter,
        status_resolver: Callable[[Path], Mapping[str, Any]] = resolve_status,
        play_callable: Callable[..., Mapping[str, Any]] = governed_remote_play,
    ) -> None:
        self.experiment_root = experiment_root.expanduser().resolve()
        self.robot_host = robot_host
        self.output_root = output_root.expanduser().absolute()
        self.bridge = bridge
        self.status_resolver = status_resolver
        self.play_callable = play_callable

    def _validate_play_receipt(
        self,
        manifest: HandoffManifest,
        receipt: Mapping[str, Any],
        bridge_id: str,
    ) -> dict[str, Any]:
        if not isinstance(bridge_id, str) or not bridge_id:
            raise RemoteStartupTerminal("BRIDGE_READY identity is malformed")
        if receipt.get("schema") != REMOTE_PLAY_SCHEMA:
            raise RemoteStartupTerminal("governed Remote Play receipt schema differs")
        if receipt.get("status") != "play_observed":
            raise RemoteStartupTerminal("governed Remote Play did not observe Play")
        expected_target = manifest.payload["script2"]["controller_target"]
        if receipt.get("expected_program") != expected_target:
            raise RemoteStartupTerminal("governed Remote Play identity differs")
        if receipt.get("release_manifest_sha256") != manifest.payload["script2"]["release_manifest_sha256"]:
            raise RemoteStartupTerminal("governed Remote Play release identity differs")
        expected_attempt = bridge_id.removeprefix("canonical:")
        if receipt.get("attempt_id") != expected_attempt:
            raise RemoteStartupTerminal("governed Remote Play attempt differs from BRIDGE_READY")
        for key in ("governed_status_sha256",):
            value = receipt.get(key)
            if not isinstance(value, str) or len(value) != 64 or any(
                character not in "0123456789abcdef" for character in value
            ):
                raise RemoteStartupTerminal(f"governed Remote Play receipt {key} is invalid")
        for key in ("observed_at_unix_ns", "play_observed_at_unix_ns"):
            value = receipt.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise RemoteStartupTerminal(f"governed Remote Play receipt {key} is invalid")
        if receipt["play_observed_at_unix_ns"] < receipt["observed_at_unix_ns"]:
            raise RemoteStartupTerminal("governed Remote Play observation order differs")
        after = receipt.get("dashboard_after")
        if not isinstance(after, Mapping):
            raise RemoteStartupTerminal("governed Remote Play dashboard-after evidence is missing")
        for key in (
            "is in remote control",
            "safetymode",
            "robotmode",
            "running",
            "programState",
            "get loaded program",
        ):
            if not isinstance(after.get(key), str):
                raise RemoteStartupTerminal(
                    f"governed Remote Play dashboard-after field {key!r} is malformed"
                )
        if after.get("is in remote control", "").strip().lower() != "true":
            raise RemoteStartupTerminal("governed Remote Play dashboard is not Remote Control")
        if "NORMAL" not in str(after.get("safetymode", "")):
            raise RemoteStartupTerminal("governed Remote Play Dashboard safety is not NORMAL")
        if after.get("running", "").strip().lower() != "program running: true":
            raise RemoteStartupTerminal("governed Remote Play running evidence is false")
        if not str(after.get("programState", "")).startswith("PLAYING"):
            raise RemoteStartupTerminal("governed Remote Play program state is not PLAYING")
        if after.get("get loaded program") != f"Loaded program: {expected_target}":
            raise RemoteStartupTerminal("governed Remote Play loaded-program evidence differs")
        write_response = receipt.get("dashboard_write_response")
        if not isinstance(write_response, str) or not write_response.startswith("Starting program"):
            raise RemoteStartupTerminal("governed Remote Play write response is missing")
        return dict(receipt)

    def play(self, manifest: HandoffManifest) -> Mapping[str, Any]:
        bridge_id = self.bridge.bridge_id
        if not bridge_id:
            raise RemoteStartupError("campaign Play requires a ready bridge identity")
        startup = manifest.remote_startup
        evidence = self.output_root / "remote-play-receipt.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        raw_receipt = self.play_callable(
            self.experiment_root,
            robot_host=self.robot_host,
            evidence_output=evidence,
            dashboard_port=startup["dashboard_port"],
            dashboard_timeout_s=startup["dashboard_timeout_s"],
            observe_timeout_s=startup["play_timeout_s"],
            status_resolver=self.status_resolver,
        )
        if not isinstance(raw_receipt, Mapping):
            raise RemoteStartupTerminal("governed Remote Play returned a non-object receipt")
        receipt = self._validate_play_receipt(manifest, raw_receipt, bridge_id)
        after = receipt["dashboard_after"]
        return {
            "action": "PLAY",
            "accepted": receipt["status"] == "play_observed",
            "program_id": manifest.payload["script2"]["program_id"],
            "program_running": after["running"].strip().lower() == "program running: true",
            "safety_mode": after["safetymode"].split(":", 1)[-1].strip(),
            "bridge_id": bridge_id,
            "remote_play_receipt": receipt,
        }


__all__ = [
    "DashboardWriteError",
    "DashboardWriteOutcome",
    "HOME_RTDE_FIELDS",
    "RemoteBridgeStarter",
    "RemoteCampaignPlayer",
    "RemoteDashboardWriter",
    "RemoteHomeObserver",
    "RemoteR026IdentityObserver",
    "RemoteR026Loader",
    "RemoteScript1Trigger",
    "RemoteStartupError",
    "RemoteStartupPending",
    "RemoteStartupTerminal",
]
