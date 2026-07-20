from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from .canonical import canonical_sha256, file_sha256
from .features import FEATURE_KEYS, features_from_bundle
from .schema import validate_manifest, validate_record


V2_DB_SHA256 = "a7632090b62a62c8e7b557de9710f179d5e1624d7e879cb700a7c97b26ce404b"
V3_FREEZE_SHA256 = "c65ec7bb78ccd3b050e1db65ddfee0ea8dec73a981f4793533a5a7cb793a47bb"
V3_OVERLAY_SHA256 = "310b9e05c858cac09ceb42567953e6694c0d0b33ae9510a6d13f94a7d280f255"


class SourceIntegrityError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SourceIntegrityError(f"JSON source is not an object: {path}")
    return value


def _require_digest(path: Path, expected: str) -> None:
    if not path.is_file():
        raise SourceIntegrityError(f"missing immutable source: {path}")
    actual = file_sha256(path)
    if actual != expected:
        raise SourceIntegrityError(f"digest mismatch for {path}: {actual} != {expected}")


def _sequence(bundle: Mapping[str, Any] | None, verify_sources: bool) -> dict[str, Any] | None:
    if not bundle:
        return None
    csv_info = bundle.get("artifact_provenance", {}).get("csv")
    if not isinstance(csv_info, Mapping):
        return None
    path = Path(str(csv_info.get("path", "")))
    expected = str(csv_info.get("sha256", ""))
    if verify_sources:
        _require_digest(path, expected)
    return {
        "path_role": "immutable_source",
        "sha256": expected,
        "size_bytes": int(csv_info.get("size_bytes", path.stat().st_size)),
    }


def _outcome(disposition: str, failures: list[str], eligible: bool) -> str:
    if disposition == "objective" and eligible:
        return "valid_parameter_observation"
    if disposition == "parameter_event":
        return "parameter_constraint_violation"
    if disposition in {"wait_infra_ready", "infra_stop", "uncertain_attempt"}:
        return "infrastructure_failure"
    if disposition in {"operator_stop", "manual_stop"}:
        return "operator_stop"
    if any(any(token in failure for token in ("cadence", "feedback", "rnn")) for failure in failures):
        return "observer_gap"
    if failures == ["orientation_profile_unqualified"] or "orientation_profile_unqualified" in failures:
        return "model_mismatch"
    return "unknown"


def _finalize_record(record: dict[str, Any]) -> dict[str, Any]:
    identity = {
        "source_kind": record["source_kind"],
        "source_artifact_sha256": record["source_artifact_sha256"],
        "campaign_uid": record["campaign_uid"],
        "trial_uid": record["trial_uid"],
        "parameter_semantics_fingerprint": record["parameter_semantics_fingerprint"],
    }
    record["record_uid"] = canonical_sha256(identity)
    validate_record(record)
    return record


