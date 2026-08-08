"""Resident r006 lifecycle, disposition, crash identity, and offline campaign."""

from __future__ import annotations

import math
import json
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from step5d_force_objective import ForcePathSample

from .contracts import (
    PACKET_LEASE_S,
    TARGET_FORCE_N,
    load_contract,
)
from .fake_rtde import FakeRTDE, TimingRegression
from .motion_profile import r006_path_reference
from .lattice import (
    ANCHOR_POINT,
    IMode,
    ParameterPoint,
    PointObservation,
    TrustRegion,
    route_bfs,
    second_warm_start_plan,
    select_second_center,
    warm_start_plan,
)
from .objective import R006ObjectiveReceipt, build_receipt_from_samples
from .optimizer import PACCertificate, WarmStartOptimizer
from .queue import Dispatch, OfflineR006DurableQueue


# Offline fixture inputs are explicit at the fixture call site.  Production
# live construction receives these values from a typed versioned receipt.
OFFLINE_PAC_EPSILON_N = 0.05
OFFLINE_APPLICATION_MAE_THRESHOLD_N = 0.20


class R006RuntimeError(RuntimeError):
    """A lifecycle, lease, identity, or disposition invariant failed."""


class LifecycleState(str, Enum):
    HOME_IDLE = "HOME_IDLE"
    DISPATCH = "DISPATCH"
    ARM = "ARM"
    ACTIVE = "ACTIVE"
    RETURN = "RETURN"
    SEAL = "SEAL"
    TELL = "TELL"
    REFILL = "REFILL"
    STOPPED = "STOPPED"
    COMPLETE = "COMPLETE"


class Disposition(str, Enum):
    OBJECTIVE = "OBJECTIVE"
    SAFE_NONTRAINABLE = "SAFE_NONTRAINABLE"
    CODE_OR_EVIDENCE_BUG = "CODE_OR_EVIDENCE_BUG"
    SAFETY_OR_RETURN_FAILURE = "SAFETY_OR_RETURN_FAILURE"


@dataclass(frozen=True)
class RuntimeThresholds:
    """Typed, versioned values required before any ARM transition."""

    schema: str
    version: str
    pac_epsilon_n: float
    application_mae_threshold_n: float

    def __post_init__(self) -> None:
        if self.schema != "step5d.autotune-v4/r006-runtime-thresholds-v1" or self.version != "r006-v1":
            raise R006RuntimeError("runtime thresholds schema/version differs")
        if not math.isfinite(float(self.pac_epsilon_n)) or self.pac_epsilon_n <= 0.0:
            raise R006RuntimeError("pac_epsilon_n is invalid")
        if not math.isfinite(float(self.application_mae_threshold_n)) or self.application_mae_threshold_n <= 0.0:
            raise R006RuntimeError("application_mae_threshold_n is invalid")


@dataclass(frozen=True)
class AttemptIdentity:
    attempt_sequence: int
    execution_id: str
    logical_request_uid: str
    kind: str
    point: ParameterPoint

    def __post_init__(self) -> None:
        if isinstance(self.attempt_sequence, bool) or not isinstance(self.attempt_sequence, int) or self.attempt_sequence <= 0:
            raise R006RuntimeError("attempt sequence must be positive")
        if not self.execution_id or not self.logical_request_uid or not self.kind:
            raise R006RuntimeError("attempt identity is incomplete")


@dataclass(frozen=True)
class AttemptResult:
    identity: AttemptIdentity
    disposition: Disposition
    safe_return: bool
    safety_gate: bool
    motion_gate: bool
    timing_gate: bool
    contact_gate: bool
    return_gate: bool
    identity_gate: bool
    receipt: R006ObjectiveReceipt | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def eligible(self) -> bool:
        return self.disposition is Disposition.OBJECTIVE and self.receipt is not None and self.receipt.trainable


