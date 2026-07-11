#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from build_v29_portable_evidence_bundle import FILES, build_bundle, verify_bundle  # noqa: E402


class V29PortableEvidenceBundleTest(unittest.TestCase):
    def test_copy_is_byte_bound_and_tamper_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "bundle"
            source.mkdir()
            for index, name in enumerate(FILES):
                (source / name).write_bytes(f"file-{index}\n".encode())

            manifest = build_bundle(source, output)

            self.assertEqual(verify_bundle(output), [])
            self.assertTrue(manifest["source_read_only"])
            self.assertFalse(manifest["claim_boundary"]["live_accepted"])
            (output / FILES[0]).write_bytes(b"tampered\n")
            self.assertTrue(
                any("mismatch" in blocker for blocker in verify_bundle(output))
            )

    def test_builder_refuses_overwrite_and_missing_input(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            with self.assertRaises(FileNotFoundError):
                build_bundle(source, root / "bundle")
            for name in FILES:
                (source / name).write_text("ok", encoding="utf-8")
            build_bundle(source, root / "bundle")
            with self.assertRaises(FileExistsError):
                build_bundle(source, root / "bundle")


if __name__ == "__main__":
    unittest.main()
