from __future__ import annotations

from typing import Any

import pytest

from step5d_autotune_v3.public_state import (
    GovernedStatusMilestone,
    PublicState,
    PublicStateFacts,
    project_status,
    resolve_public_state,
)


def running_facts(**overrides: bool) -> PublicStateFacts:
    values = {
        "delivered": True,
        "bench_ready": True,
        "attempt_bound": True,
        "process_alive": True,
        "heartbeat_fresh": True,
        "single_writer": True,
        "lease_valid": True,
        "live_phase": True,
    }
    values.update(overrides)
    return PublicStateFacts(**values)


def _projected_milestone(overrides: dict[str, Any]) -> str:
    defaults = {
        "release_contract_proven": True,
        "play_prompt_ready": True,
        "canonical_attempt_bound": True,
        "single_writer": True,
        "lease_valid": True,
        "bridge_process_alive": False,
        "bridge_heartbeat_fresh": False,
        "admission": [],
        "compatibility_phase": "RUNNING",
        "blocker_class": None,
        "play_observed": False,
        "trial_1_complete": False,
        "next_arm_published": False,
        "continuous_ready": False,
    }
    data = dict(defaults)
    data.update(overrides)
    return project_status(
        {
            "state": data["compatibility_phase"],
            "compatibility_phase": data["compatibility_phase"],
            "predicates": {
                "release_contract_proven": data["release_contract_proven"],
                "play_prompt_ready": data["play_prompt_ready"],
                "canonical_attempt_bound": data["canonical_attempt_bound"],
                "single_writer": data["single_writer"],
                "lease_valid": data["lease_valid"],
                "bridge_process_alive": data["bridge_process_alive"],
                "bridge_heartbeat_fresh": data["bridge_heartbeat_fresh"],
                "play_observed": data["play_observed"],
                "trial_1_complete": data["trial_1_complete"],
                "next_arm_published": data["next_arm_published"],
                "continuous_ready": data["continuous_ready"],
                "bench_ready": data["play_prompt_ready"],
            },
            "milestones": {
                "acceptance_certificate": {
                    "release_contract_proven": data["release_contract_proven"],
                    "play_prompt_ready": data["play_prompt_ready"],
                    "admission": list(data["admission"]),
                },
                "ownership_binding": {
                    "canonical_attempt_bound": data["canonical_attempt_bound"],
                    "single_writer": data["single_writer"],
                    "lease_valid": data["lease_valid"],
                },
            },
            "blocker": {
                "class": data["blocker_class"],
                "reason_codes": [],
            },
        }
    )["milestone"]


def test_public_state_has_six_values_and_fail_closed_precedence() -> None:
    assert {value.value for value in PublicState} == {
        "UNPREPARED",
        "DELIVERED",
        "ACTION_REQUIRED",
        "BENCH_READY",
        "RUNNING",
        "TERMINAL",
    }
    assert resolve_public_state(PublicStateFacts()) is PublicState.UNPREPARED
    assert (
        resolve_public_state(PublicStateFacts(delivered=True))
        is PublicState.DELIVERED
    )
    assert (
        resolve_public_state(
            PublicStateFacts(delivered=True, action_required=True)
        )
        is PublicState.ACTION_REQUIRED
    )
    assert resolve_public_state(running_facts()) is PublicState.RUNNING
    assert (
        resolve_public_state(running_facts(terminal=True))
        is PublicState.TERMINAL
    )


@pytest.mark.parametrize(
    "missing",
    (
        "attempt_bound",
        "process_alive",
        "heartbeat_fresh",
        "single_writer",
        "lease_valid",
        "live_phase",
    ),
)
def test_running_requires_all_live_proofs(missing: str) -> None:
    expected = (
        PublicState.DELIVERED
        if missing == "attempt_bound"
        else PublicState.BENCH_READY
    )
    assert resolve_public_state(running_facts(**{missing: False})) is expected


