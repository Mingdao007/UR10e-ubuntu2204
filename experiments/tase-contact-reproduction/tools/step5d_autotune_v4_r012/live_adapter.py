"""R012-native live adapter seam.

The mature motion executor is supplied by the live owner.  This module owns
only typed candidate conversion, protocol registers, serial dispatch, and the
one-shot lifecycle; it has no legacy queue or identity admission.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Callable, Mapping

from .censor import CensoredObservation, validate_observation
from .common import json_tree
from .descriptor import build_descriptor
from .live_host import PathEarlyEndController, R012HostController, R012HostError, R012ProductionOptimizer
from .path_cbf_live import R012PathGuardStack, install_r012_guard_stack, is_r012_guard_bound
from .serial_queue import QueueItem, SerialOneSlotQueue
from .snapshot import CampaignMode


R012_KIND_QUEUE_MAP: dict[str, tuple[str, str | None]] = {
    "REFERENCE": ("REFERENCE", None),
    "CHALLENGER_CONFIRM": ("CHALLENGER_CONFIRM", None),
    "INCUMBENT_RETEST": ("INCUMBENT_RETEST", None),
    "BO_TRIAL": ("BO_TRIAL", None),
    "NOVEL_BO": ("NOVEL_BO", None),
}
R012_TRIAL_RESULT_SCHEMA = "step5d.autotune-v4/r012-trial-result-v1"


class R012LiveAdapterError(R012HostError):
    """R012 live adapter composition failed."""


@dataclass(frozen=True)
class R012TrialResult:
    """Typed physical completion accepted by the continuous R012 host."""

    dispatch_id: str
    campaign_id: str
    run_id: str
    attempt_id: str
    state: str
    safe_return: bool
    home: bool
    return_guard: bool
    sealed_mae_n: float | None = None
    censored_observation: CensoredObservation | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        item: QueueItem,
        campaign_id: str,
        run_id: str,
        attempt_id: str,
    ) -> "R012TrialResult":
        if not isinstance(value, Mapping):
            raise R012LiveAdapterError("R012 continuous result must be a mapping")
        if value.get("schema") != R012_TRIAL_RESULT_SCHEMA:
            raise R012LiveAdapterError("R012 continuous result schema differs")
        for name, expected in (
            ("dispatch_id", item.dispatch_id),
            ("campaign_id", campaign_id),
            ("run_id", run_id),
            ("attempt_id", attempt_id),
        ):
            if value.get(name) != expected:
                raise R012LiveAdapterError(f"R012 result {name} differs from the current dispatch")
        if value.get("state") != "HOME":
            raise R012LiveAdapterError("R012 continuous result must end at Home")
        for name in ("safe_return", "home", "return_guard"):
            if value.get(name) is not True:
                raise R012LiveAdapterError(f"R012 result {name} is not a safe Home/return proof")

        exact_value = value.get("sealed_mae_n")
        censored_value = value.get("censored_observation")
        has_exact = exact_value is not None
        has_censored = censored_value is not None
        if has_exact == has_censored:
            raise R012LiveAdapterError(
                "R012 continuous result must contain exactly one sealed exact MAE or censored observation"
            )
        exact: float | None = None
        censored: CensoredObservation | None = None
        if has_exact:
            if isinstance(exact_value, bool):
                raise R012LiveAdapterError("R012 sealed exact MAE is not numeric")
            try:
                exact = float(exact_value)
            except (TypeError, ValueError) as exc:
                raise R012LiveAdapterError("R012 sealed exact MAE is not numeric") from exc
            if not math.isfinite(exact) or exact < 0.0:
                raise R012LiveAdapterError("R012 sealed exact MAE is invalid")
        else:
            try:
                candidate = validate_observation(censored_value)
            except Exception as exc:  # noqa: BLE001 - typed-result boundary
                raise R012LiveAdapterError("R012 censored observation is invalid") from exc
            if not isinstance(candidate, CensoredObservation):
                raise R012LiveAdapterError("R012 censored result is not censored")
            if (
                candidate.dispatch_id != item.dispatch_id
                or candidate.campaign_id != campaign_id
                or candidate.run_id != run_id
                or candidate.attempt_id != attempt_id
                or candidate.kind != item.kind
                or json_tree(candidate.candidate) != json_tree(item.candidate)
            ):
                raise R012LiveAdapterError("R012 censored observation does not match the current dispatch")
            censored = candidate
        return cls(
            dispatch_id=item.dispatch_id,
            campaign_id=campaign_id,
            run_id=run_id,
            attempt_id=attempt_id,
            state="HOME",
            safe_return=True,
            home=True,
            return_guard=True,
            sealed_mae_n=exact,
            censored_observation=censored,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": R012_TRIAL_RESULT_SCHEMA,
            "dispatch_id": self.dispatch_id,
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "state": self.state,
            "safe_return": self.safe_return,
            "home": self.home,
            "return_guard": self.return_guard,
            "sealed_mae_n": self.sealed_mae_n,
            "censored_observation": None if self.censored_observation is None else self.censored_observation.as_dict(),
        }


@dataclass
class R012TrialExecutor:
    """Guard-bound owner seam for exactly one mature physical attempt."""

    owner: Any
    runner: Callable[[QueueItem], Mapping[str, Any] | R012TrialResult]
    guard_stack: R012PathGuardStack = field(default_factory=R012PathGuardStack)
    _bound: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.bind()

    def bind(self) -> None:
        bound_stack = install_r012_guard_stack(self.owner, self.guard_stack)
        if bound_stack is not self.guard_stack:
            raise R012LiveAdapterError("R012 owner is bound to a different PATH guard stack")
        if not is_r012_guard_bound(self.owner):
            raise R012LiveAdapterError("R012 owner physical executor is not guard-bound")
        self._bound = True

    def ensure_guard_bound(self) -> None:
        if not self._bound or not is_r012_guard_bound(self.owner):
            raise R012LiveAdapterError("R012 owner physical executor lost its guard binding")

    def __call__(self, item: QueueItem) -> Mapping[str, Any]:
        self.ensure_guard_bound()
        result = self.runner(item)
        if isinstance(result, R012TrialResult):
            return result.as_dict()
        if not isinstance(result, Mapping):
            raise R012LiveAdapterError("R012 owner executor result is not readable")
        return result


@dataclass(frozen=True)
class NativeCandidate:
    canonical: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "canonical", json_tree(self.canonical))

    def __getattr__(self, name: str) -> Any:
        if name in self.canonical:
            return self.canonical[name]
        raise AttributeError(name)


def map_r012_kind_to_queue(r012_kind: str) -> tuple[str, str | None]:
    try:
        return R012_KIND_QUEUE_MAP[str(r012_kind)]
    except KeyError as exc:
        raise R012LiveAdapterError(f"unsupported R012 scheduler kind {r012_kind!r}") from exc


def dict_to_r006_candidate(candidate: Mapping[str, Any]) -> NativeCandidate:
    if not isinstance(candidate, Mapping) or candidate.get("i_off", True) is not True:
        raise R012LiveAdapterError("R012 live candidates require I-off")
    required = ("force_p_gain", "force_damping", "normal_filter_tau_s", "orientation_ko", "motion_kp")
    if any(key not in candidate for key in required):
        raise R012LiveAdapterError("R012 candidate is incomplete")
    return NativeCandidate(candidate)


def dispatch_id_from_record(record: Any) -> str:
    sequence = int(getattr(record, "attempt_sequence", 0) or 0)
    return f"dispatch-{sequence:06d}"


def resolve_register_writer(raw_writer: Any) -> Any:
    target = getattr(raw_writer, "writer", raw_writer)

    class RegisterWriterAdapter:
        def write_input_integer_register(self, index: int, value: int) -> None:
            for name in ("set_input_integer_register", "write_input_integer_register"):
                method = getattr(target, name, None)
                if callable(method):
                    method(int(index), int(value))
                    return
            raise R012LiveAdapterError("live writer has no input integer register API")

        def read_output_integer_register(self, index: int) -> int:
            for name in ("get_output_integer_register", "read_output_integer_register"):
                method = getattr(target, name, None)
                if callable(method):
                    return int(method(int(index)))
            raise R012LiveAdapterError("live writer has no output integer register API")

    return RegisterWriterAdapter()


def apply_r012_timing_canary_skip_qual(host: Any) -> None:
    host.events.append("R012_TIMING_CANARY:one_shot")


@dataclass
class R012HostLoop:
    """One-slot dispatch loop; executor performs one mature motion attempt."""

    r012_controller: R012HostController
    r012_early_end: PathEarlyEndController
    queue: SerialOneSlotQueue
    executor: R012TrialExecutor | None = None
    optimizer: R012ProductionOptimizer | None = None
    events: list[str] = field(default_factory=list)
    terminal: bool = False
    stop_reason: str | None = None

    def _stop(self, reason: str) -> None:
        self.stop_reason = reason
        self.terminal = True
        self.events.append(f"STOP:{reason}")

    def _refill_r012_bo(self, *, base: Any = None, pending: tuple[Any, ...] = ()) -> QueueItem:
        if self.queue.pending_item is not None or self.queue.in_flight is not None:
            raise R012LiveAdapterError("R012 q=1 refill attempted while active")
        scheduled = self.r012_controller.in_flight
        if scheduled is None:
            if not self.r012_controller.scheduler.pending:
                if self.optimizer is None:
                    raise R012LiveAdapterError("R012 scheduler needs its q=1 optimizer after the queue is empty")
                self.optimizer.ask(
                    observations=self.r012_controller.observations,
                    pending=(),
                    incumbent=self.r012_controller.scheduler.confirmed_incumbent,
                    q=1,
                )
                scheduled = self.r012_controller.in_flight
            if scheduled is None:
                scheduled = self.r012_controller.next_dispatch()
        item = scheduled
        native = self.queue.enqueue(
            item.candidate, kind=item.kind,
            route_id=self.queue.route_id, session_id=self.queue.session_id, session_epoch=self.queue.session_epoch,
        )
        self.queue.dispatch()
        self.events.append(f"DISPATCH:{native.dispatch_id}:{item.kind}")
        return native

    def _run_step(self, *, campaign_mode: CampaignMode) -> Mapping[str, Any] | None:
        if self.terminal:
            return None
        if self.executor is None:
            raise R012LiveAdapterError("R012 guard-bound physical executor is required before dispatch")
        self.executor.ensure_guard_bound()
        if self.queue.in_flight is None:
            self._refill_r012_bo()
        item = self.queue.in_flight
        if item is None:
            raise R012LiveAdapterError("R012 one-shot has no in-flight item")
        result = self.executor(item)
        if not isinstance(result, Mapping):
            raise R012LiveAdapterError("R012 executor result is not readable")
        if campaign_mode is CampaignMode.ONE_SHOT:
            self.queue.result(result)
            self._stop("r012_one_attempt_home_and_stop")
            return result

        typed = R012TrialResult.from_mapping(
            result,
            item=item,
            campaign_id=self.r012_controller.campaign_id,
            run_id=self.r012_controller.run_id,
            attempt_id=self.r012_controller.attempt_id,
        )
        self.events.append(f"RESULT_VALIDATED:{typed.dispatch_id}")
        if typed.censored_observation is None:
            self.r012_controller.record_exact(
                candidate=item.candidate,
                sealed_mae_n=float(typed.sealed_mae_n),  # type: ignore[arg-type]
                dispatch_id=typed.dispatch_id,
                kind=item.kind,
            )
        else:
            self.r012_controller.record_censored(typed.censored_observation, run_kind=item.kind)
        self.events.append(f"INGEST:{typed.dispatch_id}")
        self.queue.result(typed.as_dict())
        self.events.append(f"QUEUE_CLEAR:{typed.dispatch_id}")
        self._refill_r012_bo()
        return typed.as_dict()

    def run_one(self) -> Mapping[str, Any] | None:
        """Preserve the existing one-shot Home-and-stop lifecycle."""

        return self._run_step(campaign_mode=CampaignMode.ONE_SHOT)

    def run_continuous_step(self) -> Mapping[str, Any] | None:
        """Complete one trial, ingest it once, and dispatch exactly one next item."""

        return self._run_step(campaign_mode=CampaignMode.CONTINUOUS)

    def run_continuous(self) -> Mapping[str, Any] | None:
        """Remain resident until the scheduler's existing campaign bounds close."""

        result: Mapping[str, Any] | None = None
        while not self.terminal:
            try:
                result = self.run_continuous_step()
            except Exception as exc:
                if "bound reached" not in str(exc):
                    raise
                self._stop(str(exc))
        return result


class R012LiveAdapter:
    """Owner-facing adapter that schedules exactly one physical attempt."""

    def __init__(self, *, descriptor: Any, controller: R012HostController, optimizer: R012ProductionOptimizer, early_end: PathEarlyEndController, queue: SerialOneSlotQueue) -> None:
        self.descriptor = descriptor
        self.r012_controller = controller
        self.optimizer = optimizer
        self.r012_early_end = early_end
        self.queue = queue

    def run_one_attempt(self, executor: Any) -> Mapping[str, Any] | None:
        if not isinstance(executor, R012TrialExecutor):
            raise R012LiveAdapterError("R012 run requires an explicit guard-bound trial executor")
        loop = R012HostLoop(self.r012_controller, self.r012_early_end, self.queue, executor=executor)
        return loop.run_one()


__all__ = [
    "NativeCandidate", "R012HostLoop", "R012LiveAdapter", "R012LiveAdapterError", "R012TrialExecutor", "R012TrialResult", "R012_TRIAL_RESULT_SCHEMA",
    "apply_r012_timing_canary_skip_qual", "dict_to_r006_candidate", "dispatch_id_from_record",
    "map_r012_kind_to_queue", "resolve_register_writer",
]
