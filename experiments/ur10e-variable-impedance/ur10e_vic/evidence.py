"""Portable validation for compact, tracked offline evidence indexes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .contracts import ClaimState


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_offline_claim_state(payload: dict[str, Any]) -> ClaimState:
    """Bind ClaimState to the top-level offline authorization gates."""

    claim = ClaimState(**payload["claim_state"])
    if payload.get("live_motion_authorized") is not False:
        raise ValueError("offline evidence must retain live_motion_authorized=false")
    if claim.authorization_status == "live_authorized":
        raise ValueError("offline evidence cannot claim live authorization")
    if claim.run_status == "live_completed":
        raise ValueError("offline evidence cannot claim a completed live run")
    if claim.acceptance_status == "live_accepted":
        raise ValueError("offline evidence cannot claim live acceptance")
    if claim.reproduction_status == "reproduction_complete":
        raise ValueError("offline evidence cannot claim reproduction completion")
    return claim


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_compact_evidence_index(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise ValueError("unsupported compact evidence schema")
    if payload.get("external_bundle_root_env") != "UR10E_VIC_EVIDENCE_ROOT":
        raise ValueError("portable evidence root environment binding drifted")
    if payload.get("live_motion_authorized") or payload.get("dbil_active_enabled"):
        raise ValueError("offline evidence must retain live/DBIL disabled gates")
    semantics = payload.get("manifest_semantics", {})
    if semantics != {
        "role": "cryptographic_locator_manifest",
        "default_validation_scope": "tracked_schema_and_current_source_bindings_only",
        "locator_only_is_claim_validation": False,
    }:
        raise ValueError("compact evidence locator/claim semantics drifted")
    validate_offline_claim_state(payload)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ValueError("compact evidence index needs artifacts")
    roles: set[str] = set()
    for artifact in artifacts:
        role = str(artifact.get("role", ""))
        relative = Path(str(artifact.get("bundle_relative_path", "")))
        if not role or role in roles:
            raise ValueError("artifact roles must be unique and non-empty")
        roles.add(role)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("artifact paths must be bundle-relative")
        if not _SHA256.fullmatch(str(artifact.get("sha256", ""))):
            raise ValueError("artifact hashes must be lowercase SHA-256")
        if artifact.get("claim_level") not in {
            "conversion_evidence",
            "subset_training_evidence",
            "diagnostic_shadow_only",
            "throughput_rejection_evidence",
        }:
            raise ValueError("unsupported compact evidence claim level")
    required = {
        "full_public_dataset",
        "full_public_manifest",
        "full_public_stats",
        "subset_checkpoint",
        "subset_training_curve",
        "subset_metrics",
        "subset_timing_unpaced",
        "subset_timing_paced_3x60s",
        "v27_trace_dataset",
        "v27_shadow_ablation",
        "v29_trace_dataset",
        "v29_shadow_ablation",
    }
    if not required <= roles:
        raise ValueError("compact evidence index is missing required roles")
    verification = payload.get("external_artifact_verification", {})
    if (
        verification.get("artifact_count") != len(artifacts)
        or verification.get("hash_origin")
        != "local_rehash_when_locator_manifest_was_captured"
        or verification.get("default_sparse_checkout_status")
        != "not_rehashed_unless_external_root_is_supplied"
        or verification.get("claim_validation_requires_all_present_and_hash_match")
        is not True
    ):
        raise ValueError("external artifact verification status is incomplete")

    experiment_root = path.resolve().parent.parent
    bindings = payload.get("implementation_bindings")
    if not isinstance(bindings, list):
        raise ValueError("current implementation source bindings are missing")
    expected_binding_roles = {
        "inference_source",
        "timing_source",
        "evidence_validator_source",
    }
    binding_roles: set[str] = set()
    for binding in bindings:
        role = str(binding.get("role", ""))
        relative = Path(str(binding.get("repository_relative_path", "")))
        if role in binding_roles or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid implementation source binding")
        binding_roles.add(role)
        source = experiment_root / relative
        if not source.is_file() or _sha256_file(source) != binding.get("sha256"):
            raise ValueError(f"current implementation source hash drifted: {role}")
        if binding.get("binding_scope") != "current_validator_not_historical_producer":
            raise ValueError("implementation source binding scope is ambiguous")
    if binding_roles != expected_binding_roles:
        raise ValueError("implementation source bindings are incomplete")
    if payload.get("public_data_scope", {}).get("usable_source_files") != 20:
        raise ValueError("full public conversion must bind 20 usable files")
    if payload.get("trace_scope", {}).get("claim_evidence_valid") is not False:
        raise ValueError("historical traces lack calibration/task-ZFT claim lineage")
    paced = payload.get("paced_timing", {})
    if (
        paced.get("result")
        != "all_paced_rates_failed_provenance_ineligible_shadow_only"
        or paced.get("selected_rate_hz") is not None
        or paced.get("selection_eligible") is not False
        or paced.get("shadow_only") is not True
        or paced.get("nonfinite_outputs") != 0
    ):
        raise ValueError("paced timing must retain the all-failed shadow-only result")
    if any(
        item.get("accepted") is not False or item.get("deadline_misses", 0) <= 0
        for item in paced.get("rate_results", {}).values()
    ) or set(paced.get("rate_results", {})) != {"200", "100", "50"}:
        raise ValueError("paced timing must bind rejected 200/100/50 Hz trials")
    timing_artifacts = {item["role"]: item for item in artifacts}
    if (
        timing_artifacts["subset_timing_unpaced"].get("selection_eligible")
        is not False
        or timing_artifacts["subset_timing_unpaced"].get("timing_provenance")
        != "unpaced_throughput_diagnostic"
        or timing_artifacts["subset_timing_paced_3x60s"].get(
            "selection_eligible"
        )
        is not False
        or timing_artifacts["subset_timing_paced_3x60s"].get(
            "timing_provenance"
        )
        != "legacy_independent_wall_clock_paced_trials_v1_missing_selection_eligible"
    ):
        raise ValueError("timing artifact selection provenance drifted")
    return payload


def verify_external_artifacts(
    payload: dict[str, Any], external_root: Path | None
) -> dict[str, Any]:
    """Optionally rehash external artifacts; locator validation alone never does."""

    artifacts = payload.get("artifacts", [])
    if external_root is None:
        return {
            "status": "not_attempted_external_root_absent",
            "external_rehash_performed": False,
            "artifact_integrity_verified": False,
            "claim_validation_ready": False,
            "claim_validation_status": "requires_external_rehash_and_domain_claim_validator",
            "verified_roles": [],
            "missing_roles": [item.get("role") for item in artifacts],
            "mismatched_roles": [],
        }
    verified: list[str] = []
    missing: list[str] = []
    mismatched: list[str] = []
    for artifact in artifacts:
        role = str(artifact["role"])
        candidate = external_root / artifact["bundle_relative_path"]
        if not candidate.is_file():
            missing.append(role)
        elif _sha256_file(candidate) != artifact["sha256"]:
            mismatched.append(role)
        else:
            verified.append(role)
    all_verified = not missing and not mismatched and len(verified) == len(artifacts)
    return {
        "status": (
            "verified_all_external_artifacts"
            if all_verified
            else "external_artifacts_missing_or_mismatched"
        ),
        "external_rehash_performed": True,
        "artifact_integrity_verified": all_verified,
        # Rehash proves artifact identity only. Scientific/live/reproduction
        # claims remain false until a separate domain claim validator passes.
        "claim_validation_ready": False,
        "claim_validation_status": "requires_separate_domain_claim_validator",
        "verified_roles": sorted(verified),
        "missing_roles": sorted(missing),
        "mismatched_roles": sorted(mismatched),
    }