def test_projection_keeps_detail_phase_without_persisting_an_aggregate() -> None:
    projected = project_status(
        {
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "release_contract_proven": True,
                "play_prompt_ready": True,
                "canonical_attempt_bound": True,
            },
            "blocker": {"class": None, "reason_codes": []},
        }
    )

    assert projected["state"] == "BENCH_READY"
    assert projected["compatibility_phase"] == "WAITING_FOR_PLAY"


def test_projection_emits_separate_milestone_objects_and_play_prompt_capability() -> None:
    projected = project_status(
        {
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "release_contract_proven": True,
                "play_prompt_ready": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": False,
                "single_writer": True,
                "lease_valid": True,
            },
            "blocker": {"class": None, "reason_codes": []},
            "milestones": {
                "authorization": {
                    "live_motion_authorized": False,
                    "granted_tokens": ["token-abc"],
                },
                "acceptance_certificate": {
                    "release_contract_proven": True,
                    "admission": ["PROGRAM_LOADED_STOPPED"],
                },
                "ownership_binding": {
                    "canonical_attempt_bound": True,
                    "single_writer": True,
                    "lease_valid": True,
                },
            },
        }
    )

    assert projected["milestone"] == GovernedStatusMilestone.BRIDGE_WAITING_FOR_PLAY.value
    assert projected["milestones"]["liveness"] == {
        "bridge_process_alive": True,
        "bridge_heartbeat_fresh": False,
    }
    assert projected["milestones"]["authorization"] == {
        "live_motion_authorized": False,
        "granted_tokens": ["token-abc"],
    }
    assert projected["milestones"]["ownership_binding"] == {
        "canonical_attempt_bound": True,
        "single_writer": True,
        "lease_valid": True,
    }
    assert projected["milestones"]["acceptance_certificate"]["admission"] == [
        "PROGRAM_LOADED_STOPPED"
    ]
    assert projected["capabilities"] == {"play_prompt": True}
    assert projected["next_operator_action"] == "PRESS_PLAY"


def test_projection_with_blocker_removes_play_prompt_capability() -> None:
    projected = project_status(
        {
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "release_contract_proven": True,
                "play_prompt_ready": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "single_writer": True,
                "lease_valid": True,
            },
            "blocker": {"class": "PHYSICAL", "reason_codes": ["LOADED_PROGRAM_UNSUPPORTED"]},
            "milestones": {
                "acceptance_certificate": {"release_contract_proven": True},
                "ownership_binding": {
                    "canonical_attempt_bound": True,
                    "single_writer": True,
                    "lease_valid": True,
                },
            },
        }
    )

    assert projected["state"] == "ACTION_REQUIRED"
    assert projected["capabilities"]["play_prompt"] is False
    assert projected["next_operator_action"] is None


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, GovernedStatusMilestone.UNPREPARED.value),
        (
            {"release_contract_proven": True, "admission": []},
            GovernedStatusMilestone.RELEASE_CONTRACT_PROVEN.value,
        ),
        (
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": False,
                "admission": ["PROGRAM_LOADED_STOPPED"],
            },
            GovernedStatusMilestone.PROGRAM_LOADED_STOPPED.value,
        ),
        (
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": False,
                "admission": ["PROGRAM_LOADED_STOPPED"],
            },
            GovernedStatusMilestone.BRIDGE_PROCESS_STARTED.value,
        ),
        (
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": False,
                "admission": ["PROGRAM_LOADED_STOPPED"],
                "compatibility_phase": "WAITING_FOR_PLAY",
            },
            GovernedStatusMilestone.BRIDGE_WAITING_FOR_PLAY.value,
        ),
        (
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "admission": ["PROGRAM_LOADED_STOPPED"],
                "compatibility_phase": "WAITING_FOR_PLAY",
                "play_observed": True,
            },
            GovernedStatusMilestone.PLAY_OBSERVED.value,
        ),
        (
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "play_observed": True,
                "admission": ["PROGRAM_LOADED_STOPPED", "TRIAL_1_COMPLETE"],
                "compatibility_phase": "WAITING_FOR_PLAY",
            },
            GovernedStatusMilestone.TRIAL_1_COMPLETE.value,
        ),
        (
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "play_observed": True,
                "trial_1_complete": True,
                "next_arm_published": True,
                "admission": ["PROGRAM_LOADED_STOPPED"],
                "compatibility_phase": "WAITING_FOR_PLAY",
            },
            GovernedStatusMilestone.NEXT_ARM_PUBLISHED.value,
        ),
        (
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "admission": [
                    "PROGRAM_LOADED_STOPPED",
                    "TRIAL_1_COMPLETE",
                    "NEXT_ARM_PUBLISHED",
                    "CONTINUOUS_READY",
                ],
                "play_observed": True,
                "trial_1_complete": True,
                "next_arm_published": True,
                "continuous_ready": True,
                "compatibility_phase": "WAITING_FOR_PLAY",
            },
            GovernedStatusMilestone.CONTINUOUS_READY.value,
        ),
    ],
)
def test_top_level_milestone_progression(
    overrides: dict[str, object],
    expected: str,
) -> None:
    if overrides == {}:
        assert (
            project_status(
                {
                    "state": "UNPREPARED",
                    "predicates": {},
                    "blocker": {"class": None, "reason_codes": []},
                }
            )["milestone"]
            == expected
        )
        return

    assert _projected_milestone(overrides) == expected


