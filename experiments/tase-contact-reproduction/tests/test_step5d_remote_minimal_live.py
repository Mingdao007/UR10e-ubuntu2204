"""Offline acceptance tests for the Step5d live lifecycle and ROS seam."""

from __future__ import annotations

import copy
import csv
import io
import json
import os
import signal
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = str(EXPERIMENT_ROOT / "tools")
if TOOLS_ROOT not in sys.path:
    sys.path.insert(0, TOOLS_ROOT)

from step5d_remote_control import core, engine, live, primitives, ros_adapter, runtime
from step5d_remote_control.kinematics import CalibratedKinematics


CONFIG_PATH = "config/step5d_remote/r012.yaml"


def _cfg() -> dict:
    return copy.deepcopy(core.load_r012_config(EXPERIMENT_ROOT, CONFIG_PATH))


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, duration_s: float) -> None:
        self.value += max(0.0, float(duration_s))


class FakeMonitor:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.reset_count = 0

    def start(self) -> None:
        self.events.append("monitor_start")

    def wait_ready(self, timeout_s: float) -> bool:
        assert timeout_s > 0.0
        self.events.append("monitor_ready")
        return True

    def reset_baseline_from_recent(self) -> None:
        self.reset_count += 1
        self.events.append("monitor_rezero")

    def stop(self) -> None:
        self.events.append("monitor_stop")

    def write_summary(self, path: Path, extra: dict | None = None) -> None:
        payload = {"sensor": "fake"}
        payload.update(extra or {})
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.events.append("monitor_summary")


class FakeKernel:
    def __init__(self, cfg: dict, events: list[str] | None = None) -> None:
        self.cfg = cfg
        self.events = [] if events is None else events
        self.warmed = False
        self.desired: list[tuple[float, ...]] = []

    def reset(self) -> None:
        self.events.append("kernel_reset")
        self.warmed = False

    def warm_start(self, sample: engine.KinematicSample, *, desired_twist) -> None:
        assert len(sample.jacobian) == 6
        self.events.append("kernel_warm")
        self.warmed = True
        self.desired.append(tuple(desired_twist))

    def compute(self, sample: engine.KinematicSample, *, desired_twist, **_kwargs):
        assert self.warmed
        assert len(sample.q) == 6
        self.desired.append(tuple(desired_twist))
        return (0.001, -0.001, 0.0, 0.0, 0.0, 0.0)


def _sample(
    cfg: dict,
    timestamp_s: float,
    *,
    tcp_pose: tuple[float, float, float, float, float, float] | None = None,
) -> engine.KinematicSample:
    if tcp_pose is None:
        tcp_pose = (
            *tuple(float(value) for value in cfg["preflight"]["prior_xyz"]),
            *tuple(float(value) for value in cfg["preflight"]["prior_rotvec"]),
        )
    return engine.KinematicSample(
        q=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        qd=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        tcp_pose=tcp_pose,
        tcp_twist=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        wrench_tcp=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        jacobian=tuple(
            tuple(1.0 if row == column else 0.0 for column in range(6))
            for row in range(6)
        ),
        q_min=(-6.0, -6.0, -3.0, -6.0, -6.0, -6.0),
        q_max=(6.0, 6.0, 3.0, 6.0, 6.0, 6.0),
        timestamp_s=timestamp_s,
    )


def _dashboard() -> dict[str, str]:
    return {
        "is in remote control": "Is in remote control: true",
        "safetymode": "Safetymode: NORMAL",
        "robotmode": "Robotmode: RUNNING",
        "running": "Program running: true",
    }


