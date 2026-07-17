from __future__ import annotations

import gzip
import hashlib
import html
import json
import os
import posixpath
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.tp_watchdog import (
    TpPhase,
    WatchdogAction,
    compare_measured_home,
    decide_watchdog,
)
from build_step5d_autotune_v2_tp import (
    ATTESTATION_SCHEMA,
    CANDIDATE_PROGRAM,
    RECEIPT_SCHEMA,
    SOURCE_PROGRAM,
    BuildError,
    build_package,
    load_snapshot_receipt,
    write_stale_fixture_receipt,
)
from verify_step5d_tp_watchdog_diff import (
    WatchdogBlock,
    _validate_canonical_source,
    inspect_content,
    load_canonical_blocks,
    normalized_content,
)


BLOCK_ID = "host_heartbeat_fail_closed_v2"
NEXT_PROGRAM = CANDIDATE_PROGRAM
SOURCE_STAMP = "2026-07-15T1151HKT_STEP5D_STRICT_RNN_AUTOTUNE_V1"
CANDIDATE_STAMP = "2026-07-17T1600HKT_STEP5D_STRICT_RNN_AUTOTUNE_V2"
FIXTURE_CONTROLLER_DIR = "/programs/andyl/demo"
CANONICAL_BLOCK_PATH = (
    ROOT / "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v2.script"
)
CANONICAL_BLOCK = CANONICAL_BLOCK_PATH.read_text(encoding="utf-8")


def test_wait_ack_host_loss_halts_at_verified_home_without_motion() -> None:
    decision = decide_watchdog(
        phase=TpPhase.WAIT_ACK,
        heartbeat_age_s=3.0,
        timeout_s=2.0,
        safety_normal=True,
        measured_home_verified=True,
    )
    assert decision.action is WatchdogAction.HALT_AT_VERIFIED_HOME
    assert decision.auto_home is False
    assert decision.home_known is True


def test_every_unknown_pose_phase_never_blindly_auto_homes() -> None:
    for phase in (TpPhase.ARMED, TpPhase.RUN, TpPhase.RETRACT, TpPhase.RETURN):
        decision = decide_watchdog(
            phase=phase,
            heartbeat_age_s=1.0,
            timeout_s=0.1,
            safety_normal=True,
            measured_home_verified=False,
        )
        assert decision.action is WatchdogAction.CONTROLLED_STOP_AND_HALT
        assert decision.auto_home is False


def test_next_start_home_comparison_returns_concrete_blocker() -> None:
    result = compare_measured_home(
        actual_tcp_pose=(0.004, 0, 0, 0, 0, 0),
        saved_tcp_pose=(0, 0, 0, 0, 0, 0),
        actual_q=(0, 0, 0, 0, 0, 0),
        saved_q=(0, 0, 0, 0, 0, 0),
    )
    assert result.matches is False
    assert result.blocker == "home_position_mismatch"


def test_tp_diff_normalization_removes_only_named_bounded_block_and_identity(
    tmp_path: Path,
) -> None:
    old = (
        "def old_program():\n"
        "  waypoint = p[1,2,3,4,5,6]\n"
        "end\n"
        "old_program()\n"
    )
    new = (
        "def new_program():\n"
        f"{CANONICAL_BLOCK}"
        "  waypoint = p[1,2,3,4,5,6]\nend\n"
        "new_program()\n"
    )
    old_script = tmp_path / "old_program.script"
    new_script = tmp_path / "new_program.script"
    old_script.write_text(old, encoding="utf-8")
    new_script.write_text(new, encoding="utf-8")
    old_normalized, old_blocks = normalized_content(
        old_script, current_program="old_program", candidate_program="new_program"
    )
    new_normalized, new_blocks = normalized_content(
        new_script, current_program="old_program", candidate_program="new_program"
    )
    assert old_blocks == 0 and new_blocks == 1
    assert new_normalized == old_normalized

    old_urp = tmp_path / "old_program.urp"
    new_urp = tmp_path / "new_program.urp"
    old_urp_text = (
        '<URProgram name="old_program" crcValue="OLD">'
        f"<cachedContents>{html.escape(old, quote=True)}</cachedContents>"
        "<file>/programs/demo/old_program.script</file>"
        "</URProgram>\n"
    )
    new_urp_text = (
        '<URProgram name="new_program" crcValue="NEW">'
        "<cachedContents>"
        f"{html.escape(new, quote=True)}"
        "</cachedContents>"
        "<file>/programs/demo/new_program.script</file>"
        "</URProgram>\n"
    )
    old_urp.write_bytes(gzip.compress(old_urp_text.encode("utf-8"), mtime=0))
    new_urp.write_bytes(gzip.compress(new_urp_text.encode("utf-8"), mtime=0))
    assert normalized_content(
        old_urp, current_program="old_program", candidate_program="new_program"
    )[0] == normalized_content(
        new_urp, current_program="old_program", candidate_program="new_program"
    )[0]

    new_script.write_text(new.replace("p[1,2,3,4,5,6]", "p[9,2,3,4,5,6]"), encoding="utf-8")
    assert normalized_content(
        new_script, current_program="old_program", candidate_program="new_program"
    )[0] != old_normalized