@dataclass(frozen=True)
class ActiveLease:
    execution_id: str
    armed_at_s: float
    expires_at_s: float

    def __post_init__(self) -> None:
        if not self.execution_id or not math.isfinite(self.armed_at_s) or not math.isfinite(self.expires_at_s) or not math.isclose(self.expires_at_s - self.armed_at_s, PACKET_LEASE_S, rel_tol=0.0, abs_tol=1e-12):
            raise R006RuntimeError("active lease is invalid")

    def validate(self, *, now_s: float, state: LifecycleState) -> None:
        if state is not LifecycleState.ACTIVE:
            raise R006RuntimeError("80 ms lease is scoped only to ARM-ed ACTIVE")
        if float(now_s) >= self.expires_at_s:
            raise R006RuntimeError("active lease expired")


class ResidentLifecycle:
    """HOME/intertrial waits are unbounded; only ACTIVE owns an 80 ms lease."""

    _allowed = {
        LifecycleState.HOME_IDLE: {LifecycleState.DISPATCH, LifecycleState.STOPPED, LifecycleState.COMPLETE},
        LifecycleState.DISPATCH: {LifecycleState.ARM, LifecycleState.HOME_IDLE, LifecycleState.STOPPED},
        LifecycleState.ARM: {LifecycleState.ACTIVE, LifecycleState.HOME_IDLE, LifecycleState.STOPPED},
        LifecycleState.ACTIVE: {LifecycleState.RETURN, LifecycleState.STOPPED},
        LifecycleState.RETURN: {LifecycleState.SEAL, LifecycleState.STOPPED},
        LifecycleState.SEAL: {LifecycleState.TELL, LifecycleState.STOPPED},
        LifecycleState.TELL: {LifecycleState.REFILL, LifecycleState.COMPLETE, LifecycleState.STOPPED},
        LifecycleState.REFILL: {LifecycleState.HOME_IDLE, LifecycleState.DISPATCH, LifecycleState.STOPPED},
        LifecycleState.STOPPED: set(),
        LifecycleState.COMPLETE: set(),
    }

    def __init__(self) -> None:
        self.state = LifecycleState.HOME_IDLE
        self.lease: ActiveLease | None = None
        self.thresholds: RuntimeThresholds | None = None
        self.events: list[LifecycleState] = [self.state]

    def transition(self, state: LifecycleState) -> None:
        if state not in self._allowed[self.state]:
            raise R006RuntimeError(f"illegal lifecycle transition {self.state}->{state}")
        self.state = state
        self.events.append(state)
        if state is not LifecycleState.ACTIVE:
            self.lease = None

    def wait_at_home(self) -> None:
        if self.state not in {LifecycleState.HOME_IDLE, LifecycleState.REFILL}:
            raise R006RuntimeError("Home wait is only valid at the resident safe hold")
        if self.state is LifecycleState.REFILL:
            self.transition(LifecycleState.HOME_IDLE)

    def provide_thresholds(self, thresholds: RuntimeThresholds) -> None:
        if self.state is not LifecycleState.HOME_IDLE:
            raise R006RuntimeError("typed threshold values are accepted only at Home")
        if not isinstance(thresholds, RuntimeThresholds):
            raise R006RuntimeError("ARM thresholds are not typed")
        self.thresholds = thresholds

    def arm(self, *, execution_id: str, now_s: float) -> ActiveLease:
        if self.state is not LifecycleState.DISPATCH:
            raise R006RuntimeError("ARM requires a dispatched candidate")
        if self.thresholds is None:
            raise R006RuntimeError("typed pac_epsilon_n and application_mae_threshold_n are required before ARM")
        self.transition(LifecycleState.ARM)
        self.transition(LifecycleState.ACTIVE)
        self.lease = ActiveLease(execution_id, float(now_s), float(now_s) + PACKET_LEASE_S)
        return self.lease

    def active_tick(self, *, now_s: float) -> None:
        if self.lease is None:
            raise R006RuntimeError("ACTIVE has no lease")
        self.lease.validate(now_s=float(now_s), state=self.state)

    def safe_return(self) -> None:
        if self.state is not LifecycleState.ACTIVE:
            raise R006RuntimeError("safe return requires ACTIVE")
        self.transition(LifecycleState.RETURN)
        self.transition(LifecycleState.SEAL)


