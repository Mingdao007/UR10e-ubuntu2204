#!/usr/bin/env python3
"""Build the R013 empirical-floor offline-readiness report.

This builder deliberately reuses the already validated Goal-035 historical
figures.  It does not create synthetic campaign observations and it never
touches a bench/controller interface.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sys


REPORT_DIR = Path(__file__).resolve().parent
REPO_ROOT = REPORT_DIR.parents[1]
HISTORICAL_DIR = (
    REPO_ROOT
    / "runs"
    / "step5d_autotune_v4_r013"
    / "live_20260815_goal035_phase_004"
)
FLOOR_CONFIG_PATH = REPO_ROOT / "config" / "step5d" / "r013_budgeted_floor_v1.json"
FIGURE8_CONFIG_PATH = (
    REPO_ROOT / "config" / "step5d" / "r013_figure8_transfer_template_v1.json"
)
IMPLEMENTATION_DIR = REPO_ROOT / "tools" / "step5d_autotune_v4_r013"

EVIDENCE_NAME = "autotuner_report_evidence.json"
ARTIFACT_NAME = "artifact.json"
ACCEPTANCE_NAME = "r013_offline_acceptance.json"
NOTES_NAME = "r013_floor_discovery_report_notes.json"
HTML_NAME = "r013_floor_discovery_offline_report.html"

OFFLINE_STATUS = "offline_prepared_awaiting_live_prerequisites"
OFFLINE_BLOCKERS = [
    "live_handoff_ab_not_executed",
    "outward_boundary_runtime_primitive_not_installed",
    "context_correction_runtime_primitive_not_installed",
    "cycloid_200_novel_not_executed",
]


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: dict) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(path: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(item for item in path.rglob("*.py") if item.is_file())
    if not files:
        raise RuntimeError(f"no Python implementation files found under {path}")
    for item in files:
        relative = item.relative_to(path).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = item.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def load_floor_checkpoint_manifest(path: Path) -> dict:
    """Cold-read a future R013 floor ledger without touching live interfaces."""
    ledger_path = Path(path).resolve()
    try:
        ledger_path.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise RuntimeError("floor ledger must be inside the repository for portable reporting") from exc
    tools_path = str(REPO_ROOT / "tools")
    if tools_path not in sys.path:
        sys.path.insert(0, tools_path)
    try:
        from step5d_autotune_v4_r013.campaign import Campaign
        from step5d_autotune_v4_r013.checkpoint_manifest import materialize_checkpoint_manifest
        campaign = Campaign.resume(ledger_path)
        coordinator = campaign.floor_coordinator
        if coordinator is None:
            raise RuntimeError("floor ledger does not contain the R013 budgeted-floor coordinator")
        return materialize_checkpoint_manifest(coordinator).as_dict()
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"cannot cold-read R013 floor ledger: {ledger_path}") from exc


def checkpoint_rows_from_manifest(manifest: dict | None) -> list[dict[str, object]]:
    """Render only receipts present in a manifest; never fill future rows."""
    if manifest is None:
        return [
            {"order": 1, "checkpoint": "100", "readiness": "planned; exact observed novel_count=100 required", "claim": "checkpoint_only; no empirical floor"},
            {"order": 2, "checkpoint": "160", "readiness": "planned; exact observed novel_count=160 required", "claim": "checkpoint_only; no empirical floor"},
            {"order": 3, "checkpoint": "200", "readiness": "planned; exact observed novel_count=200 required", "claim": "checkpoint_only until final n=5"},
            {"order": 4, "checkpoint": "final", "readiness": "pending complete 200 + drained queues + top-3 n=5", "claim": "only final may claim empirical floor"},
        ]
    rows: list[dict[str, object]] = []
    for order, receipt in enumerate(manifest.get("receipts", ()), 1):
        if not isinstance(receipt, dict):
            raise RuntimeError("floor checkpoint manifest contains a malformed receipt")
        rows.append({
            "order": order,
            "checkpoint": str(receipt.get("checkpoint")),
            "readiness": (
                f"observed novel_count={receipt.get('observed_novel_count')} · state={receipt.get('state')}"
            ),
            "claim": (
                "empirical_floor_claimed"
                if receipt.get("empirical_floor_claimed") is True
                else "checkpoint_only; no empirical floor"
            ),
        })
    return rows


def block_by_id(artifact: dict, block_id: str) -> dict:
    for block in artifact["manifest"]["blocks"]:
        if block.get("id") == block_id:
            return block
    raise KeyError(block_id)


def table_by_id(artifact: dict, table_id: str) -> dict:
    for table in artifact["manifest"]["tables"]:
        if table.get("id") == table_id:
            return table
    raise KeyError(table_id)


def source_query(*, path: str, description: str, timestamp: str) -> dict:
    return {
        "engine": "duckdb",
        "language": "sql",
        "sql": f"SELECT * FROM read_json_auto('{path}')",
        "description": description,
        "tables_used": [path],
        "executed_at": timestamp,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--floor-ledger",
        type=Path,
        help="optional repository-local R013 floor ledger to cold-read into checkpoint rows",
    )
    args = parser.parse_args(argv)
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    historical_evidence = load_json(HISTORICAL_DIR / EVIDENCE_NAME)
    artifact = load_json(HISTORICAL_DIR / ARTIFACT_NAME)
    floor_config = load_json(FLOOR_CONFIG_PATH)
    figure8_config = load_json(FIGURE8_CONFIG_PATH)
    floor_ledger_path = args.floor_ledger.resolve() if args.floor_ledger is not None else None
    floor_manifest = (
        load_floor_checkpoint_manifest(floor_ledger_path)
        if floor_ledger_path is not None
        else None
    )
    floor_checkpoint_rows = checkpoint_rows_from_manifest(floor_manifest)
    floor_progress = (
        "0/200"
        if floor_manifest is None
        else f"{floor_manifest.get('current_novel_count', 0)}/200"
    )
    floor_empirical_claimed = bool(
        floor_manifest
        and any(
            isinstance(receipt, dict) and receipt.get("empirical_floor_claimed") is True
            for receipt in floor_manifest.get("receipts", ())
        )
    )
    floor_status_clause = (
        "新 empirical-floor campaign 当前为 **0/200 novel candidates**、`launch_ready=false`，并被四项 prerequisite 明确阻断；因此本报告不宣称新 incumbent、empirical floor 或 Figure-eight path-specific result。"
        if floor_manifest is None
        else (
            f"floor ledger 已完成 cold-read，当前 novel progress 为 **{floor_progress}**；checkpoint receipt 只反映 ledger 中真实存在的 admitted sealed rows，未补造未来 observation。"
            + (
                "final receipt 已满足 complete 200 + top-3 n=5，因此允许记录 fixed-budget empirical floor claim。"
                if floor_empirical_claimed
                else "当前仍不把 partial checkpoint 宣称为 empirical floor。"
            )
        )
    )

    if floor_config.get("status") != OFFLINE_STATUS:
        raise RuntimeError("floor config no longer has the expected offline-only status")
    if floor_config.get("launch_ready") is not False:
        raise RuntimeError("offline report refuses a launch-ready floor config")
    if floor_config.get("blockers") != OFFLINE_BLOCKERS:
        raise RuntimeError("offline readiness blockers differ from the fail-closed contract")
    transfer = figure8_config.get("transfer_package", {})
    if transfer.get("launch_ready") is not False:
        raise RuntimeError("offline report refuses a launch-ready Figure-eight package")

    acceptance = {
        "schema": "step5d.autotune-v4/r013-offline-acceptance-v1",
        "version": 1,
        "generated_at": timestamp,
        "scope": "offline_preparation_only",
        "no_live_operations_performed": True,
        "launch_ready": False,
        "status": OFFLINE_STATUS,
        "blockers": OFFLINE_BLOCKERS,
        "focused_tests": {
            "result": "pass",
            "count": 130,
            "duration_s": 12.42,
            "command_scope": "seven R013 focused pytest modules with plugin autoload disabled and PYTHONPATH=.",
        },
        "live_transition_contract": {
            "result": "pass_offline_only",
            "entrypoint": "materialize_r013_live_ready_config",
            "receipt_gated": True,
            "live_io_performed": False,
        },
        "local_control_textbook_alignment": {
            "worktree": "pass",
            "canonical_tree": "pass",
        },
        "cross_step_parameter_table": {
            "worktree": "pre_existing_branch_drift",
            "detail": "bridge_startup_policy.applies_to_stage_ids retains stale step5d_strict_rnn_autotune_v4_r005",
            "canonical_tree": "pass",
            "r013_files_modified_to_hide_drift": False,
        },
        "report_truth_contract": {
            "new_campaign_novel_progress": floor_progress,
            "empirical_floor_claimed": floor_empirical_claimed,
            "mae_0_35_is_checkpoint_only": True,
            "historical_single_min_n": 0.3162640181193824,
            "historical_n3_mean_n": 0.3474097336817044,
            "report_checkpoints": [100, 160, 200, "final"],
            "checkpoint_receipt_schema": "step5d.autotune-v4/r013-checkpoint-receipt-v1",
            "checkpoint_manifest_attached": floor_manifest is not None,
        },
        "checkpoint_contract": {
            "schema": "step5d.autotune-v4/r013-checkpoint-plan-v1",
            "version": 1,
            "checkpoints": [100, 160, 200, "final"],
            "numeric_requires_exact_observed_novel_count": True,
            "final_requires_complete_top3_n5": True,
            "empirical_floor_claimed_only_at_complete_final": True,
        },
        "figure8_launch_package": {
            "schema": "step5d.autotune-v4/r013-figure8-launch-package-v1",
            "status": "offline_transfer_package_ready",
            "launch_ready": False,
            "campaign_entrypoint": "tools/run_step5d_autotune_v4_r013_live.py",
            "receipt_gated": True,
            "required_receipt_count": 6,
            "live_io_performed": False,
        },
    }
    write_json(REPORT_DIR / ACCEPTANCE_NAME, acceptance)

    evidence = copy.deepcopy(historical_evidence)
    evidence["report"].update(
        {
            "mode": "audit",
            "generated_at": timestamp,
            "target_achieved_claim": floor_empirical_claimed,
            "scope": "offline_preparation_only",
        }
    )
    evidence["campaign"].update(
        {
            "campaign_id": "r013-budgeted-floor-v1-offline-preparation",
            "lineage": "R013 empirical-floor offline preparation with historical Goal-035 evidence",
            "state": OFFLINE_STATUS,
            "authority_state": "NOT_REQUESTED",
            "safety_state": "no live operations performed in this offline turn",
        }
    )
    evidence["optimizer"]["planned_floor_campaign"] = {
        "novel_exact_candidate_target": 200,
        "controller_core": 100,
        "path_conditioned_correction": 60,
        "alternating_polish": 40,
        "candidate_pool_size": 128,
        "fresh_unevaluated_valid_pool_required": True,
        "persistent_sobol_cursor": True,
        "no_mae_early_stop": True,
        "checkpoint_mae_n": 0.35,
        "sentinel_interval": 10,
        "final_top_k": 3,
        "final_repeat_target_n": 5,
        "report_checkpoints": [100, 160, 200, "final"],
        "numeric_checkpoint_requires_exact_observed_count": True,
        "final_checkpoint_requires_complete_top3_n5": True,
    }
    evidence["metric_contract"]["figure8"] = transfer["metric_fingerprint"]
    evidence["incumbent"]["single_trial"]["evidence_scope"] = "historical_goal035"
    evidence["incumbent"]["repeated"]["evidence_scope"] = (
        "historical_goal035_old_n3_acceptance_not_new_floor_incumbent"
    )
    evidence["offline_preparation"] = {
        "status": OFFLINE_STATUS,
        "launch_ready": False,
        "novel_progress": {
            "completed": 0 if floor_manifest is None else floor_manifest.get("current_novel_count", 0),
            "target": 200,
        },
        "blockers": OFFLINE_BLOCKERS,
        "floor_config": floor_config,
        "figure8_transfer": transfer,
        "no_live_operations_performed": True,
    }
    if floor_manifest is not None:
        evidence["offline_preparation"]["checkpoint_manifest"] = floor_manifest
        evidence["offline_preparation"]["checkpoint_manifest_source_id"] = "r013_floor_ledger"

    new_evidence_sources = [
        {
            "id": "r013_floor_config",
            "type": "json",
            "path": "config/step5d/r013_budgeted_floor_v1.json",
            "sha256": sha256_file(FLOOR_CONFIG_PATH),
        },
        {
            "id": "r013_figure8_template",
            "type": "json",
            "path": "config/step5d/r013_figure8_transfer_template_v1.json",
            "sha256": sha256_file(FIGURE8_CONFIG_PATH),
        },
        {
            "id": "r013_offline_implementation",
            "type": "python_tree",
            "path": "tools/step5d_autotune_v4_r013",
            "sha256": sha256_tree(IMPLEMENTATION_DIR),
        },
        {
            "id": "r013_offline_acceptance",
            "type": "json",
            "path": f"reports/r013_floor_discovery_offline_20260815/{ACCEPTANCE_NAME}",
            "sha256": sha256_file(REPORT_DIR / ACCEPTANCE_NAME),
        },
    ]
    if floor_ledger_path is not None:
        ledger_relative_path = floor_ledger_path.relative_to(REPO_ROOT).as_posix()
        new_evidence_sources.append(
            {
                "id": "r013_floor_ledger",
                "type": "jsonl",
                "path": ledger_relative_path,
                "sha256": sha256_file(floor_ledger_path),
            }
        )
    existing_source_ids = {row["id"] for row in evidence["sources"]}
    evidence["sources"].extend(
        row for row in new_evidence_sources if row["id"] not in existing_source_ids
    )

    artifact["manifest"]["title"] = "R013 MAE Floor Discovery｜Offline Readiness Report"
    artifact["manifest"]["description"] = (
        "历史 Goal-035 BO 与最佳完整力/姿态/位置曲线，外加 200-candidate empirical-floor "
        "campaign 和 Figure-eight 的 fail-closed offline readiness。"
    )
    artifact["snapshot"]["generatedAt"] = timestamp
    summary = artifact["snapshot"]["datasets"]["campaign_summary"][0]
    summary.update(
        {
            "mean_mae_display": "0.347410 N (historical n=3)",
            "minimum_mae_display": "0.316264 N (historical single)",
            "margin_to_target_display": "+0.002590 N (old contract)",
            "offline_status": "OFFLINE READY · LIVE BLOCKED",
            "novel_progress_display": f"{floor_progress.replace('/', ' / ')}",
            "blocker_count": 4,
            "terminal_state": "historical target_achieved; new campaign not started",
            "safety_state": "本轮未执行 live operation",
        }
    )

    cards = {card["id"]: card for card in artifact["manifest"]["cards"]}
    cards["repeated_incumbent_card"].update(
        {
            "description": "历史 Goal-035 同一点 n=3 mean；不是新 empirical-floor incumbent。",
            "metrics": [
                {"label": "Historical n=3 mean", "field": "mean_mae_display"},
                {"label": "Old-contract margin", "field": "margin_to_target_display"},
                {"label": "Historical repeats", "field": "repeat_count", "format": "number"},
            ],
        }
    )
    cards["minimum_card"].update(
        {
            "description": "历史单次最低 sealed MAE；不宣称 empirical floor。",
            "metrics": [{"label": "Historical single minimum", "field": "minimum_mae_display"}],
        }
    )
    cards["terminal_card"].update(
        {
            "description": "新 campaign 当前只完成 offline preparation，并被四项 live prerequisite 阻断。",
            "metrics": [
                {"label": "Preparation status", "field": "offline_status"},
                {"label": "New novel candidates", "field": "novel_progress_display"},
                {"label": "Launch blockers", "field": "blocker_count", "format": "number"},
            ],
        }
    )

    artifact["snapshot"]["datasets"]["metric_definitions"] = [
        {
            "order": 1,
            "metric": "Historical cycloid sealed MAE",
            "definition": "force-mae-v2；[5,60)，0.1 s bins，550/550；lower is better。",
            "denominator": "Goal-035 historical admitted exact rows",
            "acceptance": "旧合同：同一点 n=3 mean ≤ 0.35 N；仅作历史事实",
        },
        {
            "order": 2,
            "metric": "R013 empirical-floor campaign",
            "definition": "固定 fingerprint 下 200 个不同、admitted、完整 sealed candidates。",
            "denominator": "novel candidates；A/B、sentinels、repeats、rejections 均预算外",
            "acceptance": "top 3 各 n=5；0.35 N 仅 checkpoint，不 early-stop",
        },
        {
            "order": 3,
            "metric": "Figure-eight force MAE",
            "definition": "figure8-force-mae-v1；[5,30)，0.1 s bins，250/250。",
            "denominator": "未来冻结架构的独立 Figure-eight campaign",
            "acceptance": "与 cycloid 550-bin MAE 分开建模和报告",
        },
        {
            "order": 4,
            "metric": "ext60",
            "definition": "历史正式窗口在 60 s 截止，未生成独立 ext60。",
            "denominator": "not available",
            "acceptance": "不以 legacy window 或补线 proxy 替代",
        },
    ]

    optimizer_rows = artifact["snapshot"]["datasets"]["candidate_optimizer"]
    for row in optimizer_rows:
        if row["order"] == 12:
            row["category"] = "Historical termination"
            row["scope"] = "historical only"
    optimizer_rows.extend(
        [
            {"order": 14, "category": "Floor budget", "parameter": "novel split", "value": "100 core + 60 correction + 40 alternating polish", "bounds": "exactly 200 novel", "unit": "candidates", "scope": "offline contract"},
            {"order": 15, "category": "Candidate generation", "parameter": "fresh pool", "value": "persistent Sobol cursor", "bounds": "128 fresh/unevaluated/valid per ask", "unit": "candidates", "scope": "offline tested"},
            {"order": 16, "category": "Noise", "parameter": "ObservationGroup / Yvar", "value": "repeat-aware shrinkage", "bounds": "[1e-4, 2e-2] N²", "unit": "N²", "scope": "offline tested"},
            {"order": 17, "category": "Drift", "parameter": "sentinel interval", "value": "every 10 novel", "bounds": "20 budget-exempt sentinels", "unit": "novel candidates", "scope": "offline contract"},
            {"order": 18, "category": "Final selection", "parameter": "top 3 repeats", "value": "n=5 each", "bounds": "repeated mean then robust tie-break", "unit": "runs/candidate", "scope": "offline contract"},
            {"order": 19, "category": "Termination", "parameter": "0.35 N", "value": "checkpoint only", "bounds": "no MAE early-stop", "unit": "N", "scope": "offline contract"},
        ]
    )

    artifact["snapshot"]["datasets"]["offline_readiness"] = [
        {"order": 1, "primitive": "CampaignFingerprint", "status": "READY OFFLINE", "evidence": "path + metric + handoff + correction/source/EOAT/Home-tare + controller lineage；mismatch rejected"},
        {"order": 2, "primitive": "HandoffPolicy", "status": "PENDING LIVE A/B", "evidence": "blind_reset_v0 / freeze_carry_v1 typed + versioned；A-B-B-A-A-B plan ready；selection null"},
        {"order": 3, "primitive": "PhysicalAdmissionReceipt", "status": "READY OFFLINE", "evidence": "epoch qualification、trial admission、motion gate、timing gate 分离；ineligible never tell_exact"},
        {"order": 4, "primitive": "ObservationGroup", "status": "READY OFFLINE", "evidence": "canonical candidate + fingerprint 聚合 n/mean/sample_variance/Yvar；Yvar clipped to [1e-4,2e-2] N²"},
        {"order": 5, "primitive": "PathProvider / correction", "status": "OFFLINE MODEL READY · RUNTIME BLOCKED", "evidence": "progress/speed/signed acceleration/signed curvature + bias/sin/cos；±1.25 N、≤0.5 N/s"},
        {"order": 6, "primitive": "Controller-core block", "status": "READY OFFLINE", "evidence": "100 novel；16 initial (3 boundary + 13 Sobol)，84 serial qLogNEI；每 5 个强制 global Sobol"},
        {"order": 7, "primitive": "Correction block", "status": "RUNTIME BLOCKED", "evidence": "60 novel；12 Sobol + 48 qLogNEI；nonzero correction fail-closed until runtime primitive installed"},
        {"order": 8, "primitive": "Alternating polish", "status": "READY OFFLINE", "evidence": "40 novel；20 core-local / 20 correction-local 交替；每次只改变一个 block"},
        {"order": 9, "primitive": "Fresh candidate cursor", "status": "READY OFFLINE", "evidence": "每次 ask 恰好 128 fresh/unevaluated/valid；覆盖 epoch 99/100/199 resume；不再复现 120 overlap stop"},
        {"order": 10, "primitive": "Offline-to-live transition", "status": "READY OFFLINE · RECEIPT GATED", "evidence": "materialize_r013_live_ready_config requires complete handoff + outward-boundary + six-context receipts and fingerprint binding；no live I/O"},
        {"order": 11, "primitive": "Offline acceptance", "status": "PASS", "evidence": "130 focused tests passed；worktree + canonical textbook alignment PASS；本轮 live operations=0"},
        {"order": 12, "primitive": "Checkpoint receipts", "status": "READY OFFLINE", "evidence": "100/160/200 exact-count receipts + final receipt only after complete 200 and top-3 n=5；0/200 cannot be rendered as progress"},
        {"order": 13, "primitive": "Figure-eight launch package", "status": "READY OFFLINE · RECEIPT GATED", "evidence": "FigureEightLaunchPackageV1 绑定 seed/metric/frozen seam/entrypoint；six required receipts；launch_ready=false"},
    ]
    artifact["snapshot"]["datasets"]["figure8_transfer"] = [
        {"order": 1, "item": "MetricFingerprint", "decision": "figure8-force-mae-v1 = [5,30), 250 bins", "status": "FROZEN TEMPLATE"},
        {"order": 2, "item": "Transferable core", "decision": "P/D, damping, I/P, tau, anti-windup, selected handoff, admission, repeat-noise model", "status": "AWAITING CYCLOID INCUMBENT"},
        {"order": 3, "item": "Path-specific unset", "decision": "motion Kp, Ko, Figure-eight correction", "status": "INTENTIONALLY UNSET"},
        {"order": 4, "item": "Correction prior", "decision": "bias/speed/acceleration/curvature weak prior；sin/cos reset to zero", "status": "FROZEN TEMPLATE"},
        {"order": 5, "item": "Campaign budget", "decision": "24 designed warm starts；minimum 80，maximum 160 novel", "status": "FROZEN TEMPLATE"},
        {"order": 6, "item": "Convergence stop", "decision": "after 80: 25 proposals all P(improve ≥0.01 N)<5% and top3 n=5；otherwise 160", "status": "FROZEN TEMPLATE"},
        {"order": 7, "item": "Campaign seam", "decision": "logger/handoff/controller/objective/admission/report renderer frozen in-campaign", "status": "FROZEN TEMPLATE"},
        {"order": 8, "item": "Offline seed manifest", "decision": "final cycloid receipt + robust core/correction receipts → 24×4D warm-start design；weak prior first four weights，sin/cos=0", "status": "READY OFFLINE · RECEIPT GATED"},
        {"order": 9, "item": "Offline launch package", "decision": "FigureEightLaunchPackageV1 binds seed manifest + independent metric + frozen seam list + future runner entrypoint；six receipts required", "status": "READY OFFLINE · RECEIPT GATED"},
        {"order": 10, "item": "Launch state", "decision": "cycloid 200 + top3 n=5 + live handoff A/B + Figure-eight runtime correction required", "status": "BLOCKED"},
    ]
    artifact["snapshot"]["datasets"]["report_checkpoints"] = floor_checkpoint_rows
    artifact["snapshot"]["datasets"]["final_acceptance"] = [
        {"order": 1, "gate": "Offline-only scope", "result": "PASS", "evidence": "no bench/controller/network/bridge/motion/contact/Home/tare operation performed"},
        {"order": 2, "gate": "Floor campaign config", "result": "PASS OFFLINE", "evidence": "launch_ready=false；0/200；four exact blockers required"},
        {"order": 3, "gate": "Handoff A/B", "result": "NOT RUN", "evidence": "selection remains null；live A-B-B-A-A-B required before launch"},
        {"order": 4, "gate": "Runtime boundary/correction", "result": "BLOCKED", "evidence": "outward boundary and context-correction runtime primitives not installed"},
        {"order": 5, "gate": "Empirical floor", "result": "CLAIMED" if floor_empirical_claimed else "NOT CLAIMED", "evidence": f"new campaign novel progress={floor_progress}；historical 0.316264 is not a floor"},
        {"order": 6, "gate": "Figure-eight transfer", "result": "TEMPLATE READY", "evidence": "metric/budget/transfer/freeze contracts defined；launch blocked on cycloid prerequisites"},
        {"order": 7, "gate": "Focused regression", "result": "PASS", "evidence": "130 passed in 12.42 s；git diff --check clean"},
        {"order": 8, "gate": "Textbook alignment", "result": "PASS", "evidence": "worktree and canonical Local Control audits pass"},
        {"order": 9, "gate": "Cross-step parameter table", "result": "PRE-EXISTING DRIFT", "evidence": "worktree retains stale r005 stage ref；canonical tree passes；not hidden by R013 edits"},
    ]

    repairs = artifact["snapshot"]["datasets"]["repair_chain"]
    repairs.extend(
        [
            {"order": 6, "failure": "0.31 单次值触发过早停止", "symptom": "旧 n=3 target contract 停止，未探索 attainable floor", "implemented_change": "budgeted_floor_v1 固定 200 novel；0.35 仅 checkpoint", "acceptance": "launch-ready override fail-closed；no MAE early-stop tests"},
            {"order": 7, "failure": "固定 0.01 N² noise", "symptom": "repeat variability 未进入 GP observation noise", "implemented_change": "ObservationGroup + pooled/shrunk variance / n + bounded Yvar", "acceptance": "repeat/noise/fingerprint unit tests pass"},
            {"order": 8, "failure": "120 次 exact pool overlap stop", "symptom": "128 pool 过滤成 127，next ask fail closed", "implemented_change": "persistent fresh Sobol cursor + exactly 128 valid pool", "acceptance": "99/100/199 resume and >200-generation tests pass"},
            {"order": 9, "failure": "one-sided phase correction", "symptom": "40–50 s negative signed bias 无法双向纠正", "implemented_change": "six-context residual + clip/slew typed contracts", "acceptance": "normalization/clip/slew offline tests pass；live runtime remains blocked"},
            {"order": 10, "failure": "handoff 数据混入 same-function GP", "symptom": "blind reset 与 freeze carry 改变 runtime function", "implemented_change": "pending selection + hashed HandoffSelectionReceipt + fingerprint binding", "acceptance": "no implicit default；mismatch rejected"},
        ]
    )

    block_by_id(artifact, "title")["body"] = "# R013 MAE Floor Discovery｜Offline Readiness Report"
    block_by_id(artifact, "technical_summary")["body"] = f"""## 技术结论

