from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from contact_benchmark_tuner import (  # noqa: E402
    ContactBenchmarkTuner,
    ContactBenchmarkTunerError,
    PUBLIC_LAWS,
    TrainingObservation,
)
from contact_laws import DEFAULT_CONFIG_PATH  # noqa: E402


def _row(proposal, *, status: str = "completed", objective: float | None = 1.0, nominal: bool = True):
    return TrainingObservation(
        law=proposal.law,
        candidate=proposal.candidate,
        status=status,
        nominal_feasible=nominal,
        objective=objective,
    )


def _collect(tuner: ContactBenchmarkTuner, law: str, count: int, *, failed: bool = False):
    observations = []
    proposals = []
    for index in range(count):
        proposal = tuner.propose(law, observations, index)
        proposals.append(proposal)
        if failed:
            observations.append(_row(proposal, status="failed", objective=None))
        else:
            observations.append(_row(proposal, objective=float(index + 1)))
    return proposals, observations


def test_public_laws_seed_and_fixed_parameter_binding_are_explicit() -> None:
    tuner = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH, seed=77)
    root = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))

    assert PUBLIC_LAWS == ("LAC", "NAC", "SFC", "DSFC", "ISFC", "MSFC")
    assert "RPSFC" not in PUBLIC_LAWS
    assert tuner.bounds["m"] == (4.0, 16.0)
    assert tuner.bounds["mu_relative"] == (0.125, 8.0)
    assert tuner.bounds["g"] == (0.03, 2.0)
    assert tuner.bounds["g_relative"] == (0.03, 2.0)

    for law in PUBLIC_LAWS:
        seed = tuner.seed_candidate(law)
        config_parameters = root["laws"][law]["parameters"]
        assert seed.parameters == pytest.approx(config_parameters)
        proposal = tuner.propose(law, [], 0)
        binding = proposal.bindings
        assert binding["law"] == law
        assert binding["config_sha256"] == tuner.config_sha256
        assert binding["law_seed"] == tuner.law_seed(law)
        assert binding["fixed_parameters"] == {
            name: value
            for name, value in config_parameters.items()
            if name not in {"m", "mu", "g"}
        }
        assert binding["training_only"] is True
        assert binding["holdout_used"] is False
        assert binding["hardware_qualified"] is False
        assert binding["schedule"] == {
            "total_units": 24,
            "initial_sobol_units": 8,
            "bayesian_ei_units": 12,
            "repeat_incumbent_units": 4,
        }


def test_initial_seed_plus_sobol_schedule_is_deterministic_and_bounded() -> None:
    first = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH, seed=123)
    second = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH, seed=123)
    first_proposals, _ = _collect(first, "LAC", 8, failed=True)
    second_proposals, _ = _collect(second, "LAC", 8, failed=True)

    assert [proposal.candidate.key for proposal in first_proposals] == [
        proposal.candidate.key for proposal in second_proposals
    ]
    assert first_proposals[0].candidate == first.seed_candidate("LAC")
    assert [proposal.phase for proposal in first_proposals] == ["initial_sobol"] * 8
    assert len({proposal.candidate.key for proposal in first_proposals}) == 8
    assert all(4.0 <= proposal.candidate.m <= 16.0 for proposal in first_proposals)
    assert all(
        0.125 <= proposal.candidate.mu / first.seed_candidate("LAC").mu <= 8.0
        for proposal in first_proposals
    )
    assert all(
        0.03 <= proposal.candidate.g / first.seed_candidate("LAC").g <= 2.0
        for proposal in first_proposals
    )
    assert all(proposal.bo_used is False for proposal in first_proposals)
    assert all(proposal.nominal_feasible is None for proposal in first_proposals)


def test_failed_units_consume_ei_budget_without_fabricated_objectives() -> None:
    tuner = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH, seed=123)
    proposals, observations = _collect(tuner, "LAC", 8, failed=True)

    for index in range(8, 20):
        proposal = tuner.propose("LAC", observations, index)
        assert proposal.unit_index == index
        assert proposal.phase == "bayesian_ei"
        assert proposal.bo_used is False
        assert proposal.acquisition_value is None
        assert proposal.nominal_feasible is None
        assert "unavailable" in proposal.reason
        assert "deterministic_space_filling" in proposal.backend
        observations.append(_row(proposal, status="failed", objective=None))
        proposals.append(proposal)

    assert len(observations) == 20
    for index in range(20, 24):
        proposal = tuner.propose("LAC", observations, index)
        assert proposal.unit_index == index
        assert proposal.phase == "repeat_incumbent"
        assert proposal.reason.startswith("repeat_provisional_seed")
        assert proposal.incumbent_objective is None
        assert proposal.nominal_feasible is None
        observations.append(_row(proposal, status="failed", objective=None))
        proposals.append(proposal)
    assert len(proposals) == 24
    assert len(observations) == 24
    with pytest.raises(ContactBenchmarkTunerError, match="unit_count"):
        tuner.propose("LAC", observations, 24)


