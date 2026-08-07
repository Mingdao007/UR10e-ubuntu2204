"""r008 live adapter: phase plan and absolute-threshold completion over r006."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import hashlib
import json
import math
import os
import queue
import statistics
import threading
import time
import uuid

from step5d_autotune_v4_r004.wire import AttemptKind as R004AttemptKind
from step5d_autotune_v4_r004.evidence import PathSample, QualificationEvidence
from step5d_autotune_v4_r005.live_adapter import (
    R005LiveAdapterError,
    R005LiveWriterAdapter,
    R005LiveRuntimePort,
)
from step5d_autotune_v4_r005.observations import ObservationLedger
from step5d_autotune_v4_r005.optimizer import OptimizerAsk
from step5d_autotune_v4_r005.queue import QueueError
from step5d_autotune_v4_r005.runtime import (
    Attempt,
    AttemptOutcome,
    AttemptResult,
    CampaignPhase,
    RuntimeErrorR005,
)
from step5d_force_objective import (
    FORMAL_END_S,
    FORMAL_START_S,
    ForceObjectiveBuilder,
    TARGET_FORCE_N,
)
from step5d_autotune_v4_r008.early_abort_kappa import sigmoid_kappa
from step5d_autotune_v4_r006.live_adapter import (
    R005_LIVE_ACK,
    R006Candidate,
    R006HostLoop,
    R006LiveAdapter,
    R006LiveAdapterError,
    R006MatureWriter,
    R006ObservationLedger,
    R006ProductionOptimizer,
    R006V3DurableQueueAdapter,
    _candidate_from_point,
    _point_from_candidate,
    build_verified_mature_r006_writer,
)
from step5d_autotune_v4_r006.lattice import IMode, ParameterPoint, neighbors
from step5d_autotune_v4_r006.optimizer import OptimizerError

from .async_seal import (
    AsyncSealPipeline,
    SealJob,
    kind_holds_seal_until_path60,
    phase_needs_sync_seal,
)
from .body_reconcile import append_reconcile_for_attempt
from .early_abort_penalty import record_early_abort_for_run
from .early_abort_shadow import (
    GUARD_FRAC,
    KAPPA_END,
    KAPPA_START,
    MIDPOINT,
    STEEPNESS,
    progress_fraction as early_abort_progress_fraction,
    resolve_early_abort_mode,
    should_trigger_early_abort,
)
from .gil_isolation import run_in_fork
from .hard_stop_penalty import (
    exception_is_recoverable_hard_stop,
    exception_looks_like_hard_stop,
    extract_reason_code,
    has_penalty_for_dispatch,
    load_penalties,
    recoverable_streak_since_last_success,
    record_hard_stop_penalty_for_run,
)
from .seal_client import SealDaemonClient, kill_stray_seal_daemons
from .design import live_phase_margin_unstable, phase_margin_deg
from .design_binding import anchor_from_document, box_from_document, load_domain_design
from .lattice import (
    BoxRegion,
    PIN_TAU_S,
    confirmed_dangerous_ki,
    live_acquisition_unstable,
    pin_parameter_point_tau,
    point_from_physical,
    r006_anchor_point,
    scrambled_sobol,
)
from .optimizer import CANDIDATE_SOBOL, propose_candidates
from .path_xy import append_path_xy_sidecar, format_path_xy_event, path_xy_summary_from_record
from .path_entry_rate_limit import (  # noqa: F401 — Phase B HOOK_POINT surface
    ENV_FLAG as R008_PATH_ENTRY_RATE_LIMIT_ENV,
    HOOK_POINT as PATH_ENTRY_RATE_LIMIT_HOOK_POINT,
    PathEntryRateLimitConfig,
    PathEntryRateLimitRamp,
)
from .phase_timings import (
    append_phase_timings_sidecar,
    attach_phase_timings_to_result,
    build_phase_timings_s,
    parse_phase_event,
    wrap_path_sample_sink_for_stage25,
)
from .queue import R008DurableQueueAdapter
from .staircase import (
    STAIRCASE_USE_P_UP_KI_LOCK,
    build_staircase,
    edge_half_anchor,
)
from .state20_search_trace import attach_state20_trace
from .state25_path_trace import attach_state25_trace
from .timing import qualification_timing_metrics

OPERATOR_NEXT_SCHEMA = "step5d.autotune-v4/r008-operator-next-v1"
OPERATOR_NEXT_NAME = "r008_operator_next.json"

# I-on exploration stays locked unless R008_ALLOW_I_ON=1 *and* a sealed trial
# shows usable force tracking (mae_n <= I_ON_ENQUEUE_MAE_N). Default is I-off-only
# so early SPACEFILL/BO cannot bang-bang (e.g. hard_abs_normal_60n on limit150).
I_ON_ENQUEUE_MAE_N = 2.0
I_ON_ALLOW_ENV = "R008_ALLOW_I_ON"
_I_ON_GATED_KINDS = frozenset({"STAIRCASE", "SPACEFILL", "BO_TRIAL"})
# Tau pin: same gated kinds as I-off. feature_map keeps log2(tau); BO/SPACEFILL
# no longer vary it (overnight evidence preferred ~0.05 / lattice snap).
_TAU_PIN_GATED_KINDS = frozenset({"STAIRCASE", "SPACEFILL", "BO_TRIAL"})


class R008LiveWriterAdapter(R005LiveWriterAdapter):
    """r005 writer adapter with QUAL timing_evidence retained on AttemptResult.

    Also hosts SHADOW-ONLY early-abort observe wiring. Active motion abort is
    never implemented here (no ``_send_safe_stop``, no raise-to-stop).
    """

    def __init__(self, writer: Any, *, contract: Any) -> None:
        super().__init__(writer, contract=contract)
        self._ea_mode: str = "off"
        self._ea_armed: bool = False
        self._ea_best_so_far_mae_n: float | None = None
        self._ea_dispatch_sequence: int | None = None
        self._ea_attempt_sequence: int | None = None
        self._ea_kind: str = ""
        self._ea_point_key: list[Any] = []
        self._ea_candidate: dict[str, Any] | None = None
        self._ea_run_dir: Path | None = None
        self._ea_execution_id: str | None = None
        self._ea_fired: bool = False
        self._ea_active_blocked_logged: bool = False
        self._ea_events: list[str] | None = None
        # Local unbinned formal-window |F-target| mean (r008-only). Kept out of
        # ForceObjectiveBuilder so the r006 source-closure pin stays intact.
        self._ea_partial_abs_error_sum: float = 0.0
        self._ea_partial_count: int = 0

    def clear_early_abort_shadow(self) -> None:
        """Disarm shadow early-abort for the next PATH (e.g. QUAL)."""

        self._ea_armed = False
        self._ea_best_so_far_mae_n = None
        self._ea_dispatch_sequence = None
        self._ea_attempt_sequence = None
        self._ea_kind = ""
        self._ea_point_key = []
        self._ea_candidate = None
        self._ea_run_dir = None
        self._ea_execution_id = None
        self._ea_fired = False
        self._ea_active_blocked_logged = False
        self._ea_mode = "off"
        self._ea_partial_abs_error_sum = 0.0
        self._ea_partial_count = 0

    def arm_early_abort_shadow(
        self,
        *,
        best_so_far_mae_n: float | None,
        dispatch_sequence: int,
        attempt_sequence: int | None,
        kind: str,
        point_key: Sequence[Any],
        candidate: Mapping[str, Any] | None,
        run_dir: Path | str | None,
        execution_id: str | None = None,
        events: list[str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Snapshot dispatch-time context for shadow early-abort (no I/O)."""

        mode = resolve_early_abort_mode(environ)
        self._ea_events = events
        self._ea_mode = mode
        self._ea_fired = False
        self._ea_active_blocked_logged = False
        self._ea_dispatch_sequence = int(dispatch_sequence)
        self._ea_attempt_sequence = (
            None if attempt_sequence is None else int(attempt_sequence)
        )
        self._ea_kind = str(kind)
        self._ea_point_key = list(point_key)
        self._ea_candidate = dict(candidate) if candidate else None
        self._ea_run_dir = None if run_dir is None else Path(run_dir)
        self._ea_execution_id = execution_id
        if mode == "off":
            self._ea_armed = False
            self._ea_best_so_far_mae_n = None
            return
        if mode == "active":
            # STOP_FOR_ANDY: treat as shadow; never abort motion.
            self._log_early_abort(
                "R008_EARLY_ABORT_ACTIVE_BLOCKED:await_andy_channel"
            )
            self._ea_active_blocked_logged = True
        kind_s = str(kind)
        if kind_s == "QUALIFICATION":
            self._ea_armed = False
            self._ea_best_so_far_mae_n = None
            return
        if best_so_far_mae_n is None:
            self._ea_armed = False
            self._ea_best_so_far_mae_n = None
            return
        best = float(best_so_far_mae_n)
        if not math.isfinite(best) or best <= 0.0:
            self._ea_armed = False
            self._ea_best_so_far_mae_n = None
            return
        self._ea_best_so_far_mae_n = best
        self._ea_partial_abs_error_sum = 0.0
        self._ea_partial_count = 0
        self._ea_armed = True

    def _log_early_abort(self, message: str) -> None:
        sink = self._ea_events
        if sink is not None:
            sink.append(message)
        print(message, flush=True)

    def _evaluate_early_abort_shadow(self, path_time_s: float) -> None:
        """O(1) shadow decision; record once. Never stops motion."""

        if not self._ea_armed or self._ea_fired:
            return
        if self._ea_mode == "off":
            return
        best = self._ea_best_so_far_mae_n
        if best is None:
            return
        run_dir = self._ea_run_dir
        dispatch = self._ea_dispatch_sequence
        if run_dir is None or dispatch is None:
            return
        progress = early_abort_progress_fraction(
            path_time_s,
            formal_start_s=FORMAL_START_S,
            formal_end_s=FORMAL_END_S,
        )
        if progress < GUARD_FRAC:
            return
        if self._ea_partial_count <= 0:
            return
        partial_f = self._ea_partial_abs_error_sum / float(self._ea_partial_count)
        if not math.isfinite(partial_f):
            return
        kappa = float(
            sigmoid_kappa(
                progress,
                guard_frac=GUARD_FRAC,
                kappa_start=KAPPA_START,
                kappa_end=KAPPA_END,
                midpoint=MIDPOINT,
                steepness=STEEPNESS,
            )
        )
        if not should_trigger_early_abort(
            progress=progress,
            partial_mae_n=partial_f,
            best_so_far_mae_n=best,
            kappa=kappa,
            guard_frac=GUARD_FRAC,
        ):
            return
        # Mark fired before I/O so a slow/failed write cannot re-enter.
        self._ea_fired = True
        try:
            row = record_early_abort_for_run(
                run_dir,
                dispatch_sequence=int(dispatch),
                attempt_sequence=self._ea_attempt_sequence,
                kind=self._ea_kind or "BO_TRIAL",
                point_key=self._ea_point_key,
                candidate=self._ea_candidate,
                partial_mae_n_at_trigger=partial_f,
                best_so_far_mae_n=best,
                kappa_at_trigger=kappa,
                trigger_progress_fraction=progress,
                enters_gp_training=False,
                execution_id=self._ea_execution_id,
            )
        except Exception as exc:  # noqa: BLE001 — never raise into motion thread
            self._log_early_abort(f"R008_EARLY_ABORT_SHADOW_ERROR:{exc}")
            return
        if row is None:
            self._log_early_abort(
                f"R008_EARLY_ABORT_SHADOW_DEDUP:dispatch={int(dispatch)}"
            )
            return
        self._log_early_abort(
            "R008_EARLY_ABORT_SHADOW:"
            f"dispatch={int(dispatch)}:"
            f"progress={progress:.4f}:"
            f"partial={partial_f:.6g}:"
            f"best={best:.6g}:"
            f"kappa={kappa:.6g}:"
            f"penalty={float(row['penalty_mae_n']):.6g}"
        )

    def observe_r004_path_sample(self, sample: PathSample) -> None:
        super().observe_r004_path_sample(sample)
        path_time = getattr(sample, "path_time_s", None)
        if path_time is None:
            return
        try:
            path_time_f = float(path_time)
        except (TypeError, ValueError):
            return
        if not math.isfinite(path_time_f):
            return
        if self._ea_armed and FORMAL_START_S <= path_time_f < FORMAL_END_S:
            force = getattr(sample, "filtered_normal_n", None)
            try:
                force_f = float(force)
            except (TypeError, ValueError):
                force_f = float("nan")
            if math.isfinite(force_f):
                self._ea_partial_abs_error_sum += abs(force_f - float(TARGET_FORCE_N))
                self._ea_partial_count += 1
        # Shadow-only: never stop / raise / call _send_safe_stop.
        self._evaluate_early_abort_shadow(path_time_f)

    def run_60s(self, attempt: Attempt) -> AttemptResult:
        kind = getattr(attempt, "kind", None)
        kind_s = kind.value if hasattr(kind, "value") else str(kind or "")
        if kind_s != "QUALIFICATION":
            return super().run_60s(attempt)
        # Mirror the frozen QUAL branch, but keep layered timing diagnostics.
        self._objective_builder = ForceObjectiveBuilder()
        self._path_samples.clear()
        self._last_joint_evidence = None
        self._last_rtde_frame_identity = None
        self._rtde_sequence = 0
        raw = self.writer.run_60s(attempt)
        if isinstance(raw, AttemptResult):
            return raw
        if not isinstance(raw, QualificationEvidence):
            raise R005LiveAdapterError(
                "r008 QUAL writer returned an unknown evidence type"
            )
        return AttemptResult(
            epoch=attempt.epoch,
            attempt_sequence=attempt.attempt_sequence,
            kind=attempt.kind,
            candidate=attempt.candidate,
            safe_return=raw.return_gate_passed,
            binding_ok=True,
            safety_gate=raw.safety_gate_passed,
            contact_gate=raw.contact_gate_passed,
            return_gate=raw.return_gate_passed,
            motion_gate=False,
            timing_gate=raw.timing_gate_passed,
            identity_gate=True,
            qualification_passed=raw.qualification_passed,
            duration_s=0.0,
            metrics=qualification_timing_metrics(raw),
            execution_id=attempt.execution_id,
        )


def should_dispatch_pending_before_seal_join(pending_count: int) -> bool:
    """True → prepare_next from existing pending and overlap seal with search."""

    return int(pending_count) > 0


