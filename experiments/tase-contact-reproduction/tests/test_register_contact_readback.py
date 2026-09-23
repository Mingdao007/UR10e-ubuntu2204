from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from register_contact_readback import RegistrationError, register  # noqa: E402


PROGRAM = "step5d_contact_six_qp_v1"
TARGET_DIR = "/programs/andyl/kunwei/step5"
OLD_SELECTION = "report/yield-live-transition-v1/old-selection.json"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_case(root: Path, *, corrupt_basis: bool = False) -> tuple[Path, Path, Path]:
    local_dir = root / "programs" / PROGRAM
    local_dir.mkdir(parents=True)
    files = {
        ".script": local_dir / f"{PROGRAM}.script",
        ".txt": local_dir / f"{PROGRAM}.txt",
        ".urp": local_dir / f"{PROGRAM}.urp",
    }
    for extension, path in files.items():
        path.write_bytes(f"fixture-{extension}".encode())
    triplet = {extension: sha(path) for extension, path in files.items()}

    old_selection = root / OLD_SELECTION
    old_selection.parent.mkdir(parents=True)
    old_selection.write_text(json.dumps({"schema": "old-selection"}), encoding="utf-8")
    prior_sha = {".script": "1" * 64, ".txt": "2" * 64, ".urp": "3" * 64}
    current = {
        "program": PROGRAM,
        "current_stage_id": PROGRAM,
        "controller_target": f"{TARGET_DIR}/{PROGRAM}.urp",
        "controller_script": f"{TARGET_DIR}/{PROGRAM}.script",
        "controller_readback_manifest": OLD_SELECTION,
        "sha256": prior_sha,
        "status": "fresh_controller_readback_verified_pending_liveprep_admission",
        "evidence": {},
    }
    config = root / "config"
    config.mkdir()
    (config / "current_stage.json").write_text(json.dumps(current), encoding="utf-8")
    table = {
        "stages": [{
            "id": PROGRAM,
            "current_binding": {"controller_readback_manifest": OLD_SELECTION},
            "package_delivery": {},
        }]
    }
    (config / "step5_stage_table.json").write_text(json.dumps(table), encoding="utf-8")

    basis_path = root / "runs" / "prior" / "readback-validation.json"
    basis_path.parent.mkdir(parents=True)
    basis = {
        "pass": True,
        "state": "controller read-back verified",
        "sha": {"pairs": {
            role: {"local": triplet[extension], "readback": triplet[extension]}
            for role, extension in (("script", ".script"), ("txt", ".txt"), ("urp", ".urp"))
        }},
    }
    basis_path.write_text(json.dumps(basis), encoding="utf-8")
    basis_digest = sha(basis_path)
    if corrupt_basis:
        basis_path.write_text(json.dumps({"pass": False}), encoding="utf-8")

    manifest_path = root / "runs" / "fresh" / "delivery-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest = {
        "status": "controller read-back verified",
        "delivery_mode": "content_addressed_reuse",
        "fresh_controller_sha_verified": True,
        "fresh_controller_checked_at": "2026-09-23T17:00:00+08:00",
        "readback_source": "prior_full_readback",
        "skip_basis_manifest": basis_path.relative_to(root).as_posix(),
        "skip_basis_manifest_sha256": basis_digest,
        "program": PROGRAM,
        "local_dir": local_dir.relative_to(root).as_posix(),
        "readback_directory": "runs/fresh/controller-readback",
        "validation": {
            "program": PROGRAM,
            "target_dir": TARGET_DIR,
            "script_node_path": f"{TARGET_DIR}/{PROGRAM}.script",
            "installation_relative_path": "../../../default",
            "script_sha256": triplet[".script"],
            "txt_sha256": triplet[".txt"],
            "urp_sha256": triplet[".urp"],
        },
        "sha256": {
            key: dict(triplet) for key in ("local", "controller", "readback")
        },
        "target_resolution": {
            "controller_target": f"{TARGET_DIR}/{PROGRAM}.urp",
            "controller_directory": TARGET_DIR,
        },
        "safety_boundary": ["readback only"],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, old_selection, basis_path


class RegisterContactReadbackTest(unittest.TestCase):
    def test_content_addressed_reuse_promotes_fresh_readback_without_overwriting_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest, old_selection, _ = make_case(root)
            fixed_receipt = root / "report/yield-live-transition-v1/contact-readback-registration-20260921.json"
            fixed_receipt.write_text("preserve historical receipt", encoding="utf-8")
            selection = root / "report/yield-live-transition-v1/new-selection.json"

            result = register(
                root=root,
                manifest_path=manifest,
                selection_path=selection,
                old_selection_path=old_selection,
            )

            current = json.loads((root / "config/current_stage.json").read_text())
            table = json.loads((root / "config/step5_stage_table.json").read_text())
            selected = json.loads(selection.read_text())
            self.assertEqual(result["current_stage_updated"], True)
            self.assertEqual(current["controller_readback_manifest"], selection.relative_to(root).as_posix())
            self.assertEqual(current["sha256"], selected["sha256"]["local"])
            self.assertEqual(table["stages"][0]["package_delivery"]["delivery_mode"], "content_addressed_reuse")
            self.assertEqual(selected["readback_source"], "prior_full_readback")
            self.assertEqual(fixed_receipt.read_text(), "preserve historical receipt")
            self.assertTrue((root / result["registration_receipt"]).is_file())

    def test_content_addressed_reuse_rejects_basis_sha_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest, old_selection, _ = make_case(root, corrupt_basis=True)
            with self.assertRaisesRegex(RegistrationError, "basis manifest SHA differs"):
                register(
                    root=root,
                    manifest_path=manifest,
                    selection_path=root / "report/new-selection.json",
                    old_selection_path=old_selection,
                )


if __name__ == "__main__":
    unittest.main()
