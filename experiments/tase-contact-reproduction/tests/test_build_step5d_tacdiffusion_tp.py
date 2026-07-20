from __future__ import annotations

import gzip
import html
import re
import xml.etree.ElementTree as ET

import build_step5d_tacdiffusion_tp as builder


STAMP = "2026-07-20T1500HKT_TEST"


def test_render_is_deterministic_and_replaces_only_stage25_executor() -> None:
    first = builder.render_script(STAMP)
    second = builder.render_script(STAMP)
    assert first == second
    assert "write_output_float_register(35, 25.3)" in first
    assert "write_output_float_register(35, 25.95)" in first
    assert first.count("stop_reason = codex_step5d_direct_torque_stage25()") == 1
    assert "speedj([cmd_qd0" not in first
    assert "model_active_allowed = False" in first
    assert "vic_feedforward_zero(raw_feedforward)" in first


def test_render_uses_polyscope_compatible_return_and_absolute_value_syntax() -> None:
    script = builder.render_script(STAMP)
    assert re.search(r"^\s*return\s*$", script, flags=re.MULTILINE) is None
    assert re.search(r"(?<![A-Za-z0-9_])abs\(", script) is None
    assert "def vic_abs(value):" in script
    assert "return 13.0" in script


def test_faults_cannot_enter_automatic_return() -> None:
    script = builder.render_script(STAMP)
    wrapper = script.split("def codex_step5d_tacdiffusion_fixture_shadow_v1():", 1)[1]
    assert wrapper.count("if stop_reason == 1.0:") == 1
    assert wrapper.index("if stop_reason == 1.0:") < wrapper.index("codex_autotune_guarded_return(10")
    fault_branch = wrapper.split("  else:\n", 1)[1]
    assert "codex_autotune_guarded_return" not in fault_branch
    assert "codex_autotune_fault_forever" in fault_branch


def test_triplet_cached_contents_is_exact(tmp_path) -> None:
    result = builder.write_triplet(tmp_path, STAMP)
    urp = (tmp_path / f"{builder.PROGRAM_NAME}.urp").read_bytes()
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = next(node for node in root.iter() if node.tag == "cachedContents")
    script = (tmp_path / f"{builder.PROGRAM_NAME}.script").read_text(encoding="utf-8")
    assert html.unescape(cached.text or "") == script
    assert set(result["sha256"]) == {".script", ".txt", ".urp"}
