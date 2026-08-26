"""Per-attempt timing scheduler lease for the R013 live owner.

The process must remain ``SCHED_OTHER`` while contexts, transports, lifecycle
writers, and diagnostic helpers are built.  A lease promotes only the current
control thread for the ARM-to-safe-Home interval and records enough per-thread
evidence to reject inherited RT policy or a changed kernel RT quota.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import threading
from typing import Any, Mapping


TIMING_SCHEDULER_RECEIPT_SCHEMA = (
    "step5d.autotune-v4/r013-timing-scheduler-receipt-v2"
)
TIMING_SCHEDULER_RECEIPT_VERSION = 2
LATE_CONTROL_FIFO_PROFILE = "late_control_fifo_v1"
QUOTA_SAFE_OTHER_PROFILE = "quota_safe_other_v1"
DEFAULT_CONTROL_CPU_AFFINITY = (11, 13, 14, 15)
DEFAULT_FIFO_PRIORITY = 20


class TimingSchedulerError(RuntimeError):
    """The live timing lease could not prove its scheduler contract."""


@dataclass(frozen=True)
class TimingSchedulerProfileV1:
    """Bounded, logged scheduler decision for one physical attempt."""

    profile_id: str = LATE_CONTROL_FIFO_PROFILE
    control_cpu_affinity: tuple[int, ...] = DEFAULT_CONTROL_CPU_AFFINITY
    fifo_priority: int = DEFAULT_FIFO_PRIORITY

    def __post_init__(self) -> None:
        if self.profile_id not in {
            LATE_CONTROL_FIFO_PROFILE,
            QUOTA_SAFE_OTHER_PROFILE,
        }:
            raise ValueError(f"unknown timing scheduler profile: {self.profile_id}")
        cpus = tuple(self.control_cpu_affinity)
        if not cpus or any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0 for cpu in cpus):
            raise ValueError("control CPU affinity must be a non-empty tuple of non-negative ints")
        if len(set(cpus)) != len(cpus):
            raise ValueError("control CPU affinity contains duplicates")
        if isinstance(self.fifo_priority, bool) or not isinstance(self.fifo_priority, int):
            raise ValueError("FIFO priority must be an int")
        if not 1 <= self.fifo_priority <= 99:
            raise ValueError("FIFO priority must be in 1..99")

    @classmethod
    def from_id(cls, profile_id: str | None) -> "TimingSchedulerProfileV1":
        value = LATE_CONTROL_FIFO_PROFILE if profile_id is None else str(profile_id)
        if value == QUOTA_SAFE_OTHER_PROFILE:
            return cls(profile_id=value)
        if value == LATE_CONTROL_FIFO_PROFILE:
            return cls(profile_id=value)
        raise ValueError(f"unknown timing scheduler profile: {value}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r013-timing-scheduler-profile-v1",
            "profile_id": self.profile_id,
            "control_cpu_affinity": list(self.control_cpu_affinity),
            "fifo_priority": self.fifo_priority,
        }


def _policy_name(policy: int) -> str:
    return {
        getattr(os, "SCHED_OTHER", -1): "SCHED_OTHER",
        getattr(os, "SCHED_FIFO", -2): "SCHED_FIFO",
        getattr(os, "SCHED_RR", -3): "SCHED_RR",
    }.get(int(policy), f"UNKNOWN_{policy}")


def _scheduler_metadata(tid: int) -> dict[str, Any]:
    policy_value = int(os.sched_getscheduler(tid))
    return {
        "tid": int(tid),
        "policy": _policy_name(policy_value),
        "policy_value": policy_value,
        "priority": int(os.sched_getparam(tid).sched_priority),
        "affinity": sorted(int(cpu) for cpu in os.sched_getaffinity(tid)),
    }


def _thread_scheduler_snapshot(current_tid: int) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    task_root = Path("/proc/self/task")
    try:
        tids = sorted(int(entry.name) for entry in task_root.iterdir())
    except (FileNotFoundError, OSError, ValueError):
        tids = [int(current_tid)]
    for tid in tids:
        try:
            row = _scheduler_metadata(tid)
        except (FileNotFoundError, ProcessLookupError, OSError, ValueError):
            continue
        row["is_control_thread"] = tid == int(current_tid)
        rows.append(row)
    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['policy']}/{row['priority']}"
        counts[key] = counts.get(key, 0) + 1
    return {
        "current_tid": int(current_tid),
        "thread_count": len(rows),
        "policy_counts": dict(sorted(counts.items())),
        "threads": rows,
    }


def _rt_bandwidth_metadata() -> dict[str, int | None]:
    values: dict[str, int | None] = {}
    for name in ("sched_rt_period_us", "sched_rt_runtime_us"):
        try:
            values[name] = int(Path("/proc/sys/kernel")
                               .joinpath(name).read_text(encoding="utf-8").strip())
        except (FileNotFoundError, OSError, ValueError):
            values[name] = None
    return values


def _helper_non_other(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = snapshot.get("threads", ())
    if not isinstance(rows, list):
        return []
    return [
        dict(row)
        for row in rows
        if isinstance(row, Mapping)
        and not bool(row.get("is_control_thread"))
        and row.get("policy") != "SCHED_OTHER"
    ]


class FormalTimingSchedulerLeaseV2:
    """Promote and restore only the current thread for one live attempt."""

    def __init__(
        self,
        profile: TimingSchedulerProfileV1 | str | None = None,
    ) -> None:
        self.profile = (
            profile
            if isinstance(profile, TimingSchedulerProfileV1)
            else TimingSchedulerProfileV1.from_id(profile)
        )
        self._tid: int | None = None
        self._original: dict[str, Any] | None = None
        self._original_affinity: tuple[int, ...] | None = None
        self._entered = False
        self._restored = False
        self._receipt: dict[str, Any] = {
            "schema": TIMING_SCHEDULER_RECEIPT_SCHEMA,
            "version": TIMING_SCHEDULER_RECEIPT_VERSION,
            "profile": self.profile.as_dict(),
            "status": "not_entered",
        }

    @property
    def entered(self) -> bool:
        return self._entered

    @property
    def restored(self) -> bool:
        return self._restored

    def enter(self) -> dict[str, Any]:
        if self._entered:
            raise TimingSchedulerError("timing scheduler lease is already entered")
        tid = threading.get_native_id()
        original = _scheduler_metadata(tid)
        if original.get("policy") != "SCHED_OTHER" or int(original.get("priority", -1)) != 0:
            raise TimingSchedulerError(
                f"timing lease requires SCHED_OTHER/0 before promotion: {original}"
            )
        before_threads = _thread_scheduler_snapshot(tid)
        before_quota = _rt_bandwidth_metadata()
        helpers = _helper_non_other(before_threads)
        if helpers:
            raise TimingSchedulerError(
                f"helper thread already has non-SCHED_OTHER policy: {helpers}"
            )
        self._tid = tid
        self._original = original
        self._original_affinity = tuple(int(cpu) for cpu in original["affinity"])
        self._receipt.update(
            {
                "status": "entered",
                "control_tid": tid,
                "initial_control": original,
                "before_threads": before_threads,
                "kernel_rt_bandwidth_before": before_quota,
            }
        )
        try:
            if self.profile.profile_id == LATE_CONTROL_FIFO_PROFILE:
                os.sched_setaffinity(tid, set(self.profile.control_cpu_affinity))
                os.sched_setscheduler(
                    tid,
                    os.SCHED_FIFO,
                    os.sched_param(self.profile.fifo_priority),
                )
            active = _scheduler_metadata(tid)
            after_threads = _thread_scheduler_snapshot(tid)
            helper_after = _helper_non_other(after_threads)
            quota_after = _rt_bandwidth_metadata()
            if self.profile.profile_id == LATE_CONTROL_FIFO_PROFILE and (
                active.get("policy") != "SCHED_FIFO"
                or int(active.get("priority", -1)) != self.profile.fifo_priority
                or tuple(active.get("affinity", ())) != tuple(self.profile.control_cpu_affinity)
            ):
                raise TimingSchedulerError(f"control thread FIFO promotion differs: {active}")
            if helper_after:
                raise TimingSchedulerError(
                    f"helper thread unexpectedly uses non-SCHED_OTHER policy: {helper_after}"
                )
            if quota_after != before_quota:
                raise TimingSchedulerError(
                    f"kernel RT bandwidth changed during scheduler admission: {before_quota} -> {quota_after}"
                )
            self._entered = True
            self._receipt.update(
                {
                    "active_control": active,
                    "after_enter_threads": after_threads,
                    "helper_non_other_thread_count": len(helper_after),
                    "kernel_rt_bandwidth_after_enter": quota_after,
                    "kernel_rt_bandwidth_unchanged_at_enter": quota_after == before_quota,
                }
            )
            return self.receipt()
        except Exception:
            self._restore_current_thread()
            self._tid = None
            self._original = None
            self._original_affinity = None
            self._receipt.update({"status": "admission_failed"})
            raise

    def _restore_current_thread(self) -> None:
        if self._tid is None or self._original is None or self._original_affinity is None:
            return
        tid = self._tid
        # Demote before widening affinity so a restored control thread never
        # continues FIFO while it is moved back to its caller's CPU set.
        if _scheduler_metadata(tid).get("policy") != self._original.get("policy"):
            os.sched_setscheduler(
                tid,
                int(self._original["policy_value"]),
                os.sched_param(int(self._original["priority"])),
            )
        os.sched_setaffinity(tid, set(self._original_affinity))

    def exit(self) -> dict[str, Any]:
        if self._restored:
            return self.receipt()
        if not self._entered:
            self._receipt.setdefault("status", "not_entered")
            self._restored = True
            return self.receipt()
        assert self._tid is not None
        before_restore = _scheduler_metadata(self._tid)
        self._restore_current_thread()
        restored = _scheduler_metadata(self._tid)
        after_threads = _thread_scheduler_snapshot(self._tid)
        quota_after = _rt_bandwidth_metadata()
        original = self._original or {}
        restore_ok = (
            restored.get("policy") == original.get("policy")
            and int(restored.get("priority", -1)) == int(original.get("priority", -2))
            and tuple(restored.get("affinity", ())) == tuple(self._original_affinity or ())
            and not _helper_non_other(after_threads)
            and quota_after == self._receipt.get("kernel_rt_bandwidth_before")
        )
        self._restored = True
        self._receipt.update(
            {
                "status": "restored" if restore_ok else "restore_failed",
                "before_restore_control": before_restore,
                "restored_control": restored,
                "after_restore_threads": after_threads,
                "kernel_rt_bandwidth_after": quota_after,
                "kernel_rt_bandwidth_unchanged": quota_after == self._receipt.get("kernel_rt_bandwidth_before"),
                "restore_verified": restore_ok,
                "helper_non_other_thread_count_after_restore": len(_helper_non_other(after_threads)),
            }
        )
        if not restore_ok:
            raise TimingSchedulerError(f"timing scheduler lease restore failed: {self._receipt}")
        return self.receipt()

    def receipt(self) -> dict[str, Any]:
        return _jsonable(self._receipt)

    def __enter__(self) -> "FormalTimingSchedulerLeaseV2":
        self.enter()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        del exc_type, exc, tb
        self.exit()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "DEFAULT_CONTROL_CPU_AFFINITY",
    "DEFAULT_FIFO_PRIORITY",
    "FormalTimingSchedulerLeaseV2",
    "LATE_CONTROL_FIFO_PROFILE",
    "QUOTA_SAFE_OTHER_PROFILE",
    "TIMING_SCHEDULER_RECEIPT_SCHEMA",
    "TimingSchedulerError",
    "TimingSchedulerProfileV1",
]
