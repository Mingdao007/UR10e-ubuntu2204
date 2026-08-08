"""Recover the commanded normal motion of a sealed trial.

The r006 raw bundles record ``filtered_normal_n`` at 500 Hz but no TCP pose,
so the commanded normal velocity has to be reconstructed rather than read.
That is exact rather than approximate: with the fixed reaction normal
``n = (0, 0, 1)`` that r004 hard-wires, the production outer loop's projectors
satisfy ``Phi_bar_O = n n^T`` and ``Phi_O n = 0``, so the normal channel closes
on itself and depends on nothing but the recorded force and the trial's own
``(P, D, kf)``.

``verify_scalar_kernel_against_production`` is the standing proof of that
claim: it drives the real ``compute_step5d_outer_loop`` and requires the
scalar recursion below to agree to machine precision.  If the production law
ever changes, that check fails rather than this module silently drifting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence
import hashlib
import json
import math

import numpy as np

from step5d_force_objective import ForceObjectiveError, ForcePathSample
from step5d_autotune_v4_r004.path_controller import derive_force_terms
from step5d_autotune_v4_r008.controller_seam import IntegralLimitCoordinate
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rotvec_to_matrix,
)


class GreyboxError(ValueError):
    """Sealed evidence is unreadable, untyped, or internally inconsistent."""


#: r004 hard-wires this reaction normal in ``calibrated_runtime.desired_twist``.
REACTION_NORMAL_BASE: tuple[float, float, float] = (0.0, 0.0, 1.0)
#: Live r008 wires ``IntegralLimitCoordinate`` into desired_twist; keep greybox
#: reconstruct/plant defaults aligned with that production seam (default 5.0).
FORCE_INTEGRAL_LIMIT_N_S = float(IntegralLimitCoordinate().force_integral_limit_n_s)
#: A representative in-contact pose from the r006 post-stop preflight.  Only
#: the rotation matters here, and only to exercise the production projectors;
#: the normal channel is invariant to it (see the verification below).
REFERENCE_POSE_BASE: tuple[float, float, float, float, float, float] = (
    0.4878364996905284,
    0.129344546129942,
    0.032986218191571326,
    3.120745523258862,
    -4.445050673910771e-06,
    0.06859131666301346,
)


@dataclass(frozen=True)
class BundleTrace:
    """One sealed 60 s PATH trial, typed and reduced to the normal channel."""

    attempt_sequence: int
    execution_id: str
    kind: str
    candidate: Mapping[str, float]
    bundle_sha256: str
    sealed_mae_n: float
    path_time_s: np.ndarray
    dt_s: np.ndarray
    filtered_normal_n: np.ndarray

    def __post_init__(self) -> None:
        n = int(self.path_time_s.size)
        if n < 2:
            raise GreyboxError("bundle trace is too short")
        if self.dt_s.size != n or self.filtered_normal_n.size != n:
            raise GreyboxError("bundle trace channels have different lengths")
        if not np.all(np.isfinite(self.filtered_normal_n)):
            raise GreyboxError("bundle trace force is not finite")
        if not np.all(self.dt_s > 0.0):
            raise GreyboxError("bundle trace dt is not positive")

    @property
    def force_p_gain(self) -> float:
        return float(self.candidate["force_p_gain"])

    @property
    def force_damping(self) -> float:
        return float(self.candidate["force_damping"])

    @property
    def force_i_gain(self) -> float:
        return float(self.candidate["force_i_gain"])

    @property
    def normal_filter_tau_s(self) -> float:
        return float(self.candidate["normal_filter_tau_s"])

    @property
    def kf(self) -> float:
        """The integral corner frequency in rad/s, as the outer loop sees it."""

        return float(derive_force_terms(_CandidateView(self.candidate))["kf"])


class _CandidateView:
    """Minimal duck type for ``derive_force_terms``/``assert_runtime_target``."""

    def __init__(self, candidate: Mapping[str, float]) -> None:
        self.force_p_gain = float(candidate["force_p_gain"])
        self.force_i_gain = float(candidate["force_i_gain"])
        self.force_damping = float(candidate["force_damping"])
        self.target_force_n = float(candidate["target_force_n"])


def normal_velocity(
    filtered_normal_n: np.ndarray,
    dt_s: np.ndarray,
    *,
    force_p_gain: float,
    force_damping: float,
    kf: float = 0.0,
    target_force_n: float = 5.0,
    integral_limit_n_s: float = FORCE_INTEGRAL_LIMIT_N_S,
    initial_velocity_m_s: float = 0.0,
    initial_integral_n_s: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (closing speed, penetration depth) for a recorded force trace.

    Sign convention: positive ``u`` presses along the approach normal, so
    positive ``z`` is deeper into the surface.  The production loop stores the
    base-frame ``xdot_p``; ``u = -n . xdot_p``.

    The initial velocity defaults to zero because the sealed bundles begin at
    ``path_time_s == 0`` with the outer loop already regulating in baseline
    mode.  The resulting transient decays with time constant ``1/D`` (36 ms at
    the r006 anchor) against a 60 s trial, and the steady-state velocity it
    replaces is ~3e-6 m/s.
    """

    n = int(filtered_normal_n.size)
    u = np.empty(n, dtype=float)
    integral = float(initial_integral_n_s)
    velocity = float(initial_velocity_m_s)
    limit = abs(float(integral_limit_n_s))
    for index in range(n):
        dt = float(dt_s[index])
        error = float(target_force_n) - float(filtered_normal_n[index])
        integral = min(max(integral + error * dt, -limit), limit)
        velocity = velocity * (1.0 - dt * force_damping) + dt * force_p_gain * (
            error + kf * integral
        )
        u[index] = velocity
    return u, np.cumsum(u * dt_s)


