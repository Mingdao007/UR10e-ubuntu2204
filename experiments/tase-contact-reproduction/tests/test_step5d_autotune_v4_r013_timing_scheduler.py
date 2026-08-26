from __future__ import annotations

import json
import os

import pytest

import step5d_autotune_v4_r013.timing_scheduler as scheduler
from run_v4_stage_live import _load_coalescing_timing_contract


def _row(tid: int, *, policy: str, priority: int, affinity: tuple[int, ...], control: bool) -> dict[str, object]:
    value = {
        "tid": tid,
        "policy": policy,
        "policy_value": os.SCHED_FIFO if policy == "SCHED_FIFO" else os.SCHED_OTHER,
        "priority": priority,
        "affinity": list(affinity),
        "is_control_thread": control,
    }
    return value


def test_quota_safe_profile_keeps_real_thread_other() -> None:
    lease = scheduler.FormalTimingSchedulerLeaseV2(scheduler.QUOTA_SAFE_OTHER_PROFILE)
    entered = lease.enter()
    assert entered["profile"]["profile_id"] == scheduler.QUOTA_SAFE_OTHER_PROFILE
    assert entered["active_control"]["policy"] == "SCHED_OTHER"
    receipt = lease.exit()
    assert receipt["restore_verified"] is True
    assert receipt["kernel_rt_bandwidth_unchanged"] is True
    assert receipt["helper_non_other_thread_count_after_restore"] == 0


def test_late_fifo_promotes_only_control_and_restores(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {
        "policy": "SCHED_OTHER",
        "priority": 0,
        "affinity": (0, 1),
    }

    def metadata(_tid: int) -> dict[str, object]:
        return _row(
            10,
            policy=str(state["policy"]),
            priority=int(state["priority"]),
            affinity=tuple(state["affinity"]),
            control=True,
        )

    def snapshot(_tid: int) -> dict[str, object]:
        control = metadata(10)
        helper = _row(11, policy="SCHED_OTHER", priority=0, affinity=(0, 1), control=False)
        return {
            "current_tid": 10,
            "thread_count": 2,
            "policy_counts": {"SCHED_OTHER/0": 2},
            "threads": [control, helper],
        }

    monkeypatch.setattr(scheduler, "_scheduler_metadata", metadata)
    monkeypatch.setattr(scheduler, "_thread_scheduler_snapshot", snapshot)
    monkeypatch.setattr(scheduler, "_rt_bandwidth_metadata", lambda: {"sched_rt_period_us": 1_000_000, "sched_rt_runtime_us": 950_000})
    monkeypatch.setattr(scheduler.threading, "get_native_id", lambda: 10)

    def set_affinity(_tid: int, value: set[int]) -> None:
        state["affinity"] = tuple(sorted(value))

    def set_scheduler(_tid: int, policy: int, param: object) -> None:
        state["policy"] = "SCHED_FIFO" if policy == os.SCHED_FIFO else "SCHED_OTHER"
        state["priority"] = int(getattr(param, "sched_priority", 0))

    monkeypatch.setattr(scheduler.os, "sched_setaffinity", set_affinity)
    monkeypatch.setattr(scheduler.os, "sched_setscheduler", set_scheduler)

    lease = scheduler.FormalTimingSchedulerLeaseV2()
    entered = lease.enter()
    assert entered["active_control"]["policy"] == "SCHED_FIFO"
    assert entered["helper_non_other_thread_count"] == 0
    restored = lease.exit()
    assert restored["restore_verified"] is True
    assert state == {"policy": "SCHED_OTHER", "priority": 0, "affinity": (0, 1)}


def test_inherited_fifo_helper_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scheduler,
        "_scheduler_metadata",
        lambda _tid: _row(10, policy="SCHED_OTHER", priority=0, affinity=(0,), control=True),
    )
    monkeypatch.setattr(
        scheduler,
        "_thread_scheduler_snapshot",
        lambda _tid: {
            "current_tid": 10,
            "threads": [
                _row(10, policy="SCHED_OTHER", priority=0, affinity=(0,), control=True),
                _row(11, policy="SCHED_FIFO", priority=20, affinity=(0,), control=False),
            ],
        },
    )
    monkeypatch.setattr(scheduler.threading, "get_native_id", lambda: 10)
    with pytest.raises(scheduler.TimingSchedulerError, match="helper thread"):
        scheduler.FormalTimingSchedulerLeaseV2().enter()


def test_stage_contract_rejects_legacy_global_scheduler_receipt(tmp_path) -> None:
    receipt = tmp_path / "timing.json"
    receipt.write_text(
        '{"schema":"step5d.autotune-v4/r013-timing-characterization-v1",'
        '"classification":"coalescing_candidate","target_met":true,'
        '"timing_observation_count":3,"timing_process":{"policy":"SCHED_FIFO",'
        '"priority":20,"cpu_affinity":[11,13,14,15]}}',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="not a 3-run pass"):
        _load_coalescing_timing_contract(receipt, run_dir=tmp_path)


def test_stage_contract_requires_gc_window_receipts(tmp_path) -> None:
    process = {
        "schema": "step5d.autotune-v4/r013-timing-process-receipt-v2",
        "scope": "per_attempt",
        "profile_id": scheduler.LATE_CONTROL_FIFO_PROFILE,
        "policy": "SCHED_FIFO",
        "priority": 20,
        "cpu_affinity": [11, 13, 14, 15],
        "receipt_count": 3,
        "helper_non_other_thread_count": 0,
        "restore_verified": True,
        "kernel_rt_bandwidth_unchanged": True,
        "early_process_promotion": False,
        "gc_window_receipt_count": 3,
        "gc_window_entered": True,
        "gc_window_restored": True,
    }
    scheduler_receipt = {
        "schema": "step5d.autotune-v4/r013-timing-scheduler-receipt-v2",
        "restore_verified": True,
        "kernel_rt_bandwidth_unchanged": True,
        "helper_non_other_thread_count": 0,
    }
    gc_receipt = {
        "schema": "step5d.autotune-v4/r013-gc-window-receipt-v1",
        "entered": True,
        "restored": True,
        "restored_after_timing_lease": True,
        "pre_enabled": True,
        "post_enabled": True,
    }
    value = {
        "schema": "step5d.autotune-v4/r013-timing-characterization-v2",
        "classification": "coalescing_candidate",
        "target_met": True,
        "timing_observation_count": 3,
        "timing_process": process,
        "observations": [
            {
                "run_dir": str(tmp_path),
                "timing_scheduler": scheduler_receipt,
                "gc_window": gc_receipt,
            }
            for _ in range(3)
        ],
    }
    receipt = tmp_path / "timing.json"
    receipt.write_text(json.dumps(value), encoding="utf-8")
    assert _load_coalescing_timing_contract(receipt, run_dir=tmp_path)["target_met"] is True
