from __future__ import annotations

import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAP = json.loads((ROOT / "config/ur10e_test_dependency_map_v1.json").read_text())


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    groups = MAP.get("resource_groups", {})
    for item in items:
        relative = Path(str(item.fspath)).resolve().relative_to(ROOT).as_posix()
        for group, paths in groups.items():
            if relative in paths:
                item.add_marker(getattr(pytest.mark, group))
                item.add_marker(pytest.mark.xdist_group(name=group))