def verify_scalar_kernel_against_production(
    trace: BundleTrace,
    *,
    ticks: int = 512,
    tolerance_m_s: float = 1e-12,
) -> float:
    """Drive the real outer loop and return the worst normal-channel deviation.

    This is the load-bearing check for the whole grey-box: it asserts that the
    scalar recursion in :func:`normal_velocity` *is* the production normal
    channel, rather than a re-derivation of it.
    """

    terms = derive_force_terms(_CandidateView(trace.candidate))
    config = Step5dOuterLoopConfig(
        kp=float(trace.candidate["motion_kp"]),
        ko=float(trace.candidate["orientation_ko"]),
        kf=terms["kf"],
        Md_scalar=terms["Md"],
        Bd_scalar=terms["Bd"],
        force_target_n=float(trace.candidate["target_force_n"]),
        force_integral_limit_n_s=FORCE_INTEGRAL_LIMIT_N_S,
    )
    rotation = rotvec_to_matrix(REFERENCE_POSE_BASE[3:])
    normal = np.asarray(REACTION_NORMAL_BASE, dtype=float)
    state = Step5dOuterLoopState()

    count = min(int(ticks), int(trace.path_time_s.size))
    reference, _ = normal_velocity(
        trace.filtered_normal_n[:count],
        trace.dt_s[:count],
        force_p_gain=trace.force_p_gain,
        force_damping=trace.force_damping,
        kf=terms["kf"],
        target_force_n=float(trace.candidate["target_force_n"]),
    )

    worst = 0.0
    for index in range(count):
        # Put the recorded filtered load on the base-frame normal, exactly as
        # calibrated_runtime does before it calls the outer loop.
        force_base = normal * float(trace.filtered_normal_n[index])
        inputs = Step5dOuterLoopInputs(
            tcp_pose_base=REFERENCE_POSE_BASE,
            tcp_speed_base=(0.0,) * 6,
            force_tcp_n=tuple(rotation.T @ force_base),
            # A zero tangential error isolates the normal channel; the
            # projectors make the two independent in any case.
            x_pd_base=REFERENCE_POSE_BASE[:3],
            xdot_pd_base=(0.0, 0.0, 0.0),
            dt_s=float(trace.dt_s[index]),
            cmd_valid=True,
            control_reaction_normal_base=REACTION_NORMAL_BASE,
        )
        output = compute_step5d_outer_loop(
            config, state, inputs, include_diagnostics=False
        )
        state = output.next_state
        produced = -float(np.dot(np.asarray(output.xdot_p, dtype=float), normal))
        worst = max(worst, abs(produced - float(reference[index])))
    if worst > tolerance_m_s:
        raise GreyboxError(
            f"scalar normal kernel differs from the production outer loop by {worst:.3e} m/s"
        )
    return worst


