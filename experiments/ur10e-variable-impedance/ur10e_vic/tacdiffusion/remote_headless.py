"""Captured-response and injected, no-socket Remote/headless lifecycle contract.

This module deliberately contains no socket, Dashboard, RTDE, bridge, or
controller implementation.  A caller may inject a transport for a synthetic
fixture, while the default lifecycle mode only audits captured evidence and
stops before the first mutable gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from typing import Mapping, Protocol


class RemoteStage(str, Enum):
    REMOTE_CONTROL = "REMOTE_CONTROL"
    DASHBOARD_REACHABLE = "DASHBOARD_REACHABLE"
    RTDE_READ_ONLY_REACHABLE = "RTDE_READ_ONLY_REACHABLE"
    LOAD = "LOAD"
    PLAY = "PLAY"
    BRIDGE_READY = "BRIDGE_READY"
    ARM = "ARM"
    MOTION = "MOTION"


class RemoteLifecycleMode(str, Enum):
    NO_MOTION = "NO_MOTION"
    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"


@dataclass(frozen=True)
class RemoteEvidence:
    remote_control: bool
    dashboard_reachable: bool
    rtde_read_only_reachable: bool
    program_loaded: bool = False
    program_playing: bool = False
    bridge_ready: bool = False
    arm_authorized: bool = False
    motion_authorized: bool = False


@dataclass(frozen=True)
class RemoteLifecycleReport:
    statuses: tuple[tuple[str, bool], ...]
    no_motion: bool
    next_required: tuple[str, ...]
    issued_commands: tuple[str, ...] = ()
    sleep_calls: int = 0
    failure_reason: str | None = None
    mode: str = RemoteLifecycleMode.NO_MOTION.value


class RemoteTransport(Protocol):
    """Injected mutation boundary; there is intentionally no socket adapter."""

    def mutate(self, stage: RemoteStage, authorization: object) -> str:
        """Issue one already-authorized synthetic transition and return text."""


@dataclass(frozen=True)
class _GateAuthorization:
    artifact: str

    def __post_init__(self) -> None:
        if not isinstance(self.artifact, str) or not self.artifact.strip():
            raise ValueError("authorization artifact must be a non-empty identity")


@dataclass(frozen=True)
class LoadAuthorization(_GateAuthorization):
    pass


@dataclass(frozen=True)
class PlayAuthorization(_GateAuthorization):
    pass


@dataclass(frozen=True)
class BridgeAuthorization(_GateAuthorization):
    pass


@dataclass(frozen=True)
class ArmAuthorization(_GateAuthorization):
    pass


@dataclass(frozen=True)
class MotionAuthorization(_GateAuthorization):
    pass


_EXPECTED_AUTHORIZATION: dict[RemoteStage, type[_GateAuthorization]] = {
    RemoteStage.LOAD: LoadAuthorization,
    RemoteStage.PLAY: PlayAuthorization,
    RemoteStage.BRIDGE_READY: BridgeAuthorization,
    RemoteStage.ARM: ArmAuthorization,
    RemoteStage.MOTION: MotionAuthorization,
}
_RESPONSE_KEYS: dict[RemoteStage, str] = {
    RemoteStage.LOAD: "load accepted",
    RemoteStage.PLAY: "play accepted",
    RemoteStage.BRIDGE_READY: "bridge ready",
    RemoteStage.ARM: "arm accepted",
    RemoteStage.MOTION: "motion accepted",
}


def _parse_bool_response(text: str, key: str) -> bool:
    matches = re.findall(rf"^{re.escape(key)}\s*[:=]\s*(true|false)\s*$", text, flags=re.IGNORECASE | re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"missing or ambiguous Dashboard response: {key}")
    return matches[0].lower() == "true"


def _validate_capture_freshness(
    captured_at_s: float | None,
    now_s: float | None,
    max_age_s: float,
) -> None:
    if captured_at_s is None and now_s is None:
        return
    if captured_at_s is None or now_s is None:
        raise ValueError("captured response freshness requires capture and reference timestamps")
    if not all(math.isfinite(float(value)) for value in (captured_at_s, now_s, max_age_s)) or max_age_s < 0.0:
        raise ValueError("captured response freshness metadata is invalid")
    if captured_at_s > now_s or now_s - captured_at_s > max_age_s:
        raise ValueError("captured response is stale")


def parse_dashboard_response(
    text: str,
    *,
    captured_at_s: float | None = None,
    now_s: float | None = None,
    max_age_s: float = 1.0,
) -> RemoteEvidence:
    """Parse captured text only; never opens a Dashboard connection."""

    _validate_capture_freshness(captured_at_s, now_s, max_age_s)
    return RemoteEvidence(
        remote_control=_parse_bool_response(text, "is in remote control"),
        dashboard_reachable=True,
        rtde_read_only_reachable=_parse_bool_response(text, "RTDE read-only reachable"),
        program_loaded=_parse_bool_response(text, "program loaded"),
        program_playing=_parse_bool_response(text, "Program running"),
    )


def parse_load_response(
    text: str,
    *,
    captured_at_s: float | None = None,
    now_s: float | None = None,
    max_age_s: float = 1.0,
) -> bool:
    _validate_capture_freshness(captured_at_s, now_s, max_age_s)
    return _parse_bool_response(text, "load accepted")


def parse_play_response(
    text: str,
    *,
    captured_at_s: float | None = None,
    now_s: float | None = None,
    max_age_s: float = 1.0,
) -> bool:
    _validate_capture_freshness(captured_at_s, now_s, max_age_s)
    return _parse_bool_response(text, "play accepted")


def build_dry_run_command_plan() -> tuple[tuple[str, str], ...]:
    return (
        (RemoteStage.REMOTE_CONTROL.value, "verify captured Dashboard Remote Control response"),
        (RemoteStage.DASHBOARD_REACHABLE.value, "verify captured Dashboard reachability response"),
        (RemoteStage.RTDE_READ_ONLY_REACHABLE.value, "verify captured RTDE read-only response"),
        (RemoteStage.LOAD.value, "parse captured load response"),
        (RemoteStage.PLAY.value, "parse captured play response"),
        (RemoteStage.BRIDGE_READY.value, "verify captured bridge heartbeat artifact"),
        (RemoteStage.ARM.value, "explicit typed arm authorization required"),
        (RemoteStage.MOTION.value, "explicit typed motion authorization required"),
    )


def _status_items(evidence: RemoteEvidence) -> list[tuple[RemoteStage, bool]]:
    return [
        (RemoteStage.REMOTE_CONTROL, evidence.remote_control),
        (RemoteStage.DASHBOARD_REACHABLE, evidence.dashboard_reachable),
        (RemoteStage.RTDE_READ_ONLY_REACHABLE, evidence.rtde_read_only_reachable),
        (RemoteStage.LOAD, evidence.program_loaded),
        (RemoteStage.PLAY, evidence.program_playing),
        (RemoteStage.BRIDGE_READY, evidence.bridge_ready),
        (RemoteStage.ARM, evidence.arm_authorized),
        (RemoteStage.MOTION, evidence.motion_authorized),
    ]


def _make_report(
    statuses: list[tuple[RemoteStage, bool]],
    *,
    next_required: tuple[str, ...],
    issued_commands: list[str],
    failure_reason: str | None = None,
    mode: str = RemoteLifecycleMode.NO_MOTION.value,
) -> RemoteLifecycleReport:
    status_values = dict(statuses)
    return RemoteLifecycleReport(
        statuses=tuple((stage.value, status_values[stage]) for stage in RemoteStage),
        no_motion=not status_values[RemoteStage.MOTION],
        next_required=next_required,
        issued_commands=tuple(issued_commands),
        sleep_calls=0,
        failure_reason=failure_reason,
        mode=mode,
    )


@dataclass
class RemoteLifecycleExecutor:
    """Deterministic lifecycle composition over captured evidence and a fake transport."""

    evidence: RemoteEvidence
    transport: RemoteTransport | None = None
    mode: RemoteLifecycleMode = RemoteLifecycleMode.NO_MOTION
    authorizations: Mapping[RemoteStage, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RemoteLifecycleMode):
            raise TypeError("lifecycle mode must be an explicit RemoteLifecycleMode")
        unknown = set(self.authorizations) - set(_EXPECTED_AUTHORIZATION)
        if unknown:
            raise ValueError("authorization supplied for a non-mutable or unknown gate")

    def execute(self) -> RemoteLifecycleReport:
        statuses = _status_items(self.evidence)
        issued: list[str] = []
        preflight = (RemoteStage.REMOTE_CONTROL, RemoteStage.DASHBOARD_REACHABLE, RemoteStage.RTDE_READ_ONLY_REACHABLE)
        status_values = dict(statuses)
        for stage in preflight:
            if not status_values[stage]:
                return _make_report(statuses, next_required=(stage.value,), issued_commands=issued, failure_reason=f"{stage.value} evidence is false", mode=self.mode.value)

        if self.mode is RemoteLifecycleMode.NO_MOTION:
            return _make_report(statuses, next_required=(RemoteStage.LOAD.value,), issued_commands=issued, mode=self.mode.value)
        for stage in (RemoteStage.LOAD, RemoteStage.PLAY, RemoteStage.BRIDGE_READY, RemoteStage.ARM, RemoteStage.MOTION):
            authorization = self.authorizations.get(stage)
            expected_type = _EXPECTED_AUTHORIZATION[stage]
            if type(authorization) is not expected_type:
                return _make_report(
                    statuses,
                    next_required=(stage.value,),
                    issued_commands=issued,
                    failure_reason=f"{stage.value} requires its own typed authorization",
                    mode=self.mode.value,
                )
        if self.transport is None:
            return _make_report(
                statuses,
                next_required=(RemoteStage.LOAD.value,),
                issued_commands=issued,
                failure_reason="synthetic lifecycle requires an injected transport",
                mode=self.mode.value,
            )

        for stage in (RemoteStage.LOAD, RemoteStage.PLAY, RemoteStage.BRIDGE_READY, RemoteStage.ARM, RemoteStage.MOTION):
            authorization = self.authorizations.get(stage)
            try:
                response = self.transport.mutate(stage, authorization)
                accepted = _parse_bool_response(response, _RESPONSE_KEYS[stage])
            except Exception as exc:
                status_values = dict(statuses)
                status_values[stage] = False
                statuses = list(status_values.items())
                return _make_report(statuses, next_required=(stage.value,), issued_commands=issued, failure_reason=f"{stage.value} response rejected: {exc}", mode=self.mode.value)
            issued.append(stage.value)
            status_values = dict(statuses)
            status_values[stage] = accepted
            statuses = list(status_values.items())
            if not accepted:
                return _make_report(statuses, next_required=(stage.value,), issued_commands=issued, failure_reason=f"{stage.value} response was negative", mode=self.mode.value)
        return _make_report(statuses, next_required=(), issued_commands=issued, mode=self.mode.value)

    def run(self) -> RemoteLifecycleReport:
        """Compatibility spelling for callers that name an executor run."""

        return self.execute()


def inspect_remote_headless(evidence: RemoteEvidence) -> RemoteLifecycleReport:
    """Classify captured evidence only; never promotes a live gate or performs I/O."""

    statuses = _status_items(evidence)
    required = tuple(stage.value for stage, passed in statuses if not passed)
    return _make_report(statuses, next_required=required, issued_commands=[])