def test_bayesian_ei_uses_only_feasible_training_and_repeats_incumbent() -> None:
    tuner = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH, seed=123)
    proposals, observations = [], []
    for index in range(8):
        proposal = tuner.propose("MSFC", observations, index)
        proposals.append(proposal)
        # The seed has the lowest numeric objective but is nominally
        # infeasible, so it must not become either the EI incumbent or repeat.
        if index == 0:
            observations.append(_row(proposal, objective=0.0, nominal=False))
        else:
            observations.append(_row(proposal, objective=float(index), nominal=True))

    proposal = tuner.propose("MSFC", observations, 8)
    assert proposal.phase == "bayesian_ei"
    assert proposal.bo_used is True
    assert proposal.acquisition_value is not None
    assert proposal.backend.endswith("fixed_matern52_ei")
    assert proposal.candidate.key not in {row.candidate.key for row in observations}
    observations.append(_row(proposal, objective=20.0))
    proposals.append(proposal)

    for index in range(9, 20):
        proposal = tuner.propose("MSFC", observations, index)
        assert proposal.phase == "bayesian_ei"
        assert proposal.bo_used is True
        assert proposal.candidate.key not in {row.candidate.key for row in observations}
        observations.append(_row(proposal, objective=float(index + 20)))
        proposals.append(proposal)

    incumbent_key = proposals[1].candidate.key
    assert len(observations) == 20
    for index in range(20, 24):
        proposal = tuner.propose("MSFC", observations, index)
        assert proposal.phase == "repeat_incumbent"
        assert proposal.candidate.key == incumbent_key
        assert proposal.incumbent_objective == pytest.approx(1.0)
        assert proposal.nominal_feasible is True
        # A repeat result is recorded after proposing.  It must not change
        # the incumbent frozen for the remaining repeat units.
        observations.append(_row(proposal, objective=0.001 if index == 20 else 1.0))
        proposals.append(proposal)

    assert [proposal.candidate.key for proposal in proposals[-4:]] == [incumbent_key] * 4
    assert [proposal.incumbent_objective for proposal in proposals[-4:]] == [pytest.approx(1.0)] * 4


def test_training_boundary_rejects_holdout_failed_objective_nonfinite_and_bad_count() -> None:
    tuner = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH)
    proposal = tuner.propose("LAC", [], 0)

    with pytest.raises(ContactBenchmarkTunerError, match="holdout"):
        tuner.propose(
            "LAC",
            [
                {
                    "law": "LAC",
                    "candidate": proposal.candidate.as_dict(),
                    "status": "completed",
                    "nominal_feasible": True,
                    "objective": 1.0,
                    "split": "holdout",
                }
            ],
            1,
        )
    with pytest.raises(ContactBenchmarkTunerError, match="failed.*objective"):
        tuner.propose(
            "LAC",
            [_row(proposal, status="failed", objective=1.0)],
            1,
        )
    with pytest.raises(ContactBenchmarkTunerError, match="requires caller objective"):
        tuner.propose("LAC", [_row(proposal, objective=None)], 1)
    with pytest.raises(ContactBenchmarkTunerError, match="nominal_feasible must be bool"):
        tuner.propose(
            "LAC",
            [{**_row(proposal).__dict__, "nominal_feasible": None}],
            1,
        )
    with pytest.raises(ContactBenchmarkTunerError, match="finite"):
        tuner.propose("LAC", [_row(proposal, objective=float("nan"))], 1)
    with pytest.raises(ContactBenchmarkTunerError, match="unit_count"):
        tuner.propose("LAC", [], 1)
    with pytest.raises(ContactBenchmarkTunerError, match="RPSFC"):
        tuner.propose("RPSFC", [], 0)


def test_fixed_parameters_and_candidate_bounds_cannot_be_changed() -> None:
    tuner = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH)
    proposal = tuner.propose("NAC", [], 0)
    changed = proposal.candidate.as_dict()
    changed["alpha"] += 1.0
    with pytest.raises(ContactBenchmarkTunerError, match="fixed parameter"):
        tuner._parse_candidate("NAC", changed)

    changed = proposal.candidate.as_dict()
    changed["m"] = 3.999
    with pytest.raises(ContactBenchmarkTunerError, match="outside provisional bounds"):
        tuner._parse_candidate("NAC", changed)


def test_proposal_is_json_ready_and_binding_is_immutable() -> None:
    tuner = ContactBenchmarkTuner(DEFAULT_CONFIG_PATH)
    proposal = tuner.propose("LAC", [], 0)
    with pytest.raises(TypeError):
        proposal.bindings["phase"] = "tampered"
    serialized = json.dumps(proposal.as_dict(), sort_keys=True)
    decoded = json.loads(serialized)
    assert decoded["schema"] == "ur10e.contact-benchmark-tuner-v1"
    assert decoded["candidate"]["law"] == "LAC"
    assert decoded["nominal_feasible"] is None
    assert decoded["bindings"]["config_sha256"] == tuner.config_sha256
