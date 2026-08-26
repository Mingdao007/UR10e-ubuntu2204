#!/usr/bin/env python3
"""Capture read-only host timing context for V4 attribution."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import time
from typing import Any


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def capture(*, cpus: tuple[int, ...] = (11, 13, 14, 15)) -> dict[str, Any]:
    frequency: dict[str, Any] = {}
    siblings: dict[str, Any] = {}
    governors: dict[str, Any] = {}
    for cpu in sorted(set(cpus) | {29}):
        key = str(cpu)
        frequency[key] = _read(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_cur_freq")
        governors[key] = _read(f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/scaling_governor")
        siblings[key] = _read(f"/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list")
    interrupts = []
    raw_interrupts = _read("/proc/interrupts") or ""
    for line in raw_interrupts.splitlines():
        if any(token in line.lower() for token in ("nvme", "enp3s0", "rtde", "nvidia")):
            interrupts.append(line)
    try:
        affinity = sorted(os.sched_getaffinity(0))
        scheduler = os.sched_getscheduler(0)
        priority = int(os.sched_getparam(0).sched_priority)
    except (AttributeError, OSError, ValueError):
        affinity = []
        scheduler = None
        priority = None
    return {
        "schema": "step5d.autotune-v4/r013-host-timing-snapshot-v1",
        "version": 1,
        "captured_unix_ns": time.time_ns(),
        "hostname": platform.node(),
        "kernel": platform.release(),
        "loadavg": _read("/proc/loadavg"),
        "isolated_cpus": _read("/sys/devices/system/cpu/isolated"),
        "nohz_full": _read("/sys/devices/system/cpu/nohz_full"),
        "rt_runtime_us": _read("/proc/sys/kernel/sched_rt_runtime_us"),
        "rt_period_us": _read("/proc/sys/kernel/sched_rt_period_us"),
        "process_affinity": affinity,
        "process_scheduler": scheduler,
        "process_rt_priority": priority,
        "cpus": sorted(set(cpus)),
        "frequency_hz_or_khz": frequency,
        "governors": governors,
        "thread_siblings": siblings,
        "relevant_interrupt_lines": interrupts,
        "interpretation": {
            "affinity_is_not_whole_core_isolation": True,
            "snapshot_is_diagnostic_not_live_acceptance": True,
            "does_not_change_irq_cpufreq_or_smt": True,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = capture()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
