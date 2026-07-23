#!/usr/bin/env python3
"""Deterministic offline replay primitives for Step5d-native autotune."""

from __future__ import annotations

import contextlib
import csv
import json
import math
import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import numpy as np

from step5d_autotune_contract import (
    EXACT_REPLAY_ENGINE_ID,
    CandidateReplayEvidence,
    CaptureManifest,
    ExecutionProfile,
    ForceCandidate,
    SearchAttestation,
    SearchTier,
    TrialSpec,
    canonical_json_bytes,
)
from step5d_autotune_v3.optimizer_policy import live_trust_region_step
from step5d_autotune_state_machine import (
    HostPacket,
    TpLoopState,
    TpPacket,
    verify_transcript,
)
from ur10e_artifact_store import (
    ArtifactRef,
    artifact_store,
    publish_artifact,
    resolve_artifact,
    sha256,
)


@dataclass(frozen=True)
class CadenceEvidence:
    profile_id: str
    sent_packets: int
    consumed_packets: int | None
    fresh_feedback_packets: int
    row_gap_over_20ms_count: int


@dataclass(frozen=True)
class DelayReplay:
    configured_delay_s: float
    measured_lag_s: float
    correlation: float
    nrmse: float


@dataclass(frozen=True)
class OrientationReplay:
    rate_limit_rad_s: float
    p95_error_rad: float
    max_error_rad: float
    saturation_duty: float
    entry_p95_error_rad: float
    direction_reversal_count: int
    qualified: bool


def cadence_eligible(evidence: CadenceEvidence) -> tuple[bool, tuple[str, ...]]:
    failures: list[str] = []
    if evidence.sent_packets <= 0:
        failures.append("no_sent_packets")
    if evidence.consumed_packets is None:
        failures.append("missing_tp_consumption_echo")
    elif evidence.consumed_packets / max(evidence.sent_packets, 1) < 0.98:
        failures.append("tp_consumption_ratio_below_0p98")
    if evidence.fresh_feedback_packets / max(evidence.sent_packets, 1) < 0.98:
        failures.append("fresh_feedback_ratio_below_0p98")
    if evidence.row_gap_over_20ms_count != 0:
        failures.append("row_gap_over_20ms")
    return not failures, tuple(failures)


def v34_replay_cadence_evidence() -> CadenceEvidence:
    """Bind the known v34 replay limitation: input transport had no TP echo."""

    return CadenceEvidence(
        profile_id="step5d_strict_rnn_ablation_v34",
        sent_packets=30065,
        consumed_packets=None,
        fresh_feedback_packets=30065,
        row_gap_over_20ms_count=0,
    )


def delay_replay(
    command: Sequence[float],
    *,
    delay_s: float,
    dt_s: float = 0.002,
) -> DelayReplay:
    if not 0.0 <= delay_s <= 0.020:
        raise ValueError("delay replay range is fixed to 0-20 ms")
    if dt_s <= 0.0 or not math.isfinite(dt_s):
        raise ValueError("dt_s must be finite and positive")
    sent = np.asarray(command, dtype=float)
    if sent.ndim != 1 or sent.size < 32 or not np.all(np.isfinite(sent)):
        raise ValueError("command must be a finite one-dimensional replay")
    shift = int(round(delay_s / dt_s))
    actual = np.empty_like(sent)
    if shift:
        actual[:shift] = sent[0]
        actual[shift:] = sent[:-shift]
    else:
        actual[:] = sent
    search = range(0, int(round(0.020 / dt_s)) + 1)
    best_lag = 0
    best_correlation = -1.0
    for lag in search:
        lhs = sent[: sent.size - lag] if lag else sent
        rhs = actual[lag:] if lag else actual
        if np.std(lhs) <= 1e-12 or np.std(rhs) <= 1e-12:
            correlation = 1.0 if np.allclose(lhs, rhs) else 0.0
        else:
            correlation = float(np.corrcoef(lhs, rhs)[0, 1])
        if correlation > best_correlation:
            best_correlation = correlation
            best_lag = lag
    aligned_sent = sent[: sent.size - best_lag] if best_lag else sent
    aligned_actual = actual[best_lag:] if best_lag else actual
    rmse = float(np.sqrt(np.mean((aligned_sent - aligned_actual) ** 2)))
    scale = max(float(np.max(aligned_sent) - np.min(aligned_sent)), 1e-12)
    return DelayReplay(
        configured_delay_s=delay_s,
        measured_lag_s=best_lag * dt_s,
        correlation=best_correlation,
        nrmse=rmse / scale,
    )


