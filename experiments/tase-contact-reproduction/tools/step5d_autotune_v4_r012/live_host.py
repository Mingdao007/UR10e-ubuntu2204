"""R012 live-host brain: scheduler + 5D qLogNEI over R008 B3 motion/seal/ledger.

Motion, TP triplet, durable queue, seal join, and PATH60 timing still come from the
published R008 B3 host runner in the sibling worktree.  This module is the R012
optimizer/scheduler/censor wiring only — it does not upload, play, or move the robot.
"""

from __future__ import annotations

import os
import random
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from step5d_optimizer_runtime import OptimizerSubprocessClient

from .censor import (
    BIN_WIDTH_S,
    DENOMINATOR_BINS,
    GUARD_BINS,
    CensoredObservation,
    ExactObservation,
    Observation,
    PathEarlyEndHandshake,
    evaluate_censor_prefix,
    validate_observation,
)
from .common import R012ValueError, json_tree
from .ledger import DISPATCH_RECORD_TYPE, OBSERVATION_RECORD_TYPE, Ledger
from .qlognei import (
    MODEL_DIMENSION_COUNT,
    Q,
    CandidateProposal,
    ProductionGPConfig,
    ask_qlognei,
    candidate_to_log_features,
    candidate_to_normalized,
    fit_production_gp,
    fit_test_oracle,
    normalized_to_candidate,
    physical_candidate_key,
    production_qlognei_observations,
    snap_candidate_to_live_lattice,
)
from .path_cbf_live import R012PathGuardStack
from .scheduler import (
    REPEAT_CONFIRMATIONS,
    ConfirmedIncumbentScheduler,
    ScheduledCandidate,
    scheduler_candidate_key,
)


R012_R008_WT = (
    "/home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/tase-contact-reproduction/tools"
)

R012_FORMAL_LIVE_ALLOW_TOKEN = "r012_b3_scheduler_qlognei"
MIN_EXACT_ROWS_FOR_BO = REPEAT_CONFIRMATIONS
CANDIDATE_POOL_SIZE = 64


class R012HostError(R012ValueError):
    """R012 host composition or activation gate failed."""


class R012QuarantineViolation(R012HostError):
    """R012 formal live env conflicts with R008 historical quarantine."""


class AutotuneRunMode(str, Enum):
    """Small explicit mode switch; bounded live is the routine default."""

    BOUNDED_LIVE = "bounded_live"
    FORMAL_RELEASE = "formal_release"

    @classmethod
    def parse(cls, value: str | None) -> "AutotuneRunMode":
        raw = cls.BOUNDED_LIVE.value if value is None else str(value).strip().lower()
        try:
            return cls(raw)
        except ValueError as exc:
            raise R012HostError(f"unsupported R012 run mode {value!r}") from exc


def require_r012_run_mode(mode: AutotuneRunMode | str | None, entrypoint: str) -> AutotuneRunMode:
    """Apply the formal token only to the explicit formal-release mode."""

    selected = mode if isinstance(mode, AutotuneRunMode) else AutotuneRunMode.parse(mode)
    if selected is AutotuneRunMode.FORMAL_RELEASE:
        require_r012_formal_live(entrypoint)
    return selected


def require_r012_formal_live(entrypoint: str) -> None:
    """Fail closed unless ``R012_ALLOW_FORMAL_LIVE`` matches the R012 token.

    Rejects any ``R008_ALLOW_FORMAL_LIVE`` bypass — R012 uses its own quarantine.
    """

    if os.environ.get("R008_ALLOW_FORMAL_LIVE", "").strip():
        raise R012QuarantineViolation(
            f"R012 host rejects R008 quarantine env R008_ALLOW_FORMAL_LIVE ({entrypoint})"
        )
    allow = os.environ.get("R012_ALLOW_FORMAL_LIVE", "").strip()
    if allow != R012_FORMAL_LIVE_ALLOW_TOKEN:
        raise R012HostError(
            f"R012 formal live blocked at {entrypoint}; "
            f"set R012_ALLOW_FORMAL_LIVE={R012_FORMAL_LIVE_ALLOW_TOKEN!r}"
        )


