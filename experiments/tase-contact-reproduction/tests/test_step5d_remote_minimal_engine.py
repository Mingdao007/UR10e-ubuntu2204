from __future__ import annotations

import copy
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

import pytest

EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
TOOLS_ROOT = str(EXPERIMENT_ROOT / "tools")
if TOOLS_ROOT not in sys.path:
    sys.path.insert(0, TOOLS_ROOT)

from step5d_remote_control import core, engine, primitives, runtime

CONFIG_PATH = "config/step5d_remote/r012.yaml"


def _load_cfg():
    return copy.deepcopy(core.load_r012_config(EXPERIMENT_ROOT, CONFIG_PATH))


def _identity_sample(
    *,
    tcp_pose=(0.0, 0.0, 0.020, 0.0, 0.0, 0.0),
    raw_wrench=(0.0, 0.0, 10.0, 0.0, 0.0, 0.0),
):
    return engine.KinematicSample(
        q=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        qd=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        tcp_pose=tcp_pose,
        tcp_twist=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        wrench_tcp=(raw_wrench[0], raw_wrench[1], raw_wrench[2], raw_wrench[3], raw_wrench[4], raw_wrench[5]),
        jacobian=tuple(tuple(float(v) for v in row) for row in np.eye(6).tolist()),
        q_min=(-3.0,) * 6,
        q_max=(3.0,) * 6,
        timestamp_s=0.0,
    )


def test_tcp_point_kinematics_strict_6x6_and_bottom_rows_preserved():
    tool0_rotation = (
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    )
    tool0_translation = (0.0, 0.0, 0.0)
    tool0_jacobian = [
        [0.11, 0.12, 0.13, 0.14, 0.15, 0.16],
        [0.21, 0.22, 0.23, 0.24, 0.25, 0.26],
        [0.31, 0.32, 0.33, 0.34, 0.35, 0.36],
        [0.41, 0.42, 0.43, 0.44, 0.45, 0.46],
        [0.51, 0.52, 0.53, 0.54, 0.55, 0.56],
        [0.61, 0.62, 0.63, 0.64, 0.65, 0.66],
    ]
    positive_offset = (0.0, 0.0, 0.122)
    _, jacobian = primitives.tcp_point_kinematics(
        tool0_rotation=tool0_rotation,
        tool0_translation=tool0_translation,
        tool0_jacobian=tool0_jacobian,
        positive_offset=positive_offset,
    )
    jacobian = np.asarray(jacobian)
    assert jacobian.shape == (6, 6)
    assert np.allclose(jacobian[3:], np.asarray(tool0_jacobian)[3:])
    jv = np.asarray(tool0_jacobian)[:3]
    jw = np.asarray(tool0_jacobian)[3:]
    offset = np.asarray(positive_offset)
    skew = np.array(
        [
            [0.0, -offset[2], offset[1]],
            [offset[2], 0.0, -offset[0]],
            [-offset[1], offset[0], 0.0],
        ]
    )
    expected_top = jv - skew @ jw
    assert np.allclose(jacobian[:3], expected_top)


def test_load_r012_and_unknown_key_reject():
    cfg = _load_cfg()
    cfg["bogus"] = "bad"
    with pytest.raises(Exception, match="extra=\\['bogus'\\]"):
        core.expect_exact_keys(cfg, core._R012_SCHEMA)


def test_command_builder_tuple_api():
    cfg = _load_cfg()
    launch_cmd = primitives.build_driver_command(cfg)
    spawner_cmd = primitives.build_spawner_command(cfg, "/tmp/step5d_watchdog.yaml")
    assert isinstance(launch_cmd, tuple)
    assert isinstance(spawner_cmd, tuple)
    assert launch_cmd[:4] == (
        "ros2",
        "launch",
        "ur_robot_driver",
        "ur_control.launch.py",
    )
    calibration_arg = next(part for part in launch_cmd if part.startswith("kinematics_params_file:="))
    assert Path(calibration_arg.split(":=", 1)[1]).is_absolute()
    assert spawner_cmd == (
        "ros2",
        "run",
        "controller_manager",
        "spawner",
        "step5d_watchdog_controller",
        "-c",
        "/controller_manager",
        "-p",
        "/tmp/step5d_watchdog.yaml",
        "-t",
        "ur10e_step5d_remote_watchdog/WatchdogController",
        "--inactive",
    )


def test_guard_and_unknown_thresholds_from_real_r012():
    cfg = _load_cfg()
    altered = copy.deepcopy(cfg)
    altered["force"]["hard_normal_force_n"] = 1.0
    guard = primitives.guard_wrench(altered, (0.0, 0.0, 6.0, 0.0, 0.0, 0.0))
    assert guard.ok is False
    assert "raw_fz_hard_limit" in guard.reasons


