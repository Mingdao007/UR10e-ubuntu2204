"""Small r006 identity adapter over the existing durable queue contract.

The production owner remains the V3 durable receiver used by r005.  r006
keeps its own typed point metadata because the r006 graph is intentionally
unbounded and cannot be coerced into the bounded r005 candidate catalog.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from step5d_autotune_v4_r005.contracts import Candidate
from step5d_autotune_v4_r005.queue import (
    QueueCapacityError,
    QueueEntry,
    QueueError as V3QueueError,
    V3DurableQueueAdapter,
)

from .contracts import canonical_bytes
from .lattice import ParameterPoint


QUEUE_PRIMITIVE = "step5d_autotune_v4_r005.queue.V3DurableQueueAdapter"


class R006V3DurableQueueAdapter(V3DurableQueueAdapter):
    """Keep the V3 durable transport and change only r006 repeat semantics.

    The parent implementation's fixed repeatable set is a campaign-policy
    gate, not a transport-safety gate.  r006 deliberately repeats anchor and
    route points during both 25-trial warm-start groups, so that policy belongs
    in this additive adapter rather than in frozen r005 source.
    """

    repeatable_kinds = frozenset(
        {
            "QUALIFICATION",
            "BOOTSTRAP_PD",
            "WARM_START_1",
            "WARM_START_2",
            "ROUTE",
            "RETEST",
        }
    )

    def enqueue(
        self,
        candidate: Candidate,
        *,
        kind: str,
        epoch: int,
        request_uid: str | None = None,
    ) -> QueueEntry:
        if len(self.pending()) >= self.max_pending:
            raise QueueCapacityError("at most two pending rows are allowed")
        if (
            self._ticket is not None
            and self._ticket.candidate_uid == candidate.candidate_uid
            and kind not in self.repeatable_kinds
        ):
            raise V3QueueError("candidate duplicates the physical inflight row")
        if any(
            entry.candidate_uid == candidate.candidate_uid
            and kind not in self.repeatable_kinds
            and entry.kind not in self.repeatable_kinds
            for entry in self.pending()
        ):
            raise V3QueueError("candidate duplicates a pending row")
        ordinal = len(self._metadata) + 1
        requested_uid = request_uid or (
            "r006:request:"
            + hashlib.sha256(
                canonical_bytes(
                    {
                        "candidate_uid": candidate.candidate_uid,
                        "kind": kind,
                        "epoch": epoch,
                        "ordinal": ordinal,
                    }
                )
            ).hexdigest()
        )
        row = self._load_v3().submit_manifest(
            self.root,
            launch_profile_path=self.launch_profile_path,
            rows=(
                {
                    **candidate.canonical,
                    "source": f"r006_{kind.lower()}",
                    "position": "tail",
                    "occurrence_nonce": hashlib.sha256(requested_uid.encode()).hexdigest()[:32],
                },
            ),
        )[0]
        actual_uid = str(row["request_uid"])
        entry = QueueEntry(actual_uid, candidate, kind, epoch)
        if actual_uid in self._metadata and self._metadata[actual_uid] != entry:
            raise V3QueueError("V3 request UID changed its r006 metadata")
        self._metadata[actual_uid] = entry
        self._append(self._metadata_path, entry.as_dict())
        return entry


@dataclass(frozen=True)
class QueueItem:
    request_uid: str
    point: ParameterPoint
    kind: str
    logical_request_uid: str

    @property
    def point_uid(self) -> str:
        return self.point.uid

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_uid": self.request_uid,
            "point": self.point.canonical,
            "point_key": list(self.point.key),
            "kind": self.kind,
            "logical_request_uid": self.logical_request_uid,
        }


@dataclass(frozen=True)
class Dispatch:
    item: QueueItem
    dispatch_sequence: int


class OfflineR006DurableQueue:
    """Offline queue with two pending rows and one physical inflight row."""

    max_pending = 2
    physical_inflight_max = 1
    schema = "step5d.autotune-v4/r006-queue-v1"

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise V3QueueError("r006 queue root must be a regular directory")
        self.path = self.root / "events.jsonl"
        self._pending: list[QueueItem] = []
        self._inflight: Dispatch | None = None
        self._completed: list[Dispatch] = []
        self._cancelled: list[QueueItem] = []
        self._next_dispatch = 1
        self._replay()

    @staticmethod
    def _uid(point: ParameterPoint, kind: str, ordinal: int) -> str:
        material = {"point_uid": point.uid, "kind": kind, "ordinal": ordinal}
        return "r006:request:" + hashlib.sha256(canonical_bytes(material)).hexdigest()

    def _append(self, event: dict[str, Any]) -> None:
        row = {"schema": self.schema, **event}
        with self.path.open("ab") as stream:
            stream.write(canonical_bytes(row) + b"\n")
            stream.flush()
            import os

            os.fsync(stream.fileno())
        self._fsync_directory()

    def _fsync_directory(self) -> None:
        import os

        descriptor = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _item(payload: dict[str, Any]) -> QueueItem:
        key = payload.get("point_key")
        if not isinstance(key, list) or len(key) != 7:
            raise V3QueueError("r006 queue point key is invalid")
        from .lattice import IMode

        point = ParameterPoint(
            int(key[0]), int(key[1]), int(key[2]), IMode(str(key[3])),
            None if key[4] is None else int(key[4]), int(key[5]), int(key[6]),
        )
        request_uid = str(payload["request_uid"])
        logical = str(payload.get("logical_request_uid", request_uid))
        return QueueItem(request_uid, point, str(payload["kind"]), logical)

    def _replay(self) -> None:
        if not self.path.exists():
            return
        for number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                event = json.loads(line)
                if event.get("schema") != self.schema:
                    raise ValueError("schema")
                name = event["event"]
            except (UnicodeError, json.JSONDecodeError, KeyError, ValueError) as exc:
                raise V3QueueError(f"r006 queue event {number} is invalid") from exc
            if name == "ENQUEUE":
                self._pending.append(self._item(event))
            elif name == "DISPATCH":
                if self._inflight is not None or not self._pending:
                    raise V3QueueError("r006 queue replay has invalid inflight state")
                item = self._item(event)
                if self._pending[0] != item:
                    raise V3QueueError("r006 queue replay is not FIFO")
                self._pending.pop(0)
                sequence = int(event["dispatch_sequence"])
                self._inflight = Dispatch(item, sequence)
                self._next_dispatch = max(self._next_dispatch, sequence + 1)
            elif name == "COMPLETE":
                if self._inflight is None:
                    raise V3QueueError("r006 queue completion has no inflight row")
                self._completed.append(self._inflight)
                self._inflight = None
            elif name == "CANCEL":
                item = self._item(event)
                self._pending = [row for row in self._pending if row != item]
                self._cancelled.append(item)
            elif name == "RECONCILE_HOME":
                if self._inflight is None:
                    raise V3QueueError("r006 queue reconciliation has no inflight row")
                self._pending.insert(0, self._inflight.item)
                self._inflight = None
            else:
                raise V3QueueError(f"r006 queue event name is unknown: {name}")

    @property
    def inflight(self) -> Dispatch | None:
        return self._inflight

    def pending(self) -> tuple[QueueItem, ...]:
        return tuple(self._pending)

    def enqueue(self, point: ParameterPoint, *, kind: str, logical_request_uid: str | None = None) -> QueueItem:
        if len(self._pending) >= self.max_pending:
            raise QueueCapacityError("r006 queue allows exactly two pending rows")
        if self._inflight is not None and self._inflight.item.point == point and kind not in {"QUALIFICATION", "RETEST"}:
            raise V3QueueError("r006 point duplicates physical inflight row")
        if any(item.point == point and item.kind == kind for item in self._pending):
            raise V3QueueError("r006 point duplicates pending row")
        logical = logical_request_uid or f"r006:logical:{len(self._pending) + len(self._completed) + 1}"
        request_uid = self._uid(point, kind, len(self._pending) + len(self._completed) + 1)
        item = QueueItem(request_uid, point, kind, logical)
        self._pending.append(item)
        self._append({"event": "ENQUEUE", **item.as_dict()})
        return item

    def prepare_next(self) -> Dispatch | None:
        if self._inflight is not None:
            return self._inflight
        if not self._pending:
            return None
        item = self._pending.pop(0)
        self._inflight = Dispatch(item, self._next_dispatch)
        self._next_dispatch += 1
        self._append({"event": "DISPATCH", "dispatch_sequence": self._inflight.dispatch_sequence, **item.as_dict()})
        return self._inflight

    def complete(self, dispatch: Dispatch, *, status: str, detail: str = "") -> None:
        if self._inflight != dispatch:
            raise V3QueueError("r006 completion does not match physical inflight row")
        if status not in {"OBJECTIVE", "SAFE_NONTRAINABLE", "CODE_OR_EVIDENCE_BUG", "SAFETY_OR_RETURN_FAILURE"}:
            raise V3QueueError("r006 disposition is not typed")
        self._append({"event": "COMPLETE", "dispatch_sequence": dispatch.dispatch_sequence, "status": status, "detail": detail, **dispatch.item.as_dict()})
        self._completed.append(dispatch)
        self._inflight = None

    def cancel_pending(self, *, reason: str) -> tuple[QueueItem, ...]:
        if not reason or "\n" in reason:
            raise V3QueueError("r006 cancellation reason is invalid")
        cancelled = tuple(self._pending)
        for item in cancelled:
            self._append({"event": "CANCEL", "reason": reason, **item.as_dict()})
        self._cancelled.extend(cancelled)
        self._pending.clear()
        return cancelled

    def reconcile_inflight_after_home(self) -> QueueItem | None:
        if self._inflight is None:
            return None
        dispatch = self._inflight
        self._append({"event": "RECONCILE_HOME", "dispatch_sequence": dispatch.dispatch_sequence, **dispatch.item.as_dict()})
        self._pending.insert(0, dispatch.item)
        self._inflight = None
        return dispatch.item


__all__ = [
    "Dispatch",
    "QUEUE_PRIMITIVE",
    "QueueItem",
    "OfflineR006DurableQueue",
    "R006V3DurableQueueAdapter",
]