@dataclass(frozen=True)
class OptimizerAskResult:
    """Duck-type compatible with R008 ``OptimizerAsk`` (``.candidate``, ``.metadata``)."""

    candidate: Mapping[str, Any] | Any
    metadata: Mapping[str, Any]


class RegisterWriter(Protocol):
    """Minimal live-writer surface for protocol 612012 registers 35/36."""

    def write_input_integer_register(self, index: int, value: int) -> None: ...

    def read_output_integer_register(self, index: int) -> int: ...


@dataclass
class PathEarlyEndController:
    """Bridge :class:`PathEarlyEndHandshake` to live writer registers 35/36."""

    handshake: PathEarlyEndHandshake = field(default_factory=PathEarlyEndHandshake)
    writer: RegisterWriter | None = None
    requested_sequence: int | None = None

    @property
    def runtime_protocol(self) -> int:
        return self.handshake.runtime_protocol

    @property
    def request_register(self) -> int:
        return self.handshake.request_register

    @property
    def ack_register(self) -> int:
        return self.handshake.ack_register

    def arm(self, attempt_sequence: int) -> None:
        self.handshake.arm(attempt_sequence)
        self.requested_sequence = None
        if self.writer is not None:
            self.writer.write_input_integer_register(self.request_register, 0)

    def request_early_end(self, sequence: int) -> bool:
        ok = self.handshake.request(sequence)
        if ok and self.writer is not None:
            self.requested_sequence = int(sequence)
            self.writer.write_input_integer_register(self.request_register, sequence)
        elif ok:
            self.requested_sequence = int(sequence)
        return ok

    @property
    def requested(self) -> bool:
        """Whether this physical attempt has issued its typed early-end request."""

        return self.requested_sequence is not None

    def read_ack(self) -> int | None:
        if self.writer is not None:
            return int(self.writer.read_output_integer_register(self.ack_register))
        if self.handshake.rtde is not None:
            return self.handshake.rtde.output_registers.get(self.ack_register)
        return None

    def read_completion_registers(self) -> tuple[int | None, int | None, int | None]:
        if self.writer is None:
            rtde = self.handshake.rtde
            if rtde is None:
                return (None, None, None)
            return (
                rtde.output_registers.get(self.ack_register),
                rtde.output_registers.get(self.handshake.terminal_reason_register),
                rtde.output_registers.get(self.handshake.reason_register),
            )
        # The deployed R012 high-rate recipe does not expose output register
        # 36.  Its typed transport can nevertheless prove the same
        # sequence-matched completion from the locally issued request plus
        # the terminal state/guard/reason snapshot.  Prefer that explicit
        # adapter seam over treating the unavailable register as an implicit
        # zero; a transport without the seam remains fail-closed.
        completion = getattr(self.writer, "read_r013_completion_registers", None)
        if callable(completion) and self.requested_sequence is not None:
            return tuple(completion(int(self.requested_sequence)))
        return (
            int(self.writer.read_output_integer_register(self.ack_register)),
            int(self.writer.read_output_integer_register(self.handshake.terminal_reason_register)),
            int(self.writer.read_output_integer_register(self.handshake.reason_register)),
        )

    def observe_live_completion(self) -> bool:
        """Mirror a real TP completion into the typed handshake state machine."""

        sequence = self.handshake.attempt_sequence
        rtde = self.handshake.rtde
        if sequence is None or rtde is None:
            return False
        ack, terminal_reason, reason43_subtype = self.read_completion_registers()
        if (ack, terminal_reason, reason43_subtype) != (sequence, 0, 0):
            return False
        rtde.output_registers[self.ack_register] = sequence
        rtde.output_registers[self.handshake.terminal_reason_register] = 0
        rtde.output_registers[self.handshake.reason_register] = 0
        from .censor import HandshakeState

        rtde.state = HandshakeState.PATH_COMPLETE
        return True

    def tp_step(self) -> bool:
        return self.handshake.tp_step()

    def seal_censored_observation(self, **kwargs: Any) -> CensoredObservation | None:
        return self.handshake.seal_censored_observation(**kwargs)


