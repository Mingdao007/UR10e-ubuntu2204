"""Offline timing qualification for the contact-six writer composition.

The harness feeds the real contact provider and measured-observation runtime
with a deterministic owner-shaped input stream.  It exercises baseline,
one-second entry, the stationary State21 seam, and formal PATH for every
selected law.  It never opens RTDE, a sensor, a socket, a bridge, or a TP
program.  Timing numbers therefore describe this CPU composition only; they
are not live 500 Hz qualification.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

import numpy as np

from contact_benchmark_protocol import CONTROLLERS, ENTRY_DURATION_S, Task
from contact_benchmark_provider import ContactCommandProvider
from contact_benchmark_runtime import ContactRuntime
from contact_laws import ContactLaw
from step5c_calibrated_kinematics_audit import rotvec_to_matrix


TIMING_SCHEMA = "ur10e.contact-six-writer-timing-v2"
DEFAULT_DEADLINE_S = 0.0015
DEFAULT_TICKS = 1024
_REQUIRED_TOOL_OFFSET = (0.0, 0.0, 0.0874, 0.0, 0.0, 0.0)
_REQUIRED_PAYLOAD_KG = 0.413
_REQUIRED_COG_M = (0.0011, 0.0031, 0.0163)


def _percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * float(fraction)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + weight * (ordered[upper] - ordered[lower])


@dataclass(frozen=True)
class TimingConfig:
    """Offline writer-tick configuration.

    ``ticks`` counts owner ticks, including the baseline and the one-second
    entry.  A valid PATH seam therefore requires at least 503 ticks.
    """

    ticks: int = DEFAULT_TICKS
    dt_s: float = 0.002
    deadline_s: float = DEFAULT_DEADLINE_S
    held_age_s: float = 0.050

    def __post_init__(self) -> None:
        if type(self.ticks) is not int or self.ticks < 1:
            raise ValueError("timing ticks must be a positive integer")
        if not math.isclose(self.dt_s, 0.002, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("contact-six timing requires a 2 ms nominal period")
        if not math.isfinite(self.deadline_s) or self.deadline_s <= 0.0:
            raise ValueError("timing deadline must be positive")
        if not 0.020 <= self.held_age_s < 0.080:
            raise ValueError("held_age_s must be in the 20--80 ms band")


def _age_for_tick(index: int, *, held_age_s: float) -> float:
    """Ramp age without making the synthetic sensor timestamp regress."""

    if index < 100:
        return 0.010
    if index < 140:
        return 0.010 + (held_age_s - 0.010) * ((index - 99) / 40.0)
    return held_age_s


def _reference_pose(task: Task, stage: str, clock_s: float, anchor: np.ndarray,
                    target_rotvec: np.ndarray) -> tuple[float, ...]:
    if stage == "baseline":
        position = np.zeros(3, dtype=float)
    elif stage == "entry":
        position = np.asarray(task.entry_reference(clock_s)["position_m"], dtype=float)
    else:
        position = np.asarray(task.reference(clock_s)["position_m"], dtype=float)
    return tuple(float(value) for value in (*tuple(anchor + position), *target_rotvec))


def _owner_packets(*, now_s: float, age_s: float, pose: tuple[float, ...], q: tuple[float, ...]):
    """Build the same typed fields consumed by the provider adapter."""

    output = SimpleNamespace(
        observed_at_s=float(now_s),
        timestamp=float(now_s),
        safety_mode=1,
        safety_normal=True,
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        tcp_offset_m_rad=_REQUIRED_TOOL_OFFSET,
        payload_kg=_REQUIRED_PAYLOAD_KG,
        payload_cog_m=_REQUIRED_COG_M,
        q_rad=q,
        qd_rad_s=(0.0,) * 6,
        tcp_pose_m_rad=pose,
    )
    sensor = SimpleNamespace(
        sensor_fresh=True,
        stop_request=False,
        observed_at_s=float(now_s - age_s),
        wrench=(0.0, 0.0, -5.0, 0.0, 0.0, 0.0),
    )
    return output, sensor


def _timing_summary(
    durations_s: list[float],
    deadline_s: float | None = None,
    *,
    include_deadline: bool = True,
) -> dict[str, Any]:
    durations_ms = [1000.0 * value for value in durations_s]
    summary: dict[str, Any] = {
        "sample_count": len(durations_ms),
        "p50_ms": _percentile(durations_ms, 0.50),
        "p95_ms": _percentile(durations_ms, 0.95),
        "p99_ms": _percentile(durations_ms, 0.99),
        "max_ms": max(durations_ms) if durations_ms else None,
    }
    if include_deadline:
        if deadline_s is None:
            raise ValueError("deadline_s is required for a deadline summary")
        summary.update({
            "deadline_s": deadline_s,
            "observed_over_deadline_count": sum(value > deadline_s for value in durations_s),
            "deadline_reject_count": 0,
            "deadline_policy": "observed_only_offline; no live writer admission",
        })
    return summary


def _controller_run(
    controller: str,
    *,
    config_path: Path,
    qp_library: Path,
    law_build_root: Path,
    home: Mapping[str, Any],
    config: TimingConfig,
) -> dict[str, Any]:
    home_pose = tuple(float(value) for value in home["home_pose"])
    home_q = tuple(float(value) for value in home["home_q"])
    anchor = np.asarray(home_pose[:3], dtype=float)
    target_rotvec = np.asarray(home_pose[3:], dtype=float)
    target_rotation = rotvec_to_matrix(target_rotvec)
    task = Task()
    # Include both endpoints: entry clocks are 0.000, ..., 1.000 s.
    entry_steps = int(round(ENTRY_DURATION_S / config.dt_s)) + 1
    durations: list[float] = []
    kernel_durations: list[float] = []
    qp_durations: list[float] = []
    phase_counts: Counter[str] = Counter()
    seam_pause_count = 0
    errors: list[dict[str, str]] = []
    path_first_time: float | None = None
    now_s = 100.0

    with ContactLaw.from_config(
        controller,
        config_path,
        dimension=3,
        dt_s=config.dt_s,
        build_root=law_build_root,
    ) as law:
        runtime = ContactRuntime(
            law=law,
            qp_library=qp_library,
            anchor_m=anchor,
            task_basis=np.eye(3),
            target_rotation=target_rotation,
            # The timing run measures the full composition and records
            # deadline overruns; enforcement/rollback is covered separately by
            # the focused runtime tests.
            deadline_s=None,
        )
        provider = ContactCommandProvider(
            runtime=runtime,
            model_hashes={"calibration": runtime.model.calibration_hash},
            scenario="nominal",
            amplitude_n=0.0,
        )
        for index in range(config.ticks):
            if index == 0:
                phase, phase_clock = "baseline", None
            elif index <= entry_steps:
                phase, phase_clock = "entry", (index - 1) * config.dt_s
            elif index == entry_steps + 1:
                phase, phase_clock = "pause", None
            else:
                phase, phase_clock = "path", (index - entry_steps - 2) * config.dt_s
                if path_first_time is None:
                    path_first_time = phase_clock
            age_s = _age_for_tick(index, held_age_s=config.held_age_s)
            if phase == "baseline":
                pose = _reference_pose(task, "baseline", 0.0, anchor, target_rotvec)
            elif phase == "entry":
                pose = _reference_pose(task, "entry", float(phase_clock), anchor, target_rotvec)
            elif phase == "path":
                pose = _reference_pose(task, "path", float(phase_clock), anchor, target_rotvec)
            else:
                pose = _reference_pose(task, "entry", ENTRY_DURATION_S, anchor, target_rotvec)
            output, sensor = _owner_packets(now_s=now_s, age_s=age_s, pose=pose, q=home_q)
            started = time.perf_counter()
            try:
                command_result: Mapping[str, Any] | None = None
                if phase == "pause":
                    provider.pause(
                        output=output,
                        sensor=sensor,
                        monotonic_s=now_s,
                        actual_dt_s=config.dt_s,
                        reason="r013_tp_stationary_seam_pending",
                    )
                    seam_pause_count += 1
                elif phase == "baseline":
                    provider.command(
                        output=output,
                        sensor=sensor,
                        monotonic_s=now_s,
                        actual_dt_s=config.dt_s,
                        mode="baseline",
                        internal_setpoint_n=5.0,
                    )
                    command_result = provider.last_result
                elif phase == "entry":
                    provider.command(
                        output=output,
                        sensor=sensor,
                        monotonic_s=now_s,
                        actual_dt_s=config.dt_s,
                        mode="entry",
                        entry_time_s=float(phase_clock),
                        internal_setpoint_n=5.0,
                    )
                    command_result = provider.last_result
                else:
                    provider.command(
                        output=output,
                        sensor=sensor,
                        monotonic_s=now_s,
                        actual_dt_s=config.dt_s,
                        mode="path",
                        path_time_s=float(phase_clock),
                        internal_setpoint_n=5.0,
                    )
                    command_result = provider.last_result
                # ContactRuntime exposes the measured native-kernel and QP
                # spans in the same result that the writer consumes. Keep
                # these as diagnostics; the full writer duration is the
                # admission-relevant offline composition measure.
                if isinstance(command_result, Mapping):
                    kernel_wall_s = command_result.get("kernel_wall_s")
                    qp_solve_wall_s = command_result.get("qp_solve_wall_s")
                    if isinstance(kernel_wall_s, (int, float)) and math.isfinite(float(kernel_wall_s)):
                        kernel_durations.append(float(kernel_wall_s))
                    if isinstance(qp_solve_wall_s, (int, float)) and math.isfinite(float(qp_solve_wall_s)):
                        qp_durations.append(float(qp_solve_wall_s))
                durations.append(time.perf_counter() - started)
                phase_counts[phase] += 1
            except Exception as exc:  # structured diagnostic; never retry a failed tick
                errors.append({"phase": phase, "type": type(exc).__name__, "error": str(exc)})
                break
            now_s += config.dt_s

    freshness = runtime.freshness_summary()
    return {
        "controller": controller,
        "ticks_requested": config.ticks,
        "ticks_completed": len(durations),
        "phase_counts": dict(phase_counts),
        "state21_pause_count": seam_pause_count,
        "state21_tick_count": seam_pause_count,
        "state25_tick_count": phase_counts.get("path", 0),
        "path_first_formal_time_s": path_first_time,
        "timing": _timing_summary(durations, config.deadline_s),
        "warmup": {
            "tick_count": min(1, len(durations)),
            "timing": _timing_summary(durations[:1], include_deadline=False),
        },
        "steady_state_timing": _timing_summary(durations[1:], include_deadline=False),
        "kernel_timing": _timing_summary(kernel_durations, include_deadline=False),
        "qp_timing": _timing_summary(qp_durations, include_deadline=False),
        "freshness": freshness,
        "errors": errors,
        "seam": {
            "entry_duration_s": ENTRY_DURATION_S,
            "path_started": path_first_time == 0.0,
            "stationary_freeze_carry": seam_pause_count > 0,
            "state21_tick_count": seam_pause_count,
            "state25_first_formal_time_s": path_first_time,
            "claim_scope": "offline synthetic owner input; no transport or live qualification",
        },
    }


def run_writer_timing(
    *,
    experiment_root: Path,
    controllers: Iterable[str] = CONTROLLERS,
    config: TimingConfig = TimingConfig(),
) -> dict[str, Any]:
    """Run the offline complete-composition timing qualification."""

    root = Path(experiment_root).resolve()
    config_path = root / "config" / "contact_benchmark_laws.json"
    protocol_path = root / "config" / "contact_benchmark_protocol.json"
    qp_library = root / "build" / "contact-qp" / "libcontact_qp.so"
    law_build_root = root / "build" / "contact-six-laws"
    home_path = root / "report" / "contact-six-qp-20260917" / "preserved-home.json"
    if not config_path.is_file() or not protocol_path.is_file() or not qp_library.is_file():
        raise FileNotFoundError("contact-six timing inputs are incomplete")
    home = json.loads(home_path.read_text(encoding="utf-8"))
    selected = tuple(controllers)
    unknown = [controller for controller in selected if controller not in CONTROLLERS]
    if unknown:
        raise ValueError(f"unknown contact controller(s): {unknown}")
    if len(set(selected)) != len(selected):
        raise ValueError("timing controllers must be unique")
    rows = [
        _controller_run(
            controller,
            config_path=config_path,
            qp_library=qp_library,
            law_build_root=law_build_root,
            home=home,
            config=config,
        )
        for controller in selected
    ]
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    return {
        "schema": TIMING_SCHEMA,
        "controllers": list(selected),
        "protocol_sha256": protocol.get("sha256"),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "timing_config": {
            "ticks": config.ticks,
            "dt_s": config.dt_s,
            "deadline_s": config.deadline_s,
            "held_age_s": config.held_age_s,
        },
        "network_used": False,
        "device_io": False,
        "motion_authorized": False,
        "live_qualified": False,
        "hardware_qualified": False,
        "claim_scope": "offline synthetic owner input; measured CPU composition only; no live 500 Hz qualification",
        "controllers_results": rows,
    }


__all__ = ["TIMING_SCHEMA", "TimingConfig", "run_writer_timing"]