def rate_limited_orientation_replay(
    target_rad: Sequence[float],
    *,
    rate_limit_rad_s: float,
    dt_s: float = 0.002,
) -> OrientationReplay:
    if rate_limit_rad_s not in {0.010, 0.015, 0.020, 0.030}:
        raise ValueError("normal rate must be .010/.015/.020/.030 rad/s")
    target = np.asarray(target_rad, dtype=float)
    if target.ndim != 1 or target.size < int(10.0 / dt_s):
        raise ValueError("orientation replay must contain at least 10 seconds")
    actual = np.empty_like(target)
    actual[0] = target[0]
    limited = np.zeros_like(target, dtype=bool)
    step = rate_limit_rad_s * dt_s
    for index in range(1, target.size):
        requested = target[index] - actual[index - 1]
        applied = float(np.clip(requested, -step, step))
        actual[index] = actual[index - 1] + applied
        limited[index] = abs(requested) > step + 1e-15
    error = np.abs(target - actual)
    times = np.arange(target.size) * dt_s
    entry = error[(times >= 0.0) & (times < 5.0)]
    qualified = error[(times >= 5.0) & (times < 60.0)]
    limited_qualified = limited[(times >= 5.0) & (times < 60.0)]
    velocity = np.diff(target)
    nonzero_signs = np.sign(velocity[np.abs(velocity) > 1e-15])
    reversals = int(np.count_nonzero(nonzero_signs[1:] != nonzero_signs[:-1]))
    p95 = float(np.percentile(qualified, 95))
    maximum = float(np.max(qualified))
    duty = float(np.mean(limited_qualified))
    return OrientationReplay(
        rate_limit_rad_s=rate_limit_rad_s,
        p95_error_rad=p95,
        max_error_rad=maximum,
        saturation_duty=duty,
        entry_p95_error_rad=float(np.percentile(entry, 95)),
        direction_reversal_count=reversals,
        qualified=p95 <= 0.036 and maximum <= 0.05 and duty <= 0.05,
    )


def tier_corner_candidates(tier: SearchTier) -> tuple[ForceCandidate, ...]:
    radius = tier.p_d_radius_octaves
    i_radius = tier.positive_i_radius_octaves
    candidates: list[ForceCandidate] = []
    for p in (-radius, radius):
        for damping in (-radius, radius):
            if tier is SearchTier.T1:
                candidates.append(ForceCandidate.from_log2(p=p, damping=damping, i=0.0))
            else:
                for i in (-i_radius, i_radius):
                    candidates.append(
                        ForceCandidate.from_log2(p=p, damping=damping, i=i)
                    )
                candidates.append(
                    ForceCandidate.from_log2(p=p, damping=damping, i_off=True)
                )
    return tuple(candidates)


def axial_frontier(
    *,
    axis: str,
    direction: int,
    tier: SearchTier,
) -> tuple[ForceCandidate, ...]:
    if axis not in {"p", "damping", "i"} or direction not in {-1, 1}:
        raise ValueError("frontier requires p/damping/i and direction +/-1")
    if axis == "i" and tier is SearchTier.T1:
        raise ValueError("T1 integral is frozen at seed")
    radius = (
        tier.positive_i_radius_octaves if axis == "i" else tier.p_d_radius_octaves
    )
    steps = int(round(radius / 0.25))
    rows = [ForceCandidate()]
    for index in range(1, steps + 1):
        coordinate = direction * index * 0.25
        kwargs = {"p": 0.0, "damping": 0.0, "i": 0.0}
        kwargs[axis] = coordinate
        rows.append(ForceCandidate.from_log2(**kwargs))
    if any(
        not live_trust_region_step(previous, current)
        for previous, current in zip(rows, rows[1:])
    ):
        raise AssertionError("frontier skipped a quarter-octave live point")
    return tuple(rows)


def synthetic_safe_transcript(host: HostPacket) -> tuple[TpPacket, ...]:
    states = (
        TpLoopState.ARMED,
        TpLoopState.RUN,
        TpLoopState.TERMINAL,
        TpLoopState.RETRACT,
        TpLoopState.RETURN,
        TpLoopState.HOME_VERIFY,
        TpLoopState.WAIT_ACK,
    )
    return (
        TpPacket(0, 0, TpLoopState.READY_HOME, 0, 0, 0, 0),
        *(
            TpPacket(
                host.campaign_epoch,
                host.trial_id,
                state,
                host.candidate_token,
                1 if state >= TpLoopState.TERMINAL else 0,
                host.execution_profile_id,
                host.command_seq,
            )
            for state in states
        ),
    )


