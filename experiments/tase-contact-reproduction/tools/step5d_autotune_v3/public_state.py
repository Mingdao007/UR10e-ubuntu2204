"""Pure projection from detailed observations to public Step5d status."""

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


class GovernedStatusMilestone(str, Enum):
    UNPREPARED = "UNPREPARED"
    RELEASE_CONTRACT_PROVEN = "RELEASE_CONTRACT_PROVEN"
    PROGRAM_LOADED_STOPPED = "PROGRAM_LOADED_STOPPED"
    BRIDGE_PROCESS_STARTED = "BRIDGE_PROCESS_STARTED"
    BRIDGE_WAITING_FOR_PLAY = "BRIDGE_WAITING_FOR_PLAY"
    PLAY_OBSERVED = "PLAY_OBSERVED"
    TRIAL_1_COMPLETE = "TRIAL_1_COMPLETE"
    NEXT_ARM_PUBLISHED = "NEXT_ARM_PUBLISHED"
    CONTINUOUS_READY = "CONTINUOUS_READY"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _coerced_bool(value: Any, *, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _coerce_milestones(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _coerce_granted_tokens(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [token for token in value if isinstance(token, str)]


def _resolve_governed_milestone(
    *,
    release_contract_proven: bool,
    attempt_bound: bool,
    process_alive: bool,
    heartbeat_fresh: bool,
    compatibility_phase: str | None,
    admission_milestones: list[str],
    play_observed: bool,
    trial_1_complete: bool,
    next_arm_published: bool,
    continuous_ready: bool,
) -> str:
    if not release_contract_proven:
        return GovernedStatusMilestone.UNPREPARED.value

    milestone = GovernedStatusMilestone.RELEASE_CONTRACT_PROVEN
    if "PROGRAM_LOADED_STOPPED" in admission_milestones:
        milestone = GovernedStatusMilestone.PROGRAM_LOADED_STOPPED

    if attempt_bound and process_alive:
        milestone = GovernedStatusMilestone.BRIDGE_PROCESS_STARTED
        if (
            compatibility_phase in {"WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"}
            and "PROGRAM_LOADED_STOPPED" in admission_milestones
        ):
            milestone = GovernedStatusMilestone.BRIDGE_WAITING_FOR_PLAY
            if play_observed:
                milestone = GovernedStatusMilestone.PLAY_OBSERVED

    if (
        milestone == GovernedStatusMilestone.PLAY_OBSERVED
        and trial_1_complete
    ):
        milestone = GovernedStatusMilestone.TRIAL_1_COMPLETE
    if (
        milestone == GovernedStatusMilestone.TRIAL_1_COMPLETE
        and next_arm_published
    ):
        milestone = GovernedStatusMilestone.NEXT_ARM_PUBLISHED
    if (
        milestone == GovernedStatusMilestone.NEXT_ARM_PUBLISHED
        and continuous_ready
    ):
        milestone = GovernedStatusMilestone.CONTINUOUS_READY

    return milestone.value


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
    if facts.action_required:
        return PublicState.ACTION_REQUIRED
    if facts.bench_ready and facts.attempt_bound:
        return PublicState.BENCH_READY
    if facts.delivered:
        return PublicState.DELIVERED
    return PublicState.UNPREPARED


def project_status(status: Mapping[str, Any]) -> dict[str, Any]:
    """Return a computed public view while retaining one-window phase parity."""

    if not isinstance(status, Mapping):
        raise ValueError("status must be an object")

    result = dict(status)
    phase = result.get("compatibility_phase", result.get("state"))
    predicates = _as_mapping(result.get("predicates"))
    blocker = result.get("blocker")
    blocker_class = blocker.get("class") if isinstance(blocker, Mapping) else None
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

    existing_milestones = _as_mapping(result.get("milestones"))
    source_liveness = _as_mapping(existing_milestones.get("liveness"))
    source_legacy_acceptance = _as_mapping(existing_milestones.get("acceptance"))
    source_acceptance = _as_mapping(existing_milestones.get("acceptance_certificate"))
    if not source_acceptance:
        source_acceptance = _as_mapping(existing_milestones.get("acceptance"))
    if not source_acceptance:
        source_acceptance = {}

    release_contract_proven = _coerced_bool(
        source_acceptance.get("release_contract_proven"),
        fallback=predicates.get("release_contract_proven") is True,
    )
    play_prompt_ready = _coerced_bool(
        source_acceptance.get("play_prompt_ready"),
        fallback=(
            predicates.get("play_prompt_ready") is True
            or predicates.get("bench_ready") is True
        ),
    )
    admission_milestones = _coerce_milestones(source_acceptance.get("admission"))
    ownership_binding_source = _as_mapping(existing_milestones.get("ownership_binding"))
    if not ownership_binding_source:
        ownership_binding_source = _as_mapping(existing_milestones.get("authorization"))
    authorization_source = _as_mapping(existing_milestones.get("authorization"))
    liveness = {
        "bridge_process_alive": _coerced_bool(
            source_liveness.get("bridge_process_alive"),
            fallback=predicates.get("bridge_process_alive") is True,
        ),
        "bridge_heartbeat_fresh": _coerced_bool(
            source_liveness.get("bridge_heartbeat_fresh"),
            fallback=predicates.get("bridge_heartbeat_fresh") is True,
        ),
    }
    ownership_binding = {
        "canonical_attempt_bound": _coerced_bool(
            ownership_binding_source.get("canonical_attempt_bound"),
            fallback=_coerced_bool(
                predicates.get("canonical_attempt_bound"),
                fallback=False,
            ),
        ),
        "single_writer": _coerced_bool(
            ownership_binding_source.get("single_writer"),
            fallback=_coerced_bool(predicates.get("single_writer"), fallback=False),
        ),
        "lease_valid": _coerced_bool(
            ownership_binding_source.get("lease_valid"),
            fallback=_coerced_bool(predicates.get("lease_valid"), fallback=False),
        ),
    }
    authorization = {
        "live_motion_authorized": _coerced_bool(
            authorization_source.get("live_motion_authorized"),
            fallback=False,
        ),
        "granted_tokens": _coerce_granted_tokens(
            authorization_source.get("granted_tokens")
        ),
    }
    play_observed = (
        _coerced_bool(predicates.get("play_observed"), fallback=False)
    )
    trial_1_complete = (
        _coerced_bool(predicates.get("trial_1_complete"), fallback=False)
    )
    next_arm_published = (
        _coerced_bool(predicates.get("next_arm_published"), fallback=False)
    )
    continuous_ready = (
        _coerced_bool(predicates.get("continuous_ready"), fallback=False)
    )
    facts = PublicStateFacts(
        delivered=bool(
            predicates.get("controller_fresh_get") is True
            or predicates.get("uploaded_identity_verified") is True
            or release_contract_proven is True
        ),
        action_required=blocker_class in {"BLOCKED_EXTERNAL", "PHYSICAL"},
        bench_ready=play_prompt_ready is True,
        attempt_bound=ownership_binding["canonical_attempt_bound"],
        process_alive=liveness["bridge_process_alive"],
        heartbeat_fresh=liveness["bridge_heartbeat_fresh"],
        single_writer=ownership_binding["single_writer"],
        lease_valid=ownership_binding["lease_valid"],
        live_phase=phase == "RUNNING",
        terminal=bool(
            terminal_complete
            or attempt_state in {"COMPLETED", "FAILED", "CANCELLED"}
            or phase in {"COMPLETE", "COMPLETED", "FAILED", "CANCELLED"}
        ),
    )

    result["compatibility_phase"] = phase
    result["milestones"] = {
        "liveness": liveness,
        "authorization": authorization,
        "ownership_binding": ownership_binding,
        "acceptance_certificate": {
            "release_contract_proven": release_contract_proven,
            "play_prompt_ready": play_prompt_ready,
            "admission": admission_milestones,
        },
    }
    if source_legacy_acceptance:
        result["milestones"]["acceptance"] = {
            "release_contract_proven": _coerced_bool(
                source_legacy_acceptance.get("release_contract_proven"),
                fallback=release_contract_proven,
            ),
            "play_prompt_ready": _coerced_bool(
                source_legacy_acceptance.get("play_prompt_ready"),
                fallback=play_prompt_ready,
            ),
            "admission": _coerce_milestones(source_legacy_acceptance.get("admission")),
        }
    result["milestone"] = _resolve_governed_milestone(
        release_contract_proven=release_contract_proven,
        attempt_bound=ownership_binding["canonical_attempt_bound"],
        process_alive=liveness["bridge_process_alive"],
        heartbeat_fresh=liveness["bridge_heartbeat_fresh"],
        compatibility_phase=phase,
        admission_milestones=admission_milestones,
        play_observed=play_observed,
        trial_1_complete=trial_1_complete,
        next_arm_published=next_arm_published,
        continuous_ready=continuous_ready,
    )
    result["capabilities"] = {
        "play_prompt": (
            phase in {"WAITING_FOR_IDENTITY_PLAY", "WAITING_FOR_PLAY"}
            and result["milestone"] != GovernedStatusMilestone.UNPREPARED.value
            and ownership_binding["canonical_attempt_bound"]
            and play_prompt_ready
            and blocker_class not in {"BLOCKED_EXTERNAL", "PHYSICAL"}
        )
    }
    result["next_operator_action"] = (
        "PRESS_PLAY" if result["capabilities"]["play_prompt"] else None
    )
    result["state"] = resolve_public_state(facts).value
    return result


__all__ = [
    "PublicState",
    "PublicStateFacts",
    "project_status",
    "resolve_public_state",
    "GovernedStatusMilestone",
]
