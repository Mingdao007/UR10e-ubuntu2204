"""Isolated source/current-stage resolver for TacDiffusion formal V4.

This route is an ephemeral Remote Secondary Client program, not a TP package.
The resolver therefore proves exact repository source identity and declares TP
upload/read-back inapplicable; it never fabricates controller read-back.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


FORMAL_SOURCE_CLOSURE_SCHEMA_V1 = "ur10e_tacdiffusion_formal_source_closure/v1"
FORMAL_CURRENT_STAGE_SCHEMA_V1 = "ur10e_tacdiffusion_formal_current_stage/v1"
FORMAL_STAGE_TABLE_SCHEMA_V1 = "ur10e_tacdiffusion_formal_stage_table/v1"


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha256(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"formal identity JSON is not an object: {path}")
    return value


def _repository_root(value: str | Path) -> Path:
    candidate = Path(value).resolve()
    if (candidate / "experiments" / "tase-contact-reproduction").is_dir():
        return candidate
    if candidate.name == "tase-contact-reproduction" and candidate.parent.name == "experiments":
        return candidate.parents[1]
    raise ValueError("formal identity root is not the repository or tase experiment root")


def _confined_file(root: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise ValueError(f"{label} path is missing")
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} path escapes experiment root") from exc
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} is not a regular in-root file: {relative}")
    return path


def load_formal_source_closure(
    experiment_root: str | Path,
    closure_path: str | Path,
) -> dict[str, Any]:
    root = _repository_root(experiment_root)
    closure_file = Path(closure_path).resolve()
    closure = _load_object(closure_file)
    if closure.get("schema_version") != FORMAL_SOURCE_CLOSURE_SCHEMA_V1:
        raise ValueError("unsupported formal source closure schema")
    files = closure.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("formal source closure file list is missing")
    seen: set[str] = set()
    canonical_files: list[dict[str, str]] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            raise ValueError("formal source closure entry must be an object")
        relative = str(entry.get("path", ""))
        if relative in seen:
            raise ValueError("formal source closure contains a duplicate path")
        seen.add(relative)
        path = _confined_file(root, relative, "formal source closure entry")
        actual = sha256_file(path)
        if entry.get("sha256") != actual:
            raise ValueError(f"formal source closure hash mismatch: {relative}")
        canonical_files.append({"path": relative, "sha256": actual})
    if canonical_files != files:
        raise ValueError("formal source closure entries are not canonical")
    content_address = closure.get("content_address")
    if not isinstance(content_address, Mapping):
        raise ValueError("formal source closure content address is missing")
    expected = canonical_sha256({"files": canonical_files})
    if content_address.get("algorithm") != "sha256" or content_address.get("sha256") != expected:
        raise ValueError("formal source closure aggregate digest mismatch")
    if closure.get("model_active") is not False or closure.get("shadow_only") is not True:
        raise ValueError("formal source closure must remain model-inactive/shadow-only")
    return closure


def resolve_formal_current_state(experiment_root: str | Path) -> dict[str, Any]:
    root = _repository_root(experiment_root)
    formal_root = root / "experiments" / "tase-contact-reproduction"
    current_path = formal_root / "config" / "tacdiffusion_formal_v4_current_stage.json"
    table_path = formal_root / "config" / "tacdiffusion_formal_v4_stage_table.json"
    current = _load_object(current_path)
    table = _load_object(table_path)
    blockers: list[str] = []
    if current.get("schema_version") != FORMAL_CURRENT_STAGE_SCHEMA_V1:
        blockers.append("current_stage_schema_invalid")
    if table.get("schema_version") != FORMAL_STAGE_TABLE_SCHEMA_V1:
        blockers.append("stage_table_schema_invalid")
    if current.get("lineage") != "tacdiffusion_formal_v4":
        blockers.append("lineage_mismatch")
    if current.get("route") != "remote_secondary_client_direct_torque":
        blockers.append("route_mismatch")
    if current.get("controller_target") != "secondary_client://192.168.1.18:30002":
        blockers.append("controller_target_mismatch")
    if (
        current.get("tp_package_applicable") is not False
        or current.get("controller_upload_applicable") is not False
        or current.get("controller_readback_applicable") is not False
    ):
        blockers.append("ephemeral_route_tp_boundary_invalid")
    if current.get("loaded_tp_executed") is not False:
        blockers.append("ephemeral_route_loaded_tp_boundary_invalid")
    if current.get("kunwei_only") is not True or current.get("ur_internal_ft_used") is not False:
        blockers.append("kunwei_only_boundary_invalid")
    if current.get("model_active") is not False or current.get("shadow_only") is not True:
        blockers.append("model_authority_boundary_invalid")
    stages = table.get("stages")
    if not isinstance(stages, list):
        blockers.append("stage_table_rows_missing")
        stages = []
    current_id = current.get("current_stage_id")
    active = [row for row in stages if isinstance(row, Mapping) and row.get("active") is True]
    if len(active) != 1 or active[0].get("id") != current_id:
        blockers.append("stage_table_active_identity_mismatch")
    closure_result: dict[str, Any] | None = None
    try:
        closure_relative = current.get("source_closure_path")
        closure_file = _confined_file(root, closure_relative, "formal source closure")
        if current.get("source_closure_file_sha256") != sha256_file(closure_file):
            raise ValueError("formal current-stage closure file hash mismatch")
        closure_result = load_formal_source_closure(root, closure_file)
        if current.get("source_content_sha256") != closure_result["content_address"]["sha256"]:
            raise ValueError("formal current-stage content address mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        blockers.append(f"source_closure_invalid:{exc}")
    state_by_stage = {
        "formal_v4_no_contact_qualification": "OFFLINE_READY_LIVE_QUALIFICATION_PENDING",
        "formal_v4_fixed_k_campaign": "SOURCE_READY_FIXED_K_LIVE_PREFLIGHT_REQUIRED",
        "formal_v4_fixed_k_shadow": "FIXED_K_MODEL_SHADOW_PENDING",
        "formal_v4_variable_k_campaign": "SOURCE_READY_VARIABLE_K_LIVE_PREFLIGHT_REQUIRED",
        "formal_v4_variable_k_shadow": "VARIABLE_K_MODEL_SHADOW_PENDING",
        "formal_v4_complete": "FORMAL_V4_COMPLETE",
    }
    next_by_stage = {
        "formal_v4_no_contact_qualification": "seven_family_no_contact_qualification",
        "formal_v4_fixed_k_campaign": "fixed_k_contact_campaign",
        "formal_v4_fixed_k_shadow": "fixed_k_training_timing_and_two_shadows",
        "formal_v4_variable_k_campaign": "variable_k_contact_campaign",
        "formal_v4_variable_k_shadow": "variable_k_training_timing_and_two_shadows",
        "formal_v4_complete": "none",
    }
    state = state_by_stage.get(str(current_id), "BLOCKED") if not blockers else "BLOCKED"
    return {
        "schema_version": "ur10e_tacdiffusion_formal_resolver/v1",
        "ok": not blockers,
        "state": state,
        "lineage": current.get("lineage"),
        "current_stage_id": current_id,
        "route": current.get("route"),
        "controller_target": current.get("controller_target"),
        "source_content_sha256": (
            None
            if closure_result is None
            else closure_result["content_address"]["sha256"]
        ),
        "repository_state_consistent": not blockers,
        "controller_readback_applicable": False,
        "controller_readback_claimed": False,
        "live_ready": False,
        "next_action": next_by_stage.get(str(current_id), "repair_formal_identity") if not blockers else "repair_formal_identity",
        "blockers": blockers,
    }


__all__ = [
    "FORMAL_CURRENT_STAGE_SCHEMA_V1",
    "FORMAL_SOURCE_CLOSURE_SCHEMA_V1",
    "FORMAL_STAGE_TABLE_SCHEMA_V1",
    "canonical_sha256",
    "load_formal_source_closure",
    "resolve_formal_current_state",
    "sha256_file",
]
