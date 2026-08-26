#!/usr/bin/env python3
"""Run the R013 physical campaign as one resumable serial live owner.

The controller program is deliberately not loaded or played here.  Package
delivery and the resident PLAYING boundary are separate receipts; this
process consumes that boundary and lets the mature R006/R008 writer own the
RTDE and safe-return lifecycle.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable, Mapping

from step5d_autotune_v4_r013.campaign import (
    CONFIRMATION_THRESHOLD_N,
    Campaign,
    R013CampaignError,
)
from step5d_autotune_v4_r013.bounded_bo import (
    BOUNDED_BO_POLICY,
    BoundedBOConfigError,
    BoundedBOSeedConfigV1,
)
from step5d_autotune_v4_r013.campaign_config import (
    DEFAULT_CONFIG_PATH,
    load_r013_budgeted_floor_config,
    materialize_campaign_fingerprint,
    require_campaign_config_binding,
)
from step5d_autotune_v4_r013.feedforward import FeedforwardProfile
from step5d_autotune_v4_r013.identity import LEGACY_PROFILE_IDENTITY
from step5d_autotune_v4_r013.live_owner import (
    R013OwnerError,
    build_r013_live_context,
)
from step5d_autotune_v4_r013.timing_scheduler import (
    LATE_CONTROL_FIFO_PROFILE,
    QUOTA_SAFE_OTHER_PROFILE,
)
from ur10e_parallel import (
    ResourceProfile,
    formal_timing_lease,
    formal_timing_owner,
    notify_formal_timing_owner,
)
from step5d_autotune_v4_r012.path_cbf_live import (
    R012_HARD_TUBE_AXES_M,
    R012_SOFT_CBF_AXES_M,
)


DEFAULT_CONTROLLER = "192.168.1.18"
DEFAULT_KUNWEI = "192.168.50.25"
DEFAULT_KUNWEI_PORT = 5152
CURRENT_LIVE_READY_CONFIG_MODE = "current-live-ready"
LEGACY_PREPARED_CONFIG_MODE = "legacy-prepared-v1"


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _resume_materialized_fingerprint_check(
    *,
    campaign: Campaign,
    config: Any,
    run_dir: Path,
    feedforward_profile: FeedforwardProfile,
) -> None:
    if campaign.completion_policy.policy != "budgeted_floor_v1":
        return
    from step5d_autotune_v4_r013.prepare_live import (
        R013_PROGRAM,
        SCRIPT1_SOURCE,
        _controller_triplet_from_receipt,
        _r013_identity,
    )

    receipt_path = Path(run_dir) / "controller_receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        raise SystemExit("R013 persisted controller source receipt is missing")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("R013 persisted controller source receipt is unreadable") from exc
    if not isinstance(receipt, Mapping):
        raise SystemExit("R013 persisted controller source receipt is invalid")
    triplet = _controller_triplet_from_receipt(receipt)
    package_dir = Path(__file__).resolve().parents[1] / "programs" / "step5" / "step5d"
    for role in ("script", "txt", "urp"):
        package_path = package_dir / f"{R013_PROGRAM}.{role}"
        if package_path.is_symlink() or not package_path.is_file() or _sha(package_path) != triplet[role]:
            raise SystemExit("R013 local controller source identity differs")
    _contract, _contract_sha, _contract_fingerprint, eoat_sha = _r013_identity()
    materialized = materialize_campaign_fingerprint(
        config,
        runtime_strategy_sha256_value=campaign.runtime_strategy_sha256,
        controller_triplet_sha256=triplet,
        eoat_identity_sha256=eoat_sha,
        script1_source_sha256=_sha(SCRIPT1_SOURCE),
        feedforward_profile=(
            None
            if feedforward_profile.enabled
            and all(
                value == LEGACY_PROFILE_IDENTITY
                for value in (
                    campaign.campaign_fingerprint.feedforward_profile_identity,
                    campaign.campaign_fingerprint.motion_admission_profile_identity,
                    campaign.campaign_fingerprint.baseline_transition_profile_identity,
                    campaign.campaign_fingerprint.baseline_residual_policy_identity,
                )
            )
            else feedforward_profile
        ),
    )
    if materialized != campaign.campaign_fingerprint:
        raise SystemExit("R013 resumed materialized campaign fingerprint differs")
    if (
        campaign.snapshot.get("materialized_campaign_fingerprint") != materialized.as_dict()
        or campaign.snapshot.get("materialized_campaign_fingerprint_sha256") != materialized.sha256
    ):
        raise SystemExit("R013 resumed materialized fingerprint receipt differs")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    """Publish a complete status/receipt without exposing a partial JSON."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(dict(value), stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def _json_value(value: Any) -> Any:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--controller-host", default=DEFAULT_CONTROLLER)
    parser.add_argument("--kunwei-host", default=DEFAULT_KUNWEI)
    parser.add_argument("--kunwei-port", type=int, default=DEFAULT_KUNWEI_PORT)
    parser.add_argument("--launch-profile", type=Path, required=True)
    parser.add_argument(
        "--feedforward",
        choices=("on", "off"),
        default="on",
        help="Immutable run-level PATH velocity feedforward mode.",
    )
    parser.add_argument(
        "--solver-profile",
        choices=("finite-time", "legacy-r1"),
        default="legacy-r1",
        help="Exact typed strict-RNN profile; no automatic fallback.",
    )
    parser.add_argument(
        "--campaign-config-mode",
        choices=(CURRENT_LIVE_READY_CONFIG_MODE, LEGACY_PREPARED_CONFIG_MODE),
        default=CURRENT_LIVE_READY_CONFIG_MODE,
        help=(
            "Use current receipt-bound config gates, or consume an already "
            "prepared legacy-v1 campaign without reclassifying it as the new floor campaign."
        ),
    )
    parser.add_argument(
        "--campaign-config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
    )
    parser.add_argument("--poll-s", type=float, default=0.25)
    parser.add_argument(
        "--timing-scheduler-profile",
        choices=(LATE_CONTROL_FIFO_PROFILE, QUOTA_SAFE_OTHER_PROFILE),
        default=LATE_CONTROL_FIFO_PROFILE,
        help="Per-attempt scheduler lease; helpers remain SCHED_OTHER.",
    )
    parser.add_argument(
        "--bounded-bo-seed",
        type=Path,
        help="Immutable bounded_bo_v1 seed/control-law receipt for the fresh campaign.",
    )
    parser.add_argument(
        "--physical-attempt-budget",
        type=int,
        help="Optional bounded_bo_v1 budget assertion; must match the seed receipt.",
    )
    parser.add_argument(
        "--bounded-bo-preflight",
        type=Path,
        help="Timing 3/3 + resident boundary 2/2 receipt; defaults inside run-dir.",
    )
    return parser.parse_args(argv)


