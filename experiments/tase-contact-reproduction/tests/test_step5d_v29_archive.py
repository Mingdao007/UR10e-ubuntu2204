from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import kunwei_rtde_bridge as bridge  # noqa: E402
import step5d_runtime_interface as runtime  # noqa: E402


PROFILE = "step5d_strict_rnn_ablation_v29"
REPLACEMENT = "step5d_strict_rnn_autotune_v3_r012"
TOMBSTONE = (
    ROOT
    / "programs/step5/step5d/archive"
    / f"{PROFILE}.archive.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Step5dV29ArchiveTest(unittest.TestCase):
    def test_tombstone_closes_active_paths_and_preserves_bytes(self) -> None:
        payload = json.loads(TOMBSTONE.read_text(encoding="utf-8"))

        self.assertEqual(payload["reason"], "ARCHIVED_PROFILE")
        self.assertEqual(payload["replacement"], REPLACEMENT)
        for item in payload["files"]:
            archived = ROOT / item["new_path"]
            self.assertTrue(archived.is_file())
            self.assertEqual(_sha256(archived), item["sha256"])
            for old_path in item["old_paths"]:
                self.assertFalse((ROOT / old_path).exists())

    def test_mutable_projections_revoke_v29_execution_authority(self) -> None:
        current = json.loads((ROOT / "config/current_stage.json").read_text(encoding="utf-8"))
        candidate = current["v29_contact_candidate"]
        table = json.loads((ROOT / "config/step5_stage_table.json").read_text(encoding="utf-8"))
        row = next(item for item in table["stages"] if item.get("id") == PROFILE)

        self.assertTrue(candidate["archived"])
        self.assertFalse(candidate["executable"])
        self.assertTrue(candidate["reactivation_forbidden"])
        self.assertEqual(candidate["replacement"], REPLACEMENT)
        self.assertFalse(row["bridge"])
        self.assertFalse(row["completion_target"])
        self.assertEqual(row["owner"], "archive")
        self.assertFalse(row["lifecycle"]["current_candidate"])
        self.assertTrue(row["lifecycle"]["retained_evidence"])
        self.assertFalse(row["operator_lifecycle"]["executable"])
        self.assertFalse(row["runtime_interface_ref"]["selectable"])

    def test_raw_bridge_and_runtime_cli_refuse_before_device_access(self) -> None:
        with patch.object(bridge.socket, "create_connection") as connect:
            self.assertEqual(bridge.main(["--step4e-version", PROFILE]), 64)
        connect.assert_not_called()

        self.assertEqual(
            runtime.main(
                [
                    "--root",
                    str(ROOT),
                    "--program",
                    PROFILE,
                    "interface-json",
                ]
            ),
            64,
        )

    def test_operator_launcher_refuses_v29(self) -> None:
        completed = subprocess.run(
            ["bash", str(ROOT / "scripts/bridge-line-operator.sh"), "status"],
            cwd=ROOT,
            env={**os.environ, "BRIDGE_PROFILE": PROFILE},
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(completed.returncode, 64)
        self.assertIn("ARCHIVED_PROFILE", completed.stderr)
        self.assertIn(REPLACEMENT, completed.stderr)

    def test_flow_names_r012_release_contract_and_v29_archive(self) -> None:
        flow = (ROOT / "STEP5_FLOW.md").read_text(encoding="utf-8")
        v29_row = next(line for line in flow.splitlines() if line.startswith(f"| `{PROFILE}`"))

        self.assertIn("governed TP identity revision is r012", flow)
        self.assertIn("`RELEASE_CONTRACT_PROVEN`", flow)
        self.assertNotIn("production qualification gate", flow)
        self.assertNotIn("`release-certify`", flow)
        self.assertIn("ARCHIVED_PROFILE", v29_row)
        self.assertIn(REPLACEMENT, v29_row)


if __name__ == "__main__":
    unittest.main()
