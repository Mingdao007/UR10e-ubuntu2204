from __future__ import annotations

import json
import math
from pathlib import Path

import pytest


PROTOCOL = Path(__file__).resolve().parents[1] / "report" / "yield-frozen-transfer-v1" / "protocol.json"

from contact_laws import PARAMETER_ORDER  # noqa: E402
from yield_contact_tuner import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    G50,
    G_BOUNDS,
    METHODS,
    MSFC_ACTIVE_METRIC_FLOOR,
    M_BOUNDS,
    MU_BOUNDS,
    NATIVE_PARAMETER_ORDER,
    YieldContactTuner,
    YieldContactTunerError,
    YieldTrainingObservation,
)


CELL = "yield-fair-training-cell-v1"
CONTRACT = "yield-fair-selection-contract-v1"


def _tuner() -> YieldContactTuner:
    return YieldContactTuner(
        DEFAULT_CONFIG_PATH,
        training_cell_id=CELL,
        selection_contract_id=CONTRACT,
    )


def _row(
    proposal,
    *,
    status: str = "completed",
    objective: float | None = 1.0,
    nominal: bool = True,
    split: str = "training",
    training_cell_id: str = CELL,
    selection_contract_id: str = CONTRACT,
    pair_complete: bool = True,
    unit_index: int | None = None,
    pair=None,
):
    return YieldTrainingObservation(
        method=proposal.method,
        candidate=proposal.candidate,
        status=status,
        nominal_feasible=nominal,
        unit_index=proposal.unit_index if unit_index is None else unit_index,
        training_cell_id=training_cell_id,
        selection_contract_id=selection_contract_id,
        pair_complete=pair_complete,
        objective=None if status == "failed" else objective,
        split=split,
        pair=pair,
    )


def _collect(tuner: YieldContactTuner, method: str, count: int, *, failed: bool = False, start_objective: float = 1.0):
    observations = []
    proposals = []
    for index in range(count):
        proposal = tuner.propose(method, observations, index)
        proposals.append(proposal)
        if failed:
            observations.append(_row(proposal, status="failed", objective=None))
        else:
            observations.append(_row(proposal, objective=start_objective + float(index)))
    return proposals, observations


def test_shared_initial_triples_are_identical_across_methods() -> None:
    tuner = _tuner()
    triples = tuner.shared_mechanical_triples()
    assert len(triples) == 8
    assert triples[0] == pytest.approx((4.0, 393.0, 0.052126826121414976))
    assert triples[1] == pytest.approx((4.0, 310.66177089084647, 0.0642))
    assert triples[2] == pytest.approx(G50)
    assert G50 in triples
    for method in METHODS:
        mechanical = [(candidate.m, candidate.mu, candidate.g) for candidate in tuner.initial_candidates(method)]
        assert mechanical == list(triples)
    sfc, dsfc, msfc = [tuner.initial_candidates(method) for method in METHODS]
    for index in range(8):
        assert (sfc[index].m, sfc[index].mu, sfc[index].g) == (dsfc[index].m, dsfc[index].mu, dsfc[index].g)
        assert (dsfc[index].m, dsfc[index].mu, dsfc[index].g) == (msfc[index].m, msfc[index].mu, msfc[index].g)


def test_fixed_parameter_binding_matches_ft_v1_and_keeps_msfc_active() -> None:
    tuner = _tuner()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))["parameters"]
    assert protocol["DSFC"]["a"] == 0.05
    assert protocol["DSFC"]["p"] == 0.5
    assert protocol["MSFC"]["minimum_metric_eigenvalue"] == MSFC_ACTIVE_METRIC_FLOOR
    assert protocol["MSFC"]["minimum_metric_eigenvalue"] != 1.0

    for method in METHODS:
        seed = tuner.seed_candidate(method)
        assert NATIVE_PARAMETER_ORDER[method] == PARAMETER_ORDER[method]
        assert tuple(seed.parameters) == PARAMETER_ORDER[method]
        assert seed.parameters == pytest.approx(protocol[method])
        proposal = tuner.propose(method, [], 0)
        json.dumps(proposal.as_dict(), sort_keys=True, allow_nan=False)
        fixed = proposal.bindings["fixed_parameters"]
        expected_fixed = {name: value for name, value in protocol[method].items() if name not in {"m", "mu", "g"}}
        assert fixed == pytest.approx(expected_fixed)
        assert proposal.bindings["config_sha256"] == tuner.config_sha256
        assert proposal.bindings["training_cell_id"] == CELL
        assert proposal.bindings["selection_contract_id"] == CONTRACT
        assert proposal.bindings["does_not_freeze_campaign"] is True
        assert proposal.bindings["holdout_used"] is False
        assert proposal.phase == "initial"
        assert proposal.bo_used is False

    dsfc_fixed = tuner.propose("DSFC", [], 0).bindings["fixed_parameters"]
    assert dsfc_fixed["a"] == 0.05
    assert dsfc_fixed["p"] == 0.5
    assert dsfc_fixed["a"] != 1.2
    assert dsfc_fixed["p"] != 0.1
    msfc_fixed = tuner.propose("MSFC", [], 0).bindings["fixed_parameters"]
    assert msfc_fixed["minimum_metric_eigenvalue"] == MSFC_ACTIVE_METRIC_FLOOR
    assert msfc_fixed["minimum_metric_eigenvalue"] != 1.0


