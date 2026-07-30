"""Transport-neutral V4 live-writer composition and attempt-ledger primitive."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .baseline_ledger import BaselineQualificationLedger
from .contracts import V4Candidate, V4Contract
from .control import RuntimeDecision, RuntimeObservation, V4ControlPrimitive
from .policies import V4PolicyBundle
from .wire import CommandMode, SensorPacket, WirePacket, build_wire_packet


@dataclass(frozen=True)
class AdapterTick:
    observation: RuntimeObservation
    sensor: SensorPacket
    proposed_qdot: tuple[float, float, float, float, float, float]
    jacobian_6x6: tuple[tuple[float, ...], ...]
    normal_base: tuple[float, float, float]
    observed_model_hashes: Mapping[str, str]
    command_sequence: int


@dataclass(frozen=True)
class AdapterResult:
    decision: RuntimeDecision
    packet: WirePacket
    ledger_row: Mapping[str, object]


class V4RuntimeAdapter:
    """Compose control and wire gates without owning a sensor or RTDE transport.

    A live process may replace the providers/transport independently, but it
    must publish only the returned packet under the canonical single-writer
    lease. Startup pending is a zero-qdot hold; every frozen or external-stop
    decision is stop-dominant.
    """

    def __init__(
        self,
        contract: V4Contract,
        candidate: V4Candidate,
        *,
        policies: V4PolicyBundle | None = None,
        baseline_ledger: BaselineQualificationLedger | None = None,
        attempt_id: str = "offline-attempt",
    ) -> None:
        self.control = V4ControlPrimitive(
            contract,
            candidate,
            policies=policies or V4PolicyBundle.defaults(),
            baseline_ledger=baseline_ledger,
            attempt_id=attempt_id,
        )
        self.contract = contract
        self.candidate = candidate

    def tick(self, value: AdapterTick) -> AdapterResult:
        decision = self.control.step(
            value.observation,
            proposed_qdot=value.proposed_qdot,
            jacobian_6x6=value.jacobian_6x6,
            normal_base=value.normal_base,
            observed_model_hashes=value.observed_model_hashes,
        )
        startup_hold = decision.reason in {
            "startup_two_increments_pending",
            "first_actual_dt_pending",
        }
        structural_stop = decision.stop and not startup_hold
        proposed = (0.0,) * 6 if startup_hold else decision.qdot
        if startup_hold:
            mode = CommandMode.HOLD
        elif decision.stop:
            mode = CommandMode.STOP
        elif decision.full_path_allowed:
            mode = CommandMode.PATH
        elif decision.retract_allowed:
            mode = CommandMode.RETRACT
        else:
            mode = CommandMode.BASELINE
        packet = build_wire_packet(
            self.contract,
            self.candidate,
            sensor=value.sensor,
            proposed_qdot=proposed,
            jacobian_6x6=value.jacobian_6x6,
            normal_base=value.normal_base,
            observed_model_hashes=value.observed_model_hashes,
            internal_setpoint_n=decision.internal_setpoint_n,
            command_sequence=value.command_sequence,
            baseline_qualification=self.control.baseline_ledger.snapshot(),
            command_mode=mode,
            structural_stop=structural_stop,
        )
        row = {
            "schema": "step5d.autotune-v4/attempt-ledger-row-v1",
            "campaign_fingerprint": self.contract.campaign_fingerprint,
            "eoat_sha256": self.contract.eoat_sha256,
            "candidate_uid": self.candidate.candidate_uid,
            "target_force_n": self.candidate.target_force_n,
            "monotonic_s": value.observation.monotonic_s,
            "command_sequence": value.command_sequence,
            "phase": decision.phase,
            "internal_setpoint_n": decision.internal_setpoint_n,
            "stop": packet.stop_dominant,
            "freeze_bo": decision.freeze_bo,
            "reason": packet.reason or decision.reason,
            "qdot": list(packet.gate.qdot if not packet.stop_dominant else (0.0,) * 6),
            "layout_code": packet.doubles_by_register[47],
        }
        return AdapterResult(decision=decision, packet=packet, ledger_row=row)


def seal_attempt_ledger(
    rows: Sequence[Mapping[str, object]],
    destination: Path,
    *,
    terminal_closure: Mapping[str, object],
    completion_closure: Mapping[str, object],
    replay_closure: Mapping[str, object],
) -> Mapping[str, object]:
    """Seal complete rows and closure identities; never marks GP eligibility."""
    encoded_rows = (
        "\n".join(
            json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for row in rows
        )
        + "\n"
    ).encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"attempt ledger is immutable: {destination}")
    destination.write_bytes(encoded_rows)
    return {
        "schema": "step5d.autotune-v4/attempt-ledger-seal-v1",
        "rows": len(rows),
        "ledger_sha256": hashlib.sha256(encoded_rows).hexdigest(),
        "terminal_closure": dict(terminal_closure),
        "completion_closure": dict(completion_closure),
        "replay_closure": dict(replay_closure),
        "gp_eligibility_decided": False,
    }


__all__ = [
    "AdapterResult",
    "AdapterTick",
    "V4RuntimeAdapter",
    "seal_attempt_ledger",
]
