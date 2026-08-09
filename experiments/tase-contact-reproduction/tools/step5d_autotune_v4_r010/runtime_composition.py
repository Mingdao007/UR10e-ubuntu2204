"""Identity-bound R010 runtime composition and fail-closed readiness audit.

R010 v1 deliberately stops at an offline release.  This module makes the
missing live control-plane pieces machine-readable so a controller triplet or
an optimizer worker cannot be mistaken for a runnable campaign.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Mapping


RUNTIME_COMPOSITION_SCHEMA = "step5d.autotune-v4/r010-runtime-composition-v1"
READINESS_REPORT_SCHEMA = "step5d.autotune-v4/r010-runtime-readiness-report-v1"


class R010RuntimeCompositionError(ValueError):
    """R010 static composition or activation request is invalid."""


_RUNTIME_COMPOSITION = {
    "schema": RUNTIME_COMPOSITION_SCHEMA,
    "release_scope": "offline_candidate_live_blocked",
    "control_plane": {
        "host_campaign_runner": {
            "identity_bound": False,
            "implementation": None,
            "status": "missing",
        },
        "live_writer_adapter": {
            "identity_bound": False,
            "implementation": None,
            "status": "missing",
        },
        "single_writer_route_gate": "not_composed",
    },
    "data_plane": {
        "controller_triplet": "identity_bound_offline_only",
        "ledger": "release_identity_bound_empty",
        "optimizer_worker": "calibration_and_environment_attested",
        "runtime_protocol": 609009,
    },
    "observability": {
        "component_identity": "r009_bytes_pinned",
        "host_wiring": "unproven",
    },
    "early_abort": {
        "active_allowed": False,
        "channel_c": "absent",
        "enters_control": False,
        "enters_gp_training": False,
        "enters_ledger": False,
        "host_wiring": "unproven",
        "mode": "shadow",
    },
    "containment": {
        "legacy_r008_tube_reuse_allowed": False,
        "mode": "prohibited",
        "required_evidence": [
            "r010_tool_geometry",
            "r010_path_frame_transform",
            "r010_tracking_error_budget",
            "r010_shadow_feasibility",
        ],
        "shadow_analysis_allowed": True,
    },
    "activation": {
        "bo_dispatch_allowed": False,
        "current_pointer_switch_allowed": False,
        "live_allowed": False,
        "requires_new_behavior_manifest": True,
    },
}

_STATIC_BLOCKERS = (
    "host_campaign_runner_missing",
    "live_writer_adapter_missing",
    "single_writer_route_gate_not_composed",
    "observability_host_wiring_unproven",
    "early_abort_shadow_host_wiring_unproven",
    "containment_geometry_frame_evidence_missing",
    "controller_delivery_readback_unverified",
    "fixed_anchor_wave7_canary_missing",
)


def _json_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_tree(item) for item in value]
    return copy.deepcopy(value)


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _json_tree(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise R010RuntimeCompositionError(
            f"runtime composition is not canonical JSON: {exc}"
        ) from exc


def runtime_composition_manifest() -> dict[str, Any]:
    """Return a detached copy of the immutable R010 v1 composition."""

    return copy.deepcopy(_RUNTIME_COMPOSITION)


def validate_runtime_composition(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise R010RuntimeCompositionError("R010 runtime composition must be an object")
    detached = json.loads(_canonical_bytes(value).decode("utf-8"))
    if detached != _RUNTIME_COMPOSITION:
        raise R010RuntimeCompositionError("R010 runtime composition differs")
    return detached


def build_readiness_report(contract: Any | None = None) -> dict[str, Any]:
    """Cold-bind an offline readiness report to one validated R010 release."""

    if contract is None:
        from .contracts import load_contract

        contract = load_contract()
    raw = getattr(contract, "raw", None)
    release_identity = getattr(contract, "release_identity", None)
    if not isinstance(raw, Mapping) or release_identity is None:
        raise R010RuntimeCompositionError("R010 readiness requires a validated contract")
    composition = validate_runtime_composition(
        getattr(contract, "behavior_manifest").raw["runtime_composition"]
    )
    if raw.get("status") != "offline_candidate_live_blocked":
        raise R010RuntimeCompositionError("R010 contract is not the offline candidate")
    if any(raw.get("offline_boundary", {}).values()):
        raise R010RuntimeCompositionError("R010 offline boundary is not fail-closed")
    report = {
        "schema": READINESS_REPORT_SCHEMA,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "release_identity_sha256": contract.release_identity_sha256,
        "contract_sha256": contract.sha256,
        "runtime_composition": composition,
        "offline_analysis_ready": True,
        "live_ready": False,
        "bo_dispatch_allowed": False,
        "blockers": list(_STATIC_BLOCKERS),
        "evidence_boundary": {
            "offline_tests_are_live_acceptance": False,
            "controller_triplet_bytes_are_delivery_readback": False,
            "historical_r008_wave7_is_r010_canary": False,
        },
    }
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()
    return report


def require_live_ready(report: Mapping[str, Any]) -> None:
    """Fail closed; R010 v1 cannot be upgraded by caller-supplied evidence."""

    if not isinstance(report, Mapping) or report.get("schema") != READINESS_REPORT_SCHEMA:
        raise R010RuntimeCompositionError("R010 readiness report differs")
    raise R010RuntimeCompositionError(
        "R010 v1 is offline-only; live activation requires a new identity-bound composition"
    )


__all__ = [
    "READINESS_REPORT_SCHEMA",
    "RUNTIME_COMPOSITION_SCHEMA",
    "R010RuntimeCompositionError",
    "build_readiness_report",
    "require_live_ready",
    "runtime_composition_manifest",
    "validate_runtime_composition",
]