def _dependencies(
    cfg: dict,
    *,
    clock: FakeClock,
    events: list[str],
    fail_deactivate: bool = False,
) -> live.LiveDependencies:
    monitor = FakeMonitor(events)

    def event(name: str):
        def call(*_args, **_kwargs):
            events.append(name)
            if name == "deactivate" and fail_deactivate:
                raise RuntimeError("deactivate failed")
            if name == "driver_launch":
                return SimpleNamespace(name="fake-driver")
            return None

        return call

    return live.LiveDependencies(
        monotonic=clock.monotonic,
        epoch_time=lambda: 1_800_000_000.0,
        sleep=clock.sleep,
        dashboard_poll=lambda: (events.append("dashboard_poll") or _dashboard()),
        dashboard_start=event("dashboard_start"),
        dashboard_check=lambda: _dashboard(),
        dashboard_stop=event("dashboard_stop"),
        sample_provider=lambda: _sample(cfg, clock.monotonic()),
        publish_cmd=lambda qdot: events.append("publish_zero" if not any(qdot) else "publish_nonzero"),
        watchdog_status=lambda: live.WATCHDOG_ACTIVE,
        monitor=monitor,
        driver_launch=event("driver_launch"),
        driver_stop=event("driver_stop"),
        controller_spawn=event("spawn"),
        controller_activate=event("activate"),
        controller_deactivate=event("deactivate"),
        controller_unload=event("unload"),
        read_boot_id=lambda: "boot-id-1",
        kernel_factory=lambda kernel_cfg: FakeKernel(kernel_cfg, events),
        preflight=event("preflight"),
        close=event("close"),
    )


def _patch_fast_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "_run_zero", lambda *_args, **_kwargs: 2)
    monkeypatch.setattr(live, "_run_free_space", lambda *_args, **_kwargs: 3)
    monkeypatch.setattr(
        live,
        "_run_guarded",
        lambda *_args, **_kwargs: (
            4,
            engine.ContactProgramResult(
                phase=engine.RemotePhase.STOP,
                qdot=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                desired_twist=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
                faulted=False,
            ),
        ),
    )
    monkeypatch.setattr(
        live,
        "_run_post_canary_safe_pose",
        lambda *_args, **_kwargs: 5,
    )


def test_dashboard_snapshot_accepts_canonical_prefixed_responses() -> None:
    values = live.validate_dashboard_snapshot(_dashboard())
    assert values == {
        "is in remote control": "true",
        "safetymode": "normal",
        "robotmode": "running",
        "running": "true",
    }
    bad = _dashboard()
    bad["is in remote control"] = "Is in remote control: false"
    with pytest.raises(live.LiveError, match="not in remote"):
        live.validate_dashboard_snapshot(bad)
    stopped = _dashboard()
    stopped["running"] = "Program running: false"
    assert live.validate_dashboard_snapshot(stopped)["running"] == "false"
    with pytest.raises(live.LiveError, match="not running"):
        live.validate_dashboard_snapshot(stopped, require_running=True)


def test_driver_cleanup_terminates_the_entire_launch_process_group() -> None:
    process = ros_adapter._launch_driver(("/bin/bash", "-lc", "sleep 30 & wait"))
    process_group = process.pid
    time.sleep(0.05)
    try:
        os.killpg(process_group, 0)
        ros_adapter._stop_driver(process)
        with pytest.raises(ProcessLookupError):
            os.killpg(process_group, 0)
    finally:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_dashboard_ticker_requires_running_only_after_motion_arm() -> None:
    cfg = _cfg()
    clock = FakeClock()
    deps = _dependencies(cfg, clock=clock, events=[])
    stopped = _dashboard()
    stopped["running"] = "Program running: false"
    deps.dashboard_check = lambda: stopped
    ticker = live._SafetyTicker(deps, clock.monotonic())
    ticker.check(clock.monotonic(), force=True)
    ticker.require_running()
    with pytest.raises(live.LiveError, match="not running"):
        ticker.check(clock.monotonic(), force=True)


