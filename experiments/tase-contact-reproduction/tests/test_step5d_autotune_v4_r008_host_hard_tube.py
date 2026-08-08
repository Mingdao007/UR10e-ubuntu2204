"""Offline tests for B3 host-side HardTubeGuard (default OFF)."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(
    0,
    str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"),
)

from step5d_autotune_v4_r008.host_hard_tube import (  # noqa: E402
    EXPECTED_REFERENCE_SHA256,
    HostHardTubeDecision,
    HostHardTubeGuard,
    env_flag_enabled,
)


def test_env_default_disabled() -> None:
    assert env_flag_enabled({}) is False
    g = HostHardTubeGuard.from_environ({})
    assert g.enabled is False
    d = g.evaluate(
        tp_state=25,
        path_time_s=1.0,
        tcp_pose_m_rad=(0.5, 0.1, 0.03, 0.0, 0.0, 0.0),
        monotonic_ns=1_000_000_000,
    )
    assert d.stop is False
    assert d.reason == "disabled"
    assert d.actual_distance_m is None
    assert d.remaining_margin_m is None


def test_disabled_as_dict_is_json_safe_allow_nan_false() -> None:
    """Regression: disabled HardTube used math.nan and killed PATH finalize."""
    g = HostHardTubeGuard.from_environ({})
    payload = g.evaluate(
        tp_state=25,
        path_time_s=1.0,
        tcp_pose_m_rad=(0.5, 0.1, 0.03, 0.0, 0.0, 0.0),
        monotonic_ns=1_000_000_000,
    ).as_dict()
    encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
    assert "NaN" not in encoded
    assert json.loads(encoded)["actual_distance_m"] is None


def test_decision_as_dict_strips_nonfinite() -> None:
    payload = HostHardTubeDecision(
        enabled=True,
        stop=False,
        reason="TUBE_INACTIVE",
        actual_distance_m=math.nan,
        remaining_margin_m=math.inf,
        path_time_s=1.0,
    ).as_dict()
    json.dumps(payload, sort_keys=True, allow_nan=False)
    assert payload["actual_distance_m"] is None
    assert payload["remaining_margin_m"] is None


def test_reference_sha_matches_launch_profile() -> None:
    g = HostHardTubeGuard.from_environ({}, force_enabled=True)
    assert g.enabled is True
    assert g.adapter is not None
    assert g.adapter.reference_sha256 == EXPECTED_REFERENCE_SHA256


def test_path_near_center_ok_then_breach_stops() -> None:
    g = HostHardTubeGuard.from_environ({}, force_enabled=True)
    g.reset()
    # Warm ticks near origin of cycloid at t=0 (center ≈ path origin, z=0.033)
    origin = (0.487795411149049, 0.12932679270060748, 0.033, 0.0, 0.0, 0.0)
    mono = 10_000_000_000
    last_ok = None
    for i in range(8):
        last_ok = g.evaluate(
            tp_state=25,
            path_time_s=0.0 + i * 0.002,
            tcp_pose_m_rad=origin,
            monotonic_ns=mono + i * 2_000_000,
            controller_timestamp_s=100.0 + i * 0.002,
        )
    assert last_ok is not None
    assert last_ok.stop is False
    assert last_ok.reason in {"TUBE_OK", "TUBE_BETWEEN_SAMPLES"}

    # Jump TCP 50 mm away in XY → breach
    far = (origin[0] + 0.050, origin[1], origin[2], 0.0, 0.0, 0.0)
    breached = None
    for j in range(8):
        breached = g.evaluate(
            tp_state=25,
            path_time_s=0.020 + j * 0.002,
            tcp_pose_m_rad=far,
            monotonic_ns=mono + (8 + j) * 2_000_000,
            controller_timestamp_s=100.020 + j * 0.002,
        )
        if breached.stop and breached.reason == "TUBE_ACTUAL_BREACH":
            break
    assert breached is not None
    assert breached.stop is True
    assert breached.reason == "TUBE_ACTUAL_BREACH"


def test_non_path_state_does_not_require_progress() -> None:
    g = HostHardTubeGuard.from_environ({}, force_enabled=True)
    d = g.evaluate(
        tp_state=21,
        path_time_s=None,
        tcp_pose_m_rad=(0.5, 0.1, 0.03, 0.0, 0.0, 0.0),
        monotonic_ns=1_000_000_000,
    )
    assert d.stop is False
    assert d.reason in {"TUBE_INACTIVE", "TUBE_STAGE_UNKNOWN", "TUBE_BETWEEN_SAMPLES"}
