"""Small, composable guard-policy seam for Step5d command proposals.

The guard chain is deliberately independent of queue, release, startup, and
controller lifecycle.  An empty chain is the algebraic identity: it returns
the exact proposed command without allocating or evaluating an optional
policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Protocol


@dataclass(frozen=True)
class GuardDecision:
    """Observable result of one guard evaluation."""

    proposed_command: Any
    command: Any
    stop: bool = False
    reason: str = "identity"
    policy_id: str = "identity"
    evidence: Mapping[str, Any] = field(default_factory=dict)


class GuardPolicy(Protocol):
    """Minimal policy contract; policies cannot own lifecycle resources."""

    policy_id: str

    def evaluate(
        self, observation: Mapping[str, Any], proposed_command: Any
    ) -> GuardDecision:
        ...


class IdentityGuardPolicy:
    """Explicit no-policy identity used by the no-tube profile."""

    policy_id = "identity"

    def evaluate(
        self, observation: Mapping[str, Any], proposed_command: Any
    ) -> GuardDecision:
        del observation
        return GuardDecision(
            proposed_command=proposed_command,
            command=proposed_command,
            reason="identity",
            policy_id=self.policy_id,
        )


class GuardPolicyChain:
    """Compose independent policies in declared order."""

    def __init__(self, policies: Iterable[GuardPolicy] = ()) -> None:
        self._policies = tuple(policies)
        for policy in self._policies:
            policy_id = getattr(policy, "policy_id", None)
            if not isinstance(policy_id, str) or not policy_id:
                raise ValueError("guard policy_id must be a non-empty string")

    @property
    def policy_ids(self) -> tuple[str, ...]:
        return tuple(policy.policy_id for policy in self._policies)

    def evaluate(
        self, observation: Mapping[str, Any], proposed_command: Any
    ) -> GuardDecision:
        if not self._policies:
            return IdentityGuardPolicy().evaluate(observation, proposed_command)

        current = proposed_command
        decisions: list[dict[str, Any]] = []
        for policy in self._policies:
            decision = policy.evaluate(observation, current)
            if not isinstance(decision, GuardDecision):
                raise TypeError("guard policy must return GuardDecision")
            decisions.append(
                {
                    "policy_id": decision.policy_id,
                    "stop": decision.stop,
                    "reason": decision.reason,
                }
            )
            current = decision.command
            if decision.stop:
                return GuardDecision(
                    proposed_command=proposed_command,
                    command=current,
                    stop=True,
                    reason=decision.reason,
                    policy_id=decision.policy_id,
                    evidence={"chain": decisions},
                )
        return GuardDecision(
            proposed_command=proposed_command,
            command=current,
            stop=False,
            reason="allowed",
            policy_id="guard-chain",
            evidence={"chain": decisions},
        )


__all__ = ["GuardDecision", "GuardPolicy", "GuardPolicyChain", "IdentityGuardPolicy"]
