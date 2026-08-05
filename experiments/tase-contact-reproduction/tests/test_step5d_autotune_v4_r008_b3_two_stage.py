"""B3 FAR/NEAR contact-search schedule + TP overlay unit tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from step5d_autotune_v4_r006.contracts import load_contract
from step5d_autotune_v4_r008.b3_identity import (
    B3_CONTRACT_PATH,
    MAINLINE_FINGERPRINT,
    compute_b3_campaign_fingerprint,
)
from step5d_autotune_v4_r008.contact_search_schedule import (
    PROGRAM_B3,
    ContactSearchScheduleError,
    load_schedule,
    validate_planned_schedule,
)
from step5d_autotune_v4_r008.schedule_planner import WAVE3_V_FAR_M_S
from step5d_autotune_v4_r008.tp_two_stage_search import (
    build_b3_triplet,
    load_r006_published_script,
    transform_script,
)


ROOT = Path(__file__).resolve().parents[1]
WAVE1_SCHEDULE = (
    ROOT / "config/step5d/autotune_v4_r008_contact_search_schedule_wave1_observation.json"
)
WAVE2_SCHEDULE = (
    ROOT / "config/step5d/autotune_v4_r008_contact_search_schedule_wave2_far_x2.json"
)


def _published_b3_identity() -> tuple[str, str, dict]:
    """Read the rebuilt B3 contract without re-validating the r006 source closure."""

    path = B3_CONTRACT_PATH
    raw = json.loads(path.read_text(encoding="utf-8"))
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    fingerprint = str(raw["b3_canary"]["campaign_fingerprint"])
    return fingerprint, sha256, raw


def test_load_default_schedule_values() -> None:
    schedule = load_schedule()
    assert schedule.program == PROGRAM_B3
    assert schedule.version.startswith("b3-geometry-planned-wave3")
    assert schedule.v_far_m_s == pytest.approx(WAVE3_V_FAR_M_S)
    assert schedule.v_far_m_s == pytest.approx(0.002)
    assert schedule.v_near_m_s == pytest.approx(0.0005)
    assert schedule.d_near_start_travel_m == pytest.approx(0.011029311)
    assert schedule.F_far_n == pytest.approx(0.3)
    assert schedule.force_fuse_n == pytest.approx(50.0)
    assert schedule.v_far_m_s > schedule.v_near_m_s
    assert schedule.d_near_m < schedule.max_travel_m


def test_schedule_rejects_near_field_bump() -> None:
    raw = dict(load_schedule().raw)
    raw["v_near_m_s"] = 0.0025
    with pytest.raises(ContactSearchScheduleError, match="v_near_m_s"):
        validate_planned_schedule(raw)


def test_schedule_rejects_high_fuse() -> None:
    raw = dict(load_schedule().raw)
    raw["force_fuse_n"] = 60.0
    with pytest.raises(ContactSearchScheduleError, match="force_fuse_n"):
        validate_planned_schedule(raw)


def test_wave1_archive_fingerprint_differs_from_wave3() -> None:
    wave1 = load_schedule(WAVE1_SCHEDULE)
    wave2 = load_schedule(WAVE2_SCHEDULE)
    wave3 = load_schedule()
    fp1 = compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT, schedule=wave1
    )
    fp2 = compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT, schedule=wave2
    )
    fp3 = compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT, schedule=wave3
    )
    assert fp1 != fp2 != fp3
    assert fp1.startswith("8decbec3")
    assert fp2.startswith("f7788828")
    assert fp3.startswith("b0258539")


def test_b3_fingerprint_differs_from_mainline() -> None:
    schedule = load_schedule()
    fp = compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT,
        schedule=schedule,
    )
    assert fp != MAINLINE_FINGERPRINT
    assert not fp.startswith("eb48e895")
    assert not fp.startswith("1db4f9bf")
    assert not fp.startswith("8decbec3")  # Wave-1 observation
    assert not fp.startswith("f7788828")  # Wave-2
    assert len(fp) == 64
    published_fp, published_sha, raw = _published_b3_identity()
    assert published_fp == fp
    assert published_fp != MAINLINE_FINGERPRINT
    assert raw["program"] == PROGRAM_B3
    parent = load_contract(verify_source_closure=False)
    assert published_sha != parent.sha256


def test_transform_script_has_two_stage_and_fuse(tmp_path: Path) -> None:
    schedule = load_schedule()
    campaign_fingerprint, contract_sha256, _raw = _published_b3_identity()
    assert campaign_fingerprint == compute_b3_campaign_fingerprint(
        parent_fingerprint=MAINLINE_FINGERPRINT,
        schedule=schedule,
    )
    source = load_r006_published_script()
    script = transform_script(
        source,
        schedule,
        contract_sha256=contract_sha256,
        campaign_fingerprint=campaign_fingerprint,
    )
    assert "local v_far_m_s = 0.002000000" in script
    assert "local v_near_m_s = 0.000500000" in script
    assert "local d_near_start_travel_m = 0.011029311" in script
    assert "force_fuse_n = 50.000000000" in script
    assert "codex_r006_packet_guard(packet_reason, 100.0, 100.0, 3.0)" in script
    assert ", 75," in script
    assert ">= 0.500000000" in script
    assert ">= 0.700000000" in script
    assert PROGRAM_B3 in script
    assert campaign_fingerprint in script
    assert f"# V4_CAMPAIGN_FINGERPRINT: {campaign_fingerprint}" in script
    assert f"# V4_CAMPAIGN_FINGERPRINT: {MAINLINE_FINGERPRINT}" not in script

    # Wave2b two-segment adaptive return: home_z target, no +5mm third descend.
    return_home = script[
        script.index("def codex_r006_return_home") : script.index(
            "def codex_r006_execute_attempt"
        )
    ]
    assert "local target_z = home_pose[2]" in return_home
    assert "local z_eps_m = 0.001000000" in return_home
    assert "home_pose[2] + 0.005000000" not in return_home
    assert "safe_z" not in return_home
    assert return_home.count("movel(") == 2  # vertical (conditional) + XY
    assert "movel(home_pose," not in return_home  # no third descend to home
    assert "two-segment adaptive" in return_home
    assert "no +5mm third descend" in script

    paths = build_b3_triplet(
        tmp_path,
        contract_sha256=contract_sha256,
        campaign_fingerprint=campaign_fingerprint,
        schedule=schedule,
    )
    assert paths[".script"].name == f"{PROGRAM_B3}.script"
    assert paths[".urp"].name == f"{PROGRAM_B3}.urp"
    assert not (tmp_path / "step5d_strict_rnn_autotune_v4_r006.script").exists()
    built = paths[".script"].read_text(encoding="utf-8")
    assert "local target_z = home_pose[2]" in built
    assert "home_pose[2] + 0.005000000" not in built
    sanity = json.loads(paths[".numeric-sanity.json"].read_text(encoding="utf-8"))
    assert sanity["triplet_checks"]["two_segment_return_target_z"] is True
    assert sanity["triplet_checks"]["no_plus_5mm_return_clearance"] is True
    manifest = json.loads(paths[".deploy-manifest.json"].read_text(encoding="utf-8"))
    assert manifest["campaign_fingerprint"] != MAINLINE_FINGERPRINT
    assert manifest["overwrites_mainline_r006"] is False
