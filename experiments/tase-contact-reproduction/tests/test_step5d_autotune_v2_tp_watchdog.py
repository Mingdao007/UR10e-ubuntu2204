from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.tp_watchdog import (
    TpPhase,
    WatchdogAction,
    compare_measured_home,
    decide_watchdog,
)
from verify_step5d_tp_watchdog_diff import normalized_content


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


def test_tp_diff_normalization_allows_only_bounded_watchdog_and_identity(
    tmp_path: Path,
) -> None:
    old = "def old_program():\n  waypoint = p[1,2,3,4,5,6]\nend\n"
    new = (
        "def new_program():\n"
        "  # AUTOTUNE_WATCHDOG_V2_BEGIN\n"
        "  # host_heartbeat_timeout_at_home\n"
        "  # AUTOTUNE_WATCHDOG_V2_END\n"
        "  waypoint = p[1,2,3,4,5,6]\nend\n"
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
    old_urp.write_bytes(gzip.compress(old.encode("utf-8"), mtime=0))
    new_urp.write_bytes(gzip.compress(new.encode("utf-8"), mtime=0))
    assert normalized_content(
        old_urp, current_program="old_program", candidate_program="new_program"
    )[0] == normalized_content(
        new_urp, current_program="old_program", candidate_program="new_program"
    )[0]

    new_script.write_text(new.replace("p[1,2,3,4,5,6]", "p[9,2,3,4,5,6]"), encoding="utf-8")
    assert normalized_content(
        new_script, current_program="old_program", candidate_program="new_program"
    )[0] != old_normalized


def test_full_tp_triplet_diff_gate_accepts_only_watchdog_identity_delta(
    tmp_path: Path,
) -> None:
    current = tmp_path / "current"
    candidate = tmp_path / "candidate"
    current.mkdir()
    candidate.mkdir()
    current_program = "controller_current"
    next_program = "step5d_strict_rnn_autotune_v2"
    old = (
        '<Program crcValue="OLD">\n'
        f"def {current_program}():\n"
        "  waypoint = p[1,2,3,4,5,6]\n"
        "end\n</Program>\n"
    )
    watchdog = (
        "  # AUTOTUNE_WATCHDOG_V2_BEGIN\n"
        "  # AUTOTUNE_WATCHDOG_V2\n"
        "  # host_heartbeat_timeout_at_home\n"
        "  # host_heartbeat_timeout_unknown_home\n"
        "  # AUTO_HOME_AFTER_HEARTBEAT_LOSS: false\n"
        "  # AUTOTUNE_WATCHDOG_V2_END\n"
    )
    new = old.replace(current_program, next_program).replace(
        "  waypoint =", watchdog + "  waypoint ="
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
    current_paths[".script"].write_text(old, encoding="utf-8")
    current_paths[".txt"].write_text(current_program + "\n", encoding="utf-8")
    current_paths[".urp"].write_bytes(gzip.compress(old.encode(), mtime=0))
    candidate_paths[".script"].write_text(new, encoding="utf-8")
    candidate_paths[".txt"].write_text(next_program + "\n", encoding="utf-8")
    candidate_paths[".urp"].write_bytes(
        gzip.compress(new.replace('crcValue="OLD"', 'crcValue="NEW"').encode(), mtime=0)
    )

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    normalized = {}
    counts = {}
    for extension, path in candidate_paths.items():
        content, count = normalized_content(
            path,
            current_program=current_program,
            candidate_program=next_program,
        )
        normalized[extension] = hashlib.sha256(content).hexdigest()
        counts[extension] = count
    attestation = tmp_path / "attestation.json"
    attestation.write_text(
        json.dumps(
            {
                "schema": "step5d.autotune.tp-watchdog-diff/v2",
                "control_math_changed": False,
                "trajectory_changed": False,
                "waypoint_changed": False,
                "fetched_controller_sha256": {
                    extension: sha(path) for extension, path in current_paths.items()
                },
                "candidate_sha256": {
                    extension: sha(path) for extension, path in candidate_paths.items()
                },
                "normalized_content_sha256": normalized,
                "watchdog_block_count": counts,
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/verify_step5d_tp_watchdog_diff.py"),
            "--current-readback",
            str(current),
            "--candidate",
            str(candidate),
            "--program",
            next_program,
            "--attestation",
            str(attestation),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "tp_watchdog_diff_gate=pass"