def transcript_eligible(host: HostPacket, packets: Iterable[TpPacket]) -> bool:
    return verify_transcript(host, packets)[0]


class ProcessCleanupLedger:
    """Pure model used to verify every launched helper receives cleanup."""

    def __init__(self) -> None:
        self._launched: set[int] = set()
        self._terminated: set[int] = set()

    def launch(self, pid: int) -> None:
        if pid <= 0 or pid in self._launched:
            raise ValueError("PID must be fresh and positive")
        self._launched.add(pid)

    def terminate(self, pid: int) -> None:
        if pid not in self._launched:
            raise ValueError("cannot terminate an unknown helper")
        self._terminated.add(pid)

    @property
    def survivors(self) -> tuple[int, ...]:
        return tuple(sorted(self._launched - self._terminated))

    def assert_closed(self) -> None:
        if self.survivors:
            raise RuntimeError(f"helper process cleanup incomplete: {self.survivors}")


@contextlib.contextmanager
def managed_helper_process(
    command: Sequence[str],
    *,
    terminate_timeout_s: float = 2.0,
) -> Iterator[subprocess.Popen[bytes]]:
    """Launch a replay helper without a shell and prove its process group closes."""

    argv = tuple(command)
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise ValueError("helper command must contain non-empty string arguments")
    if not math.isfinite(terminate_timeout_s) or terminate_timeout_s <= 0.0:
        raise ValueError("terminate_timeout_s must be finite and positive")
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=terminate_timeout_s)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=terminate_timeout_s)
        if process.poll() is None:
            raise RuntimeError(f"helper process group survived cleanup: {process.pid}")


def _candidate_transition(candidate: ForceCandidate, target: ForceCandidate) -> tuple[str, int]:
    deltas = {
        "p": target.log2_p - candidate.log2_p,
        "damping": target.log2_damping - candidate.log2_damping,
    }
    if candidate.i_mode == "positive" and target.i_mode == "positive":
        deltas["i"] = target.log2_i - candidate.log2_i
    elif candidate.i_mode != target.i_mode:
        raise ValueError("candidate-bound T3 replay cannot attest an I-mode transition")
    changed = [axis for axis, delta in deltas.items() if not math.isclose(delta, 0.0, abs_tol=1e-9)]
    if len(changed) != 1 or not live_trust_region_step(candidate, target):
        raise ValueError("candidate-bound replay target must be one continuous quarter-octave step")
    axis = changed[0]
    return axis, int(math.copysign(1, deltas[axis]))


def _finite_max(values: Sequence[float], *, failure_value: float = 1e9) -> float:
    if not values or any(not math.isfinite(float(value)) for value in values):
        return failure_value
    return max(float(value) for value in values)


