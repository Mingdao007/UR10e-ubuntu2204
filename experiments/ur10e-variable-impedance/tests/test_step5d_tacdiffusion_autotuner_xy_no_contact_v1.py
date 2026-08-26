from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "tase-contact-reproduction" / "tools"))

from ur10e_vic.tacdiffusion.step5d_autotuner_xy_no_contact_v1 import (  # noqa: E402
    CBF_QP_HALF_AXES_M,
    CONTROL_RATE_HZ,
    HARD_ELLIPSE_HALF_AXES_M,
    ORIGIN_XY_M,
    P_LATERAL_XY,
    PROFILE_ID,
    U_ALONG_XY,
    build_offline_evidence,
    build_runtime_source,
    load_numeric_sanity,
    package_identity,
    reference_at,
    sample_count,
)


def test_xy_no_contact_vertical_slice_is_exact_and_fail_closed() -> None:
    sanity = load_numeric_sanity()
    assert sanity["profile_id"] == PROFILE_ID
    assert sample_count(60) == 30_000
    assert CONTROL_RATE_HZ == 500

    origin = reference_at(0.0, startup_safe_z_m=0.123, anchor_orientation_rotvec=(0.1, 0.2, 0.3))
    midpoint = reference_at(30.0, startup_safe_z_m=0.123, anchor_orientation_rotvec=(0.1, 0.2, 0.3))
    endpoint = reference_at(60.0, startup_safe_z_m=0.123, anchor_orientation_rotvec=(0.1, 0.2, 0.3))
    assert origin.pose_base[:2] == ORIGIN_XY_M
    assert origin.pose_base[2:] == (0.123, 0.1, 0.2, 0.3)
    assert midpoint.along_m == sanity["checked_points"]["t30"]["along_m"]
    assert midpoint.lateral_m == sanity["checked_points"]["t30"]["lateral_m"]
    for actual, expected in zip(midpoint.pose_base[:2], sanity["checked_points"]["t30"]["base_xy_m"]):
        assert abs(actual - expected) < 1e-15
    for actual, expected in zip(endpoint.pose_base[:2], sanity["checked_points"]["t60"]["base_xy_m"]):
        assert abs(actual - expected) < 1e-15
    assert abs(midpoint.velocity_base[0] * U_ALONG_XY[0] + midpoint.velocity_base[1] * U_ALONG_XY[1]) < 0.0031
    assert (midpoint.velocity_base[0] ** 2 + midpoint.velocity_base[1] ** 2) ** 0.5 <= 0.003 + 1e-12
    assert (endpoint.velocity_base[0] ** 2 + endpoint.velocity_base[1] ** 2) ** 0.5 <= 0.003 + 1e-12

    source = build_runtime_source()
    for token in (
        "direct_torque_cartesian_impedance",
        "get_jacobian(q)",
        "get_coriolis_and_centrifugal_torques(q, qd)",
        "zero_feedforward_wrench",
        "training_enabled = False",
        "contact_search_enabled = False",
        "hard_half_axes",
        "cbf_qp_half_axes",
        "kunwei_force_limit_n",
        "sequence_advanced = sequence_after > 0 and sequence_after > last_sequence and sequence_after <= last_sequence + 5",
        "return to the path origin before session Home",
        "zero -> abort -> stop",
    ):
        assert token in source
    for forbidden in ("speedj", "socket_open", "R013"):
        assert forbidden not in source

    evidence = build_offline_evidence("2s", startup_safe_z_m=0.123, anchor_orientation_rotvec=(0.1, 0.2, 0.3))
    assert evidence["sample_count"] == 1_000
    assert len(evidence["recorder"]["samples"]) == 1_000
    assert set(evidence["recorder"]["samples"][0]) == set(evidence["recorder"]["sample_fields"])
    assert evidence["flags"] == {
        "contact": False,
        "training_dataset": False,
        "qualification": False,
        "promotion": False,
        "tracking_failure_diagnostic_only": True,
    }
    assert evidence["terminal_sequence"] == ["path_origin", "session_home", "stop", "stationary_verified"]
    assert evidence["recorder"]["samples"][-1]["terminal_safety_snapshot"]["stationary_verified"] is True
    assert evidence["recorder"]["samples"][-1]["command_lineage"]["lineage_ok"] is True
    assert evidence["runtime"]["feedforward_wrench"] == [0.0] * 6
    assert evidence["guard_stack"]["hard_half_axes_m"] == list(HARD_ELLIPSE_HALF_AXES_M)
    assert evidence["guard_stack"]["cbf_qp_half_axes_m"] == list(CBF_QP_HALF_AXES_M)

    identity = package_identity()
    assert len(identity["package_id"]) == 64
    assert identity["numeric_sanity_sha256"] == __import__("hashlib").sha256(
        (ROOT.parent / "tase-contact-reproduction" / "config" / "step5d_tacdiffusion_autotuner_xy_no_contact_v1_numeric_sanity.json").read_bytes()
    ).hexdigest()
    table = json.loads((ROOT.parent / "tase-contact-reproduction" / "config" / "step5_stage_table.json").read_text())
    row = next(item for item in table["stages"] if item["id"] == PROFILE_ID)
    assert row["active"] is False
    assert row["contact"] is False