PENDING_WATERMARK = 2


@dataclass(frozen=True)
class HostMailboxIntent:
    """Seal-worker → motion-thread side effects (queue/phase ownership)."""

    reason: str = ""
    cancel_pending: bool = False
    stop_reason: str | None = None
    phase: Any = None
    retest_candidate: Any = None
    clear_retest_records: bool = False
    anchor_verdict: str | None = None
    log2_pd_center: float | None = None
    rebind_half_width: float = 1.5
    close_staircase: bool = False
    event: str | None = None


class R008LiveAdapterError(R006LiveAdapterError):
    """r008 live adapter refused an unsafe transition."""


def _sidecar_prefix_bytes(data: bytes, requested_ids: set[tuple[int, str]]) -> int:
    """Byte offset just past the last sidecar line referenced by ``requested_ids``.

    See ``R008ObservationLedger.sidecar_binding`` docstring for why this
    exists. Mirrored (not shared) on the worker side in
    ``bounded_worker_artifact_binding.py`` -- the worker trusts the byte
    offset the host computed here rather than re-deriving it, since the host
    is the only side that knows which rows this particular binding needs.
    """

    if not requested_ids:
        return len(data)
    remaining = set(requested_ids)
    offset = 0
    prefix = 0
    for line in data.splitlines(keepends=True):
        offset += len(line)
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        attempt_sequence = row.get("attempt_sequence")
        execution_id = row.get("execution_id")
        if attempt_sequence is None or execution_id is None:
            continue
        ident = (int(attempt_sequence), str(execution_id))
        if ident in remaining:
            remaining.discard(ident)
            prefix = offset
    if remaining:
        raise R006LiveAdapterError(
            f"r006 raw artifact sidecar is missing referenced rows: {sorted(remaining)}"
        )
    return prefix


class R008ObservationLedger(R006ObservationLedger):
    """r006 sidecar ledger that skips full fresh-ledger replay on the hot path.

    Each non-QUAL append already ran a fresh-process *artifact* verify.  Replaying
    every prior raw PATH blob after every seal is O(n²) and trips the frozen 15 s
    budget around ~20 formal rows.  Cold resume still uses
    ``ObservationLedger.fresh_process_verify`` (with the r008 timeout scope).
    """

    def fresh_process_verify(self) -> tuple[Mapping[str, Any], ...]:
        rows = self.sidecar.fresh_process_verify()
        paired = {
            (record.attempt_sequence, record.metrics.get("execution_id"))
            for record in self.ledger.records
            if record.kind != "QUALIFICATION"
        }
        return tuple(
            row
            for row in rows
            if (row["attempt_sequence"], row["execution_id"]) in paired
        )

    def sidecar_binding(self) -> dict[str, Any]:
        """r008 override: prefix-scope the sidecar digest (2026-08-07 fix).

        The frozen ``R006ObservationLedger.sidecar_binding`` hashes the
        *whole* sidecar file, then ships that snapshot digest through the
        keepalive pipe for the worker to re-check. Any concurrent append
        between this snapshot and the worker's re-read -- even an unrelated
        new sealed row from the async seal pipeline -- flips the digest and
        fail-closes the whole host (``r006 artifact sidecar bytes differ``,
        confirmed live once in the 025535_b3_phase5_raw2 run). The sidecar is
        append-only, so scoping the digest to a prefix that only needs to
        cover the rows this binding actually references removes the race:
        appends after that prefix never touch it, and the prefix itself never
        changes once written. Never edits the frozen r006 file (source
        closure hash-pinned); this class already exists to override hot-path
        behavior over it.
        """

        rows = self.fresh_process_verify()
        sidecar = self.raw_sidecar_path
        if not sidecar.is_file() or sidecar.is_symlink():
            raise R006LiveAdapterError("r006 raw artifact sidecar is unavailable")
        data = sidecar.read_bytes()
        requested_ids = {
            (int(row["attempt_sequence"]), str(row["execution_id"])) for row in rows
        }
        prefix_bytes = _sidecar_prefix_bytes(data, requested_ids)
        return {
            "sidecar_path": str(sidecar.resolve(strict=True)),
            "sidecar_sha256": hashlib.sha256(data[:prefix_bytes]).hexdigest(),
            "sidecar_prefix_bytes": prefix_bytes,
            "campaign_fingerprint": self.campaign_fingerprint,
            "rows": [
                {"attempt_sequence": int(row["attempt_sequence"]), "execution_id": str(row["execution_id"])}
                for row in rows
            ],
        }


@dataclass(frozen=True)
class PhaseSpec:
    kind: str
    count: int


DEFAULT_PHASE_PLAN: tuple[PhaseSpec, ...] = (
    PhaseSpec("QUALIFICATION", 3),
    PhaseSpec("ANCHOR", 3),
    PhaseSpec("STAIRCASE", 6),
    PhaseSpec("SPACEFILL", 24),
    PhaseSpec("BO", 0),  # rolling
    PhaseSpec("RETEST", 3),
)


def _clamp_log2_pd_center(
    box: BoxRegion, center_log2_pd: float, *, half_width: float = 1.5
) -> tuple[float, bool]:
    """Clamp a rebind center so ±half_width still fits inside the Stage-B box.

    Live 170124 wrote ``log2_pd_center≈-9.29`` (above ``log2_pd_max≈-11.29``);
    without clamping, ``_narrow_box_around_pd`` inverted the interval and
    silently fell back to the **full** box — SPACEFILL never narrowed.
    """

    span = float(box.log2_pd_max) - float(box.log2_pd_min)
    hw = float(half_width)
    if not math.isfinite(span) or span <= 0.0 or not math.isfinite(hw) or hw < 0.0:
        return float(center_log2_pd), False
    if span < 2.0 * hw:
        # Box too narrow for a full ±half_width window; leave center unchanged
        # and let ``_narrow_box_around_pd`` fall back to the full box.
        return float(center_log2_pd), False
    lo = float(box.log2_pd_min) + hw
    hi = float(box.log2_pd_max) - hw
    clamped = min(hi, max(lo, float(center_log2_pd)))
    return clamped, clamped != float(center_log2_pd)


def _narrow_box_around_pd(box: BoxRegion, center_log2_pd: float, *, half_width: float = 1.5) -> BoxRegion:
    """Shrink a box's P/D range to ``center_log2_pd`` ± ``half_width`` octaves.

    Used to rebind the SPACEFILL/BO search domain around a staircase-validated
    (or predicted-unstable) edge instead of re-sampling the whole Stage-B box,
    which otherwise keeps wasting live attempts on already-known-bad P/D.

    Out-of-box centers are **clamped** into the actionable band first so rebind
    still yields a real sub-box (preferred over a silent full-box fallback).
    Full-box fallback remains only when the Stage-B span is narrower than
    ``2 * half_width``.
    """

    center, _clamped = _clamp_log2_pd_center(box, center_log2_pd, half_width=half_width)
    lo = max(box.log2_pd_min, center - half_width)
    hi = min(box.log2_pd_max, center + half_width)
    if hi <= lo:
        lo, hi = box.log2_pd_min, box.log2_pd_max
    return BoxRegion(
        log2_pd_min=lo,
        log2_pd_max=hi,
        log2_d_min=box.log2_d_min,
        log2_d_max=box.log2_d_max,
        log2_tau_min=box.log2_tau_min,
        log2_tau_max=box.log2_tau_max,
        kf_off_allowed=box.kf_off_allowed,
        log2_kf_min=box.log2_kf_min,
        log2_kf_max=box.log2_kf_max,
        log2_ko_min=box.log2_ko_min,
        log2_ko_max=box.log2_ko_max,
        log2_kp_min=box.log2_kp_min,
        log2_kp_max=box.log2_kp_max,
    )


class R008MatureWriter(R006MatureWriter):
    """Mature writer with r008 phase-plan kinds mapped onto the frozen wire enums."""

    @staticmethod
    def _kind(kind: str) -> R004AttemptKind:
        mapping = {
            "QUALIFICATION": R004AttemptKind.QUALIFICATION,
            "BOOTSTRAP_PD": R004AttemptKind.BATCH_A,
            "P0_NOCONTACT": R004AttemptKind.BATCH_A,
            "STAIRCASE": R004AttemptKind.BATCH_A,
            "WARM_START_1": R004AttemptKind.BATCH_A,
            "ROUTE": R004AttemptKind.BATCH_A,
            "SPACEFILL": R004AttemptKind.BATCH_B,
            "WARM_START_2": R004AttemptKind.BATCH_B,
            "BO": R004AttemptKind.BATCH_B,
            "BO_TRIAL": R004AttemptKind.BATCH_B,
            "RETEST": R004AttemptKind.RETEST,
            "ANCHOR": R004AttemptKind.BATCH_A,
        }
        try:
            return mapping[kind]
        except (KeyError, TypeError) as exc:
            raise R008LiveAdapterError(f"r008 attempt kind is not mature-stack compatible: {kind!r}") from exc


