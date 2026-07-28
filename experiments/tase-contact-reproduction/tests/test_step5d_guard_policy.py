from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.guard_policy import (  # noqa: E402
    GuardDecision,
    GuardPolicyChain,
)


def test_empty_guard_chain_is_exact_identity() -> None:
    command = {"qdot": [0.1, 0.2], "candidate_id": "candidate-1"}
    decision = GuardPolicyChain().evaluate({"tcp": [0.0, 0.0, 0.0]}, command)

    assert decision.command is command
    assert decision.proposed_command is command
    assert decision.stop is False
    assert decision.policy_id == "identity"
    assert decision.reason == "identity"


class _StopPolicy:
    policy_id = "test-stop"

    def evaluate(self, observation, proposed_command):
        assert observation["phase"] == "test"
        return GuardDecision(
            proposed_command=proposed_command,
            command={"safe": True},
            stop=True,
            reason="test-stop",
            policy_id=self.policy_id,
        )


def test_guard_chain_stops_at_policy_decision() -> None:
    decision = GuardPolicyChain([_StopPolicy()]).evaluate(
        {"phase": "test"}, {"qdot": [1.0]}
    )

    assert decision.stop is True
    assert decision.reason == "test-stop"
    assert decision.command == {"safe": True}
    assert decision.evidence["chain"] == [
        {"policy_id": "test-stop", "stop": True, "reason": "test-stop"}
    ]


def test_invalid_policy_result_fails_closed() -> None:
    class BadPolicy:
        policy_id = "bad"

        def evaluate(self, observation, proposed_command):
            return None

    with pytest.raises(TypeError):
        GuardPolicyChain([BadPolicy()]).evaluate({}, {})
