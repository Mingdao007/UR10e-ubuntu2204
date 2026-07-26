"""Durable exactly-once COMPLETE_AT_HOME intent for rolling-v1 releases."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from step5d_autotune_journal import TpSnapshot
from step5d_autotune_state_machine import HostCommand, HostPacket


class CompletionError(RuntimeError):
    pass


class CompletionJournal:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    @staticmethod
    def _packet(payload: dict[str, object]) -> HostPacket:
        return HostPacket(
            campaign_epoch=int(payload["campaign_epoch"]),
            trial_id=int(payload["trial_id"]),
            command=HostCommand(int(payload["command"])),
            candidate_token=int(payload["candidate_token"]),
            execution_profile_id=int(payload["execution_profile_id"]),
            command_seq=int(payload["command_seq"]),
            logical_batch_sequence=int(payload["logical_batch_sequence"]),
        )

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self) -> tuple[HostPacket, str] | None:
        if not self.path.exists():
            return None
        payload = json.loads(self.path.read_text())
        if payload.get("schema") != "step5d.rolling-v1/pending-completion-v1":
            raise CompletionError("pending completion schema differs")
        packet = self._packet(payload["packet"])
        status = str(payload.get("status"))
        if packet.command is not HostCommand.COMPLETE_AT_HOME or status not in {"pending", "consumed"}:
            raise CompletionError("pending completion identity differs")
        return packet, status

    def prepare(self, packet: HostPacket) -> HostPacket:
        if packet.command is not HostCommand.COMPLETE_AT_HOME:
            raise CompletionError("completion journal accepts COMPLETE_AT_HOME only")
        existing = self.load()
        if existing is not None:
            if existing[0] != packet:
                raise CompletionError("durable completion packet differs")
            return existing[0]
        self._write(
            {
                "schema": "step5d.rolling-v1/pending-completion-v1",
                "status": "pending",
                "packet": {**asdict(packet), "command": int(packet.command)},
            }
        )
        return packet

    def mark_consumed(self, snapshot: TpSnapshot) -> None:
        loaded = self.load()
        if loaded is None:
            raise CompletionError("completion consumption lacks durable intent")
        packet, status = loaded
        if status == "consumed":
            return
        if any(
            (
                snapshot.state != "READY_HOME_CLOSED",
                snapshot.campaign_epoch_echo != packet.campaign_epoch,
                snapshot.trial_id_echo != packet.trial_id,
                snapshot.candidate_token_echo != packet.candidate_token,
                snapshot.execution_profile_integer_id_echo != packet.execution_profile_id,
                snapshot.consumed_command_seq != packet.command_seq,
                snapshot.logical_batch_sequence_echo != packet.logical_batch_sequence,
            )
        ):
            raise CompletionError("COMPLETE consumption differs from durable packet")
        self._write(
            {
                "schema": "step5d.rolling-v1/pending-completion-v1",
                "status": "consumed",
                "packet": {**asdict(packet), "command": int(packet.command)},
                "tp_snapshot": snapshot.payload(),
            }
        )
