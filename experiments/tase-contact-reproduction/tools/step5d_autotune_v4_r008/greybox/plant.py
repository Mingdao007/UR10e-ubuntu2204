"""Contact plant model and closed-loop simulation for grey-box identification.

The unilateral contact law and the first-order force filter are the only
plant-side unknowns once commanded normal motion has been reconstructed.  The
MAE objective here is the same within-bin absolute error that r006 seals on
the formal window ``[5, 60)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence
import math

import numpy as np

from step5d_autotune_v4_r004.path_controller import filter_alpha as production_filter_alpha
from step5d_autotune_v4_r006.contracts import (
    BIN_WIDTH_S,
    FORMAL_WINDOW_S,
    REQUIRED_BINS,
    TARGET_FORCE_N,
)

from .reconstruct import FORCE_INTEGRAL_LIMIT_N_S, BundleTrace, GreyboxError, normal_velocity


FORMAL_BINS = REQUIRED_BINS
TARGET_FORCE = float(TARGET_FORCE_N)


@dataclass(frozen=True)
class PlantParameters:
    """Shared plant parameters plus per-trial contact offsets."""

    stiffness_n_per_m: float
    rho: float
    surface_path_time_s: np.ndarray
    surface_height_m: np.ndarray
    deltas_m: Mapping[int, float]

    def __post_init__(self) -> None:
        if not math.isfinite(self.stiffness_n_per_m) or self.stiffness_n_per_m <= 0.0:
            raise GreyboxError("stiffness must be positive and finite")
        if not math.isfinite(self.rho) or self.rho <= 0.0:
            raise GreyboxError("rho must be positive and finite")
        if self.surface_path_time_s.ndim != 1 or self.surface_height_m.ndim != 1:
            raise GreyboxError("surface profile must be 1-D")
        if self.surface_path_time_s.size != self.surface_height_m.size:
            raise GreyboxError("surface profile channels disagree")
        if self.surface_path_time_s.size < 2:
            raise GreyboxError("surface profile is too short")
        if not np.all(np.diff(self.surface_path_time_s) > 0.0):
            raise GreyboxError("surface path_time_s must be strictly increasing")
        if not np.all(np.isfinite(self.surface_height_m)):
            raise GreyboxError("surface height is not finite")
        for key, value in self.deltas_m.items():
            if int(key) != key:
                raise GreyboxError("delta keys must be attempt sequences")
            if not math.isfinite(float(value)):
                raise GreyboxError("delta is not finite")

    def surface_at(self, path_time_s: np.ndarray | float) -> np.ndarray:
        """Linear interpolation of the shared surface profile."""

        return np.interp(
            np.asarray(path_time_s, dtype=float),
            self.surface_path_time_s,
            self.surface_height_m,
        )

    def delta_for(self, attempt_sequence: int) -> float:
        try:
            return float(self.deltas_m[int(attempt_sequence)])
        except KeyError as exc:
            raise GreyboxError(
                f"plant has no delta for attempt_sequence={attempt_sequence}"
            ) from exc


def filter_alpha(actual_dt_s: float, tau_s: float, *, rho: float = 1.0) -> float:
    """Production filter alpha evaluated at an effective time constant ``ρ·τ``."""

    effective = float(tau_s) * float(rho)
    return float(production_filter_alpha(actual_dt_s, effective))


def objective_mae_n(
    path_time_s: np.ndarray,
    filtered_normal_n: np.ndarray,
    *,
    target_force_n: float = TARGET_FORCE,
    window: tuple[float, float] = FORMAL_WINDOW_S,
) -> float:
    """Within-bin absolute MAE on the formal PATH window (r006 definition)."""

    start, end = window
    sums = np.zeros(FORMAL_BINS, dtype=float)
    counts = np.zeros(FORMAL_BINS, dtype=int)
    times = np.asarray(path_time_s, dtype=float)
    forces = np.asarray(filtered_normal_n, dtype=float)
    if times.size != forces.size:
        raise GreyboxError("MAE channels disagree")
    for time_s, force_n in zip(times, forces, strict=True):
        if not (start <= float(time_s) < end):
            continue
        index = int(math.floor((float(time_s) - start) / BIN_WIDTH_S + 1e-10))
        if 0 <= index < FORMAL_BINS:
            sums[index] += abs(float(force_n) - float(target_force_n))
            counts[index] += 1
    if not np.all(counts > 0):
        missing = int(np.count_nonzero(counts == 0))
        raise GreyboxError(f"formal window has {missing} empty bins")
    return float(np.mean(sums / counts))


@dataclass(frozen=True)
class TrialSimulation:
    """One closed-loop trial under a candidate plant."""

    path_time_s: np.ndarray
    commanded_normal_m_s: np.ndarray
    penetration_m: np.ndarray
    contact_force_n: np.ndarray
    filtered_normal_n: np.ndarray
    mae_n: float


def simulate_trial(
    trace: BundleTrace,
    plant: PlantParameters,
    *,
    target_force_n: float = TARGET_FORCE,
    integral_limit_n_s: float = FORCE_INTEGRAL_LIMIT_N_S,
    initial_filtered_n: float | None = None,
) -> TrialSimulation:
    """Closed-loop simulation of the normal channel against the contact plant.

    The controller half is the same scalar recursion used by
    :func:`reconstruct.normal_velocity`.  The plant half is unilateral
    stiffness against a shared surface profile plus a first-order filter at
    ``ρ·τ``.
    """

    n = int(trace.path_time_s.size)
    u = np.empty(n, dtype=float)
    z = np.empty(n, dtype=float)
    contact = np.empty(n, dtype=float)
    filtered = np.empty(n, dtype=float)

    delta = plant.delta_for(trace.attempt_sequence)
    surface = plant.surface_at(trace.path_time_s)
    k = float(plant.stiffness_n_per_m)
    rho = float(plant.rho)
    tau = float(trace.normal_filter_tau_s)
    p_gain = float(trace.force_p_gain)
    damping = float(trace.force_damping)
    kf = float(trace.kf)
    limit = abs(float(integral_limit_n_s))

    velocity = 0.0
    integral = 0.0
    penetration = 0.0
    if initial_filtered_n is None:
        filt = float(trace.filtered_normal_n[0])
    else:
        filt = float(initial_filtered_n)

    for index in range(n):
        dt = float(trace.dt_s[index])
        # Penetration z grows when the controller presses (positive u).  Contact
        # force therefore rises with z against a path-locked surface g(t):
        # F = max(0, k (z - g - δ)).  The opposite sign is positive feedback
        # through the outer loop and is ruled out by the sealed trials.
        force = max(0.0, k * (penetration - float(surface[index]) - delta))
        alpha = filter_alpha(dt, tau, rho=rho)
        filt = (1.0 - alpha) * filt + alpha * force
        error = float(target_force_n) - filt
        integral = min(max(integral + error * dt, -limit), limit)
        velocity = velocity * (1.0 - dt * damping) + dt * p_gain * (error + kf * integral)
        penetration = penetration + velocity * dt
        u[index] = velocity
        z[index] = penetration
        contact[index] = force
        filtered[index] = filt

    return TrialSimulation(
        path_time_s=np.asarray(trace.path_time_s, dtype=float),
        commanded_normal_m_s=u,
        penetration_m=z,
        contact_force_n=contact,
        filtered_normal_n=filtered,
        mae_n=objective_mae_n(trace.path_time_s, filtered, target_force_n=target_force_n),
    )


def open_loop_filtered_force(
    trace: BundleTrace,
    plant: PlantParameters,
    *,
    penetration_m: np.ndarray | None = None,
    initial_filtered_n: float | None = None,
) -> np.ndarray:
    """Filter the contact force implied by a (possibly reconstructed) trajectory."""

    if penetration_m is None:
        _, penetration_m = normal_velocity(
            trace.filtered_normal_n,
            trace.dt_s,
            force_p_gain=trace.force_p_gain,
            force_damping=trace.force_damping,
            kf=trace.kf,
            target_force_n=float(trace.candidate["target_force_n"]),
        )
    penetration_m = np.asarray(penetration_m, dtype=float)
    if penetration_m.size != trace.path_time_s.size:
        raise GreyboxError("penetration channel disagrees with trace")

    delta = plant.delta_for(trace.attempt_sequence)
    surface = plant.surface_at(trace.path_time_s)
    force = np.maximum(
        0.0,
        float(plant.stiffness_n_per_m) * (penetration_m - surface - delta),
    )
    return apply_force_filter(
        force,
        trace.dt_s,
        tau_s=float(trace.normal_filter_tau_s),
        rho=float(plant.rho),
        initial_filtered_n=(
            float(trace.filtered_normal_n[0])
            if initial_filtered_n is None
            else float(initial_filtered_n)
        ),
    )


def apply_force_filter(
    contact_force_n: np.ndarray,
    dt_s: np.ndarray,
    *,
    tau_s: float,
    rho: float,
    initial_filtered_n: float,
) -> np.ndarray:
    """Causal first-order filter at ``ρ·τ``; dt may vary per sample."""

    force = np.asarray(contact_force_n, dtype=float)
    dts = np.asarray(dt_s, dtype=float)
    out = np.empty_like(force)
    filt = float(initial_filtered_n)
    tau = float(tau_s)
    rho_v = float(rho)
    for index in range(force.size):
        alpha = filter_alpha(float(dts[index]), tau, rho=rho_v)
        filt = (1.0 - alpha) * filt + alpha * float(force[index])
        out[index] = filt
    return out


def formal_mask(path_time_s: np.ndarray) -> np.ndarray:
    start, end = FORMAL_WINDOW_S
    times = np.asarray(path_time_s, dtype=float)
    return (times >= start) & (times < end)


def force_trajectory_rms(
    measured_n: np.ndarray,
    simulated_n: np.ndarray,
    path_time_s: np.ndarray,
) -> float:
    mask = formal_mask(path_time_s)
    if not np.any(mask):
        raise GreyboxError("no formal-window samples for RMS")
    residual = np.asarray(simulated_n, dtype=float)[mask] - np.asarray(measured_n, dtype=float)[mask]
    return float(np.sqrt(np.mean(np.square(residual))))


def cross_trial_residual_rms(
    residuals: Sequence[np.ndarray],
) -> float:
    if not residuals:
        raise GreyboxError("no residuals")
    stacked = np.concatenate([np.asarray(value, dtype=float).reshape(-1) for value in residuals])
    return float(np.sqrt(np.mean(np.square(stacked))))


__all__ = [
    "FORMAL_BINS",
    "FORMAL_WINDOW_S",
    "TARGET_FORCE",
    "PlantParameters",
    "TrialSimulation",
    "cross_trial_residual_rms",
    "filter_alpha",
    "force_trajectory_rms",
    "formal_mask",
    "objective_mae_n",
    "open_loop_filtered_force",
    "simulate_trial",
]
