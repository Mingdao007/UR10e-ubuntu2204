from __future__ import annotations

import hashlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_manual_tp_v1 as manual  # noqa: E402
import promote_step5d_manual_release as promote  # noqa: E402


def _current_triplet() -> tuple[dict, str]:
    pointer = __import__("json").loads((ROOT / promote.MANUAL_POINTER).read_text())
    manifest = __import__("json").loads((ROOT / pointer["manifest_path"]).read_text())
    script = (ROOT / manifest["artifacts"][".script"]["path"]).read_text()
    return manifest, script


def test_manual_frozen_script_keeps_commit_last_and_indefinite_wait() -> None:
    manifest, rendered = _current_triplet()
    assert manifest["identity"]["parent_r009_commit"] == manual.PARENT_R009_COMMIT
    assert "while waiting_s < 30.000" in rendered
    assert "def codex_autotune_wait_for_manual_arm(" in rendered
    assert "batch_row_index != 1" in rendered
    start = rendered.index("def codex_autotune_write_state(")
    block = rendered[start : rendered.index("\nend", start) + 4]
    assert block.rstrip().endswith(
        "write_output_integer_register(30, consumed_command_seq)\nend"
    )
    state_index = block.index("write_output_integer_register(26, state)")
    commit_index = block.index(
        "write_output_integer_register(30, consumed_command_seq)"
    )
    assert state_index < commit_index
    for register in (24, 25, 27, 28, 29, 31, 32, 33, 34):
        assert block.index(f"write_output_integer_register({register},") < state_index


def test_manual_wait_can_remain_stationary_beyond_30_seconds() -> None:
    samples = [{"heartbeat": float(index)} for index in range(601)]
    result = manual.simulate_manual_home_wait(samples, step_s=0.1)
    assert result["outcome"] == "waiting"
    assert result["sample"] == 601


def test_manual_wait_fails_closed_on_stale_heartbeat_or_inexact_arm() -> None:
    stale = manual.simulate_manual_home_wait(
        [{"heartbeat": 1.0} for _ in range(12)], step_s=0.1
    )
    assert stale == {"outcome": "fault", "reason": 20, "sample": 11}
    inexact = manual.simulate_manual_home_wait(
        [
            {"heartbeat": 1.0},
            {"heartbeat": 2.0, "command": "arm", "identity_exact": False},
        ]
    )
    assert inexact == {"outcome": "fault", "reason": 21, "sample": 1}


def test_manual_immutable_triplet_is_internally_closed() -> None:
    manifest, _ = _current_triplet()
    assert set(manifest["artifacts"]) == {".script", ".txt", ".urp"}
    for extension, reference in manifest["artifacts"].items():
        assert hashlib.sha256((ROOT / reference["path"]).read_bytes()).hexdigest() == reference["sha256"]
