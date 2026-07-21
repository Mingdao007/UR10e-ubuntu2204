from __future__ import annotations

import hashlib
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp_v3 as r009  # noqa: E402
import build_step5d_manual_tp_v1 as manual  # noqa: E402


def test_manual_render_is_reversible_and_r009_is_unchanged() -> None:
    parent_before = r009.render_script()
    parent_sha = hashlib.sha256(parent_before.encode()).hexdigest()
    rendered = manual.render_script()
    manual.validate_rendered_script(rendered, parent=parent_before)
    assert hashlib.sha256(r009.render_script().encode()).hexdigest() == parent_sha
    assert "while waiting_s < 30.000" in rendered
    assert "def codex_autotune_wait_for_manual_arm(" in rendered
    assert "batch_row_index != 1" in rendered


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


def test_manual_triplet_is_internally_closed(tmp_path: Path) -> None:
    result = manual.write_triplet(tmp_path, "2026-07-21T1500HKT_TEST")
    assert result["program"] == manual.PROGRAM_NAME
    assert set(result["sha256"]) == {".script", ".txt", ".urp"}
    for extension, digest in result["sha256"].items():
        assert hashlib.sha256(Path(result["paths"][extension]).read_bytes()).hexdigest() == digest
