from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_step5d_layer_boundaries",
    ROOT / "tools/validate_step5d_layer_boundaries.py",
)
assert SPEC is not None and SPEC.loader is not None
BOUNDARIES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOUNDARIES)


def test_step5d_layers_have_no_forbidden_import_or_cycle() -> None:
    report = BOUNDARIES.validate()

    assert report["ok"] is True, report["violations"]
    assert report["violations"] == []
    assert report["policy"] == {
        "global_module_limit": None,
        "global_line_limit": None,
        "module_counts_are_report_only": True,
        "zero_cross_layer_cycles": True,
    }
    assert all(
        row["modules"] > 0 and row["source_lines"] > 0
        for row in report["metrics"].values()
    )
