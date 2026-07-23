"""Fail-closed live lifecycle for the minimal Step5d remote-control route."""

from __future__ import annotations

import copy
import csv
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from step5d_paper_outer_loop import rotvec_to_matrix

from . import engine, primitives


Vector6 = tuple[float, float, float, float, float, float]
CANONICAL_SAMPLE_FIELDS = (
    "t_s",
    "stage",
    "phase",
    "q0",
    "q1",
    "q2",
    "q3",
    "q4",
    "q5",
    "qd0",
    "qd1",
    "qd2",
    "qd3",
    "qd4",
    "qd5",
    "qcmd0",
    "qcmd1",
    "qcmd2",
    "qcmd3",
    "qcmd4",
    "qcmd5",
    "tcp_x",
    "tcp_y",
    "tcp_z",
    "tcp_r",
    "tcp_p",
    "tcp_yaw",
    "twist_x",
    "twist_y",
    "twist_z",
    "twist_rx",
    "twist_ry",
    "twist_rz",
    "wrench_fx",
    "wrench_fy",
    "wrench_fz",
    "wrench_mx",
    "wrench_my",
    "wrench_mz",
    "watchdog_status",
)
WATCHDOG_WAITING_ZERO = 0
WATCHDOG_ACTIVE = 1
WATCHDOG_FIRST_FAULT = 2


class LiveError(RuntimeError):
    """A fail-closed live gate or runtime fault."""


@dataclass
class LiveDependencies:
    monotonic: Callable[[], float]
    epoch_time: Callable[[], float]
    sleep: Callable[[float], None]
    dashboard_poll: Callable[[], Mapping[str, str]]
    dashboard_start: Callable[[], None]
    dashboard_check: Callable[[], Mapping[str, str]]
    dashboard_stop: Callable[[], None]
    sample_provider: Callable[[], engine.KinematicSample]
    publish_cmd: Callable[[Vector6], None]
    watchdog_status: Callable[[], int]
    monitor: Any
    driver_launch: Callable[[tuple[str, ...]], subprocess.Popen[Any]]
    driver_stop: Callable[[subprocess.Popen[Any]], None]
    controller_spawn: Callable[[tuple[str, ...]], None]
    controller_activate: Callable[[], None]
    controller_deactivate: Callable[[], None]
    controller_unload: Callable[[], None]
    read_boot_id: Callable[[], str]
    kernel_factory: Callable[[Mapping[str, Any]], Any]
    preflight: Callable[[], None]
    close: Callable[[], None]


