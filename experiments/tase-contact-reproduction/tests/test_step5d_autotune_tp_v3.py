from __future__ import annotations

import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_tp as v1  # noqa: E402
import build_step5d_autotune_tp_v3 as v3  # noqa: E402


def test_v3_script_is_identity_only_delta_from_frozen_v1() -> None:
    rendered = v3.render_script()
    v3.validate_rendered_script(rendered)
    assert "# CONTROL_PROFILE_ID: step5d_strict_rnn_autotune_v1" in rendered
    assert "def codex_step5d_autotune_trial_v1(" in rendered
    assert "def codex_step5d_strict_rnn_autotune_v3():" in rendered
    assert rendered.count("read_input_integer_register(26)") >= 2
    assert hashlib.sha256(v1.render_script().encode()).hexdigest() in rendered


def test_v3_triplet_has_exact_program_cache_and_stamp(tmp_path: Path) -> None:
    stamp = v3.source_stamp(
        datetime(2026, 7, 19, 0, 30, tzinfo=timezone(timedelta(hours=8)))
    )
    result = v3.write_triplet(tmp_path, stamp)
    assert result["program"] == "step5d_strict_rnn_autotune_v3"
    assert result["control_profile_id"] == "step5d_strict_rnn_autotune_v1"
    xml = gzip.decompress(
        (tmp_path / "step5d_strict_rnn_autotune_v3.urp").read_bytes()
    ).decode()
    assert 'name="step5d_strict_rnn_autotune_v3"' in xml
    assert "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3.script" in xml
    assert stamp in xml
    assert result["checks"]["cached script"] is True
    sanity = json.loads(
        (tmp_path / "step5d_strict_rnn_autotune_v3.numeric-sanity.json").read_text()
    )
    assert sanity["identity_only_delta_from_frozen_v1"] is True
    assert sanity["qdot_cap_rad_s"] == 0.5
    assert sanity["precontact_entry_speed_m_s"] == 0.09