def test_coefficient_matched_dsfc_g50_initial_point_is_present() -> None:
    tuner = _tuner()
    dsfc = tuner.initial_candidates("DSFC")
    matched = [
        candidate
        for candidate in dsfc
        if (candidate.m, candidate.mu, candidate.g) == G50
    ]
    assert len(matched) == 1
    assert matched[0].parameters["a"] == 0.05
    assert matched[0].parameters["p"] == 0.5
    assert matched[0].parameters["n"] == 3.0
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))["parameters"]["MSFC"]
    for name in ("m", "mu", "g", "a", "p", "n"):
        assert matched[0].parameters[name] == pytest.approx(protocol[name])


def test_absolute_bounds_admit_g50_and_reject_out_of_range() -> None:
    tuner = _tuner()
    assert tuner.bounds["m"] == M_BOUNDS
    assert tuner.bounds["mu"] == MU_BOUNDS
    assert tuner.bounds["g"] == G_BOUNDS
    assert M_BOUNDS[0] <= G50[0] <= M_BOUNDS[1]
    assert MU_BOUNDS[0] <= G50[1] <= MU_BOUNDS[1]
    assert G_BOUNDS[0] <= G50[2] <= G_BOUNDS[1]
    for candidate in tuner.initial_candidates("SFC"):
        assert M_BOUNDS[0] <= candidate.m <= M_BOUNDS[1]
        assert MU_BOUNDS[0] <= candidate.mu <= MU_BOUNDS[1]
        assert G_BOUNDS[0] <= candidate.g <= G_BOUNDS[1]
    proposal = tuner.propose("SFC", [], 0)
    changed = proposal.candidate.as_dict()
    changed["mu"] = 39.9
    with pytest.raises(YieldContactTunerError, match="outside offline search bounds"):
        tuner._parse_candidate("SFC", changed)
    changed = proposal.candidate.as_dict()
    changed["n"] = 4.0
    with pytest.raises(YieldContactTunerError, match="fixed parameter"):
        tuner._parse_candidate("SFC", changed)


def test_failed_units_consume_ordinal_and_do_not_fabricate_objectives() -> None:
    tuner = _tuner()
    proposals, observations = _collect(tuner, "SFC", 8, failed=True)
    assert [proposal.phase for proposal in proposals] == ["initial"] * 8
    assert all(row.status == "failed" and row.objective is None for row in observations)
    with pytest.raises(YieldContactTunerError, match="unit_count"):
        tuner.propose("SFC", observations, 0)
    with pytest.raises(YieldContactTunerError, match="Bayesian EI unavailable"):
        tuner.propose("SFC", observations, 8)
    with pytest.raises(YieldContactTunerError, match="unit_count"):
        tuner.propose("SFC", observations[:7], 8)


