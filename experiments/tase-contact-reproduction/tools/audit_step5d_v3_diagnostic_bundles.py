#!/usr/bin/env python3
"""Read-only replay of the frozen Step5d trials 13..22 diagnostic bundles."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SOURCE = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SOURCE))

from ur10e_experiment_runtime.identity import (  # noqa: E402
    canonical_sha256,
    strict_json_loads,
)
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    Stage25ControllerProgressAdapter,
)


DEFAULT_MANIFEST = (
    ROOT / "config" / "step5" / "step5d_autotune_v3_diagnostic_bundles.json"
)
SPHERE_RADIUS_M = 0.015
REQUIRED_CSV_FIELDS = frozenset(
    {
        "_step4e_path_time_s",
        "ur_output_double_register_35",
        "ur_timestamp",
        "ur_actual_TCP_pose_0",
        "ur_actual_TCP_pose_1",
        "ur_actual_TCP_pose_2",
    }
)


class DiagnosticReplayError(RuntimeError):
    pass


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise DiagnosticReplayError(f"evidence artifact is missing or symlinked: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _mapping(value: Any, role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DiagnosticReplayError(f"{role} must be an object")
    return value


def load_freeze_manifest(path: Path) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DiagnosticReplayError("diagnostic freeze manifest is missing or symlinked")
    try:
        payload = strict_json_loads(path.read_bytes())
    except ValueError as exc:
        raise DiagnosticReplayError("diagnostic freeze manifest is not strict JSON") from exc
    manifest = _mapping(payload, "diagnostic freeze manifest")
    if manifest.get("schema") != "step5d.autotune-v3/diagnostic-bundle-freeze-v1":
        raise DiagnosticReplayError("diagnostic freeze schema differs")
    bundles = manifest.get("bundles")
    if not isinstance(bundles, list) or len(bundles) != 10:
        raise DiagnosticReplayError("diagnostic freeze must contain exactly ten bundles")
    trial_numbers = tuple(row.get("legacy_trial_number") for row in bundles if isinstance(row, Mapping))
    if trial_numbers != tuple(range(13, 23)):
        raise DiagnosticReplayError("diagnostic freeze must contain ordered trials 13..22")
    decision = _mapping(manifest.get("semantic_decision"), "semantic decision")
    if decision != {
        "new_fingerprint_required": True,
        "new_plant_epoch_required": True,
        "optimizer_population_allowed": False,
        "parameter_impact": "retune_required",
    }:
        raise DiagnosticReplayError("diagnostic freeze semantic decision differs")
    return manifest


def _campaign_file(root: Path, name: str) -> Path:
    relative = {
        "candidate_plan.json": "control/candidate_plan.json",
        "v3_trial_overlays.json": "control/v3_trial_overlays.json",
        "campaign_identity.json": "store/campaign_identity.json",
        "history.jsonl": "store/history.jsonl",
    }.get(name)
    if relative is None:
        raise DiagnosticReplayError(f"unknown frozen campaign file: {name}")
    return root / relative


def _replay_csv(path: Path) -> dict[str, Any]:
    adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    )
    active_rows = 0
    actual_breaches = 0
    maximum_distance_m = 0.0
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = csv.DictReader(handle)
        if rows.fieldnames is None or not REQUIRED_CSV_FIELDS.issubset(rows.fieldnames):
            raise DiagnosticReplayError("diagnostic CSV lacks sphere replay fields")
        for line_number, row in enumerate(rows, start=2):
            try:
                stage = float(row["ur_output_double_register_35"])
            except (TypeError, ValueError) as exc:
                raise DiagnosticReplayError(
                    f"diagnostic CSV stage is invalid at line {line_number}"
                ) from exc
            if abs(stage - 25.0) >= 0.03:
                continue
            try:
                progress_s = float(row["_step4e_path_time_s"])
                timestamp_s = float(row["ur_timestamp"])
                tcp = (
                    float(row["ur_actual_TCP_pose_0"]),
                    float(row["ur_actual_TCP_pose_1"]),
                    float(row["ur_actual_TCP_pose_2"]),
                )
            except (TypeError, ValueError) as exc:
                raise DiagnosticReplayError(
                    f"diagnostic CSV sphere input is invalid at line {line_number}"
                ) from exc
            progress = adapter.sample(
                stage=stage,
                controller_progress_s=progress_s,
                controller_tick_seq=int(round(timestamp_s * 500.0)),
                controller_timestamp_s=timestamp_s,
                age_ns=0,
                tcp_z_m=tcp[2],
            )
            center = (
                progress.center_x_m,
                progress.center_y_m,
                progress.center_z_m,
            )
            if not all(math.isfinite(value) for value in (*tcp, *center)):
                raise DiagnosticReplayError(
                    f"diagnostic CSV sphere center is invalid at line {line_number}"
                )
            distance_m = math.dist(tcp, center)
            active_rows += 1
            maximum_distance_m = max(maximum_distance_m, distance_m)
            actual_breaches += int(distance_m > SPHERE_RADIUS_M)
    if active_rows == 0:
        raise DiagnosticReplayError("diagnostic CSV contains no active Stage25 rows")
    return {
        "active_stage25_rows": active_rows,
        "actual_breach_count": actual_breaches,
        "maximum_actual_distance_m": maximum_distance_m,
    }


def audit(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = load_freeze_manifest(manifest_path)
    campaign_root = Path(str(manifest["source_campaign_root"])).resolve()
    if campaign_root.is_symlink() or not campaign_root.is_dir():
        raise DiagnosticReplayError("frozen source campaign root is unavailable")
    for name, expected in _mapping(
        manifest["source_campaign_files"], "source campaign files"
    ).items():
        if _sha256_file(_campaign_file(campaign_root, str(name))) != expected:
            raise DiagnosticReplayError(f"frozen campaign file drifted: {name}")

    results = []
    for frozen in manifest["bundles"]:
        expected = _mapping(frozen, "frozen bundle row")
        trial_uid = str(expected["trial_uid"])
        bundle_path = (
            campaign_root / "store" / "trials" / trial_uid / "immutable_trial_bundle.json"
        )
        if _sha256_file(bundle_path) != expected["bundle_sha256"]:
            raise DiagnosticReplayError(f"frozen bundle drifted: trial {expected['legacy_trial_number']}")
        bundle = _mapping(strict_json_loads(bundle_path.read_bytes()), "immutable bundle")
        trial = _mapping(bundle.get("trial"), "bundle trial")
        evaluation = _mapping(bundle.get("evaluation"), "bundle evaluation")
        provenance = _mapping(bundle.get("artifact_provenance"), "artifact provenance")
        csv_artifact = _mapping(provenance.get("csv"), "CSV provenance")
        metadata_artifact = _mapping(provenance.get("metadata"), "metadata provenance")
        terminal_artifact = _mapping(
            provenance.get("terminal_manifest"), "terminal manifest provenance"
        )
        csv_path = Path(str(csv_artifact.get("path"))).resolve()
        metadata_path = Path(str(metadata_artifact.get("path"))).resolve()
        terminal_path = Path(str(terminal_artifact.get("path"))).resolve()
        if any(
            (
                trial.get("trial_uid") != trial_uid,
                trial.get("trial_id") != expected["legacy_trial_number"],
                trial.get("candidate_uid") != expected["candidate_uid"],
                evaluation.get("disposition") != expected["source_disposition"],
                evaluation.get("structural_failures") != expected["structural_failures"],
                csv_artifact.get("sha256") != expected["csv_sha256"],
                csv_artifact.get("size_bytes") != expected["csv_size_bytes"],
                csv_path.stat().st_size != expected["csv_size_bytes"],
                _sha256_file(csv_path) != expected["csv_sha256"],
                metadata_artifact.get("sha256") != expected["metadata_sha256"],
                _sha256_file(metadata_path) != expected["metadata_sha256"],
                terminal_artifact.get("sha256")
                != expected["terminal_manifest_sha256"],
                _sha256_file(terminal_path)
                != expected["terminal_manifest_sha256"],
            )
        ):
            raise DiagnosticReplayError(
                f"frozen bundle identity/provenance differs: trial {expected['legacy_trial_number']}"
            )
        trial_number = int(expected["legacy_trial_number"])
        expected_role = "unavailable" if trial_number == 21 else "diagnostic_only"
        expected_metric = None
        if trial_number != 21:
            expected_metric = (
                _mapping(
                    _mapping(
                        _mapping(evaluation.get("metrics"), "evaluation metrics").get("governor"),
                        "governor metrics",
                    ).get("nontrainable_profile_diagnostic"),
                    "diagnostic metric",
                ).get("force_mae_n")
            )
        if any(
            (
                expected.get("metric_role") != expected_role,
                expected.get("metric_value") != expected_metric,
                expected.get("optimizer_eligible") is not False,
            )
        ):
            raise DiagnosticReplayError(
                f"frozen metric role differs: trial {trial_number}"
            )
        results.append(
            {
                "legacy_trial_number": trial_number,
                "trial_uid": trial_uid,
                "bundle_sha256": expected["bundle_sha256"],
                "csv_sha256": expected["csv_sha256"],
                "metric_role": expected_role,
                "metric_value": expected_metric,
                "optimizer_eligible": False,
                "structural_failures": list(expected["structural_failures"]),
                "moving_sphere_actual_geometry": _replay_csv(csv_path),
            }
        )
    actual_breaches = sum(
        row["moving_sphere_actual_geometry"]["actual_breach_count"]
        for row in results
    )
    identity = {
        "schema": "step5d.autotune-v3/diagnostic-bundle-replay-v1",
        "source_git_sha": manifest["source_git_sha"],
        "source_campaign_files": dict(manifest["source_campaign_files"]),
        "parameter_impact": "retune_required",
        "optimizer_population_allowed": False,
        "sphere_radius_m": SPHERE_RADIUS_M,
        "predicted_stop_replay": "unavailable_uncertified_stopping_bound",
        "results": results,
    }
    return {
        **identity,
        "actual_geometry_false_trip_count": actual_breaches,
        "actual_geometry_replay_pass": actual_breaches == 0,
        "audit_identity_sha256": canonical_sha256(identity),
        "claim_boundary": (
            "read-only historical geometry replay; no certified predicted-stop "
            "claim, optimizer admission, controller access, bridge, or motion"
        ),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    result.add_argument("--json", action="store_true")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = audit(args.manifest.resolve())
    except (DiagnosticReplayError, OSError, ValueError) as exc:
        print(f"diagnostic bundle replay failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True, allow_nan=False))
    else:
        print(
            "diagnostic_bundle_replay_pass "
            f"actual_geometry_false_trips={report['actual_geometry_false_trip_count']} "
            "predicted_stop=unavailable_uncertified_stopping_bound"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
