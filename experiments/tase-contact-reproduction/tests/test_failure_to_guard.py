from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
PACKAGE_SOURCE = REPO / "src/ur10e_experiment_runtime"
sys.path.insert(0, str(PACKAGE_SOURCE))

from ur10e_experiment_runtime.failure_to_guard import (  # noqa: E402
    FailureToGuardOutcomeClassifier,
    FailureToGuardError,
    ParameterImpact,
    assess_debt_lane,
    build_change_contract,
    canonical_failure_signature,
    gate_optimizer_observation,
    load_failure_ledger,
    parameter_impact_for_paths,
)


F2G_CONFIG = ROOT / "config/failure_to_guard"
INCIDENT = ROOT / "tests/fixtures/failure_to_guard_hil_bridge_startup_incident_v1.json"
INCIDENT_SET = ROOT / "tests/fixtures/failure_to_guard_step5d_v3_incidents_v1.json"


def load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text())
    assert isinstance(payload, dict)
    return payload


def test_signature_removes_run_noise_but_preserves_failure_semantics() -> None:
    first = {
        "lane": "hil",
        "outcome_class": "startup_failure",
        "component": "production_bridge",
        "root_cause": (
            "PID 123 failed at 2026-07-19T00:37:56+08:00 in "
            "runs/hil-live-20260719T0038HKT/bridge.log because model.csv was absent"
        ),
    }
    repeated = {
        "lane": "live",
        "outcome_class": "startup_failure",
        "component": "production_bridge",
        "root_cause": (
            "PID 987 failed at 2026-07-20T01:00:00+08:00 in "
            "runs/retry-4a8e90ba/bridge.log because model.csv was absent"
        ),
    }
    different = {**repeated, "root_cause": "controller identity did not match"}

    assert canonical_failure_signature(first) == canonical_failure_signature(repeated)
    assert canonical_failure_signature(first) != canonical_failure_signature(different)


def test_only_fully_verified_valid_observation_is_optimizer_eligible() -> None:
    accepted = gate_optimizer_observation(
        "valid_parameter_observation",
        1.25,
        metric_role="trainable_objective",
        oracle_status="credible",
        observer_status="complete",
        fingerprint_verified=True,
        exact_ack_consumed=True,
        post_ack_closure_verified=True,
        publication_unique=True,
    )
    assert accepted.optimizer_eligible is True
    assert accepted.objective == 1.25

    infrastructure = gate_optimizer_observation(
        "startup_failure",
        -999.0,
        metric_role="diagnostic_only",
        oracle_status="failed",
        observer_status="incomplete",
        fingerprint_verified=True,
        exact_ack_consumed=True,
        post_ack_closure_verified=True,
        publication_unique=True,
    )
    assert infrastructure.optimizer_eligible is False
    assert infrastructure.objective is None
    assert "outcome:startup_failure" in infrastructure.rejection_reasons
    assert "metric_role:diagnostic_only" in infrastructure.rejection_reasons

    unbound = gate_optimizer_observation(
        "valid_parameter_observation",
        2.0,
        metric_role="trainable_objective",
        oracle_status="credible",
        observer_status="complete",
        fingerprint_verified=False,
        exact_ack_consumed=True,
        post_ack_closure_verified=True,
        publication_unique=True,
    )
    assert unbound.optimizer_eligible is False
    assert unbound.objective is None
    assert "fingerprint_unverified" in unbound.rejection_reasons

    classified = FailureToGuardOutcomeClassifier().classify(
        {
            "outcome_class": "valid_parameter_observation",
            "objective": 3.5,
            "metric_role": "trainable_objective",
            "oracle_status": "credible",
            "observer_status": "complete",
            "checks": {
                "fingerprint_verified": True,
                "exact_ack_consumed": True,
                "post_ack_closure_verified": True,
                "publication_unique": True,
            },
        }
    )
    assert classified["optimizer_eligible"] is True
    assert classified["objective"] == 3.5


