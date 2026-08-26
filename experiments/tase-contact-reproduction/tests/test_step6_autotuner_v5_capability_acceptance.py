from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step6_figure8_autotune_v1.v5_campaign_runner import (  # noqa: E402
    V5DurableChainResultV1,
)
from step6_figure8_autotune_v1.v5_capability_acceptance import (  # noqa: E402
    V5_CAPABILITY_CHAIN_IDS,
    V5CapabilityAcceptanceError,
    build_capability_chains,
    capability_anchor_candidate,
    capability_neighbor_candidate,
    evaluate_capability_results,
    verify_no_motion_recipe_receipt,
)
from step6_figure8_autotune_v1.v5_lifecycle_ledger import (  # noqa: E402
    BoundaryMode,
    canonical_sha256,
)
from step6_figure8_autotune_v1.v5_register_transport import (  # noqa: E402
    v5_recipe_contract_receipt,
)


def test_capability_schedule_is_fixed_seven_chains_and_seventeen_trials() -> None:
    chains = build_capability_chains(
        session_epoch=77,
        capability_fingerprint="a" * 64,
    )
    assert tuple(chains) == V5_CAPABILITY_CHAIN_IDS
    assert [len(chains[key]) for key in chains] == [2, 2, 2, 2, 2, 2, 5]
    assert sum(len(value) for value in chains.values()) == 17
    assert all(plan.wire_epoch == 77 for plans in chains.values() for plan in plans)
    assert chains[V5_CAPABILITY_CHAIN_IDS[0]][0].candidate_key == chains[
        V5_CAPABILITY_CHAIN_IDS[0]
    ][1].candidate_key
    four = chains[V5_CAPABILITY_CHAIN_IDS[-1]]
    assert [plan.candidate_key for plan in four] == [
        capability_anchor_candidate().candidate_key,
        capability_neighbor_candidate().candidate_key,
        capability_anchor_candidate().candidate_key,
        capability_neighbor_candidate().candidate_key,
        capability_anchor_candidate().candidate_key,
    ]
    anchor = capability_anchor_candidate().controller_path
    neighbor = capability_neighbor_candidate().controller_path
    changed = [key for key in anchor if anchor[key] != neighbor[key]]
    assert changed == ["motion_kp"]
    assert math.log2(neighbor["motion_kp"] / anchor["motion_kp"]) == pytest.approx(
        1.0 / 16.0
    )


def _record(plan: object, *, mae: float, home: bool) -> SimpleNamespace:
    gate_mapping = {
        "schema": "fixture",
        "version": 1,
        "safety": True,
        "timing": True,
        "freshness": True,
        "tube_cbf": True,
        "identity": True,
        "command_envelope": True,
    }
    row = {
        "normal_load_n": 5.0,
        "force_norm_n": 5.1,
        "torque_norm_nm": 0.2,
    }
    gate_families = SimpleNamespace(as_dict=lambda: gate_mapping)
    return SimpleNamespace(
        eligible=True,
        closure=SimpleNamespace(
            all_passed=True,
            path=gate_families,
            tail=gate_families,
            evidence=SimpleNamespace(
                evidence_sha256=canonical_sha256(
                    {"trial_id": plan.trial_id, "gate_mapping": gate_mapping}
                )
            ),
        ),
        trial_id=plan.trial_id,
        attempt_id=plan.attempt_id,
        record_sha256=canonical_sha256(
            {"trial_id": plan.trial_id, "mae": mae, "home": home}
        ),
        candidate_identity=SimpleNamespace(
            as_dict=lambda: dict(plan.candidate_identity)
        ),
        metric_snapshot=SimpleNamespace(formal_mae_n=mae),
        boundary=SimpleNamespace(
            mode=BoundaryMode.HOME if home else BoundaryMode.CONTACT_ROLLOVER
        ),
        source_artifact=SimpleNamespace(rows=(row,)),
        trial_slice=SimpleNamespace(sample_start_index=0, sample_end_index=1),
    )


