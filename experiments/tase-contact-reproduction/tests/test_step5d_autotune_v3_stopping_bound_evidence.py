from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_v3_stopping_bound_evidence as builder  # noqa: E402


def test_current_stopping_bound_evidence_is_partial_and_fail_closed() -> None:
    document = builder.build_document()
    assert document["certified"] is False
    assert document["optimizer_eligible"] is False
    assert document["deployment_readback_sha256"] is None
    assert document["certification_authorization_sha256"] is None
    assert document["certification_binding_sha256"] is None
    assert document["plant_epoch"] is None
    assert document["measurement_sha256"] is None
    assert document["stopping_bound_fingerprint"] is None
    assert document["schema"].endswith("stopping-bound-evidence-v2")
    assert document["live_effect"] == "stopping_bound_none_fail_closed"
    components = {row["role"]: row for row in document["components"]}
    assert components["center_speed_bound_m_s"]["value"] == 0.003
    assert components["center_acceleration_bound_m_s2"]["value"] == 0.00015
    assert components["reaction_latency_s"]["status"] == "missing"
    assert components["minimum_deceleration_m_s2"]["status"] == "missing"
    assert components["numeric_margin_m"]["status"] == "missing"


def test_stopping_bound_artifact_check_is_byte_exact(tmp_path: Path) -> None:
    output = tmp_path / "stopping.json"
    assert builder.main(["--output", str(output)]) == 0
    assert builder.main(["--output", str(output), "--check"]) == 0
    output.write_bytes(output.read_bytes() + b" ")
    try:
        builder.main(["--output", str(output), "--check"])
    except SystemExit as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("mutated stopping evidence was accepted")