def test_invalid_or_unknown_objective_fails_closed() -> None:
    non_finite = gate_optimizer_observation(
        "valid_parameter_observation",
        float("nan"),
        metric_role="trainable_objective",
        oracle_status="credible",
        observer_status="complete",
        fingerprint_verified=True,
        exact_ack_consumed=True,
        post_ack_closure_verified=True,
        publication_unique=True,
    )
    assert non_finite.objective is None
    assert non_finite.optimizer_eligible is False
    numeric_string = gate_optimizer_observation(
        "valid_parameter_observation",
        "1.0",  # type: ignore[arg-type]
        metric_role="trainable_objective",
        oracle_status="credible",
        observer_status="complete",
        fingerprint_verified=True,
        exact_ack_consumed=True,
        post_ack_closure_verified=True,
        publication_unique=True,
    )
    assert numeric_string.objective is None
    assert numeric_string.optimizer_eligible is False
    with pytest.raises(FailureToGuardError, match="unknown trial outcome"):
        gate_optimizer_observation("maybe_good", 1.0)


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"metric_role": "diagnostic_only"}, "metric_role:diagnostic_only"),
        ({"metric_role": "unavailable", "objective": None}, "metric_role:unavailable"),
        ({"oracle_status": "failed"}, "oracle_status:failed"),
        ({"observer_status": "incomplete"}, "observer_status:incomplete"),
        ({"fingerprint_verified": False}, "fingerprint_unverified"),
        ({"exact_ack_consumed": False}, "exact_ack_unverified"),
        ({"post_ack_closure_verified": False}, "post_ack_closure_unverified"),
        ({"publication_unique": False}, "publication_not_unique"),
    ],
)
def test_each_trial_brief_gate_is_independently_fail_closed(
    patch: dict[str, object], reason: str
) -> None:
    values: dict[str, object] = {
        "objective": 1.0,
        "metric_role": "trainable_objective",
        "oracle_status": "credible",
        "observer_status": "complete",
        "fingerprint_verified": True,
        "exact_ack_consumed": True,
        "post_ack_closure_verified": True,
        "publication_unique": True,
    }
    values.update(patch)
    objective = values.pop("objective")
    result = gate_optimizer_observation(
        "valid_parameter_observation", objective, **values  # type: ignore[arg-type]
    )
    assert result.optimizer_eligible is False
    assert result.objective is None
    assert reason in result.rejection_reasons


def test_debt_triggers_at_exact_governed_boundaries() -> None:
    event = {
        "lane": "offline",
        "outcome_class": "code_contract_failure",
        "component": "campaign_store",
        "root_cause": "trial evidence had no closure record",
    }
    no_trigger = assess_debt_lane(
        event, patch_test_evidence_loops=3, sampling_rounds=40
    )
    assert no_trigger.required is False

    fourth_loop = assess_debt_lane(event, patch_test_evidence_loops=4)
    assert fourth_loop.reasons == ("patch_test_evidence_loop",)
    forty_first_round = assess_debt_lane(event, sampling_rounds=41)
    assert forty_first_round.reasons == ("sampling_round_budget",)

    prior = {"canonical_signature": canonical_failure_signature(event)}
    recurrence = assess_debt_lane(event, prior_ledger_entries=[prior])
    assert recurrence.recurrence_count == 2
    assert recurrence.reasons == ("canonical_failure_recurrence",)


def test_real_hil_escape_requests_plan_only_debt_while_live_is_locked() -> None:
    incident = load_json(INCIDENT)
    decision = assess_debt_lane(
        incident,
        escaped_existing_tests=bool(incident["escaped_existing_tests"]),
        resource_locks=["live_writer"],
    )
    assert decision.required is True
    assert decision.recurrence_count == 1
    assert decision.reasons == ("hil_live_escape",)
    assert decision.execution_mode == "plan_only"
    assert decision.blocking_resource_locks == ("live_writer",)


def test_change_contract_selects_lanes_history_and_strongest_impact() -> None:
    registry = {
        "invariants": [
            {
                "id": "stable_store",
                "touched_path_patterns": ["src/runtime/store*.py"],
                "expected_failure_modes": ["stale_store"],
                "required_lanes": ["small", "medium"],
                "parameter_impact": "replay_required",
            },
            {
                "id": "objective_semantics",
                "touched_path_patterns": ["src/runtime/objective*.py"],
                "expected_failure_modes": ["stale_parameters"],
                "required_lanes": ["small"],
                "parameter_impact": "retune_required",
            },
        ]
    }
    coverage = {
        "invariants": [
            {
                "invariant_id": "objective_semantics",
                "lanes": {"hil": {"required": True}},
            }
        ]
    }
    related = {
        "canonical_signature": "a" * 64,
        "invariant_ids": ["objective_semantics"],
    }
    contract = build_change_contract(
        ["src/runtime/store.py", "src/runtime/objective_v2.py"],
        registry,
        coverage,
        failure_ledger_entries=[related],
    )

    assert contract.parameter_impact is ParameterImpact.RETUNE_REQUIRED
    assert contract.required_test_lanes == ("hil", "medium", "small")
    assert contract.related_failure_signatures == ("a" * 64,)
    assert len(contract.contract_fingerprint) == 64
    assert contract.to_dict() == contract.to_dict()
    assert parameter_impact_for_paths(
        ["src/runtime/store.py"], registry
    ) is ParameterImpact.REPLAY_REQUIRED

    with pytest.raises(FailureToGuardError, match="unmapped touched paths"):
        build_change_contract(["src/runtime/unmapped.py"], registry, coverage)


