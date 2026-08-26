"""Single-writer Autotuner V5 live owner and sealed-chain fan-in.

The module deliberately owns the seam that the earlier V5 primitives did not:
one already-admitted mature writer reads the one Kunwei stream, publishes the
layout-607 packet, drives one active plus one prepared candidate, and seals one
continuous ``.r013life`` artifact after real Home.  No performance/readiness
force window is consulted here.  Raw normal/force/torque remain inputs only to
the inherited hard-safety packet guard and to evidence capture.

The lower-level transport and controller-package lifecycle are constructed by
``build_v5_live_context`` near the bottom of this file.  The rollover kernel,
artifact sealer, and plan conversion are transport-free and independently
testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import gc
import hashlib
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

try:
    from step5d_autotune_v4_r004.home import ReturnEvidence, evaluate_return
    from step5d_autotune_v4_r004.wire import AttemptKind, CommandMode, SensorPacket
    from step5d_autotune_v4_r013.lifecycle_trace import LifecycleTrace
    from step6_figure8_autotune_v1.core import (
        CORRECTION_NORMALIZATION_SCALES,
        CorrectionPolicyV1,
    )
    from step6_figure8_autotune_v1.physical_candidate import (
        FigureEightPhysicalCandidateV1,
    )
    from step6_figure8_autotune_v1.v5_campaign import V5TrialPlan
    from step6_figure8_autotune_v1.v5_composition_contract import (
        BaseOutputOverlayV2,
        RolloverCommand,
        RolloverOutputOverlayV2,
        V5RolloverInput,
        V5TPState,
    )
    from step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        ContactRolloverBoundaryV2,
        ControllerCommitAckV2,
        FigureEightPhysicalRecordV2,
        GateClosureEvidenceV2,
        GateFamiliesV2,
        HomeBoundaryV2,
        LedgerRole,
        LifecycleEventKind,
        LifecycleEventV2,
        MetricSnapshotV1,
        PathTailClosureV2,
        TrialBoundaryReceiptV2,
        TrialSliceV2,
        V5ChainBindingV2,
        V5_EVENT_BUNDLE_SCHEMA,
        bind_v5_chain,
        canonical_sha256,
        index_sealed_r013_artifact,
        lifecycle_row_evidence_sha256,
        persist_gate_observation_artifact,
    )
    from step6_figure8_autotune_v1.v5_live_runtime import (
        V5CandidateRuntimeSpecV1,
        V5ChainControlV1,
        V5ControlBackend,
        V5ControlCommandV1,
        make_v5_runtime_path_reference,
        mature_backend_factory,
    )
    from step6_figure8_autotune_v1.v5_register_transport import (
        V5MatureTransportAdapter,
        V5OutputSnapshotV1,
    )
    from step6_figure8_autotune_v1.v5_rollover import (
        FORMAL_METRIC_START_S,
        PATH_END_S,
        TAIL_END_S,
        CandidateIdentityV1,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step5d_autotune_v4_r004.home import ReturnEvidence, evaluate_return
    from tools.step5d_autotune_v4_r004.wire import AttemptKind, CommandMode, SensorPacket
    from tools.step5d_autotune_v4_r013.lifecycle_trace import LifecycleTrace
    from tools.step6_figure8_autotune_v1.core import (
        CORRECTION_NORMALIZATION_SCALES,
        CorrectionPolicyV1,
    )
    from tools.step6_figure8_autotune_v1.physical_candidate import (
        FigureEightPhysicalCandidateV1,
    )
    from tools.step6_figure8_autotune_v1.v5_campaign import V5TrialPlan
    from tools.step6_figure8_autotune_v1.v5_composition_contract import (
        BaseOutputOverlayV2,
        RolloverCommand,
        RolloverOutputOverlayV2,
        V5RolloverInput,
        V5TPState,
    )
    from tools.step6_figure8_autotune_v1.v5_lifecycle_ledger import (
        ContactRolloverBoundaryV2,
        ControllerCommitAckV2,
        FigureEightPhysicalRecordV2,
        GateClosureEvidenceV2,
        GateFamiliesV2,
        HomeBoundaryV2,
        LedgerRole,
        LifecycleEventKind,
        LifecycleEventV2,
        MetricSnapshotV1,
        PathTailClosureV2,
        TrialBoundaryReceiptV2,
        TrialSliceV2,
        V5ChainBindingV2,
        V5_EVENT_BUNDLE_SCHEMA,
        bind_v5_chain,
        canonical_sha256,
        index_sealed_r013_artifact,
        lifecycle_row_evidence_sha256,
        persist_gate_observation_artifact,
    )
    from tools.step6_figure8_autotune_v1.v5_live_runtime import (
        V5CandidateRuntimeSpecV1,
        V5ChainControlV1,
        V5ControlBackend,
        V5ControlCommandV1,
        make_v5_runtime_path_reference,
        mature_backend_factory,
    )
    from tools.step6_figure8_autotune_v1.v5_register_transport import (
        V5MatureTransportAdapter,
        V5OutputSnapshotV1,
    )
    from tools.step6_figure8_autotune_v1.v5_rollover import (
        FORMAL_METRIC_START_S,
        PATH_END_S,
        TAIL_END_S,
        CandidateIdentityV1,
    )

try:
    from step5d_autotune_v4_r013.timing_scheduler import (
        FormalTimingSchedulerLeaseV2,
        LATE_CONTROL_FIFO_PROFILE,
        QUOTA_SAFE_OTHER_PROFILE,
        TimingSchedulerProfileV1,
    )
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step5d_autotune_v4_r013.timing_scheduler import (
        FormalTimingSchedulerLeaseV2,
        LATE_CONTROL_FIFO_PROFILE,
        QUOTA_SAFE_OTHER_PROFILE,
        TimingSchedulerProfileV1,
    )


V5_LIVE_OWNER_SCHEMA = "step6.autotune/figure8-v5-live-owner-v1"
V5_LIVE_OWNER_VERSION = 1
V5_LIVE_BINDING_SCHEMA = "step6.autotune/figure8-v5-live-binding-v1"
V5_SEALED_RESULT_SCHEMA = "step6.autotune/figure8-v5-sealed-chain-result-v1"
V5_SEALED_RESULT_VERSION = 1
V5_ACTIVATION_JOURNAL_SCHEMA = "step6.autotune/figure8-v5-rollover-activation-journal-v1"
V5_ACTIVATION_JOURNAL_VERSION = 1
V5_RUNTIME_PROTOCOL = 607007
V5_READABLE_RUNTIME_IDENTITY = (5, 520607)
MAX_CHAIN_ATTEMPTS = 5
MAX_CHAIN_ROLLOVERS = 4
# Leave enough closure-tail time for the durable COMMIT_INTENT append and the
# observed four-frame RTDE input pipeline before the exact analytic seam.  The
# acknowledgement deadline below remains an 8 ms post-seam bound; this lead is
# scheduling slack, not a relaxed acknowledgement or safety threshold.
ROLLOVER_COMMIT_LEAD_S = 0.100
ROLLOVER_COMMIT_ACK_TIMEOUT_S = 0.008
# Filesystem durability is deliberately isolated from the 500 Hz owner loop.
# This is a scheduling bound for the evidence worker, not a motion/safety gate.
ACTIVATION_DURABILITY_TIMEOUT_S = 1.000
ACTIVATION_PENDING_MAX_S = 0.020
ACTIVATION_DURABILITY_IPC_SCHEMA = "step6.autotune/figure8-v5-activation-durability-ipc-v1"
ACTIVATION_DURABILITY_STARTUP_TIMEOUT_S = 2.0
_ZERO_QDOT = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
_PATH_OUTPUT_STATES = frozenset(
    {
        V5TPState.PATH,
        V5TPState.CLOSURE_TAIL,
        V5TPState.ROLLOVER_PREPARED,
    }
)


class V5LiveOwnerError(RuntimeError):
    """The V5 single-writer owner failed closed."""


@dataclass
class V5GCWindowReceiptV1:
    """Typed proof that cyclic GC is quiet across one V5 physical chain."""

    pre_enabled: bool
    entered: bool = False
    restored: bool = False
    post_enabled: bool | None = None
    restored_after_timing_lease: bool = False

    @classmethod
    def capture(cls) -> "V5GCWindowReceiptV1":
        return cls(pre_enabled=gc.isenabled())

    def enter(self) -> None:
        gc.disable()
        self.entered = True

    def restore_after_timing_lease(self) -> None:
        if self.pre_enabled:
            if not gc.isenabled():
                gc.enable()
        elif gc.isenabled():
            gc.disable()
        self.post_enabled = gc.isenabled()
        self.restored = self.post_enabled is self.pre_enabled
        self.restored_after_timing_lease = self.restored
        if not self.restored:
            raise V5LiveOwnerError("V5 cyclic GC state was not restored")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/figure8-v5-gc-window-receipt-v1",
            "version": 1,
            "scope": "ARM_EXECUTE_MOTION_CONTACT_SAFE_RETURN_HOME",
            "pre_enabled": self.pre_enabled,
            "entered": self.entered,
            "restored": self.restored,
            "post_enabled": self.post_enabled,
            "restored_after_timing_lease": self.restored_after_timing_lease,
        }


@dataclass(frozen=True)
class V5ActivationJournalEntryV1:
    """One typed activation transition submitted to the durability worker."""

    state: str
    generation: int
    old_identity: CandidateIdentityV1
    next_identity: CandidateIdentityV1
    details: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.state, str) or not self.state:
            raise TypeError("activation journal entry state must be nonempty")
        if type(self.generation) is not int or self.generation < 1:
            raise TypeError("activation journal entry generation must be positive")
        if not isinstance(self.old_identity, CandidateIdentityV1) or not isinstance(
            self.next_identity, CandidateIdentityV1
        ):
            raise TypeError("activation journal entry identities must be typed")
        if not isinstance(self.details, Mapping):
            raise TypeError("activation journal entry details must be a mapping")
        object.__setattr__(self, "details", dict(self.details))


@dataclass(frozen=True)
class V5ActivationDurableBatchReceiptV1:
    """Cold-verified result returned by the isolated durability worker."""

    generation: int
    states: tuple[str, ...]
    row_sha256s: tuple[str, ...]
    journal_head_sha256: str

    def __post_init__(self) -> None:
        if type(self.generation) is not int or self.generation < 1:
            raise TypeError("activation durable generation must be positive")
        if not self.states or len(self.states) != len(self.row_sha256s):
            raise V5LiveOwnerError("activation durable batch is incomplete")
        for value in (*self.row_sha256s, self.journal_head_sha256):
            _sha(value, "activation durable row")


@dataclass(frozen=True)
class V5ActivationBarrierSettlementV1:
    batch: V5ActivationDurableBatchReceiptV1
    deferred_rollover_input: V5RolloverInput | None


@dataclass
class V5PendingActivationV1:
    """Controller-switched, host-not-yet-published activation state."""

    generation: int
    old_identity: CandidateIdentityV1
    next_spec: V5CandidateRuntimeSpecV1
    commit_seed_qdot: tuple[float, ...]
    seam_sample_index: int
    seam_rtde_timestamp_s: float
    last_path_time_s: float = 0.0
    pending_sample_indices: list[int] | None = None

    def __post_init__(self) -> None:
        if self.pending_sample_indices is None:
            self.pending_sample_indices = []
        if (
            type(self.generation) is not int
            or self.generation < 1
            or not isinstance(self.old_identity, CandidateIdentityV1)
            or not isinstance(self.next_spec, V5CandidateRuntimeSpecV1)
            or self.next_spec.identity.epoch != self.old_identity.epoch
            or self.next_spec.identity.ordinal <= self.old_identity.ordinal
            or type(self.seam_sample_index) is not int
            or self.seam_sample_index < 0
        ):
            raise V5LiveOwnerError("V5 pending activation identity is invalid")
        seed = tuple(float(value) for value in self.commit_seed_qdot)
        if len(seed) != 6 or not all(math.isfinite(value) for value in seed):
            raise V5LiveOwnerError("V5 pending activation seed is invalid")
        self.commit_seed_qdot = seed
        self.seam_rtde_timestamp_s = _finite(
            self.seam_rtde_timestamp_s, "V5 pending seam RTDE timestamp"
        )


def _sha(value: str, role: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise V5LiveOwnerError(f"{role} must be a lowercase SHA-256")
    return value


def _finite(value: Any, role: str) -> float:
    if isinstance(value, bool):
        raise V5LiveOwnerError(f"{role} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V5LiveOwnerError(f"{role} must be numeric") from exc
    if not math.isfinite(result):
        raise V5LiveOwnerError(f"{role} must be finite")
    return result


def candidate_identity_from_plan(plan: V5TrialPlan) -> CandidateIdentityV1:
    if not isinstance(plan, V5TrialPlan):
        raise TypeError("V5 owner plan must be V5TrialPlan")
    return CandidateIdentityV1(
        plan.wire_epoch,
        plan.candidate_ordinal,
        plan.attempt_kind,
        plan.candidate_token,
    )


def runtime_spec_from_plan(plan: V5TrialPlan) -> V5CandidateRuntimeSpecV1:
    """Convert one durable campaign plan to the exact executable runtime DTO."""

    identity = candidate_identity_from_plan(plan)
    controller_payload = {
        **dict(plan.candidate.controller_path),
        "target_force_n": plan.candidate.target_force_n,
    }
    controller = FigureEightPhysicalCandidateV1.from_canonical(controller_payload)
    policy = CorrectionPolicyV1(
        normalization_scales=tuple(CORRECTION_NORMALIZATION_SCALES),
        weights=tuple(plan.candidate.correction_weights),
    )
    correction_fingerprint = canonical_sha256(plan.candidate.correction_block)
    return V5CandidateRuntimeSpecV1(
        identity=identity,
        controller_candidate=controller,
        correction_policy=policy,
        correction_fingerprint_sha256=correction_fingerprint,
    )


class V5PrebuiltBackendCacheV1:
    """Pre-ARM, ordered, single-use backend construction boundary.

    Future entries are opaque and sensor-silent.  The live chain can only take
    the next full runtime spec; cache misses never fall back to construction in
    the 500 Hz control population.
    """

    def __init__(
        self,
        specs: Sequence[V5CandidateRuntimeSpecV1],
        backend_factory: Callable[[V5CandidateRuntimeSpecV1], V5ControlBackend],
    ) -> None:
        values = tuple(specs)
        if (
            not values
            or len(values) > MAX_CHAIN_ATTEMPTS
            or not all(
                isinstance(spec, V5CandidateRuntimeSpecV1) for spec in values
            )
            or not callable(backend_factory)
        ):
            raise V5LiveOwnerError("V5 prebuilt backend cache input differs")
        entries: list[tuple[V5CandidateRuntimeSpecV1, V5ControlBackend]] = []
        for spec in values:
            backend = backend_factory(spec)
            required = (
                "observe_filter",
                "command",
                "seed_physical_continuity",
                "validate_activation_hold",
            )
            if backend is None or any(
                not callable(getattr(backend, name, None)) for name in required
            ):
                raise V5LiveOwnerError(
                    "V5 prebuilt backend does not implement the runtime contract"
                )
            entries.append((spec, backend))
        self._entries = entries
        self._next_index = 0

    @property
    def remaining_count(self) -> int:
        return len(self._entries) - self._next_index

    def take(self, spec: V5CandidateRuntimeSpecV1) -> V5ControlBackend:
        if not isinstance(spec, V5CandidateRuntimeSpecV1):
            raise TypeError("V5 backend cache take requires a typed spec")
        if self._next_index >= len(self._entries):
            raise V5LiveOwnerError("V5 prebuilt backend cache is exhausted")
        expected_spec, backend = self._entries[self._next_index]
        if spec != expected_spec:
            raise V5LiveOwnerError(
                "V5 prebuilt backend cache order/full-spec binding differs"
            )
        self._next_index += 1
        return backend


@dataclass(frozen=True)
class V5LiveChainRequestV1:
    chain_id: str
    plans: tuple[V5TrialPlan, ...]
    campaign_fingerprint: str
    release_identity_sha256: str
    role: LedgerRole
    durable_result_path: str | None = None
    timeout_s: float = 480.0
    schema: str = V5_LIVE_OWNER_SCHEMA
    version: int = V5_LIVE_OWNER_VERSION

    def __post_init__(self) -> None:
        plans = tuple(self.plans)
        if not self.chain_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
            for character in self.chain_id
        ):
            raise V5LiveOwnerError("V5 chain ID is invalid")
        if not 1 <= len(plans) <= MAX_CHAIN_ATTEMPTS:
            raise V5LiveOwnerError("V5 chain must contain one to five plans")
        if not all(isinstance(plan, V5TrialPlan) for plan in plans):
            raise TypeError("V5 chain plans must be typed")
        identities = tuple(candidate_identity_from_plan(plan) for plan in plans)
        if len({plan.trial_id for plan in plans}) != len(plans) or len(
            {plan.attempt_id for plan in plans}
        ) != len(plans):
            raise V5LiveOwnerError("V5 chain plan IDs repeat")
        for previous, current in zip(identities, identities[1:]):
            if (
                current.epoch != previous.epoch
                or current.ordinal <= previous.ordinal
            ):
                raise V5LiveOwnerError("V5 chain candidate identities are not increasing")
        if any(plan.requires_home for plan in plans[:-1]):
            raise V5LiveOwnerError("a Home-only plan cannot be rolled into a successor")
        if len(plans) > 1 and any(not plan.packable for plan in plans):
            raise V5LiveOwnerError("only ordinary novel plans may share a contact chain")
        if len(plans) - 1 > MAX_CHAIN_ROLLOVERS:
            raise V5LiveOwnerError("V5 chain exceeds four rollovers")
        _sha(self.campaign_fingerprint, "chain campaign fingerprint")
        _sha(self.release_identity_sha256, "chain release identity")
        if not isinstance(self.role, LedgerRole):
            raise TypeError("V5 chain role must be typed")
        if not isinstance(self.durable_result_path, str):
            raise V5LiveOwnerError("durable result path must be a resolved string")
        result_path = Path(self.durable_result_path)
        resolved = result_path.resolve()
        if result_path != resolved or result_path.is_symlink() or resolved.parent.is_symlink():
            raise V5LiveOwnerError("durable result path must be resolved and non-symlinked")
        object.__setattr__(self, "durable_result_path", str(resolved))
        timeout = _finite(self.timeout_s, "chain timeout")
        if timeout <= 0.0:
            raise V5LiveOwnerError("V5 chain timeout must be positive")
        if self.schema != V5_LIVE_OWNER_SCHEMA or self.version != V5_LIVE_OWNER_VERSION:
            raise V5LiveOwnerError("V5 chain request schema/version differs")
        object.__setattr__(self, "plans", plans)
        object.__setattr__(self, "timeout_s", timeout)


class V5RolloverPhase(str, Enum):
    NO_SUCCESSOR = "no_successor"
    READY_TO_PREPARE = "ready_to_prepare"
    PREPARE_SENT = "prepare_sent"
    PREPARED = "prepared"
    COMMIT_SENT = "commit_sent"
    COMMITTED = "committed"


@dataclass(frozen=True)
class V5RolloverDecisionV1:
    wire_input: V5RolloverInput
    prepare_runtime: bool = False
    arm_commit: bool = False
    acknowledge_commit: bool = False


class V5RolloverEchoKind(str, Enum):
    CURRENT = "current"
    FIRST_COMMIT_ACK = "first_commit_ack"
    ACTIVATION_PENDING = "activation_pending"
    POST_COMMIT_DRAIN = "post_commit_drain"


@dataclass(frozen=True)
class V5RolloverEchoResolutionV1:
    kind: V5RolloverEchoKind
    expected_identity: CandidateIdentityV1
    expected_generation: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, V5RolloverEchoKind):
            raise TypeError("rollover echo kind must be typed")
        if not isinstance(self.expected_identity, CandidateIdentityV1):
            raise TypeError("rollover echo identity must be typed")
        if self.kind is V5RolloverEchoKind.CURRENT:
            if self.expected_generation is not None:
                raise V5LiveOwnerError(
                    "CURRENT rollover echo resolution must not carry a generation"
                )
        elif type(self.expected_generation) is not int or self.expected_generation <= 0:
            raise V5LiveOwnerError(
                "rollover echo resolution generation must be positive"
            )


class V5RolloverCoordinatorV1:
    """Pure one-step PREPARE/NONE/COMMIT handshake over output29..31."""

    def __init__(
        self,
        current: CandidateIdentityV1,
        successor: CandidateIdentityV1 | None,
        *,
        generation: int,
        commit_from_path_time_s: float = TAIL_END_S - ROLLOVER_COMMIT_LEAD_S,
        commit_ack_timeout_s: float = ROLLOVER_COMMIT_ACK_TIMEOUT_S,
    ) -> None:
        if not isinstance(current, CandidateIdentityV1):
            raise TypeError("rollover current identity must be typed")
        if successor is not None and (
            not isinstance(successor, CandidateIdentityV1)
            or successor.epoch != current.epoch
            or successor.ordinal <= current.ordinal
        ):
            raise V5LiveOwnerError("rollover successor identity is invalid")
        if type(generation) is not int or generation <= 0:
            raise V5LiveOwnerError("rollover generation must be positive")
        commit_time = _finite(commit_from_path_time_s, "COMMIT arm time")
        if not PATH_END_S <= commit_time < TAIL_END_S:
            raise V5LiveOwnerError("COMMIT arm time must be in the closure tail")
        commit_timeout = _finite(commit_ack_timeout_s, "COMMIT acknowledgement timeout")
        if not 0.0 < commit_timeout <= ROLLOVER_COMMIT_ACK_TIMEOUT_S:
            raise V5LiveOwnerError("COMMIT acknowledgement timeout exceeds 8 ms")
        self.current = current
        self.successor = successor
        self.generation = generation
        self.commit_from_path_time_s = commit_time
        self.commit_ack_timeout_s = commit_timeout
        self.commit_sent_monotonic_s: float | None = None
        self.commit_expected_seam_monotonic_s: float | None = None
        self.last_observed_monotonic_s: float | None = None
        self.phase = (
            V5RolloverPhase.NO_SUCCESSOR
            if successor is None
            else V5RolloverPhase.READY_TO_PREPARE
        )

    def resolve_echo(
        self,
        view: V5OutputSnapshotV1,
        *,
        active_identity: CandidateIdentityV1,
        prepared_identity: CandidateIdentityV1 | None,
    ) -> V5RolloverEchoResolutionV1:
        """Resolve the controller identity authority for one fresh output frame."""

        if not isinstance(view, V5OutputSnapshotV1):
            raise TypeError("rollover output view must be typed")
        if not isinstance(active_identity, CandidateIdentityV1):
            raise V5LiveOwnerError("active rollover identity is not typed")
        if active_identity != self.current:
            raise V5LiveOwnerError(
                "active rollover identity differs from coordinator current"
            )
        if prepared_identity is not None and not isinstance(
            prepared_identity, CandidateIdentityV1
        ):
            raise V5LiveOwnerError("prepared rollover identity is not typed")

        def identity_echo_matches(identity: CandidateIdentityV1) -> bool:
            echoes = view.integer_echoes
            return bool(
                echoes.get(24) == identity.epoch
                and echoes.get(25) == identity.ordinal
                and echoes.get(27) == identity.candidate_token
            )

        if view.state is not V5TPState.ROLLOVER_COMMITTED:
            if not identity_echo_matches(self.current):
                raise V5LiveOwnerError(
                    "V5 current identity echo differs from coordinator current"
                )
            return V5RolloverEchoResolutionV1(
                V5RolloverEchoKind.CURRENT,
                self.current,
                None,
            )

        overlay = view.overlay
        if self.phase is V5RolloverPhase.COMMIT_SENT:
            successor = self.successor
            if successor is None or prepared_identity != successor:
                raise V5LiveOwnerError(
                    "V5 COMMIT acknowledgement successor identity differs"
                )
            if not isinstance(overlay, RolloverOutputOverlayV2) or (
                overlay.rollover_ack_generation != self.generation
                or overlay.active_qdot_generation != self.generation
                or overlay.prepared_candidate_token != 0
            ):
                raise V5LiveOwnerError("V5 COMMIT acknowledgement overlay differs")
            if not identity_echo_matches(successor):
                raise V5LiveOwnerError(
                    "V5 COMMIT acknowledgement successor echo differs"
                )
            return V5RolloverEchoResolutionV1(
                V5RolloverEchoKind.FIRST_COMMIT_ACK,
                successor,
                self.generation,
            )

        if self.phase is V5RolloverPhase.COMMITTED and active_identity == self.current:
            successor = self.successor
            if successor is None or prepared_identity != successor:
                raise V5LiveOwnerError(
                    "V5 activation-pending successor identity differs"
                )
            if not isinstance(overlay, RolloverOutputOverlayV2) or (
                overlay.rollover_ack_generation != self.generation
                or overlay.active_qdot_generation != self.generation
                or overlay.prepared_candidate_token != 0
            ):
                raise V5LiveOwnerError(
                    "V5 activation-pending overlay differs"
                )
            if not identity_echo_matches(successor):
                raise V5LiveOwnerError(
                    "V5 activation-pending successor echo differs"
                )
            return V5RolloverEchoResolutionV1(
                V5RolloverEchoKind.ACTIVATION_PENDING,
                successor,
                self.generation,
            )

        prior_generation = self.generation - 1
        if prior_generation <= 0:
            raise V5LiveOwnerError(
                "V5 state28 has no prior activation generation"
            )
        if not isinstance(overlay, RolloverOutputOverlayV2) or (
            overlay.rollover_ack_generation != prior_generation
            or overlay.active_qdot_generation != prior_generation
            or overlay.prepared_candidate_token != 0
        ):
            raise V5LiveOwnerError("V5 post-COMMIT drain overlay differs")
        if not identity_echo_matches(self.current):
            raise V5LiveOwnerError(
                "V5 post-COMMIT drain active identity echo differs"
            )
        return V5RolloverEchoResolutionV1(
            V5RolloverEchoKind.POST_COMMIT_DRAIN,
            self.current,
            prior_generation,
        )

    def _identity_input(self, command: RolloverCommand) -> V5RolloverInput:
        successor = self.successor
        if successor is None:
            return V5RolloverInput()
        return V5RolloverInput(
            command=command,
            generation=self.generation,
            next_attempt_ordinal=successor.ordinal,
            next_attempt_kind=successor.attempt_kind,
            next_candidate_token=successor.candidate_token,
            qdot_generation=(
                self.generation if command is RolloverCommand.COMMIT else 0
            ),
        )

    def observe(
        self,
        view: V5OutputSnapshotV1,
        *,
        path_time_s: float,
        monotonic_s: float,
    ) -> V5RolloverDecisionV1:
        if not isinstance(view, V5OutputSnapshotV1):
            raise TypeError("rollover output view must be typed")
        time_s = _finite(path_time_s, "rollover path time")
        observed_s = _finite(monotonic_s, "rollover monotonic time")
        if not 0.0 <= time_s <= TAIL_END_S:
            raise V5LiveOwnerError("rollover path time is outside the analytic interval")
        if observed_s < 0.0 or (
            self.last_observed_monotonic_s is not None
            and observed_s < self.last_observed_monotonic_s
        ):
            raise V5LiveOwnerError("rollover monotonic time regressed")
        self.last_observed_monotonic_s = observed_s
        if self.phase is V5RolloverPhase.NO_SUCCESSOR:
            return V5RolloverDecisionV1(V5RolloverInput())
        successor = self.successor
        assert successor is not None

        if self.phase is V5RolloverPhase.READY_TO_PREPARE:
            if view.state not in _PATH_OUTPUT_STATES:
                return V5RolloverDecisionV1(V5RolloverInput())
            self.phase = V5RolloverPhase.PREPARE_SENT
            return V5RolloverDecisionV1(
                self._identity_input(RolloverCommand.PREPARE),
                prepare_runtime=True,
            )

        if self.phase is V5RolloverPhase.PREPARE_SENT:
            if view.state is V5TPState.ROLLOVER_REJECTED:
                raise V5LiveOwnerError("controller rejected V5 PREPARE")
            if view.state is V5TPState.ROLLOVER_PREPARED:
                overlay = view.overlay
                if not isinstance(overlay, RolloverOutputOverlayV2) or (
                    overlay.rollover_ack_generation != self.generation
                    or overlay.prepared_candidate_token != successor.candidate_token
                ):
                    raise V5LiveOwnerError("V5 PREPARE acknowledgement differs")
                self.phase = V5RolloverPhase.PREPARED
                return V5RolloverDecisionV1(V5RolloverInput())
            return V5RolloverDecisionV1(
                self._identity_input(RolloverCommand.PREPARE)
            )

        if self.phase is V5RolloverPhase.PREPARED:
            if time_s < self.commit_from_path_time_s:
                return V5RolloverDecisionV1(V5RolloverInput())
            self.phase = V5RolloverPhase.COMMIT_SENT
            self.commit_sent_monotonic_s = observed_s
            self.commit_expected_seam_monotonic_s = (
                observed_s + (TAIL_END_S - time_s)
            )
            return V5RolloverDecisionV1(
                self._identity_input(RolloverCommand.COMMIT),
                arm_commit=True,
            )

        if self.phase is V5RolloverPhase.COMMIT_SENT:
            if (
                self.commit_sent_monotonic_s is None
                or self.commit_expected_seam_monotonic_s is None
            ):
                raise V5LiveOwnerError("V5 COMMIT timing boundary is missing")
            if (
                observed_s
                > self.commit_expected_seam_monotonic_s
                + self.commit_ack_timeout_s
                + 1e-12
            ):
                raise V5LiveOwnerError(
                    "V5 COMMIT acknowledgement deadline exceeded"
                )
            if view.state is V5TPState.ROLLOVER_REJECTED:
                raise V5LiveOwnerError("controller rejected V5 COMMIT")
            if view.state is V5TPState.ROLLOVER_COMMITTED:
                overlay = view.overlay
                if not isinstance(overlay, RolloverOutputOverlayV2) or (
                    overlay.rollover_ack_generation != self.generation
                    or overlay.active_qdot_generation != self.generation
                    or overlay.prepared_candidate_token != 0
                ):
                    raise V5LiveOwnerError("V5 COMMIT acknowledgement differs")
                self.phase = V5RolloverPhase.COMMITTED
                return V5RolloverDecisionV1(
                    V5RolloverInput(), acknowledge_commit=True
                )
            return V5RolloverDecisionV1(
                self._identity_input(RolloverCommand.COMMIT)
            )

        return V5RolloverDecisionV1(V5RolloverInput())


@dataclass(frozen=True)
class V5EventDescriptorV1:
    kind: LifecycleEventKind
    sample_index: int
    identity: CandidateIdentityV1
    event_evidence_sha256: str
    next_identity: CandidateIdentityV1 | None = None
    generation: int = 0
    qdot_generation: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LifecycleEventKind):
            raise TypeError("event descriptor kind must be typed")
        if type(self.sample_index) is not int or self.sample_index < 0:
            raise V5LiveOwnerError("event descriptor sample index is invalid")
        if not isinstance(self.identity, CandidateIdentityV1):
            raise TypeError("event descriptor identity must be typed")
        _sha(self.event_evidence_sha256, "event descriptor evidence")


class V5LifecycleTraceAdapter:
    """Inject effective target evidence into the existing full-rate trace.

    The TP wire intentionally retains its mature base-target range ``[1,5]``.
    Correction is a host-side control term and may make the effective target
    larger than 5 N.  This adapter changes no packet; it only makes the sealed
    lifecycle's ``setpoint_n`` field describe the effective control target.
    """

    def __init__(self, trace: LifecycleTrace) -> None:
        if not isinstance(trace, LifecycleTrace):
            raise TypeError("V5 lifecycle adapter requires LifecycleTrace")
        self.trace = trace
        self.current_effective_target_n = 1.0
        self.descriptors: list[V5EventDescriptorV1] = []
        self._pending_command_monotonic_s: float | None = None
        self._control_start_marked = False

    @property
    def active(self) -> bool:
        return self.trace.active

    @property
    def next_sample_index(self) -> int:
        active = getattr(self.trace, "_active", None)
        if active is None:
            raise V5LiveOwnerError("V5 lifecycle trace is not active")
        return int(active.sample_count)

    def set_effective_target(self, value: float) -> None:
        target = _finite(value, "effective target")
        if not 1.0 <= target <= 6.25:
            raise V5LiveOwnerError("effective target exceeds the V5 control bounds")
        self.current_effective_target_n = target

    def begin_attempt(self, *args: Any, **kwargs: Any) -> None:
        self.descriptors.clear()
        self.current_effective_target_n = 1.0
        self._pending_command_monotonic_s = None
        self._control_start_marked = False
        self.trace.begin_attempt(*args, **kwargs)

    def mark_control_start(
        self,
        *,
        monotonic_s: float,
        output: Any,
        sensor: SensorPacket,
        tp_state: int,
    ) -> int:
        """Seal a V5-only source-row marker and return the first timing index."""

        if self._control_start_marked:
            raise V5LiveOwnerError("V5 CONTROL_START is already marked")
        marker_index = self.next_sample_index
        recorded = self.trace.observe_tick(
            monotonic_s=_finite(monotonic_s, "V5 CONTROL_START clock"),
            output=output,
            sensor=sensor,
            tp_state=int(tp_state),
            command_mode=None,
            qdot=_ZERO_QDOT,
            setpoint_n=self.current_effective_target_n,
            packet_sequence=-1,
            consumed_packet_sequence=-1,
        )
        if not recorded or self.next_sample_index != marker_index + 1:
            raise V5LiveOwnerError("V5 CONTROL_START marker was not recorded")
        self._control_start_marked = True
        return marker_index + 1

    def bind_command_clock(self, monotonic_s: float) -> None:
        if self._pending_command_monotonic_s is not None:
            raise V5LiveOwnerError("V5 lifecycle command clock is already bound")
        self._pending_command_monotonic_s = _finite(
            monotonic_s, "V5 lifecycle command clock"
        )

    def clear_command_clock(self) -> None:
        self._pending_command_monotonic_s = None

    def observe_tick(self, *args: Any, **kwargs: Any) -> bool:
        values = dict(kwargs)
        if self._pending_command_monotonic_s is not None:
            values["monotonic_s"] = self._pending_command_monotonic_s
            self._pending_command_monotonic_s = None
        values["setpoint_n"] = self.current_effective_target_n
        return self.trace.observe_tick(*args, **values)

    def observe_terminal(self, *args: Any, **kwargs: Any) -> bool:
        if args:
            raise TypeError("V5 terminal lifecycle observation is keyword-only")
        values = dict(kwargs)
        active = getattr(self.trace, "_active", None)
        if active is None:
            return False
        monotonic_s = values.pop("monotonic_s", None)
        mono = self.trace.clock() if monotonic_s is None else monotonic_s
        try:
            tp_state = int(values.pop("tp_state"))
        except (KeyError, TypeError, ValueError) as exc:
            raise V5LiveOwnerError("V5 terminal TP state is invalid") from exc
        recorded = self.trace.observe_tick(
            monotonic_s=float(mono),
            output=values.pop("output", None),
            sensor=values.pop("sensor", None),
            tp_state=tp_state,
            command_mode=values.pop("command_mode", None),
            packet_sequence=values.pop("packet_sequence", -1),
            consumed_packet_sequence=values.pop("consumed_packet_sequence", -1),
            setpoint_n=self.current_effective_target_n,
            terminal=True,
        )
        if values:
            raise TypeError(
                "unexpected V5 terminal lifecycle fields: "
                + ",".join(sorted(values))
            )
        active.terminal_sensor_present = kwargs.get("sensor") is not None
        active.terminal_state = tp_state
        active.home_verified = tp_state == int(V5TPState.READY_HOME_NEXT)
        return recorded

    def mark_observer_error(self, error: Any) -> None:
        self.trace.mark_observer_error(error)

    def finalize_attempt(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.trace.finalize_attempt(*args, **kwargs)


@dataclass(frozen=True)
class V5SealedChainResultV1:
    chain: V5ChainBindingV2
    records: tuple[FigureEightPhysicalRecordV2, ...]
    lifecycle_receipt: Mapping[str, Any]
    event_bundle: Mapping[str, Any]
    home_evidence_sha256: str
    activation_receipt: Mapping[str, Any] | None = None
    durable_result_path: str | None = None
    durable_result_sha256: str | None = None
    schema: str = V5_LIVE_OWNER_SCHEMA
    version: int = V5_LIVE_OWNER_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.chain, V5ChainBindingV2) or not self.records:
            raise V5LiveOwnerError("sealed chain result is incomplete")
        if len(self.records) != len(self.chain.attempts):
            raise V5LiveOwnerError("sealed chain result cardinality differs")
        _sha(self.home_evidence_sha256, "sealed Home evidence")
        if self.activation_receipt is not None and not isinstance(self.activation_receipt, Mapping):
            raise V5LiveOwnerError("activation receipt is not a mapping")
        if (self.durable_result_path is None) != (self.durable_result_sha256 is None):
            raise V5LiveOwnerError("durable result path/hash must appear together")
        if self.durable_result_path is not None:
            path = Path(self.durable_result_path)
            if path != path.resolve() or path.is_symlink():
                raise V5LiveOwnerError("durable result path is not resolved")
            _sha(self.durable_result_sha256 or "", "durable result file hash")
        if self.schema != V5_LIVE_OWNER_SCHEMA or self.version != V5_LIVE_OWNER_VERSION:
            raise V5LiveOwnerError("sealed chain result schema/version differs")


def _request_binding(request: V5LiveChainRequestV1) -> dict[str, Any]:
    return {
        "chain_id": request.chain_id,
        "plans": [plan.as_dict() for plan in request.plans],
        "durable_result_path": request.durable_result_path,
    }


def _activation_journal_path(request: V5LiveChainRequestV1) -> Path:
    target = Path(request.durable_result_path or "")
    return target.with_name(f"{target.stem}.activation.jsonl")


class V5RolloverActivationJournalV1:
    """Durable physical authority for every rollover seam transition."""

    _STATES = ("PREPARED", "COMMIT_INTENT", "COMMIT_ACK", "ACTIVATED")

    def __init__(self, request: V5LiveChainRequestV1) -> None:
        if not isinstance(request, V5LiveChainRequestV1):
            raise TypeError("activation journal requires a typed request")
        self.request = request
        self.path = _activation_journal_path(request).resolve()
        if self.path.is_symlink():
            raise V5LiveOwnerError("activation journal must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("x", encoding="utf-8") as stream:
                stream.write(json.dumps(self._header(), sort_keys=True, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        self._head_sha256 = "0" * 64
        self._states: dict[int, tuple[str, ...]] = {}
        self._journal_sha256 = "0" * 64
        self._cold_rows: tuple[Mapping[str, Any], ...] = ()
        self._last_stat: tuple[int, int, int] | None = None
        self.cold_verify()

    def _header(self) -> dict[str, Any]:
        return {
            "schema": V5_ACTIVATION_JOURNAL_SCHEMA,
            "version": V5_ACTIVATION_JOURNAL_VERSION,
            "record_type": "header",
            "chain_id": self.request.chain_id,
            "campaign_fingerprint": self.request.campaign_fingerprint,
            "release_identity_sha256": self.request.release_identity_sha256,
            "role": self.request.role.value,
            "request_sha256": canonical_sha256(_request_binding(self.request)),
            "genesis_sha256": "0" * 64,
        }

    def cold_verify(self) -> str:
        try:
            raw_bytes = self.path.read_bytes()
            rows = [
                json.loads(line)
                for line in raw_bytes.decode("utf-8").splitlines()
            ]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise V5LiveOwnerError("activation journal is unreadable") from exc
        if not rows or rows[0] != self._header():
            raise V5LiveOwnerError("activation journal header differs")
        previous = "0" * 64
        states: dict[int, list[str]] = {}
        identities = tuple(candidate_identity_from_plan(plan) for plan in self.request.plans)
        for row in rows[1:]:
            unsigned = {key: value for key, value in row.items() if key != "row_sha256"}
            if (
                row.get("schema") != V5_ACTIVATION_JOURNAL_SCHEMA
                or row.get("version") != V5_ACTIVATION_JOURNAL_VERSION
                or row.get("record_type") != "activation"
                or row.get("chain_id") != self.request.chain_id
                or row.get("campaign_fingerprint") != self.request.campaign_fingerprint
                or row.get("release_identity_sha256") != self.request.release_identity_sha256
                or row.get("role") != self.request.role.value
                or row.get("previous_sha256") != previous
                or row.get("row_sha256") != canonical_sha256(unsigned)
            ):
                raise V5LiveOwnerError("activation journal hash/namespace differs")
            generation = row.get("generation")
            state = row.get("state")
            if type(generation) is not int or not 1 <= generation < len(identities):
                raise V5LiveOwnerError("activation journal generation is invalid")
            history = states.setdefault(generation, [])
            if len(history) >= len(self._STATES) or state != self._STATES[len(history)]:
                raise V5LiveOwnerError("activation journal transition order differs")
            if generation > 1 and states.get(generation - 1) != list(self._STATES):
                raise V5LiveOwnerError("activation journal generations overlap")
            if (
                row.get("old_identity") != identities[generation - 1].as_dict()
                or row.get("next_identity") != identities[generation].as_dict()
                or not isinstance(row.get("details"), Mapping)
                or row.get("details_sha256") != canonical_sha256(row["details"])
            ):
                raise V5LiveOwnerError("activation journal identity/details differ")
            history.append(state)
            previous = row["row_sha256"]
        self._head_sha256 = previous
        self._states = {generation: tuple(history) for generation, history in states.items()}
        self._journal_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        self._cold_rows = tuple(dict(row) for row in rows[1:])
        stat = self.path.stat()
        self._last_stat = (int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))
        return previous

    def _ensure_hot_state(self) -> int:
        """Check external modification cheaply; full verify remains at seal."""

        stat = self.path.stat()
        signature = (int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))
        if self._last_stat != signature:
            self.cold_verify()
            stat = self.path.stat()
            signature = (int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))
        if self._last_stat != signature:
            raise V5LiveOwnerError("activation journal stat changed during append")
        return int(stat.st_size)

    def append(
        self,
        state: str,
        *,
        generation: int,
        old_identity: CandidateIdentityV1,
        next_identity: CandidateIdentityV1,
        details: Mapping[str, Any],
    ) -> None:
        self.append_batch(
            (
                V5ActivationJournalEntryV1(
                    state,
                    generation,
                    old_identity,
                    next_identity,
                    details,
                ),
            )
        )

    def append_batch(
        self,
        entries: Sequence[V5ActivationJournalEntryV1],
    ) -> V5ActivationDurableBatchReceiptV1:
        """Validate and durably append one ordered batch with one fsync."""

        values = tuple(entries)
        if not values or any(
            not isinstance(entry, V5ActivationJournalEntryV1)
            for entry in values
        ):
            raise TypeError("activation journal batch must contain typed entries")
        old_size = self._ensure_hot_state()
        identities = tuple(
            candidate_identity_from_plan(plan) for plan in self.request.plans
        )
        states = {
            generation: list(history)
            for generation, history in self._states.items()
        }
        previous = self._head_sha256
        rows: list[dict[str, Any]] = []
        for entry in values:
            generation = entry.generation
            if (
                not 1 <= generation < len(identities)
                or entry.old_identity != identities[generation - 1]
                or entry.next_identity != identities[generation]
                or (
                    generation > 1
                    and tuple(states.get(generation - 1, ())) != self._STATES
                )
            ):
                raise V5LiveOwnerError(
                    "activation append identity/generation differs"
                )
            history = states.setdefault(generation, [])
            expected = (
                self._STATES[len(history)]
                if len(history) < len(self._STATES)
                else None
            )
            if entry.state != expected:
                raise V5LiveOwnerError("activation append transition differs")
            details_value = dict(entry.details)
            row = {
                "schema": V5_ACTIVATION_JOURNAL_SCHEMA,
                "version": V5_ACTIVATION_JOURNAL_VERSION,
                "record_type": "activation",
                "chain_id": self.request.chain_id,
                "campaign_fingerprint": self.request.campaign_fingerprint,
                "release_identity_sha256": self.request.release_identity_sha256,
                "role": self.request.role.value,
                "generation": generation,
                "state": entry.state,
                "old_identity": entry.old_identity.as_dict(),
                "next_identity": entry.next_identity.as_dict(),
                "details": details_value,
                "details_sha256": canonical_sha256(details_value),
                "previous_sha256": previous,
            }
            row["row_sha256"] = canonical_sha256(row)
            rows.append(row)
            history.append(entry.state)
            previous = row["row_sha256"]
        with self.path.open("a", encoding="utf-8") as stream:
            stream.writelines(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in rows
            )
            stream.flush()
            os.fsync(stream.fileno())
        with self.path.open("rb") as stream:
            stream.seek(old_size)
            appended = stream.read()
        expected_bytes = b"".join(
            (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            for row in rows
        )
        if appended != expected_bytes or previous != rows[-1]["row_sha256"]:
            raise V5LiveOwnerError("activation journal appended rows differ")
        self._head_sha256 = previous
        self._states = {generation: tuple(history) for generation, history in states.items()}
        self._cold_rows = (*self._cold_rows, *rows)
        # The append path intentionally does not re-hash the complete journal.
        # ``receipt()``/final seal performs the one required cold verification;
        # the worker response is bound by the appended row hashes and head.
        stat = self.path.stat()
        self._last_stat = (int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))
        if self._head_sha256 != previous:
            raise V5LiveOwnerError("activation journal batch head differs")
        return V5ActivationDurableBatchReceiptV1(
            generation=values[0].generation,
            states=tuple(entry.state for entry in values),
            row_sha256s=tuple(row["row_sha256"] for row in rows),
            journal_head_sha256=self._head_sha256,
        )

    def receipt(self, *, require_complete: bool) -> dict[str, Any]:
        self.cold_verify()
        complete = all(
            self._states.get(generation) == self._STATES
            for generation in range(1, len(self.request.plans))
        )
        if require_complete and not complete:
            raise V5LiveOwnerError("activation journal is not complete")
        return {
            "schema": "step6.autotune/figure8-v5-rollover-activation-receipt-v1",
            "version": 1,
            "journal_path": str(self.path),
            "journal_sha256": self._journal_sha256,
            "journal_head_sha256": self._head_sha256,
            "chain_id": self.request.chain_id,
            "request_sha256": canonical_sha256(_request_binding(self.request)),
            "rollover_count": len(self.request.plans) - 1,
            "activation_state_count": sum(len(value) for value in self._states.values()),
            "complete": complete,
        }

    @classmethod
    def verify_receipt(
        cls,
        request: V5LiveChainRequestV1,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        cold, _rows = cls.verify_receipt_with_rows(request, receipt)
        return cold

    @classmethod
    def verify_receipt_with_rows(
        cls,
        request: V5LiveChainRequestV1,
        receipt: Mapping[str, Any],
    ) -> tuple[dict[str, Any], tuple[Mapping[str, Any], ...]]:
        if not isinstance(receipt, Mapping) or receipt.get("journal_path") != str(_activation_journal_path(request).resolve()):
            raise V5LiveOwnerError("activation receipt path differs")
        journal = cls(request)
        cold = journal.receipt(require_complete=True)
        base = {key: receipt.get(key) for key in cold}
        if base != cold:
            raise V5LiveOwnerError("activation receipt differs from cold journal")
        rows = tuple(dict(row) for row in journal._cold_rows)
        if set(receipt) == set(cold):
            return cold, rows
        publications = receipt.get("publication_receipts")
        if (
            not isinstance(publications, Sequence)
            or isinstance(publications, (str, bytes))
            or len(publications) != len(request.plans) - 1
            or receipt.get("publication_receipts_sha256")
            != canonical_sha256(list(publications))
            or set(receipt)
            != {*cold, "publication_receipts", "publication_receipts_sha256"}
        ):
            raise V5LiveOwnerError(
                "activation publication receipt binding differs"
            )
        identities = tuple(
            candidate_identity_from_plan(plan) for plan in request.plans
        )
        for generation, raw in enumerate(publications, start=1):
            if not isinstance(raw, Mapping):
                raise V5LiveOwnerError(
                    "activation publication receipt is not typed"
                )
            publication = dict(raw)
            durable_rows = tuple(
                row
                for row in rows
                if row.get("generation") == generation
                and row.get("state") in {"COMMIT_ACK", "ACTIVATED"}
            )
            pending_samples = publication.get("pending_sample_indices")
            if (
                publication.get("schema")
                != "step6.autotune/figure8-v5-activation-publication-v1"
                or publication.get("version") != 1
                or publication.get("generation") != generation
                or publication.get("old_identity")
                != identities[generation - 1].as_dict()
                or publication.get("next_identity")
                != identities[generation].as_dict()
                or publication.get("durable_states")
                != ["COMMIT_ACK", "ACTIVATED"]
                or len(durable_rows) != 2
                or publication.get("durable_row_sha256s")
                != [row["row_sha256"] for row in durable_rows]
                or publication.get("durable_journal_head_sha256")
                != durable_rows[-1]["row_sha256"]
                or not isinstance(pending_samples, Sequence)
                or isinstance(pending_samples, (str, bytes))
                or not pending_samples
                or any(type(value) is not int for value in pending_samples)
                or any(
                    current <= previous
                    for previous, current in zip(
                        pending_samples, pending_samples[1:]
                    )
                )
                or publication.get("seam_sample_index") != pending_samples[0]
                or type(publication.get("first_successor_sample_index")) is not int
                or publication["first_successor_sample_index"]
                <= pending_samples[-1]
                or publication.get("first_successor_rollover_command")
                != int(RolloverCommand.NONE)
                or publication.get("first_successor_host_identity")
                != identities[generation].as_dict()
                or _finite(
                    publication.get("durability_settled_monotonic_s"),
                    "activation durability settled clock",
                )
                >= _finite(
                    publication.get("first_successor_publish_monotonic_s"),
                    "activation successor publish clock",
                )
            ):
                raise V5LiveOwnerError(
                    "activation publication receipt content differs"
                )
        return cold, rows

def _activation_durability_process_main(
    connection: Any,
    request: V5LiveChainRequestV1,
) -> None:
    """Own journal fsync/cold-verify outside the live owner's GIL."""

    worker_pid = os.getpid()
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        worker_nice = os.getpriority(os.PRIO_PROCESS, 0)
    except (AttributeError, OSError):
        worker_nice = 0
    try:
        journal = V5RolloverActivationJournalV1(request)
        connection.send(
            {
                "schema": ACTIVATION_DURABILITY_IPC_SCHEMA,
                "kind": "ready",
                "worker_pid": worker_pid,
                "worker_nice": worker_nice,
                "request_sha256": canonical_sha256(_request_binding(request)),
                "journal_head_sha256": journal._head_sha256,
            }
        )
    except BaseException as exc:
        try:
            connection.send(
                {
                    "schema": ACTIVATION_DURABILITY_IPC_SCHEMA,
                    "kind": "ready",
                    "worker_pid": worker_pid,
                    "worker_nice": worker_nice,
                    "request_sha256": canonical_sha256(_request_binding(request)),
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc) or repr(exc),
                }
            )
        finally:
            connection.close()
        return
    try:
        while True:
            try:
                message = connection.recv()
            except EOFError:
                return
            if message is None:
                return
            if (
                not isinstance(message, Mapping)
                or message.get("schema") != ACTIVATION_DURABILITY_IPC_SCHEMA
                or message.get("kind") != "append"
                or type(message.get("sequence")) is not int
                or not isinstance(message.get("entries"), tuple)
            ):
                return
            sequence = int(message["sequence"])
            try:
                receipt = journal.append_batch(message["entries"])
                response = {
                    "schema": ACTIVATION_DURABILITY_IPC_SCHEMA,
                    "kind": "result",
                    "sequence": sequence,
                    "worker_pid": worker_pid,
                    "receipt": receipt,
                }
            except BaseException as exc:
                response = {
                    "schema": ACTIVATION_DURABILITY_IPC_SCHEMA,
                    "kind": "result",
                    "sequence": sequence,
                    "worker_pid": worker_pid,
                    "error_type": type(exc).__name__,
                    "error_detail": str(exc) or repr(exc),
                }
            connection.send(response)
    finally:
        connection.close()


