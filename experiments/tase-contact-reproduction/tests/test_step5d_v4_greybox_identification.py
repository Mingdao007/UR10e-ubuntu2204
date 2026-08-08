"""Offline regression for the r008 grey-box identification package."""

from __future__ import annotations

from pathlib import Path
import math
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r008.greybox.identify import (  # noqa: E402
    G3_RHO_RANGE,
    evaluate_gates,
    identify_plant,
)
from step5d_autotune_v4_r008.greybox.plant import (  # noqa: E402
    PlantParameters,
    filter_alpha,
    objective_mae_n,
    simulate_trial,
)
from step5d_autotune_v4_r008.greybox.receipt import (  # noqa: E402
    SCHEMA,
    build_receipt,
    load_receipt,
)
from step5d_autotune_v4_r008.greybox.reconstruct import (  # noqa: E402
    BundleTrace,
    GreyboxError,
    load_run_traces,
    normal_velocity,
    verify_scalar_kernel_against_production,
)


RUN_ROOT = ROOT / "runs" / "step5d_autotune_v4_r006" / "live_20260802_2258_wire_gate"


def _synthetic_trace(
    *,
    attempt_sequence: int,
    force_p_gain: float,
    force_damping: float,
    tau_s: float,
    duration_s: float = 60.0,
    dt_s: float = 0.002,
) -> BundleTrace:
    times = np.arange(0.0, duration_s, dt_s)
    # Placeholder force; overwritten by the plant simulation below.
    force = np.full(times.shape, 5.0)
    candidate = {
        "force_p_gain": force_p_gain,
        "force_i_gain": 0.0,
        "force_damping": force_damping,
        "normal_filter_tau_s": tau_s,
        "orientation_ko": 0.2,
        "motion_kp": 1.5,
        "target_force_n": 5.0,
    }
    return BundleTrace(
        attempt_sequence=attempt_sequence,
        execution_id=f"synthetic-{attempt_sequence}",
        kind="SYNTHETIC",
        candidate=candidate,
        bundle_sha256="0" * 64,
        sealed_mae_n=0.0,
        path_time_s=times,
        dt_s=np.full(times.shape, dt_s),
        filtered_normal_n=force,
    )


def _make_synthetic_bundle() -> tuple[tuple[BundleTrace, ...], PlantParameters]:
    surface_time = np.linspace(0.0, 60.0, 61)
    # Path-locked surface in the PATH-origin frame.  F = k max(0, z−g−δ), so
    # light contact at z=0 needs g+δ ≈ −5/k.  A slow undulation then drives
    # the kinematic disturbance the MAE law measures.
    k = 3.1e4
    deltas = {1: 1.5e-5, 2: 2.0e-5, 3: 1.0e-5}
    surface = -5.0 / k - 1.5e-5 + 1.5e-4 * np.sin(2.0 * math.pi * surface_time / 40.0)
    plant = PlantParameters(
        stiffness_n_per_m=k,
        rho=0.5,
        surface_path_time_s=surface_time,
        surface_height_m=surface,
        deltas_m=deltas,
    )
    # Use P/D ratios near the sealed r006 operating points so the synthetic
    # MAE lands in a realistic band without saturating the integral.
    specs = (
        (1, 2.974e-4, 28.0, 0.35),
        (2, 3.536e-4, 28.0, 0.35),
        (3, 4.204e-4, 28.0, 0.35),
    )
    traces: list[BundleTrace] = []
    for sequence, p_gain, damping, tau in specs:
        shell = _synthetic_trace(
            attempt_sequence=sequence,
            force_p_gain=p_gain,
            force_damping=damping,
            tau_s=tau,
        )
        first = simulate_trial(shell, plant)
        sealed = objective_mae_n(shell.path_time_s, first.filtered_normal_n)
        traces.append(
            BundleTrace(
                attempt_sequence=shell.attempt_sequence,
                execution_id=shell.execution_id,
                kind=shell.kind,
                candidate=shell.candidate,
                bundle_sha256=shell.bundle_sha256,
                sealed_mae_n=sealed,
                path_time_s=shell.path_time_s,
                dt_s=shell.dt_s,
                filtered_normal_n=first.filtered_normal_n,
            )
        )
    return tuple(traces), plant


def test_filter_alpha_matches_production_at_rho_one() -> None:
    from step5d_autotune_v4_r004.path_controller import filter_alpha as production

    assert filter_alpha(0.002, 0.35, rho=1.0) == pytest.approx(production(0.002, 0.35))
    assert filter_alpha(0.002, 0.35, rho=0.5) == pytest.approx(production(0.002, 0.175))


def test_objective_mae_arithmetic_on_constant_error() -> None:
    times = np.arange(5.0, 60.0, 0.002)
    force = np.full(times.shape, 6.0)  # abs error 1.0 N everywhere
    assert objective_mae_n(times, force) == pytest.approx(1.0, abs=1e-9)


def test_synthetic_plant_roundtrip_gates() -> None:
    traces, truth = _make_synthetic_bundle()
    # Perfect plant should pass gate arithmetic when used for closed-loop replay.
    from step5d_autotune_v4_r008.greybox.identify import ReconstructedTrial, reconstruct_trials

    reconstructed = reconstruct_trials(traces)
    closed = tuple(simulate_trial(item.trace, truth) for item in reconstructed)
    gates = evaluate_gates(reconstructed, closed, truth)
    assert all(gate.passed for gate in gates), gates
    assert G3_RHO_RANGE[0] <= truth.rho <= G3_RHO_RANGE[1]


def test_synthetic_identification_recovers_rho_band() -> None:
    traces, truth = _make_synthetic_bundle()
    result = identify_plant(traces, rho_init=0.55)
    assert result.plant.rho == pytest.approx(truth.rho, rel=0.25, abs=0.1)
    assert result.passed or result.gates[2].passed  # G3 at minimum


@pytest.mark.slow
def test_real_run_scalar_kernel_and_identification(tmp_path: Path) -> None:
    if not RUN_ROOT.is_dir():
        pytest.skip("sealed r006 run is not present")
    traces = load_run_traces(RUN_ROOT)
    assert len(traces) >= 10
    worst = verify_scalar_kernel_against_production(traces[0])
    assert worst < 1e-12

    result = identify_plant(traces, rho_init=0.5)
    assert result.gates[2].passed, result.gates  # G3 hard stop
    assert result.passed, result.gates

    receipt = build_receipt(result, run_root=RUN_ROOT, repo_root=ROOT)
    path = receipt.write(tmp_path)
    loaded = load_receipt(path)
    assert loaded.document["schema"] == SCHEMA
    assert loaded.digest == receipt.digest
    assert G3_RHO_RANGE[0] <= float(loaded.document["identified"]["rho"]) <= G3_RHO_RANGE[1]


def test_rho_near_one_fails_g3() -> None:
    traces, truth = _make_synthetic_bundle()
    bad = PlantParameters(
        stiffness_n_per_m=truth.stiffness_n_per_m,
        rho=1.0,
        surface_path_time_s=truth.surface_path_time_s,
        surface_height_m=truth.surface_height_m,
        deltas_m=truth.deltas_m,
    )
    from step5d_autotune_v4_r008.greybox.identify import reconstruct_trials

    reconstructed = reconstruct_trials(traces)
    closed = tuple(simulate_trial(item.trace, bad) for item in reconstructed)
    gates = evaluate_gates(reconstructed, closed, bad)
    assert not gates[2].passed
