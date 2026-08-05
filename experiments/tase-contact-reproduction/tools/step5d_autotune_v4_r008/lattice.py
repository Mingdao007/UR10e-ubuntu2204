"""r008 reparameterized lattice: axes on log2(P/D), log2(D), τ, kf, Ko, Kp.

Every emitted physical candidate is required to land on the same quarter-octave
grid that ``R006Candidate._quarter_step`` already accepts, so r008 needs no TP
rotation.  Physical P/D/I are still transported through the frozen r006
candidate decoder; only the search coordinates change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping, Sequence
import math

import numpy as np

from step5d_autotune_v4_r006.contracts import (
    D_ANCHOR,
    I_ON_ANCHOR,
    KO_ANCHOR,
    KP_ANCHOR,
    P_ANCHOR,
    STEP_OCTAVE,
    TAU_ANCHOR,
)
from step5d_autotune_v4_r006.lattice import IMode, ParameterPoint


class R008LatticeError(ValueError):
    """An r008 lattice point or box is inconsistent."""


class _LatticeAcceptError(R008LatticeError):
    """Raised when a projected point is not on the frozen r006 lattice."""


#: Default design anchor in the reparameterized axes (plan Stage B seed).
DEFAULT_PD_RATIO = 1.0e-4
DEFAULT_D = 28.0
DEFAULT_TAU = 0.15
DEFAULT_KF = 0.6
DEFAULT_KO = 0.2
DEFAULT_KP = 1.5


def _quarter_steps_from_value(value: float, anchor: float) -> int:
    exponent = math.log2(float(value) / float(anchor))
    scaled = exponent / STEP_OCTAVE
    step = int(round(scaled))
    if not math.isclose(scaled, float(step), rel_tol=0.0, abs_tol=1e-9):
        raise R008LatticeError(f"value {value} is not on the quarter-octave lattice of {anchor}")
    return step


def snap_to_quarter(value: float, anchor: float) -> float:
    step = int(round(math.log2(float(value) / float(anchor)) / STEP_OCTAVE))
    return float(anchor * (2.0 ** (step * STEP_OCTAVE)))


@dataclass(frozen=True)
class BoxRegion:
    """Axis-aligned box in the reparameterized coordinates."""

    log2_pd_min: float
    log2_pd_max: float
    log2_d_min: float
    log2_d_max: float
    log2_tau_min: float
    log2_tau_max: float
    kf_off_allowed: bool
    log2_kf_min: float
    log2_kf_max: float
    log2_ko_min: float
    log2_ko_max: float
    log2_kp_min: float
    log2_kp_max: float

    def contains_logs(
        self,
        *,
        log2_pd: float,
        log2_d: float,
        log2_tau: float,
        kf_off: bool,
        log2_kf: float | None,
        log2_ko: float,
        log2_kp: float,
    ) -> bool:
        if not (self.log2_pd_min <= log2_pd <= self.log2_pd_max):
            return False
        if not (self.log2_d_min <= log2_d <= self.log2_d_max):
            return False
        if not (self.log2_tau_min <= log2_tau <= self.log2_tau_max):
            return False
        if not (self.log2_ko_min <= log2_ko <= self.log2_ko_max):
            return False
        if not (self.log2_kp_min <= log2_kp <= self.log2_kp_max):
            return False
        if kf_off:
            return bool(self.kf_off_allowed)
        if log2_kf is None:
            return False
        return self.log2_kf_min <= log2_kf <= self.log2_kf_max


@dataclass(frozen=True)
class R008Point:
    """One search point in reparameterized axes, snapped to the r006 lattice."""

    log2_pd: float
    log2_d: float
    log2_tau: float
    kf_off: bool
    log2_kf: float | None
    log2_ko: float
    log2_kp: float

    @property
    def pd_ratio(self) -> float:
        return float(2.0 ** self.log2_pd)

    @property
    def damping(self) -> float:
        return float(2.0 ** self.log2_d)

    @property
    def force_p_gain(self) -> float:
        return float(self.pd_ratio * self.damping)

    @property
    def tau_s(self) -> float:
        return float(2.0 ** self.log2_tau)

    @property
    def kf(self) -> float:
        if self.kf_off or self.log2_kf is None:
            return 0.0
        return float(2.0 ** self.log2_kf)

    @property
    def force_i_gain(self) -> float:
        return float(self.kf * self.force_p_gain)

    @property
    def orientation_ko(self) -> float:
        return float(2.0 ** self.log2_ko)

    @property
    def motion_kp(self) -> float:
        return float(2.0 ** self.log2_kp)

    def to_parameter_point(self) -> ParameterPoint:
        """Project onto the frozen r006 ParameterPoint grid."""

        p = snap_to_quarter(self.force_p_gain, P_ANCHOR)
        d = snap_to_quarter(self.damping, D_ANCHOR)
        tau = snap_to_quarter(self.tau_s, TAU_ANCHOR)
        ko = snap_to_quarter(self.orientation_ko, KO_ANCHOR)
        kp = snap_to_quarter(self.motion_kp, KP_ANCHOR)
        if self.kf_off or self.force_i_gain <= 0.0:
            return ParameterPoint(
                p_step=_quarter_steps_from_value(p, P_ANCHOR),
                d_step=_quarter_steps_from_value(d, D_ANCHOR),
                tau_step=_quarter_steps_from_value(tau, TAU_ANCHOR),
                i_mode=IMode.OFF,
                i_step=None,
                ko_step=_quarter_steps_from_value(ko, KO_ANCHOR),
                kp_step=_quarter_steps_from_value(kp, KP_ANCHOR),
            )
        i_gain = snap_to_quarter(self.force_i_gain, I_ON_ANCHOR)
        return ParameterPoint(
            p_step=_quarter_steps_from_value(p, P_ANCHOR),
            d_step=_quarter_steps_from_value(d, D_ANCHOR),
            tau_step=_quarter_steps_from_value(tau, TAU_ANCHOR),
            i_mode=IMode.ON,
            i_step=_quarter_steps_from_value(i_gain, I_ON_ANCHOR),
            ko_step=_quarter_steps_from_value(ko, KO_ANCHOR),
            kp_step=_quarter_steps_from_value(kp, KP_ANCHOR),
        )

    def to_r006_candidate_fields(self) -> dict[str, float | bool]:
        point = self.to_parameter_point()
        return {
            "force_p_gain": point.p_gain,
            "force_i_gain": point.i_gain,
            "force_damping": point.d_gain,
            "normal_filter_tau_s": point.tau_s,
            "orientation_ko": point.ko,
            "motion_kp": point.kp,
            "target_force_n": 5.0,
            "i_off": point.i_mode is IMode.OFF,
        }

    def to_r006_candidate(self):
        """Optional heavy import — only used when the live adapter is available."""

        from step5d_autotune_v4_r006.live_adapter import R006Candidate

        return R006Candidate.from_point(self.to_parameter_point())

    def key(self) -> tuple[object, ...]:
        point = self.to_parameter_point()
        return point.key


def point_from_physical(
    *,
    force_p_gain: float,
    force_damping: float,
    normal_filter_tau_s: float,
    force_i_gain: float,
    orientation_ko: float,
    motion_kp: float,
) -> R008Point:
    pd = float(force_p_gain) / float(force_damping)
    kf_off = float(force_i_gain) <= 0.0
    kf = 0.0 if kf_off else float(force_i_gain) / float(force_p_gain)
    return R008Point(
        log2_pd=math.log2(pd),
        log2_d=math.log2(float(force_damping)),
        log2_tau=math.log2(float(normal_filter_tau_s)),
        kf_off=kf_off,
        log2_kf=None if kf_off else math.log2(kf),
        log2_ko=math.log2(float(orientation_ko)),
        log2_kp=math.log2(float(motion_kp)),
    )


def default_box() -> BoxRegion:
    """Plan Stage B seed box."""

    return BoxRegion(
        log2_pd_min=math.log2(DEFAULT_PD_RATIO) - 3.0,
        log2_pd_max=math.log2(DEFAULT_PD_RATIO) + 2.0,
        log2_d_min=math.log2(7.0),
        log2_d_max=math.log2(224.0),
        log2_tau_min=math.log2(0.05),
        log2_tau_max=math.log2(0.7),
        kf_off_allowed=True,
        log2_kf_min=math.log2(0.05),
        log2_kf_max=math.log2(2.0),
        log2_ko_min=math.log2(0.05),
        log2_ko_max=math.log2(0.8),
        log2_kp_min=math.log2(1.5),
        log2_kp_max=math.log2(6.0),
    )


def default_anchor() -> R008Point:
    return R008Point(
        log2_pd=math.log2(DEFAULT_PD_RATIO),
        log2_d=math.log2(DEFAULT_D),
        log2_tau=math.log2(DEFAULT_TAU),
        kf_off=False,
        log2_kf=math.log2(DEFAULT_KF),
        log2_ko=math.log2(DEFAULT_KO),
        log2_kp=math.log2(DEFAULT_KP),
    )


def r006_anchor_point() -> R008Point:
    """The frozen r006-era operating point, reparameterized onto r008 axes.

    Used for Stage D2's level-0 anchor-repeat validation: before climbing the
    r008 gain staircase, re-measure this exact point live and confirm the
    result still lands near the historical ~5.58 N anchor MAE.  If it does
    not, the online plant has drifted from what Stage A identified and the
    r008 anchor/domain must be re-derived, not searched around blind.
    """

    return R008Point(
        log2_pd=math.log2(P_ANCHOR / D_ANCHOR),
        log2_d=math.log2(D_ANCHOR),
        log2_tau=math.log2(TAU_ANCHOR),
        kf_off=True,
        log2_kf=None,
        log2_ko=math.log2(KO_ANCHOR),
        log2_kp=math.log2(KP_ANCHOR),
    )


def live_acquisition_unstable(*, force_damping: float, normal_filter_tau_s: float) -> bool:
    """Live-only veto for corners that overshoot then lose the 5 N acquire band.

    Seeded by ``live_20260803_025250_stage_d`` SPACEFILL #1 (D≈9.9, τ≈0.59) which
    raised ``five_newton_acquisition_timeout`` and revoked authority.  Offline
    ``offline_hard_veto`` does not cover this acquire-hold failure mode.
    """

    damping = float(force_damping)
    tau = float(normal_filter_tau_s)
    if not math.isfinite(damping) or not math.isfinite(tau) or damping <= 0.0 or tau <= 0.0:
        return True
    if damping < 10.0:
        return True
    if damping < 14.0 and tau > 0.30:
        return True
    return False


def scrambled_sobol(
    box: BoxRegion,
    *,
    count: int,
    seed: int = 8,
    include_kf_off_fraction: float = 0.15,
    apply_live_acquisition_veto: bool = True,
) -> tuple[R008Point, ...]:
    """Scrambled Sobol samples in the box, snapped to the r006 lattice and deduped."""

    if count <= 0:
        raise R008LatticeError("sobol count must be positive")
    # Oversample when the live acquisition veto is on so SPACEFILL still fills.
    draw = int(count) * (4 if apply_live_acquisition_veto else 1)
    # Continuous axes: pd, d, tau, kf, ko, kp.  kf_off is a Bernoulli draw.
    try:
        from scipy.stats import qmc

        engine = qmc.Sobol(d=6, scramble=True, seed=int(seed))
        raw = engine.random(n=int(draw))
    except ImportError:
        # Control runtime ships numpy but not scipy; keep a deterministic
        # scrambled-LHS cover so live host admission does not depend on scipy.
        rng = np.random.default_rng(int(seed))
        strata = np.linspace(0.0, 1.0, int(draw), endpoint=False)
        raw = np.column_stack(
            [rng.permutation(strata + rng.random(int(draw)) / max(int(draw), 1)) for _ in range(6)]
        )
    points: list[R008Point] = []
    seen: set[tuple[object, ...]] = set()
    for row in raw:
        log2_pd = box.log2_pd_min + float(row[0]) * (box.log2_pd_max - box.log2_pd_min)
        log2_d = box.log2_d_min + float(row[1]) * (box.log2_d_max - box.log2_d_min)
        log2_tau = box.log2_tau_min + float(row[2]) * (box.log2_tau_max - box.log2_tau_min)
        log2_kf = box.log2_kf_min + float(row[3]) * (box.log2_kf_max - box.log2_kf_min)
        log2_ko = box.log2_ko_min + float(row[4]) * (box.log2_ko_max - box.log2_ko_min)
        log2_kp = box.log2_kp_min + float(row[5]) * (box.log2_kp_max - box.log2_kp_min)
        kf_off = bool(box.kf_off_allowed and float(row[3]) < float(include_kf_off_fraction))
        point = R008Point(
            log2_pd=log2_pd,
            log2_d=log2_d,
            log2_tau=log2_tau,
            kf_off=kf_off,
            log2_kf=None if kf_off else log2_kf,
            log2_ko=log2_ko,
            log2_kp=log2_kp,
        )
        try:
            key = point.key()
            # go/no-go: must land on the frozen r006 quarter-octave lattice
            typed = point.to_parameter_point()
        except (R008LatticeError, ValueError, OverflowError):
            continue
        if apply_live_acquisition_veto and live_acquisition_unstable(
            force_damping=typed.d_gain,
            normal_filter_tau_s=typed.tau_s,
        ):
            continue
        if key in seen:
            continue
        seen.add(key)
        points.append(point)
        if len(points) >= int(count):
            break
    if len(points) < int(count):
        raise R008LatticeError(
            f"live acquisition veto left only {len(points)}/{count} Sobol points"
        )
    return tuple(points)


def assert_r006_accepts(points: Sequence[R008Point]) -> None:
    for point in points:
        typed = point.to_parameter_point()
        # Round-trip through physical coordinates must stay on-lattice.
        for value, anchor, role in (
            (typed.p_gain, P_ANCHOR, "P"),
            (typed.d_gain, D_ANCHOR, "D"),
            (typed.tau_s, TAU_ANCHOR, "tau"),
            (typed.ko, KO_ANCHOR, "Ko"),
            (typed.kp, KP_ANCHOR, "Kp"),
        ):
            _quarter_steps_from_value(value, anchor)
        if typed.i_mode is IMode.ON:
            _quarter_steps_from_value(typed.i_gain, I_ON_ANCHOR)


__all__ = [
    "DEFAULT_D",
    "DEFAULT_KF",
    "DEFAULT_KO",
    "DEFAULT_KP",
    "DEFAULT_PD_RATIO",
    "DEFAULT_TAU",
    "BoxRegion",
    "R008LatticeError",
    "R008Point",
    "assert_r006_accepts",
    "default_anchor",
    "default_box",
    "live_acquisition_unstable",
    "point_from_physical",
    "scrambled_sobol",
    "snap_to_quarter",
]
