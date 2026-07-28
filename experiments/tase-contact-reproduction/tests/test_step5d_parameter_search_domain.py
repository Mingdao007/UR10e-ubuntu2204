from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_parameter_search_domain import (  # noqa: E402
    MIN_SEARCH_FORCE_DAMPING,
    production_candidate_catalog,
    require_search_candidate,
)


def test_catalog_never_contains_damping_below_five() -> None:
    catalog = production_candidate_catalog()
    assert catalog
    assert min(row.force_damping for row in catalog) >= MIN_SEARCH_FORCE_DAMPING
    assert min(row.force_damping for row in catalog) == pytest.approx(
        5.886274906776001
    )


def test_historical_candidate_remains_decodable_but_not_searchable() -> None:
    historical = ForceCandidate.from_log2(
        p=0.0,
        damping=-0.5,
        i=0.0,
    )
    assert historical.force_damping == pytest.approx(4.949747468305833)
    with pytest.raises(ValueError, match="below the 5 search floor"):
        require_search_candidate(historical, role="test candidate")
