"""Crash-recoverable rolling campaign queue with no physical replay.

The queue owns dispatch identity.  A request is persisted as ``inflight``
before it is returned to a receiver; after restart an unresolved inflight item
is never put back into pending.  The operator must explicitly reconcile it as
completed or failed, preserving the physical no-replay boundary.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
import tempfile
from typing import Any, Callable, Deque, Mapping, Optional, Sequence


QUEUE_SCHEMA_VERSION = "ur10e_tacdiffusion_queue/v2"


class QueueDecision(str, Enum):
    ITEM = "ITEM"
    WAITING_FOR_EPISODE = "WAITING_FOR_EPISODE"
    DRAINING = "DRAINING"
    COMPLETE = "COMPLETE"


class QueueMode(str, Enum):
    OPEN = "OPEN"
    END = "END"
    DRAIN = "DRAIN"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class EpisodeRequest:
    episode_id: str
    trajectory_family: str
    seed: int
    task_ready_home: str
    dispatch_id: str | None = None

    def __post_init__(self) -> None:
        if not self.episode_id.strip() or not self.trajectory_family.strip() or not self.task_ready_home.strip():
            raise ValueError("episode request identity fields must be non-empty")
        dispatch_id = self.dispatch_id or self.episode_id
        if not dispatch_id.strip():
            raise ValueError("dispatch_id must be non-empty")
        object.__setattr__(self, "dispatch_id", dispatch_id)

    @property
    def request_fingerprint(self) -> str:
        payload = {"episode_id": self.episode_id, "trajectory_family": self.trajectory_family, "seed": self.seed, "task_ready_home": self.task_ready_home, "dispatch_id": self.dispatch_id}
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class QueueRead:
    decision: QueueDecision
    item: Optional[EpisodeRequest] = None


@dataclass(frozen=True)
class TerminalDispatch:
    dispatch_id: str
    episode_id: str
    status: str
    result_identity: str | None
    reason: str | None = None
    request_fingerprint: str = ""


def _request_from_payload(payload: dict[str, object]) -> EpisodeRequest:
    return EpisodeRequest(str(payload["episode_id"]), str(payload["trajectory_family"]), int(payload["seed"]), str(payload["task_ready_home"]), str(payload["dispatch_id"]))


class PersistentRollingQueue:
    """Bounded compact JSON persistence with atomic replace and fsync."""

    def __init__(self, *, state_path: str | Path | None = None, campaign_home: str, max_pending: int = 10000) -> None:
        self._temporary_state_dir = tempfile.TemporaryDirectory(prefix="ur10e-tacdiffusion-queue-") if state_path is None else None
        self.state_path = Path(state_path) if state_path is not None else Path(self._temporary_state_dir.name) / "state.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not campaign_home.strip() or max_pending <= 0:
            raise ValueError("campaign_home/max_pending are invalid")
        self.max_pending = max_pending
        self.campaign_home = campaign_home
        self._priority: Deque[EpisodeRequest] = deque()
        self._items: Deque[EpisodeRequest] = deque()
        self._inflight: EpisodeRequest | None = None
        self._completed: dict[str, TerminalDispatch] = {}
        self._failed: dict[str, TerminalDispatch] = {}
        self._result_identities: dict[str, str] = {}
        self._high_water = 0
        self._mode = QueueMode.OPEN
        if self.state_path.exists():
            self._load()
        else:
            self._persist()

    def _payload(self) -> dict[str, object]:
        request = lambda item: None if item is None else asdict(item)
        terminal = lambda item: asdict(item)
        return {
            "schema": QUEUE_SCHEMA_VERSION,
            "campaign_home": self.campaign_home,
            "max_pending": self.max_pending,
            "mode": self._mode.value,
            "pending": [request(item) for item in self._items],
            "priority": [request(item) for item in self._priority],
            "inflight": request(self._inflight),
            "completed": [terminal(item) for item in self._completed.values()],
            "failed": [terminal(item) for item in self._failed.values()],
            "result_identities": self._result_identities,
            "high_water": self._high_water,
        }

    def _persist(self) -> None:
        payload = json.dumps(self._payload(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        with NamedTemporaryFile("wb", dir=self.state_path.parent, prefix=f".{self.state_path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.state_path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if payload.get("schema") != QUEUE_SCHEMA_VERSION:
            raise ValueError("unsupported queue state schema")
        if payload.get("campaign_home") != self.campaign_home:
            raise ValueError("queue campaign home identity mismatch")
        if int(payload.get("max_pending", self.max_pending)) != self.max_pending:
            raise ValueError("queue capacity mismatch")
        self._mode = QueueMode(str(payload["mode"]))
        self._items = deque(_request_from_payload(item) for item in payload.get("pending", []))
        self._priority = deque(_request_from_payload(item) for item in payload.get("priority", []))
        inflight = payload.get("inflight")
        self._inflight = None if inflight is None else _request_from_payload(inflight)
        self._completed = {item["dispatch_id"]: TerminalDispatch(**item) for item in payload.get("completed", [])}
        self._failed = {item["dispatch_id"]: TerminalDispatch(**item) for item in payload.get("failed", [])}
        self._result_identities = {str(key): str(value) for key, value in payload.get("result_identities", {}).items()}
        self._high_water = int(payload.get("high_water", 0))
        # An inflight dispatch may have been physically consumed before a
        # crash.  Keeping it unresolved is conservative and guarantees no
        # automatic physical replay after restart.
        if self._inflight is not None:
            self._high_water = max(self._high_water, 1)

    def _known(self, request: EpisodeRequest) -> bool:
        return any(request.dispatch_id == item.dispatch_id for item in (*self._items, *self._priority, *(() if self._inflight is None else (self._inflight,)))) or request.dispatch_id in self._completed or request.dispatch_id in self._failed

    def _assert_new_or_same(self, request: EpisodeRequest) -> bool:
        existing = None
        for item in (*self._items, *self._priority, *(() if self._inflight is None else (self._inflight,))):
            if item.dispatch_id == request.dispatch_id:
                existing = item
        terminal = self._completed.get(request.dispatch_id) or self._failed.get(request.dispatch_id)
        if terminal is not None:
            if terminal.episode_id != request.episode_id or (terminal.request_fingerprint and terminal.request_fingerprint != request.request_fingerprint):
                raise ValueError("dispatch identity collision")
            return False
        if existing is not None:
            if existing.request_fingerprint != request.request_fingerprint:
                raise ValueError("dispatch identity collision")
            return False
        return True

    def _append(self, request: EpisodeRequest, *, priority: bool) -> None:
        if self._mode != QueueMode.OPEN:
            raise RuntimeError("cannot append after END or DRAIN")
        if not self._assert_new_or_same(request):
            return
        if self.pending_count >= self.max_pending:
            raise OverflowError("queue pending capacity exceeded")
        (self._priority if priority else self._items).append(request)
        self._high_water += 1
        self._persist()

    def append(self, item: EpisodeRequest) -> None:
        self._append(item, priority=False)

    def priority_insert(self, item: EpisodeRequest) -> None:
        # append, rather than appendleft, preserves FIFO within the priority class.
        self._append(item, priority=True)

    def request_end(self) -> None:
        """END stops intake and explicitly terminalizes un-dispatched pending rows."""
        if self._mode == QueueMode.COMPLETE:
            return
        self._mode = QueueMode.END
        while self._priority or self._items:
            item = (self._priority if self._priority else self._items).popleft()
            self._failed[item.dispatch_id] = TerminalDispatch(item.dispatch_id, item.episode_id, "abandoned_end", None, "explicit END without DRAIN", item.request_fingerprint)
        self._maybe_complete()
        self._persist()

    def request_drain(self) -> None:
        if self._mode == QueueMode.COMPLETE:
            return
        self._mode = QueueMode.DRAIN
        self._maybe_complete()
        self._persist()

    def _maybe_complete(self) -> None:
        if self._mode == QueueMode.END and self._inflight is None:
            self._mode = QueueMode.COMPLETE
        elif self._mode == QueueMode.DRAIN and self._inflight is None and not self._priority and not self._items:
            self._mode = QueueMode.COMPLETE

    def next(self) -> QueueRead:
        if self._inflight is not None:
            return QueueRead(QueueDecision.WAITING_FOR_EPISODE)
        if self._priority or self._items:
            self._inflight = (self._priority if self._priority else self._items).popleft()
            self._persist()
            return QueueRead(QueueDecision.ITEM, self._inflight)
        self._maybe_complete()
        self._persist()
        if self._mode == QueueMode.COMPLETE:
            return QueueRead(QueueDecision.COMPLETE)
        if self._mode == QueueMode.DRAIN:
            return QueueRead(QueueDecision.DRAINING)
        return QueueRead(QueueDecision.WAITING_FOR_EPISODE)

    def _terminal(self, *, status: str, result_identity: str | None, reason: str | None) -> TerminalDispatch:
        if self._inflight is None:
            raise RuntimeError("no inflight dispatch")
        item = self._inflight
        if result_identity is not None:
            prior = self._result_identities.get(result_identity)
            if prior is not None and prior != item.dispatch_id:
                raise ValueError("result identity collision")
            self._result_identities[result_identity] = item.dispatch_id
        terminal = TerminalDispatch(item.dispatch_id, item.episode_id, status, result_identity, reason, item.request_fingerprint)
        if status == "completed":
            self._completed[item.dispatch_id] = terminal
        else:
            self._failed[item.dispatch_id] = terminal
        self._inflight = None
        self._maybe_complete()
        self._persist()
        return terminal

    def complete(self, *, result_identity: str) -> TerminalDispatch:
        return self._terminal(status="completed", result_identity=result_identity, reason=None)

    def fail(self, *, reason: str, result_identity: str | None = None) -> TerminalDispatch:
        return self._terminal(status="failed", result_identity=result_identity, reason=reason)

    def reconcile_recovered_inflight(self, *, outcome: str, result_identity: str | None = None, reason: str | None = None) -> TerminalDispatch:
        if outcome not in {"completed", "failed"}:
            raise ValueError("recovered inflight outcome must be completed or failed")
        return self._terminal(status=outcome, result_identity=result_identity, reason=reason or "crash_recovery_reconciled")

    @property
    def pending_count(self) -> int:
        return len(self._items) + len(self._priority)

    @property
    def inflight(self) -> EpisodeRequest | None:
        return self._inflight

    @property
    def mode(self) -> QueueMode:
        return self._mode

    @property
    def high_water(self) -> int:
        return self._high_water

    @property
    def completed(self) -> tuple[TerminalDispatch, ...]:
        return tuple(self._completed.values())

    @property
    def failed(self) -> tuple[TerminalDispatch, ...]:
        return tuple(self._failed.values())


@dataclass(frozen=True)
class EpisodeFailureRecord:
    episode_id: str
    reason: str
    evidence_id: str


class CampaignState(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_HARDWARE = "WAITING_HARDWARE"
    COMPLETE = "COMPLETE"


class CampaignLifecycle:
    def __init__(self, queue: PersistentRollingQueue) -> None:
        self.queue = queue
        self.state = CampaignState.READY
        self.failures: list[EpisodeFailureRecord] = []
        self.current: EpisodeRequest | None = None
        self.captured_campaign_home: str | None = None

    def acquire_next(self) -> QueueRead:
        if self.state in {CampaignState.WAITING_HARDWARE, CampaignState.COMPLETE}:
            return QueueRead(QueueDecision.WAITING_FOR_EPISODE)
        read = self.queue.next()
        if read.item is not None:
            self.current = read.item
            self.state = CampaignState.RUNNING
        elif read.decision == QueueDecision.COMPLETE:
            self.state = CampaignState.COMPLETE
            self.captured_campaign_home = self.queue.campaign_home
        return read

    def episode_failed(self, *, reason: str, evidence_id: str) -> None:
        if self.current is None or self.state != CampaignState.RUNNING:
            raise RuntimeError("episode failure requires a running episode")
        self.queue.fail(reason=reason, result_identity=evidence_id)
        self.failures.append(EpisodeFailureRecord(self.current.episode_id, reason, evidence_id))
        self.current = None
        self.state = CampaignState.READY

    def episode_completed(self, *, result_identity: str) -> None:
        if self.current is None or self.state != CampaignState.RUNNING:
            raise RuntimeError("episode completion requires a running episode")
        self.queue.complete(result_identity=result_identity)
        self.current = None
        self.state = CampaignState.READY

    def protective_stop(self) -> None:
        if self.state != CampaignState.RUNNING:
            raise RuntimeError("protective stop requires RUNNING")
        # Queue inflight identity remains persisted.  Recovery must explicitly
        # reconcile it; no implicit retry can physically replay the episode.
        self.state = CampaignState.WAITING_HARDWARE

    def hardware_recovered(self, *, normal: bool, verified_safe_home: bool, reconcile: str, result_identity: str) -> None:
        if self.state != CampaignState.WAITING_HARDWARE:
            raise RuntimeError("hardware recovery requires WAITING_HARDWARE")
        if not normal or not verified_safe_home:
            raise ValueError("hardware recovery requires NORMAL and verified safe Home")
        if reconcile not in {"completed", "failed"}:
            raise ValueError("reconcile must be completed or failed")
        if self.queue.inflight is None:
            raise RuntimeError("interrupted episode identity already reconciled")
        if reconcile == "completed":
            self.queue.reconcile_recovered_inflight(outcome="completed", result_identity=result_identity, reason="protective_stop_reconciled")
        else:
            self.queue.reconcile_recovered_inflight(outcome="failed", result_identity=result_identity, reason="protective_stop_reconciled")
        self.current = None
        self.state = CampaignState.READY


@dataclass(frozen=True)
class HomeIdentity:
    identity: str
    acknowledged: bool = False
    consumed: bool = False


class HomeIdentityLedger:
    SCHEMA = "ur10e_tacdiffusion_home_identity/v1"

    def __init__(self, *, state_path: str | Path | None = None) -> None:
        self._temporary_state_dir = tempfile.TemporaryDirectory(prefix="ur10e-tacdiffusion-home-") if state_path is None else None
        self.state_path = Path(state_path) if state_path is not None else Path(self._temporary_state_dir.name) / "home.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._current: HomeIdentity | None = None
        if self.state_path.exists():
            self._load()
        else:
            self._persist()

    def _persist(self) -> None:
        current = None if self._current is None else asdict(self._current)
        payload = json.dumps({"schema": self.SCHEMA, "current": current}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        with NamedTemporaryFile("wb", dir=self.state_path.parent, prefix=f".{self.state_path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.state_path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if payload.get("schema") != self.SCHEMA:
            raise ValueError("unsupported home identity schema")
        current = payload.get("current")
        self._current = None if current is None else HomeIdentity(str(current["identity"]), bool(current["acknowledged"]), bool(current["consumed"]))

    @property
    def current(self) -> HomeIdentity | None:
        return self._current

    def announce(self, identity: str) -> None:
        if not identity.strip():
            raise ValueError("home identity must be non-empty")
        if self._current is not None and not self._current.consumed and self._current.identity != identity:
            raise ValueError("cannot overwrite unconsumed home identity")
        if self._current is not None and self._current.identity == identity:
            return
        self._current = HomeIdentity(identity)
        self._persist()

    def acknowledge(self, identity: str) -> None:
        if self._current is None or self._current.identity != identity:
            raise ValueError("home identity ACK does not match current identity")
        if self._current.acknowledged:
            return
        self._current = HomeIdentity(identity, acknowledged=True, consumed=False)
        self._persist()

    def consume(self, identity: str) -> None:
        if self._current is None or self._current.identity != identity or not self._current.acknowledged:
            raise ValueError("home identity must be ACKed before consume")
        if self._current.consumed:
            return
        self._current = HomeIdentity(identity, acknowledged=True, consumed=True)
        self._persist()

    def accept_terminal_row(self, identity: str) -> None:
        if self._current is None or self._current.identity != identity:
            raise ValueError("stale previous-home row rejected before current identity ACK")
        if not self._current.acknowledged:
            raise ValueError("stale previous-home row rejected before current identity is ACKed")
        if not self._current.consumed:
            raise ValueError("stale previous-home row rejected before current identity consume")


@dataclass(frozen=True)
class CampaignStepResult:
    decision: str
    episode_id: str | None = None
    status: str = ""
    evidence_id: str | None = None
    hook_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class SafeRetractPlan:
    """Explicit no-motion plan: retract along calibrated reaction, then Home."""

    home_identity: str
    task_ready_home: str
    reaction_normal_base: tuple[float, float, float]
    approach_normal_base: tuple[float, float, float]
    retract_distance_m: float

    @property
    def retract_vector_base(self) -> tuple[float, float, float]:
        return tuple(value * self.retract_distance_m for value in self.reaction_normal_base)

    @property
    def retract_phase(self) -> str:
        return "RETRACT_ALONG_CALIBRATED_REACTION_NORMAL"

    @property
    def home_phase(self) -> str:
        return "TASK_READY_HOME"

    def __post_init__(self) -> None:
        if not self.home_identity.strip() or not self.task_ready_home.strip():
            raise ValueError("safe retract identity and task-ready Home must be named")
        reaction = tuple(float(value) for value in self.reaction_normal_base)
        approach = tuple(float(value) for value in self.approach_normal_base)
        if len(reaction) != 3 or len(approach) != 3 or not all(math.isfinite(value) for value in reaction + approach):
            raise ValueError("safe retract normals must be finite 3-vectors")
        if not math.isfinite(self.retract_distance_m) or self.retract_distance_m <= 0.0:
            raise ValueError("safe retract distance must be positive and finite")
        norm = math.sqrt(sum(value * value for value in reaction))
        if abs(norm - 1.0) > 1e-6 or any(abs(approach[index] + reaction[index]) > 1e-6 for index in range(3)):
            raise ValueError("safe retract approach must be negative calibrated reaction normal")
        object.__setattr__(self, "reaction_normal_base", reaction)
        object.__setattr__(self, "approach_normal_base", approach)


class DurableHookOutbox:
    """Atomic durable handoff for optional hooks; enqueue never calls a sender."""

    SCHEMA = "ur10e_tacdiffusion_hook_outbox/v1"

    def __init__(self, *, state_path: str | Path | None = None) -> None:
        self._temporary_state_dir = tempfile.TemporaryDirectory(prefix="ur10e-tacdiffusion-outbox-") if state_path is None else None
        self.state_path = Path(state_path) if state_path is not None else Path(self._temporary_state_dir.name) / "outbox.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._pending: Deque[dict[str, str]] = deque()
        self._claimed: dict[str, dict[str, str]] = {}
        self._completed: set[str] = set()
        self._failed: dict[str, str] = {}
        if self.state_path.exists():
            self._load()
        else:
            self._persist()

    def _persist(self) -> None:
        payload = {"schema": self.SCHEMA, "pending": list(self._pending), "claimed": self._claimed, "completed": sorted(self._completed), "failed": self._failed}
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        with NamedTemporaryFile("wb", dir=self.state_path.parent, prefix=f".{self.state_path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.state_path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if payload.get("schema") != self.SCHEMA:
            raise ValueError("unsupported hook outbox schema")
        self._pending = deque({str(key): str(value) for key, value in row.items()} for row in payload.get("pending", []))
        self._claimed = {str(key): {str(k): str(v) for k, v in row.items()} for key, row in payload.get("claimed", {}).items()}
        self._completed = {str(value) for value in payload.get("completed", [])}
        self._failed = {str(key): str(value) for key, value in payload.get("failed", {}).items()}

    def enqueue(self, *, identity: str, kind: str, episode_id: str, payload: str = "") -> bool:
        if not identity.strip() or not kind.strip() or not episode_id.strip():
            raise ValueError("outbox identity/kind/episode must be non-empty")
        if identity in self._completed or identity in self._failed or identity in self._claimed or any(row["identity"] == identity for row in self._pending):
            return False
        self._pending.append({"identity": identity, "kind": kind, "episode_id": episode_id, "payload": payload})
        self._persist()
        return True

    def claim(self) -> dict[str, str] | None:
        if not self._pending:
            return None
        row = self._pending.popleft()
        self._claimed[row["identity"]] = row
        self._persist()
        return dict(row)

    def complete(self, identity: str) -> None:
        row = self._claimed.pop(identity, None)
        if row is None:
            raise ValueError("outbox completion identity is not claimed")
        self._completed.add(identity)
        self._persist()

    def fail(self, identity: str, reason: str) -> None:
        if identity not in self._claimed:
            raise ValueError("outbox failure identity is not claimed")
        self._claimed.pop(identity)
        self._failed[identity] = str(reason)
        self._persist()

    def process_one(self, consumer: Callable[[Mapping[str, str]], Any]) -> str:
        """Process at most one claimed handoff; caller owns scheduling."""

        row = self.claim()
        if row is None:
            return "EMPTY"
        try:
            consumer(row)
        except Exception as exc:
            self.fail(row["identity"], f"{type(exc).__name__}:{exc}")
            return "FAILED"
        self.complete(row["identity"])
        return "COMPLETED"

    def reconcile_claimed(self, identity: str, *, outcome: str, reason: str = "consumer_crash") -> None:
        """Explicitly reconcile a claimed handoff after consumer restart."""

        row = self._claimed.pop(identity, None)
        if row is None:
            raise ValueError("outbox identity is not claimed")
        if outcome == "retry":
            self._pending.appendleft(row)
        elif outcome == "completed":
            self._completed.add(identity)
        elif outcome == "failed":
            self._failed[identity] = reason
        else:
            raise ValueError("outbox reconciliation outcome must be retry, completed, or failed")
        self._persist()

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._claimed)

    @property
    def failed(self) -> Mapping[str, str]:
        return dict(self._failed)

    @property
    def claimed(self) -> tuple[str, ...]:
        return tuple(self._claimed)


class CampaignRunner:
    """Persistent, long-lived campaign coordinator over queue/lifecycle.

    Episode execution and all optional data/model hooks are isolated.  A hook
    exception becomes a trial-local failure record; it cannot kill the queue
    receiver or turn an empty campaign into a timeout/BLOCKED state.  Home ACK
    and consume are intentionally a second explicit step so a crash between
    retract and acknowledgement cannot be mistaken for a safe next episode.
    """

    def __init__(
        self,
        lifecycle: CampaignLifecycle,
        home_ledger: HomeIdentityLedger,
        *,
        episode_runner: Callable[[EpisodeRequest], Any] | None = None,
        retract_to_home: Callable[[SafeRetractPlan], Any] | None = None,
        retract_plan_provider: Callable[[EpisodeRequest, str], SafeRetractPlan] | None = None,
        hooks: Mapping[str, Callable[[EpisodeRequest], Any]] | None = None,
        outbox: DurableHookOutbox | None = None,
        state_path: str | Path | None = None,
        expert_reset: Callable[[], Any],
        filter_reset: Callable[[], Any],
    ) -> None:
        if retract_plan_provider is None or retract_to_home is None:
            raise ValueError("CampaignRunner requires concrete safe retract plan and executor")
        if not callable(expert_reset) or not callable(filter_reset):
            raise ValueError("CampaignRunner requires explicit expert_reset and filter_reset callbacks")
        if hooks is not None and any(name in hooks for name in ("expert_reset", "filter_reset")):
            raise ValueError("expert/filter reset must be synchronous callbacks, not outbox hooks")
        self.SCHEMA = "ur10e_tacdiffusion_campaign_runner/v1"
        self.lifecycle = lifecycle
        self.home_ledger = home_ledger
        self.episode_runner = episode_runner
        self.retract_to_home = retract_to_home
        self.retract_plan_provider = retract_plan_provider
        self.hooks = dict(hooks or {})
        self.mandatory_resets = (expert_reset, filter_reset)
        self.outbox = outbox or DurableHookOutbox()
        self._temporary_state_dir = tempfile.TemporaryDirectory(prefix="ur10e-tacdiffusion-runner-") if state_path is None else None
        self.state_path = Path(state_path) if state_path is not None else Path(self._temporary_state_dir.name) / "runner.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._pending_home: tuple[EpisodeRequest, str, str, tuple[str, ...], str] | None = None
        self._terminal_intent: str | None = None
        self._recovery_required = False
        if self.state_path.exists():
            self._load()
        elif self.lifecycle.queue.inflight is not None:
            # The receiver may have physically consumed this row.  Never
            # replay it; require explicit external reconciliation instead.
            self._recovery_required = True
            self.lifecycle.current = self.lifecycle.queue.inflight
            self.lifecycle.state = CampaignState.WAITING_HARDWARE
            self._persist()
        else:
            self._persist()

    def _persist(self) -> None:
        pending = None
        if self._pending_home is not None:
            request, status, evidence_id, errors, home_identity = self._pending_home
            pending = {"request": asdict(request), "status": status, "evidence_id": evidence_id, "errors": list(errors), "home_identity": home_identity}
        payload = {"schema": self.SCHEMA, "pending_home": pending, "terminal_intent": self._terminal_intent, "recovery_required": self._recovery_required}
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        with NamedTemporaryFile("wb", dir=self.state_path.parent, prefix=f".{self.state_path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)
        directory_fd = os.open(self.state_path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _load(self) -> None:
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        if payload.get("schema") != self.SCHEMA:
            raise ValueError("unsupported campaign runner schema")
        self._terminal_intent = payload.get("terminal_intent")
        self._recovery_required = bool(payload.get("recovery_required", False))
        pending = payload.get("pending_home")
        if pending is not None:
            request = _request_from_payload(pending["request"])
            if self.lifecycle.queue.inflight is None:
                terminal = next((row for row in (*self.lifecycle.queue.completed, *self.lifecycle.queue.failed) if row.dispatch_id == request.dispatch_id and row.result_identity == pending["evidence_id"]), None)
                if terminal is None:
                    raise ValueError("pending home does not match durable queue inflight identity")
                self._pending_home = None
                self._persist()
                return
            if self.lifecycle.queue.inflight.dispatch_id != request.dispatch_id:
                raise ValueError("pending home does not match durable queue inflight identity")
            self._pending_home = (request, str(pending["status"]), str(pending["evidence_id"]), tuple(str(value) for value in pending.get("errors", [])), str(pending["home_identity"]))
            self.lifecycle.current = request
            self.lifecycle.state = CampaignState.RUNNING
            if self.home_ledger.current is None or self.home_ledger.current.identity != self._pending_home[4]:
                raise ValueError("pending home does not match durable home ledger")
        elif self.lifecycle.queue.inflight is not None:
            self._recovery_required = True
            self.lifecycle.current = self.lifecycle.queue.inflight
            self.lifecycle.state = CampaignState.WAITING_HARDWARE

    @property
    def pending_home_identity(self) -> str | None:
        return None if self._pending_home is None else self._pending_home[4]

    def append(self, request: EpisodeRequest, *, priority: bool = False) -> None:
        (self.lifecycle.queue.priority_insert if priority else self.lifecycle.queue.append)(request)

    def request_end(self) -> None:
        self.lifecycle.queue.request_end()
        self._terminal_intent = "END"
        self._persist()

    def request_drain(self) -> None:
        self.lifecycle.queue.request_drain()
        self._terminal_intent = "DRAIN"
        self._persist()

    def _handoff_hook(self, name: str, request: EpisodeRequest) -> None:
        if name not in self.hooks:
            return
        identity = f"hook:{request.dispatch_id}:{name}"
        self.outbox.enqueue(identity=identity, kind=name, episode_id=request.episode_id, payload=request.request_fingerprint)

    def step(self) -> CampaignStepResult:
        if self._recovery_required:
            return CampaignStepResult("RECOVERY_REQUIRED")
        if self._pending_home is not None:
            return CampaignStepResult("WAITING_HOME", self._pending_home[0].episode_id, self._pending_home[1], self._pending_home[2], self._pending_home[3])
        read = self.lifecycle.acquire_next()
        if read.item is None:
            return CampaignStepResult(read.decision.value)
        request = read.item
        errors: list[str] = []
        home_identity = f"home:{request.dispatch_id}:{request.request_fingerprint[:16]}"
        # Expert/filter reset is mandatory episode-local state, not an
        # optional asynchronous handoff.  It runs synchronously before the
        # receiver episode and is intentionally bounded to one call each.
        reset_failed = False
        for reset in self.mandatory_resets:
            try:
                reset()
            except Exception as exc:
                reset_failed = True
                errors.append(f"mandatory_reset:{type(exc).__name__}")
        if not reset_failed:
            try:
                if self.episode_runner is not None:
                    outcome = self.episode_runner(request)
                    if outcome is False:
                        errors.append("episode_runner:returned_false")
            except Exception as exc:
                errors.append(f"episode_runner:{type(exc).__name__}")
        for name in ("optimizer", "capture", "postprocess", "model"):
            self._handoff_hook(name, request)
        try:
            plan = self.retract_plan_provider(request, home_identity)
            if not isinstance(plan, SafeRetractPlan) or plan.home_identity != home_identity or plan.task_ready_home != request.task_ready_home:
                raise ValueError("safe retract plan identity/Home mismatch")
            if self.retract_to_home(plan) is False:
                raise RuntimeError("safe retract executor did not verify completion")
        except Exception as exc:
            # Do not announce Home until the calibrated reaction-normal
            # retract and named task-ready Home are concretely verified.
            self._recovery_required = True
            self.lifecycle.state = CampaignState.WAITING_HARDWARE
            self._persist()
            return CampaignStepResult("WAITING_SAFE_RETRACT", request.episode_id, "failed", None, tuple(errors + [f"retract_plan:{type(exc).__name__}"]))
        status = "failed" if errors else "completed"
        reason = ";".join(errors) if errors else "ok"
        evidence_id = hashlib.sha256(f"{request.dispatch_id}|{status}|{reason}".encode()).hexdigest()
        self.home_ledger.announce(home_identity)
        self._pending_home = (request, status, evidence_id, tuple(errors), home_identity)
        self._persist()
        return CampaignStepResult("WAITING_HOME", request.episode_id, status, evidence_id, tuple(errors))

    def acknowledge_and_consume_home(self, identity: str) -> CampaignStepResult:
        if self._pending_home is None:
            raise RuntimeError("no pending home identity")
        request, status, evidence_id, errors, pending_identity = self._pending_home
        if identity != pending_identity:
            raise ValueError("home identity does not match pending dispatch")
        self.home_ledger.acknowledge(identity)
        self.home_ledger.consume(identity)
        self.home_ledger.accept_terminal_row(identity)
        if status == "completed":
            self.lifecycle.episode_completed(result_identity=evidence_id)
        else:
            reason = ";".join(errors) if errors else "episode_failed"
            self.lifecycle.episode_failed(reason=reason, evidence_id=evidence_id)
        self._pending_home = None
        self._persist()
        return CampaignStepResult(status, request.episode_id, status, evidence_id, errors)
