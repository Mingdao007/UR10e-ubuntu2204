"""Fresh-process live entry: real open/ARM/execute with endpoint doubles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from contact_yield_live import (
    RESIDENT_CANDIDATE_WAIT_TIMEOUT_S,
    _wait_for_resident_candidate,
    automatic_home_after_fault,
    main,
)
from contact_yield_live_contract import load_identity_contract
from contact_yield_live_fixtures import (
    YieldLiveRTDEDouble,
    write_admission_receipts,
    write_test_baseline,
)
from step5d_autotune_v4_r004.fake_rtde import FakeLiveKunweiTransport
from test_contact_benchmark_runtime import lib  # noqa: F401


PRESERVED = json.loads(
    (ROOT / "report/contact-six-qp-20260917/preserved-home.json").read_text()
)
PYTHON = Path(
    "/home/andy/.codex-worktrees/contact-yield-recovery-20260920/"
    "experiments/tase-contact-reproduction/.venv-contact-six/bin/python"
)


class Clock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def now(self) -> float:
        return self.value

    def sleep(self, duration: float) -> None:
        self.value += duration


@pytest.fixture(autouse=True)
def deterministic_endpoint_budget(monkeypatch):
    # Protocol tests run under SCHED_OTHER with virtual endpoint clocks.
    # Keep numeric behavior; hardware entry retains both production deadlines.
    import contact_yield_live_writer as entry
    original = entry.create_native_yield_runtime
    def create(**kwargs):
        runtime, binding = original(**kwargs)
        runtime.deadline_s = None
        runtime.controller.qp.qp.deadline_s = None
        return runtime, binding
    monkeypatch.setattr(entry, 'create_native_yield_runtime', create)


def _prepare(tmp_path: Path, *, program_running: bool = True, observed: float = 90.0):
    contract = load_identity_contract()
    write_admission_receipts(
        tmp_path,
        contract=contract,
        route_id="r006-yield-live",
        session_epoch=1,
        resident_session_id="r006-yield-live-session",
        home_q=contract.home_q,
        observed_controller=observed,
        observed_runtime=observed + 5.0,
        observed_home=observed,
        program_running=program_running,
    )
    write_test_baseline(tmp_path, contract, observed)
    return contract


def _argv(command: str, tmp_path: Path, **extra: str) -> list[str]:
    args = [
        command,
        "--method",
        extra.get("method", "SFC"),
        "--run-dir",
        str(tmp_path),
        "--route-id",
        "r006-yield-live",
        "--attempt-id",
        extra.get("attempt_id", "r006-yield-live-pilot"),
        "--authority-root",
        str(tmp_path / "authority"),
        "--qp-library",
        extra["qp_library"],
    ]
    if command == "pilot":
        args.extend(["--duration", extra.get("duration", "2")])
    return args


def test_fault_recovery_routes_to_existing_monitored_home_owner(tmp_path, monkeypatch):
    import run_contact_recovery

    calls = []

    def recover(source_run, host, video_url):
        calls.append((Path(source_run), host, video_url))
        return {
            "success": True,
            "state": "HOME_RECOVERED",
            "home_required": True,
            "home_attempted": True,
            "trial_stays_failed": True,
        }

    monkeypatch.setattr(run_contact_recovery, "recover_failed_contact_run", recover)
    result = automatic_home_after_fault(
        run_dir=tmp_path,
        controller_host="192.0.2.18",
        video_url="rtsp://127.0.0.1:8554/arm",
    )

    assert result["state"] == "HOME_RECOVERED"
    assert calls == [(tmp_path, "192.0.2.18", "rtsp://127.0.0.1:8554/arm")]


def _write_sealed_attempt_for_candidate_wait(run_dir: Path, *, sequence: int = 1) -> None:
    attempt_dir = run_dir / "attempts" / f"{sequence:04d}"
    attempt_dir.mkdir(parents=True)
    seal = {
        "attempt_sequence": sequence,
        "lifecycle": {"sealed": True, "home_verified": True},
    }
    result = {
        "sequence": sequence,
        "sealed_evidence": {"attempt_sequence": sequence},
    }
    (attempt_dir / "seal.json").write_text(json.dumps(seal), encoding="utf-8")
    (attempt_dir / "attempt-result.json").write_text(
        json.dumps(result), encoding="utf-8"
    )


def test_resident_candidate_wait_services_home_and_accepts_next_atomic_file(tmp_path):
    from types import SimpleNamespace

    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    candidate = json.loads(
        (ROOT / "config/tase_figure8_integral_0p1_rate400.json").read_text()
    )
    candidate["candidate_id"] = "tuner-2"
    candidate["index"] = 1
    candidate_path = candidate_dir / "candidate-0002.json"
    run_dir = tmp_path / "run"
    _write_sealed_attempt_for_candidate_wait(run_dir)
    attempt_dir = run_dir / "attempts" / "0001"
    sealed_mtime_ns = max(
        (attempt_dir / "seal.json").stat().st_mtime_ns,
        (attempt_dir / "attempt-result.json").stat().st_mtime_ns,
    )
    events = []

    class Session:
        def __init__(self):
            self.now = 0.0
            self.writer = SimpleNamespace(_service_mode=False)
            self.ticks = 0

        def mono_clock(self):
            return self.now

        def verify_ready_for_next(self, *, reason):
            events.append(("home", reason))
            return {"home_verified": True}

        def _set_service_context(self, *, attempt_sequence, stage):
            events.append(("context", attempt_sequence, stage))

        def _service_tick(self):
            self.ticks += 1
            self.now += 0.01
            events.append(("service", self.ticks))
            if self.ticks == 2:
                temporary = candidate_dir / "candidate-0002.json.tmp"
                temporary.write_text(json.dumps(candidate), encoding="utf-8")
                # Model atomic publication after the prior result and seal.
                os.utime(temporary, ns=(sealed_mtime_ns + 1_000_000, sealed_mtime_ns + 1_000_000))
                temporary.replace(candidate_path)

    session = Session()
    item = {
        "sequence": 1,
        "lifecycle": {
            "sealed": True,
            "home_verified": True,
            "ready_for_next": True,
        },
    }
    path, binding = _wait_for_resident_candidate(
        session,
        item=item,
        candidate_dir=candidate_dir,
        ordinal=2,
        duration="r013_60_rate400",
        run_dir=run_dir,
    )
    assert path == candidate_path.resolve()
    assert binding["candidate_id"] == "tuner-2"
    assert session.writer._service_mode is False
    assert events[0][0] == "context"
    assert events[1] == ("home", "candidate_2_wait_start")
    assert events[-1] == ("home", "candidate_2_ready")
    assert [event for event in events if event[0] == "service"] == [
        ("service", 1), ("service", 2)
    ]


def test_resident_candidate_wait_times_out_at_home_without_arm(tmp_path):
    from types import SimpleNamespace

    run_dir = tmp_path / "run"
    _write_sealed_attempt_for_candidate_wait(run_dir)
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    events = []

    class Session:
        def __init__(self):
            self.now = 0.0
            self.writer = SimpleNamespace(_service_mode=False)

        def mono_clock(self):
            return self.now

        def verify_ready_for_next(self, *, reason):
            events.append(("home", reason))
            return {"home_verified": True}

        def _set_service_context(self, *, attempt_sequence, stage):
            events.append(("context", attempt_sequence, stage))

        def _service_tick(self):
            self.now += 30.0
            events.append("service")

    session = Session()
    item = {
        "sequence": 1,
        "lifecycle": {
            "sealed": True,
            "home_verified": True,
            "ready_for_next": True,
        },
    }
    with pytest.raises(RuntimeError, match="timed out after 120s"):
        _wait_for_resident_candidate(
            session,
            item=item,
            candidate_dir=candidate_dir,
            ordinal=2,
            duration="r013_60_rate400",
            run_dir=run_dir,
        )
    assert session.now == RESIDENT_CANDIDATE_WAIT_TIMEOUT_S
    assert session.writer._service_mode is False
    assert events[1] == ("home", "candidate_2_wait_start")


def test_fault_recovery_without_host_is_explicitly_blocked(tmp_path):
    result = automatic_home_after_fault(
        run_dir=tmp_path, controller_host=None, video_url="unused"
    )

    assert result["state"] == "BLOCKED"
    assert result["home_required"] is True
    assert result["home_attempted"] is False
    assert "controller host" in result["home_blocked_reason"]


def test_fault_recovery_dispatch_exception_attempts_direct_home_fallback(
    tmp_path, monkeypatch
):
    import run_contact_recovery

    monkeypatch.setattr(
        run_contact_recovery,
        "recover_failed_contact_run",
        lambda *_: (_ for _ in ()).throw(RuntimeError("dispatcher crashed")),
    )
    calls = []

    def direct_fallback(source, output, host, packages, *, reason):
        calls.append((Path(source), Path(output), host, packages, str(reason)))
        return {
            "success": True,
            "state": "HOME_RECOVERED",
            "home_required": True,
            "home_attempted": True,
        }

    monkeypatch.setattr(
        run_contact_recovery, "_emergency_home_when_commandable", direct_fallback
    )
    result = automatic_home_after_fault(
        run_dir=tmp_path,
        controller_host="192.0.2.18",
        video_url="rtsp://127.0.0.1:8554/arm",
    )

    assert result["state"] == "HOME_RECOVERED"
    assert result["primary_recovery_error"] == "RuntimeError: dispatcher crashed"
    assert calls and calls[0][0] == tmp_path
    assert calls[0][2] == "192.0.2.18"


def test_supervisor_owned_writer_lock_defers_home_until_lock_release(tmp_path, monkeypatch):
    """A failed supervised attempt must not race its own recovery lock."""
    import contact_yield_live as entry
    from types import SimpleNamespace
    from contact_yield_live_writer import native_motion_profile

    args = entry._parse_args(
        [
            "qualify",
            "--method",
            "TASE_RNN_MATURE",
            "--run-dir",
            str(tmp_path),
            "--controller-host",
            "192.0.2.18",
            "--kunwei-host",
            "192.0.2.25",
            "--control-cpu",
            "1",
        ]
    )
    monkeypatch.setattr(
        entry,
        "load_live_entry_config",
        lambda: {"user_standing_live_authority": True},
    )
    monkeypatch.setattr(entry, "resolve_method", lambda _method: None)
    monkeypatch.setattr(
        entry,
        "load_identity_contract",
        lambda: SimpleNamespace(triplet={}),
    )
    monkeypatch.setattr(
        entry,
        "load_run_dir_receipts",
        lambda *args, **kwargs: (SimpleNamespace(session_epoch=1), None),
    )

    class Provider:
        class SolverProfile:
            def as_dict(self):
                return {"backend": "test"}

        solver_profile = SolverProfile()

        def snapshot(self):
            return {"state": "failed"}

        def restore(self, _state):
            return None

    class EarlyEnd:
        def arm(self, _sequence):
            return None

    class Writer:
        _r013_path_early_end_controller = EarlyEnd()

        def __init__(self):
            self._controller_transport = object()
            self.raw_observations = []
            self.robot_observations = []
            self.admission_robot_observations = []
            self.rejected_robot_observations = []
            self.command_observations = []

        def install_timing_scheduler_lease(self, _lease):
            return None

        def prepare_timing_scheduler_lease(self, **_kwargs):
            return None

    writer = Writer()

    class Mature:
        injection = SimpleNamespace(motion_profile=native_motion_profile())

        def __init__(self):
            self.writer = writer

        def open(self, **_kwargs):
            return None

        def dispatch(self, *_args):
            return None

        def arm(self, *_args):
            return None

        def run_60s(self, _attempt):
            raise RuntimeError("forced qualification fault")

        def close(self):
            return None

    class Runtime:
        def close(self):
            return None

    monkeypatch.setattr(
        entry,
        "build_native_yield_owner",
        lambda **_kwargs: (Mature(), Runtime(), Provider(), None),
    )
    monkeypatch.setattr(
        entry,
        "stop_and_confirm",
        lambda _writer: {"stopped": True},
    )
    recover_calls = []
    monkeypatch.setattr(
        entry,
        "automatic_home_after_fault",
        lambda **kwargs: recover_calls.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="forced qualification fault"):
        entry.run_live(args, kunwei_transport=object(), defer_recovery=True)

    assert recover_calls == []
    receipt = json.loads((tmp_path / "dispatch_receipt.json").read_text())
    assert receipt["automatic_home_recovery_deferred"]["state"] == (
        "DEFERRED_UNTIL_SINGLE_WRITER_RELEASE"
    )


def test_status_fresh_process_lists_native_identity_without_devices():
    interpreter = str(PYTHON if PYTHON.is_file() else sys.executable)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(ROOT / "tools"),
            env.get("PYTHONPATH", ""),
            "/opt/ros/humble/lib/python3.10/site-packages",
            "/opt/ros/humble/local/lib/python3.10/dist-packages",
        ]
    )
    env["AMENT_PREFIX_PATH"] = "/opt/ros/humble"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["OMP_NUM_THREADS"] = "1"
    env["OPENBLAS_NUM_THREADS"] = "1"
    completed = subprocess.run(
        [interpreter, str(ROOT / "tools" / "contact_yield_live.py"), "status"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["program"] == "step5d_contact_six_qp_v1"
    assert payload["readable_runtime_identity"] == [25, 618001]
    assert payload["physical_qualification"] is False
    assert payload["machine_evidence_fresh"] is False


def test_unavailable_method_does_not_claim_execution(tmp_path: Path, lib):
    _prepare(tmp_path)
    code = main(
        [
            "pilot",
            "--method",
            "TASE_RNN",
            "--duration",
            "2",
            "--run-dir",
            str(tmp_path),
            "--qp-library",
            str(lib),
        ]
    )
    assert code == 2


def test_missing_receipts_fail_before_open(tmp_path: Path, lib):
    code = main(
        _argv("pilot", tmp_path, qp_library=str(lib)),
        controller_transport=object(),
        kunwei_transport=object(),
        now_s=100.0,
    )
    assert code == 1


def test_wrong_protocol_fails_before_arm(tmp_path: Path, lib):
    contract = _prepare(tmp_path)
    clock = Clock(0.01)
    rtde = YieldLiveRTDEDouble(
        contract,
        home_pose=contract.home_pose,
        home_q=contract.home_q,
        wrong_protocol=True,
        clock=clock.now,
    )
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    code = main(
        _argv("qualify", tmp_path, qp_library=str(lib), attempt_id="r006-yield-live-qualify", method="SFC"),
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
        now_s=100.0,
    )
    assert code == 1
    assert rtde.opened is False or "arm" not in "".join(rtde.events or [])


def test_stale_receipt_fails_before_arm(tmp_path: Path, lib):
    contract = _prepare(tmp_path, observed=0.0)
    clock = Clock(0.01)
    rtde = YieldLiveRTDEDouble(
        contract, home_pose=contract.home_pose, home_q=contract.home_q, clock=clock.now
    )
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    code = main(
        _argv("qualify", tmp_path, qp_library=str(lib), attempt_id="r006-yield-live-qualify", method="SFC"),
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 400.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
        now_s=400.0,
    )
    assert code == 1


@pytest.mark.parametrize("method", ["SFC", "TASE_RNN_MATURE"])
def test_endpoint_qualify_uses_real_open_arm_execute(tmp_path: Path, lib, method):
    contract = _prepare(tmp_path)
    clock = Clock(0.01)
    events: list[str] = []
    rtde = YieldLiveRTDEDouble(
        contract,
        home_pose=contract.home_pose,
        home_q=contract.home_q,
        events=events,
        # The approved Figure-eight Home is an exact rotation-vector
        # contract; negating this non-pi vector is a different SO(3) pose.
        flip_rotvec=False,
        clock=clock.now,
    )
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    holder: list = []
    code = main(
        _argv("qualify", tmp_path, qp_library=str(lib), attempt_id="r006-yield-live-qualify", method=method),
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
        now_s=100.0,
        owner_holder=holder,
    )
    assert code == 0, "qualify lifecycle should complete with endpoint doubles"
    assert "rtde.open" in events
    assert rtde.closed
    assert kunwei.closed


def test_pilot_diagnostic_is_not_full_period_acceptance(tmp_path: Path, lib):
    contract = _prepare(tmp_path)
    clock = Clock(0.01)
    rtde = YieldLiveRTDEDouble(
        contract, home_pose=contract.home_pose, home_q=contract.home_q, clock=clock.now
    )
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now, wrench_n_nm=(0.0, 0.0, -5.0, 0.0, 0.0, 0.0))
    code = main(
        _argv("pilot", tmp_path, qp_library=str(lib), duration="2", method="SFC"),
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 100.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
        now_s=100.0,
    )
    receipt = json.loads((tmp_path / 'dispatch_receipt.json').read_text())
    assert receipt['formally_qualified'] is False
    assert receipt['full_cycle_acceptance'] is False
    assert receipt['stop']['stopped'] is True
    assert len([row for row in receipt['attempts'] if row['phase'] == 'pilot']) == 1
    assert receipt['continuous_contact_path'] is True
    assert rtde.closed
    assert rtde._early_end_sequence == 1, receipt.get('error')
    assert code == 0, receipt.get('error')


def test_second_writer_is_rejected(tmp_path: Path, lib):
    from step5d_autotune_v4_r006.live_adapter import _R006_INJECTION_LOCK
    contract = _prepare(tmp_path)
    clock = Clock(.01)
    rtde = YieldLiveRTDEDouble(contract,home_pose=contract.home_pose,
        home_q=contract.home_q,clock=clock.now)
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    assert _R006_INJECTION_LOCK.acquire(blocking=False)
    try:
        code = main(_argv("qualify",tmp_path,qp_library=str(lib)),
            controller_transport=rtde,kunwei_transport=kunwei,
            wall_clock=lambda:100.,mono_clock=clock.now,sleep=clock.sleep,now_s=100.)
        assert code == 1
        assert not rtde.opened and not rtde.sent_packets
        assert _R006_INJECTION_LOCK.locked()
    finally:
        _R006_INJECTION_LOCK.release()


def test_mature_full_figure8_with_trajectory_endpoint_double(tmp_path, lib):
    # Exercises real RNN + writer + consumed-reference evidence; synthetic
    # measured path is exact by construction, not a robot/contact simulation.
    import numpy as np
    from contact_yield_protocol import Task, PERIOD_S
    from contact_yield_task_frame import figure8_task_basis
    contract = _prepare(tmp_path)
    clock = Clock(.01)
    class FollowingEndpoint(YieldLiveRTDEDouble):
        def _mapping(self):
            row = super()._mapping()
            if self._state == 25:
                if getattr(self, 'path_origin', None) is None:
                    self.path_origin = clock.now()
                elapsed = clock.now()-self.path_origin
                ref = Task().entry_reference(elapsed) if elapsed < 1. else Task().reference((elapsed-1.) % PERIOD_S)
                xyz = np.asarray(contract.home_pose[:3])+figure8_task_basis()@np.asarray(ref['position_m'])
                velocity = figure8_task_basis()@np.asarray(ref['velocity_m_s'])
                row['actual_TCP_pose'][:3] = xyz.tolist()
                row['actual_TCP_speed'] = [*velocity, 0., 0., 0.]
            else:
                self.path_origin = None
            return row
    rtde = FollowingEndpoint(contract, home_pose=contract.home_pose,
                             home_q=contract.home_q, clock=clock.now, flip_rotvec=False)
    sensor = FakeLiveKunweiTransport(observed_clock=clock.now,
                                    wrench_n_nm=(0.,0.,-5.,0.,0.,0.))
    code = main(_argv('pilot',tmp_path,qp_library=str(lib),method='TASE_RNN_MATURE',duration='full'),
                controller_transport=rtde,kunwei_transport=sensor,
                wall_clock=lambda:100.,mono_clock=clock.now,sleep=clock.sleep,now_s=100.)
    receipt = json.loads((tmp_path/'dispatch_receipt.json').read_text())
    assert code == 0, receipt.get('error')
    assert len(receipt['attempts']) == 1
    assert receipt['attempts'][0]['phase'] == 'pilot'
    assert receipt['continuous_contact_path'] is True
    assert receipt['stop']['stopped'] is True
