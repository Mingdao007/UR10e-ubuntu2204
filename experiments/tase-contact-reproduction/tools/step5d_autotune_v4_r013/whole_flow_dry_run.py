"""CUDA whole-flow falsifier for the offline-only R013 composition."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from .campaign import (
    Campaign,
    CONFIRMATION_KIND,
    MIN_EXACT_ROWS_FOR_BO,
    PhysicalAdmissionReceipt,
    R013CampaignError,
    candidate_token,
    r012_seed_source_from_ledger,
    strategy_canary_plan,
)
from .domain import candidate_to_normalized, physical_candidate_key
from .runtime_strategy import (
    RuntimeStrategyReceiptLedger,
    runtime_strategy_sha256,
    validate_runtime_strategy,
)


def _noise_from_snapshot(path: Path) -> float:
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        return float(snapshot["behavior_config"]["noise"]["noise_floor_n2"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("R012 live snapshot lacks explicit noise_floor_n2") from exc


def _synthetic_objective(candidate: Mapping[str, Any], ordinal: int) -> float:
    unit = candidate_to_normalized(candidate)
    # Deterministic, positive and deliberately kept separate from live MAE.
    return 0.55 + sum((value - 0.45) ** 2 for value in unit) * 0.08 + (ordinal % 3) * 0.002


def _synthetic_anti_windup_metrics() -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v4/r013-trial-anti-windup-v1",
        "policy": "conditional-double-clamp-v1",
        "max_abs_integral_n_s": 0.0,
        "max_abs_i_term": 0.0,
        "saturation_duty": 0.0,
        "freeze_duty": 0.0,
        "invariant_violation_count": 0,
        "reset_reasons": ["candidate_dispatch", "path_entry", "mode_exit_or_home"],
        "path_gain_hot_switch": False,
    }


def _synthetic_admission(
    dispatch: Any,
    *,
    physical_eligible: bool,
    timing_gate: bool,
    sealed_mae_n: float | None = None,
) -> PhysicalAdmissionReceipt:
    return PhysicalAdmissionReceipt(
        dispatch_id=dispatch.dispatch_id,
        candidate_token=candidate_token(dispatch.candidate),
        candidate_key=physical_candidate_key(dispatch.candidate),
        attempt_sequence=dispatch.ordinal,
        execution_id=f"offline-{dispatch.dispatch_id}",
        sealed_mae_n=(
            _synthetic_objective(dispatch.candidate, dispatch.ordinal)
            if sealed_mae_n is None
            else float(sealed_mae_n)
        ),
        physical_eligible=physical_eligible,
        timing_gate=timing_gate,
        motion_gate=True,
        qualification_passed=physical_eligible,
        observation_uid=f"offline-observation-{dispatch.ordinal}",
    )


def _synthetic_runtime_strategy_receipt(strategy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v4/r013-runtime-strategy-receipt-v1",
        "runtime_strategy_sha256": runtime_strategy_sha256(strategy),
        "enabled": True,
        "path_clock": "runtime_desired_twist_path_time_s",
        "path_sample_count": 30001,
        "formal_sample_count": 27500,
        "minimum_effective_target_n": 3.9632449269199883,
        "maximum_applied_correction_n": 1.0367550730800117,
        "first_path_time_s": 0.0,
        "last_path_time_s": 60.0,
        "maximum_path_clock_gap_s": 0.002,
        "violation_count": 0,
        "exit_restored_to_unmodified_target": True,
        "safe_return_transition_observed": True,
        "exit_mode": "safe_return",
    }


def run_strategy_canary(
    *,
    r012_ledger: Path,
    r012_live_snapshot: Path,
    runtime_strategy_profile: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Whole-flow falsifier for strategy identity, rejection, and terminal cap."""

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    strategy = validate_runtime_strategy(
        json.loads(Path(runtime_strategy_profile).read_text(encoding="utf-8"))
    )
    if strategy["enabled"] is not True:
        raise ValueError("R013 strategy canary dry run requires an enabled strategy")
    source = r012_seed_source_from_ledger(r012_ledger)
    campaign = Campaign.create(
        output_dir / "r013_ledger.jsonl",
        campaign_id="r013-offline-strategy-canary",
        run_id=output_dir.name,
        attempt_id="offline-strategy-a001",
        noise_floor_n2=_noise_from_snapshot(r012_live_snapshot),
        r012_seed_source=source,
        runtime_strategy=strategy,
    )
    campaign.install_strategy_canary_plan(
        strategy_canary_plan(
            runtime_strategy_sha256_value=campaign.runtime_strategy_sha256
        )
    )
    sidecar = RuntimeStrategyReceiptLedger(
        output_dir / "r013-runtime-strategy-receipts.jsonl",
        campaign_id=str(campaign.ledger.header["campaign_id"]),
        run_id=str(campaign.ledger.header["run_id"]),
        attempt_id=str(campaign.ledger.header["attempt_id"]),
        strategy=strategy,
    )
    receipt = _synthetic_runtime_strategy_receipt(strategy)
    for index, (admitted, objective) in enumerate(
        ((False, 0.2), (True, 0.50), (True, 0.51), (True, 0.49))
    ):
        dispatch, proposal = campaign.ask()
        if proposal is not None or dispatch.kind != "PHASE_STRATEGY_CANARY":
            raise AssertionError("R013 strategy canary was bypassed by warm or BO")
        admission = _synthetic_admission(
            dispatch,
            physical_eligible=admitted,
            timing_gate=admitted,
            sealed_mae_n=objective,
        )
        sidecar_row = sidecar.append(
            dispatch_id=dispatch.dispatch_id,
            physical_admission=admission.as_dict(),
            runtime_strategy_receipt=receipt,
        )
        campaign.tell_exact(
            admission=admission,
            anti_windup_metrics=_synthetic_anti_windup_metrics(),
            runtime_strategy_receipt=receipt,
            runtime_strategy_sidecar_sha256=str(sidecar_row["row_sha256"]),
        )
        if index < 3 and campaign.strategy_canary_terminal:
            raise AssertionError("R013 strategy canary terminated before three admitted rows")
    if (
        not campaign.strategy_canary_terminal
        or campaign.target_achieved
        or campaign.fit_receipts
        or campaign.warm_slot_cursor != 0
    ):
        raise AssertionError("R013 strategy canary terminal boundary differs")
    try:
        campaign.ask()
    except R013CampaignError as exc:
        if "canary is terminal" not in str(exc):
            raise
    else:
        raise AssertionError("R013 strategy canary scheduled after its terminal boundary")
    resumed = Campaign.resume(campaign.ledger.path)
    RuntimeStrategyReceiptLedger(
        sidecar.path,
        campaign_id=str(resumed.ledger.header["campaign_id"]),
        run_id=str(resumed.ledger.header["run_id"]),
        attempt_id=str(resumed.ledger.header["attempt_id"]),
        strategy=strategy,
    )
    report = {
        "schema": "step5d.autotune-v4/r013-strategy-canary-whole-flow-v1",
        "mode": "offline_synthetic_objective_no_live_claim",
        "passed": True,
        "runtime_strategy_sha256": resumed.runtime_strategy_sha256,
        "strategy_canary": dict(resumed.strategy_canary_summary),
        "serial_q": resumed.snapshot["q"],
        "fit_receipt_count": len(resumed.fit_receipts),
        "warm_rows_consumed": resumed.warm_slot_cursor,
        "target_achieved": resumed.target_achieved,
        "sidecar_rows": len(sidecar.rows) - 1,
        "next_dispatch_permitted": False,
    }
    (output_dir / "whole_flow_receipt.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def run(
    *,
    r012_ledger: Path,
    r012_live_snapshot: Path,
    output_dir: Path,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    ledger_path = output_dir / "r013_ledger.jsonl"
    source = r012_seed_source_from_ledger(r012_ledger)
    noise = _noise_from_snapshot(r012_live_snapshot)
    campaign = Campaign.create(
        ledger_path,
        campaign_id="r013-offline-whole-flow",
        run_id=output_dir.name,
        attempt_id="offline-a001",
        noise_floor_n2=noise,
        r012_seed_source=source,
    )
    first_six_ids: list[str] = []
    dispatch, proposal = campaign.ask()
    rejected = campaign.tell_exact(
        admission=_synthetic_admission(
            dispatch,
            physical_eligible=False,
            timing_gate=False,
        ),
        anti_windup_metrics=_synthetic_anti_windup_metrics(),
    )
    if (
        rejected.get("status") != "rejected_ineligible"
        or campaign.exact_row_count != 0
        or campaign.in_flight is not None
        or not campaign.rejected_admissions
    ):
        raise AssertionError("R013 ineligible sealed result was ingested or blocked q=1")
    if campaign.fit_receipts:
        raise AssertionError("R013 fitted before the first admitted warm observation")
    for ordinal in range(6):
        dispatch, proposal = campaign.ask()
        if proposal is not None or dispatch.kind != "WARM_FIXED_KI":
            raise AssertionError("R013 qLogNEI or non-warm dispatch appeared in warm start")
        first_six_ids.append(dispatch.dispatch_id)
        campaign.tell_exact(
            admission=_synthetic_admission(
                dispatch,
                physical_eligible=True,
                timing_gate=True,
            ),
            anti_windup_metrics=_synthetic_anti_windup_metrics(),
        )
    if campaign.exact_row_count != 6 or campaign.warm_slot_cursor != 6:
        raise AssertionError("R013 eligible result did not enter the exact warm stream")
    campaign = Campaign.resume(ledger_path)
    if (
        campaign.exact_row_count != 6
        or campaign.warm_slot_cursor != 6
        or campaign.in_flight is not None
        or campaign.fit_receipts
    ):
        raise AssertionError("R013 warm-start restart did not restore q=1 state")
    trigger, proposal = campaign.ask()
    if proposal is not None or trigger.kind != "WARM_SOBOL":
        raise AssertionError("R013 did not dispatch the next warm Sobol candidate")
    trigger_key = physical_candidate_key(trigger.candidate)
    campaign.tell_exact(
        admission=_synthetic_admission(
            trigger,
            physical_eligible=True,
            timing_gate=True,
            sealed_mae_n=0.30,
        ),
        anti_windup_metrics=_synthetic_anti_windup_metrics(),
    )
    confirmation_rejected_dispatch, proposal = campaign.ask()
    if (
        proposal is not None
        or confirmation_rejected_dispatch.kind != CONFIRMATION_KIND
        or confirmation_rejected_dispatch.abort_allowed is not False
        or physical_candidate_key(confirmation_rejected_dispatch.candidate) != trigger_key
    ):
        raise AssertionError("R013 low warm hit did not enter the confirmation lane")
    rejected_confirmation = campaign.tell_exact(
        admission=_synthetic_admission(
            confirmation_rejected_dispatch,
            physical_eligible=False,
            timing_gate=False,
        ),
        anti_windup_metrics=_synthetic_anti_windup_metrics(),
    )
    if (
        rejected_confirmation.get("status") != "rejected_ineligible"
        or campaign.exact_row_count != 7
        or campaign.confirmation_summary().pending_confirmation is None
    ):
        raise AssertionError("R013 rejected confirmation changed admitted confirmation state")
    admitted_confirmation_ids: list[str] = []
    for value in (0.34, 0.35):
        confirmation, proposal = campaign.ask()
        if (
            proposal is not None
            or confirmation.kind != CONFIRMATION_KIND
            or confirmation.abort_allowed is not False
            or physical_candidate_key(confirmation.candidate) != trigger_key
        ):
            raise AssertionError("R013 confirmation did not repeat the triggering physical key")
        admitted_confirmation_ids.append(confirmation.dispatch_id)
        campaign.tell_exact(
            admission=_synthetic_admission(
                confirmation,
                physical_eligible=True,
                timing_gate=True,
                sealed_mae_n=value,
            ),
            anti_windup_metrics=_synthetic_anti_windup_metrics(),
        )
    summary = campaign.confirmation_summary()
    if not campaign.target_achieved or summary.confirmed_incumbent is None:
        raise AssertionError("R013 confirmation did not reach target")
    if campaign.fit_receipts:
        raise AssertionError("R013 entered GP before the warm confirmation terminal")
    expected_mean = math.fsum((0.30, 0.34, 0.35)) / 3
    if summary.confirmed_incumbent.arithmetic_mean_sealed_mae_n != expected_mean:
        raise AssertionError("R013 confirmation mean is not the sealed fsum mean")
    try:
        campaign.ask()
    except R013CampaignError as exc:
        if "target_achieved" not in str(exc):
            raise AssertionError("R013 terminal ask failed without target_achieved") from exc
    else:
        raise AssertionError("R013 scheduled after target achievement")
    resumed = Campaign.resume(ledger_path)
    if (
        resumed.exact_row_count != 9
        or resumed.in_flight is not None
        or not resumed.target_achieved
        or resumed.fit_receipts
    ):
        raise AssertionError("R013 terminal confirmation restart did not restore cleanly")
    incumbent = resumed.confirmation_summary().confirmed_incumbent
    if incumbent is None or physical_candidate_key(incumbent.candidate) != trigger_key:
        raise AssertionError("R013 terminal evidence lost the triggering physical candidate")
    report = {
        "schema": "step5d.autotune-v4/r013-whole-flow-dry-run-v1",
        "mode": "offline_synthetic_objective_no_live_claim",
        "passed": True,
        "fresh_ledger": str(ledger_path.resolve()),
        "fresh_ledger_schema": resumed.ledger.header["schema"],
        "training_lineage": resumed.ledger.header["training_lineage"],
        "r012_observation_rows_exported": source["observation_rows_exported_to_r013"],
        "warm_start_exact_rows": MIN_EXACT_ROWS_FOR_BO,
        "warm_restart_after_rows": 6,
        "ineligible_dispatch_not_ingested": True,
        "eligible_ingest_after_ineligible": True,
        "first_six_dispatch_ids": first_six_ids,
        "model_dimensions": resumed.snapshot["model_dimensions"],
        "serial_q": resumed.snapshot["q"],
        "candidate_pool_size": resumed.snapshot["candidate_pool_size"],
        "no_gp_before_target": True,
        "proposal": None,
        "proposal_physical_key": None,
        "post_tell_exact_rows": resumed.exact_row_count,
        "confirmation_rejected_dispatch_id": confirmation_rejected_dispatch.dispatch_id,
        "confirmation_rejected_not_counted": (
            rejected_confirmation.get("exact_observation_added") is False
        ),
        "confirmation_admitted_dispatch_ids": admitted_confirmation_ids,
        "confirmation_trigger_physical_key": list(trigger_key),
        "target_achieved": resumed.target_achieved,
        "target_status": "target_achieved",
        "fit_receipt": None,
        "noise_floor_n2": resumed.gp_config.noise_floor_n2,
        "noise_snapshot_sha256": resumed.gp_config.noise_snapshot_sha256,
        "sealed_mae_transform": "identity",
        "confirmation": resumed.confirmation_summary().as_dict(),
    }
    (output_dir / "whole_flow_receipt.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--r012-ledger", type=Path, required=True)
    parser.add_argument("--r012-live-snapshot", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--runtime-strategy-profile", type=Path)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = (
        run_strategy_canary(
            r012_ledger=args.r012_ledger,
            r012_live_snapshot=args.r012_live_snapshot,
            runtime_strategy_profile=args.runtime_strategy_profile,
            output_dir=args.output_dir,
        )
        if args.runtime_strategy_profile is not None
        else run(
            r012_ledger=args.r012_ledger,
            r012_live_snapshot=args.r012_live_snapshot,
            output_dir=args.output_dir,
        )
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