class V5ActivationDurabilityBarrierV1:
    """Prewarmed spawn-process barrier for activation-journal durability."""

    def __init__(
        self,
        journal: V5RolloverActivationJournalV1,
        *,
        timeout_s: float = ACTIVATION_DURABILITY_TIMEOUT_S,
    ) -> None:
        if not isinstance(journal, V5RolloverActivationJournalV1):
            raise TypeError("activation durability barrier requires a journal")
        timeout = _finite(timeout_s, "activation durability timeout")
        if not 0.0 < timeout <= 1.0:
            raise V5LiveOwnerError(
                "activation durability timeout is outside (0,1s]"
            )
        self.journal = journal
        self.timeout_s = timeout
        self._closed = False
        self._submitted_at_s: float | None = None
        self._deferred_rollover_input: V5RolloverInput | None = None
        self._pending_sequence: int | None = None
        self._request_sequence = 0
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        self._connection = parent
        self._process = context.Process(
            target=_activation_durability_process_main,
            args=(child, journal.request),
            name="v5-activation-durability",
            daemon=True,
        )
        self._process.start()
        child.close()
        if not parent.poll(ACTIVATION_DURABILITY_STARTUP_TIMEOUT_S):
            self.close()
            raise V5LiveOwnerError(
                "activation durability worker did not become ready"
            )
        ready = parent.recv()
        expected_request = canonical_sha256(_request_binding(journal.request))
        if (
            not isinstance(ready, Mapping)
            or ready.get("schema") != ACTIVATION_DURABILITY_IPC_SCHEMA
            or ready.get("kind") != "ready"
            or ready.get("request_sha256") != expected_request
            or type(ready.get("worker_pid")) is not int
            or ready.get("worker_pid") <= 0
            or type(ready.get("worker_nice")) is not int
            or not 0 <= ready.get("worker_nice") <= 19
            or ready.get("error_type") is not None
        ):
            detail = "worker readiness response differs"
            if isinstance(ready, Mapping) and ready.get("error_detail"):
                detail = f"{ready.get('error_type')}: {ready.get('error_detail')}"
            self.close()
            raise V5LiveOwnerError(detail)
        self.worker_pid = int(ready["worker_pid"])
        self.worker_nice = int(ready["worker_nice"])

    @property
    def pending(self) -> bool:
        return self._pending_sequence is not None

    def submit(
        self,
        entries: Sequence[V5ActivationJournalEntryV1],
        *,
        deferred_rollover_input: V5RolloverInput | None = None,
    ) -> None:
        if self._closed or self.pending:
            raise V5LiveOwnerError(
                "activation durability barrier already has pending work"
            )
        values = tuple(entries)
        if not values or any(
            not isinstance(entry, V5ActivationJournalEntryV1) for entry in values
        ):
            raise TypeError("activation durability batch must be typed")
        if deferred_rollover_input is not None and not isinstance(
            deferred_rollover_input, V5RolloverInput
        ):
            raise TypeError("deferred rollover input must be typed")
        self._request_sequence += 1
        self._pending_sequence = self._request_sequence
        self._deferred_rollover_input = deferred_rollover_input
        self._submitted_at_s = time.monotonic()
        try:
            self._connection.send(
                {
                    "schema": ACTIVATION_DURABILITY_IPC_SCHEMA,
                    "kind": "append",
                    "sequence": self._pending_sequence,
                    "entries": values,
                }
            )
        except (BrokenPipeError, EOFError, OSError) as exc:
            self._pending_sequence = None
            self._submitted_at_s = None
            self._deferred_rollover_input = None
            raise V5LiveOwnerError(
                "activation durability worker failed during submit"
            ) from exc

    def settle_if_ready(self) -> V5ActivationBarrierSettlementV1 | None:
        sequence = self._pending_sequence
        if sequence is None:
            return None
        submitted_at = self._submitted_at_s
        assert submitted_at is not None
        if time.monotonic() - submitted_at >= self.timeout_s:
            raise V5LiveOwnerError(
                "activation durability worker exceeded its bounded timeout"
            )
        if not self._connection.poll(0.0):
            if not self._process.is_alive():
                raise V5LiveOwnerError(
                    "activation durability worker failed: process exited"
                )
            return None
        try:
            response = self._connection.recv()
        except (EOFError, OSError) as exc:
            raise V5LiveOwnerError(
                "activation durability worker failed while receiving"
            ) from exc
        if (
            not isinstance(response, Mapping)
            or response.get("schema") != ACTIVATION_DURABILITY_IPC_SCHEMA
            or response.get("kind") != "result"
            or response.get("sequence") != sequence
        ):
            raise V5LiveOwnerError("activation durability worker response differs")
        self._pending_sequence = None
        self._submitted_at_s = None
        deferred = self._deferred_rollover_input
        self._deferred_rollover_input = None
        if response.get("error_type"):
            raise V5LiveOwnerError(
                "activation durability worker failed: "
                f"{response.get('error_type')}: {response.get('error_detail')}"
            )
        batch = response.get("receipt")
        if not isinstance(batch, V5ActivationDurableBatchReceiptV1):
            raise V5LiveOwnerError(
                "activation durability worker returned an untyped receipt"
            )
        return V5ActivationBarrierSettlementV1(batch, deferred)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._process.is_alive():
                try:
                    self._connection.send(None)
                except (BrokenPipeError, EOFError, OSError):
                    pass
                self._process.join(timeout=1.0)
                if self._process.is_alive():
                    self._process.terminate()
                    self._process.join(timeout=1.0)
        finally:
            self._connection.close()