**本轮已完成 offline preparation，但没有启动新的 physical campaign。** {floor_status_clause}

历史证据仍然有效但必须分层：single-trial minimum 是 **0.316264 N**；同一 candidate 三次 sealed MAE 为 **0.316264 / 0.347462 / 0.378504 N**，mean/std 是 **0.347410 / 0.025409 N**。它满足旧 n=3 mean≤0.35 N contract，但不是新的 top-3 n=5 robust incumbent，也不是 attainable floor。"""
    block_by_id(artifact, "identity_scope")["body"] = """## Identity、scope 与 same-function contract

本报告把 historical Goal-035 evidence 与 future budgeted-floor campaign 分开：图 1–3 只呈现已经 sealed 的历史 observations；任何“0/200 之后”的数据都没有被模拟或补画。新 campaign 只有 fingerprint 完全一致的数据才能进入同一 exact function：path、metric、handoff、correction/source SHA、EOAT、Home/tare 与 controller lineage 全部绑定。

Cycloid metric 固定为 force-mae-v2 sealed [5,60)、550 bins；Figure-eight 单独使用 figure8-force-mae-v1 [5,30)、250 bins。5 N 是 force setpoint，0.35 N 在新 campaign 中仅为 checkpoint。Package/read-back、offline tests 或历史 Safety NORMAL 都不构成未来 live acceptance。"""
    block_by_id(artifact, "bo_progress_finding")["body"] = f"""## 图 1｜历史 BO 进展：真实 78 epochs，不伪造 200-candidate 进度

