from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_step5d_autotune_start_hover_r002 as r002  # noqa: E402
import step5d_new_eoat as eoat_contract  # noqa: E402


def test_new_eoat_contract_matches_controller_values() -> None:
    eoat = eoat_contract.load_new_eoat()

    assert eoat.payload_kg == pytest.approx(0.413)
    assert eoat.cog_m == pytest.approx((0.0011, 0.0031, 0.0163))
    assert eoat.tcp_pose_m_rad == pytest.approx((0.0, 0.0, 0.0874, 0.0, 0.0, 0.0))
    assert eoat.program_z_delta_m == pytest.approx(0.0)
    assert len(eoat.controller_receipt_sha256) == 64


def test_new_eoat_contract_rejects_unbounded_tcp(tmp_path: Path) -> None:
    payload = json.loads(eoat_contract.DEFAULT_ARTIFACT.read_text(encoding="utf-8"))
    payload["tcp_pose_m_rad"][2] = 0.05914
    target = tmp_path / "eoat.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(eoat_contract.NewEoatError, match=r"TCP\[2\]"):
        eoat_contract.load_new_eoat(target)


def test_new_eoat_contract_has_real_get_receipt_not_self_assertion() -> None:
    payload = json.loads(eoat_contract.DEFAULT_ARTIFACT.read_text(encoding="utf-8"))

    assert "runtime_readback_passed" not in json.dumps(payload, sort_keys=True)
    assert payload["controller_get_receipt"]["required_schema"].endswith(
        "controller-get-receipt-v2"
    )
    assert payload["tcp_derivation"]["program_z_delta_mm"] == 0.0
    assert payload["tcp_derivation"]["unrounded_tcp_mm"] == pytest.approx(
        37.09917288991741 + 50.3
    )
    assert any(
        item["tcp_mm"] == 59.14 and item["status"] == "REVOKED"
        for item in payload["revocations"]
    )


def test_new_eoat_rejects_tampered_controller_get_receipt(tmp_path: Path) -> None:
    receipt = json.loads(
        (
            ROOT / "config/step5d/new_eoat_controller_readback_v2.json"
        ).read_text(encoding="utf-8")
    )
    receipt["readback"]["tcp_offset_m_rad"][2] = 0.05914
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    payload = json.loads(eoat_contract.DEFAULT_ARTIFACT.read_text(encoding="utf-8"))
    payload["controller_get_receipt"]["origin"] = str(receipt_path)
    import hashlib

    payload["controller_get_receipt"]["sha256"] = hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
    target = tmp_path / "eoat.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(eoat_contract.NewEoatError, match=r"controller TCP\[2\]"):
        eoat_contract.load_new_eoat(target)


def test_script1_r002_is_revoked_and_cannot_rebuild(tmp_path: Path) -> None:
    marker = r002.REVOKED_MARKER
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert payload["status"] == "REVOKED_PROTECTIVE_STOP"
    assert payload["load_or_play_allowed"] is False
    with pytest.raises(RuntimeError, match="protective stop"):
        r002.write_triplet(
            tmp_path,
            "2026-07-30T0300HKT_STEP5D_AUTOTUNE_START_HOVER_R002",
        )
