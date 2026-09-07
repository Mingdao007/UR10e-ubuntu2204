"""Focused R013 Sobol compatibility checks for current SciPy releases."""

from pathlib import Path
import sys

import pytest
from scipy.stats import qmc


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r013.floor_coordinator import (  # noqa: E402
    _fast_forward_if_needed,
)


@pytest.mark.parametrize("skip", [0, 1, 17, 128])
def test_skip_matches_direct_sobol_prefix_slice(skip: int) -> None:
    seed = 13014
    count = 9

    expected_engine = qmc.Sobol(d=6, scramble=True, seed=seed)
    expected = expected_engine.random(skip + count)[skip:]

    actual_engine = qmc.Sobol(d=6, scramble=True, seed=seed)
    _fast_forward_if_needed(actual_engine, skip)
    actual = actual_engine.random(count)

    assert actual.tolist() == expected.tolist()


def test_zero_skip_does_not_call_fast_forward() -> None:
    class Engine:
        def fast_forward(self, _skip: int) -> None:
            raise AssertionError("zero skip must not call fast_forward")

    _fast_forward_if_needed(Engine(), 0)


def test_nonzero_skip_preserves_fast_forward_call() -> None:
    class Engine:
        seen: list[int]

        def __init__(self) -> None:
            self.seen = []

        def fast_forward(self, skip: int) -> None:
            self.seen.append(skip)

    engine = Engine()
    _fast_forward_if_needed(engine, 17)
    assert engine.seen == [17]