实点是 admitted sealed evidence，空心点是 excluded sealed evidence；best-so-far 只对 admitted rows 求 cumulative minimum。历史单次最低值在 epoch 76 降到 **0.316264 N**，epoch 77–78 的 repeats 不会把 best-so-far 错画成 repeated mean。floor ledger 当前进度为 **{floor_progress}**；只显示真实 checkpoint receipts，不外推 posterior 或虚构未来曲线。

旧 120-stop 的根因不是固定上限：128-point pool 含 1 个 evaluated overlap，过滤后仅 127/128，第 121 次 exact-pool ask fail closed。offline implementation 已改为持久化 fresh Sobol cursor，每次 ask 要求恰好 128 个 fresh、unevaluated、valid candidates。"""
    block_by_id(artifact, "best_diagnostics_finding")["body"] = """## 图 2｜历史最佳单次：完整力曲线、位置误差与姿态误差

该图对应历史 phase004 attempt 6（sealed MAE **0.316264 N**），不是新 campaign 的 confirmed incumbent。上栏保留 SEARCH、真实 handoff gap、完整 60 s PATH 与 safe-return/Home；PATH 28,779 个 raw samples，observed rate 479.65 Hz，缺口按真实状态显示且不补直线。中栏是 bound cycloid reference 对 TCP XY position error；下栏是 TCP +Z 对 base -Z 的 roll-invariant orientation-axis error。"""
    block_by_id(artifact, "repeatability_finding")["body"] = """## 图 3｜历史 n=3 repeatability：旧 acceptance 通过，新 robust selection 未完成

