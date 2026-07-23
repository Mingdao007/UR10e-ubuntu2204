#!/usr/bin/env python3
from __future__ import annotations

import unittest

from ur10e_example_controllers.kunwei_force_gate import (
    FORCE_KG_TO_N,
    MOMENT_KG_M_TO_NM,
)
from ur10e_example_controllers.kunwei_persistent_monitor import (
    KunweiMonitorConfig,
    KunweiPersistentMonitor,
)


class LatestZeroedWrenchTest(unittest.TestCase):
    def test_latest_zeroed_wrench_is_thread_safe_baseline_delta_in_si(self) -> None:
        monitor = KunweiPersistentMonitor(
            KunweiMonitorConfig(latest_max_age_s=0.25)
        )
        monitor._baseline = (1.0, 2.0, 3.0, 0.1, 0.2, 0.3)
        monitor._samples.append((100.0, (1.5, 1.0, 5.0, 0.4, 0.0, 0.8)))
        wrench = monitor.latest_zeroed_wrench_si(now=100.1)
        expected = (
            0.5 * FORCE_KG_TO_N,
            -1.0 * FORCE_KG_TO_N,
            2.0 * FORCE_KG_TO_N,
            0.3 * MOMENT_KG_M_TO_NM,
            -0.2 * MOMENT_KG_M_TO_NM,
            0.5 * MOMENT_KG_M_TO_NM,
        )
        for actual, wanted in zip(wrench, expected):
            self.assertAlmostEqual(actual, wanted, places=12)

    def test_latest_zeroed_wrench_rejects_stale_or_unzeroed_data(self) -> None:
        monitor = KunweiPersistentMonitor(
            KunweiMonitorConfig(latest_max_age_s=0.25)
        )
        with self.assertRaisesRegex(RuntimeError, "no baseline"):
            monitor.latest_zeroed_wrench_si(now=100.0)
        monitor._baseline = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        monitor._samples.append((99.0, (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)))
        with self.assertRaisesRegex(RuntimeError, "stale"):
            monitor.latest_zeroed_wrench_si(now=100.0)


if __name__ == "__main__":
    unittest.main()
