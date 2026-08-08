"""r008 IntegralLimitCoordinate must reach r004 desired_twist (no hard-coded 1.0)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r008.controller_seam import IntegralLimitCoordinate  # noqa: E402


def test_integral_limit_coordinate_default_is_one_fifty() -> None:
    assert IntegralLimitCoordinate().force_integral_limit_n_s == pytest.approx(150.0)


def test_greybox_default_matches_integral_limit_coordinate() -> None:
    from step5d_autotune_v4_r008.greybox.reconstruct import FORCE_INTEGRAL_LIMIT_N_S

    assert FORCE_INTEGRAL_LIMIT_N_S == pytest.approx(
        IntegralLimitCoordinate().force_integral_limit_n_s
    )


def test_calibrated_runtime_desired_twist_source_has_no_literal_one() -> None:
    pytest.importorskip("pinocchio")
    from step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime
    import textwrap

    source = textwrap.dedent(inspect.getsource(V4CalibratedRuntime.desired_twist))
    assert "force_integral_limit_n_s=1.0" not in source
    assert "self.force_integral_limit_n_s" in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "force_integral_limit_n_s":
            assert not isinstance(node.value, ast.Constant)
            text = ast.unparse(node.value)
            assert "self.force_integral_limit_n_s" in text
            return
    raise AssertionError("force_integral_limit_n_s kwarg missing from desired_twist")


def test_calibrated_runtime_stores_injected_limit() -> None:
    pytest.importorskip("pinocchio")
    from step5d_autotune_v4_r004.calibrated_runtime import V4CalibratedRuntime

    sig = inspect.signature(V4CalibratedRuntime.__init__)
    assert "force_integral_limit_n_s" in sig.parameters
    assert sig.parameters["force_integral_limit_n_s"].default == 1.0