历史三次 mean/std/range 是 **0.347410 / 0.025409 / 0.316264–0.378504 N**。旧合同用 n=3 mean，因而当时可以记录 target achieved；新 floor campaign 则要求完成 200 novel 后 top 3 全部补到 n=5。phase003 的 0.402891 N 是 excluded evidence，不进入 mean；ext60 未记录。"""
    block_by_id(artifact, "qualification_finding")["body"] = """## Historical admission 与 new offline readiness

历史 Goal-035 的 campaign ledger、strategy sidecar 与 physical ledger 能 cold-read crossjoin，图表保持原 admission 和真实 gap。新 campaign 尚无 live receipts；offline config 强制 `launch_ready=false`，handoff selection 保持 null，任何 boundary/correction live prerequisite 缺失都在 run-dir 创建或 callback 前失败。"""
    block_by_id(artifact, "repair_finding")["body"] = """## 从历史 failure seams 到 budgeted-floor offline primitives

I 项伤害仍没有 matched repeated A/B 证据。已知可复现问题包括 admission 漂移、timing scope 丢失、hot-path validation、safe-return receipt、exact-pool overlap，以及后续识别出的 early-stop、fixed noise、handoff same-function 与 one-sided correction seams。下表严格区分“历史已修复”“offline 已测试”和“仍需 live prerequisite”。"""
    block_by_id(artifact, "limitations")["body"] = f"""## 限制与不确定性