def test_registry_coverage_ledger_and_incident_provenance_are_closed() -> None:
    registry = load_json(F2G_CONFIG / "invariant_registry_v1.json")
    coverage = load_json(F2G_CONFIG / "coverage_map_v1.json")
    entries = load_failure_ledger(F2G_CONFIG / "failure_ledger_v1.jsonl")
    incident = load_json(INCIDENT)
    source = ROOT / str(incident["provenance"]["source_manifest"])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == incident["provenance"][
        "source_manifest_sha256"
    ]
    assert entries[0]["canonical_signature"] == canonical_failure_signature(incident)
    fixture_set = load_json(INCIDENT_SET)
    fixture_rows = fixture_set["incidents"]
    assert isinstance(fixture_rows, list)
    expected_ids = {
        "step5d-v3-wrong-stage-prior",
        "step5d-v3-first-tick-relatch",
        "step5d-v3-overlay-mismatch",
        "step5d-v3-launcher-false-failure",
        "step5d-v3-metric-role-confusion-trial21",
        "step5d-v3-near-ready-home-confusion",
        "step5d-v3-trajectory-reference-mismatch",
        "step5d-v3-aabb-sphere-dual-enforcement",
    }
    assert {row["incident_id"] for row in fixture_rows} == expected_ids
    ledger_by_signature = {entry["canonical_signature"]: entry for entry in entries}
    for row in fixture_rows:
        signature = canonical_failure_signature(row)
        assert signature in ledger_by_signature
        ledger_row = ledger_by_signature[signature]
        assert ledger_row["counts_for_recurrence"] is row["counts_for_recurrence"]
        assert ledger_row["outcome_class"] == row["outcome_class"]
    assert len(entries) == 9

    registry_ids = {item["id"] for item in registry["invariants"]}
    coverage_ids = {item["invariant_id"] for item in coverage["invariants"]}
    assert coverage_ids == registry_ids
    assert set(entries[0]["invariant_ids"]) <= registry_ids
    implemented_m4_ids = {
        "f2g.step5d_physical_prior_binding",
        "f2g.step5d_trial_reset_no_relatch",
        "f2g.actual_overlay_identity",
        "f2g.exact_batch_completion",
        "f2g.metric_role_optimizer_gate",
        "f2g.typed_return_reference",
        "f2g.trajectory_reference_binding",
        "f2g.moving_sphere_exclusive_guard",
    }
    for row in coverage["invariants"]:
        if row["invariant_id"] not in implemented_m4_ids:
            continue
        for lane in row["lanes"].values():
            assert "planned_m4" not in lane["status"]
            assert "planned_m5" not in lane["status"]
    timing_statuses = {
        row["lanes"]["timing"]["status"]
        for row in coverage["invariants"]
        if "timing" in row["lanes"]
    }
    assert timing_statuses == {
        "diagnostic_seam_failed_host_schedule_formal_full_path_blocked"
    }
    for entry in entries:
        assert "pending_m4" not in entry["guard_status"]

    contract = build_change_contract(
        [
            "src/ur10e_experiment_runtime/ur10e_experiment_runtime/"
            "failure_to_guard.py"
        ],
        registry,
        coverage,
        failure_ledger_entries=entries,
    )
    assert entries[0]["canonical_signature"] in contract.related_failure_signatures
    assert contract.parameter_impact is ParameterImpact.REPLAY_REQUIRED


def test_preventive_hazards_do_not_count_as_failure_recurrences() -> None:
    rows = load_json(INCIDENT_SET)["incidents"]
    preventive = next(
        row for row in rows if row["incident_id"].endswith("near-ready-home-confusion")
    )
    prior = {
        "canonical_signature": canonical_failure_signature(preventive),
        "counts_for_recurrence": False,
    }
    decision = assess_debt_lane(preventive, prior_ledger_entries=[prior])
    assert decision.recurrence_count == 1
    assert "canonical_failure_recurrence" not in decision.reasons


@pytest.mark.parametrize(
    ("raw_record", "expected_error"),
    [
        (b'{"entry_id":"first","entry_id":"second"}\n', "duplicate key"),
        (b'{"objective":NaN}\n', "non-finite number"),
        (b'{"objective":Infinity}\n', "non-finite number"),
        (b'{"objective":-Infinity}\n', "non-finite number"),
        (b'{"objective":1e999}\n', "non-finite number"),
        (b'{"message":"\xff"}\n', "not UTF-8"),
    ],
)
def test_failure_ledger_rejects_ambiguous_jsonl_records(
    tmp_path: Path,
    raw_record: bytes,
    expected_error: str,
) -> None:
    ledger = tmp_path / "failure-ledger.jsonl"
    ledger.write_bytes(raw_record)

    with pytest.raises(FailureToGuardError, match=expected_error):
        load_failure_ledger(ledger)


def test_failure_ledger_rejects_symlink_and_non_regular_file(tmp_path: Path) -> None:
    real = tmp_path / "real.jsonl"
    real.write_text('{"entry_id":"one"}\n')
    link = tmp_path / "link.jsonl"
    link.symlink_to(real)
    with pytest.raises(FailureToGuardError, match="no-follow regular file"):
        load_failure_ledger(link)

    fifo = tmp_path / "ledger.fifo"
    import os

    os.mkfifo(fifo)
    with pytest.raises(FailureToGuardError, match="no-follow regular file"):
        load_failure_ledger(fifo)
