#!/usr/bin/env python3
"""Cross-bind the current V3 package, readback, pose evidence, and release gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

import verify_step5d_autotune_v3_execution_readiness as execution_readiness


ROOT = Path(__file__).resolve().parents[1]
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
V1_STAGE_ID = "step5d_strict_rnn_autotune_v1"
POSE_PRIOR_ID = "step5d_v3_physical_prior_contact_0p1_20260719"
EXPECTED_ROTVEC = [3.120752062, 0.0, 0.068626833]
HISTORICAL_POSE_PRIOR_ID = "step5d_v3_start_pose_prior_contact_0p1_20260719"
HISTORICAL_ROTVEC = [3.141592654, 0.0, 0.0]
EXPECTED_XYZ = [0.487834547, 0.129337053, 0.022863519]
EXPECTED_CONTACT_PLUS_0P1S_POSE = [
    0.487834547,
    0.129337053,
    0.017863519,
    3.141592654,
    0.0,
    0.0,
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
BOUND_PATHS = {
    "config/current_stage.json",
    "config/step5_stage_table.json",
    "config/step5/step5d_autotune_v3_control_contract.json",
    "config/step5/step5d_autotune_v3_offline_validation.json",
    "config/step5/step5d_autotune_v3_live_promotion.json",
    "config/step5/step5d_autotune_v3_attempt_ledger.json",
    "config/step5d_autotune_controller_readback_v3.json",
    "evidence/step5d_autotune_v3/start_pose_prior_20260719.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.deploy-manifest.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.numeric-sanity.json",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.script",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.txt",
    "programs/step5/step5d/step5d_strict_rnn_autotune_v3.urp",
} | execution_readiness.READINESS_EVIDENCE_RELATIVE_PATHS


class ArtifactVerificationError(RuntimeError):
    """A current release artifact is missing, unsafe, or cross-bound incorrectly."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactVerificationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _load_json(path: Path, *, role: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ArtifactVerificationError(f"{role} is missing or unsafe")
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ArtifactVerificationError(f"non-finite JSON constant: {value}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactVerificationError(f"{role} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactVerificationError(f"{role} must be an object")
    return payload


def _require(actual: Any, expected: Any, role: str) -> None:
    if actual != expected:
        raise ArtifactVerificationError(
            f"{role} differs: expected={expected!r}, observed={actual!r}"
        )


def _v3_row(table: Mapping[str, Any]) -> Mapping[str, Any]:
    matches = [
        row
        for row in table.get("stages", [])
        if isinstance(row, dict) and row.get("id") == V3_STAGE_ID
    ]
    if len(matches) != 1:
        raise ArtifactVerificationError("V3 stage row must exist exactly once")
    return matches[0]


def verify(root: Path = ROOT) -> dict[str, Any]:
    root = root.expanduser().resolve(strict=True)
    try:
        readiness = execution_readiness.verify(root)
    except execution_readiness.ReadinessError as exc:
        raise ArtifactVerificationError(f"execution readiness failed: {exc}") from exc

    table = _load_json(root / "config/step5_stage_table.json", role="stage table")
    v3 = _v3_row(table)
    for field, expected in (("active", False), ("blocked", True), ("bridge", False)):
        _require(v3.get(field), expected, f"V3 selector {field}")

    package = v3.get("package_delivery") or {}
    basename = package.get("program_basename")
    prefix = package.get("local_triplet")
    triplet = package.get("sha256") or {}
    if basename != V3_STAGE_ID or not isinstance(prefix, str):
        raise ArtifactVerificationError("V3 package identity differs")
    if set(triplet) != {".script", ".txt", ".urp"}:
        raise ArtifactVerificationError("V3 package triplet fields differ")
    for extension, expected_sha in triplet.items():
        if not isinstance(expected_sha, str) or _SHA256.fullmatch(expected_sha) is None:
            raise ArtifactVerificationError(f"V3 package digest is invalid: {extension}")
        _require(_sha256(root / f"{prefix}{extension}"), expected_sha, f"V3 {extension} digest")

    deploy_path = root / f"{prefix}.deploy-manifest.json"
    _require(_sha256(deploy_path), package.get("tp_fingerprint"), "TP deploy fingerprint")
    deploy = _load_json(deploy_path, role="TP deploy manifest")
    expected_artifacts = [
        {
            "filename": f"{basename}{extension}",
            "sha256": triplet[extension],
            "source": f"{basename}{extension}",
        }
        for extension in (".script", ".txt", ".urp")
    ]
    _require(deploy.get("schema_version"), 1, "TP deploy schema")
    _require(deploy.get("basename"), basename, "TP deploy basename")
    _require(deploy.get("artifacts"), expected_artifacts, "TP deploy artifacts")

    readback_relative = package.get("controller_readback_manifest")
    if not isinstance(readback_relative, str):
        raise ArtifactVerificationError("controller readback path is missing")
    readback_path = root / readback_relative
    _require(
        _sha256(readback_path),
        package.get("controller_readback_manifest_sha256"),
        "controller readback digest",
    )
    readback = _load_json(readback_path, role="controller readback")
    _require(readback.get("verified"), True, "controller readback result")
    _require(readback.get("program"), basename, "controller readback program")
    contract = execution_readiness.load_contract(
        root / "config/step5/step5d_autotune_v3_control_contract.json"
    )
    _require(readback.get("triplet_sha256"), contract["tp_artifact_sha256"], "controller readback triplet")
    if readback.get("triplet_sha256") == triplet:
        raise ArtifactVerificationError(
            "historical controller readback unexpectedly matches new local triplet"
        )
    _require(
        readback.get("tp_fingerprint"),
        contract["deployment_tp_identity"]["tp_fingerprint"],
        "controller TP fingerprint",
    )

    numeric = _load_json(root / f"{prefix}.numeric-sanity.json", role="TP numeric sanity")
    for key, expected in (
        ("schema", "step5d.autotune-v3/tp-numeric-sanity-v1"),
        ("program", V3_STAGE_ID),
        (
            "delta_class",
            "identity_precontact_prior_exact_batch_lifecycle_return_angular_envelope_stage25_watchdog_v3",
        ),
        ("stage25_stale_command_hold_s", 0.02),
        ("precontact_pose_prior_id", POSE_PRIOR_ID),
        ("precontact_xyz_m", EXPECTED_XYZ),
        ("precontact_rotvec_rad", EXPECTED_ROTVEC),
        ("precontact_clearance_m", 0.005),
        ("minimum_start_above_entry_m", 0.01),
        ("precontact_z_policy", "contact_plus_0p1s_robust_z_plus_0p005m_clearance"),
        ("input_integer_registers", list(range(24, 31))),
        ("output_integer_registers", list(range(24, 34))),
        ("safe_transfer_z_m", 0.033),
        ("return_segment_count", 3),
        ("return_controller", "speedl_bounded_twist_v1"),
        ("return_angular_speed_limit_rad_s", 0.05),
        ("return_angular_acceleration_limit_rad_s2", 0.1),
        ("return_angular_speed_guard_rad_s", 0.06),
        ("return_angular_acceleration_guard_rad_s2", 0.5),
        ("return_angular_stop_deceleration_rad_s2", 0.1),
        ("return_orientation_admission_limit_rad", 0.3490658503988659),
        ("return_controller_period_s", 0.002),
        ("return_controller_max_sample_gap_s", 0.004),
        ("return_sample_gap_clock", "controller_monotonic_time_mode_0"),
        ("return_segment_phase_codes", [40.1, 40.2, 40.3]),
        (
            "return_continuous_telemetry_output_float_registers",
            list(range(39, 45)),
        ),
        (
            "batch_row_policy",
            "rows_1_to_9_near_ready_row_10_campaign_home",
        ),
    ):
        _require(numeric.get(key), expected, f"TP numeric sanity {key}")

    source = v3.get("source_binding") or {}
    prior_relative = source.get("precontact_pose_evidence")
    if not isinstance(prior_relative, str):
        raise ArtifactVerificationError("pose prior evidence path is missing")
    prior = _load_json(root / prior_relative, role="start pose prior")
    _require(prior.get("schema"), "step5d.autotune-v3/start-pose-prior/v2", "pose prior schema")
    _require(prior.get("prior_id"), HISTORICAL_POSE_PRIOR_ID, "pose prior identity")
    scope = prior.get("scope") or {}
    _require(scope.get("entry_xyz_m"), EXPECTED_XYZ, "pose prior XYZ")
    _require(
        scope.get("contact_plus_0p1s_tcp_pose_robust"),
        EXPECTED_CONTACT_PLUS_0P1S_POSE,
        "pose prior contact-plus-0.1s center",
    )
    _require(scope.get("tcp_rotvec_rad"), HISTORICAL_ROTVEC, "pose prior rotation")
    _require(scope.get("precontact_clearance_m"), 0.005, "pose prior clearance")
    _require(scope.get("exact_surface_fit_claim"), False, "pose prior fit claim")
    derivation = prior.get("derivation") or {}
    _require(derivation.get("trial_count"), 5, "pose prior source count")
    _require(
        derivation.get("sample_rule"),
        "first complete bridge row at or after contact_trigger_time + 0.100s",
        "pose prior sample rule",
    )
    _require(
        derivation.get("eligible_as_optimizer_objective"),
        False,
        "pose prior optimizer exclusion",
    )

    ledger_relative = v3.get("migration", {}).get("ledger")
    ledger_sha = v3.get("migration", {}).get("ledger_sha256")
    if not isinstance(ledger_relative, str):
        raise ArtifactVerificationError("attempt ledger path is missing")
    _require(_sha256(root / ledger_relative), ledger_sha, "attempt ledger digest")

    verified_paths = sorted(
        set(BOUND_PATHS)
        | {prior_relative, readback_relative, f"{prefix}.numeric-sanity.json"}
    )
    fingerprint_input = json.dumps(
        {relative: _sha256(root / relative) for relative in verified_paths},
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "schema": "step5d.autotune-v3/artifact-report-v2",
        "ok": True,
        "current_stage_id": V1_STAGE_ID,
        "v3_stage_id": V3_STAGE_ID,
        "v3_active": False,
        "execution_readiness": readiness["state"],
        "ready_to_execute": readiness["ready_to_execute"],
        "acceptance_scope": execution_readiness.VALIDATION_SCOPE,
        "user_authorization_required": readiness["user_authorization_required"],
        "tp_fingerprint": package.get("tp_fingerprint"),
        "control_fingerprint": readiness["identity"]["control_fingerprint"],
        "orchestration_fingerprint": readiness["identity"]["orchestration_fingerprint"],
        "artifact_set_fingerprint": hashlib.sha256(fingerprint_input).hexdigest(),
        "verified_paths": verified_paths,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = verify(args.root)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("step5d_autotune_v3_artifacts=pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