@dataclass(frozen=True)
class DurableAttemptRow:
    identity: AttemptIdentity
    status: str
    complete: bool


class OfflineR006AttemptStore:
    """Minimal durable identity store used to prove cold resume semantics."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = None if root is None else Path(root)
        self.path = None if self.root is None else self.root / "attempts.jsonl"
        self.rows: list[DurableAttemptRow] = []
        self._load()

    @staticmethod
    def _row_payload(row: DurableAttemptRow) -> dict[str, Any]:
        return {
            "attempt_sequence": row.identity.attempt_sequence,
            "execution_id": row.identity.execution_id,
            "logical_request_uid": row.identity.logical_request_uid,
            "kind": row.identity.kind,
            "point_key": list(row.identity.point.key),
            "status": row.status,
            "complete": row.complete,
        }

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            payload = json.loads(line)
            key = payload["point_key"]
            point = ParameterPoint(int(key[0]), int(key[1]), int(key[2]), IMode(str(key[3])), None if key[4] is None else int(key[4]), int(key[5]), int(key[6]))
            identity = AttemptIdentity(int(payload["attempt_sequence"]), str(payload["execution_id"]), str(payload["logical_request_uid"]), str(payload["kind"]), point)
            self.rows.append(DurableAttemptRow(identity, str(payload["status"]), bool(payload["complete"])))

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import os

        with self.path.open("w", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(self._row_payload(row), sort_keys=True) + "\n" for row in self.rows))
            stream.flush()
            os.fsync(stream.fileno())

    def record_complete(self, identity: AttemptIdentity) -> None:
        if any(row.identity.attempt_sequence == identity.attempt_sequence and row.complete for row in self.rows):
            raise R006RuntimeError("complete attempt cannot be rerun")
        self.rows.append(DurableAttemptRow(identity, "COMPLETE", True))
        self._persist()

    def record_incomplete(self, identity: AttemptIdentity) -> None:
        self.rows.append(DurableAttemptRow(identity, "INCOMPLETE", False))
        self._persist()

    def resume_after_home(self, identity: AttemptIdentity) -> AttemptIdentity:
        if not any(row.identity == identity and not row.complete for row in self.rows):
            raise R006RuntimeError("no incomplete attempt to resume")
        resumed = AttemptIdentity(
            attempt_sequence=identity.attempt_sequence,
            execution_id=f"{identity.execution_id}:resume:{uuid.uuid4().hex}",
            logical_request_uid=identity.logical_request_uid,
            kind=identity.kind,
            point=identity.point,
        )
        self.rows.append(DurableAttemptRow(resumed, "RESUMED", False))
        self._persist()
        return resumed


@dataclass(frozen=True)
class CompletionResult:
    application_accepted: bool
    local_pac_accepted: bool
    status: str
    retest_passes: int
    retest_median_n: float | None
    anchor_median_n: float
    pending_cancelled: int
    global_convergence_claim: bool = False


class CompletionGate:
    def __init__(self, *, application_threshold_n: float, epsilon_n: float) -> None:
        self.application_threshold_n = float(application_threshold_n)
        self.epsilon_n = float(epsilon_n)

    def evaluate(
        self,
        *,
        application_candidate_mae_n: float,
        certificate: PACCertificate,
        retest_maes_n: Sequence[float],
        retest_gates: Sequence[bool],
        anchor_median_n: float,
        pending_cancelled: int,
    ) -> CompletionResult:
        if len(retest_maes_n) != 3 or len(retest_gates) != 3:
            raise R006RuntimeError("completion requires exactly three retests")
        passes = sum(bool(gate and mae <= self.application_threshold_n) for mae, gate in zip(retest_maes_n, retest_gates, strict=True))
        median = sorted(float(value) for value in retest_maes_n)[1]
        application = application_candidate_mae_n <= self.application_threshold_n and passes >= 2 and median <= anchor_median_n * 0.95 and all(retest_gates)
        local = certificate.passed and certificate.epsilon_n == self.epsilon_n
        return CompletionResult(
            application_accepted=application,
            local_pac_accepted=local,
            status="COMPLETE" if application and local else "BO",
            retest_passes=passes,
            retest_median_n=median,
            anchor_median_n=anchor_median_n,
            pending_cancelled=pending_cancelled,
        )


def apply_disposition(lifecycle: ResidentLifecycle, disposition: Disposition) -> str:
    """Apply the four terminal dispositions without auto-Home."""

    if disposition is Disposition.OBJECTIVE:
        return "TELL"
    if disposition is Disposition.SAFE_NONTRAINABLE:
        return "CONTINUE_AT_HOME"
    if disposition in {Disposition.CODE_OR_EVIDENCE_BUG, Disposition.SAFETY_OR_RETURN_FAILURE}:
        if lifecycle.state not in {LifecycleState.STOPPED, LifecycleState.COMPLETE}:
            lifecycle.transition(LifecycleState.STOPPED)
        return "STOP_AND_REVOKE_NO_AUTO_HOME"
    raise R006RuntimeError("unknown disposition")


@dataclass(frozen=True)
class FakeCampaignResult:
    status: str
    attempts: tuple[AttemptResult, ...]
    qualification_execution_ids: tuple[str, ...]
    first_warm_start: tuple[ParameterPoint, ...]
    second_center: ParameterPoint
    route_points: tuple[ParameterPoint, ...]
    second_warm_start: tuple[ParameterPoint, ...]
    hyperparameters_frozen: bool
    scheduler_q_history: tuple[int, ...]
    timing: TimingRegression
    echo_backlog: int
    pending_cancelled: int
    completion: CompletionResult
    path_snapshot_stage_id: str
    live_evidence: bool = False


def _raw_samples(force_n: float, *, attempt_sequence: int) -> tuple[ForcePathSample, ...]:
    values: list[ForcePathSample] = []
    # The union supplies both the r004 [0,55) shadow and the formal [5,60)
    # window without ever inserting a caller scalar into the objective path.
    times = [0.05 + index * 0.1 for index in range(550)] + [55.05 + index * 0.1 for index in range(50)]
    for index, path_time_s in enumerate(times):
        sequence = attempt_sequence * 100000 + index + 1
        values.append(
            ForcePathSample(
                path_time_s=path_time_s,
                path_phase=25,
                filtered_normal_n=force_n,
                source_sequences={"controller": sequence, "rtde": sequence},
                source_ages_s={"controller": 0.001, "rtde": 0.001},
                timestamp_s=path_time_s + 1000.0,
            )
        )
    return tuple(values)


def _objective_for(point: ParameterPoint, attempt_sequence: int, campaign_fingerprint: str, *, force_n: float) -> R006ObjectiveReceipt:
    return build_receipt_from_samples(
        _raw_samples(force_n, attempt_sequence=attempt_sequence),
        attempt_sequence=attempt_sequence,
        execution_id=f"r006-execution-{attempt_sequence}",
        campaign_fingerprint=campaign_fingerprint,
        candidate_uid=point.uid,
    )


def run_fake_campaign(
    root: Path | None = None,
    *,
    diagnostic_limit: int | None = None,
) -> FakeCampaignResult:
    """Run the complete deterministic offline qualification/BO/PAC fixture."""

    if diagnostic_limit is not None and (
        isinstance(diagnostic_limit, bool)
        or not isinstance(diagnostic_limit, int)
        or diagnostic_limit <= 0
    ):
        raise R006RuntimeError("offline diagnostic limit must be a positive integer")

    from .qualification import QualificationBranch

    contract = load_contract()
    path_snapshot = r006_path_reference((0.0, 0.0), 0.0)
    fake = FakeRTDE()
    timing = fake.timing_regression()
    queue = OfflineR006DurableQueue(Path(root) / "queue" if root is not None else Path("/tmp/r006-offline-queue"))
    lifecycle = ResidentLifecycle()
    lifecycle.provide_thresholds(
        RuntimeThresholds(
            schema="step5d.autotune-v4/r006-runtime-thresholds-v1",
            version="r006-v1",
            pac_epsilon_n=OFFLINE_PAC_EPSILON_N,
            application_mae_threshold_n=OFFLINE_APPLICATION_MAE_THRESHOLD_N,
        )
    )
    qualification = QualificationBranch()
    attempts: list[AttemptResult] = []
    qualification_ids: list[str] = []
    sequence = 0

    def execute(point: ParameterPoint, kind: str, *, force_n: float | None, disposition: Disposition = Disposition.OBJECTIVE) -> AttemptResult:
        nonlocal sequence
        if diagnostic_limit is not None and len(attempts) >= diagnostic_limit:
            raise R006RuntimeError("offline_diagnostic_limit")
        sequence += 1
        logical = f"r006-logical-{sequence}"
        execution_id = f"r006-execution-{sequence}"
        identity = AttemptIdentity(sequence, execution_id, logical, kind, point)
        lifecycle.state = LifecycleState.HOME_IDLE
        lifecycle.transition(LifecycleState.DISPATCH)
        queue.enqueue(point, kind=kind, logical_request_uid=logical)
        dispatch = queue.prepare_next()
        if dispatch is None:
            raise R006RuntimeError("offline dispatch did not produce an inflight row")
        lifecycle.arm(execution_id=execution_id, now_s=0.0)
        lifecycle.active_tick(now_s=0.040)
        lifecycle.safe_return()
        receipt = None if force_n is None or disposition is not Disposition.OBJECTIVE else _objective_for(point, sequence, contract.campaign_fingerprint, force_n=force_n)
        result = AttemptResult(
            identity=identity,
            disposition=disposition,
            safe_return=True,
            safety_gate=True,
            motion_gate=True,
            timing_gate=timing.passes,
            contact_gate=True,
            return_gate=True,
            identity_gate=True,
            receipt=receipt,
            metrics={"path_stage": 25, "active_lease_s": PACKET_LEASE_S},
        )
        queue.complete(dispatch, status=disposition.value)
        lifecycle.transition(LifecycleState.TELL)
        lifecycle.transition(LifecycleState.REFILL)
        attempts.append(result)
        return result

    # Exactly three fresh qualification trials, each bound to its own
    # execution_id.  Their force window is not trainable qualification data.
    for _ in range(3):
        result = execute(ANCHOR_POINT, "QUALIFICATION", force_n=None)
        qualification.bind(execution_id=result.identity.execution_id)
        qualification.accept(result)
        qualification_ids.append(result.identity.execution_id)
    if not qualification.complete:
        raise R006RuntimeError("offline qualification branch did not complete")

    first_plan = warm_start_plan(ANCHOR_POINT)
    warm_observations: list[PointObservation] = []
    for index, point in enumerate(first_plan, start=1):
        force = 5.40 if point == ANCHOR_POINT else 5.20 + (index % 3) * 0.04
        result = execute(point, "WARM_START_1", force_n=force)
        assert result.receipt is not None
        warm_observations.append(PointObservation(point, result.receipt.objective or 9.0, std_n=0.01, repeat_count=1))
    second_center = select_second_center(warm_observations)
    route = route_bfs(ANCHOR_POINT, second_center, region=TrustRegion(ANCHOR_POINT))
    optimizer = WarmStartOptimizer()
    optimizer.fit_group(1, warm_observations)
    for point in route.route_observations:
        optimizer.include_route_observation(point)
    # The route is a real safe sequence in the fixture, even when the first
    # warm-start probes already visited some of its coordinates.  Retaining
    # these observations proves the second center was reached through the
    # legal graph rather than teleported by the optimizer.
    route_observations: list[PointObservation] = []
    for point in route.points[1:]:
        routed = execute(point, "ROUTE", force_n=5.26)
        assert routed.receipt is not None
        route_observations.append(
            PointObservation(point, routed.receipt.objective or 9.0, std_n=0.01)
        )
    second_plan = second_warm_start_plan(second_center)
    second_observations: list[PointObservation] = []
    for point in second_plan:
        result = execute(point, "WARM_START_2", force_n=5.30)
        assert result.receipt is not None
        second_observations.append(PointObservation(point, result.receipt.objective or 9.0, std_n=0.005, repeat_count=2))
    optimizer.fit_group(2, (*route_observations, *second_observations))
    optimizer.freeze()

    # Keep two pending rows to prove durable cancellation when the threshold
    # candidate is sealed.
    queue.enqueue(second_center, kind="BO_TRIAL", logical_request_uid="pending-a")
    queue.enqueue(second_center.with_step("P", 1), kind="BO_TRIAL", logical_request_uid="pending-b")
    pending_cancelled = len(queue.cancel_pending(reason="application threshold reached"))
    scheduler_q_history = (4, 4, 1)

    candidate = second_center.with_step("D", 1)
    threshold_result = execute(candidate, "BO_TRIAL", force_n=5.10)
    assert threshold_result.receipt is not None and threshold_result.receipt.objective is not None
    certificate = PACCertificate(
        incumbent_ucb_n=0.08,
        minimum_lcb_n=0.04,
        epsilon_n=OFFLINE_PAC_EPSILON_N,
        region_size=6,
        simultaneous_z95=2.8,
        global_convergence_claim=False,
    )
    retest_results = [
        execute(candidate, "RETEST", force_n=5.05),
        execute(candidate, "RETEST", force_n=5.08),
        execute(candidate, "RETEST", force_n=5.06),
    ]
    retest_maes = [result.receipt.objective for result in retest_results if result.receipt is not None]
    completion = CompletionGate(
        application_threshold_n=OFFLINE_APPLICATION_MAE_THRESHOLD_N,
        epsilon_n=OFFLINE_PAC_EPSILON_N,
    ).evaluate(
        application_candidate_mae_n=threshold_result.receipt.objective,
        certificate=certificate,
        retest_maes_n=tuple(float(value) for value in retest_maes),
        retest_gates=tuple(result.safe_return and result.timing_gate and result.identity_gate for result in retest_results),
        anchor_median_n=0.40,
        pending_cancelled=pending_cancelled,
    )
    return FakeCampaignResult(
        status=completion.status,
        attempts=tuple(attempts),
        qualification_execution_ids=tuple(qualification_ids),
        first_warm_start=first_plan,
        second_center=second_center,
        route_points=route.points,
        second_warm_start=second_plan,
        hyperparameters_frozen=optimizer.frozen,
        scheduler_q_history=scheduler_q_history,
        timing=timing,
        echo_backlog=0,
        pending_cancelled=pending_cancelled,
        completion=completion,
        path_snapshot_stage_id=str(path_snapshot["stage_id"]),
    )


__all__ = [
    "ActiveLease",
    "AttemptIdentity",
    "AttemptResult",
    "OfflineR006AttemptStore",
    "apply_disposition",
    "CompletionGate",
    "CompletionResult",
    "Disposition",
    "FakeCampaignResult",
    "LifecycleState",
    "ResidentLifecycle",
    "R006RuntimeError",
    "RuntimeThresholds",
    "OFFLINE_APPLICATION_MAE_THRESHOLD_N",
    "OFFLINE_PAC_EPSILON_N",
    "run_fake_campaign",
]