def _write_triplet_fixture(
    tmp_path: Path,
    *,
    script_block: str = CANONICAL_BLOCK,
    urp_block: str | None = None,
    append_after_program: bool = False,
    insert_in_helper_suffix_function: bool = False,
    insert_in_dead_branch: bool = False,
    insert_after_top_level_halt: bool = False,
    change_program_named_guard_string: bool = False,
    urp_xml_comment_only: bool = False,
    change_nonroot_crc: bool = False,
) -> tuple[Path, Path, Path, dict[str, Path]]:
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current.mkdir(parents=True)
    candidate.mkdir()
    current_program = SOURCE_PROGRAM
    next_program = NEXT_PROGRAM
    helper = (
        f"def helper_{current_program}_dead():\n"
        "  helper_waypoint = p[0,0,0,0,0,0]\n"
        "end\n"
        if insert_in_helper_suffix_function
        else ""
    )
    dead_branch_open = "  if False:\n" if insert_in_dead_branch else ""
    dead_branch_close = "  end\n" if insert_in_dead_branch else ""
    terminator = "  halt\n" if insert_after_top_level_halt else ""
    guard = (
        f'  configured_mode = "{current_program}"\n'
        if change_program_named_guard_string
        else ""
    )
    old_script = (
        f"# VERSION: {SOURCE_STAMP}\n"
        f"# STEP5_STAGE_ID: {current_program}\n"
        f"{helper}"
        f"def codex_{current_program}():\n"
        f"{guard}"
        f"{terminator}"
        f"{dead_branch_open}"
        "  waypoint = p[1,2,3,4,5,6]\n"
        f"{dead_branch_close}"
        "end\n"
        f"codex_{current_program}()\n"
    )
    identity_script = old_script.replace(current_program, next_program).replace(
        SOURCE_STAMP, CANDIDATE_STAMP
    )
    urp_representation = html.escape(urp_block or script_block, quote=True)
    if append_after_program:
        new_script = identity_script + script_block
    elif insert_in_helper_suffix_function:
        new_script = identity_script.replace(
            "  helper_waypoint =", script_block + "  helper_waypoint ="
        )
    else:
        new_script = identity_script.replace(
            "  waypoint =", script_block + "  waypoint ="
        )
    if urp_xml_comment_only:
        new_urp_script = identity_script
    elif insert_in_helper_suffix_function:
        new_urp_script = identity_script.replace(
            "  helper_waypoint =", (urp_block or script_block) + "  helper_waypoint ="
        )
    else:
        new_urp_script = identity_script.replace(
            "  waypoint =", (urp_block or script_block) + "  waypoint ="
        )
    current_metadata = (
        '<Metadata crcValue="INNER_OLD" />\n' if change_nonroot_crc else ""
    )
    candidate_metadata = (
        '<Metadata crcValue="INNER_NEW" />\n' if change_nonroot_crc else ""
    )
    current_urp = (
        f'<URProgram name="{current_program}" directory="{FIXTURE_CONTROLLER_DIR}" '
        f'installationRelativePath="{posixpath.relpath("/programs/default", FIXTURE_CONTROLLER_DIR)}" '
        f'crcValue="100">\n'
        f"{current_metadata}"
        f"<cachedContents>{html.escape(old_script, quote=True)}</cachedContents>\n"
        f"<file resolves-to=\"file\">{FIXTURE_CONTROLLER_DIR}/{current_program}.script</file>\n"
        "</URProgram>\n"
    )
    new_urp = (
        f'<URProgram name="{next_program}" directory="{FIXTURE_CONTROLLER_DIR}" '
        f'installationRelativePath="{posixpath.relpath("/programs/default", FIXTURE_CONTROLLER_DIR)}" '
        f'crcValue="200">\n'
        f"{candidate_metadata}"
        f"<cachedContents>{html.escape(new_urp_script, quote=True)}</cachedContents>\n"
        f"<file resolves-to=\"file\">{FIXTURE_CONTROLLER_DIR}/{next_program}.script</file>\n"
        "</URProgram>\n"
    )
    if urp_xml_comment_only:
        fake_cached = (
            f"<cachedContents>def codex_{next_program}():\n"
            f"{urp_representation}end\n"
            f"codex_{next_program}()\n</cachedContents>"
        )
        new_urp = f"<!-- {fake_cached} -->\n" + new_urp
    if append_after_program:
        new_urp = new_urp.replace(
            "</URProgram>", urp_representation + "</URProgram>"
        )
    current_paths = {
        ".script": current / f"{current_program}.script",
        ".txt": current / f"{current_program}.txt",
        ".urp": current / f"{current_program}.urp",
    }
    candidate_paths = {
        extension: candidate / f"{next_program}{extension}"
        for extension in (".script", ".txt", ".urp")
    }
    current_paths[".script"].write_text(old_script, encoding="utf-8")
    current_paths[".txt"].write_text(
        f"Version:\n  {SOURCE_STAMP}\n  {FIXTURE_CONTROLLER_DIR}/{current_program}.urp\n",
        encoding="utf-8",
    )
    current_paths[".urp"].write_bytes(gzip.compress(current_urp.encode(), mtime=0))
    candidate_paths[".script"].write_text(new_script, encoding="utf-8")
    candidate_paths[".txt"].write_text(
        f"Version:\n  {CANDIDATE_STAMP}\n  {FIXTURE_CONTROLLER_DIR}/{next_program}.urp\n",
        encoding="utf-8",
    )
    candidate_paths[".urp"].write_bytes(
        gzip.compress(new_urp.encode(), mtime=0)
    )

    receipt = tmp_path / "snapshot-receipt.json"
    receipt_payload = {
        "schema": RECEIPT_SCHEMA,
        "host": "192.168.1.18",
        "controller_directory": FIXTURE_CONTROLLER_DIR,
        "basename": current_program,
        "output_dir": str(current),
        "receipt_path": str(receipt),
        "captured_at": "2026-07-17T08:00:00Z",
        "source_class": "fresh_controller_snapshot",
        "files": [
            {
                "filename": current_paths[extension].name,
                "path": str(current_paths[extension]),
                "sha256": hashlib.sha256(
                    current_paths[extension].read_bytes()
                ).hexdigest(),
            }
            for extension in (".script", ".txt", ".urp")
        ],
    }
    receipt.write_text(json.dumps(receipt_payload), encoding="utf-8")

    normalized = {}
    counts = {}
    block_hashes = {}
    for extension, path in candidate_paths.items():
        try:
            inspected = inspect_content(
                path,
                current_program=current_program,
                candidate_program=next_program,
                source_stamp=SOURCE_STAMP,
                candidate_stamp=CANDIDATE_STAMP,
                controller_directory=FIXTURE_CONTROLLER_DIR,
                require_executable_context=not (
                    append_after_program
                    or insert_in_helper_suffix_function
                    or insert_in_dead_branch
                    or insert_after_top_level_halt
                    or urp_xml_comment_only
                ),
            )
        except ValueError:
            normalized[extension] = "0" * 64
            counts[extension] = 0
            block_hashes[extension] = {}
        else:
            normalized[extension] = hashlib.sha256(inspected.normalized).hexdigest()
            counts[extension] = len(inspected.blocks)
            block_hashes[extension] = {
                block.block_id: hashlib.sha256(block.payload).hexdigest()
                for block in inspected.blocks
            }
    candidate_sha = {
        extension: hashlib.sha256(path.read_bytes()).hexdigest()
        for extension, path in candidate_paths.items()
    }
    deploy_manifest = candidate / f"{next_program}.deploy-manifest.json"
    deploy_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "basename": next_program,
                "controller_directory": FIXTURE_CONTROLLER_DIR,
                "artifacts": [
                    {
                        "filename": candidate_paths[extension].name,
                        "source": candidate_paths[extension].name,
                        "sha256": candidate_sha[extension],
                    }
                    for extension in (".script", ".txt", ".urp")
                ],
            }
        ),
        encoding="utf-8",
    )
    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "schema": ATTESTATION_SCHEMA,
                "source_class": "fresh_controller_snapshot",
                "promotable": True,
                "blocked_reason": None,
                "source_receipt": {
                    "schema": RECEIPT_SCHEMA,
                    "path": str(receipt),
                    "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                    "host": "192.168.1.18",
                    "controller_directory": FIXTURE_CONTROLLER_DIR,
                    "basename": current_program,
                    "output_dir": str(current),
                    "captured_at": "2026-07-17T08:00:00Z",
                    "source_class": "fresh_controller_snapshot",
                },
                "source_program": current_program,
                "candidate_program": next_program,
                "controller_directory": FIXTURE_CONTROLLER_DIR,
                "source_stamp": SOURCE_STAMP,
                "candidate_stamp": CANDIDATE_STAMP,
                "allowed_changes": [
                    "program_identity",
                    "source_stamp_identity",
                    "urp_crcValue",
                    "canonical_watchdog_block",
                ],
                "control_math_changed": False,
                "trajectory_changed": False,
                "waypoint_changed": False,
                "command_order_changed": False,
                "candidate_sha256": candidate_sha,
                "fetched_controller_sha256": {
                    extension: hashlib.sha256(path.read_bytes()).hexdigest()
                    for extension, path in current_paths.items()
                },
                "normalized_content_sha256": normalized,
                "watchdog_block_count": counts,
                "watchdog_block_sha256": block_hashes,
                "canonical_watchdog": {
                    "block_id": BLOCK_ID,
                    "sha256": hashlib.sha256(
                        CANONICAL_BLOCK_PATH.read_bytes()
                    ).hexdigest(),
                },
                "urp_crcValue": {
                    "source": ET.fromstring(
                        gzip.decompress(current_paths[".urp"].read_bytes())
                    ).get("crcValue"),
                    "candidate": ET.fromstring(
                        gzip.decompress(candidate_paths[".urp"].read_bytes())
                    ).get("crcValue"),
                },
                "deploy_manifest": {
                    "path": str(deploy_manifest),
                    "sha256": hashlib.sha256(
                        deploy_manifest.read_bytes()
                    ).hexdigest(),
                    "schema": "ur10e.controller.deployment-manifest/v1",
                    "promotable": True,
                },
            }
        ),
        encoding="utf-8",
    )
    return current, candidate, attestation, candidate_paths