@dataclass
class ActiveCensorRuntime:
    """Per-attempt causal binning and sequence-acked censor closure.

    The runtime owns no motion primitive.  It may write only the R012 graceful
    PATH-end request register and cannot produce an observation until TP
    acknowledgement plus the inherited safe-return gates are all closed.
    """

    controller: PathEarlyEndController
    campaign_id: str
    run_id: str
    attempt_id: str
    scheduled: ScheduledCandidate | None = None
    attempt_sequence: int | None = None
    incumbent_mean_n: float | None = None
    closed_absolute_errors: list[float] = field(default_factory=list)
    _bin_index: int | None = None
    _bin_force_sum: float = 0.0
    _bin_sample_count: int = 0
    requested: bool = False

    def arm(
        self,
        *,
        scheduled: ScheduledCandidate,
        attempt_sequence: int,
        incumbent_mean_n: float | None,
    ) -> None:
        self.scheduled = scheduled
        self.attempt_sequence = int(attempt_sequence)
        self.incumbent_mean_n = incumbent_mean_n
        self.closed_absolute_errors.clear()
        self._bin_index = None
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        self.requested = False
        self.controller.arm(self.attempt_sequence)

    def _close_current_bin(self) -> None:
        if self._bin_index is None or self._bin_sample_count <= 0:
            return
        mean_force_n = self._bin_force_sum / float(self._bin_sample_count)
        self.closed_absolute_errors.append(abs(mean_force_n - 5.0))
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        if self.scheduled is None or self.requested:
            return
        decision = evaluate_censor_prefix(
            self.closed_absolute_errors,
            run_kind=self.scheduled.kind,
            confirmed_incumbent_mean_n=self.incumbent_mean_n,
            novel_bo=self.scheduled.abort_allowed,
        )
        if decision.triggered:
            sequence = self.attempt_sequence
            if sequence is None or not self.controller.request_early_end(sequence):
                raise R012HostError("R012 active censor could not issue sequence-matched request")
            self.requested = True

    def observe_path_sample(self, sample: Any) -> None:
        if self.scheduled is None or self.requested:
            return
        state = getattr(sample, "state", None)
        path_time = getattr(sample, "path_time_s", None)
        force = getattr(sample, "filtered_normal_n", None)
        try:
            path_time_f = float(path_time)
            force_f = float(force)
        except (TypeError, ValueError):
            return
        if state != 25 or not math.isfinite(path_time_f) or not math.isfinite(force_f):
            return
        if not 5.0 <= path_time_f < 60.0:
            return
        bin_index = int(math.floor((path_time_f - 5.0) / BIN_WIDTH_S))
        if not 0 <= bin_index < DENOMINATOR_BINS:
            return
        if self._bin_index is None:
            self._bin_index = bin_index
        elif bin_index != self._bin_index:
            if bin_index < self._bin_index:
                raise R012HostError("R012 censor PATH bin index regressed")
            self._close_current_bin()
            if self.requested:
                return
            self._bin_index = bin_index
        self._bin_force_sum += force_f
        self._bin_sample_count += 1

    def finalize(
        self,
        *,
        dispatch_id: str,
        return_guard: bool,
        home: bool,
        safe_return: bool,
    ) -> CensoredObservation | None:
        if not self.requested or self.scheduled is None or self.incumbent_mean_n is None:
            return None
        if len(self.closed_absolute_errors) < GUARD_BINS:
            raise R012HostError("R012 active censor fired before the 55-bin guard")
        if not self.controller.observe_live_completion():
            return None
        self.controller.handshake.begin_return_home()
        row = self.controller.seal_censored_observation(
            dispatch_id=dispatch_id,
            candidate=self.scheduled.candidate,
            closed_absolute_errors=tuple(self.closed_absolute_errors),
            watermark_s=5.0 + len(self.closed_absolute_errors) * BIN_WIDTH_S,
            confirmed_incumbent_mean_n=self.incumbent_mean_n,
            campaign_id=self.campaign_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            return_guard=bool(return_guard),
            home=bool(home),
            safe_return=bool(safe_return),
        )
        return row

    def reset(self) -> None:
        self.scheduled = None
        self.attempt_sequence = None
        self.incumbent_mean_n = None
        self.closed_absolute_errors.clear()
        self._bin_index = None
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        self.requested = False
        self.controller.handshake.reset_for_next_arm()


