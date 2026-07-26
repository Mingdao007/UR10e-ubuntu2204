from __future__ import annotations

import gzip
import hashlib
import html
import json
import xml.etree.ElementTree as ET

import build_step5d_tacdiffusion_simulation_probe_v2 as v2_builder
import build_step5d_tacdiffusion_simulation_probe_v3 as builder


STAMP = "2026-07-26TDIAGNOSTIC_STEP5D_TACDIFFUSION_DIRECT_TORQUE_SIMULATION_PROBE_V3_TEST"


def test_v3_binds_ack_pacing_without_changing_controller_stage() -> None:
    script = builder.render_script(STAMP)
    v2_stage = v2_builder.diagnostic_stage_source()
    assert "# SEQUENCE_PACING: ack_gated_host_driver; controller_sequence_guard_unchanged" in script
    assert v2_stage in script
    assert "codex_step5d_tacdiffusion_simulation_probe_v3()" in script
    assert "codex_step5d_tacdiffusion_simulation_probe_v2()" not in script
    assert "def codex_step5d_direct_torque_stage25(diagnostic_base):" in script
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


def test_v3_triplet_and_manifest_are_hash_closed(tmp_path) -> None:
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