def test_prealign_restores_r012_xy_then_vertical_contract() -> None:
    cfg = _cfg()
    current_pose = (
        0.460802483,
        0.124884915,
        0.066847150,
        3.109205904,
        0.004330144,
        0.038693807,
    )
    sample = _sample(cfg, 1.0, tcp_pose=current_pose)
    live._validate_prealign_start(cfg, sample, 1.0)

    target_xyz = tuple(cfg["preflight"]["prior_xyz"])
    target_rotvec = tuple(cfg["preflight"]["prior_rotvec"])
    xy_target = (target_xyz[0], target_xyz[1], current_pose[2], *target_rotvec)
    xy_twist, xy_error, orientation_error = live._prealign_desired_twist(
        cfg,
        sample,
        xy_target,
    )
    assert xy_error < cfg["prealign"]["max_xy_offset_m"]
    assert 0.0 < orientation_error < cfg["preflight"]["orientation_tolerance_rad"]
    assert xy_twist[0] > 0.0
    assert xy_twist[1] > 0.0
    assert abs(xy_twist[2]) < 1e-12
    assert np.linalg.norm(xy_twist[:3]) <= cfg["prealign"]["linear_speed_m_s"] + 1e-12
    assert np.linalg.norm(xy_twist[3:]) <= cfg["prealign"]["angular_limit_rad_s"] + 1e-12

    at_xy = _sample(cfg, 2.0, tcp_pose=xy_target)
    z_target = (*target_xyz, *target_rotvec)
    z_twist, z_error, z_orientation_error = live._prealign_desired_twist(
        cfg,
        at_xy,
        z_target,
    )
    assert z_error > 0.04
    assert z_orientation_error < 1e-7
    assert abs(z_twist[0]) < 1e-12
    assert abs(z_twist[1]) < 1e-12
    assert z_twist[2] < 0.0


def test_prealign_start_rejects_low_or_far_pose() -> None:
    cfg = _cfg()
    target = tuple(cfg["preflight"]["prior_xyz"])
    rotvec = tuple(cfg["preflight"]["prior_rotvec"])
    low = _sample(
        cfg,
        1.0,
        tcp_pose=(target[0], target[1], target[2] + 0.005, *rotvec),
    )
    with pytest.raises(live.LiveError, match="not high enough"):
        live._validate_prealign_start(cfg, low, 1.0)
    far = _sample(
        cfg,
        1.0,
        tcp_pose=(target[0] + 0.2, target[1], target[2] + 0.02, *rotvec),
    )
    with pytest.raises(live.LiveError, match="XY offset"):
        live._validate_prealign_start(cfg, far, 1.0)


def test_calibrated_kinematics_uses_six_limits_and_tcp_jacobian() -> None:
    cfg = _cfg()
    calibrated = CalibratedKinematics.from_config(cfg)
    q = (0.0, -1.57, 1.57, -1.57, -1.57, 0.0)
    qd = (0.01, -0.02, 0.03, -0.01, 0.02, -0.03)
    sample = calibrated.evaluate(q, qd, 10.0)
    assert calibrated.calibration_hash == cfg["calibration"]["expected_hash"]
    assert calibrated.flange_frame != calibrated.tool0_frame
    assert len(sample.q_min) == len(sample.q_max) == 6
    jacobian = np.asarray(sample.jacobian)
    assert jacobian.shape == (6, 6)
    assert np.allclose(jacobian @ np.asarray(qd), np.asarray(sample.tcp_twist), atol=1e-12)
    assert np.linalg.norm(np.asarray(sample.tcp_pose[3:])) > 3.0


def test_free_space_stage_solves_joint_velocity_instead_of_publishing_twist() -> None:
    cfg = _cfg()
    cfg["canary"]["free_space_s"] = 0.004
    clock = FakeClock()
    events: list[str] = []
    deps = _dependencies(cfg, clock=clock, events=events)
    created: list[FakeKernel] = []
    deps.kernel_factory = lambda kernel_cfg: created.append(FakeKernel(kernel_cfg)) or created[-1]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=live.CANONICAL_SAMPLE_FIELDS)
    writer.writeheader()
    rows = live._run_free_space(
        deps,
        cfg,
        writer,
        live._SafetyTicker(deps, clock.monotonic()),
        start_s=clock.monotonic(),
        duration_s=cfg["canary"]["free_space_s"],
    )
    assert rows >= 4
    assert created[0].cfg["limits"]["qdot_limit_rad_s"] == cfg["canary"]["free_space_qdot_limit_rad_s"]
    assert "publish_nonzero" in events
    trace = list(csv.DictReader(io.StringIO(stream.getvalue())))
    assert float(trace[0]["qcmd0"]) == 0.001
    assert abs(float(trace[0]["twist_x"])) < 1e-12
    reaction = np.asarray(cfg["frame"]["reaction_normal_b"])
    for desired in created[0].desired:
        assert abs(float(np.dot(np.asarray(desired[:3]), reaction))) < 1e-12