def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                dict(value),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V5LiveOwnerError("sealed result is not canonical JSON") from exc


def _sealed_result_payload(
    request: V5LiveChainRequestV1,
    result: V5SealedChainResultV1,
) -> dict[str, Any]:
    if result.chain.chain_id != request.chain_id:
        raise V5LiveOwnerError("sealed result chain differs from request")
    if result.activation_receipt is None:
        raise V5LiveOwnerError("sealed result lacks activation receipt")
    V5RolloverActivationJournalV1.verify_receipt(
        request,
        result.activation_receipt,
    )
    request_value = _request_binding(request)
    body = {
        "schema": V5_SEALED_RESULT_SCHEMA,
        "version": V5_SEALED_RESULT_VERSION,
        "record_type": "sealed_chain_result",
        "campaign_fingerprint": request.campaign_fingerprint,
        "release_identity_sha256": request.release_identity_sha256,
        "role": request.role.value,
        "chain_id": request.chain_id,
        "dispatch_request_sha256": canonical_sha256(request_value),
        "durable_result_path": request.durable_result_path,
        "plans": [plan.as_dict() for plan in request.plans],
        "records": [record.as_dict() for record in result.records],
        "lifecycle_receipt": dict(result.lifecycle_receipt),
        "event_bundle": dict(result.event_bundle),
        "event_bundle_sha256": canonical_sha256(dict(result.event_bundle)),
        "home_evidence_sha256": result.home_evidence_sha256,
        "activation_receipt": None if result.activation_receipt is None else dict(result.activation_receipt),
    }
    body["result_sha256"] = canonical_sha256(body)
    return body