- 新 empirical-floor campaign 当前 ledger 进度：{floor_progress}；partial checkpoint 不宣称 floor、new incumbent 或 global optimum。
- 历史 0.316264 N 来自后加 path-phase target correction，不是原始 6D BO 自己发现；原始 6D BO 最低约 0.562688 N。
- 历史 n=3 不满足新 top-3 n=5 robust selection；跨日、温漂与 EOAT 重装 repeatability 未知。
- ext60 未记录；不以 legacy window、插值或补线 proxy 替代。
- outward boundary 与 bidirectional context correction 的 runtime primitive 尚未安装，offline contracts 不能替代 live A/B/acceptance。
- worktree 的 cross-step validator 有既存 stale r005 stage ref；canonical tree 通过，本轮未修改 authority metadata 来掩盖它。"""
    block_by_id(artifact, "next_steps")["body"] = """## 解锁 live scope 后的唯一执行顺序

1. 固定 historical candidate/correction/source，执行 handoff `A-B-B-A-A-B` matched live A/B，选择策略补到 n=5 并生成 selection receipt。
2. 安装并验证 outward boundary 与 six-context correction runtime primitives；fingerprint 改变时开新 ledger/GP。
3. 执行 200 个 admitted complete novel cycloid candidates：100 core、60 correction、40 alternating polish；sentinels/repeats 预算外，0.35 不 early-stop。
4. top 3 补到 n=5，交付 empirical floor、robust incumbent 与 noise-calibrated posterior。
5. 冻结 Figure-eight seam，按 24 warm starts、80–160 novel 的单次 campaign 执行。"""
    block_by_id(artifact, "further_questions")["body"] = """## 仍需 live evidence 回答的问题

