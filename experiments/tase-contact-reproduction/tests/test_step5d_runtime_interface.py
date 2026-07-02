#!/usr/bin/env python3
"""Tests for Step5d live-prep runtime interface defaults."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import step5d_runtime_interface as iface  # noqa: E402


class Step5dRuntimeInterfaceTest(unittest.TestCase):
    def test_v21_defaults_are_bridge_tunable(self) -> None:
        runtime = iface.resolve_runtime_interface(program=iface.STEP5D_LIVEPREP_V21_STAGE_ID, root=ROOT, env={})

        self.assertEqual(runtime.program, iface.STEP5D_LIVEPREP_V21_STAGE_ID)
        self.assertEqual(runtime.preload_gate.filtered_min_n, 7.5)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 14.0)
        self.assertEqual(runtime.preload_gate.raw_min_n, 7.0)
        self.assertEqual(runtime.preload_gate.raw_max_n, 15.0)
        self.assertEqual(runtime.preload_gate.hold_s, 0.100)
        self.assertEqual(runtime.line_entry_param_valid_code, 521.0)

    def test_step5d_env_overrides_use_step5d_namespace(self) -> None:
        runtime = iface.resolve_runtime_interface(
            program=iface.STEP5D_LIVEPREP_V21_STAGE_ID,
            root=ROOT,
            env={
                "STEP5D_PRELOAD_FILTERED_MIN_N": "6.5",
                "STEP5D_PRELOAD_FILTERED_MAX_N": "13.5",
                "STEP5D_FORCE_DAMPING": "8.5",
                "STEP5D_NORMAL_FILTER_ALPHA": "0.6",
            },
        )

        self.assertEqual(runtime.preload_gate.filtered_min_n, 6.5)
        self.assertEqual(runtime.preload_gate.filtered_max_n, 13.5)
        self.assertEqual(runtime.bridge_defaults.force_damping, 8.5)
        self.assertEqual(runtime.bridge_defaults.normal_filter_alpha, 0.6)


if __name__ == "__main__":
    unittest.main()