def _validate_bounded_bo_seed_binding(
    *, campaign: Campaign, run_dir: Path, seed_path: Path | None, budget: int | None
) -> BoundedBOSeedConfigV1 | None:
    """Bind the immutable seed receipt to the persisted fresh campaign."""

    if campaign.bounded_bo_profile is None:
        if seed_path is not None or budget is not None:
            raise SystemExit("bounded BO arguments require a bounded_bo_v1 campaign")
        return None
    source = Path(seed_path) if seed_path is not None else Path(run_dir) / "bounded_bo_seed.json"
    try:
        seed = BoundedBOSeedConfigV1.load(source)
        seed.verify_profile(campaign.bounded_bo_profile, seed)
    except (BoundedBOConfigError, OSError, ValueError) as exc:
        raise SystemExit(f"R013 bounded BO seed binding failed: {exc}") from exc
    if dict(campaign.snapshot.get("seed_template", {})) != dict(seed.candidate):
        raise SystemExit("R013 bounded BO seed candidate differs from campaign template")
    if budget is not None and budget != seed.physical_attempt_budget:
        raise SystemExit("R013 bounded BO physical-attempt budget differs from seed receipt")
    return seed


def _validate_bounded_bo_preflight(
    *, campaign: Campaign, seed: BoundedBOSeedConfigV1, run_dir: Path, receipt_path: Path | None
) -> dict[str, Any]:
    source = Path(receipt_path) if receipt_path is not None else Path(run_dir) / "r013_bounded_bo_preflight.json"
    if source.is_symlink() or not source.is_file():
        raise SystemExit(f"R013 bounded BO preflight receipt is missing: {source}")
    try:
        receipt = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("R013 bounded BO preflight receipt is unreadable") from exc
    if not isinstance(receipt, Mapping) or receipt.get("schema") != "step5d.autotune-v4/r013-bounded-bo-preflight-v1":
        raise SystemExit("R013 bounded BO preflight receipt schema differs")
    if receipt.get("version") != 1:
        raise SystemExit("R013 bounded BO preflight receipt version differs")
    if receipt.get("campaign_fingerprint_sha256") != campaign.campaign_fingerprint.sha256:
        raise SystemExit("R013 bounded BO preflight campaign identity differs")
    if receipt.get("source_closure_sha256") != seed.source_closure_sha256:
        raise SystemExit("R013 bounded BO preflight source identity differs")
    if receipt.get("runtime_strategy_sha256") != seed.runtime_strategy_sha256:
        raise SystemExit("R013 bounded BO preflight control-law identity differs")
    timing = receipt.get("timing_characterization")
    boundary = receipt.get("resident_boundary_canary")
    if not isinstance(timing, Mapping) or not isinstance(boundary, Mapping):
        raise SystemExit("R013 bounded BO preflight sections are incomplete")
    if (timing.get("attempts_completed"), timing.get("attempts_passed")) != (3, 3):
        raise SystemExit("R013 bounded BO timing characterization is not 3/3")
    if (boundary.get("attempts_completed"), boundary.get("attempts_passed")) != (2, 2):
        raise SystemExit("R013 bounded BO resident boundary canary is not 2/2")
    if receipt.get("verified_home") is not True or receipt.get("safety_normal") is not True:
        raise SystemExit("R013 bounded BO preflight lacks verified Home/Safety NORMAL")
    return dict(receipt)