def _run_candidate_bound_exact_replay(
    *,
    root: Path,
    trace_path: Path,
    candidate: ForceCandidate,
    execution_profile: ExecutionProfile,
) -> dict[str, int | float]:
    """Replay the exact v35 outer/RNN/reference/slew seam with CUDA only."""

    # Heavy production dependencies remain lazy so pure contract imports and
    # artifact verification never initialize CUDA or robot-side libraries.
    from kunwei_rtde_bridge import (
        STEP5D_LIVEPREP_TRUTH_PATH,
        rnn_target_state_from_outer_loop,
        step5d_kin,
        step5d_omega_bounds,
        step5d_tcp_jacobian_base,
        step5d_v30_bridge_control_step,
    )
    from run_step5d_v30_remote_timing import load_rows, prepare_rows
    from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
    from step5d_control_contract import (
        DeferredV30Diagnostics,
        SafetyEnvelope,
        Step5dObservation,
        StrictRnnControlPolicy,
    )
    from step5d_paper_outer_loop import (
        Step5dOuterLoopConfig,
        Step5dOuterLoopInputs,
        Step5dOuterLoopState,
        compute_step5d_outer_loop,
    )

    rows = load_rows(trace_path)
    prepared_rows = prepare_rows(rows)
    if not prepared_rows:
        raise ValueError("candidate-bound replay trace has no compatible v35 rows")
    model_bundle = step5d_kin.build_calibrated_model()
    audit_rows = step5d_kin.finite_run_rows(trace_path)
    tcp_offset = step5d_kin.infer_tcp_offset(model_bundle, audit_rows)["mean"]
    q_min = model_bundle.model.lowerPositionLimit
    q_max = model_bundle.model.upperPositionLimit
    solver = StrictTaseRnnSolver(
        StrictRnnConfig(
            paper_truth_path=STEP5D_LIVEPREP_TRUTH_PATH,
            qdot_limit_rad_s=0.5,
            epsilon=0.010,
            sigr_exponent_r=0.8,
            inner_iterations=512,
            backend="cupy",
        )
    )
    if str(solver.config.backend) != "cupy":
        raise RuntimeError("candidate-bound exact replay forbids CPU fallback")
    policy = StrictRnnControlPolicy(solver)
    safety = SafetyEnvelope(qdot_cap_rad_s=0.5)
    deferred = DeferredV30Diagnostics(capacity=len(prepared_rows))
    mapping = candidate.native_mapping
    outer_config = Step5dOuterLoopConfig(
        kp=1.5,
        ko=0.4,
        kf=mapping["kf"],
        Md_scalar=mapping["Md"],
        Bd_scalar=mapping["Bd"],
        force_integral_limit_n_s=1.0,
        force_target_n=12.0,
        delay_T_s=0.002,
        force_sign_convention="step5_step6_positive_normal_load",
    )
    outer_state = Step5dOuterLoopState()
    previous_qdot: tuple[float, float, float, float, float, float] | None = None
    residuals: list[float] = []
    oracle_deltas: list[float] = []
    qdot_maxima: list[float] = []
    slew_violations: list[float] = []
    accepted_rows = 0
    active_bounds_rows = 0
    structural_failure_rows = 0
    for index, row in enumerate(prepared_rows):
        outer = compute_step5d_outer_loop(
            outer_config,
            outer_state,
            Step5dOuterLoopInputs(
                tcp_pose_base=row.pose,
                tcp_speed_base=row.speed,
                force_tcp_n=row.force_tcp,
                control_reaction_normal_base=row.reaction_tuple,
                x_pd_base=(row.desired_x_m, row.desired_y_m, row.pose[2]),
                xdot_pd_base=(row.desired_vx_m_s, row.desired_vy_m_s, 0.0),
                dt_s=0.002,
                cmd_valid=True,
            ),
            include_diagnostics="compact",
        )
        outer_state = outer.next_state
        jacobian = step5d_tcp_jacobian_base(model_bundle, row.q, tcp_offset)
        lower, upper = step5d_omega_bounds(
            row.q,
            q_min,
            q_max,
            alpha_s_inv=1.0,
            qdot_limit_rad_s=0.5,
        )
        target = rnn_target_state_from_outer_loop(
            outer,
            J=jacobian,
            omega_minus=lower,
            omega_plus=upper,
            dt_s=0.002,
            epsilon=0.010,
            r=0.8,
        )
        desired = tuple(float(value) for value in target["xdot_c"])
        reaction = row.reaction_tuple
        observation = Step5dObservation(
            sequence=index,
            timestamp_s=index * 0.002,
            q=tuple(float(value) for value in row.q),
            qd=tuple(float(value) for value in row.qd),
            tcp_pose=row.pose,
            tcp_twist=row.speed,
            wrench=(*row.force_tcp, 0.0, 0.0, 0.0),
            jacobian=tuple(
                tuple(float(value) for value in matrix_row)
                for matrix_row in jacobian
            ),
            desired_twist=desired,
            reaction_normal=reaction,
            approach_normal=tuple(-float(value) for value in reaction),
            command_frame="base",
            normal_frame="base",
            path_time_s=index * 0.002,
            force_error_n=float(outer.diagnostics["e_f"]),
            orientation_error_rad=float(
                outer.diagnostics["outer_orientation_angle_rad"]
            ),
            omega_minus=tuple(float(value) for value in lower),
            omega_plus=tuple(float(value) for value in upper),
            dt_s=0.002,
            normal_motion_policy="frame_contract_only",
        )

        def warm_start(governed: Step5dObservation) -> None:
            solver.warm_start(
                J=governed.jacobian,
                xdot_c=governed.desired_twist,
                omega_minus=governed.omega_minus,
                omega_plus=governed.omega_plus,
            )

        prior = previous_qdot or (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        result = step5d_v30_bridge_control_step(
            observation,
            policy,
            previous_qdot=previous_qdot,
            safety_envelope=safety,
            deferred_diagnostics=deferred,
            prepare_policy=warm_start if index == 0 else None,
            max_slew_rad_s2=execution_profile.host_qdot_slew_rad_s2,
        )
        raw = np.asarray(result.raw_candidate.qdot, dtype=float)
        post_slew = np.asarray(result.candidate.qdot, dtype=float)
        residuals.append(float(result.candidate.residual_norm))
        qdot_maxima.append(float(np.max(np.abs(raw))))
        active_bounds_rows += int(result.raw_candidate.active_bounds_count > 0)
        if result.dls_shadow is None:
            oracle_deltas.append(1e9)
            structural_failure_rows += 1
        else:
            oracle_deltas.append(
                float(
                    np.linalg.norm(
                        np.asarray(result.dls_shadow.qdot, dtype=float) - raw
                    )
                )
            )
        delta_limit = execution_profile.host_qdot_slew_rad_s2 * 0.002
        slew_violations.append(
            max(0.0, float(np.max(np.abs(post_slew - np.asarray(prior)))) - delta_limit)
        )
        if result.decision.accepted:
            accepted_rows += 1
            previous_qdot = result.decision.qdot
        else:
            previous_qdot = None
            if result.decision.action == "stop":
                structural_failure_rows += 1
    return {
        "replayed_rows": len(prepared_rows),
        "accepted_rows": accepted_rows,
        "active_bounds_rows": active_bounds_rows,
        "structural_failure_rows": structural_failure_rows,
        "rnn_residual_max": _finite_max(residuals),
        "rnn_oracle_qdot_delta_max_rad_s": _finite_max(oracle_deltas),
        "qdot_max_abs_rad_s": _finite_max(qdot_maxima),
        "slew_violation_max_rad_s": _finite_max(slew_violations),
    }


def _candidate_replay_report_payload(
    *,
    source_trial_uid: str,
    source_fingerprint: str,
    config_fingerprint: str,
    profile: ExecutionProfile,
    plant_epoch: int,
    from_candidate: ForceCandidate,
    to_candidate: ForceCandidate,
    trace_ref: ArtifactRef,
    metrics: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": EXACT_REPLAY_ENGINE_ID,
        "bindings": {
            "source_trial_uid": source_trial_uid,
            "source_fingerprint": source_fingerprint,
            "config_fingerprint": config_fingerprint,
            "profile_id": profile.profile_id,
            "plant_epoch": plant_epoch,
            "from_candidate": from_candidate.payload(),
            "to_candidate": to_candidate.payload(),
            "next_candidate_uid": to_candidate.candidate_uid,
            "trace_artifact_ref": trace_ref.as_dict(),
        },
        "execution_profile": profile.payload(),
        "runtime": {
            "backend": "cupy",
            "inner_iterations": 512,
            "epsilon": 0.010,
            "sigr_exponent_r": 0.8,
            "qdot_cap_rad_s": 0.5,
            "cpu_fallback_allowed": False,
            "normal_motion_policy": "frame_contract_only",
        },
        "metrics": dict(metrics),
    }


def build_candidate_bound_search_attestation(
    *,
    root: Path,
    source_trial: TrialSpec,
    source_manifest: CaptureManifest,
    trace_path: Path,
    to_candidate: ForceCandidate,
    artifact_store_override: Path | None = None,
) -> SearchAttestation:
    """Run, publish, and bind one fresh exact candidate replay attestation."""

    root = root.resolve()
    trace_path = trace_path.resolve()
    if not trace_path.is_file():
        raise FileNotFoundError(f"candidate replay trace not found: {trace_path}")
    if source_manifest.trial_uid != source_trial.trial_uid:
        raise ValueError("candidate replay source manifest does not bind source trial")
    if source_manifest.csv_sha256 != sha256(trace_path):
        raise ValueError("candidate replay trace bytes do not match source manifest")
    if (
        source_manifest.source_fingerprint_pre != source_trial.source_fingerprint
        or source_manifest.source_fingerprint_post != source_trial.source_fingerprint
        or source_manifest.config_fingerprint_pre != source_trial.config_fingerprint
        or source_manifest.config_fingerprint_post != source_trial.config_fingerprint
    ):
        raise ValueError("candidate replay source manifest fingerprint closure mismatches trial")
    if not (
        source_manifest.returned_safe
        and source_manifest.immutable_bundle_written
        and source_manifest.hashes_complete
    ):
        raise ValueError("candidate replay source requires immutable safe-closure evidence")
    axis, direction = _candidate_transition(source_trial.candidate, to_candidate)
    store = artifact_store(root, artifact_store_override)
    trace_ref = publish_artifact(trace_path, store=store)
    metrics = _run_candidate_bound_exact_replay(
        root=root,
        trace_path=trace_path,
        candidate=to_candidate,
        execution_profile=source_trial.execution_profile,
    )
    provisional = CandidateReplayEvidence(
        engine_id=EXACT_REPLAY_ENGINE_ID,
        trace_artifact_ref=trace_ref,
        report_artifact_ref=trace_ref,
        **metrics,
    )
    report_payload = _candidate_replay_report_payload(
        source_trial_uid=source_trial.trial_uid,
        source_fingerprint=source_trial.source_fingerprint,
        config_fingerprint=source_trial.config_fingerprint,
        profile=source_trial.execution_profile,
        plant_epoch=source_trial.plant_epoch,
        from_candidate=source_trial.candidate,
        to_candidate=to_candidate,
        trace_ref=trace_ref,
        metrics=provisional.metrics_payload(),
    )
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix="step5d-candidate-replay-",
            suffix=".json",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            output.write(canonical_json_bytes(report_payload))
            output.flush()
            os.fsync(output.fileno())
        report_ref = publish_artifact(temporary, store=store)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    evidence = CandidateReplayEvidence(
        engine_id=EXACT_REPLAY_ENGINE_ID,
        trace_artifact_ref=trace_ref,
        report_artifact_ref=report_ref,
        **metrics,
    )
    attestation = SearchAttestation(
        profile_id=source_trial.execution_profile.profile_id,
        plant_epoch=source_trial.plant_epoch,
        source_trial_uid=source_trial.trial_uid,
        latest_trace_sha256=trace_ref.sha256,
        replay_source_fingerprint=source_trial.source_fingerprint,
        replay_config_fingerprint=source_trial.config_fingerprint,
        from_candidate=source_trial.candidate,
        to_candidate=to_candidate,
        next_candidate_uid=to_candidate.candidate_uid,
        outward_axis=axis,
        outward_direction=direction,
        replay_evidence=evidence,
    )
    if not attestation.exact_replay_passed:
        raise ValueError("candidate-bound exact RNN/oracle/slew replay did not pass")
    verify_candidate_bound_search_attestation(
        attestation,
        root=root,
        execution_profile=source_trial.execution_profile,
        artifact_store_override=artifact_store_override,
    )
    return attestation


def verify_candidate_bound_search_attestation(
    attestation: SearchAttestation,
    *,
    root: Path,
    execution_profile: ExecutionProfile,
    artifact_store_override: Path | None = None,
) -> tuple[Path, Path]:
    """Resolve immutable bytes and verify the report exactly matches its proof."""

    if not isinstance(attestation, SearchAttestation):
        raise ValueError("attestation must be SearchAttestation")
    if execution_profile.profile_id != attestation.profile_id:
        raise ValueError("replay report execution profile does not bind attestation")
    if not attestation.exact_replay_passed:
        raise ValueError("candidate-bound replay metrics do not pass fixed thresholds")
    store = artifact_store(root.resolve(), artifact_store_override)
    trace_path = resolve_artifact(
        attestation.replay_evidence.trace_artifact_ref, store=store
    )
    report_path = resolve_artifact(
        attestation.replay_evidence.report_artifact_ref, store=store
    )
    if sha256(trace_path) != attestation.latest_trace_sha256:
        raise ValueError("resolved candidate replay trace does not match attestation")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("candidate replay report is unreadable") from exc
    expected = _candidate_replay_report_payload(
        source_trial_uid=attestation.source_trial_uid,
        source_fingerprint=attestation.replay_source_fingerprint,
        config_fingerprint=attestation.replay_config_fingerprint,
        profile=execution_profile,
        plant_epoch=attestation.plant_epoch,
        from_candidate=attestation.from_candidate,
        to_candidate=attestation.to_candidate,
        trace_ref=attestation.replay_evidence.trace_artifact_ref,
        metrics=attestation.replay_evidence.metrics_payload(),
    )
    if canonical_json_bytes(report) != canonical_json_bytes(expected):
        raise ValueError("candidate replay report payload does not match attestation")
    return trace_path, report_path