@dataclass
class R012HostController:
    """Serial scheduler facade for host dispatch and typed observation ingest."""

    campaign_id: str
    run_id: str
    attempt_id: str
    ledger: Ledger | None = None
    scheduler: ConfirmedIncumbentScheduler = field(init=False)
    _dispatch_counter: int = 0

    def __post_init__(self) -> None:
        self.scheduler = ConfirmedIncumbentScheduler(
            campaign_id=self.campaign_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
        )
        self._restore_from_ledger()

    @staticmethod
    def _scheduled_from_mapping(value: Mapping[str, Any]) -> ScheduledCandidate:
        return ScheduledCandidate(
            candidate=value["candidate"],
            kind=value["kind"],
            ordinal=value["ordinal"],
            confirmation_index=value["confirmation_index"],
            abort_allowed=value["abort_allowed"],
        )

    def _restore_from_ledger(self) -> None:
        if self.ledger is None:
            return
        if (self.ledger.campaign_id, self.ledger.run_id, self.ledger.attempt_id) != (
            self.campaign_id, self.run_id, self.attempt_id,
        ):
            raise R012HostError("R012 host controller ledger identity differs")
        for record in self.ledger.records[1:]:
            record_type = record["record_type"]
            if record_type == DISPATCH_RECORD_TYPE:
                expected = self._scheduled_from_mapping(record["dispatch"])
                pending = self.scheduler.pending
                if (
                    expected.kind == "CHALLENGER_CONFIRM"
                    and (not pending or pending[0].kind != expected.kind or pending[0].key != expected.key)
                    and self.scheduler.in_flight is None
                ):
                    self.scheduler.resume_current_policy(
                        discard_unpersisted_pending=(
                            self.scheduler.confirmed_incumbent is not None
                        )
                    )
                    pending = self.scheduler.pending
                if not pending or (
                    pending[0].kind != expected.kind or pending[0].key != expected.key
                ):
                    if expected.kind not in {"BO_TRIAL", "NOVEL_BO"}:
                        raise R012HostError("R012 persisted dispatch is not reproducible")
                    self.scheduler.enqueue_bo(expected.candidate)
                replayed = self.scheduler.next()
                if replayed.as_dict() != expected.as_dict():
                    raise R012HostError("R012 persisted dispatch order differs")
                self._dispatch_counter += 1
            elif record_type == OBSERVATION_RECORD_TYPE:
                row = validate_observation(record["observation"])
                self.scheduler.record(row, run_kind=row.kind, historical_policy=True)
        last_record_type = self.ledger.records[-1]["record_type"]
        self.scheduler.resume_current_policy(
            discard_unpersisted_pending=(
                last_record_type == OBSERVATION_RECORD_TYPE
                and self.scheduler.confirmed_incumbent is not None
            )
        )

    @property
    def in_flight(self) -> ScheduledCandidate | None:
        return self.scheduler.in_flight

    @property
    def observations(self) -> tuple[Observation, ...]:
        """Typed exact/censored rows restored from the append-only ledger."""

        return self.scheduler.observations

    def next_dispatch(self) -> ScheduledCandidate:
        item = self.scheduler.next()
        if self.ledger is not None:
            self.ledger.append_dispatch(item)
        self._dispatch_counter += 1
        return item

    def record_exact(
        self,
        *,
        candidate: Mapping[str, Any],
        sealed_mae_n: float,
        dispatch_id: str,
        kind: str | None = None,
        observation_variance_n2: float = 0.01,
    ) -> ExactObservation:
        row = ExactObservation(
            dispatch_id,
            candidate,
            float(sealed_mae_n),
            self.campaign_id,
            self.run_id,
            self.attempt_id,
            observation_variance_n2=observation_variance_n2,
            kind=kind or "BO_TRIAL",
        )
        self.scheduler.record(row, run_kind=row.kind)
        if self.ledger is not None:
            self.ledger.append_observation(row)
        return row

    def record_censored(
        self,
        observation: CensoredObservation | Mapping[str, Any],
        *,
        run_kind: str | None = None,
    ) -> None:
        row = validate_observation(observation)
        if not isinstance(row, CensoredObservation):
            raise R012HostError("record_censored requires a censored observation")
        self.scheduler.record(row, run_kind=run_kind or row.kind)
        if self.ledger is not None:
            self.ledger.append_observation(row)

    def scheduler_pending_kinds(self) -> tuple[str, ...]:
        return tuple(item.kind for item in self.scheduler.pending)

    def has_pre_bo_scheduler_work(self) -> bool:
        if self.scheduler.confirmed_incumbent is None:
            return True
        return any(
            kind in {"REFERENCE", "CHALLENGER_CONFIRM", "INCUMBENT_RETEST", "QUALIFICATION"}
            for kind in self.scheduler_pending_kinds()
        )

    def exact_training_observations(self) -> tuple[ExactObservation, ...]:
        return tuple(
            row
            for row in self.scheduler.observations
            if isinstance(row, ExactObservation)
        )

    def receipt(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r012-host-controller-v1",
            "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id,
            "dispatch_counter": self._dispatch_counter,
            "scheduler": self.scheduler.receipt(),
        }


