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

from contact_yield_live import main
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
        home_q=PRESERVED["home_q"],
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
    assert payload["readable_runtime_identity"] == [20, 618001]
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
        home_q=PRESERVED["home_q"],
        wrong_protocol=True,
        clock=clock.now,
    )
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    code = main(
        _argv("qualify", tmp_path, qp_library=str(lib), attempt_id="r006-yield-live-qualify"),
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
        contract, home_pose=contract.home_pose, home_q=PRESERVED["home_q"], clock=clock.now
    )
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    code = main(
        _argv("qualify", tmp_path, qp_library=str(lib), attempt_id="r006-yield-live-qualify"),
        controller_transport=rtde,
        kunwei_transport=kunwei,
        wall_clock=lambda: 400.0,
        mono_clock=clock.now,
        sleep=clock.sleep,
        now_s=400.0,
    )
    assert code == 1


def test_endpoint_qualify_uses_real_open_arm_execute(tmp_path: Path, lib):
    contract = _prepare(tmp_path)
    clock = Clock(0.01)
    events: list[str] = []
    rtde = YieldLiveRTDEDouble(
        contract,
        home_pose=contract.home_pose,
        home_q=PRESERVED["home_q"],
        events=events,
        flip_rotvec=True,
        clock=clock.now,
    )
    kunwei = FakeLiveKunweiTransport(observed_clock=clock.now)
    holder: list = []
    code = main(
        _argv("qualify", tmp_path, qp_library=str(lib), attempt_id="r006-yield-live-qualify"),
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
        contract, home_pose=contract.home_pose, home_q=PRESERVED["home_q"], clock=clock.now
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
    assert len([row for row in receipt['attempts'] if row['phase'] == 'qualify']) == 3
    assert rtde.closed
    assert rtde._early_end_sequence == 4, receipt.get('error')
    assert code == 0, receipt.get('error')


def test_second_writer_is_rejected(tmp_path: Path, lib):
    from step5d_autotune_v4_r006.live_adapter import _R006_INJECTION_LOCK
    contract = _prepare(tmp_path)
    clock = Clock(.01)
    rtde = YieldLiveRTDEDouble(contract,home_pose=contract.home_pose,
        home_q=PRESERVED["home_q"],clock=clock.now)
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