def extract_v2_records(db_path: Path, verify_sources: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if verify_sources:
        _require_digest(db_path, V2_DB_SHA256)
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT t.trial_id, t.state, t.snapshot_json, t.arm_sequence,
                   c.candidate_id, c.group_id, c.p_text, c.i_text, c.d_text,
                   c.profile_id, c.status, d.code_fingerprint, d.tp_fingerprint,
                   d.guard_fingerprint, d.profile_json, a.metrics_json,
                   a.objective_mae_n, a.objective_eligible
            FROM trials t JOIN candidates c USING(candidate_id)
            JOIN deployments d USING(deployment_id)
            LEFT JOIN analyses a USING(trial_id)
            ORDER BY t.arm_sequence
            """
        ).fetchall()
        artifacts = {
            row["trial_id"]: dict(row)
            for row in connection.execute(
                "SELECT trial_id, role, path, sha256, immutable FROM artifacts ORDER BY role"
            ).fetchall()
        }
    finally:
        connection.close()

    records: list[dict[str, Any]] = []
    verified_outer_artifacts = 0
    for row in rows:
        artifact = artifacts.get(row["trial_id"])
        bundle: dict[str, Any] | None = None
        source_sha = V2_DB_SHA256
        if artifact:
            artifact_path = Path(artifact["path"])
            if verify_sources:
                _require_digest(artifact_path, artifact["sha256"])
            verified_outer_artifacts += 1
            source_sha = artifact["sha256"]
            if artifact["role"] == "raw_capture_seal":
                bundle = _load_json(artifact_path)

        metrics = json.loads(row["metrics_json"]) if row["metrics_json"] else {}
        evaluation = bundle.get("evaluation", {}) if bundle else {}
        failures = sorted(set(evaluation.get("structural_failures", [])))
        objective_eligible = bool(row["objective_eligible"])
        disposition = str(evaluation.get("disposition", row["state"]))
        outcome = _outcome(disposition, failures, objective_eligible)
        objective = row["objective_mae_n"] if objective_eligible else None
        features = features_from_bundle(bundle) if bundle else {key: None for key in FEATURE_KEYS}
        if metrics.get("force_mae_n") is not None:
            features["force_mae_n"] = metrics["force_mae_n"]
        missing = ["orientation_ko_not_hash_bound_in_v2"]
        if artifact is None:
            missing.append("outer_trial_artifact_missing")
        missing.extend(f"feature_missing:{key}" for key, value in features.items() if value is None)
        record = {
            "schema": "step5d.offline-trial-rl/dataset-record-v1",
            "record_uid": "0" * 64,
            "source_kind": "v2_control_plane",
            "source_artifact_sha256": source_sha,
            "campaign_uid": "step5d-autotune-v2",
            "trial_uid": row["trial_id"],
            "legacy_trial_number": int(row["arm_sequence"]),
            "source_git_sha": None,
            "plant_epoch": f"v2:{row['code_fingerprint']}:{row['guard_fingerprint']}",
            "controller_identity": {
                "code_fingerprint": row["code_fingerprint"],
                "profile_id": row["profile_id"],
                "profile": json.loads(row["profile_json"]),
            },
            "tp_identity": {"fingerprint": row["tp_fingerprint"]},
            "trajectory_identity": {},
            "objective_identity": {"metric": "objective_mae_n", "eligible_in_source": objective_eligible},
            "parameter_semantics_fingerprint": f"v2-3d-no-orientation:{row['guard_fingerprint']}",
            "action": {
                "force_p_gain": float(row["p_text"]), "force_i_gain": float(row["i_text"]),
                "force_damping": float(row["d_text"]), "orientation_ko": None,
            },
            "action_complete": False,
            "sequence_artifact": _sequence(bundle, verify_sources),
            "outcome_class": outcome,
            "metric_role": "trainable_objective" if objective_eligible else ("diagnostic_only" if row["metrics_json"] else "unavailable"),
            "objective": objective,
            "reward": -float(objective) if objective is not None else None,
            "reward_eligible": objective_eligible and outcome == "valid_parameter_observation",
            "constraint_eligible": outcome in {"valid_parameter_observation", "parameter_constraint_violation"},
            "constraint_unsafe": outcome == "parameter_constraint_violation" if outcome in {"valid_parameter_observation", "parameter_constraint_violation"} else None,
            "structural_failures": failures,
            "features": features,
            "missing_reasons": sorted(set(missing)),
            "split_group": f"v2:{row['code_fingerprint']}:{row['guard_fingerprint']}",
        }
        records.append(_finalize_record(record))
    return records, {
        "kind": "v2_control_plane", "sha256": V2_DB_SHA256,
        "physical_trials": len(rows), "verified_outer_artifacts": verified_outer_artifacts,
        "missing_outer_artifacts": len(rows) - len(artifacts),
    }


def _overlay_index(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for batch in payload.get("batches", []):
        for row in batch.get("trials", []):
            result[str(row["transport_candidate_uid"])] = dict(row["overlay"])
    return result


def extract_v3_records(freeze_path: Path, verify_sources: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if verify_sources:
        _require_digest(freeze_path, V3_FREEZE_SHA256)
    freeze = _load_json(freeze_path)
    campaign_root = Path(freeze["source_campaign_root"])
    overlay_path = campaign_root / "control/v3_trial_overlays.json"
    if verify_sources:
        _require_digest(overlay_path, V3_OVERLAY_SHA256)
        for relative, expected in freeze["source_campaign_files"].items():
            candidates = {
                "campaign_identity.json": campaign_root / "store/campaign_identity.json",
                "candidate_plan.json": campaign_root / "control/candidate_plan.json",
                "history.jsonl": campaign_root / "store/history.jsonl",
                "v3_trial_overlays.json": overlay_path,
            }
            _require_digest(candidates[relative], expected)
    overlays = _overlay_index(_load_json(overlay_path))
    records: list[dict[str, Any]] = []
    for row in freeze["bundles"]:
        bundle_path = campaign_root / "store/trials" / row["trial_uid"] / "immutable_trial_bundle.json"
        if verify_sources:
            _require_digest(bundle_path, row["bundle_sha256"])
        bundle = _load_json(bundle_path)
        overlay = overlays.get(row["candidate_uid"])
        if overlay is None:
            raise SourceIntegrityError(f"missing V3 overlay for {row['candidate_uid']}")
        failures = sorted(set(row["structural_failures"]))
        outcome = _outcome(row["source_disposition"], failures, bool(row["optimizer_eligible"]))
        features = features_from_bundle(bundle)
        missing = [f"feature_missing:{key}" for key, value in features.items() if value is None]
        trial = bundle.get("trial", {})
        campaign = trial.get("campaign", {})
        capture = bundle.get("capture", {})
        record = {
            "schema": "step5d.offline-trial-rl/dataset-record-v1",
            "record_uid": "0" * 64,
            "source_kind": "v3_diagnostic_bundle",
            "source_artifact_sha256": row["bundle_sha256"],
            "campaign_uid": str(campaign.get("campaign_fingerprint", campaign.get("campaign_id", "v3-diagnostic"))),
            "trial_uid": row["trial_uid"],
            "legacy_trial_number": int(row["legacy_trial_number"]),
            "source_git_sha": freeze["source_git_sha"],
            "plant_epoch": f"v3:{campaign.get('campaign_epoch', 'unknown')}:{capture.get('source_fingerprint_pre', 'unknown')}",
            "controller_identity": {"backend_id": trial.get("backend_id"), "config_fingerprint": capture.get("config_fingerprint_pre")},
            "tp_identity": {"source_fingerprint": capture.get("source_fingerprint_pre")},
            "trajectory_identity": {"stage_id": campaign.get("stage_id"), "source_stage_id": campaign.get("source_stage_id")},
            "objective_identity": {"metric": "fixed_f0_force_mae_n", "metric_role": row["metric_role"]},
            "parameter_semantics_fingerprint": f"v3-4d-overlay:{V3_OVERLAY_SHA256}",
            "action": {
                "force_p_gain": float(overlay["force_p_gain"]), "force_i_gain": float(overlay["force_i_gain"]),
                "force_damping": float(overlay["force_damping"]), "orientation_ko": float(overlay["orientation_ko"]),
            },
            "action_complete": True,
            "sequence_artifact": _sequence(bundle, verify_sources),
            "outcome_class": outcome,
            "metric_role": row["metric_role"],
            "objective": None,
            "reward": None,
            "reward_eligible": False,
            "constraint_eligible": False,
            "constraint_unsafe": None,
            "structural_failures": failures,
            "features": features,
            "missing_reasons": sorted(set(missing + (["objective_metric_unavailable"] if row["metric_value"] is None else ["diagnostic_metric_not_reward_eligible"]))),
            "split_group": f"v3:{campaign.get('campaign_epoch', 'unknown')}:{capture.get('source_fingerprint_pre', 'unknown')}",
        }
        records.append(_finalize_record(record))
    return records, {
        "kind": "v3_diagnostic_bundle", "sha256": V3_FREEZE_SHA256,
        "physical_trials": len(records), "optimizer_eligible": 0,
    }


def build_historical_dataset(db_path: Path, freeze_path: Path, verify_sources: bool = True) -> dict[str, Any]:
    v2_records, v2_source = extract_v2_records(db_path, verify_sources)
    v3_records, v3_source = extract_v3_records(freeze_path, verify_sources)
    records = sorted(v2_records + v3_records, key=lambda row: (row["source_kind"], row["legacy_trial_number"], row["trial_uid"]))
    counts = {
        "physical_trials": len(records),
        "reward_eligible": sum(row["reward_eligible"] for row in records),
        "constraint_eligible": sum(row["constraint_eligible"] for row in records),
        "action_complete": sum(row["action_complete"] for row in records),
        "by_source_kind": dict(sorted(Counter(row["source_kind"] for row in records).items())),
        "by_outcome_class": dict(sorted(Counter(row["outcome_class"] for row in records).items())),
        "by_metric_role": dict(sorted(Counter(row["metric_role"] for row in records).items())),
    }
    dataset_sha = canonical_sha256(records)
    manifest = {
        "schema": "step5d.offline-trial-rl/historical-dataset-manifest-v1",
        "sources": [v2_source, v3_source], "records": records,
        "counts": counts, "dataset_sha256": dataset_sha,
    }
    validate_manifest(manifest)
    return manifest