def test_holdout_cell_contract_split_and_incomplete_pair_are_rejected() -> None:
    tuner = _tuner()
    proposal = tuner.propose("DSFC", [], 0)
    with pytest.raises(YieldContactTunerError, match="holdout"):
        tuner.propose("DSFC", [_row(proposal, split="holdout")], 1)
    with pytest.raises(YieldContactTunerError, match="training_cell_id"):
        tuner.propose("DSFC", [_row(proposal, training_cell_id="other-cell")], 1)
    with pytest.raises(YieldContactTunerError, match="selection_contract_id"):
        tuner.propose("DSFC", [_row(proposal, selection_contract_id="other-contract")], 1)
    with pytest.raises(YieldContactTunerError, match="incomplete pair"):
        tuner.propose("DSFC", [_row(proposal, pair_complete=False)], 1)
    with pytest.raises(YieldContactTunerError, match="incomplete pair"):
        tuner.propose(
            "DSFC",
            [_row(proposal, pair={"nominal": {"status": "completed"}, "disturbed": {"status": "running"}})],
            1,
        )
    with pytest.raises(YieldContactTunerError, match="sequential"):
        tuner.propose("DSFC", [_row(proposal, unit_index=1)], 1)
    second = tuner.propose("DSFC", [_row(proposal)], 1)
    with pytest.raises(YieldContactTunerError, match="sequential"):
        tuner.propose("DSFC", [_row(second), _row(proposal)], 2)
    with pytest.raises(YieldContactTunerError, match="six-law proxy"):
        tuner.propose("LAC", [], 0)
    mixed = YieldContactTuner(
        DEFAULT_CONFIG_PATH,
        training_cell_id="cell-a",
        selection_contract_id=CONTRACT,
    )
    with pytest.raises(YieldContactTunerError, match="training_cell_id"):
        mixed.propose("DSFC", [_row(proposal)], 1)


def test_repeat_incumbent_is_frozen_from_discovery_and_not_promoted_from_seed() -> None:
    tuner = _tuner()
    proposals, observations = _collect(tuner, "MSFC", 8, start_objective=10.0)
    observations[0] = _row(proposals[0], objective=0.0, nominal=False)
    for index in range(8, 20):
        proposal = tuner.propose("MSFC", observations, index)
        assert proposal.phase == "bayesian_ei"
        assert proposal.bo_used is True
        assert proposal.acquisition_value is not None
        assert math.isfinite(proposal.acquisition_value)
        assert "space_filling" not in proposal.backend
        observations.append(_row(proposal, objective=float(index + 20)))
        proposals.append(proposal)
    incumbent_key = proposals[1].candidate.key
    for index in range(20, 24):
        proposal = tuner.propose("MSFC", observations, index)
        assert proposal.phase == "repeat_incumbent"
        assert proposal.candidate.key == incumbent_key
        assert proposal.incumbent_objective == pytest.approx(11.0)
        assert proposal.nominal_feasible is True
        observations.append(_row(proposal, objective=0.001 if index == 20 else 11.0))
        proposals.append(proposal)
    assert [proposal.candidate.key for proposal in proposals[-4:]] == [incumbent_key] * 4

    failed = _tuner()
    _, failed_rows = _collect(failed, "SFC", 8, failed=True)
    with pytest.raises(YieldContactTunerError, match="Bayesian EI unavailable"):
        failed.propose("SFC", failed_rows, 8)


def test_deterministic_valid_ei_after_eight_completed_training_rows() -> None:
    first = _tuner()
    second = _tuner()
    first_proposals, first_rows = _collect(first, "DSFC", 8)
    second_proposals, second_rows = _collect(second, "DSFC", 8)
    assert [proposal.candidate.key for proposal in first_proposals] == [
        proposal.candidate.key for proposal in second_proposals
    ]
    dry = first.propose("DSFC", first_rows, 8)
    again = first.propose("DSFC", first_rows, 8)
    other = second.propose("DSFC", second_rows, 8)
    assert dry.phase == "bayesian_ei"
    assert dry.bo_used is True
    assert dry.acquisition_value is not None
    assert math.isfinite(dry.acquisition_value)
    assert dry.backend.endswith("fixed_matern52_ei")
    assert "space_filling" not in dry.backend
    assert dry.candidate.key == again.candidate.key == other.candidate.key
    assert dry.acquisition_value == pytest.approx(again.acquisition_value)
    assert dry.candidate.key not in {row.candidate.key for row in first_rows}
    assert len(first_rows) == 8