def _offline_bo_proposal(
    fit: Any,
    candidates: Sequence[Mapping[str, Any]],
    *,
    evaluated_keys: set[tuple[Any, ...]],
    pending_keys: set[tuple[Any, ...]],
) -> CandidateProposal:
    """Dry-run / CPU-only proposal using the test-oracle GP surrogate."""

    excluded = evaluated_keys | pending_keys
    admissible: list[tuple[float, Mapping[str, Any], tuple[float, ...]]] = []
    for candidate in candidates:
        key = physical_candidate_key(candidate)
        if key in excluded:
            continue
        features = candidate_to_log_features(candidate)
        mean, _var = fit.model.predict(features)
        unit = candidate_to_normalized(candidate)
        admissible.append((float(mean), candidate, unit))
    if not admissible:
        raise R012HostError("offline BO pool is exhausted")
    score, candidate, unit = min(admissible, key=lambda item: (item[0], tuple(-v for v in item[2])))
    return CandidateProposal(candidate, unit, -score, Q, tuple(sorted(excluded, key=repr)))


def _deterministic_candidate_pool(*, seed: int, count: int = CANDIDATE_POOL_SIZE) -> tuple[dict[str, Any], ...]:
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for _ in range(count * 4):
        unit = tuple(rng.random() for _ in range(MODEL_DIMENSION_COUNT))
        candidate = snap_candidate_to_live_lattice(
            normalized_to_candidate(unit, template={"target_force_n": 5.0})
        )
        key = physical_candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
        if len(out) >= count:
            break
    if len(out) < count:
        raise R012HostError("R012 executable lattice pool is too small")
    return tuple(out)