def persist_sealed_chain_result(
    request: V5LiveChainRequestV1,
    result: V5SealedChainResultV1,
) -> V5SealedChainResultV1:
    """Persist the complete sealed result before returning control to the runner."""

    if request.durable_result_path is None:
        return result
    target = Path(request.durable_result_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _canonical_json_bytes(_sealed_result_payload(request, result))
    if target.exists():
        if not target.is_file() or target.is_symlink() or target.read_bytes() != payload:
            raise V5LiveOwnerError("durable sealed result already exists with different bytes")
    else:
        temporary = target.with_name(
            f".{target.name}.tmp-{os.getpid()}-{time.time_ns()}"
        )
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(
                target.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()
    return replace(
        result,
        durable_result_path=str(target),
        durable_result_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _event_from_descriptor(
    descriptor: V5EventDescriptorV1,
    rows: Sequence[Mapping[str, Any]],
) -> LifecycleEventV2:
    try:
        row = rows[descriptor.sample_index]
    except IndexError as exc:
        raise V5LiveOwnerError("event descriptor is outside lifecycle rows") from exc
    return LifecycleEventV2(
        kind=descriptor.kind,
        sample_index=descriptor.sample_index,
        monotonic_s=float(row["monotonic_s"]),
        identity=descriptor.identity,
        rtde_timestamp_s=float(row["rtde_timestamp_s"]),
        next_identity=descriptor.next_identity,
        generation=descriptor.generation,
        qdot_generation=descriptor.qdot_generation,
        terminal_state=(
            V5TPState.READY_HOME_NEXT
            if descriptor.kind is LifecycleEventKind.HOME
            else None
        ),
        event_evidence_sha256=descriptor.event_evidence_sha256,
        referenced_row_sha256=lifecycle_row_evidence_sha256(row),
    )


def seal_v5_chain(
    request: V5LiveChainRequestV1,
    *,
    lifecycle_receipt: Mapping[str, Any],
    descriptors: Sequence[V5EventDescriptorV1],
    gate_observations: Sequence[Mapping[str, Any]],
    timing_sample_indices: Sequence[int],
    control_start_sample_index: int,
    runtime_timing_acceptance: Mapping[str, Any],
    switch_gate_receipts: Sequence[Mapping[str, Any]],
    activation_receipt: Mapping[str, Any],
    optional_telemetry: Sequence[Mapping[str, Any]] | None = None,
) -> V5SealedChainResultV1:
    """Cold-index one Home-terminated lifecycle and derive all attempt slices."""

    if not isinstance(request, V5LiveChainRequestV1):
        raise TypeError("V5 sealer request must be typed")
    descriptor_tuple = tuple(descriptors)
    switch_receipt_tuple = tuple(dict(item) for item in switch_gate_receipts)
    artifact_path_value = lifecycle_receipt.get("artifact_path")
    if not isinstance(artifact_path_value, str) or not artifact_path_value:
        raise V5LiveOwnerError("lifecycle receipt lacks artifact path")
    artifact_path = Path(artifact_path_value).resolve()

    # First cold-load without V5 events only to obtain immutable row evidence.
    try:
        from step5d_autotune_v4_r013.lifecycle_trace import load_lifecycle_artifact
    except ModuleNotFoundError:  # pragma: no cover
        from tools.step5d_autotune_v4_r013.lifecycle_trace import load_lifecycle_artifact
    _metadata, raw_rows = load_lifecycle_artifact(artifact_path)
    rows = tuple(raw_rows)
    events = tuple(_event_from_descriptor(item, rows) for item in descriptor_tuple)
    start_events = [event for event in events if event.kind is LifecycleEventKind.CHAIN_START]
    rollover_events = [
        event for event in events if event.kind is LifecycleEventKind.CONTACT_ROLLOVER
    ]
    home_events = [event for event in events if event.kind is LifecycleEventKind.HOME]
    if (
        len(start_events) != 1
        or len(rollover_events) != len(request.plans) - 1
        or len(home_events) != 1
    ):
        raise V5LiveOwnerError("V5 event counts differ from chain plans")
    event_mappings = [event.as_dict() for event in events]
    sidecar_target = (
        Path(request.durable_result_path).with_name(
            Path(request.durable_result_path).stem + ".gate-observations.json"
        )
        if request.durable_result_path is not None
        else artifact_path.with_name(
            artifact_path.name + ".v5-gate-observations.json"
        )
    )
    gate_observation_binding = persist_gate_observation_artifact(
        sidecar_target,
        source_artifact_path=artifact_path,
        observations=gate_observations,
        timing_sample_indices=timing_sample_indices,
        control_start_sample_index=control_start_sample_index,
        control_end_sample_index=timing_sample_indices[-1],
        runtime_timing_acceptance=runtime_timing_acceptance,
    )
    event_bundle = {
        "schema": V5_EVENT_BUNDLE_SCHEMA,
        "version": 2,
        "cold_verified": True,
        "events_sha256": canonical_sha256(event_mappings),
        "entry_evidence_sha256": start_events[0].event_evidence_sha256,
        "events": event_mappings,
        "switch_gate_receipts": list(switch_receipt_tuple),
        "gate_observation_artifact": gate_observation_binding,
    }
    artifact = index_sealed_r013_artifact(
        artifact_path,
        lifecycle_receipt,
        event_bundle,
    )

    starts = [start_events[0].sample_index, *[item.sample_index for item in rollover_events]]
    home = home_events[0]
    attempts: list[TrialSliceV2] = []
    for index, plan in enumerate(request.plans):
        identity = candidate_identity_from_plan(plan)
        start = starts[index]
        if index < len(request.plans) - 1:
            seam_event = rollover_events[index]
            end = seam_event.sample_index
            seam = artifact.rows[end]
            ack = ControllerCommitAckV2(
                candidate_identity_from_plan(request.plans[index + 1]),
                seam_event.generation,
                seam_event.qdot_generation,
                end,
                float(seam["monotonic_s"]),
                float(seam["rtde_timestamp_s"]),
            )
            boundary = TrialBoundaryReceiptV2(
                ContactRolloverBoundaryV2(
                    identity,
                    candidate_identity_from_plan(request.plans[index + 1]),
                    seam_event.generation,
                    seam_event.qdot_generation,
                    TAIL_END_S,
                    seam_event.event_evidence_sha256,
                    ack,
                    end,
                    float(seam["monotonic_s"]),
                    float(seam["rtde_timestamp_s"]),
                )
            )
            clock_end = float(seam["monotonic_s"])
        else:
            end = home.sample_index + 1
            home_row = artifact.rows[home.sample_index]
            boundary = TrialBoundaryReceiptV2(
                HomeBoundaryV2(
                    V5TPState.READY_HOME_NEXT,
                    home.sample_index,
                    float(home_row["monotonic_s"]),
                    home.event_evidence_sha256,
                    float(home_row["rtde_timestamp_s"]),
                )
            )
            clock_end = float(home_row["monotonic_s"])
        clock_start = float(artifact.rows[start]["monotonic_s"])
        metric = MetricSnapshotV1.from_artifact_slice(
            artifact,
            candidate_identity=identity,
            sample_start_index=start,
            sample_end_index=end,
            path_clock_start_s=clock_start,
        )
        attempts.append(
            TrialSliceV2(
                plan.attempt_id,
                plan.trial_id,
                identity,
                start,
                end,
                clock_start,
                clock_end,
                metric,
                boundary,
            )
        )
    chain = bind_v5_chain(
        artifact,
        attempts,
        campaign_fingerprint=request.campaign_fingerprint,
        release_identity_sha256=request.release_identity_sha256,
        role=request.role,
        chain_id=request.chain_id,
    )
    closures = tuple(
        PathTailClosureV2(evidence.path, evidence.tail, evidence)
        for evidence in (
            GateClosureEvidenceV2.from_artifact(artifact, attempt)
            for attempt in chain.attempts
        )
    )
    telemetry = (
        tuple({} for _ in request.plans)
        if optional_telemetry is None
        else tuple(optional_telemetry)
    )
    if len(telemetry) != len(request.plans):
        raise V5LiveOwnerError("V5 telemetry count differs from chain plans")
    records = tuple(
        FigureEightPhysicalRecordV2.from_attempt(
            chain,
            attempt,
            closure,
            optional_telemetry=telemetry[index],
        )
        for index, (attempt, closure) in enumerate(zip(chain.attempts, closures, strict=True))
    )
    if any(not record.eligible for record in records):
        raise V5LiveOwnerError(
            "V5 cold gate-observation closure is not eligible"
        )
    return V5SealedChainResultV1(
        chain=chain,
        records=records,
        lifecycle_receipt=dict(lifecycle_receipt),
        event_bundle=event_bundle,
        home_evidence_sha256=home.event_evidence_sha256,
        activation_receipt=dict(activation_receipt),
    )


_GATE_NAMES = (
    "safety",
    "timing",
    "freshness",
    "tube_cbf",
    "identity",
    "command_envelope",
)


class _GateClosureAccumulator:
    """Hash-chain the observed gate inputs and aggregate failures per slice."""

    def __init__(self, attempt_count: int) -> None:
        self._summaries = [
            {
                phase: {
                    "observed_count": 0,
                    "failure_counts": {name: 0 for name in _GATE_NAMES},
                    "observation_chain_sha256": "0" * 64,
                }
                for phase in ("path", "tail")
            }
            for _ in range(attempt_count)
        ]
        self._observations: list[dict[str, Any]] = []
        self._timing_sample_indices: list[int] = []

    @staticmethod
    def _guard_passed(command: V5ControlCommandV1) -> bool:
        guard = command.gate_receipt.get("guard_stack")
        if not isinstance(guard, Mapping):
            return False
        soft = guard.get("soft")
        soft_failed = bool(
            isinstance(soft, Mapping) and soft.get("fail_closed") is True
        )
        return guard.get("terminal_stop") is False and not soft_failed

    @staticmethod
    def _command_envelope_passed(command: V5ControlCommandV1) -> bool:
        qdot = command.gate_receipt.get("qdot")
        return bool(isinstance(qdot, Mapping) and qdot.get("allowed") is True)

    def observe(
        self,
        attempt_index: int,
        *,
        command: V5ControlCommandV1,
        output: Any,
        sensor: SensorPacket,
        expected_identity: CandidateIdentityV1,
        sample_index: int,
        host_identity: CandidateIdentityV1,
        rollover_input: V5RolloverInput,
        activation_pending: bool = False,
        activation_durable: bool = True,
        include_in_phase_summary: bool = True,
    ) -> None:
        if (
            not isinstance(host_identity, CandidateIdentityV1)
            or not isinstance(rollover_input, V5RolloverInput)
            or type(activation_pending) is not bool
            or type(activation_durable) is not bool
            or type(include_in_phase_summary) is not bool
        ):
            raise TypeError("V5 gate authority evidence is not typed")
        phase = "path" if command.path_time_s < PATH_END_S else "tail"
        guard = command.gate_receipt.get("guard_stack")
        soft = guard.get("soft") if isinstance(guard, Mapping) else None
        qdot_receipt = command.gate_receipt.get("qdot")
        guard_terminal_stop = (
            guard.get("terminal_stop")
            if isinstance(guard, Mapping)
            and type(guard.get("terminal_stop")) is bool
            else True
        )
        guard_soft_fail_closed = bool(
            isinstance(soft, Mapping) and soft.get("fail_closed") is True
        )
        command_envelope_allowed = bool(
            isinstance(qdot_receipt, Mapping)
            and qdot_receipt.get("allowed") is True
        )
        controller_echo = {
            "epoch": int(output.integer_echoes[24]),
            "ordinal": int(output.integer_echoes[25]),
            "candidate_token": int(output.integer_echoes[27]),
        }
        identity_valid = bool(
            controller_echo["epoch"] == expected_identity.epoch
            and controller_echo["ordinal"] == expected_identity.ordinal
            and controller_echo["candidate_token"]
            == expected_identity.candidate_token
        )
        observed = {
            "safety": bool(output.safety_normal and not sensor.stop_request),
            "timing": bool(0.0 < command.actual_dt_s < 0.08),
            "freshness": bool(sensor.sensor_fresh),
            "tube_cbf": self._guard_passed(command),
            "identity": bool(identity_valid),
            "command_envelope": self._command_envelope_passed(command),
        }
        observation = {
            "sample_index": sample_index,
            "qdot": list(command.qdot),
            "gate_receipt_sha256": canonical_sha256(dict(command.gate_receipt)),
            "safety_normal": bool(output.safety_normal),
            "stop_request": bool(sensor.stop_request),
            "controller_epoch": controller_echo["epoch"],
            "controller_ordinal": controller_echo["ordinal"],
            "controller_candidate_token": controller_echo["candidate_token"],
            "guard_terminal_stop": guard_terminal_stop,
            "guard_soft_fail_closed": guard_soft_fail_closed,
            "command_envelope_allowed": command_envelope_allowed,
            "host_epoch": host_identity.epoch,
            "host_ordinal": host_identity.ordinal,
            "host_candidate_token": host_identity.candidate_token,
            "rollover_command": int(rollover_input.command),
            "rollover_generation": rollover_input.generation,
            "activation_pending": activation_pending,
            "activation_durable": activation_durable,
        }
        self._observations.append(observation)
        if activation_pending and (activation_durable or not all(observed.values())):
            raise V5LiveOwnerError(
                "V5 activation-pending gate/authority closure failed"
            )
        if include_in_phase_summary:
            summary = self._summaries[attempt_index][phase]
            summary["observed_count"] += 1
            for name, passed in observed.items():
                if not passed:
                    summary["failure_counts"][name] += 1
            summary["observation_chain_sha256"] = canonical_sha256(
                {
                    "previous_sha256": summary["observation_chain_sha256"],
                    "observation": observation,
                }
            )

    def observe_timing(self, sample_index: int) -> None:
        self._timing_sample_indices.append(int(sample_index))

    @property
    def observations(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(dict(value) for value in self._observations)

    @property
    def timing_sample_indices(self) -> tuple[int, ...]:
        return tuple(self._timing_sample_indices)

    def summaries(
        self,
        *,
        timing_acceptance: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            {
                "path": {
                    "observed_count": value["path"]["observed_count"],
                    "failure_counts": dict(value["path"]["failure_counts"]),
                    "observation_chain_sha256": value["path"]["observation_chain_sha256"],
                },
                "tail": {
                    "observed_count": value["tail"]["observed_count"],
                    "failure_counts": dict(value["tail"]["failure_counts"]),
                    "observation_chain_sha256": value["tail"]["observation_chain_sha256"],
                },
                "timing_acceptance": dict(timing_acceptance),
            }
            for value in self._summaries
        )

    def all_passed(self, *, timing_acceptance_passed: bool) -> bool:
        return bool(
            timing_acceptance_passed
            and all(
                phase["observed_count"] > 0
                and not any(phase["failure_counts"].values())
                for value in self._summaries
                for phase in (value["path"], value["tail"])
            )
        )


@dataclass
class V5LiveContextV1:
    writer: Any
    transport: V5MatureTransportAdapter
    trace: V5LifecycleTraceAdapter
    backend_factory: Callable[[V5CandidateRuntimeSpecV1], Any]
    anchor_pose: tuple[float, ...]
    close_callback: Callable[[], None]
    timing_scheduler_profile: str = LATE_CONTROL_FIFO_PROFILE
    schema: str = V5_LIVE_OWNER_SCHEMA
    version: int = V5_LIVE_OWNER_VERSION

    def close(self) -> None:
        self.close_callback()


class V5SingleWriterOwnerV1:
    """Execute one one-contact chain through the sole mature writer."""

    def __init__(self, context: V5LiveContextV1) -> None:
        if not isinstance(context, V5LiveContextV1):
            raise TypeError("V5 owner requires V5LiveContextV1")
        self.context = context

    @staticmethod
    def _return_evidence(writer: Any, terminal: Any, seen_states: set[int]) -> ReturnEvidence:
        home = getattr(writer, "_home", None)
        if home is None:
            raise V5LiveOwnerError("mature writer has no captured Home reference")
        pose_error = math.dist(terminal.tcp_pose_m_rad[:3], home.pose[:3])
        orientation_error = math.dist(terminal.tcp_pose_m_rad[3:], home.pose[3:])
        q_error = max(
            abs(actual - expected)
            for actual, expected in zip(
                terminal.q_rad,
                home.q or _ZERO_QDOT,
                strict=True,
            )
        )
        guard = int(terminal.integer_echoes[31])
        return ReturnEvidence(
            stationary=terminal.stationary,
            retract_z_m=0.005 if guard & 2 else 0.0,
            transfer_floor_z_m=0.0,
            entry_linear_speed_m_s=float(writer._entry_linear_speed_m_s),
            return_linear_speed_m_s=math.sqrt(
                sum(value * value for value in terminal.tcp_speed_m_s_rad_s[:3])
            ),
            entry_angular_speed_rad_s=float(writer._entry_angular_speed_rad_s),
            return_angular_speed_rad_s=math.sqrt(
                sum(value * value for value in terminal.tcp_speed_m_s_rad_s[3:])
            ),
            descended_to_captured_home=bool(guard & 32),
            home_pose_error_m=pose_error,
            home_orientation_error_rad=orientation_error,
            home_q_error_rad=q_error,
            safety_gate_passed=bool(terminal.safety_normal),
            # V5's fresh stable 1 N latch enters PATH on the same controller
            # tick, so an RTDE observer may never see legacy State21.  The V5
            # contact proof is the observed contact-acquisition state plus the
            # identity-bound PATH state; all unchanged return/safety checks
            # remain inside evaluate_return().
            contact_gate_passed={20, 25}.issubset(seen_states),
        )

    @staticmethod
    def _entry_evidence(
        command: V5ControlCommandV1,
        view: V5OutputSnapshotV1,
    ) -> str:
        return canonical_sha256(
            {
                "schema": "step6.autotune/figure8-v5-chain-entry-evidence-v1",
                "identity": command.identity.as_dict(),
                "state": int(view.state),
                "filtered_normal_n": command.filtered_normal_n,
                "base_target_n": command.base_target_n,
                "effective_target_n": command.effective_target_n,
                "gate_receipt": dict(command.gate_receipt),
            }
        )

    @staticmethod
    def _switch_evidence(
        old_command: V5ControlCommandV1,
        view: V5OutputSnapshotV1,
        next_identity: CandidateIdentityV1,
        generation: int,
        commit_seed_qdot: Sequence[float],
    ) -> dict[str, Any]:
        return {
            "schema": "step6.autotune/figure8-v5-switch-gate-receipt-v1",
            "version": 1,
            "old_identity": old_command.identity.as_dict(),
            "next_identity": next_identity.as_dict(),
            "generation": generation,
            "qdot_generation": generation,
            "tail_endpoint_s": TAIL_END_S,
            "controller_overlay": dict(view.overlay.by_register),
            "commit_seed_qdot": list(commit_seed_qdot),
            "gate_receipt": dict(old_command.gate_receipt),
            "narrow_force_windows_blocking": False,
        }

    @staticmethod
    def _send_at_command_clock(
        writer: Any,
        trace: V5LifecycleTraceAdapter,
        sensor: SensorPacket,
        *,
        monotonic_s: float,
        command_mode: CommandMode,
        proposed_qdot: Sequence[float],
        internal_setpoint_n: float,
    ) -> Any:
        """Bind the lifecycle row clock to the exact ``chain.step`` clock."""

        trace.bind_command_clock(monotonic_s)
        try:
            return writer._send_packet(
                sensor,
                command_mode=command_mode,
                proposed_qdot=proposed_qdot,
                internal_setpoint_n=internal_setpoint_n,
            )
        finally:
            trace.clear_command_clock()

    def execute_chain(self, request: V5LiveChainRequestV1) -> V5SealedChainResultV1:
        if not isinstance(request, V5LiveChainRequestV1):
            raise TypeError("V5 chain request must be typed")
        writer = self.context.writer
        transport = self.context.transport
        trace = self.context.trace
        specs = tuple(runtime_spec_from_plan(plan) for plan in request.plans)
        runtime_epoch = getattr(getattr(writer, "prerequisites", None), "session_epoch", None)
        if runtime_epoch is not None and any(
            spec.identity.epoch != runtime_epoch for spec in specs
        ):
            raise V5LiveOwnerError(
                "V5 chain plans are not bound to the fresh resident session epoch"
            )
        # Pinocchio/URDF/controller construction is deliberately completed
        # before trace.begin_attempt(), ARM, and the 500 Hz timing population.
        # Hot-path prepare() below is therefore an exact, single-use cache take.
        backend_cache = V5PrebuiltBackendCacheV1(
            specs,
            self.context.backend_factory,
        )
        chain = V5ChainControlV1(
            specs[0],
            backend_factory=backend_cache.take,
            anchor_pose=self.context.anchor_pose,
        )
        if len(specs) > 1:
            chain.prepare(specs[1])
        coordinator = V5RolloverCoordinatorV1(
            specs[0].identity,
            None if len(specs) == 1 else specs[1].identity,
            generation=1,
        )
        current_index = 0
        path_origin_rtde_s: float | None = None
        last_path_time_s = 0.0
        first_path_recorded = False
        seen_states: set[int] = set()
        rejected_reason: int | None = None
        gate_closure = _GateClosureAccumulator(len(request.plans))
        switch_gate_receipts: list[Mapping[str, Any]] = []
        activation_journal = V5RolloverActivationJournalV1(request)
        activation_barrier = V5ActivationDurabilityBarrierV1(
            activation_journal
        )
        activation_barrier_closed = False
        pending_activation: V5PendingActivationV1 | None = None
        awaiting_successor_publication: dict[str, Any] | None = None
        activation_publication_receipts: list[dict[str, Any]] = []
        terminal_output: Any | None = None
        terminal_sensor: SensorPacket | None = None
        last_command: V5ControlCommandV1 | None = None
        commit_seed = _ZERO_QDOT
        trace.begin_attempt(
            specs[0].identity.ordinal,
            request.chain_id,
            specs[0].identity.attempt_kind.name,
            epoch=specs[0].identity.epoch,
            path_requested=True,
            capture_started_before_motion=True,
        )
        setattr(writer, "_r013_lifecycle_trace", trace)
        writer.candidate = specs[0].controller_candidate
        writer._baseline_successes = 3  # layout-606 host DTO compatibility only; not qualification evidence
        transport.bind_attempt_kind(specs[0].identity.attempt_kind)
        transport.set_rollover_input(V5RolloverInput())
        start = writer._mono_clock()
        gc_window = V5GCWindowReceiptV1.capture()
        timing_scheduler_profile = TimingSchedulerProfileV1.from_id(
            self.context.timing_scheduler_profile
        )
        timing_scheduler_lease = FormalTimingSchedulerLeaseV2(
            timing_scheduler_profile
        )
        timing_scheduler_entered = False
        timing_scheduler_writer_owned = False
        timing_scheduler_writer_releaser: Callable[[], Any] | None = None
        timing_scheduler_receipt: Mapping[str, Any] | None = None

        def _release_timing_scheduler() -> Mapping[str, Any]:
            """Release exactly once through the seam that acquired the lease."""

            nonlocal timing_scheduler_entered
            nonlocal timing_scheduler_writer_owned
            result: Any = None
            if timing_scheduler_writer_owned:
                if not callable(timing_scheduler_writer_releaser):
                    raise V5LiveOwnerError(
                        "V5 writer-owned timing lease lacks its release seam"
                    )
                result = (
                    timing_scheduler_lease.receipt()
                    if timing_scheduler_lease.restored
                    else timing_scheduler_writer_releaser()
                )
                timing_scheduler_writer_owned = False
            elif timing_scheduler_entered:
                result = timing_scheduler_lease.exit()
            timing_scheduler_entered = False
            if isinstance(result, Mapping):
                return dict(result)
            return timing_scheduler_lease.receipt()

        try:
            # Keep cyclic GC quiet before the scheduler lease and ARM/session
            # mutation; disabling only after arm_unbounded() leaves the
            # ARM-to-first-control seam exposed to a long collection.
            gc_window.enter()
            preparer = getattr(writer, "prepare_timing_scheduler_lease", None)
            installer = getattr(writer, "install_timing_scheduler_lease", None)
            releaser = getattr(writer, "release_timing_scheduler_lease", None)
            seam_members = tuple(callable(item) for item in (preparer, installer, releaser))
            if any(seam_members) and not all(seam_members):
                raise V5LiveOwnerError(
                    "V5 writer timing scheduler lease seam is incomplete"
                )
            mature_timing_seam = all(seam_members)
            if mature_timing_seam:
                preparer()
            else:
                # Compatibility-only test doubles may not expose the mature
                # writer seam.  Keep the same ordering and one-shot token for
                # an underlying arm() implementation when it does.
                gc.collect()
                setattr(writer, "_prearm_gc_collected", True)
            # The mature writer owns the final pre-ARM Home poll.  Install the
            # lease there so arm() enters it only after that poll and before
            # ARM/session mutation.  Compatibility-only fakes with neither
            # seam retain the explicit external fallback; a partial seam is
            # rejected instead of risking double enter/exit.
            if mature_timing_seam:
                installer(timing_scheduler_lease)
                timing_scheduler_writer_owned = True
                timing_scheduler_writer_releaser = releaser
            else:
                timing_scheduler_lease.enter()
                timing_scheduler_entered = True
            arm_tick = writer.arm_unbounded(
                ordinal=specs[0].identity.ordinal,
                kind=AttemptKind.BATCH_B,
                candidate_token=specs[0].identity.candidate_token,
            )
            if timing_scheduler_writer_owned:
                if not timing_scheduler_lease.entered or timing_scheduler_lease.restored:
                    raise V5LiveOwnerError(
                        "V5 mature writer returned from ARM without an active timing lease"
                    )
                timing_scheduler_entered = True
            arm_output = getattr(arm_tick, "output", None)
            if arm_output is None:
                raise V5LiveOwnerError("V5 ARM did not return its output boundary")
            try:
                arm_state = int(arm_output.integer_echoes[26])
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise V5LiveOwnerError("V5 ARM output state is untyped") from exc
            control_start_sample_index = trace.mark_control_start(
                monotonic_s=writer._mono_clock(),
                output=arm_output,
                sensor=writer._read_sensor(),
                tp_state=arm_state,
            )
            while True:
                if activation_barrier.pending:
                    settlement = activation_barrier.settle_if_ready()
                    if settlement is not None:
                        if pending_activation is None:
                            if settlement.deferred_rollover_input is not None:
                                transport.set_rollover_input(
                                    settlement.deferred_rollover_input
                                )
                        else:
                            pending = pending_activation
                            if (
                                settlement.batch.generation != pending.generation
                                or settlement.batch.states
                                != ("COMMIT_ACK", "ACTIVATED")
                                or settlement.deferred_rollover_input is not None
                            ):
                                raise V5LiveOwnerError(
                                    "V5 activation durability settlement differs"
                                )
                            settled_monotonic_s = writer._mono_clock()
                            next_spec = chain.acknowledge_commit()
                            current_index += 1
                            if next_spec != specs[current_index] or (
                                next_spec != pending.next_spec
                            ):
                                raise V5LiveOwnerError(
                                    "V5 durable committed runtime spec differs from plan"
                                )
                            writer._ordinal = next_spec.identity.ordinal
                            writer._candidate_token = (
                                next_spec.identity.candidate_token
                            )
                            writer.candidate = next_spec.controller_candidate
                            transport.bind_attempt_kind(
                                next_spec.identity.attempt_kind
                            )
                            path_origin_rtde_s = pending.seam_rtde_timestamp_s
                            last_path_time_s = pending.last_path_time_s
                            last_command = None
                            successor = (
                                specs[current_index + 1].identity
                                if current_index + 1 < len(specs)
                                else None
                            )
                            coordinator = V5RolloverCoordinatorV1(
                                next_spec.identity,
                                successor,
                                generation=current_index + 1,
                            )
                            if successor is not None:
                                chain.prepare(specs[current_index + 1])
                            transport.set_rollover_input(V5RolloverInput())
                            awaiting_successor_publication = {
                                "schema": (
                                    "step6.autotune/figure8-v5-activation-"
                                    "publication-v1"
                                ),
                                "version": 1,
                                "generation": pending.generation,
                                "old_identity": pending.old_identity.as_dict(),
                                "next_identity": next_spec.identity.as_dict(),
                                "seam_sample_index": pending.seam_sample_index,
                                "pending_sample_indices": list(
                                    pending.pending_sample_indices or ()
                                ),
                                "durability_settled_monotonic_s": (
                                    settled_monotonic_s
                                ),
                                "durable_states": list(
                                    settlement.batch.states
                                ),
                                "durable_row_sha256s": list(
                                    settlement.batch.row_sha256s
                                ),
                                "durable_journal_head_sha256": (
                                    settlement.batch.journal_head_sha256
                                ),
                            }
                            pending_activation = None
                if writer._mono_clock() - start >= request.timeout_s:
                    raise V5LiveOwnerError("V5 live chain timed out")
                output = writer._poll_checked(
                    wait_s=writer.fresh_frame_wait_policy.wait_s
                )
                if output is None:
                    continue
                view = transport.latest_v5
                if view is None:
                    raise V5LiveOwnerError("V5 transport has no typed output view")
                state = view.state
                seen_states.add(int(state))
                if state is V5TPState.STOPPED:
                    raise V5LiveOwnerError(
                        "V5 resident STOPPED: "
                        f"reason={output.integer_echoes.get(28)}"
                    )
                if not writer._last_poll_was_fresh:
                    # A cached terminal image is not lifecycle evidence.  Keep
                    # polling until the mature stale-frame guard either yields
                    # one fresh terminal frame or fails closed.
                    continue
                sensor = writer._read_sensor()
                now = writer._mono_clock()
                active_identity = chain.active_spec.identity
                prepared_identity = (
                    None
                    if chain.prepared_spec is None
                    else chain.prepared_spec.identity
                )
                resolution = coordinator.resolve_echo(
                    view,
                    active_identity=active_identity,
                    prepared_identity=prepared_identity,
                )
                expected_identity = resolution.expected_identity

                if state is V5TPState.READY_HOME_NEXT:
                    if (
                        activation_barrier.pending
                        or pending_activation is not None
                        or awaiting_successor_publication is not None
                    ):
                        raise V5LiveOwnerError(
                            "V5 reached Home with pending activation durability"
                        )
                    terminal_output = output
                    terminal_sensor = replace(
                        sensor,
                        filtered_normal_n=float(chain.active_backend.filtered_normal_n),
                    )
                    terminal_index = trace.next_sample_index
                    trace.observe_terminal(
                        monotonic_s=now,
                        output=output,
                        sensor=terminal_sensor,
                        tp_state=int(V5TPState.READY_HOME_NEXT),
                        command_mode=int(CommandMode.HOLD),
                        packet_sequence=max(0, int(writer._packet_sequence) - 1),
                        consumed_packet_sequence=int(output.consumed_packet_sequence),
                    )
                    home_evidence = canonical_sha256(
                        {
                            "schema": "step6.autotune/figure8-v5-home-evidence-v1",
                            "identity": specs[current_index].identity.as_dict(),
                            "return_guard": int(output.integer_echoes[31]),
                            "stationary": bool(output.stationary),
                            "safety_normal": bool(output.safety_normal),
                            "tcp_pose": list(output.tcp_pose_m_rad),
                            "q": list(output.q_rad),
                        }
                    )
                    trace.descriptors.append(
                        V5EventDescriptorV1(
                            LifecycleEventKind.HOME,
                            terminal_index,
                            specs[current_index].identity,
                            home_evidence,
                        )
                    )
                    break
                if state is V5TPState.COMPLETE:
                    raise V5LiveOwnerError("V5 resident reached COMPLETE without typed Home")
                if state is V5TPState.ROLLOVER_REJECTED:
                    rejected_reason = int(output.integer_echoes[28])

                if resolution.kind is V5RolloverEchoKind.FIRST_COMMIT_ACK:
                    old_path_time = TAIL_END_S
                    old_command = chain.step(
                        output=output,
                        sensor=sensor,
                        tp_state=V5TPState.CLOSURE_TAIL,
                        path_time_s=old_path_time,
                        monotonic_s=now,
                    )
                    sensor = replace(
                        sensor,
                        filtered_normal_n=old_command.filtered_normal_n,
                    )
                    seam_index = trace.next_sample_index
                    gate_closure.observe_timing(seam_index)
                    commit_seed = chain.refresh_commit_seed()
                    decision = coordinator.observe(
                        view,
                        path_time_s=old_path_time,
                        monotonic_s=now,
                    )
                    if not decision.acknowledge_commit:
                        raise V5LiveOwnerError("V5 state28 did not acknowledge the armed COMMIT")
                    next_spec = chain.prepared_spec
                    if next_spec is None or next_spec != specs[current_index + 1]:
                        raise V5LiveOwnerError(
                            "V5 staged successor runtime spec differs from plan"
                        )
                    switch_receipt = self._switch_evidence(
                        old_command,
                        view,
                        next_spec.identity,
                        coordinator.generation,
                        commit_seed,
                    )
                    switch_gate_receipts.append(switch_receipt)
                    switch_hash = canonical_sha256(switch_receipt)
                    activation_generation = coordinator.generation
                    old_identity = specs[current_index].identity
                    trace.descriptors.append(
                        V5EventDescriptorV1(
                            LifecycleEventKind.CONTACT_ROLLOVER,
                            seam_index,
                            old_identity,
                            switch_hash,
                            next_identity=next_spec.identity,
                            generation=activation_generation,
                            qdot_generation=activation_generation,
                        )
                    )
                    pending_activation = V5PendingActivationV1(
                        activation_generation,
                        old_identity,
                        next_spec,
                        tuple(commit_seed),
                        seam_index,
                        float(output.timestamp),
                    )
                    pending_activation.pending_sample_indices.append(seam_index)
                    gate_closure.observe(
                        current_index,
                        command=old_command,
                        output=output,
                        sensor=sensor,
                        expected_identity=expected_identity,
                        sample_index=seam_index,
                        host_identity=old_identity,
                        rollover_input=transport.rollover_input,
                        activation_pending=True,
                        activation_durable=False,
                    )
                    activation_barrier.submit(
                        (
                            V5ActivationJournalEntryV1(
                                "COMMIT_ACK",
                                activation_generation,
                                old_identity,
                                next_spec.identity,
                                {
                                    "sample_index": seam_index,
                                    "rtde_timestamp_s": float(output.timestamp),
                                    "switch_gate_receipt_sha256": switch_hash,
                                    "controller_overlay": dict(
                                        view.overlay.by_register
                                    ),
                                },
                            ),
                            V5ActivationJournalEntryV1(
                                "ACTIVATED",
                                activation_generation,
                                old_identity,
                                next_spec.identity,
                                {
                                    "sample_index": seam_index,
                                    "candidate_token": (
                                        next_spec.identity.candidate_token
                                    ),
                                    "candidate_ordinal": next_spec.identity.ordinal,
                                    "activation_qdot": list(commit_seed),
                                    # COMMIT continuation packets may keep the
                                    # controller loop alive while this batch is
                                    # syncing.  The first ordinary successor
                                    # PATH/NONE packet remains after durability.
                                    "successor_path_packet_follows_fsync": True,
                                },
                            ),
                        ),
                    )
                    # The controller identity is already successor at state28,
                    # but the host binding remains old and the wire remains the
                    # exact duplicate COMMIT until the batch is cold-durable.
                    trace.set_effective_target(old_command.effective_target_n)
                    self._send_at_command_clock(
                        writer,
                        trace,
                        sensor,
                        monotonic_s=now,
                        command_mode=CommandMode.PATH,
                        proposed_qdot=commit_seed,
                        internal_setpoint_n=old_command.base_target_n,
                    )
                    continue

                if resolution.kind is V5RolloverEchoKind.ACTIVATION_PENDING:
                    pending = pending_activation
                    if (
                        pending is None
                        or not activation_barrier.pending
                        or coordinator.phase is not V5RolloverPhase.COMMITTED
                        or transport.rollover_input.command
                        is not RolloverCommand.COMMIT
                        or transport.rollover_input.generation
                        != pending.generation
                    ):
                        raise V5LiveOwnerError(
                            "V5 activation-pending host/wire state differs"
                        )
                    path_time_s = min(
                        math.nextafter(FORMAL_METRIC_START_S, 0.0),
                        max(
                            0.0,
                            float(output.timestamp)
                            - pending.seam_rtde_timestamp_s,
                        ),
                    )
                    pending.last_path_time_s = max(
                        pending.last_path_time_s, path_time_s
                    )
                    command = chain.step_activation_pending(
                        output=output,
                        sensor=sensor,
                        successor_identity=pending.next_spec.identity,
                        commit_seed_qdot=pending.commit_seed_qdot,
                        path_time_s=path_time_s,
                        monotonic_s=now,
                    )
                    sensor = replace(
                        sensor,
                        filtered_normal_n=command.filtered_normal_n,
                    )
                    sample_index = trace.next_sample_index
                    pending.pending_sample_indices.append(sample_index)
                    gate_closure.observe_timing(sample_index)
                    gate_closure.observe(
                        current_index + 1,
                        command=command,
                        output=output,
                        sensor=sensor,
                        expected_identity=expected_identity,
                        sample_index=sample_index,
                        host_identity=pending.old_identity,
                        rollover_input=transport.rollover_input,
                        activation_pending=True,
                        activation_durable=False,
                        include_in_phase_summary=False,
                    )
                    trace.set_effective_target(command.effective_target_n)
                    self._send_at_command_clock(
                        writer,
                        trace,
                        sensor,
                        monotonic_s=now,
                        command_mode=CommandMode.PATH,
                        proposed_qdot=pending.commit_seed_qdot,
                        internal_setpoint_n=command.base_target_n,
                    )
                    continue

                if resolution.kind is V5RolloverEchoKind.POST_COMMIT_DRAIN:
                    if path_origin_rtde_s is None:
                        raise V5LiveOwnerError(
                            "V5 post-COMMIT drain lacks successor path origin"
                        )
                    # The controller intentionally persists state28 while the
                    # input pipeline drains repeated COMMIT packets.  The
                    # successor is already active, so these fresh frames are
                    # ordinary successor PATH ticks and must not mint a second
                    # switch event or refresh a cleared COMMIT seed.
                    path_time_s = min(
                        math.nextafter(TAIL_END_S, 0.0),
                        max(0.0, output.timestamp - path_origin_rtde_s),
                    )
                    last_path_time_s = max(last_path_time_s, path_time_s)
                elif state in _PATH_OUTPUT_STATES:
                    if path_origin_rtde_s is None:
                        path_origin_rtde_s = output.timestamp
                    path_time_s = min(
                        math.nextafter(TAIL_END_S, 0.0),
                        max(0.0, output.timestamp - path_origin_rtde_s),
                    )
                    last_path_time_s = max(last_path_time_s, path_time_s)
                else:
                    path_time_s = last_path_time_s
                command = chain.step(
                    output=output,
                    sensor=sensor,
                    tp_state=state,
                    path_time_s=path_time_s,
                    monotonic_s=now,
                )
                last_command = command
                sensor = replace(sensor, filtered_normal_n=command.filtered_normal_n)
                sample_index = trace.next_sample_index
                gate_closure.observe_timing(sample_index)
                if command.command_mode is CommandMode.PATH:
                    gate_closure.observe(
                        current_index,
                        command=command,
                        output=output,
                        sensor=sensor,
                        expected_identity=expected_identity,
                        sample_index=sample_index,
                        host_identity=specs[current_index].identity,
                        rollover_input=transport.rollover_input,
                        activation_pending=False,
                        activation_durable=True,
                    )
                if activation_barrier.pending:
                    # The storage worker owns every flush/fsync/cold-verify.
                    # The single live owner keeps polling, gating and sending
                    # the already-published PREPARE/COMMIT command at 500 Hz;
                    # no new rollover transition is observed until the prior
                    # durable transition has completed.
                    if coordinator.phase is V5RolloverPhase.COMMIT_SENT:
                        commit_seed = chain.refresh_commit_seed()
                    trace.set_effective_target(command.effective_target_n)
                    writer._sticky_latched = command.sticky_one_newton_latched
                    self._send_at_command_clock(
                        writer,
                        trace,
                        sensor,
                        monotonic_s=now,
                        command_mode=command.command_mode,
                        proposed_qdot=command.qdot,
                        internal_setpoint_n=command.base_target_n,
                    )
                    if (
                        command.command_mode is CommandMode.PATH
                        and not first_path_recorded
                    ):
                        first_path_recorded = True
                        trace.descriptors.append(
                            V5EventDescriptorV1(
                                LifecycleEventKind.CHAIN_START,
                                sample_index,
                                specs[0].identity,
                                self._entry_evidence(command, view),
                            )
                    )
                    continue
                if awaiting_successor_publication is not None:
                    # A newly durable activation publishes exactly one
                    # ordinary successor PATH/NONE packet before the next
                    # PREPARE state machine may start.
                    transport.set_rollover_input(V5RolloverInput())
                    trace.set_effective_target(command.effective_target_n)
                    writer._sticky_latched = command.sticky_one_newton_latched
                    self._send_at_command_clock(
                        writer,
                        trace,
                        sensor,
                        monotonic_s=now,
                        command_mode=command.command_mode,
                        proposed_qdot=command.qdot,
                        internal_setpoint_n=command.base_target_n,
                    )
                    settled_s = float(
                        awaiting_successor_publication[
                            "durability_settled_monotonic_s"
                        ]
                    )
                    if (
                        specs[current_index].identity
                        != chain.active_spec.identity
                        or now <= settled_s
                    ):
                        raise V5LiveOwnerError(
                            "V5 first successor publication preceded durability: "
                            f"host={specs[current_index].identity.as_dict()},"
                            f"active={chain.active_spec.identity.as_dict()},"
                            f"settled={settled_s:.9f},publish={now:.9f}"
                        )
                    awaiting_successor_publication.update(
                        {
                            "first_successor_sample_index": sample_index,
                            "first_successor_packet_sequence": max(
                                0, int(writer._packet_sequence) - 1
                            ),
                            "first_successor_publish_monotonic_s": now,
                            "first_successor_rollover_command": int(
                                RolloverCommand.NONE
                            ),
                            "first_successor_host_identity": specs[
                                current_index
                            ].identity.as_dict(),
                        }
                    )
                    activation_publication_receipts.append(
                        dict(awaiting_successor_publication)
                    )
                    awaiting_successor_publication = None
                    if (
                        command.command_mode is CommandMode.PATH
                        and not first_path_recorded
                    ):
                        first_path_recorded = True
                        trace.descriptors.append(
                            V5EventDescriptorV1(
                                LifecycleEventKind.CHAIN_START,
                                sample_index,
                                specs[0].identity,
                                self._entry_evidence(command, view),
                            )
                        )
                    continue
                defer_rollover_publish = False
                prior_rollover_phase = coordinator.phase
                decision = coordinator.observe(
                    view,
                    path_time_s=path_time_s,
                    monotonic_s=now,
                )
                if (
                    prior_rollover_phase is V5RolloverPhase.PREPARE_SENT
                    and coordinator.phase is V5RolloverPhase.PREPARED
                ):
                    successor_identity = coordinator.successor
                    assert successor_identity is not None
                    activation_barrier.submit(
                        (
                            V5ActivationJournalEntryV1(
                                "PREPARED",
                                coordinator.generation,
                                coordinator.current,
                                successor_identity,
                                {
                                    "sample_index": sample_index,
                                    "rtde_timestamp_s": float(output.timestamp),
                                    "controller_overlay": dict(
                                        view.overlay.by_register
                                    ),
                                },
                            ),
                        )
                    )
                if decision.prepare_runtime and chain.prepared_spec is None:
                    raise V5LiveOwnerError("V5 PREPARE has no sensor-silent prepared backend")
                if decision.arm_commit:
                    commit_seed = chain.arm_commit()
                    successor_identity = coordinator.successor
                    assert successor_identity is not None
                    activation_barrier.submit(
                        (
                            V5ActivationJournalEntryV1(
                                "COMMIT_INTENT",
                                coordinator.generation,
                                coordinator.current,
                                successor_identity,
                                {
                                    "sample_index": sample_index,
                                    "rtde_timestamp_s": float(output.timestamp),
                                    "commit_seed_qdot": list(commit_seed),
                                    "wire_input": dict(
                                        decision.wire_input.by_register
                                    ),
                                },
                            ),
                        ),
                        deferred_rollover_input=decision.wire_input,
                    )
                    defer_rollover_publish = True
                if coordinator.phase is V5RolloverPhase.COMMIT_SENT:
                    commit_seed = chain.refresh_commit_seed()
                if not defer_rollover_publish:
                    transport.set_rollover_input(decision.wire_input)
                trace.set_effective_target(command.effective_target_n)
                writer._sticky_latched = command.sticky_one_newton_latched
                self._send_at_command_clock(
                    writer,
                    trace,
                    sensor,
                    monotonic_s=now,
                    command_mode=command.command_mode,
                    proposed_qdot=command.qdot,
                    # The resident packet contract remains [1,5]; correction is
                    # already represented in qdot and the trace adapter records
                    # the effective target separately.
                    internal_setpoint_n=command.base_target_n,
                )
                if command.command_mode is CommandMode.PATH and not first_path_recorded:
                    first_path_recorded = True
                    trace.descriptors.append(
                        V5EventDescriptorV1(
                            LifecycleEventKind.CHAIN_START,
                            sample_index,
                            specs[0].identity,
                            self._entry_evidence(command, view),
                        )
                    )

            if terminal_output is None or terminal_sensor is None:
                raise V5LiveOwnerError("V5 chain ended without a fresh Home sensor row")
            if timing_scheduler_entered or timing_scheduler_writer_owned:
                timing_scheduler_receipt = _release_timing_scheduler()
            try:
                gc_window.restore_after_timing_lease()
            finally:
                setattr(writer, "_prearm_gc_collected", False)
            if (
                activation_barrier.pending
                or pending_activation is not None
                or awaiting_successor_publication is not None
            ):
                raise V5LiveOwnerError(
                    "V5 chain ended with pending activation durability"
                )
            if backend_cache.remaining_count != 0:
                raise V5LiveOwnerError(
                    "V5 chain reached Home before consuming its exact prebuilt "
                    "backend sequence"
                )
            activation_barrier.close()
            activation_barrier_closed = True
            if rejected_reason is not None:
                raise V5LiveOwnerError(
                    f"V5 rollover rejected before Home: reason={rejected_reason}"
                )
            evidence = self._return_evidence(writer, terminal_output, seen_states)
            decision = evaluate_return(evidence)
            if not decision.passed:
                raise V5LiveOwnerError(
                    f"V5 Home return gate failed: {decision.reason}"
                )
            timing_acceptance = chain.timing.acceptance()
            gate_summaries = gate_closure.summaries(
                timing_acceptance=timing_acceptance
            )
            if not gate_closure.all_passed(
                timing_acceptance_passed=timing_acceptance.get("passed") is True
            ):
                raise V5LiveOwnerError(
                    "V5 PATH/tail gate-family closure is incomplete or failed; "
                    f"timing={json.dumps(dict(timing_acceptance), sort_keys=True)}; "
                    f"gates={json.dumps(list(gate_summaries), sort_keys=True)}"
                )
            writer.session.finish_attempt(decision)
            receipt = trace.finalize_attempt(
                terminal_state=int(V5TPState.READY_HOME_NEXT),
                home_verified=True,
            )
            receipt["timing_scheduler"] = dict(timing_scheduler_receipt or {})
            receipt["gc_window"] = gc_window.as_dict()
            receipt_path = receipt.get("receipt_path")
            if isinstance(receipt_path, str) and receipt_path:
                destination = Path(receipt_path)
                temporary = destination.with_suffix(destination.suffix + ".part")
                with temporary.open("w", encoding="utf-8") as stream:
                    json.dump(receipt, stream, sort_keys=True, indent=2, allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            if receipt.get("coverage_complete") is not True:
                raise V5LiveOwnerError(
                    "V5 lifecycle coverage is incomplete: "
                    + json.dumps(dict(receipt), sort_keys=True)
                )
            activation_receipt = activation_journal.receipt(
                require_complete=True
            )
            if len(activation_publication_receipts) != len(request.plans) - 1:
                raise V5LiveOwnerError(
                    "V5 activation publication receipt count differs"
                )
            activation_receipt["publication_receipts"] = list(
                activation_publication_receipts
            )
            activation_receipt["publication_receipts_sha256"] = canonical_sha256(
                activation_publication_receipts
            )
            sealed = seal_v5_chain(
                request,
                lifecycle_receipt=receipt,
                descriptors=trace.descriptors,
                gate_observations=gate_closure.observations,
                timing_sample_indices=gate_closure.timing_sample_indices,
                control_start_sample_index=control_start_sample_index,
                runtime_timing_acceptance=timing_acceptance,
                switch_gate_receipts=switch_gate_receipts,
                activation_receipt=activation_receipt,
            )
            return persist_sealed_chain_result(request, sealed)
        except Exception as exc:
            if trace.active:
                try:
                    trace.finalize_attempt(error=f"V5 live chain failed: {exc}")
                except Exception:
                    pass
            try:
                writer._fail_closed(str(exc))
            except Exception:
                pass
            if timing_scheduler_entered or timing_scheduler_writer_owned:
                try:
                    timing_scheduler_receipt = _release_timing_scheduler()
                except Exception:
                    # Preserve the original fail-closed exception; context
                    # close will still perform its normal cleanup checks.
                    pass
            if not gc_window.restored:
                try:
                    gc_window.restore_after_timing_lease()
                except Exception:
                    pass
            setattr(writer, "_prearm_gc_collected", False)
            if isinstance(exc, V5LiveOwnerError):
                raise
            raise V5LiveOwnerError(str(exc)) from exc
        finally:
            if timing_scheduler_entered or timing_scheduler_writer_owned:
                try:
                    _release_timing_scheduler()
                except Exception:
                    pass
            if not activation_barrier_closed:
                try:
                    activation_barrier.close()
                except Exception:
                    pass
            if not gc_window.restored:
                try:
                    gc_window.restore_after_timing_lease()
                except Exception:
                    pass
            setattr(writer, "_prearm_gc_collected", False)


def _read_json(path: Path, role: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise V5LiveOwnerError(f"{role} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise V5LiveOwnerError(f"{role} is not an object")
    return value


class _V5MaturePrerequisites:
    """V5 identity validator over the mature content/EOAT prerequisites."""

    def __init__(self, base: Any) -> None:
        self._base = base

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    def validate(self, *, now_s: float) -> None:
        try:
            from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
            from step5d_autotune_v4_r005.live_adapter import (
                _validate_controller_receipt_content,
                _validate_not_from_future,
                _validate_script1_receipt_content,
            )
            from step5d_eoat_profiles import load_new_eoat_profile
        except ModuleNotFoundError:  # pragma: no cover
            from tools.step5d_autotune_v4_r004.contracts import runtime_identity_limbs
            from tools.step5d_autotune_v4_r005.live_adapter import (
                _validate_controller_receipt_content,
                _validate_not_from_future,
                _validate_script1_receipt_content,
            )
            from tools.step5d_eoat_profiles import load_new_eoat_profile
        base = self._base
        if base.controller.route_id != base.route_id:
            raise V5LiveOwnerError("V5 controller route differs")
        _validate_not_from_future(base.controller.observed_at_s, now_s, "controller receipt")
        _validate_not_from_future(base.script1.observed_at_s, now_s, "Script1 receipt")
        _validate_controller_receipt_content(
            base.controller,
            contract=base.contract,
            expected_triplet=base.expected_triplet,
        )
        _validate_script1_receipt_content(
            base.script1,
            expected_script_sha256=base.contract.script1_sha256["script"],
            expected_eoat_sha256=base.contract.eoat_sha256,
        )
        profile = load_new_eoat_profile()
        base.controller.validate_eoat_readback(
            payload_kg=profile.payload_kg,
            payload_cog_m=profile.cog_m,
            tcp_offset_m_rad=profile.controller_tcp_m_rad,
        )
        hi, lo = runtime_identity_limbs(
            base.contract.raw["program"],
            base.contract.sha256,
            base.contract.campaign_fingerprint,
        )
        if (
            base.controller.runtime_protocol != V5_RUNTIME_PROTOCOL
            or base.controller.runtime_digest_hi != hi
            or base.controller.runtime_digest_lo != lo
            or base.runtime.program != base.contract.raw["program"]
            or base.runtime.script_sha256 != base.controller.script_sha256
            or base.runtime.runtime_protocol != V5_RUNTIME_PROTOCOL
            or base.runtime.runtime_digest_hi != hi
            or base.runtime.runtime_digest_lo != lo
            or base.runtime.session_epoch != base.session_epoch
            or base.runtime.resident_session_id != base.resident_session_id
            or not base.runtime.program_running
            or not base.runtime.uninterrupted
        ):
            raise V5LiveOwnerError("V5 resident runtime identity differs at writer open")
        if (
            base.controller.safety_mode != "NORMAL"
            or not base.controller.stationary
            or base.runtime.observed_at_s <= base.controller.observed_at_s
        ):
            raise V5LiveOwnerError("V5 writer entry is not fresh stationary Safety NORMAL")


def build_v5_live_context(
    *,
    run_dir: Path,
    controller_host: str,
    kunwei_host: str,
    kunwei_port: int,
    launch_profile: Path,
    expected_campaign_fingerprint: str | None = None,
    expected_release_identity_sha256: str | None = None,
    expected_role: LedgerRole | None = None,
    timing_scheduler_profile: str = LATE_CONTROL_FIFO_PROFILE,
) -> V5LiveContextV1:
    """Build and open the sole V5 writer from an immutable READY directory."""

    run_root = Path(run_dir).resolve()
    selected_timing_scheduler_profile = TimingSchedulerProfileV1.from_id(
        timing_scheduler_profile
    )
    ready = _read_json(run_root / "r013_live_owner_ready.json", "V5 READY receipt")
    launch = _read_json(run_root / "launch_context.json", "V5 launch context")
    if ready.get("status") != "resident_ready_no_arm":
        raise V5LiveOwnerError("V5 resident is not READY/no-ARM")
    if ready.get("runtime_protocol") != V5_RUNTIME_PROTOCOL or launch.get(
        "runtime_protocol"
    ) != V5_RUNTIME_PROTOCOL:
        raise V5LiveOwnerError("V5 READY identity is not protocol 607007")
    if ready.get("arm_dispatched") is not False or ready.get("trial_dispatched") is not False:
        raise V5LiveOwnerError("V5 READY directory already dispatched motion")
    expected_values = (
        expected_campaign_fingerprint,
        expected_release_identity_sha256,
        expected_role,
    )
    if any(value is not None for value in expected_values):
        if (
            expected_campaign_fingerprint is None
            or expected_release_identity_sha256 is None
            or not isinstance(expected_role, LedgerRole)
        ):
            raise V5LiveOwnerError("V5 expected live binding is incomplete")
        binding = _read_json(run_root / "v5_live_binding.json", "V5 live binding")
        unsigned = {
            key: value for key, value in binding.items() if key != "binding_sha256"
        }
        if (
            binding.get("schema") != V5_LIVE_BINDING_SCHEMA
            or binding.get("version") != V5_LIVE_OWNER_VERSION
            or binding.get("campaign_fingerprint")
            != expected_campaign_fingerprint
            or binding.get("release_identity_sha256")
            != expected_release_identity_sha256
            or binding.get("role") != expected_role.value
            or binding.get("session_epoch") != ready.get("session_epoch")
            or binding.get("run_dir") != str(run_root)
            or binding.get("compatibility_fingerprint_sha256")
            != ready.get("figure8_campaign_fingerprint_sha256")
            or binding.get("binding_sha256") != canonical_sha256(unsigned)
        ):
            raise V5LiveOwnerError("V5 live binding identity/hash differs")

    try:
        from step5d_autotune_v4_r005.live_adapter import R005LiveInputs
        from step5d_autotune_v4_r006.live_adapter import (
            R006LiveInputs,
            build_verified_mature_r006_writer,
        )
        from step5d_autotune_v4_r006.parent import load_frozen_r005_contract
        from step5d_autotune_v4_r013.lifecycle_trace import (
            LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
            attach_lifecycle_trace,
        )
        from step5d_autotune_v4_r013.live_owner import (
            R013PathProfileV1,
            _r013_compat_contract,
        )
        from step6_figure8_autotune_v1.live_composition import (
            FigureEightPathEvidenceCollectorV1,
            FigureEightPathGuardStackV1,
            figure8_fresh_frame_wait_policy,
            figure8_motion_profile,
            load_figure8_home_binding,
        )
        from step5d_autotune_v4_r005.live_adapter import _R005SessionIdentityGate
        from step5d_autotune_v4_r004_live_writer import LIVE_ACK
        import step5d_autotune_v4_r004.calibrated_runtime as calibrated_module
    except ModuleNotFoundError:  # pragma: no cover
        from tools.step5d_autotune_v4_r005.live_adapter import R005LiveInputs
        from tools.step5d_autotune_v4_r006.live_adapter import (
            R006LiveInputs,
            build_verified_mature_r006_writer,
        )
        from tools.step5d_autotune_v4_r006.parent import load_frozen_r005_contract
        from tools.step5d_autotune_v4_r013.lifecycle_trace import (
            LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
            attach_lifecycle_trace,
        )
        from tools.step5d_autotune_v4_r013.live_owner import (
            R013PathProfileV1,
            _r013_compat_contract,
        )
        from tools.step6_figure8_autotune_v1.live_composition import (
            FigureEightPathEvidenceCollectorV1,
            FigureEightPathGuardStackV1,
            figure8_fresh_frame_wait_policy,
            figure8_motion_profile,
            load_figure8_home_binding,
        )
        from tools.step5d_autotune_v4_r005.live_adapter import _R005SessionIdentityGate
        from tools.step5d_autotune_v4_r004_live_writer import LIVE_ACK
        from tools.step5d_autotune_v4_r004 import calibrated_runtime as calibrated_module

    contract = _r013_compat_contract(R013PathProfileV1.figure8())
    published_parent, admission_parent = load_frozen_r005_contract()
    del published_parent
    expected_triplet = dict(launch.get("triplet", {}))
    if set(expected_triplet) != {"script", "txt", "urp"} or expected_triplet != dict(
        ready.get("triplet", {})
    ):
        raise V5LiveOwnerError("V5 READY triplet binding differs")
    controller_receipt = _read_json(
        run_root / "controller_receipt.json", "V5 controller receipt"
    )
    parent_inputs = R005LiveInputs(
        controller_receipt=run_root / "controller_receipt.json",
        script1_receipt=run_root / "script1_receipt.json",
        runtime_evidence=run_root / "runtime_evidence.json",
        runtime_attestation=run_root / "runtime_evidence.json",
        optimizer_pointer=run_root / "runtime_evidence.json",
        launch_profile_path=Path(launch_profile),
        release_manifest_sha256="0" * 64,
        baseline_ledger=run_root / "software_baseline.json",
        software_baseline_receipt=run_root / "software_baseline.json",
        ledger_path=Path(ready["ledger_path"]),
        queue_root=Path(ready["queue_root"]),
        authority_root=Path(ready["authority_root"]),
        controller_host=controller_host,
        kunwei_host=kunwei_host,
        kunwei_port=int(kunwei_port),
        route_id=str(ready["route_id"]),
        attempt_id=str(ready["attempt_id"]),
        resident_session_id=str(ready["resident_session_id"]),
        session_epoch=int(ready["session_epoch"]),
        expected_triplet=expected_triplet,
        eoat_sha256=str(controller_receipt["eoat_identity_sha256"]),
        campaign_fingerprint=contract.campaign_fingerprint,
        contract_sha256=contract.sha256,
    )
    inputs = R006LiveInputs(
        parent=parent_inputs,
        thresholds_receipt=run_root / "thresholds_receipt.json",
        route_id=parent_inputs.route_id,
        attempt_id=parent_inputs.attempt_id,
        contract_sha256=contract.sha256,
        campaign_fingerprint=contract.campaign_fingerprint,
        expected_triplet=expected_triplet,
    )
    home_binding = load_figure8_home_binding(run_root / "home_start_receipt.json")
    anchor_pose = tuple(float(value) for value in home_binding.profile.pose)
    path_reference = make_v5_runtime_path_reference(anchor_pose)
    guard_stack = FigureEightPathGuardStackV1(path_reference=path_reference)
    transport = V5MatureTransportAdapter(controller_host)
    previous_reference = calibrated_module.step5_path_reference
    previous_hard_tube = os.environ.get("R008_HOST_HARD_TUBE")
    had_hard_tube = "R008_HOST_HARD_TUBE" in os.environ
    os.environ["R008_HOST_HARD_TUBE"] = "0"
    verified: Any | None = None
    trace: V5LifecycleTraceAdapter | None = None
    opened = False
    injection_active = False
    try:
        verified = build_verified_mature_r006_writer(
            inputs,
            contract=contract,
            parent_contract=admission_parent,
            motion_profile=figure8_motion_profile(),
            path_reference=path_reference,
            path_evidence_collector_type=FigureEightPathEvidenceCollectorV1,
            home_binding=home_binding,
            fresh_frame_wait_policy=figure8_fresh_frame_wait_policy(),
        )
        raw_trace = attach_lifecycle_trace(
            verified.writer,
            run_root,
            path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
        )
        trace = V5LifecycleTraceAdapter(raw_trace)
        setattr(verified.writer, "_r013_lifecycle_trace", trace)
        base_prerequisites = verified.writer.prerequisites
        verified.writer.prerequisites = _V5MaturePrerequisites(base_prerequisites)
        verified.writer._runtime_protocol = V5_RUNTIME_PROTOCOL
        verified.writer._readable_runtime_identity = V5_READABLE_RUNTIME_IDENTITY
        verified.writer.session.identity = _R005SessionIdentityGate(verified.writer.contract)
        verified.writer._controller_transport = transport
        calibrated_module.step5_path_reference = path_reference
        verified.injection.activate()
        injection_active = True
        verified.writer.open(live_ack=LIVE_ACK)
        opened = True
    except Exception:
        if verified is not None:
            if opened:
                try:
                    verified.writer.close()
                except Exception:
                    pass
            if injection_active:
                try:
                    verified.injection.deactivate()
                except Exception:
                    pass
        calibrated_module.step5_path_reference = previous_reference
        if had_hard_tube:
            assert previous_hard_tube is not None
            os.environ["R008_HOST_HARD_TUBE"] = previous_hard_tube
        else:
            os.environ.pop("R008_HOST_HARD_TUBE", None)
        raise
    assert verified is not None and trace is not None

    closed = False

    def close() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        errors: list[Exception] = []
        try:
            verified.writer.close()
        except Exception as exc:
            errors.append(exc)
        try:
            verified.injection.deactivate()
        except Exception as exc:
            errors.append(exc)
        calibrated_module.step5_path_reference = previous_reference
        if had_hard_tube:
            assert previous_hard_tube is not None
            os.environ["R008_HOST_HARD_TUBE"] = previous_hard_tube
        else:
            os.environ.pop("R008_HOST_HARD_TUBE", None)
        if errors:
            raise V5LiveOwnerError("V5 context cleanup failed") from errors[0]

    return V5LiveContextV1(
        writer=verified.writer,
        transport=transport,
        trace=trace,
        backend_factory=mature_backend_factory(
            path_reference=path_reference,
            guard_stack=guard_stack,
        ),
        anchor_pose=anchor_pose,
        close_callback=close,
        timing_scheduler_profile=selected_timing_scheduler_profile.profile_id,
    )


__all__ = [
    "ACTIVATION_DURABILITY_TIMEOUT_S",
    "MAX_CHAIN_ATTEMPTS",
    "MAX_CHAIN_ROLLOVERS",
    "V5ActivationDurabilityBarrierV1",
    "V5ActivationDurableBatchReceiptV1",
    "V5ActivationBarrierSettlementV1",
    "V5ActivationJournalEntryV1",
    "V5PendingActivationV1",
    "V5EventDescriptorV1",
    "V5LifecycleTraceAdapter",
    "V5LiveChainRequestV1",
    "V5LiveContextV1",
    "V5LiveOwnerError",
    "V5RolloverCoordinatorV1",
    "V5RolloverActivationJournalV1",
    "V5RolloverDecisionV1",
    "V5RolloverEchoKind",
    "V5RolloverEchoResolutionV1",
    "V5RolloverPhase",
    "V5SealedChainResultV1",
    "V5SingleWriterOwnerV1",
    "V5_LIVE_OWNER_SCHEMA",
    "V5_LIVE_OWNER_VERSION",
    "V5_LIVE_BINDING_SCHEMA",
    "V5_ACTIVATION_JOURNAL_SCHEMA",
    "V5_ACTIVATION_JOURNAL_VERSION",
    "V5_SEALED_RESULT_SCHEMA",
    "V5_SEALED_RESULT_VERSION",
    "V5_READABLE_RUNTIME_IDENTITY",
    "V5_RUNTIME_PROTOCOL",
    "build_v5_live_context",
    "candidate_identity_from_plan",
    "runtime_spec_from_plan",
    "seal_v5_chain",
    "persist_sealed_chain_result",
]
