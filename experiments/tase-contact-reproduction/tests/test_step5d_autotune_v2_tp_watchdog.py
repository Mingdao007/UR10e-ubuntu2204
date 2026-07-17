from __future__ import annotations

import gzip
import hashlib
import html
import json
import subprocess
import sys
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
from verify_step5d_tp_watchdog_diff import (
    inspect_content,
    load_canonical_blocks,
    normalized_content,
)


BLOCK_ID = "host_heartbeat_fail_closed_v1"
NEXT_PROGRAM = "step5d_strict_rnn_autotune_v2"
CANONICAL_BLOCK_PATH = (
    ROOT / "config/step5/tp_watchdog_blocks/host_heartbeat_fail_closed_v1.script"
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
    current_program = "controller_current"
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
    urp_representation = html.escape(urp_block or script_block, quote=True)
    if append_after_program:
        new_script = old_script.replace(current_program, next_program) + script_block
    elif insert_in_helper_suffix_function:
        new_script = old_script.replace(current_program, next_program).replace(
            "  helper_waypoint =", script_block + "  helper_waypoint ="
        )
    else:
        new_script = old_script.replace(current_program, next_program).replace(
            "  waypoint =", script_block + "  waypoint ="
        )
    if urp_xml_comment_only:
        new_urp_script = old_script.replace(current_program, next_program)
    elif insert_in_helper_suffix_function:
        new_urp_script = old_script.replace(current_program, next_program).replace(
            "  helper_waypoint =", (urp_block or script_block) + "  helper_waypoint ="
        )
    else:
        new_urp_script = old_script.replace(current_program, next_program).replace(
            "  waypoint =", (urp_block or script_block) + "  waypoint ="
        )
    current_metadata = (
        '<Metadata crcValue="INNER_OLD" />\n' if change_nonroot_crc else ""
    )
    candidate_metadata = (
        '<Metadata crcValue="INNER_NEW" />\n' if change_nonroot_crc else ""
    )
    current_urp = (
        f'<URProgram name="{current_program}" crcValue="OLD">\n'
        f"{current_metadata}"
        f"<cachedContents>{html.escape(old_script, quote=True)}</cachedContents>\n"
        f"<file resolves-to=\"file\">/programs/demo/{current_program}.script</file>\n"
        "</URProgram>\n"
    )
    new_urp = (
        f'<URProgram name="{next_program}" crcValue="NEW">\n'
        f"{candidate_metadata}"
        f"<cachedContents>{html.escape(new_urp_script, quote=True)}</cachedContents>\n"
        f"<file resolves-to=\"file\">/programs/demo/{next_program}.script</file>\n"
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
        new_urp += urp_representation
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
        f"  /programs/demo/{current_program}.urp\n", encoding="utf-8"
    )
    current_paths[".urp"].write_bytes(gzip.compress(current_urp.encode(), mtime=0))
    candidate_paths[".script"].write_text(new_script, encoding="utf-8")
    candidate_paths[".txt"].write_text(
        f"  /programs/demo/{next_program}.urp\n", encoding="utf-8"
    )
    candidate_paths[".urp"].write_bytes(
        gzip.compress(new_urp.encode(), mtime=0)
    )

    normalized = {}
    counts = {}
    block_hashes = {}
    for extension, path in candidate_paths.items():
        try:
            inspected = inspect_content(
                path,
                current_program=current_program,
                candidate_program=next_program,
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
    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune.tp-watchdog-diff/v2",
                "control_math_changed": False,
                "trajectory_changed": False,
                "waypoint_changed": False,
                "candidate_sha256": {
                    extension: hashlib.sha256(path.read_bytes()).hexdigest()
                    for extension, path in candidate_paths.items()
                },
                "fetched_controller_sha256": {
                    extension: hashlib.sha256(path.read_bytes()).hexdigest()
                    for extension, path in current_paths.items()
                },
                "normalized_content_sha256": normalized,
                "watchdog_block_count": counts,
                "watchdog_block_sha256": block_hashes,
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
            "  # AUTOTUNE_WATCHDOG_V2_BEGIN host_heartbeat_fail_closed_v1\n"
            "  # if host_heartbeat_timeout:\n"
            "  # host_heartbeat_timeout_at_home\n"
            "  # host_heartbeat_timeout_unknown_home\n"
            "  # AUTO_HOME_AFTER_HEARTBEAT_LOSS: false\n"
            "  # halt\n"
            "  # AUTOTUNE_WATCHDOG_V2_END host_heartbeat_fail_closed_v1\n",
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
        'textmsg("host_heartbeat_timeout_unknown_home")',
        'textmsg("host_heartbeat_timeout_at_home")',
    )
    current, candidate, attestation, _ = _write_triplet_fixture(
        tmp_path,
        urp_block=changed_urp,
    )
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert ".urp:host_heartbeat_fail_closed_v1" in completed.stderr


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
    payload.pop("watchdog_block_sha256")
    attestation.write_text(json.dumps(payload), encoding="utf-8")
    completed = _run_gate(current, candidate, attestation)
    assert completed.returncode != 0
    assert "canonical-block SHA attestation drift" in completed.stderr
