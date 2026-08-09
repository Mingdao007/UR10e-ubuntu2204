"""Static R011 composition and fail-closed offline readiness report."""

from __future__ import annotations

import copy
from typing import Any, Mapping

from .common import R011ValueError, digest, json_tree


RUNTIME_COMPOSITION_SCHEMA = "step5d.autotune-v4/r011-runtime-composition-v1"
READINESS_REPORT_SCHEMA = "step5d.autotune-v4/r011-readiness-report-v1"


class R011RuntimeCompositionError(R011ValueError):
    """Static composition or activation request is invalid."""


_COMPOSITION = {
    "schema": RUNTIME_COMPOSITION_SCHEMA,
    "release_scope": "offline_candidate_live_blocked",
    "production_optimizer": "qLogNEI_reused_r010_bytes",
    "theory_shadow": {
        "implementation": "async_ts",
        "production_state": "separate",
        "ledger_authority": False,
        "execution_authority": False,
    },
    "censored_protocol": {"denominator_bins": 550, "bin_width_s": 0.1, "watermark_unit": "seconds", "mode": "shadow_only", "active_early_abort_allowed": False},
    "safety_filter": {"scope": "2-D_PATH_frame", "dt_s": 0.002, "hard_safety_independent": True},
    "stars": {"sidecar": "offline_replay", "admission": "explicit_batch_idle_only", "campaign_member": False},
    "activation": {"network": False, "upload": False, "readback": False, "load": False, "play": False, "bridge": False, "live": False, "current_pointer_switch": False},
}


def runtime_composition_manifest() -> dict[str, Any]:
    return copy.deepcopy(_COMPOSITION)


def validate_runtime_composition(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or json_tree(value) != _COMPOSITION:
        raise R011RuntimeCompositionError("R011 runtime composition differs")
    return json_tree(value)


def build_readiness_report(contract: Any | None = None) -> dict[str, Any]:
    if contract is None:
        from .contracts import load_contract

        contract = load_contract()
    if not hasattr(contract, "raw") or not hasattr(contract, "release_identity"):
        raise R011RuntimeCompositionError("readiness requires a validated R011 contract")
    boundary = contract.raw.get("offline_boundary")
    if not isinstance(boundary, Mapping) or any(boundary.values()):
        raise R011RuntimeCompositionError("R011 offline boundary is not fail-closed")
    report = {
        "schema": READINESS_REPORT_SCHEMA,
        "campaign_fingerprint": contract.campaign_fingerprint,
        "release_identity_sha256": contract.release_identity_sha256,
        "contract_sha256": contract.sha256,
        "runtime_composition": runtime_composition_manifest(),
        "offline_analysis_ready": True,
        "live_ready": False,
        "bo_dispatch_allowed": False,
        "blockers": [
            "controller_delivery_readback_unverified",
            "host_campaign_runner_missing",
            "live_writer_adapter_missing",
            "current_pointer_switch_forbidden",
            "theory_shadow_has_no_execution_authority",
        ],
    }
    report["report_sha256"] = digest(report)
    return report


def require_live_ready(report: Mapping[str, Any]) -> None:
    if not isinstance(report, Mapping) or report.get("schema") != READINESS_REPORT_SCHEMA:
        raise R011RuntimeCompositionError("R011 readiness report differs")
    raise R011RuntimeCompositionError("R011 is offline-only; live activation is not composed")


__all__ = [
    "READINESS_REPORT_SCHEMA", "R011RuntimeCompositionError", "RUNTIME_COMPOSITION_SCHEMA",
    "build_readiness_report", "require_live_ready", "runtime_composition_manifest",
    "validate_runtime_composition",
]
