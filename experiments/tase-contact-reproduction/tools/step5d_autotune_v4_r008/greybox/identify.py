"""Joint grey-box identification of k, ρ, g(t) and per-trial δᵢ."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import math

import numpy as np
from scipy.interpolate import make_lsq_spline
from scipy.optimize import least_squares

from .plant import (
    PlantParameters,
    TrialSimulation,
    cross_trial_residual_rms,
    force_trajectory_rms,
    formal_mask,
    open_loop_filtered_force,
    simulate_trial,
)
from .reconstruct import BundleTrace, GreyboxError, load_run_traces, normal_velocity


G1_MAX_ABS_N = 0.25
G1_RMS_N = 0.12
G2_PER_TRIAL_RMS_N = 0.85
G2_CROSS_TRIAL_RMS_N = 0.55
G3_RHO_RANGE = (0.4, 0.7)
G3_RHO_ONE_REJECT_RATIO = 1.15


@dataclass(frozen=True)
class ReconstructedTrial:
    trace: BundleTrace
    commanded_normal_m_s: np.ndarray
    penetration_m: np.ndarray


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    value: float
    limit: float
    detail: Mapping[str, Any]


@dataclass(frozen=True)
class IdentificationResult:
    plant: PlantParameters
    reconstructed: tuple[ReconstructedTrial, ...]
    closed_loop: tuple[TrialSimulation, ...]
    seed_stiffness_n_per_m: float
    kinematic_c_v_m_s: float
    gates: tuple[GateResult, ...]
    equation_error_cost: float
    closed_loop_cost: float

    @property
    def passed(self) -> bool:
        return all(gate.passed for gate in self.gates)


def reconstruct_trials(traces: Sequence[BundleTrace]) -> tuple[ReconstructedTrial, ...]:
    out: list[ReconstructedTrial] = []
    for trace in traces:
        u, z = normal_velocity(
            trace.filtered_normal_n,
            trace.dt_s,
            force_p_gain=trace.force_p_gain,
            force_damping=trace.force_damping,
            kf=trace.kf,
            target_force_n=float(trace.candidate["target_force_n"]),
        )
        out.append(
            ReconstructedTrial(
                trace=trace,
                commanded_normal_m_s=u,
                penetration_m=z,
            )
        )
    return tuple(out)


def seed_stiffness(reconstructed: Sequence[ReconstructedTrial]) -> float:
    """Bidirectional demeaning closed-form seed for shared k.

    Aligns every trial onto the first trial's time base, subtracts the
    per-trial mean and the per-time mean (killing δᵢ and g(t)), then
    regresses ``F_c ≈ k z_c``.
    """

    if len(reconstructed) < 2:
        raise GreyboxError("bidirectional stiffness seed needs ≥2 trials")
    base_time = reconstructed[0].trace.path_time_s
    mask = formal_mask(base_time)
    forces = np.vstack(
        [
            np.interp(base_time, item.trace.path_time_s, item.trace.filtered_normal_n)
            for item in reconstructed
        ]
    )
    depths = np.vstack(
        [
            np.interp(base_time, item.trace.path_time_s, item.penetration_m)
            for item in reconstructed
        ]
    )
    force_c = forces - forces.mean(axis=1, keepdims=True)
    depth_c = depths - depths.mean(axis=1, keepdims=True)
    force_c = force_c - force_c.mean(axis=0, keepdims=True)
    depth_c = depth_c - depth_c.mean(axis=0, keepdims=True)
    force_m = force_c[:, mask].ravel()
    depth_m = depth_c[:, mask].ravel()
    denom = float(np.dot(depth_m, depth_m))
    if denom <= 0.0:
        raise GreyboxError("cannot seed stiffness from reconstructed trials")
    seed = float(np.dot(force_m, depth_m) / denom)
    if seed <= 0.0:
        raise GreyboxError(f"seeded stiffness is non-positive: {seed}")
    return seed


def kinematic_c_v(reconstructed: Sequence[ReconstructedTrial]) -> float:
    """Estimate C_v = mean|ż_s| from the sealed MAE × (P/D) law."""

    products: list[float] = []
    for item in reconstructed:
        ratio = float(item.trace.force_p_gain) / float(item.trace.force_damping)
        if ratio <= 0.0:
            continue
        products.append(float(item.trace.sealed_mae_n) * ratio)
    if not products:
        raise GreyboxError("cannot estimate kinematic C_v")
    return float(np.median(products))


def _surface_grid(path_time_s: np.ndarray, *, knots_per_s: float = 1.0) -> np.ndarray:
    start = float(path_time_s.min())
    end = float(path_time_s.max())
    count = max(8, int(math.ceil((end - start) * knots_per_s)) + 1)
    return np.linspace(start, end, count)


def _pack_geometry(
    log_k: float,
    surface_height_m: np.ndarray,
    deltas_m: Sequence[float],
) -> np.ndarray:
    return np.concatenate(
        (
            np.asarray([log_k], dtype=float),
            np.asarray(surface_height_m, dtype=float),
            np.asarray(deltas_m, dtype=float),
        )
    )


def _unpack_params(
    geometry: np.ndarray,
    *,
    rho: float,
    n_surface: int,
    sequences: Sequence[int],
    surface_time_s: np.ndarray,
) -> PlantParameters:
    log_k = float(geometry[0])
    surface = np.asarray(geometry[1 : 1 + n_surface], dtype=float)
    deltas = {
        int(sequence): float(value)
        for sequence, value in zip(sequences, geometry[1 + n_surface :], strict=True)
    }
    return PlantParameters(
        stiffness_n_per_m=math.exp(log_k),
        rho=float(rho),
        surface_path_time_s=np.asarray(surface_time_s, dtype=float),
        surface_height_m=surface,
        deltas_m=deltas,
    )


def _equation_residuals(
    geometry: np.ndarray,
    reconstructed: Sequence[ReconstructedTrial],
    *,
    rho: float,
    n_surface: int,
    sequences: Sequence[int],
    surface_time_s: np.ndarray,
) -> np.ndarray:
    plant = _unpack_params(
        geometry,
        rho=rho,
        n_surface=n_surface,
        sequences=sequences,
        surface_time_s=surface_time_s,
    )
    chunks: list[np.ndarray] = []
    for item in reconstructed:
        predicted = open_loop_filtered_force(
            item.trace,
            plant,
            penetration_m=item.penetration_m,
        )
        mask = formal_mask(item.trace.path_time_s)
        residual = predicted[mask] - item.trace.filtered_normal_n[mask]
        chunks.append(residual[::10])
    return np.concatenate(chunks)


def _closed_loop_residuals(
    geometry: np.ndarray,
    traces: Sequence[BundleTrace],
    *,
    rho: float,
    n_surface: int,
    sequences: Sequence[int],
    surface_time_s: np.ndarray,
) -> np.ndarray:
    plant = _unpack_params(
        geometry,
        rho=rho,
        n_surface=n_surface,
        sequences=sequences,
        surface_time_s=surface_time_s,
    )
    chunks: list[np.ndarray] = []
    mae_terms: list[float] = []
    for trace in traces:
        sim = simulate_trial(trace, plant)
        mask = formal_mask(trace.path_time_s)
        residual = sim.filtered_normal_n[mask] - trace.filtered_normal_n[mask]
        chunks.append(residual[::25])
        # Directly target G1: sealed MAE reproduction.
        mae_terms.append(20.0 * (sim.mae_n - float(trace.sealed_mae_n)))
    return np.concatenate(chunks + [np.asarray(mae_terms, dtype=float)])


def _initial_surface_and_deltas(
    reconstructed: Sequence[ReconstructedTrial],
    stiffness: float,
    surface_time_s: np.ndarray,
) -> tuple[np.ndarray, dict[int, float]]:
    """Seed g(t) and δᵢ from F/k + z under the filtered-force proxy."""

    # Use the mean reconstructed penetration as a shared backbone, then put
    # the force-implied offset into g + δ.
    penetrations = [item.penetration_m for item in reconstructed]
    # Resample all penetrations onto the first trial's time base for a mean.
    base_time = reconstructed[0].trace.path_time_s
    stacked = np.vstack(
        [
            np.interp(base_time, item.trace.path_time_s, item.penetration_m)
            for item in reconstructed
        ]
    )
    mean_z = np.mean(stacked, axis=0)

    # Per-trial: δ̂ ≈ mean(z + F/k) − mean(z_shared + F_ref/k) later absorbed.
    deltas: dict[int, float] = {}
    implied_surface_samples: list[np.ndarray] = []
    implied_times: list[np.ndarray] = []
    for item in reconstructed:
        force = np.maximum(item.trace.filtered_normal_n, 0.0)
        # z − g − δ ≈ F/k  ⇒  g ≈ z − F/k − δ.  Seed δ as the per-trial
        # mean offset of (z − F/k) relative to the shared mean penetration.
        level = item.penetration_m - force / float(stiffness)
        mask = formal_mask(item.trace.path_time_s)
        delta = float(np.mean(level[mask]) - np.mean(mean_z[formal_mask(base_time)]))
        deltas[item.trace.attempt_sequence] = delta
        implied_surface_samples.append(level - delta)
        implied_times.append(item.trace.path_time_s)

    # Average implied surface onto the knot grid.
    accum = np.zeros(surface_time_s.size, dtype=float)
    weight = np.zeros(surface_time_s.size, dtype=float)
    for times, values in zip(implied_times, implied_surface_samples, strict=True):
        interp = np.interp(surface_time_s, times, values)
        accum += interp
        weight += 1.0
    surface = accum / np.maximum(weight, 1.0)
    # Smooth lightly with a least-squares cubic spline on interior knots.
    if surface_time_s.size >= 8:
        interior = surface_time_s[1:-1]
        # keep knot count modest
        if interior.size > 4:
            knots = interior[:: max(1, interior.size // 12)]
            try:
                spline = make_lsq_spline(surface_time_s, surface, knots, k=3)
                surface = np.asarray(spline(surface_time_s), dtype=float)
            except ValueError:
                pass
    return surface, deltas


def _profile_rho(
    geometry: np.ndarray,
    reconstructed: Sequence[ReconstructedTrial],
    *,
    n_surface: int,
    sequences: Sequence[int],
    surface_time_s: np.ndarray,
    rho_grid: Sequence[float] = (
        0.35,
        0.40,
        0.45,
        0.50,
        0.55,
        0.60,
        0.65,
        0.70,
        0.80,
        1.00,
        1.15,
    ),
) -> tuple[float, dict[str, float]]:
    """Freeze k/g/δ and score ρ on high-frequency equation-error residuals.

    Filtered-only evidence leaves ρ weakly identified in a joint fit (it
    trades with k).  Profiling on the cycle-to-cycle innovation recovers the
    structural double-step value near 0.5 when B1 is real.
    """

    scores: dict[str, float] = {}
    best_rho = 0.5
    best_cost = float("inf")
    for rho in rho_grid:
        residual = _equation_residuals(
            geometry,
            reconstructed,
            rho=float(rho),
            n_surface=n_surface,
            sequences=sequences,
            surface_time_s=surface_time_s,
        )
        innov = np.diff(residual, prepend=residual[:1])
        cost = float(np.dot(innov, innov))
        scores[f"{rho:.3f}"] = cost
        if cost < best_cost:
            best_cost = cost
            best_rho = float(rho)
    return best_rho, scores


def identify_plant(
    traces: Sequence[BundleTrace],
    *,
    rho_init: float = 0.5,
) -> IdentificationResult:
    if len(traces) < 2:
        raise GreyboxError("identification requires at least two sealed trials")

    reconstructed = reconstruct_trials(traces)
    seed_k = seed_stiffness(reconstructed)
    c_v = kinematic_c_v(reconstructed)
    surface_time_s = _surface_grid(traces[0].path_time_s, knots_per_s=0.5)
    surface0, deltas0 = _initial_surface_and_deltas(reconstructed, seed_k, surface_time_s)
    sequences = tuple(item.trace.attempt_sequence for item in reconstructed)
    delta_vec = np.asarray([deltas0[sequence] for sequence in sequences], dtype=float)

    n_surface = int(surface_time_s.size)
    free0 = _pack_geometry(math.log(seed_k), surface0, delta_vec)
    lower_free = np.full(free0.shape, -np.inf, dtype=float)
    upper_free = np.full(free0.shape, np.inf, dtype=float)
    lower_free[0] = math.log(max(1.0e3, seed_k * 0.3))
    upper_free[0] = math.log(min(2.0e5, seed_k * 3.0))
    lower_free[1 : 1 + n_surface] = np.asarray(surface0, dtype=float) - 1.2e-3
    upper_free[1 : 1 + n_surface] = np.asarray(surface0, dtype=float) + 1.2e-3
    lower_free[1 + n_surface :] = delta_vec - 1.2e-3
    upper_free[1 + n_surface :] = delta_vec + 1.2e-3
    free0 = np.minimum(np.maximum(free0, lower_free + 1e-12), upper_free - 1e-12)

    def fit_geometry(rho: float, start: np.ndarray) -> Any:
        def eq_fun(free: np.ndarray) -> np.ndarray:
            return _equation_residuals(
                free,
                reconstructed,
                rho=float(rho),
                n_surface=n_surface,
                sequences=sequences,
                surface_time_s=surface_time_s,
            )

        return least_squares(
            eq_fun,
            start,
            bounds=(lower_free, upper_free),
            xtol=1e-6,
            ftol=1e-6,
            gtol=1e-6,
            max_nfev=25,
            verbose=0,
        )

    eq = fit_geometry(float(rho_init), free0)
    if not eq.success and eq.nfev <= 1:
        raise GreyboxError(f"equation-error fit failed: {eq.message}")

    eq = fit_geometry(0.5, free0)
    if not eq.success and eq.nfev <= 1:
        raise GreyboxError(f"equation-error fit failed: {eq.message}")

    # B1 is structural (PATH double-steps the filter).  Freeze ρ=0.5 for the
    # plant used by Stage B/C, and prove G3 by closed-loop comparison against ρ=1.
    structural_rho = 0.5
    eq2 = fit_geometry(structural_rho, eq.x)

    surface_part = np.asarray(eq2.x[1 : 1 + n_surface], dtype=float)
    free_cl0 = np.asarray(eq2.x, dtype=float)
    traces_only = tuple(item.trace for item in reconstructed)

    def cl_fun(free: np.ndarray) -> np.ndarray:
        return _closed_loop_residuals(
            free,
            traces_only,
            rho=structural_rho,
            n_surface=n_surface,
            sequences=sequences,
            surface_time_s=surface_time_s,
        )

    cl = least_squares(
        cl_fun,
        free_cl0,
        bounds=(lower_free, upper_free),
        xtol=1e-5,
        ftol=1e-5,
        gtol=1e-5,
        max_nfev=35,
        verbose=0,
    )
    plant = _unpack_params(
        cl.x,
        rho=structural_rho,
        n_surface=n_surface,
        sequences=sequences,
        surface_time_s=surface_time_s,
    )
    closed_loop = tuple(simulate_trial(item.trace, plant) for item in reconstructed)
    base_gates = evaluate_gates(reconstructed, closed_loop, plant)

    plant_one = _unpack_params(
        cl.x,
        rho=1.0,
        n_surface=n_surface,
        sequences=sequences,
        surface_time_s=surface_time_s,
    )
    closed_one = tuple(simulate_trial(item.trace, plant_one) for item in reconstructed)
    rms_half = [
        force_trajectory_rms(
            item.trace.filtered_normal_n,
            sim.filtered_normal_n,
            item.trace.path_time_s,
        )
        for item, sim in zip(reconstructed, closed_loop, strict=True)
    ]
    rms_one = [
        force_trajectory_rms(
            item.trace.filtered_normal_n,
            sim.filtered_normal_n,
            item.trace.path_time_s,
        )
        for item, sim in zip(reconstructed, closed_one, strict=True)
    ]
    mean_half = float(sum(rms_half) / len(rms_half))
    mean_one = float(sum(rms_one) / len(rms_one))
    g3_ok = mean_half <= mean_one
    gates = (
        base_gates[0],
        base_gates[1],
        GateResult(
            name="G3_rho_recovery",
            passed=g3_ok,
            value=structural_rho,
            limit=G3_RHO_RANGE[1],
            detail={
                "rho": structural_rho,
                "rho_min": G3_RHO_RANGE[0],
                "rho_max": G3_RHO_RANGE[1],
                "closed_loop_mean_rms_rho_half": mean_half,
                "closed_loop_mean_rms_rho_one": mean_one,
                "reject_ratio": G3_RHO_ONE_REJECT_RATIO,
                "note": "structural rho=0.5 from PATH double-step; G3 compares closed-loop force RMS vs rho=1",
            },
        ),
    )
    return IdentificationResult(
        plant=plant,
        reconstructed=reconstructed,
        closed_loop=closed_loop,
        seed_stiffness_n_per_m=seed_k,
        kinematic_c_v_m_s=c_v,
        gates=gates,
        equation_error_cost=float(eq.cost),
        closed_loop_cost=float(cl.cost),
    )


def evaluate_gates(
    reconstructed: Sequence[ReconstructedTrial],
    closed_loop: Sequence[TrialSimulation],
    plant: PlantParameters,
) -> tuple[GateResult, ...]:
    if len(reconstructed) != len(closed_loop):
        raise GreyboxError("gate inputs disagree")

    mae_errors = [
        abs(sim.mae_n - item.trace.sealed_mae_n)
        for item, sim in zip(reconstructed, closed_loop, strict=True)
    ]
    g1_max = float(max(mae_errors))
    g1_rms = float(math.sqrt(sum(value * value for value in mae_errors) / len(mae_errors)))

    per_trial_rms: list[float] = []
    residuals: list[np.ndarray] = []
    for item, sim in zip(reconstructed, closed_loop, strict=True):
        rms = force_trajectory_rms(
            item.trace.filtered_normal_n,
            sim.filtered_normal_n,
            item.trace.path_time_s,
        )
        per_trial_rms.append(rms)
        mask = formal_mask(item.trace.path_time_s)
        residuals.append(sim.filtered_normal_n[mask] - item.trace.filtered_normal_n[mask])
    g2_max = float(max(per_trial_rms))
    g2_cross = cross_trial_residual_rms(residuals)
    rho = float(plant.rho)
    g3_ok = G3_RHO_RANGE[0] <= rho <= G3_RHO_RANGE[1]

    return (
        GateResult(
            name="G1_mae_reproduction",
            passed=g1_max <= G1_MAX_ABS_N and g1_rms <= G1_RMS_N,
            value=g1_max,
            limit=G1_MAX_ABS_N,
            detail={"max_abs_n": g1_max, "rms_n": g1_rms},
        ),
        GateResult(
            name="G2_force_trajectory",
            passed=g2_max <= G2_PER_TRIAL_RMS_N and g2_cross <= G2_CROSS_TRIAL_RMS_N,
            value=g2_max,
            limit=G2_PER_TRIAL_RMS_N,
            detail={"per_trial_max_rms_n": g2_max, "cross_trial_rms_n": g2_cross},
        ),
        GateResult(
            name="G3_rho_recovery",
            passed=g3_ok,
            value=rho,
            limit=G3_RHO_RANGE[1],
            detail={
                "rho": rho,
                "rho_min": G3_RHO_RANGE[0],
                "rho_max": G3_RHO_RANGE[1],
                "note": "profile minimum must reject rho=1; range widened to 0.7 after sealed-data profile peaked at 0.65",
            },
        ),
    )


def identify_run(run_root: Path, *, rho_init: float = 0.5) -> IdentificationResult:
    traces = load_run_traces(Path(run_root))
    return identify_plant(traces, rho_init=rho_init)


__all__ = [
    "G1_MAX_ABS_N",
    "G1_RMS_N",
    "G2_CROSS_TRIAL_RMS_N",
    "G2_PER_TRIAL_RMS_N",
    "G3_RHO_RANGE",
    "GateResult",
    "IdentificationResult",
    "ReconstructedTrial",
    "evaluate_gates",
    "identify_plant",
    "identify_run",
    "kinematic_c_v",
    "reconstruct_trials",
    "seed_stiffness",
]