def _as_float(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: expected float")
    value_f = float(value)
    if not np.isfinite(value_f):
        raise ValueError(f"{path}: non-finite")
    return value_f


def _as_float_seq(value: object, n: int, path: str) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or len(value) != n:
        raise ValueError(f"{path}: expected {n} values")
    return tuple(_as_float(item, f"{path}[{index}]") for index, item in enumerate(value))


def _zero_vec() -> Vector6:
    return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def _publish_zero(deps: LiveDependencies) -> None:
    deps.publish_cmd(_zero_vec())


def _trace_row(
    *,
    t_s: float,
    stage: str,
    phase: str,
    sample: engine.KinematicSample,
    qcmd: Vector6,
    watchdog_status: int,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "t_s": f"{t_s:.9f}",
        "stage": stage,
        "phase": phase,
        "watchdog_status": str(int(watchdog_status)),
    }
    for prefix, sequence in (
        ("q", sample.q),
        ("qd", sample.qd),
        ("qcmd", qcmd),
    ):
        for index, value in enumerate(sequence):
            values[f"{prefix}{index}"] = value
    for name, value in zip(
        ("tcp_x", "tcp_y", "tcp_z", "tcp_r", "tcp_p", "tcp_yaw"),
        sample.tcp_pose,
    ):
        values[name] = value
    for name, value in zip(
        ("twist_x", "twist_y", "twist_z", "twist_rx", "twist_ry", "twist_rz"),
        sample.tcp_twist,
    ):
        values[name] = value
    for name, value in zip(
        ("wrench_fx", "wrench_fy", "wrench_fz", "wrench_mx", "wrench_my", "wrench_mz"),
        sample.wrench_tcp,
    ):
        values[name] = value
    return values


def _dashboard_value(response: object) -> str:
    text = str(response).strip().lower()
    if ":" in text:
        text = text.rsplit(":", 1)[1].strip()
    return " ".join(text.split())


def validate_dashboard_snapshot(snapshot: Mapping[str, str]) -> dict[str, str]:
    if set(snapshot) != set(primitives.DASHBOARD_COMMANDS):
        raise LiveError("dashboard snapshot keys mismatch")
    values = {command: _dashboard_value(snapshot[command]) for command in primitives.DASHBOARD_COMMANDS}
    if values["is in remote control"] not in {"1", "true", "yes", "on"}:
        raise LiveError("robot is not in remote control")
    if values["safetymode"] != "normal":
        raise LiveError("safety mode is not NORMAL")
    if values["robotmode"] != "running":
        raise LiveError("robot mode is not RUNNING")
    if values["running"] not in {"true", "false"}:
        raise LiveError("program running state is unknown")
    return values


def _rotation_mismatch_angle(
    prior_rotvec: Sequence[float],
    current_rotvec: Sequence[float],
) -> float:
    prior = rotvec_to_matrix(np.asarray(_as_float_seq(prior_rotvec, 3, "prior_rotvec")))
    current = rotvec_to_matrix(np.asarray(_as_float_seq(current_rotvec, 3, "current_rotvec")))
    delta = prior.T @ current
    cos_angle = (float(np.trace(delta)) - 1.0) * 0.5
    return float(np.arccos(max(-1.0, min(1.0, cos_angle))))


def _validate_sample_freshness(
    cfg: Mapping[str, Any],
    sample: engine.KinematicSample,
    now_s: float,
) -> None:
    age_s = now_s - _as_float(sample.timestamp_s, "sample.timestamp_s")
    if age_s < 0.0:
        raise LiveError("joint-state sample timestamp is in the future")
    if age_s > _as_float(
        cfg["preflight"]["joint_state_stale_timeout_s"],
        "preflight.joint_state_stale_timeout_s",
    ):
        raise LiveError("joint-state sample stale")


def _validate_start_preflight(
    cfg: Mapping[str, Any],
    sample: engine.KinematicSample,
    now_s: float,
) -> None:
    _validate_sample_freshness(cfg, sample, now_s)
    if max(abs(value) for value in sample.qd) > _as_float(
        cfg["preflight"]["qd_tolerance_rad_s"],
        "preflight.qd_tolerance_rad_s",
    ):
        raise LiveError("joint speed exceeds preflight tolerance")
    prior_xyz = np.asarray(
        _as_float_seq(cfg["preflight"]["prior_xyz"], 3, "preflight.prior_xyz"),
        dtype=float,
    )
    current_xyz = np.asarray(sample.tcp_pose[:3], dtype=float)
    if float(np.linalg.norm(current_xyz - prior_xyz)) > _as_float(
        cfg["preflight"]["position_tolerance_m"],
        "preflight.position_tolerance_m",
    ):
        raise LiveError("TCP position is outside the r012 preflight tolerance")
    if _rotation_mismatch_angle(
        _as_float_seq(cfg["preflight"]["prior_rotvec"], 3, "preflight.prior_rotvec"),
        sample.tcp_pose[3:],
    ) > _as_float(
        cfg["preflight"]["orientation_tolerance_rad"],
        "preflight.orientation_tolerance_rad",
    ):
        raise LiveError("TCP orientation is outside the r012 preflight tolerance")


def _validate_wrench(cfg: Mapping[str, Any], sample: engine.KinematicSample) -> None:
    result = primitives.guard_wrench(cfg, sample.wrench_tcp)
    if not result.ok:
        raise LiveError("wrench guard triggered: " + ",".join(result.reasons))


def _sample(deps: LiveDependencies) -> engine.KinematicSample:
    try:
        return deps.sample_provider()
    except LiveError:
        raise
    except Exception as exc:
        raise LiveError(f"sample unavailable: {type(exc).__name__}: {exc}") from exc


def _watchdog(deps: LiveDependencies) -> int:
    try:
        status = int(deps.watchdog_status())
    except Exception as exc:
        raise LiveError(f"watchdog status unavailable: {type(exc).__name__}: {exc}") from exc
    if status < WATCHDOG_WAITING_ZERO or status > 255:
        raise LiveError(f"invalid watchdog status: {status}")
    if status >= WATCHDOG_FIRST_FAULT:
        raise LiveError(f"watchdog latched fault status={status}")
    return status


class _SafetyTicker:
    def __init__(self, deps: LiveDependencies, start_s: float) -> None:
        self._deps = deps
        self._next_check_s = start_s

    def check(self, now_s: float, *, force: bool = False) -> None:
        if not force and now_s < self._next_check_s:
            return
        try:
            snapshot = self._deps.dashboard_check()
        except Exception as exc:
            raise LiveError(f"dashboard safety watcher unavailable: {type(exc).__name__}: {exc}") from exc
        validate_dashboard_snapshot(snapshot)
        self._next_check_s = now_s + 1.0


def _iter_timing(
    deps: LiveDependencies,
    *,
    period_s: float,
    duration_s: float,
    max_miss_s: float,
):
    if period_s <= 0.0 or duration_s < 0.0 or max_miss_s < 0.0:
        raise ValueError("invalid control timing")
    started_s = deps.monotonic()
    deadline_s = started_s + duration_s
    next_tick_s = started_s
    previous_s = started_s - period_s
    while next_tick_s < deadline_s:
        now_s = deps.monotonic()
        dt_s = now_s - previous_s
        if dt_s <= 0.0:
            dt_s = period_s
        if dt_s > period_s + max_miss_s:
            raise LiveError(f"control loop missed deadline by {dt_s - period_s:.6f}s")
        previous_s = now_s
        yield now_s, dt_s
        next_tick_s += period_s
        delay_s = next_tick_s - deps.monotonic()
        if delay_s > 0.0:
            deps.sleep(delay_s)


def _timing_values(cfg: Mapping[str, Any]) -> tuple[float, float]:
    period_s = 1.0 / _as_float(cfg["command_rate_hz"], "command_rate_hz")
    max_miss_s = _as_float(
        cfg["guard"]["rate_watchdog_max_miss_s"],
        "guard.rate_watchdog_max_miss_s",
    )
    return period_s, max_miss_s


def _write_trace(
    writer: csv.DictWriter[Any],
    *,
    start_s: float,
    now_s: float,
    stage: str,
    phase: str,
    sample: engine.KinematicSample,
    qcmd: Vector6,
    watchdog_status: int,
) -> None:
    writer.writerow(
        _trace_row(
            t_s=now_s - start_s,
            stage=stage,
            phase=phase,
            sample=sample,
            qcmd=qcmd,
            watchdog_status=watchdog_status,
        )
    )


def _run_zero(
    deps: LiveDependencies,
    cfg: Mapping[str, Any],
    writer: csv.DictWriter[Any],
    safety: _SafetyTicker,
    *,
    start_s: float,
    duration_s: float,
) -> int:
    period_s, max_miss_s = _timing_values(cfg)
    rows = 0
    for now_s, _dt_s in _iter_timing(
        deps,
        period_s=period_s,
        duration_s=duration_s,
        max_miss_s=max_miss_s,
    ):
        safety.check(now_s)
        sample = _sample(deps)
        _validate_sample_freshness(cfg, sample, deps.monotonic())
        _validate_wrench(cfg, sample)
        status = _watchdog(deps)
        if status != WATCHDOG_ACTIVE:
            raise LiveError(f"watchdog left ACTIVE during zero stage: {status}")
        _publish_zero(deps)
        _write_trace(
            writer,
            start_s=start_s,
            now_s=now_s,
            stage="zero",
            phase="zero",
            sample=sample,
            qcmd=_zero_vec(),
            watchdog_status=status,
        )
        rows += 1
    return rows


def _run_free_space(
    deps: LiveDependencies,
    cfg: Mapping[str, Any],
    writer: csv.DictWriter[Any],
    safety: _SafetyTicker,
    *,
    start_s: float,
    duration_s: float,
) -> int:
    stage_cfg = copy.deepcopy(cfg)
    linear_cap = _as_float(
        cfg["canary"]["free_space_linear_limit_m_s"],
        "canary.free_space_linear_limit_m_s",
    )
    qdot_cap = min(
        _as_float(cfg["canary"]["free_space_qdot_limit_rad_s"], "canary.free_space_qdot_limit_rad_s"),
        _as_float(cfg["watchdog"]["max_abs_velocity_rad_s"], "watchdog.max_abs_velocity_rad_s"),
    )
    stage_cfg["limits"]["qdot_limit_rad_s"] = qdot_cap
    stage_cfg["path"]["motion_limit_m_s"] = linear_cap
    stage_cfg["path"]["total_linear_limit_m_s"] = linear_cap
    kernel = deps.kernel_factory(stage_cfg)
    kernel.reset()
    period_s, max_miss_s = _timing_values(cfg)
    stage_started_s = deps.monotonic()
    first_sample = _sample(deps)
    first_desired = primitives.free_space_twist(stage_cfg, 0.0)
    kernel.warm_start(first_sample, desired_twist=first_desired)
    rows = 0
    reaction = tuple(float(value) for value in cfg["frame"]["reaction_normal_b"])
    approach = tuple(float(value) for value in cfg["frame"]["approach_normal_b"])
    for now_s, dt_s in _iter_timing(
        deps,
        period_s=period_s,
        duration_s=duration_s,
        max_miss_s=max_miss_s,
    ):
        safety.check(now_s)
        sample = _sample(deps)
        _validate_sample_freshness(cfg, sample, deps.monotonic())
        _validate_wrench(cfg, sample)
        status = _watchdog(deps)
        if status != WATCHDOG_ACTIVE:
            raise LiveError(f"watchdog left ACTIVE during free-space stage: {status}")
        elapsed_s = max(0.0, now_s - stage_started_s)
        desired = primitives.free_space_twist(stage_cfg, elapsed_s)
        qcmd = tuple(
            float(value)
            for value in kernel.compute(
                sample,
                desired_twist=desired,
                reaction_normal=reaction,
                approach_normal=approach,
                dt_s=dt_s,
                normal_motion_policy="frame_contract_only",
                path_time_s=0.0,
            )
        )
        deps.publish_cmd(qcmd)
        _write_trace(
            writer,
            start_s=start_s,
            now_s=now_s,
            stage="free_space",
            phase="tangent",
            sample=sample,
            qcmd=qcmd,
            watchdog_status=status,
        )
        rows += 1

    settle_timeout_s = qdot_cap / _as_float(
        cfg["watchdog"]["max_acceleration_rad_s2"],
        "watchdog.max_acceleration_rad_s2",
    ) + 0.5
    zero_step_limit = (
        _as_float(
            cfg["watchdog"]["max_acceleration_rad_s2"],
            "watchdog.max_acceleration_rad_s2",
        )
        * period_s
    )
    for now_s, dt_s in _iter_timing(
        deps,
        period_s=period_s,
        duration_s=settle_timeout_s,
        max_miss_s=max_miss_s,
    ):
        safety.check(now_s)
        sample = _sample(deps)
        _validate_sample_freshness(cfg, sample, deps.monotonic())
        _validate_wrench(cfg, sample)
        status = _watchdog(deps)
        if status != WATCHDOG_ACTIVE:
            raise LiveError(f"watchdog left ACTIVE during free-space settle: {status}")
        qcmd = tuple(
            float(value)
            for value in kernel.compute(
                sample,
                desired_twist=_zero_vec(),
                reaction_normal=reaction,
                approach_normal=approach,
                dt_s=dt_s,
                normal_motion_policy="frame_contract_only",
                path_time_s=0.0,
            )
        )
        deps.publish_cmd(qcmd)
        _write_trace(
            writer,
            start_s=start_s,
            now_s=now_s,
            stage="free_space",
            phase="settle",
            sample=sample,
            qcmd=qcmd,
            watchdog_status=status,
        )
        rows += 1
        if max(abs(value) for value in qcmd) <= zero_step_limit + 1e-12:
            deps.sleep(period_s)
            _publish_zero(deps)
            deps.sleep(period_s)
            final_sample = _sample(deps)
            _validate_sample_freshness(cfg, final_sample, deps.monotonic())
            _validate_wrench(cfg, final_sample)
            final_status = _watchdog(deps)
            if final_status != WATCHDOG_ACTIVE:
                raise LiveError(
                    f"watchdog left ACTIVE before free-space exact zero: {final_status}"
                )
            _write_trace(
                writer,
                start_s=start_s,
                now_s=deps.monotonic(),
                stage="free_space",
                phase="zero",
                sample=final_sample,
                qcmd=_zero_vec(),
                watchdog_status=final_status,
            )
            return rows + 1
    _publish_zero(deps)
    raise LiveError("free-space command did not settle to exact zero")


def _guarded_timeout_s(cfg: Mapping[str, Any], track_duration_s: float) -> float:
    search_s = _as_float(cfg["search"]["timeout_s"], "search.timeout_s")
    distance_m = _as_float(cfg["retract"]["distance_m"], "retract.distance_m")
    speed_m_s = _as_float(cfg["retract"]["speed_m_s"], "retract.speed_m_s")
    retract_s = distance_m / speed_m_s
    return search_s + track_duration_s + retract_s + 2.0


def _run_guarded(
    deps: LiveDependencies,
    cfg: Mapping[str, Any],
    writer: csv.DictWriter[Any],
    safety: _SafetyTicker,
    *,
    start_s: float,
    track_duration_s: float,
) -> tuple[int, engine.ContactProgramResult]:
    guarded_cfg = copy.deepcopy(cfg)
    guarded_cfg["run_duration_s"] = track_duration_s
    guarded_cfg["limits"]["qdot_limit_rad_s"] = min(
        _as_float(cfg["limits"]["qdot_limit_rad_s"], "limits.qdot_limit_rad_s"),
        _as_float(cfg["watchdog"]["max_abs_velocity_rad_s"], "watchdog.max_abs_velocity_rad_s"),
    )
    kernel = deps.kernel_factory(guarded_cfg)
    program = engine.ContactProgram.create(guarded_cfg, kernel=kernel)
    period_s, max_miss_s = _timing_values(cfg)
    rows = 0
    last: engine.ContactProgramResult | None = None
    for now_s, dt_s in _iter_timing(
        deps,
        period_s=period_s,
        duration_s=_guarded_timeout_s(cfg, track_duration_s),
        max_miss_s=max_miss_s,
    ):
        safety.check(now_s)
        sample = _sample(deps)
        _validate_sample_freshness(cfg, sample, deps.monotonic())
        _validate_wrench(cfg, sample)
        status = _watchdog(deps)
        if status != WATCHDOG_ACTIVE:
            raise LiveError(f"watchdog left ACTIVE during guarded stage: {status}")
        try:
            result = program.step(sample, dt_s=dt_s)
        except Exception as exc:
            raise LiveError(f"contact program failed: {type(exc).__name__}: {exc}") from exc
        last = result
        qcmd = tuple(float(value) for value in result.qdot)
        if result.faulted or result.phase == engine.RemotePhase.ABORT:
            _publish_zero(deps)
            raise LiveError(f"contact program faulted in {result.phase.value}")
        deps.publish_cmd(qcmd)
        _write_trace(
            writer,
            start_s=start_s,
            now_s=now_s,
            stage="guarded_contact",
            phase=result.phase.value.lower(),
            sample=sample,
            qcmd=qcmd,
            watchdog_status=status,
        )
        rows += 1
        if result.phase == engine.RemotePhase.STOP:
            _publish_zero(deps)
            return rows, result
    _publish_zero(deps)
    if last is None:
        raise LiveError("guarded stage produced no samples")
    raise LiveError(f"guarded stage timed out in {last.phase.value}")


def _ensure_watchdog_active(
    deps: LiveDependencies,
    safety: _SafetyTicker,
    *,
    timeout_s: float,
) -> None:
    deadline_s = deps.monotonic() + timeout_s
    while deps.monotonic() < deadline_s:
        now_s = deps.monotonic()
        safety.check(now_s)
        _publish_zero(deps)
        status = _watchdog(deps)
        if status == WATCHDOG_ACTIVE:
            return
        deps.sleep(0.005)
    raise LiveError("watchdog did not enter ACTIVE after exact-zero commands")


def _wait_start_sample(
    deps: LiveDependencies,
    cfg: Mapping[str, Any],
    safety: _SafetyTicker,
) -> engine.KinematicSample:
    deadline_s = deps.monotonic() + _as_float(
        cfg["preflight"]["ready_timeout_s"],
        "preflight.ready_timeout_s",
    )
    last_error = "sample unavailable"
    while deps.monotonic() < deadline_s:
        now_s = deps.monotonic()
        safety.check(now_s)
        _publish_zero(deps)
        try:
            sample = _sample(deps)
            _validate_start_preflight(cfg, sample, deps.monotonic())
            return sample
        except LiveError as exc:
            last_error = str(exc)
        deps.sleep(0.005)
    raise LiveError(f"preflight did not become ready: {last_error}")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _write_monitor_summary(
    deps: LiveDependencies,
    path: Path,
    *,
    command: str,
    outcome: str,
    rows: int,
    cleanup_errors: Sequence[str],
) -> None:
    extra = {
        "command": command,
        "outcome": outcome,
        "trace_rows": rows,
        "cleanup_errors": list(cleanup_errors),
        "live_motion": True,
    }
    if hasattr(deps.monitor, "write_summary"):
        deps.monitor.write_summary(path, extra=extra)
    else:
        _write_json(path, extra)


def _failure_summary(
    *,
    command: str,
    cfg: Mapping[str, Any],
    params_path: Path,
    params_sha256: str,
    outcome: str,
    error: str | None,
    rows: int,
    startup_commands: Sequence[tuple[str, ...]],
    cleanup_errors: Sequence[str],
    canary_path: Path | None,
) -> dict[str, Any]:
    from .runtime import materialize_watchdog_param_yaml

    return {
        "command": command,
        "route": cfg["route"],
        "schema_version": 1,
        "params_file": str(params_path),
        "params_sha256": params_sha256,
        "outcome": outcome,
        "error": error,
        "trace_rows": rows,
        "startup_commands": [" ".join(command) for command in startup_commands],
        "cleanup_errors": list(cleanup_errors),
        "canary_evidence_path": None if canary_path is None else str(canary_path),
        "watchdog_controller_yaml": materialize_watchdog_param_yaml(cfg),
    }


def run_live(
    command: str,
    experiment_root: Path,
    output_root: Path,
    cfg: Mapping[str, Any],
    params_path: Path,
    params_yaml: str,
    params_sha256: str,
    *,
    dependencies: LiveDependencies | None = None,
) -> int:
    del experiment_root, params_yaml
    command = command.strip().lower()
    if command not in {"canary", "run"}:
        raise ValueError(f"unsupported live command: {command}")

    output_root.mkdir(parents=True, exist_ok=True)
    trace_path = output_root / "trace.csv"
    summary_path = output_root / "summary.json"
    monitor_path = output_root / "kunwei_monitor.json"
    deps = dependencies
    trace_handle = None
    dashboard_started = False
    monitor_started = False
    driver_process: subprocess.Popen[Any] | None = None
    controller_spawned = False
    controller_active = False
    watchdog_yaml_path: Path | None = None
    startup_commands: list[tuple[str, ...]] = []
    cleanup_errors: list[str] = []
    canary_evidence_path: Path | None = None
    canary_evidence: dict[str, Any] | None = None
    rows = 0
    outcome = "fault"
    error: str | None = None
    rc = 78

    try:
        if deps is None:
            from .ros_adapter import build_production_dependencies

            deps = build_production_dependencies(cfg, output_root)

        if command == "run":
            from .runtime import latest_canary_summary, validate_canary_evidence

            base_output_root = output_root.parents[1]
            canary_evidence_path = latest_canary_summary(base_output_root)
            if canary_evidence_path is None:
                raise LiveError("missing prior canary evidence")
            validate_canary_evidence(
                cfg,
                canary_evidence_path,
                params_sha256=params_sha256,
                current_boot_id=deps.read_boot_id(),
                now_epoch_s=deps.epoch_time(),
            )

        deps.preflight()
        trace_handle = trace_path.open("w", newline="", encoding="utf-8")
        writer = csv.DictWriter(trace_handle, fieldnames=CANONICAL_SAMPLE_FIELDS)
        writer.writeheader()

        initial_dashboard = deps.dashboard_poll()
        validate_dashboard_snapshot(initial_dashboard)
        deps.dashboard_start()
        dashboard_started = True
        safety = _SafetyTicker(deps, deps.monotonic())
        safety.check(deps.monotonic(), force=True)

        deps.monitor.start()
        monitor_started = True
        if not deps.monitor.wait_ready(
            _as_float(cfg["sensor"]["ready_timeout_s"], "sensor.ready_timeout_s")
        ):
            raise LiveError("Kunwei monitor did not become ready")
        deps.sleep(_as_float(cfg["sensor"]["baseline_s"], "sensor.baseline_s"))
        deps.monitor.reset_baseline_from_recent()
        deps.sleep(_as_float(cfg["sensor"]["rezero_s"], "sensor.rezero_s"))
        deps.monitor.reset_baseline_from_recent()

        driver_command = build_driver_command(cfg, reverse=True)
        startup_commands.append(driver_command)
        driver_process = deps.driver_launch(driver_command)

        from .runtime import materialize_watchdog_param_yaml

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(materialize_watchdog_param_yaml(cfg))
            watchdog_yaml_path = Path(handle.name)
        spawner_command = build_spawner_command(cfg, watchdog_yaml_path)
        startup_commands.append(spawner_command)
        deps.controller_spawn(spawner_command)
        controller_spawned = True
        deps.controller_activate()
        controller_active = True
        _ensure_watchdog_active(deps, safety, timeout_s=2.0)
        _wait_start_sample(deps, cfg, safety)
        start_s = deps.monotonic()

        if command == "canary":
            rows += _run_zero(
                deps,
                cfg,
                writer,
                safety,
                start_s=start_s,
                duration_s=_as_float(cfg["canary"]["zero_s"], "canary.zero_s"),
            )
            rows += _run_free_space(
                deps,
                cfg,
                writer,
                safety,
                start_s=start_s,
                duration_s=_as_float(cfg["canary"]["free_space_s"], "canary.free_space_s"),
            )
            guarded_rows, result = _run_guarded(
                deps,
                cfg,
                writer,
                safety,
                start_s=start_s,
                track_duration_s=_as_float(
                    cfg["canary"]["guarded_contact_s"],
                    "canary.guarded_contact_s",
                ),
            )
            rows += guarded_rows
            if result.phase != engine.RemotePhase.STOP or result.faulted:
                raise LiveError("canary did not reach STOP")
            canary_evidence = primitives.build_canary_evidence(
                params_sha256=params_sha256,
                boot_id=deps.read_boot_id(),
                created_at_epoch_s=deps.epoch_time(),
                stage_statuses={
                    "zero": "passed",
                    "free_space": "passed",
                    "guarded_contact": "passed",
                },
            )
        else:
            guarded_rows, result = _run_guarded(
                deps,
                cfg,
                writer,
                safety,
                start_s=start_s,
                track_duration_s=_as_float(cfg["run_duration_s"], "run_duration_s"),
            )
            rows += guarded_rows
            if result.phase != engine.RemotePhase.STOP or result.faulted:
                raise LiveError("run did not reach STOP")
        outcome = "ok"
        rc = 0
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        outcome = "fault"
        rc = 78
    finally:
        if deps is not None:
            if controller_active:
                try:
                    _publish_zero(deps)
                except Exception as exc:
                    cleanup_errors.append(f"zero:{type(exc).__name__}:{exc}")
            if controller_active:
                try:
                    deps.controller_deactivate()
                    controller_active = False
                except Exception as exc:
                    cleanup_errors.append(f"deactivate:{type(exc).__name__}:{exc}")
            if controller_spawned:
                try:
                    deps.controller_unload()
                    controller_spawned = False
                except Exception as exc:
                    cleanup_errors.append(f"unload:{type(exc).__name__}:{exc}")
            if driver_process is not None:
                try:
                    deps.driver_stop(driver_process)
                    driver_process = None
                except Exception as exc:
                    cleanup_errors.append(f"driver_stop:{type(exc).__name__}:{exc}")
            if monitor_started:
                try:
                    deps.monitor.stop()
                    monitor_started = False
                except Exception as exc:
                    cleanup_errors.append(f"monitor_stop:{type(exc).__name__}:{exc}")
            if dashboard_started:
                try:
                    deps.dashboard_stop()
                    dashboard_started = False
                except Exception as exc:
                    cleanup_errors.append(f"dashboard_stop:{type(exc).__name__}:{exc}")
        if trace_handle is not None:
            trace_handle.close()
        if watchdog_yaml_path is not None:
            watchdog_yaml_path.unlink(missing_ok=True)

        if cleanup_errors and rc == 0:
            error = "cleanup failed: " + "; ".join(cleanup_errors)
            outcome = "fault"
            rc = 78
            canary_evidence = None
        if deps is not None:
            try:
                deps.close()
            except Exception as exc:
                cleanup_errors.append(f"close:{type(exc).__name__}:{exc}")
                error = "resource close failed"
                outcome = "fault"
                rc = 78
                canary_evidence = None
            try:
                _write_monitor_summary(
                    deps,
                    monitor_path,
                    command=command,
                    outcome=outcome,
                    rows=rows,
                    cleanup_errors=cleanup_errors,
                )
            except Exception as exc:
                cleanup_errors.append(f"monitor_summary:{type(exc).__name__}:{exc}")
                error = "monitor summary failed"
                outcome = "fault"
                rc = 78
                canary_evidence = None

        if command == "canary" and rc == 0 and canary_evidence is not None:
            _write_json(summary_path, canary_evidence)
        else:
            _write_json(
                summary_path,
                _failure_summary(
                    command=command,
                    cfg=cfg,
                    params_path=params_path,
                    params_sha256=params_sha256,
                    outcome=outcome,
                    error=error,
                    rows=rows,
                    startup_commands=startup_commands,
                    cleanup_errors=cleanup_errors,
                    canary_path=canary_evidence_path,
                ),
            )
    return rc


def build_driver_command(
    cfg: Mapping[str, Any],
    *,
    reverse: bool = True,
) -> tuple[str, ...]:
    return primitives.build_driver_command(cfg, reverse=reverse)


def build_spawner_command(
    cfg: Mapping[str, Any],
    temp_yaml_path: str | Path,
) -> tuple[str, ...]:
    return primitives.build_spawner_command(cfg, temp_yaml_path)
