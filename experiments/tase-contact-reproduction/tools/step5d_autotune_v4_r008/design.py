"""Offline loop-shaping scan that produces the r008 domain contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import math

import numpy as np

from step5d_autotune_v4_r008.greybox.plant import PlantParameters, simulate_trial
from step5d_autotune_v4_r008.greybox.receipt import SCHEMA as GREYBOX_SCHEMA, load_receipt
from step5d_autotune_v4_r008.greybox.reconstruct import BundleTrace, GreyboxError
from step5d_autotune_v4_r008.lattice import (
    DEFAULT_D,
    DEFAULT_KF,
    DEFAULT_KO,
    DEFAULT_KP,
    DEFAULT_PD_RATIO,
    DEFAULT_TAU,
    BoxRegion,
    R008Point,
    default_anchor,
    default_box,
    snap_to_quarter,
)
from step5d_autotune_v4_r006.contracts import D_ANCHOR, I_ON_ANCHOR, KO_ANCHOR, KP_ANCHOR, P_ANCHOR, TAU_ANCHOR


class DesignError(ValueError):
    """Domain design inputs or scan failed."""


DT_S = 0.002
TD_S = 0.012  # ~10–15 ms transport + tick budget from the plan
PM_MIN_DEG = 25.0
PEAK_U_MAX_M_S = 0.002
PEAK_FORCE_MAX_N = 12.0
APPLICATION_MAE_THRESHOLD_N = 1.0


def plant_from_receipt(receipt_path: Path) -> tuple[PlantParameters, Mapping[str, Any]]:
    receipt = load_receipt(Path(receipt_path))
    identified = receipt.document["identified"]
    plant = PlantParameters(
        stiffness_n_per_m=float(identified["stiffness_n_per_m"]),
        rho=float(identified["rho"]),
        surface_path_time_s=np.asarray(identified["surface_path_time_s"], dtype=float),
        surface_height_m=np.asarray(identified["surface_height_m"], dtype=float),
        deltas_m={int(key): float(value) for key, value in identified["deltas_m"].items()},
    )
    return plant, receipt.document


def phase_margin_deg(
    *,
    stiffness: float,
    pd_ratio: float,
    damping: float,
    tau_eff_s: float,
    kf: float,
    td_s: float = TD_S,
) -> float:
    """Approximate PM of L(s)=k P (s+kf)/(s^2 (s+D) (τs+1)) e^{-s Td} at gain crossover."""

    p_gain = float(pd_ratio) * float(damping)
    # Gain crossover estimate from ω_c ≈ k * P/D for the dominant double integrator.
    omega_c = max(1e-3, float(stiffness) * float(pd_ratio))
    # Phase of each factor at ω_c.
    phase = -180.0  # 1/s^2
    phase += math.degrees(math.atan2(omega_c, max(kf, 1e-9)))  # (s+kf)
    phase -= math.degrees(math.atan2(omega_c, max(damping, 1e-9)))  # 1/(s+D)
    phase -= math.degrees(math.atan2(omega_c * tau_eff_s, 1.0))  # 1/(τs+1)
    phase -= math.degrees(omega_c * float(td_s))  # e^{-sTd}
    # Ignore the constant gain for PM (phase-only); report PM = 180 + angle(L).
    return float(phase + 180.0)


def _synthetic_trace_for_point(point: R008Point, template: BundleTrace) -> BundleTrace:
    candidate = dict(template.candidate)
    candidate.update(
        {
            "force_p_gain": point.force_p_gain,
            "force_damping": point.damping,
            "normal_filter_tau_s": point.tau_s,
            "force_i_gain": point.force_i_gain,
            "orientation_ko": point.orientation_ko,
            "motion_kp": point.motion_kp,
        }
    )
    return BundleTrace(
        attempt_sequence=template.attempt_sequence,
        execution_id=f"design-{template.attempt_sequence}",
        kind="DESIGN",
        candidate=candidate,
        bundle_sha256=template.bundle_sha256,
        sealed_mae_n=template.sealed_mae_n,
        path_time_s=template.path_time_s,
        dt_s=template.dt_s,
        filtered_normal_n=template.filtered_normal_n,
    )


@dataclass(frozen=True)
class ScanPoint:
    point: R008Point
    predicted_mae_n: float
    phase_margin_deg: float
    peak_u_m_s: float
    peak_force_n: float
    contact_loss_fraction: float
    discrete_ok: bool
    vetoed: bool
    veto_reasons: tuple[str, ...]


def scan_point(
    point: R008Point,
    plant: PlantParameters,
    template: BundleTrace,
) -> ScanPoint:
    tau_eff = float(point.tau_s) * float(plant.rho)
    pm = phase_margin_deg(
        stiffness=plant.stiffness_n_per_m,
        pd_ratio=point.pd_ratio,
        damping=point.damping,
        tau_eff_s=tau_eff,
        kf=point.kf,
    )
    discrete_ok = float(point.damping) * DT_S < 0.5
    shell = _synthetic_trace_for_point(point, template)
    # Use the plant's first available delta for design probes.
    if template.attempt_sequence not in plant.deltas_m:
        # Temporarily bind the template sequence to an existing delta.
        first_key = next(iter(plant.deltas_m))
        plant = PlantParameters(
            stiffness_n_per_m=plant.stiffness_n_per_m,
            rho=plant.rho,
            surface_path_time_s=plant.surface_path_time_s,
            surface_height_m=plant.surface_height_m,
            deltas_m={int(template.attempt_sequence): float(plant.deltas_m[first_key])},
        )
    sim = simulate_trial(shell, plant)
    peak_u = float(np.max(np.abs(sim.commanded_normal_m_s)))
    peak_f = float(np.max(sim.filtered_normal_n))
    formal = (shell.path_time_s >= 5.0) & (shell.path_time_s < 60.0)
    contact_loss = float(np.mean(sim.filtered_normal_n[formal] < 1.0)) if np.any(formal) else 1.0
    reasons: list[str] = []
    if pm < PM_MIN_DEG:
        reasons.append(f"pm<{PM_MIN_DEG}")
    if peak_u > PEAK_U_MAX_M_S:
        reasons.append("peak_u")
    if peak_f > PEAK_FORCE_MAX_N:
        reasons.append("peak_force")
    if not discrete_ok:
        reasons.append("discrete_D")
    return ScanPoint(
        point=point,
        predicted_mae_n=float(sim.mae_n),
        phase_margin_deg=pm,
        peak_u_m_s=peak_u,
        peak_force_n=peak_f,
        contact_loss_fraction=contact_loss,
        discrete_ok=discrete_ok,
        vetoed=bool(reasons),
        veto_reasons=tuple(reasons),
    )


def build_domain_document(
    *,
    greybox_document: Mapping[str, Any],
    greybox_sha256: str,
    plant: PlantParameters,
    anchor: R008Point,
    box: BoxRegion,
    scan: Sequence[ScanPoint],
) -> dict[str, Any]:
    veto = [
        {
            "force_p_gain": point.point.force_p_gain,
            "force_damping": point.point.damping,
            "normal_filter_tau_s": point.point.tau_s,
            "force_i_gain": point.point.force_i_gain,
            "orientation_ko": point.point.orientation_ko,
            "motion_kp": point.point.motion_kp,
            "reasons": list(point.veto_reasons),
            "predicted_mae_n": point.predicted_mae_n,
            "phase_margin_deg": point.phase_margin_deg,
        }
        for point in scan
        if point.vetoed
    ]
    survivors = [point for point in scan if not point.vetoed]
    return {
        "schema": "step5d.autotune-v4/r008-domain-design-v1",
        "greybox_schema": GREYBOX_SCHEMA,
        "greybox_artifact_sha256": greybox_sha256,
        "greybox_stiffness_n_per_m": float(plant.stiffness_n_per_m),
        "greybox_rho": float(plant.rho),
        "greybox_kinematic_c_v_m_s": float(
            greybox_document["identified"]["kinematic_c_v_m_s"]
        ),
        "application_mae_threshold_n": APPLICATION_MAE_THRESHOLD_N,
        "delay_budget_s": TD_S,
        "anchor": {
            "pd_ratio": anchor.pd_ratio,
            "force_p_gain": snap_to_quarter(anchor.force_p_gain, P_ANCHOR),
            "force_damping": snap_to_quarter(anchor.damping, D_ANCHOR),
            "normal_filter_tau_s": snap_to_quarter(anchor.tau_s, TAU_ANCHOR),
            "force_i_gain": 0.0
            if anchor.kf_off
            else snap_to_quarter(anchor.force_i_gain, I_ON_ANCHOR),
            "orientation_ko": snap_to_quarter(anchor.orientation_ko, KO_ANCHOR),
            "motion_kp": snap_to_quarter(anchor.motion_kp, KP_ANCHOR),
            "kf": anchor.kf,
        },
        "box": {
            "log2_pd": [box.log2_pd_min, box.log2_pd_max],
            "log2_d": [box.log2_d_min, box.log2_d_max],
            "log2_tau": [box.log2_tau_min, box.log2_tau_max],
            "kf_off_allowed": box.kf_off_allowed,
            "log2_kf": [box.log2_kf_min, box.log2_kf_max],
            "log2_ko": [box.log2_ko_min, box.log2_ko_max],
            "log2_kp": [box.log2_kp_min, box.log2_kp_max],
        },
        "r006_anchors_unchanged": {
            "P_ANCHOR": P_ANCHOR,
            "D_ANCHOR": D_ANCHOR,
            "TAU_ANCHOR": TAU_ANCHOR,
            "I_ON_ANCHOR": I_ON_ANCHOR,
            "KO_ANCHOR": KO_ANCHOR,
            "KP_ANCHOR": KP_ANCHOR,
        },
        "scan_summary": {
            "evaluated": len(scan),
            "vetoed": len(veto),
            "survivors": len(survivors),
            "survivor_mae_min": None
            if not survivors
            else min(point.predicted_mae_n for point in survivors),
            "survivor_mae_at_anchor": next(
                (
                    point.predicted_mae_n
                    for point in scan
                    if math.isclose(point.point.pd_ratio, anchor.pd_ratio, rel_tol=1e-6)
                    and math.isclose(point.point.damping, anchor.damping, rel_tol=1e-6)
                ),
                None,
            ),
        },
        "offline_hard_veto": veto,
        "defaults": {
            "pd_ratio": DEFAULT_PD_RATIO,
            "D": DEFAULT_D,
            "tau": DEFAULT_TAU,
            "kf": DEFAULT_KF,
            "Ko": DEFAULT_KO,
            "Kp": DEFAULT_KP,
        },
    }
