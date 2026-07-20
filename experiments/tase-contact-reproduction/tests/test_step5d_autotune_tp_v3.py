from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp as v1  # noqa: E402
import build_step5d_autotune_tp_v3 as v3  # noqa: E402


def test_r001_direct_campaign_preserves_v1_kernel_and_retires_certification() -> None:
    rendered = v3.render_script()
    v3.validate_rendered_script(rendered)

    assert v3.PROGRAM_NAME == "step5d_strict_rnn_autotune_v3_r001"
    assert "# CONTROL_PROFILE_ID: step5d_strict_rnn_autotune_v1" in rendered
    assert hashlib.sha256(v1.render_script().encode()).hexdigest() in rendered
    assert "def codex_step5d_autotune_trial_v1(" in rendered
    assert "local entry_xy_pose = p[entry_x, entry_y, p_current[2]" in rendered
    assert "movel(entry_xy_pose, a=0.135, v=0.090, r=0.0)" in rendered
    assert "movel(entry_precontact_pose, a=0.060, v=0.040, r=0.0)" in rendered
    assert "movel(rise_pose, a=0.060, v=0.040, r=0.0)" in rendered
    assert "movel(transfer_pose, a=0.135, v=0.090, r=0.0)" in rendered
    assert "movel(target_pose, a=0.060, v=0.040, r=0.0)" in rendered
    assert "read_input_integer_register(30) == batch_row_index" in rendered
    assert "if stale_s2 > 0.020:" in rendered
    for forbidden in (
        "def codex_autotune_certification_stop(",
        "def codex_autotune_certification_return(",
        "command == 4",
        "command == 5",
        "command == 6",
        "codex_autotune_bounded_return_segment",
    ):
        assert forbidden not in rendered


def test_r001_triplet_is_exact_and_revision_is_immutable(tmp_path: Path) -> None:
    stamp = v3.source_stamp(
        datetime(2026, 7, 20, 13, 25, tzinfo=timezone(timedelta(hours=8)))
    )
    result = v3.write_triplet(tmp_path, stamp)
    basename = "step5d_strict_rnn_autotune_v3_r001"

    assert result["program"] == basename
    assert result["control_profile_id"] == "step5d_strict_rnn_autotune_v1"
    xml = gzip.decompress((tmp_path / f"{basename}.urp").read_bytes()).decode()
    assert f'name="{basename}"' in xml
    assert f"/programs/andyl/kunwei/step5/{basename}.script" in xml
    sanity = json.loads((tmp_path / f"{basename}.numeric-sanity.json").read_text())
    assert sanity["delta_class"] == "identity_precontact_prior_exact_batch_lifecycle_return_v2"
    assert sanity["stage25_stale_command_hold_s"] == 0.020
    assert sanity["return_segment_count"] == 3
    assert sanity["batch_row_policy"] == "rows_1_to_9_near_ready_row_10_campaign_home"

    with pytest.raises(FileExistsError, match="increment rNNN"):
        v3.write_triplet(tmp_path, stamp)