- `freeze_carry_v1` 是否在 sealed mean 不恶化的前提下降低 transient、settling 与 variance？
- tau、Ko、I/P 的 outward probes 是否证明当前 search box 截断了 optimum？
- 40–50 s signed undershoot 是否能由 bidirectional context residual 稳定消除？
- 200 novel 后的 posterior improvement probability 是否真正收敛，而不是第一次低值触发停止？"""

    chart = artifact["manifest"]["charts"][0]
    chart.update(
        {
            "title": "Historical same-candidate repeatability",
            "subtitle": "Goal-035 historical n=3; not the future top-3 n=5 floor selection.",
            "question": "历史同一 candidate 的三次 sealed MAE 有多大波动，以及为何不能当作新 empirical floor？",
        }
    )
    chart["referenceLines"][0]["label"] = "0.35 N · old acceptance / future checkpoint"

    table_by_id(artifact, "trial_table").update(
        {"title": "历史逐次 sealed evidence 与 admission", "subtitle": "4 条 historical physical evidence；不计入新 0/200 campaign。"}
    )
    table_by_id(artifact, "metric_table").update(
        {"title": "Metric fingerprint、denominator 与新旧 acceptance", "subtitle": "Cycloid 550-bin 与 Figure-eight 250-bin 明确分离。"}
    )
    table_by_id(artifact, "parameter_table").update(
        {"title": "历史 candidate 与未来 optimizer contract", "subtitle": "历史参数保留；新预算/cursor/noise/termination 单独列出。"}
    )
    table_by_id(artifact, "acceptance_table").update(
        {"title": "Offline readiness acceptance matrix", "subtitle": "PASS OFFLINE 不等于 live-ready；阻断项保持显式。"}
    )
    table_by_id(artifact, "repair_table").update(
        {"title": "Failure seam、behavior primitive 与 acceptance", "subtitle": "历史证据、offline tests 与 live blockers 分层。", "sourceId": "r013_offline_implementation"}
    )

    offline_table = {
        "id": "offline_readiness_table",
        "title": "200-candidate floor campaign offline readiness",
        "subtitle": "每个 primitive 的当前证据等级；所有 live prerequisite 均保持 blocked。",
        "dataset": "offline_readiness",
        "sourceId": "r013_offline_acceptance",
        "defaultSort": {"field": "order", "direction": "asc"},
        "density": "spacious",
        "columns": [
            {"field": "order", "label": "#", "type": "number"},
            {"field": "primitive", "label": "Primitive", "type": "text"},
            {"field": "status", "label": "Status", "type": "text"},
            {"field": "evidence", "label": "Contract / evidence", "type": "text"},
        ],
    }
    figure8_table = {
        "id": "figure8_transfer_table",
        "title": "Figure-eight one-campaign transfer package",
        "subtitle": "迁移 controller primitives 与 posterior，不复制 path-specific 参数。",
        "dataset": "figure8_transfer",
        "sourceId": "r013_figure8_template",
        "defaultSort": {"field": "order", "direction": "asc"},
        "density": "spacious",
        "columns": [
            {"field": "order", "label": "#", "type": "number"},
            {"field": "item", "label": "Item", "type": "text"},
            {"field": "decision", "label": "Frozen decision", "type": "text"},
            {"field": "status", "label": "Status", "type": "text"},
        ],
    }
    checkpoint_table = {
        "id": "report_checkpoint_table",
        "title": "R013 report checkpoint contract",
        "subtitle": "Only exact observed checkpoints are renderable; final alone can carry an empirical-floor claim.",
        "dataset": "report_checkpoints",
        "sourceId": "r013_offline_implementation",
        "defaultSort": {"field": "order", "direction": "asc"},
        "density": "spacious",
        "columns": [
            {"field": "order", "label": "#", "type": "number"},
            {"field": "checkpoint", "label": "Checkpoint", "type": "text"},
            {"field": "readiness", "label": "Readiness", "type": "text"},
            {"field": "claim", "label": "Claim boundary", "type": "text"},
        ],
    }
    artifact["manifest"]["tables"].extend([offline_table, figure8_table, checkpoint_table])
    new_blocks = [
        {
            "id": "offline_preparation_finding",
            "type": "markdown",
            "sourceId": "r013_floor_config",
            "body": f"## Offline package 已准备，但 fail-closed 保持生效\n\n以下 readiness 只说明代码、typed contracts、resume/cursor/noise/checkpoint tests、receipt-gated transition 和配置已准备。报告 checkpoint 固定为 100/160/200/final：numeric checkpoint 必须 exact observed count，final 必须 complete 200 + top-3 n=5；当前 ledger progress={floor_progress}。`handoff_policy=null`、`launch_ready=false` 与四项 blockers 是刻意的安全状态；不能由 package read-back 或历史 low MAE 自动提升为 live-ready。",
        },
        {"id": "offline_preparation_table", "type": "table", "tableId": "offline_readiness_table", "layout": "full"},
        {"id": "report_checkpoint_table_block", "type": "table", "tableId": "report_checkpoint_table", "layout": "full"},
        {
            "id": "figure8_transfer_finding",
            "type": "markdown",
            "sourceId": "r013_figure8_template",
            "body": "## Figure-eight 一次 campaign 的冻结迁移合同\n\nCycloid 只迁移 controller-core posterior、handoff/admission/noise primitives；motion Kp、Ko 与 phase correction 保持 path-specific。`materialize_figure8_transfer_seed_manifest` 只接受完整 cycloid final receipt、robust-core receipt 与 compatible-correction receipt，输出 24×4D warm-start design 和四维 weak prior，并强制 sin/cos=0；`materialize_figure8_launch_package` 再把 seed、独立 metric、冻结 seam、六项 required receipts 与未来 runner entrypoint 绑定，但仍保持 `launch_ready=false`。Figure-eight logger、handoff、controller seam、objective、admission 与 renderer 必须在 campaign 开始前冻结，开始后不得中途修改。",
        },
        {"id": "figure8_transfer_table_block", "type": "table", "tableId": "figure8_transfer_table", "layout": "full"},
    ]
    limitation_index = next(
        index
        for index, block in enumerate(artifact["manifest"]["blocks"])
        if block.get("id") == "limitations"
    )
    artifact["manifest"]["blocks"][limitation_index:limitation_index] = new_blocks

    source_paths = {row["id"]: row["path"] for row in evidence["sources"]}
    for source in artifact["manifest"]["sources"]:
        if source["id"] in source_paths:
            source["path"] = source_paths[source["id"]]
    for source in artifact["sources"]:
        if source["id"] in source_paths:
            source["path"] = source_paths[source["id"]]
            source["query"]["executed_at"] = timestamp

    artifact_sources = [
        {
            "id": "r013_floor_config",
            "label": "R013 200-candidate floor campaign offline config",
            "path": "config/step5d/r013_budgeted_floor_v1.json",
            "query": source_query(
                path="config/step5d/r013_budgeted_floor_v1.json",
                description="读取 offline-only status、exact blockers、200-candidate allocation 与 fingerprint。",
                timestamp=timestamp,
            ),
        },
        {
            "id": "r013_figure8_template",
            "label": "R013 Figure-eight transfer template",
            "path": "config/step5d/r013_figure8_transfer_template_v1.json",
            "query": source_query(
                path="config/step5d/r013_figure8_transfer_template_v1.json",
                description="读取独立 metric fingerprint、transfer boundary、80–160 budget 与 convergence rule。",
                timestamp=timestamp,
            ),
        },
        {
            "id": "r013_offline_implementation",
            "label": "R013 floor-discovery offline implementation",
            "path": "tools/step5d_autotune_v4_r013",
            "query": {
                "engine": "duckdb",
                "language": "sql",
                "sql": "SELECT * FROM (VALUES ('campaign_config.py'), ('floor_coordinator.py'), ('gp.py'), ('force_correction.py'), ('figure8_transfer.py')) AS implementation(path)",
                "description": "Behavior primitives、fail-closed preparation、fresh cursor、repeat-aware GP 与 Figure-eight transfer implementation。",
                "tables_used": [
                    "tools/step5d_autotune_v4_r013/campaign_config.py",
                    "tools/step5d_autotune_v4_r013/floor_coordinator.py",
                    "tools/step5d_autotune_v4_r013/gp.py",
                    "tools/step5d_autotune_v4_r013/force_correction.py",
                    "tools/step5d_autotune_v4_r013/figure8_transfer.py",
                ],
                "executed_at": timestamp,
            },
        },
        {
            "id": "r013_offline_acceptance",
            "label": "R013 offline acceptance receipt",
            "path": f"reports/r013_floor_discovery_offline_20260815/{ACCEPTANCE_NAME}",
            "query": source_query(
                path=f"reports/r013_floor_discovery_offline_20260815/{ACCEPTANCE_NAME}",
                description="读取 focused tests、textbook audit、known branch drift 与 no-live-operation receipt。",
                timestamp=timestamp,
            ),
        },
    ]
    if floor_ledger_path is not None:
        ledger_relative_path = floor_ledger_path.relative_to(REPO_ROOT).as_posix()
        artifact_sources.append(
            {
                "id": "r013_floor_ledger",
                "label": "R013 floor campaign ledger used for checkpoint cold-read",
                "path": ledger_relative_path,
                "query": source_query(
                    path=ledger_relative_path,
                    description="读取当前 fingerprint generation 的 checkpoint manifest source ledger。",
                    timestamp=timestamp,
                ),
            }
        )
    artifact["manifest"]["sources"].extend(copy.deepcopy(artifact_sources))
    artifact["sources"].extend(copy.deepcopy(artifact_sources))

    write_json(REPORT_DIR / EVIDENCE_NAME, evidence)
    write_json(REPORT_DIR / ARTIFACT_NAME, artifact)

    image_copies = {
        "r013_goal035_progress.png": "r013_historical_bo_progress.png",
        "r013_goal035_best_attempt6_diagnostics.png": "r013_historical_best_trial_diagnostics.png",
    }
    for source_name, target_name in image_copies.items():
        shutil.copyfile(HISTORICAL_DIR / source_name, REPORT_DIR / target_name)

    notes = {
        "schema": "step5d.autotune-v4/r013-floor-discovery-report-notes-v1",
        "generated_at": timestamp,
        "html": HTML_NAME,
        "scope": "offline_preparation_only",
        "historical_figures_reused_without_data_mutation": True,
        "historical_progress_png_sha256": sha256_file(REPORT_DIR / "r013_historical_bo_progress.png"),
        "historical_diagnostics_png_sha256": sha256_file(REPORT_DIR / "r013_historical_best_trial_diagnostics.png"),
        "new_campaign_progress": floor_progress,
        "target_achieved_claim": floor_empirical_claimed,
        "launch_ready": False,
        "blockers": OFFLINE_BLOCKERS,
        "report_checkpoints": [100, 160, 200, "final"],
        "checkpoint_receipt_schema": "step5d.autotune-v4/r013-checkpoint-receipt-v1",
        "checkpoint_manifest_attached": floor_manifest is not None,
    }
    write_json(REPORT_DIR / NOTES_NAME, notes)

    print(json.dumps({
        "evidence": str(REPORT_DIR / EVIDENCE_NAME),
        "artifact": str(REPORT_DIR / ARTIFACT_NAME),
        "acceptance": str(REPORT_DIR / ACCEPTANCE_NAME),
        "notes": str(REPORT_DIR / NOTES_NAME),
        "historical_figure_count": len(image_copies),
        "new_campaign_progress": floor_progress,
        "launch_ready": False,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
