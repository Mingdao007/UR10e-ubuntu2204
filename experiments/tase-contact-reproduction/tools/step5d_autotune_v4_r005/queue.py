"""Offline durable queue ports for the V4 r005 host loop.

The production-shaped adapter delegates request/dispatch/receipt storage to
the existing V3 durable receiver.  The small event-log implementation is an
offline/FakeRTDE port used by tests; it has the same one-inflight and bounded
pending contract without importing a controller-facing path.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .contracts import Candidate, canonical_bytes


class QueueError(RuntimeError):
    """A durable queue identity, capacity, or state transition is invalid."""


class QueueCapacityError(QueueError):
    """The bounded asynchronous queue cannot accept another pending row."""


@dataclass(frozen=True)
class QueueEntry:
    request_uid: str
    candidate: Candidate
    kind: str
    epoch: int
    logical_request_uid: str | None = None

    def __post_init__(self) -> None:
        if self.logical_request_uid is None:
            object.__setattr__(self, "logical_request_uid", self.request_uid)

    @property
    def candidate_uid(self) -> str:
        return self.candidate.candidate_uid

    @property
    def logical_uid(self) -> str:
        return str(self.logical_request_uid)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_uid": self.request_uid,
            "candidate": self.candidate.canonical,
            "kind": self.kind,
            "epoch": self.epoch,
            "logical_request_uid": self.logical_uid,
        }


@dataclass(frozen=True)
class DispatchTicket:
    entry: QueueEntry
    dispatch_sequence: int

    @property
    def request_uid(self) -> str:
        return self.entry.request_uid

    @property
    def candidate(self) -> Candidate:
        return self.entry.candidate

    @property
    def candidate_uid(self) -> str:
        return self.entry.candidate_uid

    @property
    def kind(self) -> str:
        return self.entry.kind

    @property
    def epoch(self) -> int:
        return self.entry.epoch


class QueuePort(Protocol):
    max_pending: int

    def enqueue(
        self,
        candidate: Candidate,
        *,
        kind: str,
        epoch: int,
        request_uid: str | None = None,
    ) -> QueueEntry: ...

    def prepare_next(self) -> DispatchTicket | None: ...

    def complete(
        self, ticket: DispatchTicket, *, status: str, detail: str | None = None
    ) -> None: ...

    def cancel_pending(self, *, reason: str) -> tuple[QueueEntry, ...]: ...

    def reconcile_inflight_after_home(self) -> QueueEntry | None: ...

    def record_execution(
        self,
        ticket: DispatchTicket,
        *,
        attempt_sequence: int,
        execution_id: str,
    ) -> None: ...

    def pending(self) -> tuple[QueueEntry, ...]: ...

    @property
    def inflight(self) -> DispatchTicket | None: ...


def _uid(candidate: Candidate, kind: str, epoch: int, ordinal: int) -> str:
    return "r005:request:" + hashlib.sha256(
        canonical_bytes(
            {
                "candidate_uid": candidate.candidate_uid,
                "kind": kind,
                "epoch": epoch,
                "ordinal": ordinal,
            }
        )
    ).hexdigest()


def _crash_resume_occurrence_nonce(logical_request_uid: str, dispatch_sequence: int) -> str:
    return hashlib.sha256(
        f"{logical_request_uid}:crash-resume:{dispatch_sequence}".encode()
    ).hexdigest()[:32]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class OfflineDurableQueue:
    """Crash-replayable queue for offline tests and FakeRTDE integration."""

    max_pending = 2
    _schema = "step5d.autotune-v4/r005-queue-event-v1"

    def __init__(self, root: Path, *, max_pending: int = 2) -> None:
        if max_pending != 2:
            raise QueueError("r005 asynchronous bound is fixed at two pending rows")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise QueueError("queue root must be a real directory")
        self.path = self.root / "events.jsonl"
        if self.path.is_symlink():
            raise QueueError("queue event log must not be a symlink")
        self._pending: list[QueueEntry] = []
        self._inflight: DispatchTicket | None = None
        self._completed: list[DispatchTicket] = []
        self._cancelled: list[QueueEntry] = []
        self._next_ordinal = 1
        self._next_dispatch_sequence = 1
        self._last_attempt_sequence = 0
        self._execution_ids: dict[str, str] = {}
        self._replay()

    def _write(self, event: Mapping[str, Any]) -> None:
        row = {"schema": self._schema, **dict(event)}
        with self.path.open("ab") as handle:
            handle.write(canonical_bytes(row) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(self.root)

    @staticmethod
    def _entry(payload: Mapping[str, Any]) -> QueueEntry:
        candidate_payload = dict(payload["candidate"])
        candidate_payload.pop("i_off", None)
        return QueueEntry(
            request_uid=str(payload["request_uid"]),
            candidate=Candidate(**candidate_payload),
            kind=str(payload["kind"]),
            epoch=int(payload["epoch"]),
            logical_request_uid=(
                None
                if payload.get("logical_request_uid") is None
                else str(payload["logical_request_uid"])
            ),
        )

    def _replay(self) -> None:
        if not self.path.exists():
            return
        if not self.path.is_file():
            raise QueueError("queue event log is not a regular file")
        for line_number, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                event = json.loads(line)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise QueueError(f"invalid queue event {line_number}") from exc
            if not isinstance(event, dict) or event.get("schema") != self._schema:
                raise QueueError(f"queue event {line_number} schema differs")
            name = event.get("event")
            if name == "ENQUEUE":
                entry = self._entry(event)
                self._pending.append(entry)
                self._next_ordinal = max(self._next_ordinal, int(event.get("ordinal", 0)) + 1)
            elif name == "DISPATCH":
                entry = self._entry(event)
                if self._inflight is not None:
                    raise QueueError("replayed queue has two inflight dispatches")
                if not self._pending or self._pending[0].request_uid != entry.request_uid:
                    raise QueueError("replayed dispatch is not the FIFO pending row")
                self._pending.pop(0)
                sequence = int(event["dispatch_sequence"])
                self._inflight = DispatchTicket(entry, sequence)
                self._next_dispatch_sequence = max(self._next_dispatch_sequence, sequence + 1)
            elif name == "COMPLETE":
                if self._inflight is None or self._inflight.request_uid != event.get("request_uid"):
                    raise QueueError("replayed completion has no matching inflight")
                self._completed.append(self._inflight)
                self._inflight = None
            elif name == "RECONCILE_INFLIGHT_AFTER_HOME":
                if self._inflight is None or self._inflight.request_uid != event.get("request_uid"):
                    raise QueueError("replayed Home reconciliation has no matching inflight")
                self._pending.insert(0, self._inflight.entry)
                self._inflight = None
            elif name == "ATTEMPT_EXECUTION":
                if self._inflight is None or self._inflight.request_uid != event.get("request_uid"):
                    raise QueueError("replayed attempt execution has no matching inflight")
                sequence = int(event.get("attempt_sequence", 0))
                execution_id = str(event.get("execution_id", ""))
                if sequence <= self._last_attempt_sequence or not execution_id:
                    raise QueueError("replayed attempt execution is not monotonic")
                logical_uid = str(event.get("logical_request_uid", ""))
                if logical_uid != self._inflight.entry.logical_uid:
                    raise QueueError("replayed attempt logical identity differs")
                self._last_attempt_sequence = sequence
                self._execution_ids[logical_uid] = execution_id
            elif name == "CANCEL_PENDING":
                uids = tuple(str(uid) for uid in event.get("request_uids", ()))
                if any(entry.request_uid not in uids for entry in self._pending):
                    raise QueueError("replayed cancellation does not cover all pending rows")
                self._cancelled.extend(self._pending)
                self._pending.clear()
            elif name == "HOME_BIND":
                continue
            else:
                raise QueueError(f"unknown queue event {name!r}")

    @property
    def inflight(self) -> DispatchTicket | None:
        return self._inflight

    def pending(self) -> tuple[QueueEntry, ...]:
        return tuple(self._pending)

    def bind_home(
        self, *, campaign_epoch: int, last_trial_id: int = 0, last_command_seq: int = 0
    ) -> None:
        if campaign_epoch <= 0 or last_trial_id != 0 or last_command_seq != 0:
            raise QueueError("offline Home binding must start a positive fresh epoch")
        self._write(
            {
                "event": "HOME_BIND",
                "campaign_epoch": campaign_epoch,
                "last_trial_id": last_trial_id,
                "last_command_seq": last_command_seq,
            }
        )

    @property
    def completed(self) -> tuple[DispatchTicket, ...]:
        return tuple(self._completed)

    @property
    def cancelled(self) -> tuple[QueueEntry, ...]:
        return tuple(self._cancelled)

    @property
    def last_attempt_sequence(self) -> int:
        return self._last_attempt_sequence

    def enqueue(
        self,
        candidate: Candidate,
        *,
        kind: str,
        epoch: int,
        request_uid: str | None = None,
    ) -> QueueEntry:
        if not isinstance(candidate, Candidate) or not isinstance(kind, str) or not kind:
            raise QueueError("queue enqueue requires a typed candidate and kind")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch <= 0:
            raise QueueError("queue epoch must be positive")
        if len(self._pending) >= self.max_pending:
            raise QueueCapacityError("at most two pending rows are allowed")
        repeatable = {"QUALIFICATION", "BOOTSTRAP_PD", "RETEST"}
        if (
            self._inflight is not None
            and self._inflight.candidate_uid == candidate.candidate_uid
            and kind not in repeatable
        ):
            raise QueueError("candidate duplicates the physical inflight row")
        if any(
            entry.candidate_uid == candidate.candidate_uid
            and kind not in repeatable
            and entry.kind not in repeatable
            for entry in self._pending
        ):
            raise QueueError("candidate duplicates a pending row")
        if kind == "BO_TRIAL" and any(
            ticket.candidate_uid == candidate.candidate_uid for ticket in self._completed
        ):
            raise QueueError("BO candidate was already observed")
        ordinal = self._next_ordinal
        request_uid = request_uid or _uid(candidate, kind, epoch, ordinal)
        if any(
            entry.request_uid == request_uid
            for entry in (*self._pending, *self._cancelled)
        ) or (self._inflight is not None and self._inflight.request_uid == request_uid):
            raise QueueError("request UID already exists")
        entry = QueueEntry(request_uid, candidate, kind, epoch)
        self._write({"event": "ENQUEUE", "ordinal": ordinal, **entry.as_dict()})
        self._pending.append(entry)
        self._next_ordinal += 1
        return entry

    def prepare_next(self) -> DispatchTicket | None:
        if self._inflight is not None:
            return self._inflight
        if not self._pending:
            return None
        entry = self._pending.pop(0)
        sequence = self._next_dispatch_sequence
        ticket = DispatchTicket(entry, sequence)
        self._write({"event": "DISPATCH", "dispatch_sequence": sequence, **entry.as_dict()})
        self._inflight = ticket
        self._next_dispatch_sequence += 1
        return ticket

    def reconcile_inflight_after_home(self) -> QueueEntry | None:
        if self._inflight is None:
            return None
        entry = self._inflight.entry
        self._write(
            {
                "event": "RECONCILE_INFLIGHT_AFTER_HOME",
                "request_uid": entry.request_uid,
                "logical_request_uid": entry.logical_uid,
                "dispatch_sequence": self._inflight.dispatch_sequence,
            }
        )
        self._pending.insert(0, entry)
        self._inflight = None
        return entry

    def record_execution(
        self,
        ticket: DispatchTicket,
        *,
        attempt_sequence: int,
        execution_id: str,
    ) -> None:
        if self._inflight != ticket:
            raise QueueError("attempt execution does not match the physical inflight row")
        if (
            isinstance(attempt_sequence, bool)
            or not isinstance(attempt_sequence, int)
            or attempt_sequence <= self._last_attempt_sequence
            or not isinstance(execution_id, str)
            or not execution_id
        ):
            raise QueueError("attempt execution identity is not monotonic")
        self._write(
            {
                "event": "ATTEMPT_EXECUTION",
                "request_uid": ticket.request_uid,
                "logical_request_uid": ticket.entry.logical_uid,
                "attempt_sequence": attempt_sequence,
                "execution_id": execution_id,
            }
        )
        self._last_attempt_sequence = attempt_sequence
        self._execution_ids[ticket.entry.logical_uid] = execution_id

    def complete(
        self, ticket: DispatchTicket, *, status: str, detail: str | None = None
    ) -> None:
        if status not in {"SUCCEEDED", "SAFE_NONTRAINABLE"}:
            raise QueueError("offline queue completion status is invalid")
        if self._inflight != ticket:
            raise QueueError("completion does not match the physical inflight row")
        self._write(
            {
                "event": "COMPLETE",
                "request_uid": ticket.request_uid,
                "dispatch_sequence": ticket.dispatch_sequence,
                "status": status,
                "detail": detail,
            }
        )
        self._completed.append(ticket)
        self._inflight = None

    def cancel_pending(self, *, reason: str) -> tuple[QueueEntry, ...]:
        if not isinstance(reason, str) or not reason or "\n" in reason:
            raise QueueError("pending cancellation reason must be one line")
        cancelled = tuple(self._pending)
        self._write(
            {
                "event": "CANCEL_PENDING",
                "reason": reason,
                "request_uids": [entry.request_uid for entry in cancelled],
            }
        )
        self._cancelled.extend(cancelled)
        self._pending.clear()
        return cancelled


class V3DurableQueueAdapter:
    """V4 adapter over the existing V3 durable request/dispatch receiver.

    The V3 receiver remains the durable source of transport records.  This
    adapter adds the V4 candidate/kind metadata and uses V3's durable
    non-physical policy tombstone for pending cancellation.
    """

    max_pending = 2

    def __init__(
        self,
        root: Path,
        *,
        campaign_id: str,
        launch_profile_path: Path,
        release_manifest_sha256: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.launch_profile_path = Path(launch_profile_path)
        self.campaign_id = campaign_id
        self._v3 = None
        self._metadata_path = self.root / "r005-metadata.jsonl"
        self._cancel_path = self.root / "r005-cancellations.jsonl"
        self._execution_path = self.root / "r005-executions.jsonl"
        self._metadata: dict[str, QueueEntry] = {}
        self._ticket: DispatchTicket | None = None
        self._dispatch: Mapping[str, Any] | None = None
        self._completed: list[DispatchTicket] = []
        self._cancelled: list[QueueEntry] = []
        self._last_attempt_sequence = 0
        self._execution_ids: dict[str, str] = {}
        self._release_manifest_sha256 = release_manifest_sha256
        self.root.mkdir(parents=True, exist_ok=True)
        self._load_metadata()
        self._load_v3()
        self._durable_cancel_tombstones()
        self._load_executions()
        self._recover_crash_resume()
        self._hydrate_inflight()

    def _load_v3(self) -> Any:
        if self._v3 is not None:
            return self._v3
        try:
            import step5d_parameter_queue as v3_queue
        except ImportError as exc:
            raise QueueError("V3 durable queue is unavailable on PYTHONPATH") from exc
        v3_queue.initialize(
            self.root,
            campaign_id=self.campaign_id,
            release_manifest_sha256=self._release_manifest_sha256,
            launch_profile_path=self.launch_profile_path,
        )
        self._v3 = v3_queue
        return v3_queue

    def _durable_cancel_tombstones(self) -> None:
        if not self._cancelled:
            return
        try:
            self._load_v3().cancel_pending_requests(
                self.root,
                request_uids=tuple(entry.request_uid for entry in self._cancelled),
                reason="r005_durable_pending_cancellation",
            )
        except Exception as exc:
            raise QueueError("V3 pending cancellation tombstone could not be verified") from exc

    def _append(self, path: Path, payload: Mapping[str, Any]) -> None:
        with path.open("ab") as handle:
            handle.write(canonical_bytes(dict(payload)) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(self.root)

    def _load_metadata(self) -> None:
        if not self._metadata_path.exists():
            return
        for line in self._metadata_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            candidate_payload = dict(row["candidate"])
            candidate_payload.pop("i_off", None)
            self._metadata[str(row["request_uid"])] = QueueEntry(
                request_uid=str(row["request_uid"]),
                candidate=Candidate(**candidate_payload),
                kind=str(row["kind"]),
                epoch=int(row["epoch"]),
                logical_request_uid=(
                    None
                    if row.get("logical_request_uid") is None
                    else str(row["logical_request_uid"])
                ),
            )
        if self._cancel_path.exists():
            for line in self._cancel_path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                entry = self._metadata.get(str(row["request_uid"]))
                if entry is not None:
                    self._cancelled.append(entry)

    def _load_executions(self) -> None:
        if not self._execution_path.exists():
            return
        for line in self._execution_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            sequence = int(row["attempt_sequence"])
            execution_id = str(row["execution_id"])
            logical_uid = str(row["logical_request_uid"])
            if sequence <= self._last_attempt_sequence or not execution_id or not logical_uid:
                raise QueueError("V3 execution identity is not monotonic")
            self._last_attempt_sequence = sequence
            self._execution_ids[logical_uid] = execution_id

    def _v3_pending(self) -> tuple[dict[str, Any], ...]:
        return self._load_v3().list_pending(self.root)

    @staticmethod
    def _crash_resume_request_matches(
        row: Mapping[str, Any], *, old: QueueEntry, occurrence_nonce: str
    ) -> bool:
        if (
            row.get("occurrence_nonce") != occurrence_nonce
            or row.get("source") != f"r005_{old.kind.lower()}_crash_resume"
            or row.get("position") != "next"
        ):
            return False
        overlay = row.get("overlay")
        if not isinstance(overlay, Mapping):
            return False
        for field in (
            "force_p_gain",
            "force_i_gain",
            "force_damping",
            "normal_filter_tau_s",
            "orientation_ko",
            "motion_kp",
        ):
            try:
                if not math.isclose(
                    float(overlay[field]),
                    float(getattr(old.candidate, field)),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    return False
            except (KeyError, TypeError, ValueError):
                return False
        return str(row.get("request_uid", "")) != old.request_uid

    def _v3_receipt(self, request_uid: str) -> Mapping[str, Any] | None:
        path = self.root / "receipts" / f"{request_uid.rsplit(':', 1)[-1]}.json"
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise QueueError(
                f"V3 crash resume phase=receipt_scan failed: receipt for {request_uid} is not a regular file"
            )
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise QueueError(
                f"V3 crash resume phase=receipt_scan failed: {type(exc).__name__}: {exc}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise QueueError(
                f"V3 crash resume phase=receipt_scan failed: receipt for {request_uid} is not an object"
            )
        return payload

    def _append_recovered_metadata(self, entry: QueueEntry) -> None:
        existing = self._metadata.get(entry.request_uid)
        if existing is not None:
            if existing != entry:
                raise QueueError(
                    "V3 crash resume phase=metadata_repair failed: transport request metadata differs"
                )
            return
        try:
            self._append(self._metadata_path, entry.as_dict())
        except Exception as exc:
            raise QueueError(
                f"V3 crash resume phase=metadata_append failed: {type(exc).__name__}: {exc}"
            ) from exc
        self._metadata[entry.request_uid] = entry

    def _recover_crash_resume(self) -> None:
        try:
            requests = tuple(self._load_v3().list_requests(self.root))
        except Exception as exc:
            raise QueueError(
                f"V3 crash resume phase=request_scan failed: {type(exc).__name__}: {exc}"
            ) from exc
        requests_by_uid = {str(row["request_uid"]): row for row in requests}
        candidates: list[tuple[int, QueueEntry, Mapping[str, Any], Mapping[str, Any]]] = []
        for old in self._metadata.values():
            request = requests_by_uid.get(old.request_uid)
            if request is None:
                continue
            receipt = self._v3_receipt(old.request_uid)
            if receipt is None:
                continue
            if (
                receipt.get("request_uid") != old.request_uid
                or receipt.get("status") != "FAILED"
                or receipt.get("detail")
                != "r005_crash_resume_after_explicit_home_reconciliation"
            ):
                continue
            try:
                dispatch_sequence = int(receipt["dispatch_sequence"])
            except (KeyError, TypeError, ValueError) as exc:
                raise QueueError(
                    f"V3 crash resume phase=receipt_scan failed: invalid dispatch sequence for {old.request_uid}: {exc}"
                ) from exc
            if dispatch_sequence <= 0:
                raise QueueError(
                    f"V3 crash resume phase=receipt_scan failed: non-positive dispatch sequence for {old.request_uid}"
                )
            candidates.append((dispatch_sequence, old, request, receipt))

        latest_by_logical_uid: dict[str, tuple[int, QueueEntry, Mapping[str, Any], Mapping[str, Any]]] = {}
        for candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
            latest_by_logical_uid.setdefault(candidate[1].logical_uid, candidate)

        for dispatch_sequence, old, old_request, _receipt in latest_by_logical_uid.values():
            occurrence_nonce = _crash_resume_occurrence_nonce(
                old.logical_uid, dispatch_sequence
            )
            newer_metadata = []
            old_enqueue_sequence = int(old_request["enqueue_sequence"])
            for entry in self._metadata.values():
                if (
                    entry.request_uid == old.request_uid
                    or entry.logical_uid != old.logical_uid
                    or entry.candidate_uid != old.candidate_uid
                    or entry.kind != old.kind
                    or entry.epoch != old.epoch
                ):
                    continue
                request = requests_by_uid.get(entry.request_uid)
                if request is None:
                    raise QueueError(
                        f"V3 crash resume phase=metadata_check failed: newer metadata row {entry.request_uid} has no transport request"
                    )
                if int(request["enqueue_sequence"]) > old_enqueue_sequence:
                    newer_metadata.append(entry)
            if newer_metadata:
                continue

            matches = [
                row
                for row in requests
                if self._crash_resume_request_matches(
                    row, old=old, occurrence_nonce=occurrence_nonce
                )
            ]
            if len(matches) > 1:
                raise QueueError(
                    f"V3 crash resume phase=transport_check failed: occurrence nonce {occurrence_nonce} has duplicate transport requests"
                )
            if matches:
                resumed = matches[0]
                resumed_uid = str(resumed["request_uid"])
                existing = self._metadata.get(resumed_uid)
                resumed_entry = QueueEntry(
                    resumed_uid,
                    old.candidate,
                    old.kind,
                    old.epoch,
                    logical_request_uid=old.logical_uid,
                )
                if existing is not None and existing != resumed_entry:
                    raise QueueError(
                        f"V3 crash resume phase=metadata_repair failed: {resumed_uid} is bound to different r005 metadata"
                    )
                if existing is None:
                    self._append_recovered_metadata(resumed_entry)
                continue

            try:
                pending_count = len(self._v3_pending())
            except Exception as exc:
                raise QueueError(
                    f"V3 crash resume phase=capacity_check failed: {type(exc).__name__}: {exc}"
                ) from exc
            if pending_count >= self.max_pending:
                raise QueueCapacityError(
                    f"V3 crash resume phase=requeue_capacity failed: {pending_count} pending rows already occupy the max {self.max_pending}"
                )
            try:
                resumed = self._load_v3().submit_manifest(
                    self.root,
                    launch_profile_path=self.launch_profile_path,
                    rows=(
                        {
                            **old.candidate.canonical,
                            "source": f"r005_{old.kind.lower()}_crash_resume",
                            "position": "next",
                            "occurrence_nonce": occurrence_nonce,
                        },
                    ),
                )[0]
            except Exception as exc:
                raise QueueError(
                    f"V3 crash resume phase=requeue_submit failed: {type(exc).__name__}: {exc}"
                ) from exc
            if not self._crash_resume_request_matches(
                resumed, old=old, occurrence_nonce=occurrence_nonce
            ):
                raise QueueError(
                    "V3 crash resume phase=requeue_submit failed: returned transport identity differs"
                )
            resumed_entry = QueueEntry(
                str(resumed["request_uid"]),
                old.candidate,
                old.kind,
                old.epoch,
                logical_request_uid=old.logical_uid,
            )
            self._append_recovered_metadata(resumed_entry)

    def _hydrate_inflight(self) -> None:
        state = self._load_v3().load_state(self.root)
        if state.get("inflight") is None:
            return
        dispatch = self._load_v3().prepare_next_dispatch(self.root)
        if dispatch is None:
            raise QueueError("V3 state claims inflight but no dispatch record exists")
        uid = str(dispatch["request"]["request_uid"])
        entry = self._metadata.get(uid)
        if entry is None:
            raise QueueError("V3 inflight dispatch has no r005 metadata")
        self._ticket = DispatchTicket(entry, int(dispatch["dispatch_sequence"]))
        self._dispatch = dispatch

    @property
    def inflight(self) -> DispatchTicket | None:
        return self._ticket

    @property
    def last_attempt_sequence(self) -> int:
        return self._last_attempt_sequence

    def bind_home(
        self, *, campaign_epoch: int, last_trial_id: int = 0, last_command_seq: int = 0
    ) -> None:
        try:
            self._load_v3().bind_home(
                self.root,
                campaign_epoch=campaign_epoch,
                last_trial_id=last_trial_id,
                last_command_seq=last_command_seq,
            )
        except Exception as exc:
            raise QueueError("V3 Home binding failed") from exc

    def pending(self) -> tuple[QueueEntry, ...]:
        rows: list[QueueEntry] = []
        cancelled = {entry.request_uid for entry in self._cancelled}
        for row in self._v3_pending():
            entry = self._metadata.get(str(row["request_uid"]))
            if entry is not None and entry.request_uid not in cancelled:
                rows.append(entry)
        return tuple(rows)

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
        repeatable = {"QUALIFICATION", "BOOTSTRAP_PD", "RETEST"}
        if (
            self._ticket is not None
            and self._ticket.candidate_uid == candidate.candidate_uid
            and kind not in repeatable
        ):
            raise QueueError("candidate duplicates the physical inflight row")
        if any(
            entry.candidate_uid == candidate.candidate_uid
            and kind not in repeatable
            and entry.kind not in repeatable
            for entry in self.pending()
        ):
            raise QueueError("candidate duplicates a pending row")
        ordinal = len(self._metadata) + 1
        request_uid = request_uid or _uid(candidate, kind, epoch, ordinal)
        entry = QueueEntry(request_uid, candidate, kind, epoch)
        row = self._load_v3().submit_manifest(
            self.root,
            launch_profile_path=self.launch_profile_path,
            rows=(
                {
                    **candidate.canonical,
                    "source": f"r005_{kind.lower()}",
                    "position": "tail",
                    "occurrence_nonce": hashlib.sha256(request_uid.encode()).hexdigest()[:32],
                },
            ),
        )[0]
        actual_uid = str(row["request_uid"])
        # The V3 request UID is transport-owned.  Keep it as the stable adapter
        # key; the requested r005 UID is retained in metadata for evidence.
        entry = QueueEntry(actual_uid, candidate, kind, epoch)
        if actual_uid in self._metadata and self._metadata[actual_uid] != entry:
            raise QueueError("V3 request UID changed its r005 metadata")
        self._metadata[actual_uid] = entry
        self._append(self._metadata_path, entry.as_dict())
        return entry

    def prepare_next(self) -> DispatchTicket | None:
        if self._ticket is not None:
            return self._ticket
        cancelled_uids = {entry.request_uid for entry in self._cancelled}
        while True:
            dispatch = self._load_v3().prepare_next_dispatch(self.root)
            if dispatch is None:
                return None
            uid = str(dispatch["request"]["request_uid"])
            entry = self._metadata.get(uid)
            if entry is None:
                raise QueueError("V3 dispatch has no r005 metadata binding")
            if uid in cancelled_uids:
                raise QueueError(
                    "V3 cancellation tombstone was not applied before physical dispatch"
                )
            self._ticket = DispatchTicket(entry, int(dispatch["dispatch_sequence"]))
            self._dispatch = dispatch
            return self._ticket

    def complete(
        self, ticket: DispatchTicket, *, status: str, detail: str | None = None
    ) -> None:
        if self._ticket != ticket:
            raise QueueError("completion does not match V3 inflight dispatch")
        if self._dispatch is None:
            raise QueueError("V3 dispatch evidence is missing")
        packet = self._dispatch["packet"]
        observed = {
            "campaign_epoch": packet["campaign_epoch"],
            "trial_id": packet["trial_id"],
            "state": 78,
            "candidate_token": packet["candidate_token"],
            "execution_profile_id": packet["execution_profile_id"],
            "consumed_command_seq": packet["command_seq"],
            "logical_batch_sequence": packet["logical_batch_sequence"],
            "batch_row_index": 1,
            "terminal_reason": 0,
        }
        self._load_v3().finish_dispatch(
            self.root,
            status="SUCCEEDED" if status == "SUCCEEDED" else "FAILED",
            observed=observed,
            detail=detail,
        )
        self._completed.append(ticket)
        self._ticket = None
        self._dispatch = None

    def reconcile_inflight_after_home(self) -> QueueEntry | None:
        """Close an interrupted V3 dispatch after the host has reached Home.

        V3 records the prior request as an identity-reconciled failure, then
        submits a fresh transport row retaining the same r005 logical UID.
        The next ARM therefore receives a new execution ID and dispatch
        sequence without pretending the old receipt was an objective.
        """

        if self._ticket is None or self._dispatch is None:
            return None
        old = self._ticket
        packet = self._dispatch["packet"]
        try:
            self._load_v3().finish_dispatch(
                self.root,
                status="FAILED",
                failure_class="IDENTITY",
                observed={
                    "campaign_epoch": packet["campaign_epoch"],
                    "trial_id": packet["trial_id"],
                    "state": 78,
                    "safety_mode": 1,
                    "candidate_token": packet["candidate_token"],
                    "execution_profile_id": packet["execution_profile_id"],
                    "consumed_command_seq": packet["command_seq"],
                    "logical_batch_sequence": packet["logical_batch_sequence"],
                    "batch_row_index": 1,
                },
                detail="r005_crash_resume_after_explicit_home_reconciliation",
            )
        except Exception as exc:
            raise QueueError(
                f"V3 crash resume phase=receipt failed: {type(exc).__name__}: {exc}"
            ) from exc
        occurrence_nonce = _crash_resume_occurrence_nonce(
            old.entry.logical_uid, old.dispatch_sequence
        )
        try:
            resumed = self._load_v3().submit_manifest(
                self.root,
                launch_profile_path=self.launch_profile_path,
                rows=(
                    {
                        **old.candidate.canonical,
                        "source": f"r005_{old.kind.lower()}_crash_resume",
                        "position": "next",
                        "occurrence_nonce": occurrence_nonce,
                    },
                ),
            )[0]
        except Exception as exc:
            raise QueueError(
                f"V3 crash resume phase=requeue_submit failed: {type(exc).__name__}: {exc}"
            ) from exc
        resumed_entry = QueueEntry(
            str(resumed["request_uid"]),
            old.candidate,
            old.kind,
            old.epoch,
            logical_request_uid=old.entry.logical_uid,
        )
        try:
            self._append(self._metadata_path, resumed_entry.as_dict())
        except Exception as exc:
            raise QueueError(
                f"V3 crash resume phase=metadata_append failed: {type(exc).__name__}: {exc}"
            ) from exc
        self._metadata[resumed_entry.request_uid] = resumed_entry
        self._ticket = None
        self._dispatch = None
        return resumed_entry

    def record_execution(
        self,
        ticket: DispatchTicket,
        *,
        attempt_sequence: int,
        execution_id: str,
    ) -> None:
        if self._ticket != ticket:
            raise QueueError("attempt execution does not match V3 inflight dispatch")
        if (
            isinstance(attempt_sequence, bool)
            or not isinstance(attempt_sequence, int)
            or attempt_sequence <= self._last_attempt_sequence
            or not isinstance(execution_id, str)
            or not execution_id
        ):
            raise QueueError("V3 attempt execution identity is not monotonic")
        self._append(
            self._execution_path,
            {
                "request_uid": ticket.request_uid,
                "logical_request_uid": ticket.entry.logical_uid,
                "attempt_sequence": attempt_sequence,
                "execution_id": execution_id,
            },
        )
        self._last_attempt_sequence = attempt_sequence
        self._execution_ids[ticket.entry.logical_uid] = execution_id

    def cancel_pending(self, *, reason: str) -> tuple[QueueEntry, ...]:
        if not isinstance(reason, str) or not reason or "\n" in reason:
            raise QueueError("pending cancellation reason must be one line")
        entries = self.pending()
        try:
            self._load_v3().cancel_pending_requests(
                self.root,
                request_uids=tuple(entry.request_uid for entry in entries),
                reason=reason,
            )
        except Exception as exc:
            raise QueueError("V3 durable pending cancellation failed closed") from exc
        for entry in entries:
            self._append(
                self._cancel_path,
                {"request_uid": entry.request_uid, "reason": reason},
            )
            self._cancelled.append(entry)
        return entries


__all__ = [
    "DispatchTicket",
    "OfflineDurableQueue",
    "QueueCapacityError",
    "QueueEntry",
    "QueueError",
    "QueuePort",
    "V3DurableQueueAdapter",
]
