#!/usr/bin/env python3
"""Run a logical 60 s packet/filter canary without URSim or robot I/O."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ur10e_vic.backends import (  # noqa: E402
    DIRECT_TORQUE_BASELINE_K,
    DIRECT_TORQUE_FRAME_TOKEN,
    DIRECT_TORQUE_VIRTUAL_MASS,
    DirectTorqueGuardState,
    DirectTorquePacket,
    torque_formula_reference,
    validate_direct_torque_packet,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _packet(
    controller_sequence: int,
    *,
    model_sequence: int,
    model_period_us: int,
    model_mode: int,
    raw_f_df: tuple[float, ...],
) -> DirectTorquePacket:
    damping = tuple(
        2.0 * math.sqrt(stiffness * mass)
        for stiffness, mass in zip(
            DIRECT_TORQUE_BASELINE_K, DIRECT_TORQUE_VIRTUAL_MASS
        )
    )
    return DirectTorquePacket(
        sequence_before=controller_sequence,
        sequence_after=controller_sequence,
        heartbeat=controller_sequence,
        lease_id=7,
        mode=1,
        equilibrium_pose=(0.4, 0.1, 0.05, 0.0, 0.0, 0.0),
        stiffness=DIRECT_TORQUE_BASELINE_K,
        damping=damping,
        raw_feedforward_wrench=raw_f_df,
        model_sequence_before=model_sequence,
        model_sequence_after=model_sequence,
        model_period_us=model_period_us,
        model_mode=model_mode,
        wrench_frame_token=DIRECT_TORQUE_FRAME_TOKEN,
    )


def run_canary(*, ticks: int = 30_000, model_rate_hz: int = 200) -> dict:
    if ticks <= 0:
        raise ValueError("ticks must be positive")
    if model_rate_hz not in {50, 100, 200, 500}:
        raise ValueError("model_rate_hz must be 50, 100, 200, or 500")
    model_period_us = int(1_000_000 / model_rate_hz)
    raw_f_df = (1.0, -0.5, 0.25, 0.1, -0.05, 0.025)
    state = DirectTorqueGuardState()
    zero = (0.0,) * 6
    identity = tuple(
        tuple(1.0 if row == column else 0.0 for column in range(6))
        for row in range(6)
    )
    accepted = 0
    shadow_command_mismatches = 0
    max_diagnostic_filter_abs = 0.0
    started = time.perf_counter()
    for tick_index in range(ticks):
        controller_sequence = tick_index + 1
        model_sequence = (tick_index * model_rate_hz) // 500 + 1
        decision = validate_direct_torque_packet(
            _packet(
                controller_sequence,
                model_sequence=model_sequence,
                model_period_us=model_period_us,
                model_mode=1,
                raw_f_df=raw_f_df,
            ),
            state,
            release_ready=tick_index == 0,
            runtime_guard_ok=True,
        )
        if not decision.accepted:
            raise RuntimeError(
                f"logical canary rejected tick {tick_index}: {decision.reason}"
            )
        state = decision.next_state
        accepted += 1
        max_diagnostic_filter_abs = max(
            max_diagnostic_filter_abs,
            max(abs(value) for value in state.filtered_feedforward_wrench),
        )
        if state.applied_feedforward_wrench != zero:
            shadow_command_mismatches += 1
        disabled_tau = torque_formula_reference(
            identity, zero, zero, zero, zero, zero
        )
        shadow_tau = torque_formula_reference(
            identity,
            zero,
            zero,
            zero,
            zero,
            state.applied_feedforward_wrench,
        )
        if shadow_tau != disabled_tau:
            shadow_command_mismatches += 1

    # Independently prove that a stale model and MODEL_ACTIVE both fail closed.
    stale_state = DirectTorqueGuardState()
    stale_reason = None
    for tick_index in range(8):
        decision = validate_direct_torque_packet(
            _packet(
                tick_index + 1,
                model_sequence=1,
                model_period_us=5_000,
                model_mode=1,
                raw_f_df=raw_f_df,
            ),
            stale_state,
            release_ready=tick_index == 0,
            runtime_guard_ok=True,
        )
        stale_state = decision.next_state
        if not decision.accepted:
            stale_reason = decision.reason
            break
    active = validate_direct_torque_packet(
        _packet(
            1,
            model_sequence=1,
            model_period_us=5_000,
            model_mode=2,
            raw_f_df=raw_f_df,
        ),
        DirectTorqueGuardState(),
        release_ready=True,
        runtime_guard_ok=True,
    )
    elapsed = time.perf_counter() - started
    bindings = {
        "backend_source_sha256": _sha256(ROOT / "ur10e_vic" / "backends.py"),
        "layout_sha256": _sha256(ROOT / "config" / "direct_torque_rtde_layout.json"),
        "template_sha256": _sha256(
            ROOT / "programs" / "direct_torque_vic_offline_template.script"
        ),
        "canary_source_sha256": _sha256(Path(__file__)),
    }
    return {
        "schema": "ur10e_tacdiffusion_logical_synthetic_canary_v1",
        "scope": "Python packet/filter oracle; not URSim, wall-clock control timing, or hardware evidence",
        "logical_control_rate_hz": 500,
        "logical_ticks": ticks,
        "logical_duration_s": ticks / 500.0,
        "model_rate_hz": model_rate_hz,
        "model_period_us": model_period_us,
        "accepted_ticks": accepted,
        "accepted_ratio": accepted / ticks,
        "shadow_command_mismatches": shadow_command_mismatches,
        "max_diagnostic_filter_abs": max_diagnostic_filter_abs,
        "stale_fault_reason": stale_reason,
        "model_active_fault_reason": active.reason,
        "wall_clock_compute_s": elapsed,
        "source_bindings": bindings,
        "deterministic_test_pass": (
            accepted == ticks
            and shadow_command_mismatches == 0
            and max_diagnostic_filter_abs > 0.0
            and stale_reason == "model_stale_over_two_periods"
            and not active.accepted
            and active.reason == "model_active_not_authorized"
        ),
        "simulation_run": False,
        "hardware_run": False,
        "controller_verified": False,
        "live_motion_authorized": False,
        "claim_boundary": "UR10e 500 Hz force-domain diffusion adaptation preparation only",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks", type=int, default=30_000)
    parser.add_argument("--model-rate-hz", type=int, default=200)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = run_canary(ticks=args.ticks, model_rate_hz=args.model_rate_hz)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(rendered)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if payload["deterministic_test_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
