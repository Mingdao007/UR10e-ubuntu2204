"""Offline campaign session metadata and attempt timing receipts.

The session is a small durable coordination layer around the existing single
attempt owner.  It does not open an endpoint, prepare a controller package, or
make a physical qualification claim.  A caller may prepare the metadata once,
reuse it for several attempts, and explicitly stop/resume the campaign while
keeping the attempt timing ledger append-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Callable, Mapping
import uuid


SESSION_SCHEMA = "tase.campaign-session-v1"
TIMING_SCHEMA = "tase.attempt-timing-receipt-v1"
SESSION_FILENAME = "campaign-session.json"
TIMING_LEDGER_FILENAME = "attempt-timing-receipts.jsonl"
TIMING_DIRECTORY = "attempt-timing"


class CampaignSessionError(RuntimeError):
    """A session transition or persisted receipt is invalid."""


class SessionState(str, Enum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    COMPLETE = "COMPLETE"


def _finite(value: Any, *, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise CampaignSessionError(f"{field} must be finite and non-negative")
    return number


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return text or "attempt"


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace one session-owned JSON file only after it is fully written."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(dict(payload), stream, indent=2, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _append_jsonl(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(payload), sort_keys=True, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@dataclass(frozen=True)
class AttemptTimingReceipt:
    """Wall and monotonic timing for one logical campaign attempt."""

    session_id: str
    attempt_id: str
    ordinal: int
    status: str
    started_at_s: float
    finished_at_s: float
    started_monotonic_s: float
    finished_monotonic_s: float
    elapsed_s: float
    returncode: int | None = None
    stop_requested: bool = False
    reason: str | None = None
    resumed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": TIMING_SCHEMA,
            "session_id": self.session_id,
            "attempt_id": self.attempt_id,
            "ordinal": self.ordinal,
            "status": self.status,
            "started_at_s": self.started_at_s,
            "finished_at_s": self.finished_at_s,
            "started_monotonic_s": self.started_monotonic_s,
            "finished_monotonic_s": self.finished_monotonic_s,
            "elapsed_s": self.elapsed_s,
            "returncode": self.returncode,
            "stop_requested": self.stop_requested,
            "reason": self.reason,
            "resumed": self.resumed,
        }


class CampaignSession:
    """Durable offline state for a reusable campaign directory.

    ``prepare_once`` creates the identity record and returns the same session
    on subsequent calls when the immutable campaign fields match.  The caller
    owns the actual attempt process; this class only records its lifecycle and
    timing.  A stopped session must be explicitly resumed before another
    attempt can start.
    """

    def __init__(
        self,
        campaign_dir: Path,
        metadata: Mapping[str, Any],
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        preparation_reused: bool = False,
    ) -> None:
        self.campaign_dir = Path(campaign_dir).expanduser().resolve()
        self._metadata = dict(metadata)
        self._monotonic_clock = monotonic_clock
        self._wall_clock = wall_clock
        self.preparation_reused = bool(preparation_reused)

    @property
    def path(self) -> Path:
        return self.campaign_dir / SESSION_FILENAME

    @property
    def timing_ledger_path(self) -> Path:
        return self.campaign_dir / TIMING_LEDGER_FILENAME

    @property
    def state(self) -> SessionState:
        try:
            return SessionState(str(self._metadata["state"]))
        except (KeyError, ValueError) as exc:
            raise CampaignSessionError("campaign session state is invalid") from exc

    @property
    def session_id(self) -> str:
        value = self._metadata.get("session_id")
        if not isinstance(value, str) or not value:
            raise CampaignSessionError("campaign session id is missing")
        return value

    @property
    def active_attempt(self) -> dict[str, Any] | None:
        value = self._metadata.get("active_attempt")
        return None if value is None else dict(value)

    @property
    def metadata(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._metadata, allow_nan=False))

    @classmethod
    def prepare_once(
        cls,
        campaign_dir: Path,
        *,
        protocol_id: str,
        duration_token: str,
        method: str,
        config_path: Path | None = None,
        session_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> "CampaignSession":
        root = Path(campaign_dir).expanduser().resolve()
        path = root / SESSION_FILENAME
        immutable = {
            "protocol_id": str(protocol_id),
            "duration_token": str(duration_token),
            "method": str(method),
            "config_path": None if config_path is None else str(Path(config_path).expanduser().resolve()),
        }
        if path.is_file():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise CampaignSessionError(f"campaign session is unreadable: {path}") from exc
            cls._validate_document(stored)
            for key, expected in immutable.items():
                if stored.get(key) != expected:
                    raise CampaignSessionError(
                        f"campaign session {key} differs from requested campaign"
                    )
            return cls(
                root,
                stored,
                monotonic_clock=monotonic_clock,
                wall_clock=wall_clock,
                preparation_reused=True,
            )

        root.mkdir(parents=True, exist_ok=True)
        now = _finite(wall_clock(), field="prepared_at_s")
        document: dict[str, Any] = {
            "schema": SESSION_SCHEMA,
            "version": 1,
            "session_id": str(session_id or f"tase-{uuid.uuid4().hex}"),
            "state": SessionState.PREPARED.value,
            **immutable,
            "prepared_once": True,
            "prepare_count": 1,
            "resume_count": 0,
            "attempt_count": 0,
            "completed_attempt_count": 0,
            "prepared_at_s": now,
            "updated_at_s": now,
            "stop_reason": None,
            "active_attempt": None,
            "last_attempt": None,
            "prepare_metadata": dict(metadata or {}),
        }
        cls._validate_document(document)
        _write_json(path, document)
        _append_jsonl(
            root / "session-events.jsonl",
            {
                "schema": "tase.campaign-session-event-v1",
                "session_id": document["session_id"],
                "event": "prepared",
                "state": SessionState.PREPARED.value,
                "at_s": now,
            },
        )
        return cls(
            root,
            document,
            monotonic_clock=monotonic_clock,
            wall_clock=wall_clock,
            preparation_reused=False,
        )

    @classmethod
    def load(
        cls,
        campaign_dir: Path,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> "CampaignSession":
        root = Path(campaign_dir).expanduser().resolve()
        path = root / SESSION_FILENAME
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CampaignSessionError(f"campaign session is unreadable: {path}") from exc
        cls._validate_document(document)
        return cls(
            root,
            document,
            monotonic_clock=monotonic_clock,
            wall_clock=wall_clock,
            preparation_reused=True,
        )

    @staticmethod
    def _validate_document(document: Mapping[str, Any]) -> None:
        if document.get("schema") != SESSION_SCHEMA or document.get("version") != 1:
            raise CampaignSessionError("campaign session schema differs")
        if not isinstance(document.get("session_id"), str) or not document["session_id"]:
            raise CampaignSessionError("campaign session id is invalid")
        try:
            SessionState(str(document["state"]))
        except (KeyError, ValueError) as exc:
            raise CampaignSessionError("campaign session state is invalid") from exc
        if document.get("prepared_once") is not True:
            raise CampaignSessionError("campaign session was not prepared once")
        if not isinstance(document.get("active_attempt"), (dict, type(None))):
            raise CampaignSessionError("campaign active attempt is invalid")

    def _persist(self, *, event: str | None = None, at_s: float | None = None) -> None:
        now = _finite(self._wall_clock() if at_s is None else at_s, field="updated_at_s")
        self._metadata["updated_at_s"] = now
        _write_json(self.path, self._metadata)
        if event is not None:
            _append_jsonl(
                self.campaign_dir / "session-events.jsonl",
                {
                    "schema": "tase.campaign-session-event-v1",
                    "session_id": self.session_id,
                    "event": str(event),
                    "state": self._metadata["state"],
                    "at_s": now,
                },
            )

    def summary(self) -> dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA,
            "session_id": self.session_id,
            "state": self.state.value,
            "prepared_once": True,
            "preparation_reused": self.preparation_reused,
            "prepare_count": int(self._metadata["prepare_count"]),
            "resume_count": int(self._metadata["resume_count"]),
            "attempt_count": int(self._metadata["attempt_count"]),
            "active_attempt": self.active_attempt,
            "last_attempt": self._metadata.get("last_attempt"),
            "stop_reason": self._metadata.get("stop_reason"),
            "timing_ledger": str(self.timing_ledger_path),
        }

    def start_attempt(
        self,
        attempt_id: str,
        ordinal: int,
        *,
        run_dir: Path | None = None,
        metadata: Mapping[str, Any] | None = None,
        resumed: bool = False,
    ) -> dict[str, Any]:
        if self.state is not SessionState.PREPARED:
            raise CampaignSessionError(
                f"attempt cannot start while session is {self.state.value}"
            )
        if not str(attempt_id).strip():
            raise CampaignSessionError("attempt id is required")
        if type(ordinal) is not int or ordinal < 0:
            raise CampaignSessionError("attempt ordinal must be a non-negative int")
        started_at = _finite(self._wall_clock(), field="started_at_s")
        started_mono = _finite(self._monotonic_clock(), field="started_monotonic_s")
        active = {
            "attempt_id": str(attempt_id),
            "ordinal": ordinal,
            "run_dir": None if run_dir is None else str(Path(run_dir).expanduser().resolve()),
            "metadata": dict(metadata or {}),
            "started_at_s": started_at,
            "started_monotonic_s": started_mono,
            "resumed": bool(resumed),
        }
        self._metadata["state"] = SessionState.RUNNING.value
        self._metadata["active_attempt"] = active
        self._metadata["stop_reason"] = None
        self._metadata["attempt_count"] = int(self._metadata["attempt_count"]) + 1
        self._persist(event="attempt_started", at_s=started_at)
        return dict(active)

    def finish_attempt(
        self,
        *,
        status: str,
        returncode: int | None = None,
        reason: str | None = None,
        finished_at_s: float | None = None,
        finished_monotonic_s: float | None = None,
    ) -> dict[str, Any]:
        if self.state not in {SessionState.RUNNING, SessionState.STOPPING}:
            raise CampaignSessionError(
                f"attempt cannot finish while session is {self.state.value}"
            )
        if status not in {"complete", "failed", "stopped", "interrupted"}:
            raise CampaignSessionError(f"unknown attempt status: {status}")
        active = self.active_attempt
        if active is None:
            raise CampaignSessionError("active attempt is missing")
        finished_at = _finite(
            self._wall_clock() if finished_at_s is None else finished_at_s,
            field="finished_at_s",
        )
        finished_mono = _finite(
            self._monotonic_clock() if finished_monotonic_s is None else finished_monotonic_s,
            field="finished_monotonic_s",
        )
        elapsed = finished_mono - float(active["started_monotonic_s"])
        if elapsed < 0.0:
            raise CampaignSessionError("attempt monotonic clock moved backwards")
        receipt = AttemptTimingReceipt(
            session_id=self.session_id,
            attempt_id=str(active["attempt_id"]),
            ordinal=int(active["ordinal"]),
            status=status,
            started_at_s=float(active["started_at_s"]),
            finished_at_s=finished_at,
            started_monotonic_s=float(active["started_monotonic_s"]),
            finished_monotonic_s=finished_mono,
            elapsed_s=elapsed,
            returncode=None if returncode is None else int(returncode),
            stop_requested=self.state is SessionState.STOPPING
            or status in {"stopped", "interrupted"},
            reason=None if reason is None else str(reason),
            resumed=bool(active.get("resumed", False)),
        )
        payload = receipt.as_dict()
        ordinal = int(active["ordinal"])
        attempt_name = f"{ordinal:04d}-{_safe_name(str(active['attempt_id']))}"
        receipt_path = self.campaign_dir / TIMING_DIRECTORY / f"{attempt_name}.json"
        _write_json(receipt_path, payload)
        _append_jsonl(self.timing_ledger_path, payload)
        payload["receipt_path"] = str(receipt_path)
        self._metadata["last_attempt"] = payload
        self._metadata["active_attempt"] = None
        self._metadata["completed_attempt_count"] = int(
            self._metadata["completed_attempt_count"]
        ) + (1 if status == "complete" else 0)
        self._metadata["state"] = (
            SessionState.STOPPED.value
            if self.state is SessionState.STOPPING or status in {"stopped", "interrupted"}
            else SessionState.PREPARED.value
        )
        self._persist(event="attempt_finished", at_s=finished_at)
        return payload

    def stop(self, reason: str = "operator_requested") -> dict[str, Any]:
        if self.state is SessionState.COMPLETE:
            raise CampaignSessionError("completed campaign session cannot be stopped")
        if self.state is SessionState.STOPPED:
            return self.summary()
        self._metadata["stop_reason"] = str(reason)
        self._metadata["state"] = (
            SessionState.STOPPING.value
            if self.active_attempt is not None
            else SessionState.STOPPED.value
        )
        self._persist(event="stop_requested")
        return self.summary()

    def resume(self) -> dict[str, Any]:
        if self.state is SessionState.COMPLETE:
            raise CampaignSessionError("completed campaign session cannot resume")
        if self.state in {SessionState.RUNNING, SessionState.STOPPING}:
            if self.active_attempt is None:
                raise CampaignSessionError("running session has no active attempt")
            self.finish_attempt(
                status="interrupted",
                reason="resume_recovered_interrupted_attempt",
            )
        if self.state is SessionState.PREPARED:
            return self.summary()
        if self.state is not SessionState.STOPPED:
            raise CampaignSessionError(f"session cannot resume from {self.state.value}")
        self._metadata["state"] = SessionState.PREPARED.value
        self._metadata["stop_reason"] = None
        self._metadata["resume_count"] = int(self._metadata["resume_count"]) + 1
        self._persist(event="resumed")
        return self.summary()

    def complete(self) -> dict[str, Any]:
        if self.state is SessionState.COMPLETE:
            return self.summary()
        if self.state is not SessionState.PREPARED or self.active_attempt is not None:
            raise CampaignSessionError(
                f"campaign cannot complete while session is {self.state.value}"
            )
        self._metadata["state"] = SessionState.COMPLETE.value
        self._persist(event="completed")
        return self.summary()


__all__ = [
    "AttemptTimingReceipt",
    "CampaignSession",
    "CampaignSessionError",
    "SESSION_FILENAME",
    "SESSION_SCHEMA",
    "SessionState",
    "TIMING_LEDGER_FILENAME",
    "TIMING_SCHEMA",
]