def _run_gate(
    current: Path, candidate: Path, attestation: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/verify_step5d_tp_watchdog_diff.py"),
            "--current-readback",
            str(current),
            "--candidate",
            str(candidate),
            "--program",
            NEXT_PROGRAM,
            "--attestation",
            str(attestation),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_full_tp_triplet_diff_gate_accepts_exact_reviewed_block_in_script_and_urp(
    tmp_path: Path,
) -> None:
    canonical = load_canonical_blocks()
    assert tuple(canonical) == (BLOCK_ID,)
    assert canonical[BLOCK_ID].payload == CANONICAL_BLOCK_PATH.read_bytes()
    assert canonical[BLOCK_ID].sha256 == hashlib.sha256(
        CANONICAL_BLOCK_PATH.read_bytes()
    ).hexdigest()

    current, candidate, attestation, candidate_paths = _write_triplet_fixture(tmp_path)
    assert html.escape(CANONICAL_BLOCK, quote=True).encode() in gzip.decompress(
        candidate_paths[".urp"].read_bytes()
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "tp_watchdog_diff_gate=pass"


@pytest.mark.parametrize(
    ("case", "block"),
    (
        (
            "movej",
            CANONICAL_BLOCK.replace("    halt\n", "    movej(home_q)\n    halt\n"),
        ),
        (
            "speedj",
            CANONICAL_BLOCK.replace(
                "    halt\n", "    speedj(qdot, 0.5, 0.002)\n    halt\n"
            ),
        ),
        (
            "control_law",
            CANONICAL_BLOCK.replace(
                "    halt\n", "    local qdot = force_error * 0.1\n    halt\n"
            ),
        ),
            (
                "comment_only",
                "  # AUTOTUNE_WATCHDOG_V2_BEGIN host_heartbeat_fail_closed_v2\n"
                "  # if host_heartbeat_timeout:\n"
                "  # host_heartbeat_timeout_at_home\n"
                "  # host_heartbeat_timeout_unknown_home\n"
                "  # AUTO_HOME_AFTER_HEARTBEAT_LOSS: false\n"
                "  # halt\n"
                "  # AUTOTUNE_WATCHDOG_V2_END host_heartbeat_fail_closed_v2\n",
        ),
    ),
)
def test_tp_diff_gate_rejects_noncanonical_marker_bodies(
    tmp_path: Path, case: str, block: str
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path / case,
        script_block=block,
        urp_block=block,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "bytes/hash differ from reviewed canonical source" in completed.stderr
    assert "tp_watchdog_diff_gate=pass" not in completed.stdout


def test_tp_diff_gate_rejects_script_urp_block_disagreement(tmp_path: Path) -> None:
    changed_urp = CANONICAL_BLOCK.replace(
        'textmsg("host_heartbeat_timeout_unknown_home_controlled_stop")',
        'textmsg("host_heartbeat_timeout_at_verified_home")',
    )
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        urp_block=changed_urp,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert f".urp:{BLOCK_ID}" in completed.stderr


def test_tp_diff_gate_rejects_exact_block_outside_executable_program(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        append_after_program=True,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "not inside the exact candidate program function" in completed.stderr


def test_tp_diff_gate_rejects_exact_block_in_helper_with_candidate_name_suffix(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        insert_in_helper_suffix_function=True,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "not inside the exact candidate program function" in completed.stderr


def test_tp_diff_gate_rejects_exact_block_in_dead_nested_branch(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        insert_in_dead_branch=True,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "nested or conditionally unreachable scope" in completed.stderr


def test_tp_diff_gate_does_not_normalize_program_text_inside_guard_logic(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        change_program_named_guard_string=True,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "changes content outside identity/watchdog blocks" in completed.stderr


def test_tp_diff_gate_rejects_exact_block_after_top_level_halt(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        insert_after_top_level_halt=True,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "dead after a top-level halt/return" in completed.stderr


def test_tp_diff_gate_rejects_block_in_xml_comment_with_fake_cached_contents(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        urp_xml_comment_only=True,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "outside actual cachedContents text" in completed.stderr


def test_tp_diff_gate_normalizes_only_root_program_crc_attribute(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        change_nonroot_crc=True,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "changes content outside identity/watchdog blocks" in completed.stderr


def test_tp_diff_gate_requires_attested_canonical_block_hashes(tmp_path: Path) -> None:
    current, candidate, attestation, _ = _write_triplet_fixture(tmp_path)
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    payload["watchdog_block_sha256"][".script"][BLOCK_ID] = "0" * 64
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "canonical-block SHA attestation drift" in completed.stderr


def _verified_v1_snapshot(
    tmp_path: Path, *, source_class: str
) -> tuple[Path, Path]:
    source_dir = tmp_path / "snapshot"
    source_dir.mkdir(parents=True)
    retained = ROOT / "programs/step5/step5d"
    for extension in (".script", ".txt", ".urp"):
        name = f"{SOURCE_PROGRAM}{extension}"
        (source_dir / name).write_bytes((retained / name).read_bytes())
    receipt = tmp_path / "snapshot-receipt.json"
    if source_class == "stale_source_fixture":
        write_stale_fixture_receipt(
            triplet_dir=source_dir,
            receipt_path=receipt,
            captured_at="2026-07-15T03:51:00Z",
        )
    else:
        payload = {
            "schema": RECEIPT_SCHEMA,
            "host": "192.168.1.18",
            "controller_directory": "/programs/andyl/kunwei/step5",
            "basename": SOURCE_PROGRAM,
            "output_dir": str(source_dir),
            "receipt_path": str(receipt),
            "captured_at": "2026-07-17T08:15:00Z",
            "source_class": "fresh_controller_snapshot",
            "files": [
                {
                    "filename": f"{SOURCE_PROGRAM}{extension}",
                    "path": str(source_dir / f"{SOURCE_PROGRAM}{extension}"),
                    "sha256": hashlib.sha256(
                        (source_dir / f"{SOURCE_PROGRAM}{extension}").read_bytes()
                    ).hexdigest(),
                }
                for extension in (".script", ".txt", ".urp")
            ],
        }
        receipt.write_text(json.dumps(payload), encoding="utf-8")
    load_snapshot_receipt(receipt, source_dir)
    return source_dir, receipt


def _build_fixture(
    tmp_path: Path, *, source_class: str = "fresh_controller_snapshot"
) -> tuple[Path, Path, Path, dict[str, object]]:
    source_dir, receipt = _verified_v1_snapshot(
        tmp_path, source_class=source_class
    )
    candidate = tmp_path / "candidate"
    attestation = tmp_path / "diff-attestation.json"
    result = build_package(
        snapshot_receipt=receipt,
        snapshot_triplet_dir=source_dir,
        output_dir=candidate,
        v2_source_stamp=CANDIDATE_STAMP,
        diff_attestation=attestation,
    )
    return source_dir, candidate, attestation, result


def test_production_builder_happy_path_binds_fresh_receipt_and_exact_triplet(
    tmp_path: Path,
) -> None:
    source, candidate, attestation_path, result = _build_fixture(tmp_path)
    assert result["status"] == "local_candidate_unuploaded"
    assert result["promotable"] is True
    script_path = candidate / f"{CANDIDATE_PROGRAM}.script"
    txt_path = candidate / f"{CANDIDATE_PROGRAM}.txt"
    urp_path = candidate / f"{CANDIDATE_PROGRAM}.urp"
    assert all(path.is_file() and not path.is_symlink() for path in (script_path, txt_path, urp_path))
    script = script_path.read_text(encoding="utf-8")
    assert script.startswith(f"# VERSION: {CANDIDATE_STAMP}\n")
    assert f"def codex_{CANDIDATE_PROGRAM}():" in script
    assert script.rstrip().endswith(f"codex_{CANDIDATE_PROGRAM}()")
    assert script.count(CANONICAL_BLOCK) == 1
    assert "read_input_float_register(26)" in CANONICAL_BLOCK
    assert "local watchdog_timeout_s = 0.100" in CANONICAL_BLOCK
    assert "watchdog_timeout_s = 2.000" in CANONICAL_BLOCK
    assert "stopj(0.500)" in CANONICAL_BLOCK
    assert "movel(" not in CANONICAL_BLOCK
    assert "movej(" not in CANONICAL_BLOCK
    assert "speedl(" not in CANONICAL_BLOCK
    assert "speedj(" not in CANONICAL_BLOCK
    assert "force_mode(" not in CANONICAL_BLOCK
    assert "codex_should_auto_home" not in CANONICAL_BLOCK

    root = ET.fromstring(gzip.decompress(urp_path.read_bytes()))
    assert root.get("name") == CANDIDATE_PROGRAM
    assert root.get("directory") == "/programs/andyl/kunwei/step5"
    file_nodes = [node for node in root.iter() if node.tag == "file"]
    cached_nodes = [node for node in root.iter() if node.tag == "cachedContents"]
    assert len(file_nodes) == 1
    assert file_nodes[0].get("resolves-to") == "file"
    assert file_nodes[0].text == (
        f"/programs/andyl/kunwei/step5/{CANDIDATE_PROGRAM}.script"
    )
    assert len(cached_nodes) == 1
    assert cached_nodes[0].text == script
    assert CANDIDATE_STAMP in txt_path.read_text(encoding="utf-8")

    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert attestation["schema"] == ATTESTATION_SCHEMA
    assert attestation["source_class"] == "fresh_controller_snapshot"
    assert attestation["promotable"] is True
    assert attestation["blocked_reason"] is None
    assert attestation["source_receipt"]["output_dir"] == str(source)
    deploy = candidate / f"{CANDIDATE_PROGRAM}.deploy-manifest.json"
    deploy_payload = json.loads(deploy.read_text(encoding="utf-8"))
    assert set(deploy_payload) == {
        "schema_version",
        "basename",
        "controller_directory",
        "artifacts",
    }
    assert deploy_payload["basename"] == CANDIDATE_PROGRAM
    assert {
        row["filename"] for row in deploy_payload["artifacts"]
    } == {f"{CANDIDATE_PROGRAM}{extension}" for extension in (".script", ".txt", ".urp")}


def test_stale_v1_fixture_build_is_non_promotable_and_not_helper_compatible(
    tmp_path: Path,
) -> None:
    _, candidate, attestation_path, result = _build_fixture(
        tmp_path, source_class="stale_source_fixture"
    )
    assert result["status"] == "stale_source_fixture_non_promotable"
    assert result["promotable"] is False
    assert result["blocked_reason"] == "stale_source_fixture"
    deploy = json.loads(
        (candidate / f"{CANDIDATE_PROGRAM}.deploy-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert deploy["schema"] == "step5d.autotune.non-promotable-deploy-manifest/v1"
    assert deploy["promotable"] is False
    assert deploy["blocked_reason"] == "stale_source_fixture"
    assert "schema_version" not in deploy
    assert deploy["would_deploy"]["schema_version"] == 1
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert attestation["promotable"] is False
    assert attestation["blocked_reason"] == "stale_source_fixture"


def test_builder_rejects_receipt_sha_drift_unknown_field_path_escape_and_symlink(
    tmp_path: Path,
) -> None:
    source, receipt = _verified_v1_snapshot(
        tmp_path / "sha", source_class="fresh_controller_snapshot"
    )
    script = source / f"{SOURCE_PROGRAM}.script"
    script.write_bytes(script.read_bytes() + b"drift")
    with pytest.raises(BuildError, match="SHA256 drift"):
        build_package(
            snapshot_receipt=receipt,
            snapshot_triplet_dir=source,
            output_dir=tmp_path / "sha-candidate",
            v2_source_stamp=CANDIDATE_STAMP,
            diff_attestation=tmp_path / "sha-attestation.json",
        )

    source, receipt = _verified_v1_snapshot(
        tmp_path / "unknown", source_class="fresh_controller_snapshot"
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["remote_command"] = "id"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BuildError, match="unsupported fields"):
        load_snapshot_receipt(receipt, source)

    source, receipt = _verified_v1_snapshot(
        tmp_path / "escape", source_class="fresh_controller_snapshot"
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    payload["files"][0]["path"] = str(tmp_path / "escape.script")
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BuildError, match="escapes triplet_dir"):
        load_snapshot_receipt(receipt, source)

    source, receipt = _verified_v1_snapshot(
        tmp_path / "symlink", source_class="fresh_controller_snapshot"
    )
    script = source / f"{SOURCE_PROGRAM}.script"
    target = tmp_path / "source-target.script"
    target.write_bytes(script.read_bytes())
    script.unlink()
    script.symlink_to(target)
    with pytest.raises(BuildError, match="symlink"):
        load_snapshot_receipt(receipt, source)


def test_builder_rejects_stale_source_stamp_and_invalid_gzip_even_when_rehashed(
    tmp_path: Path,
) -> None:
    source, receipt = _verified_v1_snapshot(
        tmp_path / "stamp", source_class="fresh_controller_snapshot"
    )
    script = source / f"{SOURCE_PROGRAM}.script"
    script.write_bytes(
        script.read_bytes().replace(b"_AUTOTUNE_V1", b"_AUTOTUNE_V0", 1)
    )
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    for row in payload["files"]:
        if row["filename"].endswith(".script"):
            row["sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BuildError, match="first stamp"):
        build_package(
            snapshot_receipt=receipt,
            snapshot_triplet_dir=source,
            output_dir=tmp_path / "stamp-candidate",
            v2_source_stamp=CANDIDATE_STAMP,
            diff_attestation=tmp_path / "stamp-attestation.json",
        )

    source, receipt = _verified_v1_snapshot(
        tmp_path / "gzip", source_class="fresh_controller_snapshot"
    )
    urp = source / f"{SOURCE_PROGRAM}.urp"
    urp.write_bytes(b"not-gzip")
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    for row in payload["files"]:
        if row["filename"].endswith(".urp"):
            row["sha256"] = hashlib.sha256(urp.read_bytes()).hexdigest()
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BuildError, match="gzip"):
        build_package(
            snapshot_receipt=receipt,
            snapshot_triplet_dir=source,
            output_dir=tmp_path / "gzip-candidate",
            v2_source_stamp=CANDIDATE_STAMP,
            diff_attestation=tmp_path / "gzip-attestation.json",
        )


def test_diff_gate_rejects_waypoint_byte_drift_urp_path_cache_and_xml_escape(
    tmp_path: Path,
) -> None:
    current, candidate, attestation, paths = _write_triplet_fixture(
        tmp_path / "waypoint"
    )
    old = b"p[1,2,3,4,5,6]"
    new = b"p[9,2,3,4,5,6]"
    paths[".script"].write_bytes(paths[".script"].read_bytes().replace(old, new, 1))
    xml = gzip.decompress(paths[".urp"].read_bytes()).replace(old, new, 1)
    paths[".urp"].write_bytes(gzip.compress(xml, mtime=0))
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    for extension in (".script", ".urp"):
        payload["candidate_sha256"][extension] = hashlib.sha256(
            paths[extension].read_bytes()
        ).hexdigest()
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "outside identity/watchdog blocks" in completed.stderr

    current, candidate, attestation, paths = _write_triplet_fixture(
        tmp_path / "urp-path"
    )
    xml = gzip.decompress(paths[".urp"].read_bytes()).replace(
        f"{FIXTURE_CONTROLLER_DIR}/{NEXT_PROGRAM}.script".encode(),
        f"{FIXTURE_CONTROLLER_DIR}/wrong.script".encode(),
        1,
    )
    paths[".urp"].write_bytes(gzip.compress(xml, mtime=0))
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    payload["candidate_sha256"][".urp"] = hashlib.sha256(
        paths[".urp"].read_bytes()
    ).hexdigest()
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "Script node path/resolves-to mismatch" in completed.stderr

    current, candidate, attestation, paths = _write_triplet_fixture(
        tmp_path / "xml-escape"
    )
    xml = gzip.decompress(paths[".urp"].read_bytes()).replace(b"&quot;", b'"', 1)
    paths[".urp"].write_bytes(gzip.compress(xml, mtime=0))
    payload = json.loads(attestation.read_text(encoding="utf-8"))
    payload["candidate_sha256"][".urp"] = hashlib.sha256(
        paths[".urp"].read_bytes()
    ).hexdigest()
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "well-formed XML" in completed.stderr or "cachedContents" in completed.stderr


def test_diff_gate_rejects_duplicate_block_wrong_timeout_auto_home_and_speedl(
    tmp_path: Path,
) -> None:
    variants = {
        "duplicate": CANONICAL_BLOCK + CANONICAL_BLOCK,
        "wrong-timeout": CANONICAL_BLOCK.replace(
            "local watchdog_timeout_s = 0.100",
            "local watchdog_timeout_s = 0.200",
        ),
        "auto-home": CANONICAL_BLOCK.replace(
            "stopj(0.500)", "movej(watchdog_home_q)"
        ),
        "speedl": CANONICAL_BLOCK.replace(
            "stopj(0.500)", "speedl([0,0,0,0,0,0], 0.5, 0.1)"
        ),
    }
    for name, block in variants.items():
        current, candidate, attestation, _ = _write_triplet_fixture(
            tmp_path / name,
            script_block=block,
            urp_block=block,
        )
        completed = _run_gate(current, candidate, attestation)
        assert completed.returncode != 0, name
        assert "tp_watchdog_diff_gate=pass" not in completed.stdout


def test_canonical_validator_rejects_undefined_placeholder_and_bounded_symbol() -> None:
    placeholder = CANONICAL_BLOCK.replace(
        "      sync()\n",
        "      local watchdog_placeholder_copy = host_heartbeat_timeout\n      sync()\n",
        1,
    ).encode()
    with pytest.raises(ValueError, match="undefined placeholder"):
        _validate_canonical_source(
            WatchdogBlock(BLOCK_ID, placeholder, 0, len(placeholder)),
            source=CANONICAL_BLOCK_PATH,
        )

    undefined = CANONICAL_BLOCK.replace(
        "      sync()\n",
        "      if watchdog_never_defined:\n        sync()\n      end\n      sync()\n",
        1,
    ).encode()
    with pytest.raises(ValueError, match="undefined bounded identifiers"):
        _validate_canonical_source(
            WatchdogBlock(BLOCK_ID, undefined, 0, len(undefined)),
            source=CANONICAL_BLOCK_PATH,
        )


def test_canonical_manifest_rejects_runtime_policy_drift(tmp_path: Path) -> None:
    manifest_path = ROOT / "config/step5/tp_watchdog_v2.json"
    mutations = (
        ("heartbeat", "source_float_register", 27),
        ("heartbeat", "run_timeout_s", 0.2),
        ("heartbeat", "wait_ack_timeout_s", 3.0),
        ("restart_home_gate", "position_error_max_m", 0.004),
        ("wait_ack", "unknown_home_timeout_action", "auto_home"),
    )
    for index, (section, key, value) in enumerate(mutations):
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload[section][key] = value
        mutated = tmp_path / f"manifest-{index}.json"
        mutated.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="runtime policy drift"):
            load_canonical_blocks(mutated)
