from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_v3_return_route_evidence as builder  # noqa: E402


def test_return_route_evidence_keeps_attended_certification_closed() -> None:
    document = builder.build_document()
    assert document["certified"] is False
    assert document["optimizer_eligible"] is False
    assert document["schema"].endswith("return-route-evidence-v2")
    assert document["offline_guards"]["phase_and_telemetry_capture"] is True
    assert document["policy"]["return_angular_speed_limit_rad_s"] == 0.05
    assert document["policy"]["return_angular_acceleration_limit_rad_s2"] == 0.1
    assert document["policy"]["return_controller_max_sample_gap_s"] == 0.004
    assert document["attended_controller_readback_sha256"] is None
    assert document["source_exact_return_telemetry_sha256"] is None
    assert document["certification_authorization_sha256"] is None


def test_return_route_evidence_check_is_byte_exact(tmp_path: Path) -> None:
    output = tmp_path / "return.json"
    assert builder.main(["--output", str(output)]) == 0
    assert builder.main(["--output", str(output), "--check"]) == 0
    output.write_bytes(output.read_bytes() + b" ")
    try:
        builder.main(["--output", str(output), "--check"])
    except SystemExit as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("mutated return-route evidence was accepted")
