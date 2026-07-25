from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from prepare_step5_tp_layout import load_plan, prepare  # noqa: E402


class PrepareStep5TpLayoutTest(unittest.TestCase):
    def write_plan(self, root: Path, basename: str) -> Path:
        path = root / "layout.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "controller_root": "/programs/andyl/kunwei/step5",
                    "protected_autotune_release_min": 21,
                    "protected_root_basename": "step5d_strict_rnn_autotune_v3_r021",
                    "packages": [
                        {
                            "basename": basename,
                            "controller_present": True,
                            "source_controller_directory": "/programs/andyl/kunwei/step5",
                            "destination_controller_directory": (
                                "/programs/andyl/kunwei/step5/step5d/archive"
                            ),
                            "disposition": "archive",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def write_triplet(self, root: Path, basename: str) -> None:
        source = root / basename
        source.mkdir(parents=True)
        controller_dir = "/programs/andyl/kunwei/step5"
        script = "# VERSION: TEST\ndef test():\n  sync()\nend\n"
        (source / f"{basename}.script").write_text(script, encoding="utf-8")
        (source / f"{basename}.txt").write_text(
            f"Open {controller_dir}/{basename}.urp\n", encoding="utf-8"
        )
        xml = (
            f'<URProgram directory="{controller_dir}" '
            'installationRelativePath="../../../default">'
            "<children><Script>"
            f"<cachedContents>{script}</cachedContents>"
            f"<file>{controller_dir}/{basename}.script</file>"
            "</Script></children></URProgram>"
        )
        (source / f"{basename}.urp").write_bytes(
            gzip.compress(xml.encode("utf-8"), mtime=0)
        )

    def test_prepares_relocated_triplet_and_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            basename = "step5d_strict_rnn_autotune_v3_r017"
            plan = self.write_plan(root, basename)
            sources = root / "sources"
            self.write_triplet(sources, basename)

            result = prepare(plan, sources, root / "output", root / "manifests")

            self.assertEqual(result["package_count"], 1)
            self.assertEqual(result["controller_source_count"], 1)
            self.assertEqual(len(result["removal_manifests"]), 1)
            package = (
                root
                / "output"
                / "step5d"
                / "archive"
                / f"{basename}.urp"
            )
            xml = gzip.decompress(package.read_bytes()).decode("utf-8")
            self.assertIn(
                'directory="/programs/andyl/kunwei/step5/step5d/archive"', xml
            )
            self.assertTrue(
                (root / "manifests" / f"deploy-{basename}.json").is_file()
            )

    def test_accepts_r018_r019_r020_but_rejects_protected_r021_and_future(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for basename in (
                "step5d_strict_rnn_autotune_v3_r018",
                "step5d_strict_rnn_autotune_v3_r019",
                "step5d_strict_rnn_autotune_v3_r020",
            ):
                with self.subTest(basename=basename):
                    parsed = load_plan(self.write_plan(root, basename))
                    self.assertEqual(parsed["packages"][0]["basename"], basename)
            for basename in (
                "step5d_strict_rnn_autotune_v3_r021",
                "step5d_strict_rnn_autotune_v3_r022",
            ):
                with self.subTest(basename=basename):
                    with self.assertRaisesRegex(ValueError, "protected"):
                        load_plan(self.write_plan(root, basename))

    def test_accepts_sha_bound_delete_without_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = self.write_plan(root, "broken_v1")
            payload = json.loads(plan.read_text(encoding="utf-8"))
            payload["packages"][0]["destination_controller_directory"] = None
            payload["packages"][0]["disposition"] = "delete"
            plan.write_text(json.dumps(payload), encoding="utf-8")

            parsed = load_plan(plan)

            self.assertEqual(parsed["packages"][0]["disposition"], "delete")


if __name__ == "__main__":
    unittest.main()