def test_canary_lifecycle_writes_exact_strict_evidence_after_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, params_path, params_sha = runtime.load_runtime_config(EXPERIMENT_ROOT)
    params_yaml = yaml.safe_dump(cfg, sort_keys=True)
    output = tmp_path / "canary" / "one"
    runtime.write_runtime_artifacts(
        output,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        command="canary",
    )
    events: list[str] = []
    clock = FakeClock()
    deps = _dependencies(cfg, clock=clock, events=events)
    _patch_fast_stages(monkeypatch)
    rc = live.run_live(
        "canary",
        EXPERIMENT_ROOT,
        output,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        dependencies=deps,
    )
    assert rc == 0
    evidence = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert set(evidence) == {"ok", "params_sha256", "boot_id", "created_at_epoch_s", "stages"}
    runtime.validate_canary_evidence(
        cfg,
        output / "summary.json",
        params_sha256=params_sha,
        current_boot_id="boot-id-1",
        now_epoch_s=1_800_000_000.0,
    )
    assert events.index("preflight") < events.index("dashboard_poll")
    assert events.index("deactivate") < events.index("unload") < events.index("driver_stop")
    assert events.index("driver_stop") < events.index("monitor_stop") < events.index("dashboard_stop")
    assert events.index("dashboard_stop") < events.index("close") < events.index("monitor_summary")
    assert set(path.name for path in output.iterdir()) == set(runtime.ARTIFACT_FILES)


def test_cleanup_failure_invalidates_canary_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, params_path, params_sha = runtime.load_runtime_config(EXPERIMENT_ROOT)
    params_yaml = yaml.safe_dump(cfg, sort_keys=True)
    output = tmp_path / "canary" / "cleanup-fault"
    runtime.write_runtime_artifacts(
        output,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        command="canary",
    )
    events: list[str] = []
    deps = _dependencies(cfg, clock=FakeClock(), events=events, fail_deactivate=True)
    _patch_fast_stages(monkeypatch)
    assert live.run_live(
        "canary",
        EXPERIMENT_ROOT,
        output,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        dependencies=deps,
    ) == 78
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "fault"
    assert "deactivate" in " ".join(summary["cleanup_errors"])
    assert summary.get("ok") is not True


def test_run_requires_same_boot_fresh_canary_before_external_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg, params_path, params_sha = runtime.load_runtime_config(EXPERIMENT_ROOT)
    params_yaml = yaml.safe_dump(cfg, sort_keys=True)
    output = tmp_path / "run" / "one"
    runtime.write_runtime_artifacts(
        output,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        command="run",
    )
    events: list[str] = []
    deps = _dependencies(cfg, clock=FakeClock(), events=events)
    _patch_fast_stages(monkeypatch)
    assert live.run_live(
        "run",
        EXPERIMENT_ROOT,
        output,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        dependencies=deps,
    ) == 78
    assert "dashboard_poll" not in events
    assert "driver_launch" not in events

    canary_root = tmp_path / "canary" / "one"
    canary_root.mkdir(parents=True)
    evidence = primitives.build_canary_evidence(
        params_sha256=params_sha,
        boot_id="boot-id-1",
        created_at_epoch_s=1_799_999_999.0,
        stage_statuses={
            "zero": "passed",
            "free_space": "passed",
            "guarded_contact": "passed",
            "post_canary_safe_pose": "passed",
        },
    )
    (canary_root / "summary.json").write_text(json.dumps(evidence), encoding="utf-8")
    events.clear()
    output_ok = tmp_path / "run" / "two"
    runtime.write_runtime_artifacts(
        output_ok,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        command="run",
    )
    deps = _dependencies(cfg, clock=FakeClock(), events=events)
    assert live.run_live(
        "run",
        EXPERIMENT_ROOT,
        output_ok,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        dependencies=deps,
    ) == 0
    summary = json.loads((output_ok / "summary.json").read_text(encoding="utf-8"))
    assert summary["outcome"] == "ok"
    assert summary["canary_evidence_path"] == str(canary_root / "summary.json")
    assert "dashboard_poll" in events
