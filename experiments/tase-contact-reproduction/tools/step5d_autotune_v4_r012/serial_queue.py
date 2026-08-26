"""R012-owned serial one-slot queue and append-only attempt state."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .common import R012ValueError, canonical_bytes, json_tree


class SerialQueueError(R012ValueError):
    """The R012 one-slot queue cannot accept or resume a state transition."""


@dataclass(frozen=True)
class QueueItem:
    request_id: str
    dispatch_id: str
    candidate: Mapping[str, Any]
    kind: str
    route_id: str
    session_id: str
    session_epoch: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id, "dispatch_id": self.dispatch_id,
            "candidate": json_tree(self.candidate), "kind": self.kind,
            "route_id": self.route_id, "session_id": self.session_id,
            "session_epoch": self.session_epoch,
        }


class SerialOneSlotQueue:
    """At most one pending or in-flight item; refill happens only after result."""

    def __init__(self, path: Path | None = None, *, route_id: str = "r012-route-local", session_id: str = "r012-session-local", session_epoch: int = 1) -> None:
        self.path = Path(path) if path is not None else None
        self.route_id, self.session_id, self.session_epoch = route_id, session_id, session_epoch
        self._next = 1
        self.pending_item: QueueItem | None = None
        self.in_flight: QueueItem | None = None
        self.results: list[dict[str, Any]] = []
        if self.path is not None and self.path.exists():
            self._restore()

    def _append(self, record: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            stream.write(canonical_bytes(record) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _new_id(self, prefix: str) -> str:
        value = f"{prefix}-{self._next:06d}"
        if prefix == "dispatch":
            self._next += 1
        return value

    def enqueue(self, candidate: Mapping[str, Any], *, kind: str, route_id: str | None = None, session_id: str | None = None, session_epoch: int | None = None) -> QueueItem:
        if self.pending_item is not None or self.in_flight is not None:
            raise SerialQueueError("R012 serial q=1 already has a pending or in-flight item")
        request_id = self._new_id("request")
        dispatch_id = f"dispatch-{self._next:06d}"
        item = QueueItem(
            request_id, dispatch_id, json_tree(candidate), str(kind),
            route_id or self.route_id, session_id or self.session_id,
            int(session_epoch if session_epoch is not None else self.session_epoch),
        )
        self.pending_item = item
        self._append({"schema": "step5d.autotune-v4/r012-serial-state-v1", "record_type": "request", **item.as_dict()})
        return item

    def dispatch(self) -> QueueItem:
        if self.pending_item is None or self.in_flight is not None:
            raise SerialQueueError("R012 dispatch requires exactly one pending item")
        self.in_flight = self.pending_item
        self.pending_item = None
        self._append({"schema": "step5d.autotune-v4/r012-serial-state-v1", "record_type": "dispatch", **self.in_flight.as_dict()})
        return self.in_flight

    def result(self, value: Mapping[str, Any]) -> dict[str, Any]:
        if self.in_flight is None:
            raise SerialQueueError("R012 result requires one in-flight item")
        if not isinstance(value, Mapping):
            raise SerialQueueError("R012 result must be readable JSON")
        ordinal = int(self.in_flight.dispatch_id.rsplit("-", 1)[-1])
        result = {"schema": "step5d.autotune-v4/r012-serial-state-v1", "record_type": "result", **self.in_flight.as_dict(), "result": json_tree(value)}
        self._append(result)
        self.results.append(result)
        self.in_flight = None
        self._next = max(self._next, ordinal + 1)
        return result

    def _restore(self) -> None:
        try:
            records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError) as exc:
            raise SerialQueueError("R012 serial state is unreadable") from exc
        for record in records:
            if not isinstance(record, dict) or record.get("schema") != "step5d.autotune-v4/r012-serial-state-v1":
                raise SerialQueueError("R012 serial state schema differs")
            item = QueueItem(record["request_id"], record["dispatch_id"], record["candidate"], record["kind"], record["route_id"], record["session_id"], int(record["session_epoch"]))
            ordinal = int(item.dispatch_id.rsplit("-", 1)[-1])
            self._next = max(self._next, ordinal + 1)
            if record["record_type"] == "request":
                if self.pending_item is not None or self.in_flight is not None:
                    raise SerialQueueError("R012 restored state has two active items")
                self.pending_item = item
            elif record["record_type"] == "dispatch":
                if self.pending_item is None or self.pending_item.as_dict() != item.as_dict():
                    raise SerialQueueError("R012 restored dispatch does not match request")
                self.pending_item = None
                self.in_flight = item
            elif record["record_type"] == "result":
                if self.in_flight is None or self.in_flight.as_dict() != item.as_dict():
                    raise SerialQueueError("R012 restored result does not match dispatch")
                self.results.append(record)
                self.in_flight = None
            else:
                raise SerialQueueError("R012 serial record type differs")


__all__ = ["QueueItem", "SerialOneSlotQueue", "SerialQueueError"]
