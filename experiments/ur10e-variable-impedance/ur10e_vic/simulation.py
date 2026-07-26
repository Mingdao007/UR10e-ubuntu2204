"""Offline-only simulator capability adapter for the existing VIC policies.

This module has no controller, network, upload, or live authorization surface.
It grants command capability only to the three deterministic phase-1 policies
inside a simulator.  DBIL remains physically separated from the command path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
import struct
from typing import Sequence

from .backends import (
    CapabilitySeparatedShadowMux,
    CapabilitySeparatedVelocityMux,
    Step5bTwistInput,
    VelocityAdmittanceSurrogate,
)
from .constraints import ProposalSupervisor, SupervisionDecision
from .contracts import ImpedanceObservation, ImpedanceProposal
from .policies import (
    DBILShadowPolicy,
    DirectionalVICPolicy,
    FixedImpedancePolicy,
    ImpedancePolicy,
    ScriptedPhasePolicy,
)


SIMULATION_CLAIM_BOUNDARY = (
    "offline_simulation_only; not package acceptance; not live acceptance; "
    "not reproduction completion"
)
_SIMULATION_ACTIVE_POLICY_TYPES = (
    FixedImpedancePolicy,
    ScriptedPhasePolicy,
    DirectionalVICPolicy,
)


def actual_command_bytes(command: Sequence[float], valid: bool) -> bytes:
    """Canonical bit pattern used to prove shadow command invariance."""

    values = tuple(float(value) for value in command)
    if len(values) != 6 or not all(math.isfinite(value) for value in values):
        raise ValueError("actual simulator command must contain six finite values")
    return struct.pack(">?6d", bool(valid), *values)


@dataclass(frozen=True)
class SimulatorTickResult:
    actual_command: tuple[float, ...] | None
    actual_command_valid: bool
    route: str
    execution_mode: str
    proposal: ImpedanceProposal | None
    supervision: SupervisionDecision | None
    claim_boundary: str = SIMULATION_CLAIM_BOUNDARY
    live_motion_authorized: bool = False
    dbil_active_enabled: bool = False

    @property
    def actual_command_bit_pattern(self) -> bytes | None:
        if self.actual_command is None:
            return None
        return actual_command_bytes(self.actual_command, self.actual_command_valid)


class VICSimulatorAdapter:
    """Run an existing VIC policy through supervision and an offline backend.

    ``baseline`` and ``shadow`` always route the caller's established simulator
    command through the capability-separated shadow mux. ``simulation_active``
    requires an explicit constructor capability and an exact allowlisted policy
    type.  Exact type matching is intentional: ``DBILShadowPolicy`` subclasses
    ``DirectionalVICPolicy`` and must never inherit its command capability.
    """

    def __init__(
        self,
        policy: ImpedancePolicy,
        supervisor: ProposalSupervisor,
        backend: VelocityAdmittanceSurrogate,
        *,
        execution_mode: str,
        simulation_active_capability: bool = False,
    ) -> None:
        if execution_mode not in {"baseline", "shadow", "simulation_active"}:
            raise ValueError("invalid simulator execution mode")
        if execution_mode == "simulation_active":
            if not simulation_active_capability:
                raise ValueError("simulation-active VIC requires explicit capability")
            if type(policy) not in _SIMULATION_ACTIVE_POLICY_TYPES:
                if isinstance(policy, DBILShadowPolicy):
                    raise ValueError("DBIL is permanently shadow-only")
                raise ValueError("policy is not allowlisted for simulation-active VIC")
        self.policy = policy
        self.supervisor = supervisor
        self.execution_mode = execution_mode
        self._shadow_mux = CapabilitySeparatedShadowMux()
        self._active_mux = CapabilitySeparatedVelocityMux(
            backend,
            active_capability=(execution_mode == "simulation_active"),
        )

    @staticmethod
    def _baseline(values: Sequence[float]) -> tuple[float, ...]:
        result = tuple(float(value) for value in values)
        if len(result) != 6 or not all(math.isfinite(value) for value in result):
            raise ValueError("baseline simulator command must contain six finite values")
        return result

    def tick(
        self,
        observation: ImpedanceObservation,
        baseline_command: Sequence[float],
        *,
        baseline_valid: bool = True,
        dt_s: float = 0.005,
        step5b_input: Step5bTwistInput | None = None,
        policy_sample_available: bool = True,
    ) -> SimulatorTickResult:
        if not math.isfinite(dt_s) or dt_s <= 0.0:
            raise ValueError("simulator dt_s must be finite and positive")
        baseline = self._baseline(baseline_command)
        if self.execution_mode == "baseline":
            selected = self._shadow_mux.select(None, baseline, baseline_valid)
            return SimulatorTickResult(
                selected.command,
                selected.command_valid,
                selected.route,
                self.execution_mode,
                None,
                None,
            )

        incoming = self.policy.propose(observation) if policy_sample_available else None
        decision = self.supervisor.step(
            incoming,
            now_s=observation.timestamp_s,
            dt_s=dt_s,
            fallback_zft=observation.nominal_zft,
        )
        if self.execution_mode == "shadow":
            selected = self._shadow_mux.select(
                decision.proposal,
                baseline,
                baseline_valid,
            )
            return SimulatorTickResult(
                selected.command,
                selected.command_valid,
                selected.route,
                self.execution_mode,
                decision.proposal,
                decision,
            )

        if decision.request_stop or not decision.proposal.valid:
            return SimulatorTickResult(
                None,
                False,
                f"supervisor_{decision.mode}",
                self.execution_mode,
                decision.proposal,
                decision,
            )

        # The policy and supervisor remain shadow-safe.  Command capability is
        # attached only at this offline simulator boundary after exact-type
        # authorization; DBIL cannot reach this branch.
        active_proposal = replace(decision.proposal, shadow_only=False)
        selected = self._active_mux.select(
            observation,
            active_proposal,
            baseline,
            step5b_input=step5b_input,
        )
        return SimulatorTickResult(
            selected.qdot_rad_s,
            selected.mode == "command" and selected.qdot_rad_s is not None,
            selected.route,
            self.execution_mode,
            decision.proposal,
            decision,
        )
