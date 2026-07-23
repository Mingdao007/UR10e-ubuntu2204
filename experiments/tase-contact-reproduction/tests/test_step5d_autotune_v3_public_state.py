from __future__ import annotations

import pytest

from step5d_autotune_v3.public_state import (
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
                "offline_proven": True,
                "play_prompt_ready": True,
                "canonical_attempt_bound": True,
            },
            "blocker": {"class": None, "reason_codes": []},
        }
    )

    assert projected["state"] == "BENCH_READY"
    assert projected["compatibility_phase"] == "WAITING_FOR_PLAY"
