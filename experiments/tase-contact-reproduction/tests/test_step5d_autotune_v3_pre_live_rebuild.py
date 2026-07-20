from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import rebuild_step5d_autotune_v3_pre_live_evidence as rebuild  # noqa: E402


def test_stage_table_rebuild_preserves_unrelated_bytes(tmp_path: Path) -> None:
    path = tmp_path / "step5_stage_table.json"
    original = """{
  "sentinel": {"compact": true},
  "stages": [
    {
      "id": "unrelated",
      "value": [1, 2, 3]
    },
    {
      "id": "step5d_strict_rnn_autotune_v3",
      "active": false,
      "execution_readiness": {"schema": "old"}
    }
  ]
}
"""
    path.write_text(original, encoding="utf-8")
    payload = json.loads(original)
    payload["stages"][1]["execution_readiness"] = {"schema": "new"}

    rebuilt = rebuild._stage_table_bytes(path, payload).decode("utf-8")

    assert '"sentinel": {"compact": true}' in rebuilt
    assert '"value": [1, 2, 3]' in rebuilt
    assert json.loads(rebuilt) == payload
    path.write_text(rebuilt, encoding="utf-8")
    assert rebuild._stage_table_bytes(path, payload).decode("utf-8") == rebuilt


def test_rebuild_is_byte_exact_for_repository_pre_live_state() -> None:
    assert rebuild.main(["--check"]) == 0


def test_rebuild_binds_the_versioned_active_tp_manifest() -> None:
    contract = json.loads(
        (ROOT / rebuild.CONTROL_CONTRACT_RELATIVE).read_text(encoding="utf-8")
    )
    program = contract["deployment_tp_identity"]["program"]
    manifest = (
        ROOT
        / "programs/step5/step5d"
        / f"{program}.deploy-manifest.json"
    )
    table = json.loads((ROOT / rebuild.STAGE_TABLE_RELATIVE).read_text(encoding="utf-8"))
    row = next(row for row in table["stages"] if row["id"] == rebuild.V3_STAGE_ID)

    assert row["package_delivery"]["tp_fingerprint"] == hashlib.sha256(
        manifest.read_bytes()
    ).hexdigest()