def _require_legacy_prepared_campaign_binding(
    *,
    campaign: Campaign,
    run_dir: Path,
) -> dict[str, Any]:
    """Validate one pre-floor-schema prepared campaign without forging readiness.

    This compatibility seam never accepts the current offline floor-discovery
    config.  It consumes only a campaign that was already prepared by the
    legacy five-field config contract and whose resident/package identities
    still close over the persisted preparation receipts.
    """

    legacy_config = campaign.snapshot.get("budgeted_floor_config")
    legacy_fields = {
        "schema",
        "version",
        "completion_policy",
        "handoff_policy",
        "campaign_fingerprint",
    }
    if not isinstance(legacy_config, Mapping) or set(legacy_config) != legacy_fields:
        raise SystemExit(
            "legacy-prepared-v1 requires the exact pre-floor-schema campaign config"
        )
    preparation_path = Path(run_dir) / "r013_live_preparation.json"
    ready_path = Path(run_dir) / "r013_live_owner_ready.json"
    launch_path = Path(run_dir) / "launch_context.json"
    controller_path = Path(run_dir) / "controller_receipt.json"
    for path in (preparation_path, ready_path, launch_path, controller_path):
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"legacy-prepared-v1 receipt is missing: {path.name}")
    try:
        preparation = json.loads(preparation_path.read_text(encoding="utf-8"))
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        launch = json.loads(launch_path.read_text(encoding="utf-8"))
        controller = json.loads(controller_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemExit("legacy-prepared-v1 receipt is unreadable") from exc
    if not all(isinstance(value, Mapping) for value in (preparation, ready, launch, controller)):
        raise SystemExit("legacy-prepared-v1 receipt is not an object")
    if (
        preparation.get("preparation_only") is not True
        or preparation.get("controller_readback_verified") is not True
        or preparation.get("arm_dispatched") is not False
        or preparation.get("trial_dispatched") is not False
        or preparation.get("budgeted_floor_config") != legacy_config
        or preparation.get("fresh_campaign_ledger")
        != str((Path(run_dir) / "r013_ledger.jsonl").resolve())
    ):
        raise SystemExit("legacy-prepared-v1 preparation boundary differs")
    expected_identity = {
        "campaign_id": str(campaign.ledger.header["campaign_id"]),
        "run_id": str(campaign.ledger.header["run_id"]),
        "attempt_id": str(campaign.ledger.header["attempt_id"]),
    }
    for value in (preparation, launch):
        if any(value.get(key) != expected for key, expected in expected_identity.items()):
            raise SystemExit("legacy-prepared-v1 campaign identity differs")
    if ready.get("attempt_id") != expected_identity["attempt_id"]:
        raise SystemExit("legacy-prepared-v1 resident attempt identity differs")
    materialized = campaign.campaign_fingerprint.as_dict()
    if (
        preparation.get("materialized_campaign_fingerprint") != materialized
        or preparation.get("materialized_campaign_fingerprint_sha256")
        != campaign.campaign_fingerprint.sha256
        or campaign.snapshot.get("materialized_campaign_fingerprint") != materialized
        or campaign.snapshot.get("materialized_campaign_fingerprint_sha256")
        != campaign.campaign_fingerprint.sha256
    ):
        raise SystemExit("legacy-prepared-v1 materialized fingerprint differs")
    from step5d_autotune_v4_r013.prepare_live import (
        R013_PROGRAM,
        _controller_triplet_from_receipt,
    )

    triplet = _controller_triplet_from_receipt(controller)
    if any(
        value.get("triplet") != triplet
        for value in (ready, launch)
    ) or preparation.get("package_triplet") != triplet:
        raise SystemExit("legacy-prepared-v1 controller triplet binding differs")
    package_dir = Path(__file__).resolve().parents[1] / "programs" / "step5" / "step5d"
    for role in ("script", "txt", "urp"):
        package_path = package_dir / f"{R013_PROGRAM}.{role}"
        if package_path.is_symlink() or not package_path.is_file() or _sha(package_path) != triplet[role]:
            raise SystemExit("legacy-prepared-v1 local controller source identity differs")
    return {
        "schema": "step5d.autotune-v4/r013-campaign-execution-profile-v1",
        "campaign_config_mode": LEGACY_PREPARED_CONFIG_MODE,
        "campaign_id": expected_identity["campaign_id"],
        "run_id": expected_identity["run_id"],
        "attempt_id": expected_identity["attempt_id"],
        "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "controller_triplet": triplet,
        "legacy_prepared_receipts_verified": True,
        "new_floor_launch_ready_claimed": False,
    }


def _status_path(run_dir: Path) -> Path:
    return Path(run_dir) / "r013_campaign_status.json"


def _trial_path(run_dir: Path, ordinal: int) -> Path:
    return Path(run_dir) / f"r013_trial_{int(ordinal):04d}.json"


def _bounded_bo_report(*, campaign: Campaign, run_dir: Path) -> dict[str, Any]:
    """Build a deterministic closeout/checkpoint view from fresh ledger rows."""

    if campaign.bounded_bo_profile is None:
        raise R013CampaignError("bounded BO report requested for a non-bounded campaign")
    gap_counts: dict[str, int] = {}
    censored_count = 0
    for ordinal in range(1, campaign.physical_attempt_count + 1):
        path = _trial_path(run_dir, ordinal)
        if not path.is_file():
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        result = receipt.get("result", {}) if isinstance(receipt, Mapping) else {}
        if isinstance(result, Mapping) and result.get("censored_observation") is not None:
            censored_count += 1
        classification = (
            result.get("arm_transition_classification", {}).get("classification")
            if isinstance(result, Mapping)
            and isinstance(result.get("arm_transition_classification"), Mapping)
            else "insufficient_evidence"
        )
        gap_counts[str(classification)] = gap_counts.get(str(classification), 0) + 1
    exact_rows = list(campaign.observations)
    rejected_rows = list(campaign.rejected_admissions)
    timing_ineligible = sum(
        1
        for row in rejected_rows
        if "timing_gate" in set(row.get("reasons", ()))
        or row.get("physical_admission", {}).get("timing_gate") is False
    )
    best = min(
        (float(row["objective_n"]) for row in exact_rows),
        default=None,
    )
    return {
        "schema": "step5d.autotune-v4/r013-bounded-bo-report-v1",
        "version": 1,
        "policy": BOUNDED_BO_POLICY,
        "physical_attempt_count": campaign.physical_attempt_count,
        "physical_attempt_budget": campaign.bounded_bo_profile["physical_attempt_budget"],
        "exact_admitted_count": len(exact_rows),
        "timing_ineligible_count": timing_ineligible,
        "censored_count": censored_count,
        "rejected_admission_count": len(rejected_rows),
        "hard_guard_count": sum(
            1 for record in campaign.ledger.records if record.get("record_type") == "hard_guard"
        ),
        "best_admitted_single_mae_n": best,
        "target_checkpoint": campaign.target_checkpoint,
        "target_mae_n": CONFIRMATION_THRESHOLD_N,
        "gap_classification_counts": dict(sorted(gap_counts.items())),
        "confirmation": _json_value(campaign.confirmation_summary()),
        "campaign_fingerprint": _json_value(campaign.campaign_fingerprint),
        "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "bounded_bo_profile": dict(campaign.bounded_bo_profile),
    }


def _write_bounded_bo_checkpoint(*, campaign: Campaign, run_dir: Path) -> None:
    if campaign.bounded_bo_profile is None or campaign.physical_attempt_count % 10 != 0:
        return
    checkpoint = _bounded_bo_report(campaign=campaign, run_dir=run_dir)
    checkpoint["checkpoint_attempt"] = campaign.physical_attempt_count
    _write_json(
        Path(run_dir) / f"r013_bounded_bo_checkpoint_{campaign.physical_attempt_count:03d}.json",
        checkpoint,
    )


def _base_status(*, run_dir: Path, campaign: Campaign, active: bool) -> dict[str, Any]:
    return {
        "schema": "step5d.autotune-v4/r013-campaign-status-v1",
        "pid": os.getpid(),
        "run_dir": str(Path(run_dir).resolve()),
        "active": bool(active),
        "exact_row_count": campaign.exact_row_count,
        "dispatch_count": len(campaign.dispatches),
        "physical_attempt_count": campaign.physical_attempt_count,
        "q": int(campaign.snapshot["q"]),
        "model_dimensions": int(campaign.snapshot["model_dimensions"]),
        "candidate_pool_size": int(campaign.snapshot["candidate_pool_size"]),
        "rejected_admission_count": len(campaign.rejected_admissions),
        "confirmation": _json_value(campaign.confirmation_summary()),
        "confirmation_target": _json_value(campaign.confirmation_target),
        "completion_policy": _json_value(campaign.completion_policy),
        "novel_exact_candidate_target": campaign.completion_policy.novel_exact_candidate_target,
        "target_checkpoint_only": campaign.completion_policy.checkpoint_only_target,
        "target_mae_n": campaign.completion_policy.target_mae_n,
        "handoff_policy": _json_value(campaign.handoff_policy),
        "campaign_fingerprint": _json_value(campaign.campaign_fingerprint),
        "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "budgeted_floor_config": campaign.snapshot.get("budgeted_floor_config"),
        "campaign_fingerprint_template": (
            campaign.snapshot.get("budgeted_floor_config", {}).get("campaign_fingerprint")
            if isinstance(campaign.snapshot.get("budgeted_floor_config"), Mapping)
            else None
        ),
        "materialized_campaign_fingerprint": _json_value(campaign.campaign_fingerprint),
        "materialized_campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
        "floor_discovery_policy": campaign.snapshot.get("floor_discovery_policy"),
        "floor_discovery": _json_value(campaign.floor_discovery_status),
        "target_achieved": campaign.target_achieved,
        "bounded_bo": campaign.bounded_bo_profile,
        "bounded_bo_budget_exhausted": campaign.bounded_bo_budget_exhausted,
        "runtime_strategy": _json_value(campaign.runtime_strategy),
        "runtime_strategy_sha256": campaign.runtime_strategy_sha256,
        "strategy_canary": _json_value(campaign.strategy_canary_summary),
        "anchor_retest": _json_value(campaign.anchor_retest_summary),
        "local_refinement": _json_value(campaign.local_refinement_summary),
        "high_ki_probe": _json_value(campaign.high_ki_probe_summary),
        "normal_velocity_gain_probe": _json_value(
            campaign.normal_velocity_gain_probe_summary
        ),
        "lower_p_over_d_probe": _json_value(campaign.lower_p_over_d_probe_summary),
        "noise_floor_n2": float(campaign.snapshot["noise_floor_n2"]),
        "soft_tube": {
            "mode": "active",
            "semi_axes_m": list(R012_SOFT_CBF_AXES_M),
        },
        "hard_tube": {
            "enabled": True,
            "independent": True,
            "checked_first": True,
            "axes_m": list(R012_HARD_TUBE_AXES_M),
        },
    }


def _handle_target_achieved(
    *,
    campaign: Campaign,
    context: Any,
    status: Mapping[str, Any],
    write_status: Callable[[Mapping[str, Any]], None],
) -> Mapping[str, Any] | None:
    """Publish target evidence, then revoke live authority exactly once."""

    summary = campaign.confirmation_summary()
    if not campaign.target_achieved:
        return None
    target_evidence = (
        _json_value(summary.confirmed_incumbent)
        if campaign.completion_policy.policy != "budgeted_floor_v1"
        else _json_value(campaign.completion_status)
    )
    terminal = {
        **dict(status),
        "state": "target_achieved",
        "terminal": True,
        "target_achieved": True,
        "confirmation": _json_value(summary),
        "target_evidence": target_evidence,
    }
    write_status(terminal)
    context.stop("target_achieved")
    return terminal


def _handle_bounded_bo_budget(
    *,
    campaign: Campaign,
    context: Any,
    run_dir: Path,
    status: Mapping[str, Any],
    write_status: Callable[[Mapping[str, Any]], None],
) -> Mapping[str, Any] | None:
    """Close a bounded BO lane only after its physical budget is sealed."""

    if not campaign.bounded_bo_budget_exhausted:
        return None
    terminal = {
        **dict(status),
        "state": "bounded_bo_budget_exhausted",
        "terminal": True,
        "target_achieved": False,
        "target_checkpoint": campaign.target_checkpoint,
        "physical_attempt_count": campaign.physical_attempt_count,
        "physical_attempt_budget": campaign.bounded_bo_profile["physical_attempt_budget"],
        "next_dispatch_permitted": False,
        "confirmation_is_checkpoint_only": True,
    }
    _write_json(
        Path(run_dir) / "r013_bounded_bo_report.json",
        _bounded_bo_report(campaign=campaign, run_dir=Path(run_dir)),
    )
    write_status(terminal)
    context.stop("bounded_bo_budget_exhausted")
    return terminal


def _handle_strategy_canary_terminal(
    *,
    campaign: Campaign,
    context: Any,
    status: Mapping[str, Any],
    write_status: Callable[[Mapping[str, Any]], None],
) -> Mapping[str, Any] | None:
    """Stop safely after the bounded strategy falsifier without entering BO."""

    summary = dict(campaign.strategy_canary_summary)
    if campaign.bounded_bo_profile is not None and not summary["attempt_limit_exhausted"]:
        # bounded_bo_v1 uses the strategy canary as a fresh falsifier, then
        # continues into warm-start/GP even when the canary does not hit 0.35 N.
        return None
    if not summary["terminal"] or campaign.target_achieved:
        return None
    state = (
        "strategy_canary_attempt_limit_exhausted"
        if summary["attempt_limit_exhausted"]
        else "strategy_canary_complete_no_target"
    )
    terminal = {
        **dict(status),
        "state": state,
        "terminal": True,
        "target_achieved": False,
        "strategy_canary": summary,
        "next_dispatch_permitted": False,
    }
    write_status(terminal)
    context.stop(state)
    return terminal


def _startup_resume_terminal(
    *,
    campaign: Campaign,
    run_dir: Path,
) -> tuple[int, dict[str, Any]] | None:
    """Resolve terminal/recovery resume states before any live context opens."""

    if (
        campaign.runtime_strategy.get("enabled") is True
        and campaign.strategy_canary_plan is None
    ):
        return 1, {
            **_base_status(run_dir=run_dir, campaign=campaign, active=False),
            "state": "configuration_invalid",
            "terminal": True,
            "startup_resume": True,
            "error": "enabled runtime strategy lacks its immutable fresh canary plan",
        }

    if campaign.in_flight is not None:
        dispatch = campaign.in_flight
        return 1, {
            **_base_status(run_dir=run_dir, campaign=campaign, active=False),
            "state": "recovery_required",
            "terminal": True,
            "startup_resume": True,
            "recovery_required": {
                "reason": "outstanding in-flight dispatch; automatic motion retry is forbidden",
                "dispatch": _json_value(dispatch),
            },
            "error": "R013 resume requires recovery of the outstanding in-flight dispatch",
        }
    if campaign.bounded_bo_budget_exhausted:
        return 0, {
            **_base_status(run_dir=run_dir, campaign=campaign, active=False),
            "state": "bounded_bo_budget_exhausted",
            "terminal": True,
            "startup_resume": True,
            "target_achieved": False,
            "target_checkpoint": campaign.target_checkpoint,
            "next_dispatch_permitted": False,
            "error": None,
        }
    if campaign.target_achieved:
        summary = campaign.confirmation_summary()
        return 0, {
            **_base_status(run_dir=run_dir, campaign=campaign, active=False),
            "state": "target_achieved",
            "terminal": True,
            "startup_resume": True,
            "target_evidence": (
                _json_value(summary.confirmed_incumbent)
                if campaign.completion_policy.policy != "budgeted_floor_v1"
                else _json_value(campaign.completion_status)
            ),
            "error": None,
        }
    if campaign.strategy_canary_terminal and not (
        campaign.bounded_bo_profile is not None
        and not campaign.strategy_canary_summary["attempt_limit_exhausted"]
    ):
        summary = dict(campaign.strategy_canary_summary)
        state = (
            "strategy_canary_attempt_limit_exhausted"
            if summary["attempt_limit_exhausted"]
            else "strategy_canary_complete_no_target"
        )
        return 2, {
            **_base_status(run_dir=run_dir, campaign=campaign, active=False),
            "state": state,
            "terminal": True,
            "startup_resume": True,
            "target_achieved": False,
            "next_dispatch_permitted": False,
            "error": None,
        }
    return None


def main() -> int:
    args = _parse_args()
    run_dir = Path(args.run_dir).resolve()
    ledger_path = run_dir / "r013_ledger.jsonl"
    if args.poll_s <= 0.0:
        raise SystemExit("--poll-s must be positive")
    if not ledger_path.is_file():
        raise SystemExit(f"fresh R013 ledger is missing: {ledger_path}")
    # ``resume`` is intentional: the preparation seam already created the
    # fresh header and the owner must never create a second ledger.
    campaign = Campaign.resume(ledger_path)
    bounded_seed = _validate_bounded_bo_seed_binding(
        campaign=campaign,
        run_dir=run_dir,
        seed_path=getattr(args, "bounded_bo_seed", None),
        budget=getattr(args, "physical_attempt_budget", None),
    )
    bounded_preflight = (
        None
        if bounded_seed is None
        else _validate_bounded_bo_preflight(
            campaign=campaign,
            seed=bounded_seed,
            run_dir=run_dir,
            receipt_path=getattr(args, "bounded_bo_preflight", None),
        )
    )
    feedforward_profile = FeedforwardProfile.from_value(
        getattr(args, "feedforward", "on")
    )
    from step5d_autotune_v4_r014.solver_profile import FINITE_TIME_R08, LEGACY_R1

    solver_profile = (
        FINITE_TIME_R08
        if getattr(args, "solver_profile", "legacy-r1") == "finite-time"
        else LEGACY_R1
    )
    campaign_config_mode = getattr(
        args,
        "campaign_config_mode",
        CURRENT_LIVE_READY_CONFIG_MODE,
    )
    execution_profile: dict[str, Any]
    if campaign.completion_policy.policy == "budgeted_floor_v1":
        if campaign_config_mode == LEGACY_PREPARED_CONFIG_MODE:
            execution_profile = _require_legacy_prepared_campaign_binding(
                campaign=campaign,
                run_dir=run_dir,
            )
        else:
            campaign_config = load_r013_budgeted_floor_config(
                getattr(args, "campaign_config", DEFAULT_CONFIG_PATH)
            )
            try:
                require_campaign_config_binding(
                    config=campaign_config,
                    snapshot=campaign.snapshot,
                )
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            _resume_materialized_fingerprint_check(
                campaign=campaign,
                config=campaign_config,
                run_dir=run_dir,
                feedforward_profile=feedforward_profile,
            )
            execution_profile = {
                "schema": "step5d.autotune-v4/r013-campaign-execution-profile-v1",
                "campaign_config_mode": CURRENT_LIVE_READY_CONFIG_MODE,
                "campaign_id": str(campaign.ledger.header["campaign_id"]),
                "run_id": str(campaign.ledger.header["run_id"]),
                "attempt_id": str(campaign.ledger.header["attempt_id"]),
                "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
                "legacy_prepared_receipts_verified": False,
                "new_floor_launch_ready_claimed": True,
            }
    else:
        if campaign_config_mode != CURRENT_LIVE_READY_CONFIG_MODE:
            raise SystemExit(
                "legacy-prepared-v1 is limited to budgeted-floor legacy campaigns"
            )
        execution_profile = {
            "schema": "step5d.autotune-v4/r013-campaign-execution-profile-v1",
            "campaign_config_mode": CURRENT_LIVE_READY_CONFIG_MODE,
            "campaign_id": str(campaign.ledger.header["campaign_id"]),
            "run_id": str(campaign.ledger.header["run_id"]),
            "attempt_id": str(campaign.ledger.header["attempt_id"]),
            "campaign_fingerprint_sha256": campaign.campaign_fingerprint.sha256,
            "legacy_prepared_receipts_verified": False,
            "new_floor_launch_ready_claimed": False,
        }
    execution_profile["feedforward"] = feedforward_profile.as_dict()
    if bounded_seed is not None:
        execution_profile["bounded_bo"] = bounded_seed.campaign_profile()
        execution_profile["bounded_bo_seed_receipt_sha256"] = bounded_seed.receipt_sha256
        execution_profile["bounded_bo_preflight"] = bounded_preflight
    execution_profile["timing_scheduler_profile"] = str(
        getattr(args, "timing_scheduler_profile", LATE_CONTROL_FIFO_PROFILE)
    )
    _write_json(run_dir / "r013_campaign_execution_profile.json", execution_profile)
    if campaign.stopped:
        raise SystemExit("R013 campaign ledger is already stopped by hard guard")
    startup_terminal = _startup_resume_terminal(campaign=campaign, run_dir=run_dir)
    if startup_terminal is not None:
        startup_exit_code, startup_status = startup_terminal
        _write_json(_status_path(run_dir), startup_status)
        return startup_exit_code

    timing_profile = ResourceProfile.from_env()
    timing_stack = ExitStack()
    try:
        timing_owner = timing_stack.enter_context(
            formal_timing_lease(
                timing_profile,
                task=f"r013-live:{run_dir}",
                blocking=False,
            )
        )
    except BlockingIOError as exc:
        owner = formal_timing_owner(timing_profile)
        notice_sent = notify_formal_timing_owner(
            owner,
            f"V4 live owner is blocked by formal_timing resource: {run_dir}",
        )
        blocked = {
            "schema": "ur10e/formal-timing-resource-block-v1",
            "resource": "formal_timing",
            "owner": owner,
            "notice_sent": notice_sent,
            "run_dir": str(run_dir),
            "action": "stop_no_retry",
        }
        execution_profile["formal_timing_lease"] = {
            "acquired": False,
            **blocked,
        }
        _write_json(run_dir / "r013_campaign_execution_profile.json", execution_profile)
        _write_json(
            _status_path(run_dir),
            {
                **_base_status(run_dir=run_dir, campaign=campaign, active=False),
                "state": "resource_blocked",
                "terminal": True,
                "resource_block": blocked,
                "error": f"formal_timing resource is busy: {owner!r}",
            },
        )
        raise SystemExit(75) from exc

    execution_profile["formal_timing_lease"] = {
        "acquired": True,
        **timing_owner,
    }
    _write_json(run_dir / "r013_campaign_execution_profile.json", execution_profile)

    stop_requested = False
    signal_name: str | None = None

    def request_stop(signum: int, _frame: Any) -> None:
        nonlocal stop_requested, signal_name
        stop_requested = True
        try:
            signal_name = signal.Signals(signum).name
        except ValueError:
            signal_name = str(signum)

    previous_signals: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_signals[signum] = signal.getsignal(signum)
        signal.signal(signum, request_stop)

    _write_json(
        _status_path(run_dir),
        {
            **_base_status(run_dir=run_dir, campaign=campaign, active=True),
            "state": "starting",
            "started_at_unix_s": time.time(),
            "fresh_ledger": True,
        },
    )

    context = None
    exit_code = 0
    last_error: str | None = None
    target_achieved = False
    target_terminal_status: Mapping[str, Any] | None = None
    bounded_bo_terminal_status: Mapping[str, Any] | None = None
    strategy_canary_terminal_status: Mapping[str, Any] | None = None
    try:
        context = build_r013_live_context(
            run_dir=run_dir,
            controller_host=str(args.controller_host),
            kunwei_host=str(args.kunwei_host),
            kunwei_port=int(args.kunwei_port),
            launch_profile=Path(args.launch_profile).resolve(),
            campaign_id=str(campaign.ledger.header["campaign_id"]),
            run_id=str(campaign.ledger.header["run_id"]),
            attempt_id=str(campaign.ledger.header["attempt_id"]),
            runtime_strategy=campaign.runtime_strategy,
            campaign=campaign,
            feedforward_mode=feedforward_profile.mode,
            solver_profile=solver_profile,
            timing_scheduler_profile=str(
                getattr(args, "timing_scheduler_profile", LATE_CONTROL_FIFO_PROFILE)
            ),
        )
        _write_json(
            _status_path(run_dir),
            {
                **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                "state": "owner_open",
                "started_at_unix_s": time.time(),
                "state25_sidecar": str(context.state25_sidecar or ""),
                "physical_ledger": str(context.physical_ledger.path),
            },
        )

        while not stop_requested:
            if campaign.bounded_bo_budget_exhausted:
                bounded_bo_terminal_status = _handle_bounded_bo_budget(
                    campaign=campaign,
                    context=context,
                    run_dir=run_dir,
                    status={
                        **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                        "state": "bounded_bo_budget_exhausted",
                    },
                    write_status=lambda value: _write_json(_status_path(run_dir), value),
                )
                break
            try:
                dispatch, proposal = campaign.ask()
            except R013CampaignError as exc:
                last_error = f"ask: {type(exc).__name__}: {exc}"
                exit_code = 1
                break

            dispatch_value = _json_value(dispatch)
            proposal_value = _json_value(proposal) if proposal is not None else None
            _write_json(
                _status_path(run_dir),
                {
                    **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                    "state": "trial_running",
                    "dispatch": dispatch_value,
                    "proposal": proposal_value,
                    "started_at_unix_s": time.time(),
                },
            )
            try:
                result = context.run_trial(dispatch)
                observation = campaign.tell_exact(
                    admission=result["physical_admission"],
                    anti_windup_metrics=result["anti_windup"],
                    runtime_strategy_receipt=result.get("runtime_strategy"),
                    runtime_strategy_sidecar_sha256=result.get(
                        "runtime_strategy_sidecar_sha256"
                    ),
                )
                receipt = {
                    "schema": "step5d.autotune-v4/r013-trial-receipt-v1",
                    "recorded_at_unix_s": time.time(),
                    "dispatch": dispatch_value,
                    "proposal": proposal_value,
                    "result": _json_value(result),
                    "observation": _json_value(observation),
                }
                _write_json(_trial_path(run_dir, dispatch.ordinal), receipt)
                _write_bounded_bo_checkpoint(campaign=campaign, run_dir=run_dir)
                target_terminal_status = _handle_target_achieved(
                    campaign=campaign,
                    context=context,
                    status={
                        **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                        "state": (
                            "exact_sealed"
                            if observation.get("eligible") is True
                            else "physical_ineligible"
                        ),
                        "last_trial": receipt,
                        "last_exact_at_unix_s": time.time(),
                    },
                    write_status=lambda value: _write_json(_status_path(run_dir), value),
                )
                if target_terminal_status is not None:
                    target_achieved = True
                    break
                strategy_canary_terminal_status = _handle_strategy_canary_terminal(
                    campaign=campaign,
                    context=context,
                    status={
                        **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                        "state": (
                            "exact_sealed"
                            if observation.get("eligible") is True
                            else "physical_ineligible"
                        ),
                        "last_trial": receipt,
                        "last_exact_at_unix_s": time.time(),
                    },
                    write_status=lambda value: _write_json(_status_path(run_dir), value),
                )
                if strategy_canary_terminal_status is not None:
                    exit_code = 2
                    break
                bounded_bo_terminal_status = _handle_bounded_bo_budget(
                    campaign=campaign,
                    context=context,
                    run_dir=run_dir,
                    status={
                        **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                        "state": (
                            "exact_sealed"
                            if observation.get("eligible") is True
                            else "physical_ineligible"
                        ),
                        "last_trial": receipt,
                        "last_exact_at_unix_s": time.time(),
                    },
                    write_status=lambda value: _write_json(_status_path(run_dir), value),
                )
                if bounded_bo_terminal_status is not None:
                    break
                _write_json(
                    _status_path(run_dir),
                    {
                        **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                        "state": (
                            "exact_sealed"
                            if observation.get("eligible") is True
                            else "physical_ineligible"
                        ),
                        "last_trial": receipt,
                        "last_exact_at_unix_s": time.time(),
                    },
                )
            except BaseException as exc:  # fail closed for every physical fault
                last_error = f"trial {dispatch.ordinal}: {type(exc).__name__}: {exc}"
                exit_code = 1
                hard_guard: Mapping[str, Any] | None = None
                try:
                    hard_guard = campaign.tell_hard_guard(reason=last_error)
                except Exception as guard_exc:
                    last_error += f"; hard_guard: {type(guard_exc).__name__}: {guard_exc}"
                _write_json(
                    _status_path(run_dir),
                    {
                        **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                        "state": "hard_guard",
                        "dispatch": dispatch_value,
                        "proposal": proposal_value,
                        "hard_guard": _json_value(hard_guard),
                        "error": last_error,
                    },
                )
                try:
                    context.stop("trial_failure")
                except Exception:
                    pass
                break

            # The loop is intentionally serial: campaign.ask() cannot issue
            # another dispatch until the exact observation was appended.
            if args.poll_s:
                # Do not make the physical owner depend on an arbitrary shell
                # sleep.  This is only a pacing point between sealed trials;
                # the tmux/systemd owner remains responsible for lifetime.
                time.sleep(float(args.poll_s))
    except BaseException as exc:
        last_error = f"owner: {type(exc).__name__}: {exc}"
        exit_code = 1
        try:
            _write_json(
                _status_path(run_dir),
                {
                    **_base_status(run_dir=run_dir, campaign=campaign, active=True),
                    "state": "owner_error",
                    "error": last_error,
                },
            )
        except Exception:
            pass
    finally:
        if context is not None:
            if stop_requested:
                try:
                    context.stop(signal_name or "operator_stop")
                except Exception as exc:
                    last_error = last_error or f"stop: {type(exc).__name__}: {exc}"
            try:
                context.close()
            except Exception as exc:
                exit_code = 1
                last_error = last_error or f"close: {type(exc).__name__}: {exc}"
        for signum, previous in previous_signals.items():
            signal.signal(signum, previous)
        if campaign.bounded_bo_profile is not None:
            try:
                _write_json(
                    Path(run_dir) / "r013_bounded_bo_report.json",
                    _bounded_bo_report(campaign=campaign, run_dir=run_dir),
                )
            except Exception as report_error:
                last_error = last_error or f"bounded report: {type(report_error).__name__}: {report_error}"
        final_state = (
            "bounded_bo_budget_exhausted"
            if bounded_bo_terminal_status is not None
            else ("target_achieved" if target_achieved
            else (
                str(strategy_canary_terminal_status["state"])
                if strategy_canary_terminal_status is not None
                else ("operator_stopped" if stop_requested else ("stopped" if exit_code else "running"))
            ))
        )
        try:
            _write_json(
                _status_path(run_dir),
                {
                    **_base_status(run_dir=run_dir, campaign=campaign, active=False),
                    "state": final_state,
                    "stop_signal": signal_name,
                    "error": last_error,
                    "target_terminal_status": _json_value(target_terminal_status),
                    "bounded_bo_terminal_status": _json_value(bounded_bo_terminal_status),
                    "strategy_canary_terminal_status": _json_value(
                        strategy_canary_terminal_status
                    ),
                    "finished_at_unix_s": time.time(),
                },
            )
        except Exception:
            pass
        timing_stack.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
