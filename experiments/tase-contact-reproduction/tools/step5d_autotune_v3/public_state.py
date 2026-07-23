"""Pure projection from detailed observations to the six public Step5d states."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class PublicState(str, Enum):
    UNPREPARED = "UNPREPARED"
    DELIVERED = "DELIVERED"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    BENCH_READY = "BENCH_READY"
    RUNNING = "RUNNING"
    TERMINAL = "TERMINAL"


@dataclass(frozen=True)
class PublicStateFacts:
    delivered: bool = False
    action_required: bool = False
    bench_ready: bool = False
    attempt_bound: bool = False
    process_alive: bool = False
    heartbeat_fresh: bool = False
    single_writer: bool = False
    lease_valid: bool = False
    live_phase: bool = False
    terminal: bool = False

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be boolean")


def resolve_public_state(facts: PublicStateFacts) -> PublicState:
    """Resolve by safety precedence; no result is persisted as mutable truth."""

    if not isinstance(facts, PublicStateFacts):
        raise ValueError("public state facts differ")
    if facts.terminal:
        return PublicState.TERMINAL
    if all(
        (
            facts.live_phase,
            facts.attempt_bound,
            facts.process_alive,
            facts.heartbeat_fresh,
            facts.single_writer,
            facts.lease_valid,
        )
    ):
        return PublicState.RUNNING
    if facts.bench_ready and facts.attempt_bound:
        return PublicState.BENCH_READY
    if facts.action_required:
        return PublicState.ACTION_REQUIRED
    if facts.delivered:
        return PublicState.DELIVERED
    return PublicState.UNPREPARED


def project_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return a computed public view while retaining one-window phase parity."""

    if not isinstance(status, Mapping):
        raise ValueError("status must be an object")
    result = dict(status)
    phase = result.get("compatibility_phase", result.get("state"))
    predicates = result.get("predicates")
    if not isinstance(predicates, Mapping):
        predicates = {}
    blocker = result.get("blocker")
    blocker_class = (
        blocker.get("class") if isinstance(blocker, Mapping) else None
    )
    launch_attempt = result.get("launch_attempt")
    attempt_state = (
        launch_attempt.get("state")
        if isinstance(launch_attempt, Mapping)
        else None
    )
    terminal = result.get("terminal")
    terminal_complete = bool(
        isinstance(terminal, Mapping) and terminal.get("completed") is True
    )
    facts = PublicStateFacts(
        delivered=bool(
            predicates.get("controller_fresh_get") is True
            or predicates.get("uploaded_identity_verified") is True
            or predicates.get("offline_proven") is True
        ),
        action_required=blocker_class in {"BLOCKED_EXTERNAL", "PHYSICAL"},
        bench_ready=bool(
            predicates.get("play_prompt_ready") is True
            or predicates.get("bench_ready") is True
        ),
        attempt_bound=predicates.get("canonical_attempt_bound") is True,
        process_alive=predicates.get("bridge_process_alive") is True,
        heartbeat_fresh=predicates.get("bridge_heartbeat_fresh") is True,
        single_writer=predicates.get("single_writer") is True,
        lease_valid=predicates.get("lease_valid") is True,
        live_phase=phase == "RUNNING",
        terminal=bool(
            terminal_complete
            or attempt_state in {"COMPLETED", "FAILED", "CANCELLED"}
            or phase in {"COMPLETE", "COMPLETED", "FAILED", "CANCELLED"}
        ),
    )
    result["compatibility_phase"] = phase
    result["state"] = resolve_public_state(facts).value
    return result


__all__ = [
    "PublicState",
    "PublicStateFacts",
    "project_status",
    "resolve_public_state",
]
