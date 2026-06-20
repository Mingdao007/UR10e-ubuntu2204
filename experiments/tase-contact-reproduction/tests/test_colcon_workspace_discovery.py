#!/usr/bin/env python3
from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
ALLOWED_SOURCE_ROOT = WORKSPACE / "src"
IGNORED_ROOT_NAMES = {".git", "build", "install", "log"}


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _has_colcon_ignore_between(path: Path, stop: Path) -> bool:
    current = path.parent
    while current != stop and current != current.parent:
        if (current / "COLCON_IGNORE").is_file():
            return True
        current = current.parent
    return (stop / "COLCON_IGNORE").is_file()


class ColconWorkspaceDiscoveryTest(unittest.TestCase):
    def test_non_src_package_metadata_is_hidden_from_colcon_discovery(self) -> None:
        leaks: list[str] = []
        for package_xml in WORKSPACE.rglob("package.xml"):
            relative_parts = package_xml.relative_to(WORKSPACE).parts
            if any(part in IGNORED_ROOT_NAMES for part in relative_parts):
                continue
            if _is_under(package_xml, ALLOWED_SOURCE_ROOT):
                continue
            if not _has_colcon_ignore_between(package_xml, WORKSPACE):
                leaks.append(str(package_xml.relative_to(WORKSPACE)))

        self.assertEqual(leaks, [], "non-src package.xml files must be shielded by COLCON_IGNORE")


if __name__ == "__main__":
    unittest.main()
