"""Focused R012 readable-identity and controller-package acceptance."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r012.behavior import R012_PROGRAM, R012_RUNTIME_PROTOCOL  # noqa: E402
from step5d_autotune_v4_r012.controller_triplet import (  # noqa: E402
    SOURCE_SCRIPT,
    UNDERLYING_MOTION_PROTOCOL,
    build_controller_triplet,
    validate_controller_triplet,
)
from step5d_autotune_v4_r012.descriptor import build_descriptor  # noqa: E402
from step5d_autotune_v4_r012.runtime_composition import (  # noqa: E402
    BOUNDED_TUNING_BLOCKERS,
    build_readiness_report,
    require_live_admission,
    verify_controller_readback,
    verify_local_controller_triplet,
)
from step5d_autotune_v4_r012.snapshot import build_live_snapshot  # noqa: E402
from step5d_autotune_v4_r012.serial_queue import SerialOneSlotQueue, SerialQueueError  # noqa: E402


CAMPAIGN = "r012-campaign-test"
RUN = "r012-run-test"
ATTEMPT = "attempt-000001"


def _programs(directory: Path | None = None) -> dict[str, dict[str, str]]:
    directory = directory or ROOT / "programs/step5/step5d"
    return {
        role: {"path": str(directory / f"{R012_PROGRAM}.{role}"), "basename": f"{R012_PROGRAM}.{role}"}
        for role in ("script", "txt", "urp")
    }


def _snapshot():
    return build_live_snapshot(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT, controller_programs=_programs())


def test_descriptor_is_readable_and_fixed() -> None:
    descriptor = build_descriptor(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT)
    assert descriptor.as_dict() == {
        "program": R012_PROGRAM, "revision": 12,
        "motion_protocol": 606006, "extension_protocol": 612012,
        "campaign_id": CAMPAIGN, "run_id": RUN, "attempt_id": ATTEMPT,
        "route_id": f"r012-route-{RUN}", "session_id": f"r012-session-{RUN}", "session_epoch": 1,
    }


def test_snapshot_contains_no_release_identity() -> None:
    document = str(_snapshot().as_dict()).lower()
    assert all(token not in document for token in ("sha256", "fingerprint", "digest", "sourceclosure", "releaseidentity"))


def test_controller_triplet_uses_mature_motion_and_readable_registers(tmp_path: Path) -> None:
    paths = build_controller_triplet(tmp_path, stamp="test-r012-readable")
    script = paths["script"].read_text(encoding="utf-8")
    checks = validate_controller_triplet(script, paths["txt"].read_text(encoding="utf-8"), paths["urp"].read_bytes())
    assert all(checks.values())
    assert f"write_output_integer_register(32, {UNDERLYING_MOTION_PROTOCOL})" in script
    assert "write_output_integer_register(33, 12)" in script
    assert f"write_output_integer_register(34, {R012_RUNTIME_PROTOCOL})" in script
    assert "read_input_integer_register(35) == ordinal" in script
    assert "write_output_integer_register(36, ordinal)" in script
    assert not (tmp_path / f"{R012_PROGRAM}.controller-manifest.json").exists()
    assert not (tmp_path / f"{R012_PROGRAM}.deploy-manifest.json").exists()


def test_build_accepts_source_comment_change_without_resealing(tmp_path: Path) -> None:
    source = tmp_path / "mature.script"
    source.write_text(SOURCE_SCRIPT.read_text(encoding="utf-8") + "\n# owner comment changed\n", encoding="utf-8")
    paths = build_controller_triplet(tmp_path / "package", source_script_path=source, stamp="stable-readable")
    assert "owner comment changed" in paths["script"].read_text(encoding="utf-8")
    assert all("sha" not in path.read_text(encoding="utf-8", errors="ignore").lower() for path in (paths["script"], paths["txt"]))


def test_local_package_validation_is_direct_and_manifest_free() -> None:
    assert verify_local_controller_triplet(_snapshot()) is True


def _readback_receipt(snapshot, readback_dir: Path) -> dict[str, object]:
    local = {role: Path(item["path"]) for role, item in snapshot.controller_programs.items()}
    readback_dir.mkdir()
    files: dict[str, str] = {}
    for role, path in local.items():
        target = readback_dir / path.name
        shutil.copyfile(path, target)
        files[role] = str(target)
    descriptor = build_descriptor(campaign_id=snapshot.campaign_id, run_id=snapshot.run_id, attempt_id=snapshot.attempt_id)
    return {
        "schema": "step5d.autotune-v4/r012-controller-readback-v2", "fresh_get": True,
        "program": R012_PROGRAM, "revision": 12, "motion_protocol": 606006,
        "extension_protocol": 612012, "controller_target": f"/programs/andyl/kunwei/step5/{R012_PROGRAM}.urp",
        "basenames": {role: path.name for role, path in local.items()}, "readback_files": files,
        "byte_equal": True, "safety_mode": "NORMAL", "stationary": True, "home": True,
        "timestamp": "2026-08-12T07:00:00+00:00",
        "descriptor": descriptor.as_dict(), "state": "HOME_STATIONARY_NORMAL",
    }


def test_readback_byte_equal_passes_and_one_byte_mismatch_fails(tmp_path: Path) -> None:
    snapshot = _snapshot()
    receipt = _readback_receipt(snapshot, tmp_path / "readback")
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert verify_controller_readback(snapshot, receipt_path) is True
    script = Path(receipt["readback_files"]["script"])
    script.write_bytes(script.read_bytes()[:-1] + bytes([script.read_bytes()[-1] ^ 1]))
    assert verify_controller_readback(snapshot, receipt_path) is False


def test_readiness_uses_only_readable_behavioral_gates() -> None:
    snapshot = _snapshot()
    report = build_readiness_report(snapshot, evidence={name: True for name in BOUNDED_TUNING_BLOCKERS})
    assert all(report["gates"][name] for name in BOUNDED_TUNING_BLOCKERS)
    assert report["live_ready"] is True


def test_wrong_identity_or_stale_physical_facts_fail_admission() -> None:
    snapshot = _snapshot()
    facts = {name: True for name in BOUNDED_TUNING_BLOCKERS}
    facts.update({"payload_cog_tcp_valid": True, "protective_stop_clear": True, "force_torque_velocity_limits": True})
    facts.update(build_descriptor(campaign_id=CAMPAIGN, run_id=RUN, attempt_id=ATTEMPT).as_dict())
    facts.update({"controller_target": f"/programs/andyl/kunwei/step5/{R012_PROGRAM}.urp", "controller_basename": f"{R012_PROGRAM}.urp", "txt_basename": f"{R012_PROGRAM}.txt", "urp_basename": f"{R012_PROGRAM}.urp"})
    require_live_admission(snapshot, facts)
    facts["program"] = "wrong-program"
    with pytest.raises(ValueError):
        require_live_admission(snapshot, facts)
    facts["program"] = R012_PROGRAM
    facts["rtde_and_kunwei_fresh"] = False
    with pytest.raises(ValueError):
        require_live_admission(snapshot, facts)


def test_native_queue_ids_are_monotonic_and_resume_direct_state(tmp_path: Path) -> None:
    path = tmp_path / "r012_serial.jsonl"
    first = SerialOneSlotQueue(path, route_id="route-1", session_id="session-1")
    item = first.enqueue({"force_p_gain": 0.1, "normal_filter_tau_s": 0.05}, kind="REFERENCE")
    assert (item.request_id, item.dispatch_id) == ("request-000001", "dispatch-000001")
    first.dispatch()
    with pytest.raises(SerialQueueError):
        first.enqueue({"force_p_gain": 0.2}, kind="BO_TRIAL")
    first.result({"state": "HOME", "safe_return": True})
    resumed = SerialOneSlotQueue(path, route_id="route-1", session_id="session-1")
    next_item = resumed.enqueue({"force_p_gain": 0.2}, kind="BO_TRIAL")
    assert next_item.request_id == "request-000002"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert all("previous_record" not in row and "sha256" not in row for row in records)