def test_no_skip_milestone_progression_without_prior_prerequisites() -> None:
    projected = _projected_milestone(
        {
            "release_contract_proven": True,
            "canonical_attempt_bound": True,
            "bridge_process_alive": True,
            "bridge_heartbeat_fresh": True,
            "admission": ["PROGRAM_LOADED_STOPPED", "CONTINUOUS_READY"],
            "compatibility_phase": "WAITING_FOR_PLAY",
            "play_observed": True,
            "next_arm_published": True,
        }
    )
    assert projected == GovernedStatusMilestone.PLAY_OBSERVED.value


def test_authorization_is_copied_not_inferred_from_binding_predicates() -> None:
    projected = project_status(
        {
            "state": "WAITING_FOR_PLAY",
            "predicates": {
                "release_contract_proven": True,
                "play_prompt_ready": True,
                "canonical_attempt_bound": True,
                "single_writer": False,
                "lease_valid": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": False,
            },
            "milestones": {
                "authorization": {
                    "live_motion_authorized": False,
                    "granted_tokens": ["token-xyz"],
                },
                "acceptance_certificate": {
                    "release_contract_proven": True,
                    "play_prompt_ready": True,
                },
            },
            "blocker": {"class": None, "reason_codes": []},
        }
    )

    assert projected["milestones"]["authorization"] == {
        "live_motion_authorized": False,
        "granted_tokens": ["token-xyz"],
    }
    assert projected["milestones"]["ownership_binding"]["canonical_attempt_bound"] is True
    assert projected["capabilities"]["play_prompt"] is True
    assert projected["milestone"] == GovernedStatusMilestone.BRIDGE_PROCESS_STARTED.value


def test_fresh_heartbeat_without_play_observed_stays_waiting_for_play() -> None:
    assert (
        _projected_milestone(
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "compatibility_phase": "WAITING_FOR_PLAY",
                "admission": ["PROGRAM_LOADED_STOPPED"],
            }
        )
        == GovernedStatusMilestone.BRIDGE_WAITING_FOR_PLAY.value
    )


def test_live_phase_without_explicit_play_observed_does_not_advance_to_play_observed() -> None:
    assert (
        _projected_milestone(
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "compatibility_phase": "RUNNING",
                "admission": ["PROGRAM_LOADED_STOPPED"],
            }
        )
        == GovernedStatusMilestone.BRIDGE_PROCESS_STARTED.value
    )


def test_next_arm_published_without_trial_cannot_skip_advance() -> None:
    assert (
        _projected_milestone(
            {
                "release_contract_proven": True,
                "canonical_attempt_bound": True,
                "bridge_process_alive": True,
                "bridge_heartbeat_fresh": True,
                "compatibility_phase": "WAITING_FOR_PLAY",
                "admission": ["PROGRAM_LOADED_STOPPED"],
                "play_observed": True,
                "next_arm_published": True,
            }
        )
        == GovernedStatusMilestone.PLAY_OBSERVED.value
    )
