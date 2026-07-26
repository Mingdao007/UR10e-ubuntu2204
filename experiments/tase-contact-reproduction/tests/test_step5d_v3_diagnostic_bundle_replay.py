from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import audit_step5d_v3_diagnostic_bundles as replay  # noqa: E402
from ur10e_experiment_runtime.stage_adapters import PATH_ORIGIN_XY_M  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path: Path) -> tuple[Path, Path]:
    campaign = tmp_path / "campaign"
    (campaign / "control").mkdir(parents=True)
    (campaign / "store" / "trials").mkdir(parents=True)
    campaign_files = {
        "candidate_plan.json": campaign / "control" / "candidate_plan.json",
        "v3_trial_overlays.json": campaign / "control" / "v3_trial_overlays.json",
        "campaign_identity.json": campaign / "store" / "campaign_identity.json",
        "history.jsonl": campaign / "store" / "history.jsonl",
    }
    for index, path in enumerate(campaign_files.values(), start=1):
        path.write_text(json.dumps({"fixture": index}, sort_keys=True) + "\n")

    frozen_rows = []
    first_csv: Path | None = None
    for trial_number in range(13, 23):
        trial_uid = f"{trial_number:064x}"
        trial_root = campaign / "store" / "trials" / trial_uid
        artifact_root = campaign / "artifacts" / trial_uid
        trial_root.mkdir()
        artifact_root.mkdir(parents=True)
        csv_path = artifact_root / "capture.csv"
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=sorted(replay.REQUIRED_CSV_FIELDS))
            writer.writeheader()
            for index in range(2):
                writer.writerow(
                    {
                        "_step4e_path_time_s": 0.0,
                        "ur_output_double_register_35": 25.0,
                        "ur_timestamp": 1.0 + index * 0.002,
                        "ur_actual_TCP_pose_0": PATH_ORIGIN_XY_M[0],
                        "ur_actual_TCP_pose_1": PATH_ORIGIN_XY_M[1],
                        "ur_actual_TCP_pose_2": 0.008,
                    }
                )
        if first_csv is None:
            first_csv = csv_path
        metadata_path = artifact_root / "metadata.json"
        terminal_path = artifact_root / "terminal.json"
        metadata_path.write_text("{}\n", encoding="utf-8")
        terminal_path.write_text("{}\n", encoding="utf-8")
        candidate_uid = f"{100 + trial_number:064x}"
        failures = ["rnn_oracle_failed"] if trial_number == 21 else ["diagnostic"]
        metric_role = "unavailable" if trial_number == 21 else "diagnostic_only"
        metric_value = None if trial_number == 21 else 0.5
        bundle = {
            "trial": {
                "trial_id": trial_number,
                "trial_uid": trial_uid,
                "candidate_uid": candidate_uid,
            },
            "evaluation": {
                "disposition": "fail_closed",
                "structural_failures": failures,
                "metrics": {
                    "governor": {
                        "nontrainable_profile_diagnostic": {"force_mae_n": 0.5}
                    }
                },
            },
            "artifact_provenance": {
                "csv": {
                    "path": str(csv_path.resolve()),
                    "sha256": sha(csv_path),
                    "size_bytes": csv_path.stat().st_size,
                },
                "metadata": {
                    "path": str(metadata_path.resolve()),
                    "sha256": sha(metadata_path),
                },
                "terminal_manifest": {
                    "path": str(terminal_path.resolve()),
                    "sha256": sha(terminal_path),
                },
            },
        }
        bundle_path = trial_root / "immutable_trial_bundle.json"
        bundle_path.write_text(json.dumps(bundle, sort_keys=True) + "\n", encoding="utf-8")
        frozen_rows.append(
            {
                "legacy_trial_number": trial_number,
                "trial_uid": trial_uid,
                "candidate_uid": candidate_uid,
                "bundle_sha256": sha(bundle_path),
                "csv_sha256": sha(csv_path),
                "csv_size_bytes": csv_path.stat().st_size,
                "metadata_sha256": sha(metadata_path),
                "terminal_manifest_sha256": sha(terminal_path),
                "metric_role": metric_role,
                "metric_value": metric_value,
                "optimizer_eligible": False,
                "source_disposition": "fail_closed",
                "structural_failures": failures,
            }
        )
    manifest = {
        "schema": "step5d.autotune-v3/diagnostic-bundle-freeze-v1",
        "source_campaign_root": str(campaign.resolve()),
        "source_git_sha": "a" * 40,
        "source_campaign_files": {
            name: sha(path) for name, path in campaign_files.items()
        },
        "semantic_decision": {
            "new_fingerprint_required": True,
            "new_plant_epoch_required": True,
            "optimizer_population_allowed": False,
            "parameter_impact": "retune_required",
        },
        "bundles": frozen_rows,
    }
    manifest_path = tmp_path / "freeze.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    assert first_csv is not None
    return manifest_path, first_csv


def test_exact_ten_bundle_replay_preserves_metric_roles_and_has_no_actual_breach(
    tmp_path: Path,
) -> None:
    manifest, _ = fixture(tmp_path)
    report = replay.audit(manifest)
    assert report["actual_geometry_replay_pass"] is True
    assert report["actual_geometry_false_trip_count"] == 0
    assert report["predicted_stop_replay"] == "unavailable_uncertified_stopping_bound"
    assert len(report["results"]) == 10
    assert report["results"][8]["legacy_trial_number"] == 21
    assert report["results"][8]["metric_role"] == "unavailable"
    assert report["results"][8]["metric_value"] is None
    assert all(row["optimizer_eligible"] is False for row in report["results"])


def test_replay_rejects_artifact_drift(tmp_path: Path) -> None:
    manifest, csv_path = fixture(tmp_path)
    csv_path.write_text("tampered\n", encoding="utf-8")
    with pytest.raises(replay.DiagnosticReplayError, match="identity/provenance"):
        replay.audit(manifest)