@dataclass
class R012ProductionOptimizer:
    """Duck-type replacement for ``R008ProductionOptimizer`` during R012 live host."""

    campaign_id: str
    run_id: str
    attempt_id: str
    controller: R012HostController
    gp_config: ProductionGPConfig = field(default_factory=ProductionGPConfig)
    candidate_seed: int = 6012
    use_production_gp: bool = False
    optimizer_client: OptimizerSubprocessClient | None = None
    last_ask_metadata: dict[str, Any] = field(default_factory=dict)

    def ask(
        self,
        *,
        observations: Sequence[Any] = (),
        pending: Sequence[Any] = (),
        incumbent: Any = None,
        q: int = 1,
    ) -> OptimizerAskResult:
        if q != Q:
            raise R012HostError(f"R012 production optimizer is serial q={Q} only")
        pending_keys = _pending_keys(pending)
        exact_rows = _exact_rows(observations, self.controller)
        if self.controller.has_pre_bo_scheduler_work() or len(exact_rows) < MIN_EXACT_ROWS_FOR_BO:
            return self._ask_scheduler_head()
        evaluated = {physical_candidate_key(row.candidate) for row in exact_rows}
        pool = _deterministic_candidate_pool(seed=self.candidate_seed + len(exact_rows))
        use_botorch = False
        if self.use_production_gp:
            if self.optimizer_client is None:
                raise R012HostError("R012 production optimizer subprocess is missing")
            result = self.optimizer_client.request({
                "observations": [row.as_dict() for row in exact_rows],
                "candidates": list(pool),
                "pending_keys": [list(key) for key in pending_keys],
                "gp_config": self.gp_config.as_dict(),
            })
            proposal_raw = result["proposal"]
            proposal = CandidateProposal(
                candidate=proposal_raw["candidate"],
                normalized_X=tuple(proposal_raw["normalized_X"]),
                acquisition_value=float(proposal_raw["acquisition_value"]),
                q=int(proposal_raw["q"]),
                excluded_keys=tuple(),
            )
            fit_receipt = dict(result["fit_receipt"])
            use_botorch = True
        else:
            fit = self._fit_gp(exact_rows)
            proposal = _offline_bo_proposal(
                fit,
                pool,
                evaluated_keys=evaluated,
                pending_keys=pending_keys,
            )
        self.controller.scheduler.enqueue_bo(proposal.candidate)
        scheduled = self.controller.next_dispatch()
        metadata = {
            "r012_source": "qlognei" if use_botorch else "qlognei_offline_fallback",
            "acquisition_value": proposal.acquisition_value,
            "scheduled_kind": scheduled.kind,
            "model_dimensions": MODEL_DIMENSION_COUNT,
            "excluded_key_count": len(proposal.excluded_keys),
        }
        if use_botorch:
            metadata["production_gp_fit"] = fit_receipt
        self.last_ask_metadata = metadata
        return OptimizerAskResult(candidate=scheduled.candidate, metadata=metadata)

    def _ask_scheduler_head(self) -> OptimizerAskResult:
        pending = self.controller.scheduler.pending
        if not pending:
            raise R012HostError("R012 scheduler queue is empty before BO admission")
        head = pending[0]
        metadata = {
            "r012_source": "scheduler",
            "scheduled_kind": head.kind,
            "confirmation_index": head.confirmation_index,
            "ordinal": head.ordinal,
            "abort_allowed": head.abort_allowed,
        }
        self.last_ask_metadata = metadata
        return OptimizerAskResult(candidate=head.candidate, metadata=metadata)

    def _fit_gp(self, exact_rows: Sequence[ExactObservation]):
        rows: Sequence[Observation] = tuple(exact_rows)
        if self.use_production_gp:
            return fit_production_gp(rows, config=self.gp_config, allow_historical_out_of_box=True)
        return fit_test_oracle(rows, config=self.gp_config)

    def tell(self, record: Any) -> None:
        """Optional hook for R008 adapter compatibility; ledger authority stays R008."""

        _ = record


def _pending_keys(pending: Sequence[Any]) -> set[tuple[Any, ...]]:
    keys: set[tuple[Any, ...]] = set()
    for item in pending:
        candidate = item.candidate if hasattr(item, "candidate") else item
        if isinstance(candidate, Mapping):
            keys.add(scheduler_candidate_key(candidate))
    return keys


