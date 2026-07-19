from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from relocate_ur_tp_triplet import relocate  # noqa: E402


class RelocateUrTpTripletTest(unittest.TestCase):
    def test_rewrites_directory_and_script_node_but_preserves_script(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            output = root / "archive"
            source.mkdir()
            basename = "failed_v1"
            old = "/programs/andyl/kunwei/step5"
            new = f"{old}/archive"
            script = "# VERSION: TEST\ndef codex_failed_v1():\n  sync()\nend\n"
            (source / f"{basename}.script").write_text(script, encoding="utf-8")
            (source / f"{basename}.txt").write_text(f"Open: {old}/{basename}.urp\n", encoding="utf-8")
            xml = (
                f'<URProgram directory="{old}" installationRelativePath="../../../default">'
                '<children><Script type="File">'
                f"<cachedContents>{script}</cachedContents>"
                f'<file resolves-to="file">{old}/{basename}.script</file>'
                "</Script></children></URProgram>"
            )
            (source / f"{basename}.urp").write_bytes(gzip.compress(xml.encode("utf-8"), mtime=0))

            result = relocate(source, output, basename, new)

            self.assertEqual(result["controller_directory"], new)
            self.assertEqual((output / f"{basename}.script").read_text(), script)
            self.assertIn(new, (output / f"{basename}.txt").read_text())
            relocated_xml = gzip.decompress((output / f"{basename}.urp").read_bytes()).decode()
            self.assertIn(f'directory="{new}"', relocated_xml)
            self.assertIn('installationRelativePath="../../../../default"', relocated_xml)
            self.assertIn(f"{new}/{basename}.script", relocated_xml)


if __name__ == "__main__":
    unittest.main()
