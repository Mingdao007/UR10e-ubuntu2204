from __future__ import annotations

from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_autotune_optimizer import candidate_vector  # noqa: E402
from step5d_parameter_search_domain import (  # noqa: E402
    MIN_SEARCH_FORCE_DAMPING,
    MOTION_KP_LATTICE,
    ORIENTATION_KO_LATTICE,
    augment_catalog_with_orientation_anchors,
    damping_variants,
    orientation_variants,
    production_candidate_catalog,
    require_search_candidate,
)


def test_catalog_bootstrap_support_is_not_the_damping_acceptance_boundary() -> None:
    catalog = production_candidate_catalog()
    assert catalog
    assert min(row.force_damping for row in catalog) == pytest.approx(
        0.109375
    )
    assert MIN_SEARCH_FORCE_DAMPING == 0.0


def test_candidate_above_point_one_is_searchable() -> None:
    candidate = ForceCandidate.from_log2(
        p=0.0,
        damping=-6.0,
        i=0.0,
    )
    assert candidate.force_damping == pytest.approx(0.109375)
    assert require_search_candidate(candidate, role="test candidate") is candidate
    assert candidate.within_production_search_envelope()


def test_candidate_below_point_one_is_searchable_without_a_hard_floor() -> None:
    candidate = ForceCandidate.from_log2(
        p=0.0,
        damping=-6.25,
        i=0.0,
    )
    assert candidate.force_damping == pytest.approx(0.09197304541837502)
    assert require_search_candidate(candidate, role="test candidate") is candidate
    assert candidate.within_production_search_envelope()


def test_observed_boundary_expands_damping_without_a_new_floor() -> None:
    boundary = min(production_candidate_catalog(), key=lambda row: row.force_damping)
    lower = min(damping_variants(boundary), key=lambda row: row.force_damping)
    assert lower.force_damping < boundary.force_damping
    assert require_search_candidate(lower, role="expanded candidate") is lower


def test_orientation_k_is_a_bounded_quarter_octave_axis() -> None:
    assert len(ORIENTATION_KO_LATTICE) == 13
    assert ORIENTATION_KO_LATTICE[0] == pytest.approx(0.1)
    assert ORIENTATION_KO_LATTICE[-1] == pytest.approx(0.8)
    assert all(
        right / left == pytest.approx(2.0**0.25)
        for left, right in zip(
            ORIENTATION_KO_LATTICE, ORIENTATION_KO_LATTICE[1:]
        )
    )


def test_orientation_variants_change_identity_without_cross_product() -> None:
    anchor = ForceCandidate()
    variants = orientation_variants(anchor)
    assert {row.orientation_ko for row in variants} == set(ORIENTATION_KO_LATTICE)
    assert len({row.candidate_uid for row in variants}) == len(ORIENTATION_KO_LATTICE)
    assert anchor in variants
    assert ForceCandidate().candidate_uid == anchor.candidate_uid

    base = production_candidate_catalog()
    augmented = augment_catalog_with_orientation_anchors(base, (anchor,))
    assert len(augmented) == (
        len(base)
        + len(ORIENTATION_KO_LATTICE)
        + len(MOTION_KP_LATTICE)
        - 2
    )


def test_orientation_k_is_an_explicit_gp_feature_with_legacy_default_uid() -> None:
    default = ForceCandidate()
    lower = ForceCandidate(orientation_ko=0.2)
    assert "orientation_ko" not in default.payload()
    assert lower.payload()["orientation_ko"] == pytest.approx(0.2)
    assert default.candidate_uid != lower.candidate_uid
    assert len(candidate_vector(default)) == 7
    assert candidate_vector(default)[:5] == candidate_vector(lower)[:5]
    assert candidate_vector(default)[6] == candidate_vector(lower)[6]
    assert candidate_vector(default)[5] == pytest.approx(0.0)
    assert candidate_vector(lower)[5] == pytest.approx(-1.0)
