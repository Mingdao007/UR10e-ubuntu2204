from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_start_hover_r001 as builder  # noqa: E402


STAMP = "2026-07-28T0000HKT_STEP5D_AUTOTUNE_START_HOVER_R001"
R026_TRIPLET_SHA256 = {
    ".script": "bd0058b9977279db60c706e2092629c88ef811ad9fbaf7d4672967c05c118874",
    ".txt": "f511dd7b371d9894dca3fb814befab5e4bde7e085deca6a6239911dd57cfb1d3",
    ".urp": "1e30209b0f5c8f15629e5a937680c5f5ca54e50735fcc55db3dd3a95bf220c8b",
}


def test_route_geometry_covers_both_conditional_descent_branches() -> None:
    below = builder.route_geometry((0.450, 0.100, 0.020, 3.000, 0.100, 0.000))
    above = builder.route_geometry((0.450, 0.100, 0.050, 3.000, 0.100, 0.000))

    assert below["safe_transfer_z_m"] == builder.TARGET_POSE[2]
    assert below["segments"][0]["span_m"] == pytest.approx(0.013)
    assert below["segments"][2]["included"] is False
    assert above["safe_transfer_z_m"] == pytest.approx(0.050)
    assert above["segments"][0]["span_m"] == pytest.approx(0.0)
    assert above["segments"][2]["included"] is True
    assert above["segments"][2]["span_m"] == pytest.approx(0.017)


def test_fail_closed_numeric_guards() -> None:
    assert builder.initial_pose_is_finite((0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
    assert not builder.initial_pose_is_finite((float("nan"), 0.0, 0.0, 0.0, 0.0, 0.0))
    assert not builder.initial_pose_is_finite((float("inf"), 0.0, 0.0, 0.0, 0.0, 0.0))
    assert builder.target_pose_is_within_limits(builder.TARGET_POSE)
    rejected_target = (*builder.TARGET_POSE[:2], 0.010, *builder.TARGET_POSE[3:])
    assert not builder.target_pose_is_within_limits(rejected_target)


def test_rendered_script_is_standalone_one_shot_and_has_no_runtime_protocol() -> None:
    script = builder.build_package_script(STAMP)
    main_start = script.index(f"def {builder.PROGRAM_NAME}():")
    assert script[main_start:].splitlines()[1].strip() == "local current_pose = get_actual_tcp_pose()"
    assert script.count("movel(") == 3
    assert script.count("stopl(0.1)") == 3
    assert script.index("sleep(0.20)") < script.rindex(
        "codex_start_hover_final_stationary_verified(target_pose)"
    )
    assert script.rindex("codex_start_hover_final_stationary_verified(target_pose)") < script.rfind(
        "  halt"
    )
    assert all(token not in script.lower() for token in builder.FORBIDDEN_SCRIPT_TOKENS)
    assert "get_actual_tcp_pose()" in script
    assert "0.033000000" in script
    assert "r=0.0" in script


def test_triplet_manifest_and_reproducible_immutable_render(tmp_path: Path) -> None:
    generated = builder.write_triplet(tmp_path, STAMP)
    assert all(generated["checks"].values())
    checked = builder.check_triplet(tmp_path, STAMP)
    assert checked["pass"] is True

    manifest = json.loads(
        (tmp_path / f"{builder.PROGRAM_NAME}.deploy-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["immutable"] is True
    assert manifest["status"] == "local_candidate_not_uploaded"
    assert manifest["geometry_basis"]["program"] == "step5d_strict_rnn_autotune_v3_r026"
    assert manifest["delivery"]["current_release_pointer_changed"] is False

    with pytest.raises(FileExistsError):
        builder.write_triplet(tmp_path, STAMP)


def test_r026_triplet_remains_byte_identical() -> None:
    package_dir = ROOT / "programs/step5/step5d"
    for suffix, expected in R026_TRIPLET_SHA256.items():
        actual = hashlib.sha256(
            (package_dir / f"step5d_strict_rnn_autotune_v3_r026{suffix}").read_bytes()
        ).hexdigest()
        assert actual == expected