def test_direct_control_kernel_reset_calls_solver_reset_state():
    class FakeSolver:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.reset_calls = 0

        def reset_state(self) -> None:
            self.reset_calls += 1

    class _NoOpPolicy:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class _NoOpEnvelope:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def evaluate(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("unexpected envelope usage in reset test")

    captured: dict[str, FakeSolver] = {}

    def fake_solver_factory(_cfg: object) -> FakeSolver:
        solver = FakeSolver()
        captured["solver"] = solver
        return solver

    kernel = engine.DirectControlKernel.create(
        _load_cfg(),
        solver_factory=fake_solver_factory,
        policy_factory=_NoOpPolicy,
        envelope_factory=_NoOpEnvelope,
    )
    kernel.reset()
    assert captured["solver"].reset_calls == 1
    assert kernel.warmed is False
    assert kernel.last_qdot == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_direct_control_kernel_numpy_backend_and_compute_path():
    cfg = _load_cfg()
    kernel = engine.DirectControlKernel.create(cfg, backend_override="numpy")
    assert kernel.backend == "numpy"
    assert str(cfg["rates"]["rnn_backend"]) == "cupy"
    sample = _identity_sample()
    kernel.warm_start(sample, desired_twist=(0.0, 0.0, -0.003, 0.0, 0.0, 0.0))
    qdot = kernel.compute(
        sample,
        desired_twist=(0.0, 0.0, -0.003, 0.0, 0.0, 0.0),
        reaction_normal=(0.0, 0.0, 1.0),
        approach_normal=(0.0, 0.0, -1.0),
        dt_s=0.002,
    )
    assert len(qdot) == 6


def test_track_twist_uses_cumulative_track_elapsed_and_unrotated_tcp_force(monkeypatch):
    cfg = _load_cfg()
    cfg["search"]["timeout_s"] = 0.5
    cfg["run_duration_s"] = 0.05
    cfg["preload"]["hold_s"] = 0.0

    observed_forces: list[tuple[float, float, float]] = []

    def fake_outer_loop(_outer_cfg, _outer_state, inputs, include_diagnostics: bool = False):  # type: ignore[override]
        observed_forces.append(tuple(float(v) for v in inputs.force_tcp_n))
        return SimpleNamespace(
            next_state=_outer_state,
            xdot_c=(0.001, 0.0, -0.002, 0.0, 0.0, 0.0),
        )

    monkeypatch.setattr(engine, "compute_step5d_outer_loop", fake_outer_loop)

    class _ProbeKernel(_FakeKernel):
        def __init__(self) -> None:
            super().__init__()
            self.path_times: list[float] = []

        def compute(
            self,
            sample: engine.KinematicSample,
            desired_twist: engine.Vector6,
            reaction_normal: engine.Vector3,
            approach_normal: engine.Vector3,
            dt_s: float,
            normal_motion_policy: str = "frame_contract_only",
            path_time_s: float = 0.0,
            raw_desired_twist: engine.Vector6 | None = None,
        ) -> engine.Vector6:
            self.path_times.append(path_time_s)
            return super().compute(
                sample,
                desired_twist=desired_twist,
                reaction_normal=reaction_normal,
                approach_normal=approach_normal,
                dt_s=dt_s,
                normal_motion_policy=normal_motion_policy,
                path_time_s=path_time_s,
                raw_desired_twist=raw_desired_twist,
            )

    probe_kernel = _ProbeKernel()
    program = engine.ContactProgram.create(cfg, kernel=probe_kernel)

    sample = _identity_sample(
        tcp_pose=(0.0, 0.0, 0.020, 0.12, 0.0, -0.08),
        raw_wrench=(1.7, -2.3, 8.0, 0.0, 0.0, 0.0),
    )

    last = program.step(sample, dt_s=0.01)
    for _ in range(6):
        if last.phase == engine.RemotePhase.TRACK:
            break
        last = program.step(sample, dt_s=0.01)

    assert last.phase == engine.RemotePhase.TRACK
    assert observed_forces
    assert observed_forces[0] == sample.wrench_tcp[:3]
    assert probe_kernel.path_times
    assert probe_kernel.path_times[0] == pytest.approx(program.state_machine.state.track_elapsed_s)


def test_contact_program_outer_terms_match_expected():
    cfg = _load_cfg()
    terms = engine.compute_outer_force_terms(cfg["force"])
    assert terms["Md"] == 1000.0
    assert terms["kf"] == 0.01
    assert terms["Bd"] == 7000.0
    assert cfg["force"]["orientation_ko"] == 0.4


def test_canary_evidence_parser_rejects_bad_and_accepts_exact(tmp_path):
    cfg = _load_cfg()
    tmp = tmp_path
    summary_root = tmp / "canary"
    good = summary_root / "run-001"
    bad = summary_root / "run-002"
    params_sha = "test-sha-20260723"
    good.mkdir(parents=True, exist_ok=True)
    (good / "summary.json").write_text(
        (
            "{"
            f'"ok": true, '
            f'"params_sha256": "{params_sha}", '
            '"boot_id": "abc", '
            '"created_at_epoch_s": 1000.0, '
            '"stages": {"zero":"passed","free_space":"passed","guarded_contact":"passed","post_canary_safe_pose":"passed"}'
            "}"
        ),
        encoding="utf-8",
    )
    bad.mkdir(parents=True, exist_ok=True)
    assert runtime.latest_canary_summary(tmp) == good / "summary.json"
    runtime.validate_canary_evidence(
        cfg,
        good / "summary.json",
        params_sha256=params_sha,
        current_boot_id="abc",
        now_epoch_s=1200.0,
    )
    (bad / "summary.json").write_text(
        (
            "{"
            f'"ok": false, '
            f'"params_sha256": "{params_sha}", '
            '"boot_id": "abc", '
            '"created_at_epoch_s": 1000.0, '
            '"stages": {"zero":"passed","free_space":"passed","guarded_contact":"passed","post_canary_safe_pose":"passed"}'
            "}"
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not ok"):
        runtime.validate_canary_evidence(
            cfg,
            bad / "summary.json",
            params_sha256=params_sha,
            current_boot_id="abc",
            now_epoch_s=1200.0,
        )


class _FakeKernel:
    def __init__(self) -> None:
        self.reset_calls = 0
        self.warm_start_calls = 0

    def reset(self) -> None:
        self.reset_calls += 1

    def warm_start(self, sample: engine.KinematicSample, *, desired_twist: engine.Vector6) -> None:
        self.warm_start_calls += 1

    def compute(
        self,
        sample: engine.KinematicSample,
        desired_twist: engine.Vector6,
        reaction_normal: engine.Vector3,
        approach_normal: engine.Vector3,
        dt_s: float,
        normal_motion_policy: str = "frame_contract_only",
        path_time_s: float = 0.0,
        raw_desired_twist: engine.Vector6 | None = None,
    ) -> engine.Vector6:
        return desired_twist


def _build_fake_program():
    cfg = _load_cfg()
    cfg["search"]["timeout_s"] = 0.01
    cfg["run_duration_s"] = 0.03
    cfg["preload"]["hold_s"] = 0.0
    fake = _FakeKernel()
    program = engine.ContactProgram.create(cfg, kernel=fake)
    return program, fake, cfg


def test_fake_kernel_phase_sign_and_retract_stop():
    program, fake, cfg = _build_fake_program()
    reaction = np.asarray(cfg["frame"]["reaction_normal_b"], dtype=float)
    sample = _identity_sample(raw_wrench=(0.0, 0.0, 10.0, 0.0, 0.0, 0.0))

    out = program.step(sample, dt_s=0.005)
    assert out.phase.name == "SEARCH"
    assert np.dot(out.desired_twist[:3], reaction) < 0.0

    out = program.step(sample, dt_s=0.005)
    assert out.phase.name == "TRACK"
    assert fake.reset_calls >= 2

    for _ in range(3):
        out = program.step(sample, dt_s=0.01)
        assert out.phase in {engine.RemotePhase.TRACK, engine.RemotePhase.RETRACT}
        if out.phase == engine.RemotePhase.RETRACT:
            break
    assert out.phase == engine.RemotePhase.RETRACT
    assert np.dot(out.desired_twist[:3], reaction) > 0.0

    retract_distance = 0.0105
    progressed_pose = tuple(
        float(sample.tcp_pose[i] + reaction[i] * retract_distance) for i in range(3)
    ) + sample.tcp_pose[3:]
    progressed = _identity_sample(
        tcp_pose=progressed_pose,
        raw_wrench=(0.0, 0.0, 10.0, 0.0, 0.0, 0.0),
    )
    out = program.step(progressed, dt_s=0.02)
    if out.phase == engine.RemotePhase.RETRACT:
        out = program.step(progressed, dt_s=0.02)
    assert out.phase == engine.RemotePhase.STOP
    assert out.qdot == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    assert out.faulted is False
    assert out.desired_twist == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