class R008ProductionOptimizer(R006ProductionOptimizer):
    """Full-box Sobol candidate set with periodic GP refits owned by the worker."""

    def __init__(self, *args: Any, domain_path: str | Path, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        domain = load_domain_design(Path(domain_path))
        self._box = box_from_document(domain)
        self._domain_stiffness_n_per_m = float(domain["greybox_stiffness_n_per_m"])
        self._domain_rho = float(domain["greybox_rho"])
        self._sobol_seed = 8
        self._ask_count = 0
        # I-off lock default: native Sobol draws are all I-off (fraction=1.0).
        # Host raises to the historical 0.15 mix only after I-on unlock.
        self._include_kf_off_fraction = 1.0

    def ask(
        self,
        *,
        observations: Sequence[Any],
        pending: Sequence[Any],
        incumbent: Any,
        q: int = 1,
    ) -> OptimizerAsk:
        if q not in (1, 4):
            raise OptimizerError("r008 scheduler q must be 4 or 1")
        incumbent_point = _point_from_candidate(incumbent)
        pending_points = {_point_from_candidate(candidate) for candidate in pending}
        # Sealed trainable keys already in the GP train set must not re-enter
        # the choice pool (pending exclusion alone is not enough — 181153
        # re-sampled the same D≈16–20 pocket after SPACEFILL).
        trained_points = {
            _point_from_candidate(record.candidate)
            for record in observations
            if getattr(record, "sealed", False)
            and getattr(record, "eligible", False)
            and hasattr(record, "candidate")
        }
        # Fresh Sobol cover of the Stage-B box ∪ local neighbors.
        # Tau is pinned (Sobol ignores free tau draw; neighbors rewritten).
        sobol = propose_candidates(
            self._box,
            count=CANDIDATE_SOBOL,
            seed=self._sobol_seed + self._ask_count,
            include_kf_off_fraction=float(self._include_kf_off_fraction),
            pin_tau=True,
        )
        local = tuple(
            pin_parameter_point_tau(point) for point in neighbors(incumbent_point)
        )
        choices = tuple(
            point
            for point in (*sobol, *local)
            if point not in pending_points
            and point not in trained_points
            and not live_acquisition_unstable(
                force_damping=point.d_gain,
                normal_filter_tau_s=point.tau_s,
            )
            and not confirmed_dangerous_ki(force_i_gain=point.i_gain)
            and not live_phase_margin_unstable(
                force_p_gain=point.p_gain,
                force_damping=point.d_gain,
                force_i_gain=point.i_gain,
                normal_filter_tau_s=point.tau_s,
                stiffness_n_per_m=self._domain_stiffness_n_per_m,
                rho=self._domain_rho,
            )
        )
        if len(choices) < q:
            raise OptimizerError("r008 box frontier is exhausted")
        # Phase 2 ask no-join: barrier records sealed-rows-only (does not join_all).
        # BO/ask binds the current durable sidecar; in-flight seals appear on a
        # later ask (lag ≥1). preHOME/search-critical joins unchanged.
        barrier = getattr(self, "_pre_artifact_binding_barrier", None)
        if callable(barrier):
            barrier()
        self.client.update_artifact_binding(self.r006_ledger.sidecar_binding())
        ask = self.client.ask(
            choices=choices,
            pending=tuple(pending_points),
            incumbent=incumbent_point,
            q=q,
        )
        self._ask_count += 1
        candidate = _candidate_from_point(ask.point)
        self.last_ask_metadata = dict(ask.metadata)
        return OptimizerAsk(candidate=candidate, metadata=self.last_ask_metadata)

    def tell(self, record: Any) -> None:
        super().tell(record)


def apply_timing_canary_skip_qual(host: Any) -> None:
    """Skip formal 3×QUAL and start at ANCHOR (timing canary only).

    Formal default remains 3×QUAL; this helper is opt-in via ``timing_canary``.
    """

    host.qualification_passes = 3
    host.phase = R008HostLoop._ANCHOR
    if hasattr(host, "_qual_scheduled"):
        host._qual_scheduled = max(int(getattr(host, "_qual_scheduled", 0) or 0), 3)
    events = getattr(host, "events", None)
    if isinstance(events, list):
        events.append("R008_TIMING_CANARY:skip_qual")
    # Full HostLoop instances sync the runtime gate; bare test objects may omit it.
    if hasattr(host, "runtime"):
        sync = getattr(host, "_sync_qualification_state", None)
        if callable(sync):
            sync()


class R008HostLoop(R006HostLoop):
    """Qualification → anchor-repeat → staircase → spacefill → BO → absolute-threshold RETEST."""

    _ANCHOR = "ANCHOR"
    _STAIRCASE = "STAIRCASE"
    _SPACEFILL = "SPACEFILL"

    # Consecutive candidate-specific hard-stops (reason_code 61/62/63) allowed
    # to seal a penalty and continue before the (N+1)th escalates to the
    # normal fatal _stop(). Counts consecutive only -- any attempt that
    # reaches queue.complete() (i.e. did not hard-stop) resets it to 0, so a
    # long healthy overnight run is not penalized for two unrelated,
    # widely-separated incidents. Exists specifically because closing the
    # phase_margin_deg blind spot around Ki≈0.00181 (2026-08-06) is not yet
    # verified live -- this is the backstop if that gap repeats.
    RECOVERABLE_HARD_STOP_LIMIT = 2

    def __init__(
        self,
        *,
        domain_path: str | Path,
        r006_contract: Any,
        r006_ledger: R006ObservationLedger,
        **kwargs: Any,
    ) -> None:
        # Pop before super: r005 HostLoop rejects unknown kwargs.
        timing_canary = bool(kwargs.pop("timing_canary", False))
        # Live path passes seal_daemon=True (off-host sole writer). Unit tests
        # keep the default False in-process fork path.
        use_seal_daemon = bool(kwargs.pop("seal_daemon", False))
        self._timing_canary = timing_canary
        self._use_seal_daemon = use_seal_daemon
        self._seal_daemon: SealDaemonClient | None = None
        self._domain = load_domain_design(Path(domain_path))
        self._box = box_from_document(self._domain)
        self._anchor = anchor_from_document(self._domain)
        self._mae_threshold_n = float(self._domain["application_mae_threshold_n"])
        self._staircase = build_staircase(self._anchor)
        # A staircase edge/preemptive veto narrows self._box in-memory (see
        # _persist_domain_rebind below); without persisting that decision, a
        # process restart/resume silently forgets it, rebuilds the full
        # static-domain 24-point SPACEFILL set, and the campaign never
        # recognizes SPACEFILL as complete -- observed live 2026-08-03 (42
        # SPACEFILL rows recorded against a 24-point budget, including one
        # relapse from BO back into SPACEFILL after a resume).
        self._domain_rebind_path_value = self._domain_rebind_path(kwargs.get("ledger"))
        rebind = self._load_domain_rebind(self._domain_rebind_path_value)
        if rebind is not None:
            self._box = _narrow_box_around_pd(
                self._box, float(rebind["log2_pd_center"]), half_width=float(rebind.get("half_width", 1.5))
            )
        self._spacefill = scrambled_sobol(
            self._box,
            count=24,
            seed=8,
            include_kf_off_fraction=1.0,
        )
        self._staircase_count = 0
        self._spacefill_count = 0
        self._last_staircase_mae: float | None = None
        # Stage D2 level-0 gate: re-measure the frozen r006 anchor live before
        # trusting the r008 anchor/staircase at all. See lattice.r006_anchor_point.
        anchor_cfg = self._domain.get("anchor_validation") or {}
        self._anchor_point = r006_anchor_point()
        self._anchor_target = int(anchor_cfg.get("repeats", 3))
        self._anchor_expected_mae_n = float(anchor_cfg.get("expected_mae_n", 5.58))
        self._anchor_tolerance_n = float(anchor_cfg.get("tolerance_n", 0.5))
        self._anchor_max_attempts = int(anchor_cfg.get("max_attempts", self._anchor_target * 3))
        self._anchor_count = 0
        self._anchor_values: list[float] = []
        self._anchor_verdict: str | None = None
        # Motion-owned enqueue cursors (continuous-run): refill indexes these,
        # never sealed counts + pending (that double-count / re-queue without join).
        self._qual_scheduled = 0
        self._anchor_scheduled = 0
        self._staircase_scheduled = 0
        self._spacefill_scheduled = 0
        self._bo_scheduled = 0
        self._retest_scheduled = 0
        self._mailbox: queue.SimpleQueue[HostMailboxIntent] = queue.SimpleQueue()
        # Track A1: per-attempt marks owned here so r005/runtime.py stays inside
        # the frozen r006 source-closure digest. Thread-local storage: the
        # async-seal worker thread (async_seal.py) can still be sealing/telling
        # attempt N while the main motion thread has already moved on to
        # attempt N+1's HOME. Without per-thread isolation, the worker's
        # SEAL/TELL marks for N land in N+1's freshly-reset dict instead (and
        # vice versa for reads) -- a real cross-attempt data race confirmed
        # live 2026-08-04 (Wave3 seq11: R008_PHASE_TIMINGS logged attempt 11's
        # own tell with attempt 12's home=0.0/search=None/... state). See
        # _phase_state()/_seed_phase_state_for_worker() below.
        self._phase_state_local = threading.local()
        self._phase_marks = {}
        self._stage25_marked = False
        self._recoverable_hard_stop_streak = 0
        super().__init__(r006_contract=r006_contract, r006_ledger=r006_ledger, **kwargs)
        if self._use_seal_daemon:
            kill_stray_seal_daemons()
            self._seal_daemon = SealDaemonClient(
                ledger_path=Path(self.ledger.path),
                r006_sidecar_path=Path(r006_ledger.raw_sidecar_path),
                campaign_fingerprint=str(self.contract.campaign_fingerprint),
                eoat_sha256=str(self.ledger.eoat_sha256),
            )
            self.events.append("R008_SEAL_DAEMON:enabled")
        self._async_seal = AsyncSealPipeline(
            seal_fn=self._record_and_tell,
            advance_fn=self._advance_after_record,
            on_done=self._on_async_seal_done,
            # Canary "023243" (2026-08-05): disabling these for the off-host
            # daemon on the assumption that seal CPU living in another process
            # means the host never needs to gate/join was falsified live --
            # reason=43 recurred with async_seal_wall/seal_overlap≈9s and
            # blocked=0.0 (host never explicitly waited). Measured offline:
            # pickling a realistic ~650-sample AttemptResult host-side takes
            # ~0.3ms, so that specific mechanism is ruled out, but gating's
            # job was never "protect against this one operation" -- it is
            # "do not let search start while a seal job is still running,
            # whatever it is doing". That invariant does not become false
            # just because the job got 3x cheaper (fork ~30s -> daemon ~9s).
            # Daemon and fork share the same gating; do not special-case
            # offhost here again without new live evidence.
            join_executing_on_search_critical=True,
            search_critical_gates_seals=True,
        )
        # Ask hashes the objectives sidecar; wire a drain barrier so binding
        # cannot race an in-flight async seal/tell append.
        optimizer = getattr(self, "optimizer", None)
        if isinstance(optimizer, R008ProductionOptimizer):
            optimizer._pre_artifact_binding_barrier = self._join_seals_before_optimizer_ask
        self._hydrate_schedule_cursors()
        if self._timing_canary:
            apply_timing_canary_skip_qual(self)

    def _join_seals_before_optimizer_ask(self) -> None:
        """Ask no-join (Phase 2): bind against already-sealed sidecar rows only.

        In-flight seals may still be appending; BO/ask must use durable sealed
        rows (natural lag ≥1) — never 「占位预测分」as sealed score. Motion
        preHOME / search-critical joins are unchanged (reason=43).
        """

        async_seal = getattr(self, "_async_seal", None)
        if async_seal is None:
            return
        # Intentionally do not join_all(): ask proceeds on current sidecar bytes.
        # Contract: docs/r008_async_seal_contract.md (sealed MAE authority; lag ≥1 OK).
        self.events.append("R008_ASK_SEAL_NO_JOIN:sealed_rows_only")

    def _phase_state(self) -> dict[str, Any]:
        """Per-thread {marks, origin_s, last_s}; lazily created for new threads.

        Also tolerates instances built via ``object.__new__`` in tests (which
        skip ``__init__`` and never set ``_phase_state_local``).
        """

        local = self.__dict__.get("_phase_state_local")
        if local is None:
            local = threading.local()
            self._phase_state_local = local
        state = getattr(local, "state", None)
        if state is None:
            state = {"marks": {}, "origin_s": None, "last_s": None}
            local.state = state
        return state

    @property
    def _phase_marks(self) -> dict[str, float]:
        return self._phase_state()["marks"]

    @_phase_marks.setter
    def _phase_marks(self, value: dict[str, float]) -> None:
        self._phase_state()["marks"] = value

    @property
    def _phase_origin_s(self) -> float | None:
        # Shadows the frozen r005 HostLoop's plain instance attribute of the
        # same name (runtime.py:248) with a thread-local one; base-class code
        # in _begin_phase_clock()/_phase() still does plain `self.x = ...`,
        # which Python resolves through this descriptor since it inspects
        # type(self), not the defining class.
        return self._phase_state()["origin_s"]

    @_phase_origin_s.setter
    def _phase_origin_s(self, value: float | None) -> None:
        self._phase_state()["origin_s"] = value

    @property
    def _phase_last_s(self) -> float | None:
        return self._phase_state()["last_s"]

    @_phase_last_s.setter
    def _phase_last_s(self, value: float | None) -> None:
        self._phase_state()["last_s"] = value

    def _seed_phase_state_for_worker(self, snapshot: dict[str, Any]) -> None:
        """Run on the async-seal worker thread just before it seals a job.

        ``snapshot`` was captured on the main thread at submit() time (see
        run_one()), so the worker gets an isolated copy of that attempt's
        phase state instead of racing the main thread's live one.
        """

        self._phase_state_local.state = {
            "marks": dict(snapshot["marks"]),
            "origin_s": snapshot["origin_s"],
            "last_s": snapshot["last_s"],
        }

    def _begin_phase_clock(self) -> None:
        super()._begin_phase_clock()
        self._phase_marks = {}
        self._stage25_marked = False

    def _record_phase_mark_from_tail(self) -> None:
        parsed = parse_phase_event(self.events[-1]) if self.events else None
        if parsed is not None and parsed[0] not in self._phase_marks:
            self._phase_marks[parsed[0]] = parsed[1]

    def _phase(self, name: str) -> None:
        super()._phase(name)
        self._record_phase_mark_from_tail()
        # ARM complete ⇒ TP contact search begins inside the next run_60s call.
        if name == "ARM":
            super()._phase("CONTACT_SEARCH_START")
            self._record_phase_mark_from_tail()
            # begin_search_critical is intentionally NOT here: canaries
            # 025746/034742 died when a ~9s seal join ran after arm() while
            # the TP already needed continuous RTDE packets (post-ARM
            # reconnect is fail-closed). Join now runs in run_one before arm()
            # so prearm reconnect still applies.

    def notify_stage25_start(self) -> None:
        """PATH/stage-25 sample arrived: search ended, formal path window started."""

        if self._stage25_marked or "STAGE25_START" in self._phase_marks:
            return
        self._stage25_marked = True
        self._phase("STAGE25_START")
        # Same wall time; SEARCH_DONE aliases residual-table readers.
        if "SEARCH_DONE" not in self._phase_marks:
            self._phase("SEARCH_DONE")
        # Do NOT end_search_critical here: PATH60‖seal still trips reason=43
        # (2026-08-05). Prior seals release at SAFE_RETURN instead.

    def _assert_prepared_matches_ticket(self, attempt: Any, ticket: Any) -> None:
        """Before downward search: ticket / attempt / armed control agree.

        ``prepare_control`` is consumed inside ``writer.arm`` (CanonicalQualification
        construct) — do not require ``injection._prepared_key`` after ARM returns.
        Does not prove TP gains (search uses compile-time speedl); proves the
        host armed the same candidate the queue dispatched.
        """

        attempt_uid = str(getattr(attempt.candidate, "candidate_uid", ""))
        ticket_uid = str(
            getattr(ticket, "candidate_uid", "")
            or getattr(getattr(ticket, "candidate", None), "candidate_uid", "")
        )
        if not attempt_uid or attempt_uid != ticket_uid:
            raise RuntimeErrorR005(
                f"r008 search param gate: attempt/ticket candidate_uid mismatch "
                f"attempt={attempt_uid!r} ticket={ticket_uid!r}"
            )
        writer = getattr(self.runtime, "writer", None)
        control = getattr(writer, "_qualification_control", None) if writer is not None else None
        control_cand = getattr(control, "candidate", None) if control is not None else None
        control_uid = str(getattr(control_cand, "candidate_uid", "") or "")
        if control_uid and control_uid != attempt_uid:
            raise RuntimeErrorR005(
                f"r008 search param gate: armed control uid mismatch "
                f"control={control_uid!r} attempt={attempt_uid!r}"
            )
        self.events.append(f"R008_SEARCH_PARAM_GATE:uid={attempt_uid[:16]}")

    @staticmethod
    def _domain_rebind_path(ledger: Any) -> Path | None:
        path = getattr(ledger, "path", None)
        if path is None:
            return None
        return Path(path).resolve().parent / "r008_domain_rebind.json"

    @staticmethod
    def _load_domain_rebind(path: Path | None) -> dict[str, Any] | None:
        if path is None or not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or "log2_pd_center" not in payload:
            return None
        return payload

    def _persist_domain_rebind(self, log2_pd_center: float, *, source: str) -> None:
        path = getattr(self, "_domain_rebind_path_value", None)
        if path is None:
            return
        half_width = 1.5
        center = float(log2_pd_center)
        box = getattr(self, "_box", None)
        if isinstance(box, BoxRegion):
            center, clamped = _clamp_log2_pd_center(
                box, center, half_width=half_width
            )
            if clamped:
                self.events.append(
                    f"R008_DOMAIN_REBIND_CLAMPED:raw={float(log2_pd_center):.4g}:"
                    f"clamped={center:.4g}:source={source}"
                )
        payload = {
            "schema": "step5d.autotune-v4/r008-domain-rebind-v1",
            "log2_pd_center": center,
            "half_width": half_width,
            "source": source,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def _anchor_values_from_rows(self, rows: Sequence[Mapping[str, Any]]) -> list[float]:
        values: list[float] = []
        for row in rows:
            receipt = self._row_receipt(row)
            value = receipt.get("objective_mae_n")
            if isinstance(value, (int, float)) and math.isfinite(float(value)):
                values.append(float(value))
        return values

    def _compute_anchor_verdict(self) -> str | None:
        if len(self._anchor_values) < self._anchor_target:
            if self._anchor_count >= self._anchor_max_attempts:
                return "failed"
            return None
        median = statistics.median(self._anchor_values[-self._anchor_target :])
        if abs(median - self._anchor_expected_mae_n) <= self._anchor_tolerance_n:
            return "passed"
        return "failed"

    @staticmethod
    def _i_on_explicitly_allowed() -> bool:
        return os.environ.get(I_ON_ALLOW_ENV, "") == "1"

    def _has_i_on_unlock_observation(self) -> bool:
        """True only with R008_ALLOW_I_ON=1 and a sealed mae_n <= I_ON_ENQUEUE_MAE_N."""
        if not self._i_on_explicitly_allowed():
            return False
        records = getattr(self.ledger, "records", ()) or ()
        for record in records:
            if not bool(getattr(record, "sealed", False)):
                continue
            mae = getattr(record, "mae_n", None)
            if mae is None:
                continue
            try:
                if float(mae) <= I_ON_ENQUEUE_MAE_N:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _sobol_include_kf_off_fraction(self) -> float:
        """While I-off locked, draw Sobol as 100% native I-off (fraction=1.0)."""

        return 0.15 if self._has_i_on_unlock_observation() else 1.0

    def _sync_optimizer_sobol_fraction(self) -> None:
        optimizer = getattr(self, "optimizer", None)
        if optimizer is not None and hasattr(optimizer, "_include_kf_off_fraction"):
            optimizer._include_kf_off_fraction = float(self._sobol_include_kf_off_fraction())

    def _spacefill_points(self) -> tuple[Any, ...]:
        return scrambled_sobol(
            self._box,
            count=24,
            seed=8,
            include_kf_off_fraction=self._sobol_include_kf_off_fraction(),
        )

    def _bo_incumbent_candidate(self) -> Any:
        """Argmin sealed trainable I-off MAE in the current epoch; else cursor.

        Must not use pending[-1] — that locked 181153 BO onto the last
        SPACEFILL dispatch (D≈16.6) instead of the sealed elite (D≈188).
        """

        best: Any | None = None
        best_mae: float | None = None
        records = getattr(self.ledger, "records", ()) or ()
        for record in records:
            if int(getattr(record, "epoch", -1)) != int(self.epoch):
                continue
            if not bool(getattr(record, "sealed", False)):
                continue
            if not bool(getattr(record, "eligible", False)):
                continue
            candidate = getattr(record, "candidate", None)
            if candidate is None:
                continue
            if not bool(getattr(candidate, "i_off", False)):
                continue
            mae = getattr(record, "mae_n", None)
            if mae is None:
                continue
            try:
                mae_f = float(mae)
            except (TypeError, ValueError):
                continue
            if best_mae is None or mae_f < best_mae:
                best_mae = mae_f
                best = candidate
        if best is None:
            return self.cursor
        return best

    def _bo_incumbent_mae_n(self) -> float | None:
        """In-memory sealed I-off rolling min MAE for early-abort snapshot.

        Same filter as ``_bo_incumbent_candidate``; returns None when no sealed
        trainable incumbent exists yet (no per-sample ledger I/O).
        """

        best_mae: float | None = None
        records = getattr(self.ledger, "records", ()) or ()
        for record in records:
            if int(getattr(record, "epoch", -1)) != int(self.epoch):
                continue
            if not bool(getattr(record, "sealed", False)):
                continue
            if not bool(getattr(record, "eligible", False)):
                continue
            candidate = getattr(record, "candidate", None)
            if candidate is None:
                continue
            if not bool(getattr(candidate, "i_off", False)):
                continue
            mae = getattr(record, "mae_n", None)
            if mae is None:
                continue
            try:
                mae_f = float(mae)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(mae_f):
                continue
            if best_mae is None or mae_f < best_mae:
                best_mae = mae_f
        return best_mae

    def _arm_early_abort_shadow_for_attempt(
        self, attempt: Attempt, ticket: Any
    ) -> None:
        """Dispatch-time snapshot onto writer-adapter (shadow only)."""

        writer = getattr(self.runtime, "writer", None)
        if writer is None or not hasattr(writer, "arm_early_abort_shadow"):
            return
        kind_s = str(getattr(attempt, "kind", "") or getattr(ticket, "kind", ""))
        if kind_s == "QUALIFICATION":
            clear = getattr(writer, "clear_early_abort_shadow", None)
            if callable(clear):
                clear()
            return
        candidate_obj = attempt.candidate
        try:
            point = _point_from_candidate(candidate_obj)
            point_key = list(point.key)
        except Exception as exc:  # noqa: BLE001
            self.events.append(f"R008_EARLY_ABORT_SHADOW_SKIP:point:{exc}")
            clear = getattr(writer, "clear_early_abort_shadow", None)
            if callable(clear):
                clear()
            return
        cand_fields: dict[str, float] = {}
        for name in (
            "force_p_gain",
            "force_i_gain",
            "force_damping",
            "normal_filter_tau_s",
            "orientation_ko",
            "motion_kp",
        ):
            value = getattr(candidate_obj, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cand_fields[name] = float(value)
        writer.arm_early_abort_shadow(
            best_so_far_mae_n=self._bo_incumbent_mae_n(),
            dispatch_sequence=int(attempt.dispatch_sequence),
            attempt_sequence=int(attempt.attempt_sequence),
            kind=kind_s or "BO_TRIAL",
            point_key=point_key,
            candidate=cand_fields or None,
            run_dir=self._run_dir(),
            execution_id=str(attempt.execution_id or "") or None,
            events=self.events,
        )
    @staticmethod
    def _force_candidate_i_off(candidate: R006Candidate) -> R006Candidate:
        if candidate.i_off and float(candidate.force_i_gain) == 0.0:
            return candidate
        return R006Candidate(
            force_p_gain=candidate.force_p_gain,
            force_i_gain=0.0,
            force_damping=candidate.force_damping,
            normal_filter_tau_s=candidate.normal_filter_tau_s,
            orientation_ko=candidate.orientation_ko,
            motion_kp=candidate.motion_kp,
            target_force_n=candidate.target_force_n,
            i_mode=IMode.OFF,
        )

    @staticmethod
    def _force_candidate_tau_pin(candidate: R006Candidate) -> R006Candidate:
        if math.isclose(
            float(candidate.normal_filter_tau_s),
            float(PIN_TAU_S),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            return candidate
        return R006Candidate(
            force_p_gain=candidate.force_p_gain,
            force_i_gain=candidate.force_i_gain,
            force_damping=candidate.force_damping,
            normal_filter_tau_s=float(PIN_TAU_S),
            orientation_ko=candidate.orientation_ko,
            motion_kp=candidate.motion_kp,
            target_force_n=candidate.target_force_n,
            i_mode=candidate.i_mode,
        )

    def _queue_request(
        self,
        candidate: R006Candidate,
        *,
        kind: str,
        base: R006Candidate | None = None,
    ) -> None:
        # r008 searches a multi-octave box; after each safe return the next
        # trial may jump more than one quarter-octave.  The frozen one-step
        # graph remains the r006 policy; r008 replaces it here only.
        if not isinstance(candidate, R006Candidate):
            raise RuntimeErrorR005("r008 refill candidate is not native")
        transition_base = self.cursor if base is None else base
        if not isinstance(transition_base, R006Candidate):
            raise RuntimeErrorR005("r008 refill base candidate is not native")
        kind_name = str(kind)
        if kind_name in _I_ON_GATED_KINDS and (
            not candidate.i_off or float(candidate.force_i_gain) > 0.0
        ):
            if not self._has_i_on_unlock_observation():
                if not self._i_on_explicitly_allowed() and not getattr(
                    self, "_i_on_lock_event_emitted", False
                ):
                    self.events.append("R008_I_ON_LOCKED")
                    self._i_on_lock_event_emitted = True
                candidate = self._force_candidate_i_off(candidate)
                self.events.append(f"R008_I_ON_ENQUEUE_GATED:{kind_name}")
        if kind_name in _TAU_PIN_GATED_KINDS and not math.isclose(
            float(candidate.normal_filter_tau_s),
            float(PIN_TAU_S),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            candidate = self._force_candidate_tau_pin(candidate)
            self.events.append(f"R008_TAU_PIN_ENQUEUE:{kind_name}")
        self.queue.enqueue(candidate, kind=kind, epoch=self.epoch)
        if kind == "QUALIFICATION":
            self._qual_scheduled += 1
        elif kind == self._ANCHOR:
            self._anchor_scheduled += 1
        elif kind == self._STAIRCASE:
            self._staircase_scheduled += 1
        elif kind == self._SPACEFILL:
            self._spacefill_scheduled += 1
        elif kind == "BO_TRIAL":
            self._bo_scheduled += 1
        elif kind == "RETEST":
            self._retest_scheduled += 1
        self.events.append(f"ENQUEUE:{kind}")

    def _post_mailbox(self, intent: HostMailboxIntent) -> None:
        self._mailbox.put(intent)

    def _drain_mailbox(self) -> None:
        while True:
            try:
                intent = self._mailbox.get_nowait()
            except queue.Empty:
                return
            self._apply_mailbox_intent(intent)
            if self.terminal:
                return

    def _apply_mailbox_intent(self, intent: HostMailboxIntent) -> None:
        if intent.event:
            self.events.append(intent.event)
        if intent.anchor_verdict is not None:
            self._anchor_verdict = intent.anchor_verdict
        if intent.log2_pd_center is not None:
            self._box = _narrow_box_around_pd(
                self._box,
                float(intent.log2_pd_center),
                half_width=float(intent.rebind_half_width),
            )
            self._spacefill = self._spacefill_points()
        if intent.close_staircase:
            self._staircase_scheduled = len(self._staircase)
            self._staircase_count = len(self._staircase)
        if intent.phase is not None:
            self.phase = intent.phase
        if intent.retest_candidate is not None:
            self.retest_candidate = intent.retest_candidate
        if intent.clear_retest_records:
            self._retest_records.clear()
        if intent.cancel_pending:
            cancelled = self.queue.cancel_pending(reason=intent.reason or "r008_mailbox_cancel")
            self.events.append(f"R008_CANCEL_PENDING:{len(cancelled)}")
        if intent.stop_reason:
            self._stop(intent.stop_reason)

    def _hydrate_schedule_cursors(self) -> None:
        """Count durable enqueues (incl. cancelled) so resume does not re-queue.

        Dedupes by logical identity (``logical_uid``/``logical_request_uid``
        when present, else ``request_uid``), not raw metadata-row count.
        2026-08-06: a crash-resume resubmission (see
        ``_reconcile_or_complete_stale_inflight``) durably records a NEW
        ``request_uid`` for the SAME logical candidate. Counting raw rows
        inflated SPACEFILL's count from 16 real unique points to 24 counted
        rows in a live run (two crash-resume chains, 4 duplicate rows each)
        -- the cursor would have concluded SPACEFILL was done 8 points early
        and jumped to BO having tested fewer points than the schedule budget.
        """

        counts = {
            "QUALIFICATION": 0,
            self._ANCHOR: 0,
            self._STAIRCASE: 0,
            self._SPACEFILL: 0,
            "BO_TRIAL": 0,
            "RETEST": 0,
        }
        seen: dict[str, set[str]] = {kind: set() for kind in counts}

        def _count_once(kind: str, logical: str) -> None:
            if kind not in counts or logical in seen[kind]:
                return
            seen[kind].add(logical)
            counts[kind] += 1

        def _logical_id(item: Any) -> str:
            for attr in ("logical_uid", "logical_request_uid", "request_uid"):
                value = getattr(item, attr, None)
                if value is not None:
                    return str(value)
            return str(id(item))

        meta = getattr(self.queue, "_metadata", None)
        if isinstance(meta, Mapping):
            for entry in meta.values():
                kind = str(getattr(entry, "kind", "") or "")
                _count_once(kind, _logical_id(entry))
        else:
            for record in getattr(self.ledger, "records", ()):
                kind = str(getattr(record, "kind", "") or "")
                _count_once(kind, _logical_id(record))
            for item in self.queue.pending():
                kind = str(getattr(item, "kind", "") or "")
                _count_once(kind, _logical_id(item))
            cancelled_fn = getattr(self.queue, "cancelled", None)
            if callable(cancelled_fn):
                for item in cancelled_fn():
                    kind = str(getattr(item, "kind", "") or "")
                    _count_once(kind, _logical_id(item))
        self._qual_scheduled = counts["QUALIFICATION"]
        self._anchor_scheduled = counts[self._ANCHOR]
        self._staircase_scheduled = counts[self._STAIRCASE]
        self._spacefill_scheduled = counts[self._SPACEFILL]
        self._bo_scheduled = counts["BO_TRIAL"]
        self._retest_scheduled = counts["RETEST"]
        if self._load_domain_rebind(self._domain_rebind_path_value) is not None:
            self._staircase_scheduled = max(
                self._staircase_scheduled, len(self._staircase)
            )
        self._align_staircase_cursor_to_p_up_ki_lock()

    def _align_staircase_cursor_to_p_up_ki_lock(self) -> None:
        """Do not skip I_OFF P-up climb after coupled fixed-kf Ki-pocket failure.

        Queue metadata still credits STAIRCASE enqueues (pocket hard-stop +
        pending coupled ×2). Those candidates are not the P-up Ki-lock plan;
        resuming at scheduled≥2 would jump past the only PM-stable I_OFF
        levels straight into SPACEFILL. Reset when no sealed staircase rows
        exist and a credited enqueue sits in the Ki pocket or is I_ON while
        the rebuilt plan level is I_OFF.
        """

        if not STAIRCASE_USE_P_UP_KI_LOCK or self._staircase_scheduled <= 0:
            return
        if self._load_domain_rebind(self._domain_rebind_path_value) is not None:
            return
        sealed = self._rows_for(self._STAIRCASE)
        if sealed:
            return
        meta = getattr(self.queue, "_metadata", None)
        if not isinstance(meta, Mapping):
            return
        plan = self._staircase
        needs_reset = False
        stair_i = 0
        for entry in meta.values():
            if str(getattr(entry, "kind", "") or "") != self._STAIRCASE:
                continue
            cand = getattr(entry, "candidate", None)
            ki = float(
                getattr(cand, "i_gain", None)
                or getattr(cand, "force_i_gain", None)
                or 0.0
            )
            if confirmed_dangerous_ki(force_i_gain=ki):
                needs_reset = True
                break
            if stair_i < len(plan):
                plan_ki = float(plan[stair_i].point.to_parameter_point().i_gain)
                if plan_ki <= 0.0 and ki > 0.0:
                    needs_reset = True
                    break
            stair_i += 1
        if not needs_reset:
            return
        was = int(self._staircase_scheduled)
        self._staircase_scheduled = 0
        self.events.append(f"R008_STAIRCASE_P_UP_KI_LOCK_RESET:was_scheduled={was}")

    def start_epoch(self) -> int:
        epoch = super().start_epoch()
        self._staircase_count = 0
        self._spacefill_count = 0
        self._last_staircase_mae = None
        self._anchor_count = 0
        self._anchor_values = []
        self._anchor_verdict = None
        self._qual_scheduled = 0
        self._anchor_scheduled = 0
        self._staircase_scheduled = 0
        self._spacefill_scheduled = 0
        self._bo_scheduled = 0
        self._retest_scheduled = 0
        self._recoverable_hard_stop_streak = 0
        self.events.append("R008_EPOCH_PHASE_PLAN")
        # Parent start_epoch resets to QUALIFICATION / 0 passes; re-apply canary.
        if getattr(self, "_timing_canary", False):
            apply_timing_canary_skip_qual(self)
        return epoch

    def _resume_from_ledger(self) -> None:
        self.ledger.fresh_process_verify()
        records = self.ledger.records
        queue_sequence = int(getattr(self.queue, "last_attempt_sequence", 0))
        self.next_attempt_sequence = max(
            [queue_sequence, *(int(record.attempt_sequence) for record in records)],
            default=0,
        ) + 1
        if records:
            self.epoch = max(record.epoch for record in records)
            current = [record for record in records if record.epoch == self.epoch]
            try:
                self.cursor = (
                    current[-1].candidate
                    if isinstance(current[-1].candidate, R006Candidate)
                    else R006Candidate.from_canonical(current[-1].candidate.canonical)
                )
            except (AttributeError, R006LiveAdapterError) as exc:
                raise RuntimeErrorR005("r008 cold-read candidate is not native") from exc
            self.qualification_passes = sum(
                1 for record in current if record.qualification_eligible
            )
        done = False
        if self.qualification_passes < 3:
            if getattr(self, "_timing_canary", False):
                apply_timing_canary_skip_qual(self)
            else:
                self.phase = CampaignPhase.QUALIFICATION
                done = True
        if not done and self.qualification_passes < 3:
            self.phase = CampaignPhase.QUALIFICATION
            done = True
        if not done:
            anchor_rows = self._rows_for(self._ANCHOR)
            self._anchor_count = len(anchor_rows)
            self._anchor_values = self._anchor_values_from_rows(anchor_rows)
            self._anchor_verdict = self._compute_anchor_verdict()
            if self._anchor_verdict is None:
                self.phase = self._ANCHOR
                done = True
            elif self._anchor_verdict == "failed":
                self.phase = self._ANCHOR
                self.events.append(
                    "R008_ANCHOR_VALIDATION_BLOCKED:"
                    f"count={self._anchor_count}:values={self._anchor_values}"
                )
                done = True
        if not done:
            self._staircase_count = len(self._rows_for(self._STAIRCASE))
            # Persisted domain rebind means staircase closed early (edge/veto).
            if self._load_domain_rebind(self._domain_rebind_path_value) is not None:
                self._staircase_count = len(self._staircase)
            if self._staircase_count < len(self._staircase):
                self.phase = self._STAIRCASE
                done = True
        if not done:
            self._spacefill_count = len(self._rows_for(self._SPACEFILL))
            if self._spacefill_count < len(self._spacefill):
                self.phase = self._SPACEFILL
                done = True
        if not done:
            retests = self._rows_for("RETEST")
            if retests and len(retests) < 3 and self.retest_candidate is not None:
                self.phase = CampaignPhase.RETEST
                done = True
        if not done:
            # Brand-new optimizer client must warm-start+freeze before BO asks.
            self._warm_start_and_freeze_optimizer()
            self.phase = CampaignPhase.BO
            self.events.append("R008_RESUME_COLD_READ_VERIFIED")
        self._hydrate_schedule_cursors()
        phase = self._phase_name(self.phase)
        if phase in {self._SPACEFILL, CampaignPhase.BO.value, CampaignPhase.RETEST.value}:
            stale = [
                item
                for item in self.queue.pending()
                if item.kind in {self._STAIRCASE, self._ANCHOR, "QUALIFICATION"}
            ]
            if stale:
                cancelled = self.queue.cancel_pending(reason="r008_resume_phase_reconcile")
                self.events.append(f"R008_CANCEL_PENDING:{len(cancelled)}")

    def _warm_start_and_freeze_optimizer(self) -> None:
        # Two-stage warm-start fit then permanently freeze hyperparameters,
        # mirroring r006's proven _FIT_FREEZE phase. Without this, every
        # ask() sends hyperparameters_frozen=False and pays a full
        # fit_gpytorch_mll refit (grew to 8-19 minutes per ask live on
        # 2026-08-03, vs. ~30-40s for a frozen-hyperparameter ask).
        self._sync_optimizer_sobol_fraction()
        self.optimizer.fit_group_once(1)  # type: ignore[attr-defined]
        self.optimizer.fit_group_once(2)  # type: ignore[attr-defined]
        self.optimizer.freeze()  # type: ignore[attr-defined]
        self._persist_freeze_hypers()

    def _persist_freeze_hypers(self) -> None:
        """Write ℓ / outputscale / noise / train counts after FIT_FREEZE (audit)."""

        try:
            run_dir = Path(self.ledger.path).resolve().parent
        except (TypeError, OSError):
            return
        client = getattr(self.optimizer, "client", None)
        metadata = {}
        if client is not None:
            metadata = dict(getattr(client, "last_metadata", {}) or {})
        if not metadata:
            metadata = dict(getattr(self.optimizer, "last_ask_metadata", {}) or {})
        init = dict(metadata.get("initialization") or {})
        payload = {
            "schema": "step5d.autotune-v4/r008-gp-freeze-hypers-v1",
            "fit_group": getattr(getattr(self.optimizer, "client", None), "fit_group", None),
            "hyperparameters_frozen": bool(
                getattr(getattr(self.optimizer, "client", None), "hyperparameters_frozen", False)
            ),
            "observation_count": metadata.get("observation_count"),
            "raw_trainable_row_count": metadata.get("raw_trainable_row_count"),
            "deduplicated_row_count": metadata.get("deduplicated_row_count"),
            "train_yvar_n2_floor": metadata.get("train_yvar_n2_floor"),
            "kernel": metadata.get("kernel"),
            "initialization": init,
            "lengthscales": init.get("lengthscales"),
            "signal_scale_n": init.get("signal_scale_n"),
            "repeat_noise_n2": init.get("repeat_noise_n2"),
            "repeat_noise_n2_floor": init.get("repeat_noise_n2_floor"),
        }
        state = getattr(client, "frozen_model_state", None) if client is not None else None
        if isinstance(state, Mapping) and state:
            # Bound size: keep only finite scalar/list hypers, skip huge buffers.
            compact: dict[str, Any] = {}
            for name, value in state.items():
                if not isinstance(name, str):
                    continue
                if isinstance(value, (int, float)):
                    compact[name] = value
                elif isinstance(value, list) and len(json.dumps(value)) < 8_000:
                    compact[name] = value
            if compact:
                payload["frozen_model_state"] = compact
        path = run_dir / "r008_gp_freeze_hypers.json"
        try:
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self.events.append(f"R008_GP_FREEZE_HYPERS:{path.name}")
        except OSError as exc:
            self.events.append(f"R008_GP_FREEZE_HYPERS_WRITE_FAILED:{exc}")

    def _sidecar_rows(self) -> tuple[Mapping[str, Any], ...]:
        # Hot seal path: sidecar cold-read only (see R008ObservationLedger).
        return self.r006_ledger.fresh_process_verify()

    def _operator_next_path(self) -> Path:
        return Path(self.ledger.path).resolve().parent / OPERATOR_NEXT_NAME

    def _consume_operator_next(self) -> tuple[R006Candidate, str] | None:
        """One-shot operator injection: ``r008_operator_next.json`` → next enqueue."""

        path = self._operator_next_path()
        if not path.is_file() or path.is_symlink():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeErrorR005(f"r008 operator next is unreadable: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema") != OPERATOR_NEXT_SCHEMA:
            raise RuntimeErrorR005("r008 operator next schema differs")
        kind = str(payload.get("kind") or "BO_TRIAL")
        try:
            point = point_from_physical(
                force_p_gain=float(payload["force_p_gain"]),
                force_damping=float(payload["force_damping"]),
                normal_filter_tau_s=float(payload["normal_filter_tau_s"]),
                force_i_gain=float(payload.get("force_i_gain") or 0.0),
                orientation_ko=float(payload["orientation_ko"]),
                motion_kp=float(payload["motion_kp"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeErrorR005(f"r008 operator next fields invalid: {exc}") from exc
        if live_acquisition_unstable(
            force_damping=float(point.damping),
            normal_filter_tau_s=float(point.tau_s),
        ):
            raise RuntimeErrorR005("r008 operator next fails live acquisition veto")
        snapped_i = float(point.to_parameter_point().i_gain)
        if confirmed_dangerous_ki(force_i_gain=snapped_i):
            raise RuntimeErrorR005(
                "r008 operator next fails confirmed-dangerous Ki veto"
            )
        if live_phase_margin_unstable(
            force_p_gain=float(point.force_p_gain),
            force_damping=float(point.damping),
            force_i_gain=float(point.force_i_gain),
            normal_filter_tau_s=float(point.tau_s),
            stiffness_n_per_m=float(self._domain["greybox_stiffness_n_per_m"]),
            rho=float(self._domain["greybox_rho"]),
        ):
            raise RuntimeErrorR005("r008 operator next fails live phase-margin veto")
        consumed = path.with_name(
            f"{path.stem}.consumed_{time.strftime('%Y%m%d_%H%M%S')}{path.suffix}"
        )
        path.replace(consumed)
        fields = point.to_r006_candidate_fields()
        self.events.append(
            "R008_OPERATOR_NEXT:"
            f"kind={kind}:P={float(fields['force_p_gain']):.6g}:"
            f"D={float(fields['force_damping']):.6g}:"
            f"I={float(fields['force_i_gain']):.6g}:file={consumed.name}"
        )
        return _candidate_from_point(point.to_parameter_point()), kind

    def _refill(self) -> None:
        if self.terminal:
            return
        if self.epoch <= 0:
            raise RuntimeErrorR005("r008 refill requires a started epoch")
        if self.queue.inflight is not None:
            raise RuntimeErrorR005("r008 refill cannot run with a physical inflight row")
        while len(self.queue.pending()) < PENDING_WATERMARK:
            pending = self.queue.pending()
            base = pending[-1].candidate if pending else self.cursor
            phase = self._phase_name(self.phase)
            # Never steal QUAL slots — operator probes run only after timing-eligible QUAL.
            if phase != CampaignPhase.QUALIFICATION.value:
                override = self._consume_operator_next()
                if override is not None:
                    candidate, kind = override
                    self._queue_request(candidate, kind=kind, base=base)
                    continue
            if phase == CampaignPhase.QUALIFICATION.value:
                # Only seal-thread mailbox may advance QUAL→ANCHOR once
                # qualification_passes >= 3. Scheduling 3 QUAL must not unlock
                # PATH/ANCHOR (TP reason=50 baseline_qualification_missing).
                if self._qual_scheduled >= 3:
                    break
                self._queue_request(R006Candidate(), kind="QUALIFICATION", base=base)
                continue
            if phase == self._ANCHOR:
                if self._anchor_verdict == "passed":
                    self.phase = self._STAIRCASE
                    continue
                if self._anchor_verdict == "failed":
                    # Blocked: online plant disagrees with the r006-era anchor.
                    break
                if self._anchor_scheduled >= self._anchor_max_attempts:
                    # Sealed verdict decides failed; do not invent failed on motion.
                    break
                self._queue_request(
                    _candidate_from_point(self._anchor_point.to_parameter_point()),
                    kind=self._ANCHOR,
                    base=base,
                )
                continue
            if phase == self._STAIRCASE:
                index = self._staircase_scheduled
                if index >= len(self._staircase):
                    if pending:
                        break
                    self.phase = self._SPACEFILL
                    self.events.append("R008_STAIRCASE_COMPLETE")
                    continue
                level_point = self._staircase[index].point
                stiffness = float(self._domain["greybox_stiffness_n_per_m"])
                rho = float(self._domain["greybox_rho"])
                level_typed = level_point.to_parameter_point()
                # Ki pocket confirmed by far005 disp42 + limit50 STAIRCASE #10;
                # PM does not catch it — skip the level (do not deadlock on idx0).
                if confirmed_dangerous_ki(force_i_gain=float(level_typed.i_gain)):
                    self.events.append(
                        "R008_STAIRCASE_CONFIRMED_DANGEROUS_KI:"
                        f"index={index}:ki={float(level_typed.i_gain):.12g}"
                    )
                    self._staircase_scheduled += 1
                    continue
                predicted_pm = phase_margin_deg(
                    stiffness=stiffness,
                    pd_ratio=level_point.pd_ratio,
                    damping=level_point.damping,
                    tau_eff_s=level_point.tau_s * rho,
                    kf=level_point.kf,
                )
                if live_phase_margin_unstable(
                    force_p_gain=float(level_point.force_p_gain),
                    force_damping=float(level_point.damping),
                    force_i_gain=float(level_point.force_i_gain),
                    normal_filter_tau_s=float(level_point.tau_s),
                    stiffness_n_per_m=stiffness,
                    rho=rho,
                ):
                    if index == 0:
                        self.events.append(
                            f"R008_STAIRCASE_ANCHOR_PREDICTED_UNSTABLE:pm={predicted_pm:.3g}"
                        )
                        break
                    self.events.append(
                        f"R008_STAIRCASE_PREDICTED_UNSTABLE:index={index}:pm={predicted_pm:.3g}"
                    )
                    edge_point = edge_half_anchor(self._staircase, index - 1)
                    self._box = _narrow_box_around_pd(self._box, edge_point.log2_pd)
                    self._spacefill = self._spacefill_points()
                    self._persist_domain_rebind(edge_point.log2_pd, source="preemptive_veto")
                    self.events.append(
                        f"R008_DOMAIN_REBIND:log2_pd_center={edge_point.log2_pd:.4g}"
                    )
                    self._staircase_scheduled = len(self._staircase)
                    self._staircase_count = len(self._staircase)
                    self.phase = self._SPACEFILL
                    continue
                point = level_point.to_parameter_point()
                self._queue_request(_candidate_from_point(point), kind=self._STAIRCASE, base=base)
                continue
            if phase == self._SPACEFILL:
                index = self._spacefill_scheduled
                if index >= len(self._spacefill):
                    if pending:
                        break
                    self._warm_start_and_freeze_optimizer()
                    self.phase = CampaignPhase.BO
                    self.events.append("R008_SPACEFILL_COMPLETE_ENTER_BO_FROZEN")
                    continue
                point = self._spacefill[index].to_parameter_point()
                self._queue_request(_candidate_from_point(point), kind=self._SPACEFILL, base=base)
                continue
            if phase == CampaignPhase.BO.value:
                # Barrier lives inside R008ProductionOptimizer.ask (join seals
                # before sidecar_binding). Motion HOME→DISPATCH still never
                # join_all; only the CUDA ask path serializes seal/tell.
                self._sync_optimizer_sobol_fraction()
                ask = self.optimizer.ask(
                    observations=self.ledger.records,
                    pending=tuple(item.candidate for item in pending),
                    incumbent=self._bo_incumbent_candidate(),
                    q=1,
                )
                self._queue_request(ask.candidate, kind="BO_TRIAL", base=base)
                continue
            if phase == CampaignPhase.RETEST.value:
                if self.retest_candidate is None:
                    raise RuntimeErrorR005("r008 retest incumbent is missing")
                if self._retest_scheduled >= 3:
                    break
                self._queue_request(self.retest_candidate, kind="RETEST", base=base)
                continue
            raise RuntimeErrorR005(f"unknown r008 host phase {phase}")
        self.events.append(f"R008_REFILL:PENDING={len(self.queue.pending())}")

    def _finish_retests(self) -> None:
        rows = self._rows_for("RETEST")
        if len(rows) < 3:
            return
        values = [
            float(self._row_receipt(row)["objective_mae_n"])
            for row in rows[-3:]
            if isinstance(self._row_receipt(row).get("objective_mae_n"), (int, float))
        ]
        gates = [
            record.eligible and record.safe_return and record.binding_ok
            and record.safety_gate and record.contact_gate and record.return_gate
            and record.timing_gate and record.identity_gate and record.motion_gate
            for record in self.ledger.records
            if record.kind == "RETEST"
        ][-3:]
        threshold = float(self._mae_threshold_n)
        application = (
            len(values) == 3
            and len(gates) == 3
            and all(gates)
            and statistics.median(values) <= threshold
        )
        if not application:
            self._post_mailbox(
                HostMailboxIntent(
                    phase=CampaignPhase.BO,
                    clear_retest_records=True,
                    event="R008_RETEST_FAILED_RETURN_TO_BO",
                )
            )
            return
        self._post_mailbox(
            HostMailboxIntent(
                phase=CampaignPhase.COMPLETE,
                event=(
                    f"R008_COMPLETE_ABSOLUTE_MAE:median={statistics.median(values):.6g}:"
                    f"threshold={threshold:g}"
                ),
            )
        )

    def _emit_path_xy(self, record: Any) -> None:
        """Ops visibility: slow cycloid peaks in mm (not tracking-error metres)."""

        summary = path_xy_summary_from_record(record)
        if summary is None:
            return
        self.events.append(format_path_xy_event(summary))
        try:
            run_dir = Path(self.ledger.path).resolve().parent
            append_path_xy_sidecar(run_dir, summary)
        except (OSError, TypeError, ValueError) as exc:
            self.events.append(f"R008_PATH_XY_SIDECAR_SKIP:{exc}")

    def _emit_phase_timings(self, record: Any) -> None:
        """Durable A1 residual split: observation metrics + r008 sidecar JSONL."""

        marks = dict(getattr(self, "_phase_marks", {}) or {})
        timings = build_phase_timings_s(
            marks,
            attempt_sequence=int(getattr(record, "attempt_sequence", 0) or 0),
            kind=str(getattr(record, "kind", "") or ""),
            execution_id=(
                str(record.metrics["execution_id"])
                if isinstance(getattr(record, "metrics", None), Mapping)
                and isinstance(record.metrics.get("execution_id"), str)
                else None
            ),
        )
        durations = timings.get("durations_s") or {}
        self.events.append(
            "R008_PHASE_TIMINGS:"
            f"home={durations.get('home_s')}:"
            f"search={durations.get('contact_search_s')}:"
            f"path60={durations.get('path_60_s')}:"
            f"return={durations.get('safe_return_s')}:"
            f"seal={durations.get('seal_s')}"
        )
        run_dir = Path(self.ledger.path).resolve().parent
        try:
            append_phase_timings_sidecar(run_dir, timings)
        except (OSError, TypeError, ValueError) as exc:
            self.events.append(f"R008_PHASE_TIMINGS_SIDECAR_SKIP:{exc}")
        # Finance reconcile sidecar (not GP / not r006-observations).
        try:
            marks = dict(getattr(self, "_phase_marks", {}) or {})
            t0 = marks.get("CONTACT_SEARCH_START") or marks.get("ARM")
            t1 = marks.get("SEAL") or marks.get("SAFE_RETURN")
            wall_t0 = None
            wall_t1 = None
            wall0 = getattr(self, "_phase_wall0", None)
            if isinstance(wall0, (int, float)):
                if isinstance(t0, (int, float)):
                    wall_t0 = float(wall0) + float(t0)
                if isinstance(t1, (int, float)):
                    wall_t1 = float(wall0) + float(t1)
            exec_id = (
                str(record.metrics["execution_id"])
                if isinstance(getattr(record, "metrics", None), Mapping)
                and isinstance(record.metrics.get("execution_id"), str)
                else None
            )
            append_reconcile_for_attempt(
                run_dir,
                attempt_sequence=int(getattr(record, "attempt_sequence", 0) or 0),
                kind=str(getattr(record, "kind", "") or ""),
                execution_id=exec_id,
                wall_t0=wall_t0,
                wall_t1=wall_t1,
            )
        except (OSError, TypeError, ValueError) as exc:
            self.events.append(f"R008_BODY_RECONCILE_SKIP:{exc}")

    def _record_and_tell(self, result: Any) -> Any:
        """Seal via off-host daemon (default) or forked child; tell on parent.

        Daemon path: sole writer owns r005/r006 ``_cached`` (011258 fix) and
        keeps heavy cold verify off the motion process (reason=43). Parent only
        extends its in-memory ledger cache from the returned row + record pickle.
        """

        result = attach_phase_timings_to_result(
            result, getattr(self, "_phase_marks", {})
        )
        seq = int(result.attempt_sequence)
        kind_s = str(result.kind)
        daemon = getattr(self, "_seal_daemon", None)
        if daemon is not None:
            return self._record_and_tell_via_daemon(result, daemon)

        return self._record_and_tell_via_fork(result)

    def _record_and_tell_via_daemon(self, result: Any, daemon: SealDaemonClient) -> Any:
        import pickle

        seq = int(result.attempt_sequence)
        kind_s = str(result.kind)
        payload = daemon.seal_result(result)
        sealed_seq = int(payload["sealed_seq"])
        if sealed_seq != seq:
            raise RuntimeErrorR005(
                f"r008 daemon seal sequence mismatch want={seq} got={sealed_seq}"
            )
        record_path = Path(str(payload["record_pickle_path"]))
        record = pickle.loads(record_path.read_bytes())
        record_path.unlink(missing_ok=True)
        row = payload.get("ledger_row")
        if not isinstance(row, Mapping):
            raise RuntimeErrorR005("r008 daemon seal missing ledger_row")
        # Extend host cache without cold subprocess (daemon already verified).
        if seq not in self.ledger._cached_attempt_sequences:  # noqa: SLF001
            self.ledger._set_cache(  # noqa: SLF001
                self.ledger._cached_rows + (dict(row),),  # noqa: SLF001
                self.ledger._cached_records + (record,),  # noqa: SLF001
            )
        # Do not touch r006_ledger on the host: daemon is the sole sidecar
        # writer/verifier. Tell uses payload trainable/receipt only.
        self._phase("SEAL")
        if kind_s != "QUALIFICATION":
            receipt_dict = payload.get("receipt")
            if bool(payload.get("trainable")) and record.eligible:
                self.optimizer.tell(record)
                self._phase("R006_TELL_FORMAL_RAW")
            elif record.eligible:
                self._phase("R006_SAFE_NONTRAINABLE_CONTINUE")
            self._last_sidecar_receipt = receipt_dict
        self._emit_phase_timings(record)
        return record

    def _record_and_tell_via_fork(self, result: Any) -> Any:
        """Legacy in-process fork seal (tests / seal_daemon=False)."""

        seq = int(result.attempt_sequence)
        campaign_fp = self.contract.campaign_fingerprint
        kind_s = str(result.kind)
        execution_id = result.execution_id

        def _heavy_seal_artifacts() -> dict[str, Any]:
            if self.evidence_sink is not None and kind_s != "QUALIFICATION":
                self.evidence_sink(result)
            sealed = self.ledger.append(result.to_record(campaign_fp))
            sealed_seq = int(sealed.attempt_sequence)
            self.ledger.fresh_process_verify()
            trainable = False
            eligible = bool(sealed.eligible)
            receipt_dict: dict[str, Any] | None = None
            if kind_s != "QUALIFICATION":
                rows = self._sidecar_rows()
                matched = [
                    row
                    for row in rows
                    if row.get("attempt_sequence") == seq
                    and row.get("execution_id") == execution_id
                ]
                if len(matched) != 1:
                    raise RuntimeErrorR005(
                        "r008 sealed attempt lacks one raw sidecar identity"
                    )
                receipt = self._row_receipt(matched[0])
                trainable = bool(matched[0].get("trainable"))
                as_dict = getattr(receipt, "as_dict", None)
                receipt_dict = as_dict() if callable(as_dict) else dict(receipt)
            return {
                "sealed_seq": sealed_seq,
                "eligible": eligible,
                "trainable": trainable,
                "receipt": receipt_dict,
            }

        payload = run_in_fork(_heavy_seal_artifacts)
        sealed_seq = int(payload["sealed_seq"])
        if sealed_seq != seq:
            raise RuntimeErrorR005(
                f"r008 fork seal sequence mismatch want={seq} got={sealed_seq}"
            )
        self.ledger.fresh_process_verify()
        # Parent r006 refresh (011258): fork children do not update parent _cached.
        r006 = getattr(self, "r006_ledger", None)
        if r006 is not None:
            r006.fresh_process_verify()
        record = next(
            (row for row in self.ledger.records if int(row.attempt_sequence) == seq),
            None,
        )
        if record is None:
            raise RuntimeErrorR005(
                f"r008 fork seal missing ledger row attempt_sequence={seq}"
            )
        self._phase("SEAL")
        if kind_s != "QUALIFICATION":
            receipt_dict = payload.get("receipt")
            if bool(payload.get("trainable")) and record.eligible:
                self.optimizer.tell(record)
                self._phase("R006_TELL_FORMAL_RAW")
            elif record.eligible:
                self._phase("R006_SAFE_NONTRAINABLE_CONTINUE")
            self._last_sidecar_receipt = receipt_dict
        self._emit_phase_timings(record)
        return record

    def _on_async_seal_done(self, job: SealJob) -> None:
        metrics = AsyncSealPipeline._metrics_for(job)
        self.events.append(
            "R008_ASYNC_SEAL:"
            f"seq={job.attempt_sequence}:"
            f"kind={job.kind}:"
            f"sync={int(job.needs_sync)}:"
            f"overlap={metrics.seal_overlap_s}:"
            f"blocked={metrics.motion_blocked_on_seal_s}:"
            f"wall={metrics.seal_wall_s}"
        )
        if job.error is not None:
            self._post_mailbox(
                HostMailboxIntent(
                    stop_reason=f"async_seal_fault:{job.error}",
                    event=f"R008_ASYNC_SEAL_FAULT:{job.error}",
                )
            )
        try:
            run_dir = Path(self.ledger.path).resolve().parent
            append_phase_timings_sidecar(
                run_dir,
                {
                    "schema": "step5d.autotune-v4/r008-async-seal-overlap-v1",
                    "attempt_sequence": job.attempt_sequence,
                    "kind": job.kind,
                    "needs_sync": job.needs_sync,
                    "durations_s": {
                        "seal_overlap_s": metrics.seal_overlap_s,
                        "motion_blocked_on_seal_s": metrics.motion_blocked_on_seal_s,
                        "async_seal_wall_s": metrics.seal_wall_s,
                    },
                },
            )
        except (OSError, TypeError, ValueError) as exc:
            self.events.append(f"R008_ASYNC_SEAL_SIDECAR_SKIP:{exc}")

    def _close_seal_daemon(self) -> None:
        daemon = getattr(self, "_seal_daemon", None)
        if daemon is not None:
            try:
                daemon.close()
            except Exception as exc:  # noqa: BLE001
                self.events.append(f"R008_SEAL_DAEMON_CLOSE_ERROR:{exc}")
            self._seal_daemon = None

    def _stop(self, reason: str) -> None:
        try:
            self._async_seal.join_all()
        except Exception as exc:  # noqa: BLE001
            self.events.append(f"R008_ASYNC_SEAL_JOIN_ERROR:{exc}")
        super()._stop(reason)
        self._async_seal.close_and_join(timeout_s=120.0)
        self._close_seal_daemon()

    def _run_dir(self) -> Path | None:
        ledger = getattr(self, "ledger", None)
        path = getattr(ledger, "path", None) if ledger is not None else None
        if path is None:
            return None
        return Path(path).resolve().parent

    def _maybe_record_hard_stop_penalty(
        self,
        *,
        attempt: Attempt | None,
        ticket: Any | None,
        fault: BaseException | str | None = None,
        force: bool = False,
    ) -> None:
        """Append BO penalty sidecar for hard-stop / safety revoke failures."""

        if not force:
            if fault is None or not exception_looks_like_hard_stop(fault):
                return
        run_dir = self._run_dir()
        if run_dir is None:
            return
        candidate_obj = None
        dispatch_sequence = None
        attempt_sequence = None
        kind = "BO_TRIAL"
        execution_id = None
        if attempt is not None:
            candidate_obj = attempt.candidate
            dispatch_sequence = int(attempt.dispatch_sequence)
            attempt_sequence = int(attempt.attempt_sequence)
            kind = str(attempt.kind)
            execution_id = str(attempt.execution_id or "") or None
        elif ticket is not None:
            candidate_obj = getattr(ticket, "candidate", None)
            dispatch_sequence = getattr(ticket, "dispatch_sequence", None)
            kind = str(getattr(ticket, "kind", kind))
        if dispatch_sequence is None:
            inflight = getattr(self.queue, "inflight", None)
            if inflight is not None:
                dispatch_sequence = getattr(inflight, "dispatch_sequence", None)
                if candidate_obj is None:
                    entry = getattr(inflight, "entry", None) or inflight
                    candidate_obj = getattr(entry, "candidate", None)
                    kind = str(getattr(entry, "kind", kind))
        if dispatch_sequence is None or candidate_obj is None:
            return
        try:
            point = _point_from_candidate(candidate_obj)
            point_key = list(point.key)
        except Exception as exc:  # noqa: BLE001
            self.events.append(f"R008_HARD_STOP_PENALTY_SKIP:point:{exc}")
            return
        cand_fields: dict[str, float] = {}
        for name in (
            "force_p_gain",
            "force_i_gain",
            "force_damping",
            "normal_filter_tau_s",
            "orientation_ko",
            "motion_kp",
        ):
            value = getattr(candidate_obj, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cand_fields[name] = float(value)
        try:
            row = record_hard_stop_penalty_for_run(
                run_dir,
                dispatch_sequence=int(dispatch_sequence),
                attempt_sequence=attempt_sequence,
                kind=kind,
                point_key=point_key,
                candidate=cand_fields or None,
                execution_id=execution_id,
            )
        except Exception as exc:  # noqa: BLE001
            self.events.append(f"R008_HARD_STOP_PENALTY_ERROR:{exc}")
            return
        if row is None:
            self.events.append(
                f"R008_HARD_STOP_PENALTY_DEDUP:dispatch={int(dispatch_sequence)}"
            )
            return
        self.events.append(
            "R008_HARD_STOP_PENALTY:"
            f"dispatch={int(dispatch_sequence)}:"
            f"mae={float(row['penalty_mae_n']):.6g}:"
            f"hist={row.get('historical_max_mae_n')}"
        )

    def _stop_or_recover_from_run_one_fault(self, prefix: str, exc: BaseException) -> None:
        """Seal a hard-stop penalty, then recycle the host (not in-process continue).

        A candidate-specific force/torque limit trip (reason_code 61/62/63,
        see ``RECOVERABLE_HARD_STOP_REASON_CODES``) is, by autotune design,
        exactly the outcome the hard-stop penalty sidecar exists to punish --
        it should not permanently kill the campaign. Prior to 2026-08-06 every
        one of these called the frozen r005 ``_stop()`` with a generic
        unexpected-code reason and left a human to restart.

        2026-08-06 in-process "continue" was tried (skip ``_stop()``, hope the
        next ``run_one()`` ``home()`` + ``reconcile_inflight_after_home``
        recovers). That is false: after 61 the mature writer is fail-closed
        (``_stopped``, session ``STOPPED``), and ``R005MatureWriter.home()`` is
        verify-only -- it raises ``r005 mature writer is not verified at Home``
        and kills the host anyway. Proven recovery is a **new host process**
        (new writer → ``READY_HOME_NEXT``), which the overnight autoresume
        watcher already performs.

        So on recoverable 61/62/63 we: (1) complete the punished inflight so
        resume does not redispatch the same candidate, (2) ``_stop`` with
        ``recoverable_hard_stop_recycle:…`` so authority drops and the
        watcher relaunches. Circuit breaker escalates to a fatal ``_stop``
        after ``RECOVERABLE_HARD_STOP_LIMIT`` consecutive recycles.

        2026-08-06: the streak used for that decision is read fresh from the
        durable penalty ledger (``recoverable_streak_since_last_success``),
        not from ``self._recoverable_hard_stop_streak``. Every recoverable
        hard-stop tears this process down and starts a new one -- an
        in-memory instance counter reset in ``__init__`` can never exceed 1
        in real deployment, so the breaker could structurally never trip.
        ``self._recoverable_hard_stop_streak`` is still incremented for
        event-log continuity within one process's own lifetime, but it is
        not what decides recycle vs. escalate.
        """

        if exception_is_recoverable_hard_stop(exc):
            self._recoverable_hard_stop_streak += 1
            reason_code = extract_reason_code(exc)
            run_dir = self._run_dir()
            if run_dir is not None:
                last_sealed = max(
                    (
                        int(getattr(record, "attempt_sequence", 0) or 0)
                        for record in getattr(self.ledger, "records", ())
                    ),
                    default=0,
                )
                streak = recoverable_streak_since_last_success(
                    load_penalties(run_dir),
                    last_sealed_attempt_sequence=last_sealed,
                )
            else:
                # No run dir to read the durable ledger from (e.g. an
                # offline/test harness) -- fall back to the in-memory count
                # rather than silently treating this as always-safe.
                streak = self._recoverable_hard_stop_streak
            if streak <= self.RECOVERABLE_HARD_STOP_LIMIT:
                self.events.append(
                    "R008_RECOVERABLE_HARD_STOP:"
                    f"streak={streak}:"
                    f"limit={self.RECOVERABLE_HARD_STOP_LIMIT}:"
                    f"reason_code={reason_code}:{prefix}{exc}"
                )
                inflight = getattr(getattr(self, "queue", None), "inflight", None)
                if inflight is not None:
                    try:
                        self.queue.complete(
                            inflight,
                            status="SAFETY_OR_RETURN_FAILURE",
                            detail=(
                                f"recoverable_hard_stop:reason_code={reason_code}"
                            ),
                        )
                        self.events.append(
                            "R008_RECOVERABLE_INFLIGHT_COMPLETE:"
                            f"dispatch={int(inflight.dispatch_sequence)}"
                        )
                    except Exception as qexc:  # noqa: BLE001
                        self.events.append(
                            f"R008_RECOVERABLE_INFLIGHT_COMPLETE_ERROR:{qexc}"
                        )
                self._stop(
                    "recoverable_hard_stop_recycle:"
                    f"streak={streak}:"
                    f"reason_code={reason_code}:{prefix}{exc}"
                )
                return
            self.events.append(
                "R008_HARD_STOP_CIRCUIT_BREAKER_TRIPPED:"
                f"streak={streak}:"
                f"limit={self.RECOVERABLE_HARD_STOP_LIMIT}"
            )
        self._stop(f"{prefix}{exc}")

    # Sentinels distinguishing the three _reconcile_or_complete_stale_inflight
    # outcomes from reconcile_inflight_after_home() genuinely returning None
    # (an ambiguous crash reconcile could not resolve).
    _STALE_INFLIGHT_ALREADY_COMPLETED = object()
    _RECONCILE_METHOD_MISSING = object()

    def _reconcile_or_complete_stale_inflight(self) -> Any:
        """After Home, resolve a stale inflight left by a prior process.

        2026-08-06: reconcile_inflight_after_home() exists for genuinely
        ambiguous crashes (process died, outcome unknown) and resubmits the
        candidate at the head of the queue. But a recoverable hard-stop
        (reason_code 61/62/63, see _stop_or_recover_from_run_one_fault)
        already seals a penalty row for that exact dispatch_sequence before
        exiting; if that seal landed but queue.complete() did not land before
        process teardown, the outcome is NOT ambiguous -- it is a known,
        already-punished failure. Confirmed live: dispatch 18 hard-stopped,
        queue.complete() did not land in time, the next process treated it as
        an ambiguous crash and reconcile resubmitted the identical candidate
        as dispatch 19 -- which hard-stopped again, then again as 20. Check
        the durable penalty ledger first; only fall through to the genuine
        ambiguous-crash reconcile path when there is no such record.
        """

        inflight = self.queue.inflight
        run_dir = self._run_dir()
        dispatch_sequence = getattr(inflight, "dispatch_sequence", None)
        if (
            run_dir is not None
            and dispatch_sequence is not None
            and has_penalty_for_dispatch(run_dir, int(dispatch_sequence))
        ):
            self.queue.complete(
                inflight,
                status="SAFETY_OR_RETURN_FAILURE",
                detail=(
                    "r008_resume_known_hard_stop_penalty:"
                    f"dispatch={int(dispatch_sequence)}"
                ),
            )
            self.events.append(
                "R008_RESUME_INFLIGHT_KNOWN_HARD_STOP_COMPLETE:"
                f"dispatch={int(dispatch_sequence)}"
            )
            return self._STALE_INFLIGHT_ALREADY_COMPLETED
        reconcile = getattr(self.queue, "reconcile_inflight_after_home", None)
        if reconcile is None:
            return self._RECONCILE_METHOD_MISSING
        return reconcile()

    def run_until_terminal(self, *, diagnostic_limit: int | None = None) -> str:
        status = super().run_until_terminal(diagnostic_limit=diagnostic_limit)
        try:
            self._async_seal.join_all()
        except Exception as exc:  # noqa: BLE001
            self.events.append(f"R008_ASYNC_SEAL_JOIN_ERROR:{exc}")
            if not self.terminal:
                self._stop(f"async_seal_fault:{exc}")
        self._async_seal.close_and_join(timeout_s=120.0)
        self._close_seal_daemon()
        return self.status if self.terminal else status

    def _motion_refill_prepare_next(self) -> Any:
        """Motion-path prepare: drain mailbox → refill → prepare; never join_all."""

        self._drain_mailbox()
        if self.terminal:
            return None
        if self.queue.inflight is not None:
            # Resume reconcile may still own inflight; do not refill yet.
            return self.queue.prepare_next()
        self._refill()
        if self.terminal:
            return None
        return self.queue.prepare_next()

    @staticmethod
    def _prepare_after_home_allowing_seal_overlap(
        *,
        has_pending: bool,
        prepare_next: Any,
        join_refill_prepare: Any,
    ) -> Any:
        """Archived join path — always use refill_prepare (no motion join_all).

        ``has_pending`` is ignored; continuous-run tops up via cursors every cycle.
        ``join_refill_prepare`` must itself be a no-join refill+prepare helper.
        """

        _ = has_pending, prepare_next
        return join_refill_prepare()

    def _join_refill_prepare_next(self) -> Any:
        """Archived name: does NOT join seals — delegates to motion refill path."""

        return self._motion_refill_prepare_next()

    def run_one(self) -> Any:
        """Physical attempt with seal/tell overlapped into next PATH60.

        ``r005.runtime.HostLoop.run_one`` stays frozen (source closure). Queue
        ``complete`` runs on the motion thread immediately after SAFE_RETURN so
        inflight clears; ledger seal/tell/advance run on the async worker.
        Motion path never calls ``join_all`` (stop/terminal / QUAL baseline only).
        PATH-kind seals hold until the next STAGE25; QUAL releases immediately.
        """

        if self.terminal:
            return None
        if self.epoch == 0:
            self.start_epoch()
        attempt: Attempt | None = None
        ticket: Any | None = None
        try:
            # Prior seal is held until this attempt's SAFE_RETURN (PATH60 overlap).
            self._async_seal.mark_next_motion_start()
            self._begin_phase_clock()
            # Drain post-SAFE_RETURN seal work BEFORE HOME so begin_search_critical
            # at ARM does not pay the ~9s hitch (canaries 040407/041113/041810).
            # Gating stays on; we only move the join off the search-critical edge.
            async_seal_pre = getattr(self, "_async_seal", None)
            if async_seal_pre is not None:
                prehome_s = async_seal_pre.join_executing()
                if prehome_s > 0.0:
                    self.events.append(
                        f"R008_PREHOME_SEAL_JOIN:waited_s={prehome_s:.3f}"
                    )
            had_incomplete_inflight = self.queue.inflight is not None
            self.runtime.home()
            self._phase("HOME")
            if had_incomplete_inflight:
                reconciled = self._reconcile_or_complete_stale_inflight()
                if reconciled is self._RECONCILE_METHOD_MISSING:
                    self._stop("resume_inflight_lacks_home_reconciliation")
                    return None
                if reconciled is self._STALE_INFLIGHT_ALREADY_COMPLETED:
                    pass
                elif reconciled is None:
                    self._stop("resume_inflight_home_reconciliation_missing")
                    return None
                else:
                    self.events.append(
                        f"RESUME_HOME_RECONCILED:{reconciled.logical_uid}"
                    )
            ticket = self._motion_refill_prepare_next()
            if ticket is None:
                if not self.terminal:
                    self._stop("optimizer_exhausted_or_no_dispatchable_domain")
                return None
            # No join_all here — that serialized ~seal_wall into dispatch_s
            # (~118s cycles). Prior PATH seals stay held until SAFE_RETURN;
            # begin_search_critical joins only if a seal already started
            # executing (should be rare with hold_until_path60).
            self._drain_mailbox()
            attempt = Attempt(
                epoch=self.epoch,
                attempt_sequence=self.next_attempt_sequence,
                candidate=ticket.candidate,
                kind=ticket.kind,
                dispatch_sequence=ticket.dispatch_sequence,
                request_uid=ticket.request_uid,
                logical_request_uid=ticket.entry.logical_uid,
                execution_id=(
                    f"r005-e{self.epoch}-s{self.next_attempt_sequence}-"
                    f"{uuid.uuid4().hex}"
                ),
            )
            self.next_attempt_sequence += 1
            record_execution = getattr(self.queue, "record_execution", None)
            if record_execution is not None:
                record_execution(
                    ticket,
                    attempt_sequence=attempt.attempt_sequence,
                    execution_id=attempt.execution_id,
                )
            self.runtime.dispatch(attempt, ticket)
            self._phase("DISPATCH")
            # Join BEFORE arm(): post-ARM ~9s seal waits (025746/034742) left
            # the TP without HOLD packets and killed RTDE before execute_attempt
            # (samples=0). Pre-ARM, arm_unbounded still allows one prearm
            # reconnect under READY_HOME_NEXT.
            async_seal = getattr(self, "_async_seal", None)
            if async_seal is not None:
                joined_s = async_seal.begin_search_critical()
                if joined_s > 0.0:
                    self.events.append(
                        f"R008_SEARCH_CRITICAL_JOIN:waited_s={joined_s:.3f}"
                    )
            self.runtime.arm(attempt)
            self._phase("ARM")
            self._assert_prepared_matches_ticket(attempt, ticket)
            self._arm_early_abort_shadow_for_attempt(attempt, ticket)
            result = self.runtime.run_60s(attempt)
            self._phase("RUN_60S")
            self._validate_identity(attempt, result)
            if not result.safety_gate or not result.safe_return:
                self._maybe_record_hard_stop_penalty(
                    attempt=attempt, ticket=ticket, force=True
                )
                self._stop("safety_or_return_failure_before_safe_return")
                return result
            result = self.runtime.safe_return(attempt, result)
            self._phase("SAFE_RETURN")
            # PATH finished: release prior hold_until_path60 seals (not STAGE25).
            self._async_seal.end_search_critical()
            self._validate_identity(attempt, result)
            if result.outcome is AttemptOutcome.CODE_OR_EVIDENCE_BUG:
                self._stop("code_or_evidence_binding_failure")
                return result
            if result.outcome is AttemptOutcome.SAFETY_OR_RETURN_FAILURE:
                self._maybe_record_hard_stop_penalty(
                    attempt=attempt, ticket=ticket, force=True
                )
                self._stop("safety_or_safe_return_failure")
                return result

            # Free physical inflight before seal I/O so the next HOME can start.
            status = "SUCCEEDED" if result.eligible else "SAFE_NONTRAINABLE"
            self.queue.complete(
                ticket, status=status, detail="physical_complete_async_seal_pending"
            )
            # Reached past every hard-stop branch above: this attempt did not
            # trip a candidate-specific safety limit. Clear the consecutive
            # recoverable-hard-stop streak so an isolated incident hours ago
            # does not eat into tonight's remaining circuit-breaker budget.
            self._recoverable_hard_stop_streak = 0
            self._phase("QUEUE_COMPLETE")
            # Top up pending while seal of this attempt runs (no join_all).
            self._drain_mailbox()
            if not self.terminal and self.queue.inflight is None:
                self._refill()

            needs_sync = phase_needs_sync_seal(self.phase, str(result.kind))
            phase_snapshot = {
                "marks": dict(self._phase_marks),
                "origin_s": self._phase_origin_s,
                "last_s": self._phase_last_s,
            }
            kind_s = str(result.kind)
            # Canary "023243" falsified the "off-host daemon needs no PATH60
            # hold" assumption -- reason=43 recurred with the hold disabled
            # (see AsyncSealPipeline construction above for the full note).
            # Daemon and fork share the same hold policy until proven
            # otherwise by a clean live run. If a future canary shows cycle
            # time comfortably past ~95s with join_executing_on_search_critical
            # alone holding the line, THIS is the lever to relax next -- not
            # the join (that one lands "reason=43 recurred with search-time
            # gating disabled").
            hold_until_path60 = kind_holds_seal_until_path60(kind_s)
            job = self._async_seal.submit(
                result,
                ticket,
                needs_sync=needs_sync,
                before_seal=lambda snap=phase_snapshot: self._seed_phase_state_for_worker(snap),
                hold_until_path60=hold_until_path60,
            )
            if not hold_until_path60:
                # QUAL: no next STAGE25/SAFE_RETURN release path for this job.
                self._async_seal.end_search_critical()
            if needs_sync and job.error is not None:
                raise job.error
            # QUAL→ANCHOR is seal/mailbox-gated. After the third scheduled QUAL
            # physically completes, drain seals so PATH/ANCHOR cannot start
            # before baseline qualification is sealed (TP reason=50).
            if kind_s == "QUALIFICATION" and int(self._qual_scheduled) >= 3:
                self._async_seal.wait_done(job)
                self._drain_mailbox()
                if self._phase_name(self.phase) == CampaignPhase.QUALIFICATION.value:
                    self._async_seal.join_all()
                    self._drain_mailbox()
            return result
        except (RuntimeErrorR005, QueueError, OptimizerError, OSError) as exc:
            self._maybe_record_hard_stop_penalty(
                attempt=attempt, ticket=ticket, fault=exc
            )
            self._stop_or_recover_from_run_one_fault("runtime_or_evidence_fault:", exc)
            return None
        except Exception as exc:
            self._maybe_record_hard_stop_penalty(
                attempt=attempt, ticket=ticket, fault=exc
            )
            self._stop_or_recover_from_run_one_fault("unexpected_code_fault:", exc)
            return None

    def _advance_after_record(self, record: Any) -> None:
        """Seal-thread advance: sealed counters + mailbox intents only (no queue)."""

        self.cursor = record.candidate
        if record.kind == "QUALIFICATION":
            if record.qualification_eligible:
                self.qualification_passes += 1
                self._sync_qualification_state()
                if self.qualification_passes >= 3:
                    self._post_mailbox(
                        HostMailboxIntent(phase=self._ANCHOR)
                    )
            else:
                self._post_mailbox(
                    HostMailboxIntent(
                        cancel_pending=True,
                        reason="r008_qual_ineligible",
                        stop_reason="qualification_ineligible",
                        event="R008_QUAL_INELIGIBLE_STOP",
                    )
                )
            return
        self._emit_path_xy(record)
        if record.kind == self._ANCHOR:
            self._anchor_count += 1
            value = self._record_formal_objective(record)
            if value is not None:
                self._anchor_values.append(value)
            verdict = self._compute_anchor_verdict()
            if verdict == "passed":
                self._post_mailbox(
                    HostMailboxIntent(
                        phase=self._STAIRCASE,
                        anchor_verdict="passed",
                        event=(
                            "R008_ANCHOR_VALIDATION_PASSED:"
                            f"median={statistics.median(self._anchor_values[-self._anchor_target:]):.6g}"
                        ),
                    )
                )
            elif verdict == "failed":
                self._post_mailbox(
                    HostMailboxIntent(
                        anchor_verdict="failed",
                        cancel_pending=True,
                        reason="r008_anchor_validation_failed",
                        event=(
                            "R008_ANCHOR_VALIDATION_FAILED:"
                            f"count={self._anchor_count}:values={self._anchor_values}:"
                            f"expected={self._anchor_expected_mae_n:.3g}"
                        ),
                    )
                )
            return
        if record.kind == self._STAIRCASE:
            self._staircase_count += 1
            value = self._record_formal_objective(record)
            if value is not None:
                if (
                    self._last_staircase_mae is not None
                    and value > self._last_staircase_mae * 1.05
                    and self._staircase_count >= 2
                    and len(self._staircase) > 0
                ):
                    last_passed_index = min(
                        self._staircase_count - 2, len(self._staircase) - 1
                    )
                    if last_passed_index < 0:
                        return
                    edge_point = edge_half_anchor(self._staircase, last_passed_index)
                    self._persist_domain_rebind(
                        edge_point.log2_pd, source="empirical_edge"
                    )
                    self._post_mailbox(
                        HostMailboxIntent(
                            cancel_pending=True,
                            reason="r008_staircase_edge",
                            phase=self._SPACEFILL,
                            log2_pd_center=float(edge_point.log2_pd),
                            close_staircase=True,
                            event=(
                                f"R008_STAIRCASE_EDGE:mae={value:.6g}:"
                                f"prev={self._last_staircase_mae:.6g}"
                            ),
                        )
                    )
                    self._post_mailbox(
                        HostMailboxIntent(
                            event=(
                                f"R008_DOMAIN_REBIND:log2_pd_center={edge_point.log2_pd:.4g}"
                            ),
                        )
                    )
                self._last_staircase_mae = value
                if value <= self._mae_threshold_n:
                    self._post_mailbox(
                        HostMailboxIntent(
                            cancel_pending=True,
                            reason="r008_staircase_mae_threshold",
                            phase=CampaignPhase.RETEST,
                            retest_candidate=record.candidate,
                            clear_retest_records=True,
                            event="R008_STAIRCASE_HIT_ABSOLUTE_THRESHOLD",
                        )
                    )
            return
        if record.kind == self._SPACEFILL:
            self._spacefill_count += 1
            value = self._record_formal_objective(record)
            if value is not None and value <= self._mae_threshold_n:
                self._post_mailbox(
                    HostMailboxIntent(
                        cancel_pending=True,
                        reason="r008_spacefill_mae_threshold",
                        phase=CampaignPhase.RETEST,
                        retest_candidate=record.candidate,
                        clear_retest_records=True,
                        event="R008_SPACEFILL_HIT_ABSOLUTE_THRESHOLD",
                    )
                )
            return
        if record.kind == "BO_TRIAL":
            value = self._record_formal_objective(record)
            if value is not None and value <= self._mae_threshold_n:
                self._post_mailbox(
                    HostMailboxIntent(
                        cancel_pending=True,
                        reason="r008_application_threshold_incumbent_retests",
                        phase=CampaignPhase.RETEST,
                        retest_candidate=record.candidate,
                        clear_retest_records=True,
                        event="R008_BO_HIT_ABSOLUTE_THRESHOLD",
                    )
                )
            return
        if record.kind == "RETEST":
            self._retest_records.append(record)
            self._finish_retests()


class R008LiveAdapter(R006LiveAdapter):
    """Subclass that replaces the warm-start crawl with staircase + Sobol + BO."""

    def __init__(
        self,
        *args: Any,
        domain_path: str | Path | None = None,
        timing_canary: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if domain_path is None:
            raise R008LiveAdapterError("r008 live adapter requires a domain design path")
        self.domain_path = Path(domain_path)
        self.timing_canary = bool(timing_canary)
        self._domain = load_domain_design(self.domain_path)
        self._box = box_from_document(self._domain)
        self._anchor = anchor_from_document(self._domain)
        self._mae_threshold_n = float(self._domain["application_mae_threshold_n"])
        self._phase_plan = DEFAULT_PHASE_PLAN
        self._spacefill = scrambled_sobol(
            self._box,
            count=24,
            seed=8,
            include_kf_off_fraction=1.0,
        )
        self._staircase = build_staircase(self._anchor)

    @property
    def application_mae_threshold_n(self) -> float:
        return self._mae_threshold_n

    def phase_plan(self) -> tuple[PhaseSpec, ...]:
        return self._phase_plan

    def staircase_points(self) -> tuple[ParameterPoint, ...]:
        return tuple(level.point.to_parameter_point() for level in self._staircase)

    def spacefill_points(self) -> tuple[ParameterPoint, ...]:
        return tuple(point.to_parameter_point() for point in self._spacefill)

    def completion_reached(self, retest_mae_n: Sequence[float]) -> bool:
        if len(retest_mae_n) < 3:
            return False
        median = sorted(float(value) for value in retest_mae_n)[len(retest_mae_n) // 2]
        return median <= self._mae_threshold_n

    def build_verified_writer(
        self,
        inputs: Any,
        *,
        path_sample_sink: Any,
    ) -> R008MatureWriter:
        if self.writer_factory is not None:
            writer = self.writer_factory(inputs, path_sample_sink)
            if isinstance(writer, R008MatureWriter):
                return writer
            raise R008LiveAdapterError("r008 writer_factory must return R008MatureWriter")
        from .controller_seam import IntegralLimitCoordinate

        mature = build_verified_mature_r006_writer(
            inputs,
            contract=self.contract,
            parent_contract=self.parent_contract,
            path_sample_sink=path_sample_sink,
            force_integral_limit_n_s=float(
                IntegralLimitCoordinate().force_integral_limit_n_s
            ),
        )
        # Re-wrap the already-verified mature stack with the r008 kind map.
        return R008MatureWriter(mature.writer, injection=mature.injection)

    def run_forever(
        self,
        *,
        inputs: Any,
        queue: Any,
        ledger: ObservationLedger,
        optimizer: Any,
        writer: Any | None = None,
    ) -> str:
        self.thresholds = inputs.validate(contract=self.contract, parent_contract=self.parent_contract)
        if isinstance(queue, R008DurableQueueAdapter):
            pass
        elif isinstance(queue, R006V3DurableQueueAdapter):
            queue = R008DurableQueueAdapter(
                queue.root,
                campaign_id=queue.campaign_id,
                launch_profile_path=queue.launch_profile_path,
                release_manifest_sha256=getattr(queue, "_release_manifest_sha256", None),
            )
        else:
            raise R008LiveAdapterError("r008 live queue must be the r008/r006 V3 durable adapter")
        if not isinstance(ledger, ObservationLedger):
            raise R008LiveAdapterError("r008 live ledger must be ObservationLedger")
        if not isinstance(optimizer, (R008ProductionOptimizer, R006ProductionOptimizer)):
            raise R008LiveAdapterError("r008 live optimizer must be the managed CUDA adapter")
        r006_ledger = optimizer.r006_ledger
        if (
            r006_ledger.ledger.path.resolve() != ledger.path.resolve()
            or r006_ledger.campaign_fingerprint != self.contract.campaign_fingerprint
        ):
            raise R008LiveAdapterError("r008 optimizer evidence owner differs from the live ledger")
        if writer is None:
            writer = self.build_verified_writer(inputs, path_sample_sink=None)
        adapted = R008LiveWriterAdapter(writer, contract=self.parent_contract)
        sink_owner = getattr(writer, "writer", writer)
        if not hasattr(sink_owner, "_path_sample_sink"):
            raise R008LiveAdapterError("mature writer has no raw PATH evidence seam")
        # B3 Wave 1: open state-20 travel-vs-force sidecar beside the ledger.
        # PATH state-25 force+integral ring: hard-stop reconstructability.
        run_dir = Path(ledger.path).resolve().parent
        attach_state20_trace(sink_owner, run_dir)
        attach_state25_trace(sink_owner, run_dir)
        loop = R008HostLoop(
            domain_path=self.domain_path,
            r006_contract=self.contract,
            r006_ledger=r006_ledger,
            timing_canary=self.timing_canary,
            seal_daemon=True,
            contract=self.contract,  # type: ignore[arg-type]
            queue=queue,
            ledger=ledger,
            optimizer=optimizer,
            runtime=R005LiveRuntimePort(adapted),
            threshold_policy=self.thresholds,
            evidence_sink=lambda result: r006_ledger.append_attempt_result(
                result,
                epoch=result.epoch,
                point=_point_from_candidate(result.candidate),
            ),
        )
        # A1: STAGE25_START / SEARCH_DONE on first formal PATH sample.
        # CONTACT_SEARCH_START is emitted by R008HostLoop._phase after ARM.
        setattr(
            sink_owner,
            "_path_sample_sink",
            wrap_path_sample_sink_for_stage25(
                adapted.observe_r004_path_sample,
                on_stage25_start=loop.notify_stage25_start,
            ),
        )
        adapted.open(live_ack=R005_LIVE_ACK)
        try:
            while not loop.terminal:
                loop.run_one()
            self.last_stop_reason = loop.stop_reason
            self.last_events = tuple(loop.events)
            return loop.status
        finally:
            self.last_stop_reason = loop.stop_reason
            self.last_events = tuple(loop.events)
            adapted.close()


__all__ = [
    "DEFAULT_PHASE_PLAN",
    "HostMailboxIntent",
    "PENDING_WATERMARK",
    "PhaseSpec",
    "R008HostLoop",
    "R008LiveAdapter",
    "R008LiveAdapterError",
    "R008LiveWriterAdapter",
    "R008MatureWriter",
    "R008ObservationLedger",
    "R008ProductionOptimizer",
    "apply_timing_canary_skip_qual",
]
