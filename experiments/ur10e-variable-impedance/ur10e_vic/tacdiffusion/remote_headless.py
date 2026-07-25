"""No-I/O Remote/headless lifecycle state contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class RemoteStage(str, Enum):
    REMOTE_CONTROL = "REMOTE_CONTROL"
    DASHBOARD_REACHABLE = "DASHBOARD_REACHABLE"
    LOAD = "LOAD"
    PLAY = "PLAY"
    BRIDGE_READY = "BRIDGE_READY"
    ARM = "ARM"
    MOTION = "MOTION"


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


def inspect_remote_headless(evidence: RemoteEvidence) -> RemoteLifecycleReport:
    statuses = (
        (RemoteStage.REMOTE_CONTROL.value, evidence.remote_control),
        (RemoteStage.DASHBOARD_REACHABLE.value, evidence.dashboard_reachable),
        ("RTDE_READ_ONLY_REACHABLE", evidence.rtde_read_only_reachable),
        (RemoteStage.LOAD.value, evidence.program_loaded),
        (RemoteStage.PLAY.value, evidence.program_playing),
        (RemoteStage.BRIDGE_READY.value, evidence.bridge_ready),
        (RemoteStage.ARM.value, evidence.arm_authorized),
        (RemoteStage.MOTION.value, evidence.motion_authorized),
    )
    required = tuple(name for name, passed in statuses if not passed)
    # This function only classifies evidence; it never promotes a live gate.
    return RemoteLifecycleReport(statuses, no_motion=not evidence.motion_authorized, next_required=required)


def _parse_bool_response(text: str, key: str) -> bool:
    matches = re.findall(rf"^{re.escape(key)}\s*[:=]\s*(true|false)\s*$", text, flags=re.IGNORECASE | re.MULTILINE)
    if len(matches) != 1:
        raise ValueError(f"missing or ambiguous Dashboard response: {key}")
    return matches[0].lower() == "true"


def parse_dashboard_response(text: str) -> RemoteEvidence:
    """Parse captured text only; never opens a Dashboard connection."""
    return RemoteEvidence(
        remote_control=_parse_bool_response(text, "is in remote control"),
        dashboard_reachable=True,
        rtde_read_only_reachable=_parse_bool_response(text, "RTDE read-only reachable"),
        program_loaded=_parse_bool_response(text, "program loaded"),
        program_playing=_parse_bool_response(text, "Program running"),
    )


def parse_load_response(text: str) -> bool:
    return _parse_bool_response(text, "load accepted")


def parse_play_response(text: str) -> bool:
    return _parse_bool_response(text, "play accepted")


def build_dry_run_command_plan() -> tuple[tuple[str, str], ...]:
    return (
        ("REMOTE_CONTROL", "verify captured Dashboard Remote Control response"),
        ("LOAD", "parse captured load response"),
        ("PLAY", "parse captured play response"),
        ("BRIDGE_READY", "verify captured bridge heartbeat artifact"),
        ("ARM", "explicit live authorization required"),
        ("MOTION", "explicit user live gate required"),
    )