def load_bundle_trace(path: Path, *, sealed_mae_n: float, candidate: Mapping[str, float]) -> BundleTrace:
    """Read one sealed raw bundle through the production sample type."""

    raw = Path(path).read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GreyboxError(f"raw bundle is not strict JSON: {path}") from exc
    samples = document.get("samples")
    if not isinstance(samples, list) or not samples:
        raise GreyboxError(f"raw bundle has no samples: {path}")

    times: list[float] = []
    forces: list[float] = []
    stamps: list[float] = []
    try:
        for entry in samples:
            typed = ForcePathSample.from_mapping(entry)
            if typed.timestamp_s is None:
                raise GreyboxError("raw sample has no timestamp")
            times.append(typed.path_time_s)
            forces.append(typed.filtered_normal_n)
            stamps.append(typed.timestamp_s)
    except ForceObjectiveError as exc:
        raise GreyboxError(f"raw bundle sample is untyped: {path}") from exc

    stamp_array = np.asarray(stamps, dtype=float)
    intervals = np.diff(stamp_array)
    if intervals.size == 0 or not np.all(intervals > 0.0):
        raise GreyboxError(f"raw bundle timestamps do not increase: {path}")
    # The runtime measures actual_dt_s from consecutive frames, so the first
    # tick's interval is not observable here; the median stands in for it.
    dt = np.concatenate(([float(np.median(intervals))], intervals))

    return BundleTrace(
        attempt_sequence=int(document["attempt_sequence"]),
        execution_id=str(document["execution_id"]),
        kind=str(document["kind"]),
        candidate=dict(candidate),
        bundle_sha256=hashlib.sha256(raw).hexdigest(),
        sealed_mae_n=float(sealed_mae_n),
        path_time_s=np.asarray(times, dtype=float),
        dt_s=dt,
        filtered_normal_n=np.asarray(forces, dtype=float),
    )


def load_run_traces(run_root: Path) -> tuple[BundleTrace, ...]:
    """Load every sealed trainable trial of one live run directory.

    The candidate and the sealed objective come from the durable ledger rather
    than the bundle, so a trace can never be paired with the wrong parameters.
    """

    run_root = Path(run_root)
    ledger_path = run_root / "r006-observations.jsonl"
    if not ledger_path.is_file():
        raise GreyboxError(f"run has no r006 ledger: {run_root}")

    by_sequence: dict[int, tuple[Mapping[str, float], float]] = {}
    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("record_type") == "header":
            continue
        objective = row.get("force_objective")
        if not objective or not row.get("sealed"):
            continue
        mae = objective.get("v2_mae_n")
        if mae is None:
            continue
        by_sequence[int(row["attempt_sequence"])] = (dict(row["candidate"]), float(mae))

    traces: list[BundleTrace] = []
    for path in sorted((run_root / "raw_force_evidence").glob("*.json")):
        sequence = int(json.loads(path.read_text(encoding="utf-8"))["attempt_sequence"])
        if sequence not in by_sequence:
            continue
        candidate, mae = by_sequence[sequence]
        traces.append(load_bundle_trace(path, sealed_mae_n=mae, candidate=candidate))
    if not traces:
        raise GreyboxError(f"run has no sealed trainable bundles: {run_root}")
    return tuple(sorted(traces, key=lambda trace: trace.attempt_sequence))


def iter_candidate_groups(traces: Sequence[BundleTrace]) -> Iterator[tuple[str, tuple[BundleTrace, ...]]]:
    """Group traces by identical physical candidate, for repeatability checks."""

    grouped: dict[str, list[BundleTrace]] = {}
    for trace in traces:
        key = json.dumps(dict(sorted(trace.candidate.items())), sort_keys=True)
        grouped.setdefault(key, []).append(trace)
    for key, group in grouped.items():
        yield key, tuple(group)


def _finite(value: Any, role: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise GreyboxError(f"{role} is not finite")
    return number


__all__ = [
    "FORCE_INTEGRAL_LIMIT_N_S",
    "REACTION_NORMAL_BASE",
    "REFERENCE_POSE_BASE",
    "BundleTrace",
    "GreyboxError",
    "iter_candidate_groups",
    "load_bundle_trace",
    "load_run_traces",
    "normal_velocity",
    "verify_scalar_kernel_against_production",
]