def test_unavailable_bo_is_explicit_and_not_labeled_as_bayesian() -> None:
    tuner = _tuner()
    _, failed = _collect(tuner, "SFC", 8, failed=True)
    with pytest.raises(YieldContactTunerError, match="Bayesian EI unavailable"):
        tuner.propose("SFC", failed, 8)
    feasible = _tuner()
    proposals, rows = _collect(feasible, "SFC", 8)
    import yield_contact_tuner as module

    def _boom(*_args, **_kwargs):
        raise YieldContactTunerError("Bayesian EI unavailable: numpy is unavailable")

    original = module.matern52_expected_improvement
    module.matern52_expected_improvement = _boom
    try:
        with pytest.raises(YieldContactTunerError, match="Bayesian EI unavailable"):
            feasible.propose("SFC", rows, 8)
    finally:
        module.matern52_expected_improvement = original
    recovered = feasible.propose("SFC", rows, 8)
    assert recovered.phase == "bayesian_ei"
    assert recovered.bo_used is True
    assert recovered.acquisition_value is not None
    assert proposals[0].phase == "initial"


def test_no_qualified_incumbent_is_explicit_failure() -> None:
    tuner = _tuner()
    initials = tuner.initial_candidates("MSFC")
    observations = []
    for index in range(20):
        candidate = initials[index % 8]
        observations.append(
            YieldTrainingObservation(
                method="MSFC",
                candidate=candidate,
                status="failed",
                nominal_feasible=False,
                unit_index=index,
                training_cell_id=CELL,
                selection_contract_id=CONTRACT,
                pair_complete=True,
                objective=None,
                split="training",
            )
        )
    with pytest.raises(YieldContactTunerError, match="no qualified incumbent"):
        tuner.propose("MSFC", observations, 20)


def test_constructor_rejects_empty_cell_or_contract() -> None:
    with pytest.raises(YieldContactTunerError, match="training_cell_id"):
        YieldContactTuner(DEFAULT_CONFIG_PATH, training_cell_id="", selection_contract_id=CONTRACT)
    with pytest.raises(YieldContactTunerError, match="selection_contract_id"):
        YieldContactTuner(DEFAULT_CONFIG_PATH, training_cell_id=CELL, selection_contract_id=" ")


def test_missing_split_and_contradictory_terminal_pair_are_rejected():
    from dataclasses import asdict
    tuner = _tuner()
    proposal = tuner.propose('SFC', [], 0)
    row = asdict(_row(proposal))
    row['candidate'] = proposal.candidate.as_dict()
    del row['split']
    with pytest.raises(YieldContactTunerError, match='non-training'):
        tuner.propose('SFC', [row], 1)
    row['split'] = 'training'
    row['pair'] = {'nominal': 'failed', 'disturbed': 'completed'}
    with pytest.raises(YieldContactTunerError, match='contradicts'):
        tuner.propose('SFC', [row], 1)
    row['status'] = 'failed'
    row['objective'] = None
    assert tuner.propose('SFC', [row], 1).unit_index == 1
    row['pair']['nominal'] = 'unknown_terminal'
    with pytest.raises(YieldContactTunerError, match='incomplete pair'):
        tuner.propose('SFC', [row], 1)


@pytest.mark.parametrize('section,key,value', [
    ('schedule', 'total_units', 25),
    ('acquisition', 'pool_size', 1024),
    ('method_roles', 'SFC', 'proposal'),
])
def test_behavioral_config_cannot_disagree_with_execution(tmp_path, section, key, value):
    config = json.loads(DEFAULT_CONFIG_PATH.read_text())
    config[section][key] = value
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    with pytest.raises(YieldContactTunerError, match='drifted'):
        YieldContactTuner(path, training_cell_id=CELL, selection_contract_id=CONTRACT)


def test_search_scale_is_bound(tmp_path):
    config = json.loads(DEFAULT_CONFIG_PATH.read_text())
    config['search_bounds']['mu']['scale'] = 'linear'
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    with pytest.raises(YieldContactTunerError, match='scale drifted'):
        YieldContactTuner(path, training_cell_id=CELL, selection_contract_id=CONTRACT)


@pytest.mark.parametrize('mutation', ['seed', 'task'])
def test_declared_ft_seeds_and_fixed_task_cannot_drift(tmp_path, mutation):
    config = json.loads(DEFAULT_CONFIG_PATH.read_text())
    if mutation == 'seed':
        config['methods_spec']['SFC']['seed_parameters']['g'] *= 1.01
    else:
        config['task_binding']['normal_force_n'] = 6.
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    with pytest.raises(YieldContactTunerError, match='drifted'):
        YieldContactTuner(path, training_cell_id=CELL, selection_contract_id=CONTRACT)
