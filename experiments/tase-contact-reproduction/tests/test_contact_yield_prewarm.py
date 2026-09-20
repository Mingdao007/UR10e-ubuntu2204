"""Transport-free warmup of the exact live provider/runtime. No devices."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest
from contact_yield_live_fixtures import (
    YieldLiveRTDEDouble,
    write_admission_receipts,
    write_test_baseline,
)
from contact_yield_live_writer import (
    PREWARM_PURPOSE,
    _prewarm_native_provider,
    build_native_yield_owner,
    load_run_dir_receipts,
)
from contact_yield_live_contract import load_identity_contract
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.fake_rtde import FakeLiveKunweiTransport
from yield_native_route import (
    PRODUCTION_QP_DEADLINE_S,
    PRODUCTION_RUNTIME_DEADLINE_S,
    create_native_yield_provider,
    create_native_yield_runtime,
)

ROOT = Path(__file__).resolve().parents[1]
MAIN = Path(
    "/home/andy/.codex-worktrees/contact-yield-recovery-20260920/"
    "experiments/tase-contact-reproduction"
)
PRESERVED = json.loads(
    (ROOT / "report/contact-six-qp-20260917/preserved-home.json").read_text()
)


def existing_qp_library() -> Path:
    for path in (
        ROOT / "build/contact-qp/libcontact_qp.so",
        MAIN / "build/contact-qp/libcontact_qp.so",
    ):
        if path.is_file() and not path.is_symlink():
            return path
    pytest.skip("native QP library is not present")


def existing_laws_root() -> Path:
    for path in (
        MAIN / "build/contact-six-laws",
        Path("/tmp/ur10e_contact_laws"),
    ):
        if path.is_dir() and any(path.glob("contact-laws-*/libcontact_laws.so")):
            return path
    pytest.skip("native contact-law library is not present")


def native_provider(method: str):
    pose = PRESERVED["home_pose"]
    q = PRESERVED["home_q"]
    rotation = rotvec_to_matrix(np.array(pose[3:], dtype=float))
    runtime, binding = create_native_yield_runtime(
        method=method,
        qp_library=existing_qp_library(),
        build_root=existing_laws_root(),
        anchor_m=pose[:3],
        task_basis=rotation,
        approach_inward_base=rotation[:, 2],
        home_observations={"home_pose": pose, "home_q": q},
    )
    provider = create_native_yield_provider(runtime=runtime, binding=binding)
    return provider, runtime, pose, q


def capture(provider):
    runtime = provider.runtime
    return {
        "provider": copy.deepcopy(provider.snapshot()),
        "law": runtime.controller.law.snapshot(),
        "qp": copy.deepcopy(runtime.controller.qp.snapshot()),
        "estimator": copy.deepcopy(runtime.controller.estimator.snapshot()),
        "freshness": copy.deepcopy(runtime.freshness.as_dict()),
        "deadlines": (runtime.deadline_s, runtime.controller.qp.qp.deadline_s),
        "clocks": (
            runtime.last_sample_s,
            runtime.last_controller_timestamp,
            runtime.last_sensor_timestamp,
            runtime.paused_s,
            runtime.phase,
            runtime.last_entry_time,
            runtime.controller.last_time_s,
            runtime.controller.last_path_time_s,
        ),
        "history": copy.deepcopy(provider.command_history),
        "readiness": (
            provider.lifecycle_observer.filtered_normal_n,
            None
            if provider.lifecycle_observer.last_log is None
            else (
                provider.lifecycle_observer.last_log.filtered_normal_n,
                provider.lifecycle_observer.last_log.actual_dt_s,
            ),
        ),
        "last_result": copy.deepcopy(provider.last_result),
    }


def assert_equivalent(provider, before):
    after = capture(provider)
    assert after["provider"] == before["provider"]
    assert after["law"] == before["law"]
    assert after["qp"] == before["qp"]
    assert after["estimator"] == before["estimator"]
    assert after["freshness"] == before["freshness"]
    assert after["deadlines"] == before["deadlines"]
    assert after["clocks"] == before["clocks"]
    assert after["history"] == before["history"]
    assert after["readiness"] == before["readiness"]
    assert after["last_result"] == before["last_result"]


@pytest.mark.parametrize("method", ["SFC", "MSFC"])
def test_prewarm_restores_deep_state_and_records_command_timing(method):
    provider, runtime, pose, q = native_provider(method)
    with runtime:
        assert runtime.deadline_s == PRODUCTION_RUNTIME_DEADLINE_S
        assert runtime.controller.qp.qp.deadline_s == PRODUCTION_QP_DEADLINE_S
        before = capture(provider)
        record = _prewarm_native_provider(provider, pose=pose, q=q)
        assert_equivalent(provider, before)
        assert record["purpose"] == PREWARM_PURPOSE
        assert record is provider.prewarm_record
        assert record["deadline_restored"] is True
        assert record["command_count"] == 4
        assert record["state_changed_during"] is True
        assert record["first_command_s"] > 1e-6
        assert record["last_command_s"] > 1e-6
        assert len(record["command_times_s"]) == 4
        assert runtime.deadline_s == PRODUCTION_RUNTIME_DEADLINE_S
        assert runtime.controller.qp.qp.deadline_s == PRODUCTION_QP_DEADLINE_S
        # Wall timings are diagnostic observations, not deterministic test gates.


def test_prewarm_restores_deadlines_on_failure():
    provider, runtime, pose, q = native_provider("MSFC")
    with runtime:
        before = capture(provider)
        original = provider.command
        calls = {"n": 0}

        def boom(**kwargs):
            calls["n"] += 1
            assert runtime.deadline_s is None
            assert runtime.controller.qp.qp.deadline_s is None
            if calls["n"] >= 2:
                raise RuntimeError("injected prewarm failure")
            return original(**kwargs)

        provider.command = boom
        with pytest.raises(RuntimeError, match="injected prewarm failure"):
            _prewarm_native_provider(provider, pose=pose, q=q)
        assert_equivalent(provider, before)
        assert runtime.deadline_s == PRODUCTION_RUNTIME_DEADLINE_S
        assert runtime.controller.qp.qp.deadline_s == PRODUCTION_QP_DEADLINE_S
        assert provider.prewarm_record["deadline_restored"] is True
        assert provider.prewarm_record["command_count"] == 1


def test_owner_prewarms_actual_instance_before_writer_without_opening_endpoints(
    tmp_path, monkeypatch
):
    import contact_yield_live_writer as writer_mod

    original_runtime = writer_mod.create_native_yield_runtime

    def create(**kwargs):
        kwargs.setdefault("build_root", existing_laws_root())
        return original_runtime(**kwargs)

    monkeypatch.setattr(writer_mod, "create_native_yield_runtime", create)
    contract = load_identity_contract()
    write_admission_receipts(
        tmp_path,
        contract=contract,
        route_id="r006-yield-live",
        session_epoch=1,
        resident_session_id="r006-yield-live-session",
        home_q=PRESERVED["home_q"],
        observed_controller=90.0,
        observed_runtime=95.0,
        observed_home=90.0,
    )
    write_test_baseline(tmp_path, contract, 90.0)
    prerequisites, home_binding = load_run_dir_receipts(
        tmp_path,
        contract=contract,
        route_id="r006-yield-live",
        attempt_id="r006-yield-live-prewarm",
        now_s=100.0,
    )
    events: list[str] = []
    rtde = YieldLiveRTDEDouble(
        contract, home_pose=contract.home_pose, home_q=PRESERVED["home_q"], events=events
    )
    kunwei = FakeLiveKunweiTransport(events=events)
    order: list[str] = []
    original_prewarm = writer_mod._prewarm_native_provider

    def tracked_prewarm(provider, **kwargs):
        order.append("prewarm")
        assert getattr(provider, "prewarm_record", None) is None
        return original_prewarm(provider, **kwargs)

    monkeypatch.setattr(writer_mod, "_prewarm_native_provider", tracked_prewarm)

    class TrackingWriter(writer_mod.NativeYieldLiveWriter):
        def __init__(self, *args, **kwargs):
            order.append("writer")
            super().__init__(*args, **kwargs)

        def open(self, *args, **kwargs):
            raise AssertionError("owner construction opened an endpoint")

        def _send_packet(self, *args, **kwargs):
            raise AssertionError("prewarm published a writer packet")

    monkeypatch.setattr(writer_mod, "NativeYieldLiveWriter", TrackingWriter)
    mature, runtime, provider, request = build_native_yield_owner(
        method="SFC",
        duration=None,
        command="qualify",
        prerequisites=prerequisites,
        home_binding=home_binding,
        qp_library=existing_qp_library(),
        authority_root=tmp_path / "authority",
        route_id="r006-yield-live",
        attempt_id="r006-yield-live-prewarm",
        controller_transport=rtde,
        kunwei_transport=kunwei,
    )
    try:
        assert request is None
        assert order == ["prewarm", "writer"]
        assert events == []
        assert rtde.opened is False
        assert kunwei.opened is False
        assert mature.writer.command_observations == []
        assert mature.writer.raw_observations == []
        assert mature.writer.robot_observations == []
        assert getattr(provider, "_command_timing_installed", False) is False
        record = provider.prewarm_record
        assert record["purpose"] == PREWARM_PURPOSE
        assert record["command_count"] == 4
        assert record["deadline_restored"] is True
        assert runtime.deadline_s == PRODUCTION_RUNTIME_DEADLINE_S
        assert runtime.controller.qp.qp.deadline_s == PRODUCTION_QP_DEADLINE_S
        assert runtime.last_sample_s is None
        assert runtime.phase is None
        assert provider.last_result is None
        assert provider.command_history is None
    finally:
        runtime.close()


def test_supervisor_prewarm_does_not_construct_a_throwaway_runtime(monkeypatch):
    calls: list[str] = []

    def qp(library):
        calls.append(f"qp:{Path(library).name}")

    monkeypatch.setattr("contact_yield_live_writer._prewarm_qp", qp)

    def forbidden(**kwargs):
        raise AssertionError("supervisor prewarm constructed a throwaway runtime")

    monkeypatch.setattr("yield_native_route.create_native_yield_runtime", forbidden)
    from contact_yield_supervisor import _prewarm

    _prewarm("SFC")
    assert calls == ["qp:libcontact_qp.so"]


def test_command_timing_preserves_real_clocks_and_original_failure():
    from types import SimpleNamespace
    from contact_yield_live_writer import _install_command_timing
    runtime = SimpleNamespace(step=lambda **kwargs: 'original-result')
    provider = SimpleNamespace(runtime=runtime)
    def command(**kwargs):
        assert runtime.step() == 'original-result'
        raise ValueError('original failure')
    provider.command = command
    _install_command_timing(provider)
    with pytest.raises(ValueError, match='original failure'):
        provider.command(sensor=SimpleNamespace(observed_at_s=10.),
                         output=SimpleNamespace(received_monotonic_s=11.),
                         monotonic_s=12.,actual_dt_s=.003)
    row = provider.command_timeline[0]
    assert row['sensor_received_s'] == 10.
    assert row['robot_received_s'] == 11.
    assert row['host_use_s'] == 12.
    assert row['actual_dt_s'] == .003
    assert row['provider_enter_s'] <= row['runtime_enter_s'] <= row['runtime_exit_s'] <= row['provider_exit_s']
    assert row['error'] == 'ValueError'