def _exact_rows(
    observations: Sequence[Any],
    controller: R012HostController,
) -> tuple[ExactObservation, ...]:
    if observations:
        rows: list[ExactObservation] = []
        for item in observations:
            if isinstance(item, ExactObservation):
                rows.append(item)
            elif hasattr(item, "candidate") and hasattr(item, "objective_n"):
                rows.append(
                    ExactObservation(
                        getattr(item, "dispatch_id", "ledger-row"),
                        item.candidate,
                        float(item.objective_n),
                        controller.campaign_id,
                        controller.run_id,
                        controller.attempt_id,
                        kind=getattr(item, "kind", "BO_TRIAL"),
                    )
                )
        if rows:
            return tuple(rows)
    return controller.exact_training_observations()


def build_host_stack(
    *,
    campaign_id: str,
    run_id: str,
    attempt_id: str,
    use_production_gp: bool = False,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Construct the R012 brain stack for dry-run or R008 adapter injection."""

    ledger = None
    if ledger_path is not None:
        ledger = Ledger.load(
            ledger_path,
            campaign_id=campaign_id,
            run_id=run_id,
            attempt_id=attempt_id,
        )
    controller = R012HostController(
        campaign_id=campaign_id,
        run_id=run_id,
        attempt_id=attempt_id,
        ledger=ledger,
    )
    optimizer = R012ProductionOptimizer(
        campaign_id=campaign_id,
        run_id=run_id,
        attempt_id=attempt_id,
        controller=controller,
        use_production_gp=use_production_gp,
        optimizer_client=(
            OptimizerSubprocessClient(worker_module="step5d_autotune_v4_r012.optimizer_worker")
            if use_production_gp else None
        ),
    )
    path_early_end = PathEarlyEndController()
    path_guard_stack = R012PathGuardStack()
    return {
        "schema": "step5d.autotune-v4/r012-host-stack-v1",
        "campaign_id": campaign_id, "run_id": run_id, "attempt_id": attempt_id,
        "r008_motion_tools": R012_R008_WT,
        "controller": controller,
        "optimizer": optimizer,
        "path_early_end": path_early_end,
        "path_guard_stack": path_guard_stack,
        "ledger": ledger,
        "protocol": path_early_end.runtime_protocol,
        "gp_observation_count": len(production_qlognei_observations(())),
    }


def dry_run_dispatch_plan(
    stack: Mapping[str, Any],
    *,
    reference_repeats: int = REPEAT_CONFIRMATIONS,
) -> dict[str, Any]:
    """Simulate REFERENCE×N scheduler dispatches then one BO metadata probe."""

    controller: R012HostController = stack["controller"]  # type: ignore[assignment]
    optimizer: R012ProductionOptimizer = stack["optimizer"]  # type: ignore[assignment]
    reference_plan: list[dict[str, Any]] = []
    for _ in range(reference_repeats):
        item = controller.next_dispatch()
        reference_plan.append(item.as_dict())
        controller.record_exact(
            candidate=item.candidate,
            sealed_mae_n=0.42,
            dispatch_id=f"dry-{item.ordinal}",
            kind=item.kind,
        )
    bo_probe = optimizer.ask()
    return {
        "schema": "step5d.autotune-v4/r012-host-dry-run-plan-v1",
        "reference_dispatches": reference_plan,
        "bo_ask_metadata": json_tree(bo_probe.metadata),
        "bo_candidate_keys": list(bo_probe.candidate.keys()) if isinstance(bo_probe.candidate, Mapping) else [],
        "confirmed_incumbent": controller.scheduler.confirmed_incumbent_mean_n,
    }


__all__ = [
    "ActiveCensorRuntime",
    "CANDIDATE_POOL_SIZE",
    "MIN_EXACT_ROWS_FOR_BO",
    "OptimizerAskResult",
    "PathEarlyEndController",
    "R012_FORMAL_LIVE_ALLOW_TOKEN",
    "R012HostController",
    "R012HostError",
    "R012ProductionOptimizer",
    "R012QuarantineViolation",
    "R012_R008_WT",
    "RegisterWriter",
    "build_host_stack",
    "dry_run_dispatch_plan",
    "AutotuneRunMode", "require_r012_formal_live", "require_r012_run_mode",
]