def _results(*, pair_differences: tuple[float, ...]) -> tuple[V5DurableChainResultV1, ...]:
    chains = build_capability_chains(
        session_epoch=91,
        capability_fingerprint="a" * 64,
    )
    pair_by_chain = {
        V5_CAPABILITY_CHAIN_IDS[0]: pair_differences[0],
        **{
            chain_id: difference
            for chain_id, difference in zip(
                V5_CAPABILITY_CHAIN_IDS[2:6],
                pair_differences[1:],
                strict=True,
            )
        },
    }
    results = []
    for chain_id, plans in chains.items():
        records = []
        for index, plan in enumerate(plans):
            mae = 0.20 + 0.001 * index
            if chain_id in pair_by_chain and index == 1:
                mae = 0.20 + pair_by_chain[chain_id]
            records.append(_record(plan, mae=mae, home=index == len(plans) - 1))
        rollovers = len(plans) - 1
        results.append(
            V5DurableChainResultV1(
                chain_id,
                plans,
                tuple(records),
                {"artifact_path": f"/{chain_id}.r013life"},
                "b" * 64,
                "c" * 64,
                {
                    "complete": True,
                    "rollover_count": rollovers,
                    "activation_state_count": 4 * rollovers,
                },
            )
        )
    return tuple(results)


def test_capability_evaluator_accepts_only_cold_gate_closed_equivalence() -> None:
    evaluation = evaluate_capability_results(
        _results(pair_differences=(0.004, -0.003, 0.002, -0.001, 0.003))
    )
    paired = evaluation["home_vs_rollover_five_paired_entries"]
    assert paired["passed"] is True
    assert paired["pair_count"] == 5
    assert paired["ci90_n"][0] >= -0.05
    assert paired["ci90_n"][1] <= 0.05
    assert evaluation["campaign_qualification"] is False
    assert evaluation["tell_exact_calls"] == 0
    assert evaluation["narrow_force_windows_blocking"] is False
    assert evaluation["four_rollover_chain"]["terminal_boundary"] == "HOME"

    from step6_figure8_autotune_v1.v5_capability_acceptance import (
        V5CapabilityMeasurementResolutionError,
    )

    with pytest.raises(V5CapabilityMeasurementResolutionError, match="90% CI") as exc_info:
        evaluate_capability_results(
            _results(pair_differences=(0.08, 0.08, 0.08, 0.08, 0.08))
        )
    assert exc_info.value.estimated_pairs_required >= 0
    assert exc_info.value.as_dict()["margin_widened"] is False


def test_no_motion_receipt_is_source_triplet_and_canonical_hash_bound(
    tmp_path: Path,
) -> None:
    contract = v5_recipe_contract_receipt().as_dict()
    body = {
        "schema": "step6.autotune/autotuner-v5-live-no-motion-recipe-receipt-v1",
        "version": 1,
        "source_sha256": "1" * 64,
        "main_package_triplet_sha256": {
            "script": "2" * 64,
            "txt": "3" * 64,
            "urp": "4" * 64,
        },
        "managed_control_environment_id": "5" * 64,
        "recipe_contract": contract,
        "recipe_contract_sha256": canonical_sha256(contract),
        "rtde_setup_succeeded": True,
        "input_packet_sent": False,
        "motion_command_sent": False,
        "program_state_changed": False,
        "controller_upload_or_readback_performed": False,
        "live_acceptance_claim": False,
    }
    receipt = {**body, "receipt_sha256": canonical_sha256(body)}
    path = tmp_path / "recipe.json"
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    verified = verify_no_motion_recipe_receipt(
        path,
        expected_source_sha256="1" * 64,
        expected_triplet_sha256={
            "script": "2" * 64,
            "txt": "3" * 64,
            "urp": "4" * 64,
        },
    )
    assert verified["passed"] is True
    assert verified["input_packet_sent"] is False

    receipt["input_packet_sent"] = True
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(V5CapabilityAcceptanceError, match="no-write"):
        verify_no_motion_recipe_receipt(
            path,
            expected_source_sha256="1" * 64,
            expected_triplet_sha256={
                "script": "2" * 64,
                "txt": "3" * 64,
                "urp": "4" * 64,
            },
        )
