from __future__ import annotations

import gzip
import hashlib
import html
import json
import xml.etree.ElementTree as ET

import build_step5d_tacdiffusion_simulation_probe_v2 as builder


STAMP = "2026-07-26TDIAGNOSTIC_STEP5D_TACDIFFUSION_DIRECT_TORQUE_SIMULATION_PROBE_V2_TEST"


def test_v2_is_independent_and_preserves_no_motion_boundary() -> None:
    script = builder.render_script(STAMP)
    assert "def vic_diag(diagnostic_base, reason, detail):" in script
    assert "def codex_step5d_direct_torque_stage25(diagnostic_base):" in script
    assert "local input_shape_ok = vic_input_valid(eq, stiffness, damping)" in script
    assert "local fixed_impedance_ok = vic_fixed_impedance_exact(stiffness, damping)" in script
    assert "vic_diag(diagnostic_base, 12, 1)" in script
    assert "vic_diag(diagnostic_base, failure_reason, 100 + joint)" in script
    assert "vic_diag(diagnostic_base, failure_reason, 400 + joint)" in script
    assert "codex_step5d_direct_torque_stage25(34)" in script
    assert "codex_step5d_direct_torque_stage25(36)" in script
    assert "codex_step5d_direct_torque_stage25()" not in script
    for forbidden in (
        "movel(",
        "movej(",
        "speedl(",
        "speedj(",
        "servoj(",
        "force_mode(",
        "zero_ftsensor",
        "set_tcp(",
        "set_payload(",
        "socket_open(",
    ):
        assert forbidden not in script


def test_v2_diagnostic_registers_are_phase_specific() -> None:
    script = builder.render_script(STAMP)
    assert "write_output_integer_register(32, 0)" in script
    assert "write_output_integer_register(33, 0)" in script
    assert "write_output_integer_register(34, 0)" in script
    assert "write_output_integer_register(35, 0)" in script
    assert "write_output_integer_register(36, 0)" in script
    assert "write_output_integer_register(37, 0)" in script
    assert "vic_diag(diagnostic_base, 14, 0)" not in script
    assert script.count("vic_safe_exit_tick(vic_zero_six(), diagnostic_base, 14)") == 1
    assert script.count("vic_safe_exit_tick(vic_zero_six(), diagnostic_base, 11)") == 1
    assert script.count("vic_safe_exit_tick(last_applied_feedforward, diagnostic_base, 13)") == 1


def test_v2_triplet_and_manifest_are_hash_closed(tmp_path) -> None:
    result = builder.write_triplet(tmp_path, STAMP)
    manifest = json.loads(
        (tmp_path / f"{builder.PROGRAM_NAME}.deploy.json").read_text(encoding="utf-8")
    )
    assert manifest["basename"] == builder.PROGRAM_NAME
    assert {item["sha256"] for item in manifest["artifacts"]} == set(result["sha256"].values())
    for suffix, digest in result["sha256"].items():
        path = tmp_path / f"{builder.PROGRAM_NAME}{suffix}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest

    urp = (tmp_path / f"{builder.PROGRAM_NAME}.urp").read_bytes()
    root = ET.fromstring(gzip.decompress(urp).decode("utf-8"))
    cached = next(node for node in root.iter() if node.tag == "cachedContents")
    script = (tmp_path / f"{builder.PROGRAM_NAME}.script").read_text(encoding="utf-8")
    assert html.unescape(cached.text or "") == script
