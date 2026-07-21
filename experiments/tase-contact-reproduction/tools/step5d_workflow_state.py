#!/usr/bin/env python3
"""Validate compact Step5d state, retained artifacts, and deterministic gates."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Collection, Mapping

from ur10e_artifact_store import (
    ARTIFACT_STORE_ENV,
    ArtifactRef,
    ArtifactStoreError,
    artifact_store as resolve_artifact_store,
    resolve_artifact,
    sha256,
)


ROOT = Path(__file__).resolve().parents[1]
CURRENT = Path("config/step5d/current.json")
ARTIFACT_LOCATOR = Path("config/step5d/artifact_locators/step5d_v35_retained_inputs.json")
STATES = ("candidate", "controller_verified", "live_ready", "live_accepted", "retained")
MANIFEST_SCHEMAS = {
    "candidate": "step5d_candidate_manifest_v1",
    "controller_verification": "step5d_controller_verification_manifest_v1",
    "live_ready": "step5d_live_ready_snapshot_v1",
}
HIGH_RISK_CONTRACTS = frozenset(
    {
        "force_frame",
        "units",
        "register_layout",
        "safety_limits",
        "stop_logic",
        "live_writer",
        "controller_transaction",
        "authorization_contract",
    }
)
CONTROLLER_READBACK_ROLES = frozenset(
    {
        "controller_readback_manifest",
        "controller_readback_script",
        "controller_readback_txt",
        "controller_readback_urp",
    }
)
_LOCATOR_FIELDS = {
    "schema_version",
    "program",
    "store_layout",
    "destructive_migration_performed",
    "artifacts",
}
_ARTIFACT_COMMON_FIELDS = {
    "logical_role",
    "original_path",
    "sha256",
    "size",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ROLE_RE = re.compile(r"[a-z0-9]+(?:_[a-z0-9]+)*")


class WorkflowStateError(ValueError):
    """Raised when a Step5d machine-state contract fails closed."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WorkflowStateError(f"missing required JSON: {path}") from exc
    except json.JSONDecodeError as exc:
        raise WorkflowStateError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise WorkflowStateError(f"expected JSON object in {path}")
    return payload


def artifact_store(*, root: Path = ROOT, value: Path | None = None) -> Path:
    return resolve_artifact_store(root=root, override=value)


def _relative_posix_path(value: Any, *, role: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise WorkflowStateError(f"artifact locator {role} must be a relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise WorkflowStateError(
            f"artifact locator {role} must be a normalized relative POSIX path"
        )
    return path


def _repository_artifact(
    *, root: Path, relative: PurePosixPath, digest: str, size: int, role: str
) -> Path:
    repository_root = root.resolve(strict=True)
    path = repository_root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise WorkflowStateError(
                f"repository artifact is symlink-backed for {role}: {relative}"
            )
    if not path.is_file():
        raise WorkflowStateError(
            f"repository artifact is missing or unsafe for {role}: {relative}"
        )
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(repository_root)
    except ValueError as exc:
        raise WorkflowStateError(
            f"repository artifact escapes root for {role}: {relative}"
        ) from exc
    if resolved.stat().st_size != size:
        raise WorkflowStateError(f"repository artifact size mismatch for {role}")
    if sha256(resolved) != digest:
        raise WorkflowStateError(f"repository artifact sha256 mismatch for {role}")
    return resolved


def resolve_artifacts(
    *,
    root: Path = ROOT,
    locator_path: Path | None = None,
    store: Path | None = None,
    required_roles: Collection[str] | None = None,
) -> dict[str, Path]:
    locator_path = locator_path or root / ARTIFACT_LOCATOR
    locator = load_json(locator_path)
    if (
        set(locator) != _LOCATOR_FIELDS
        or locator.get("schema_version") != "ur10e_artifact_locator_v2"
        or not isinstance(locator.get("program"), str)
        or not locator["program"]
        or locator.get("store_layout") != "sha256/<digest>"
        or locator.get("destructive_migration_performed") is not False
        or not isinstance(locator.get("artifacts"), list)
        or not locator["artifacts"]
    ):
        raise WorkflowStateError("unknown artifact locator schema")

    requested: frozenset[str] | None
    if required_roles is None:
        requested = None
    else:
        if isinstance(required_roles, (str, bytes)) or any(
            not isinstance(role, str) or _ROLE_RE.fullmatch(role) is None
            for role in required_roles
        ):
            raise WorkflowStateError("required artifact roles are invalid")
        requested = frozenset(required_roles)

    rows: dict[str, tuple[str, dict[str, Any]]] = {}
    for row in locator["artifacts"]:
        if not isinstance(row, dict):
            raise WorkflowStateError("artifact locator row must be an object")
        role = row.get("logical_role")
        digest = row.get("sha256")
        size = row.get("size")
        if (
            not isinstance(role, str)
            or _ROLE_RE.fullmatch(role) is None
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            raise WorkflowStateError("artifact locator row is incomplete")
        if role in rows:
            raise WorkflowStateError("artifact locator logical roles must be unique")
        _relative_posix_path(row.get("original_path"), role=f"{role} original_path")
        repository_backed = "repository_path" in row
        store_backed = "store_key" in row
        if repository_backed == store_backed:
            raise WorkflowStateError(
                f"artifact locator {role} must have exactly one storage source"
            )
        if repository_backed:
            if set(row) != _ARTIFACT_COMMON_FIELDS | {"repository_path"}:
                raise WorkflowStateError(
                    f"repository artifact locator fields differ for {role}"
                )
            _relative_posix_path(
                row["repository_path"], role=f"{role} repository_path"
            )
            rows[role] = ("repository", row)
        else:
            if set(row) != _ARTIFACT_COMMON_FIELDS | {"store_key"}:
                raise WorkflowStateError(
                    f"stored artifact locator fields differ for {role}"
                )
            try:
                ArtifactRef.from_mapping(row)
            except ArtifactStoreError as exc:
                raise WorkflowStateError(
                    f"artifact locator ref is invalid for {role}: {exc}"
                ) from exc
            rows[role] = ("store", row)

    selected = frozenset(rows) if requested is None else requested
    missing = sorted(selected - rows.keys())
    if missing:
        raise WorkflowStateError(f"required artifact roles are missing: {missing}")

    resolved: dict[str, Path] = {}
    store_root: Path | None = None
    for role in sorted(selected):
        source, row = rows[role]
        if source == "repository":
            resolved[role] = _repository_artifact(
                root=root,
                relative=_relative_posix_path(
                    row["repository_path"], role=f"{role} repository_path"
                ),
                digest=row["sha256"],
                size=row["size"],
                role=role,
            )
            continue
        try:
            if store_root is None:
                store_root = artifact_store(root=root, value=store)
            ref = ArtifactRef.from_mapping(row)
            resolved[role] = resolve_artifact(ref, store=store_root)
        except ArtifactStoreError as exc:
            raise WorkflowStateError(
                f"artifact resolution failed for {role}: {exc}"
            ) from exc
    return resolved


def classify_risk(
    *, changed_contracts: list[str], owners: list[str], impact_known: bool = True
) -> dict[str, Any]:
    contracts = sorted(set(changed_contracts))
    owner_set = sorted(set(owners))
    reasons = sorted(HIGH_RISK_CONTRACTS.intersection(contracts))
    if len(owner_set) != 1:
        reasons.append("cross_owner")
    if not impact_known:
        reasons.append("unknown_impact")
    risk = "high" if reasons else "low"
    return {
        "risk_class": risk,
        "changed_contracts": contracts,
        "owners": owner_set,
        "impact_known": impact_known,
        "reasons": sorted(set(reasons)),
        "review_gate": "1+1_once_or_1+0_unavailable" if risk == "high" else "0+0",
        "remediation_gate": "deterministic_closure_only",
    }


def assert_transition(previous: str, current: str) -> None:
    if previous not in STATES or current not in STATES:
        raise WorkflowStateError(f"unknown workflow state transition: {previous} -> {current}")
    allowed = {
        "candidate": {"controller_verified", "retained"},
        "controller_verified": {"live_ready", "retained"},
        "live_ready": {"live_accepted", "retained"},
        "live_accepted": {"retained"},
        "retained": set(),
    }
    if current not in allowed[previous]:
        raise WorkflowStateError(f"invalid workflow state transition: {previous} -> {current}")


def _validate_pointer(root: Path, name: str, pointer: Any) -> tuple[Path, dict[str, Any]]:
    if not isinstance(pointer, dict):
        raise WorkflowStateError(f"manifest pointer {name} is missing")
    relative = pointer.get("path")
    expected_sha = pointer.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected_sha, str):
        raise WorkflowStateError(f"manifest pointer {name} is incomplete")
    path = root / relative
    if not path.is_file() or sha256(path) != expected_sha:
        raise WorkflowStateError(f"manifest pointer hash mismatch: {name}")
    payload = load_json(path)
    if payload.get("schema_version") != MANIFEST_SCHEMAS[name]:
        raise WorkflowStateError(f"unknown {name} manifest schema")
    return path, payload


def _verify_controller_artifact_closure(
    candidate: Mapping[str, Any],
    controller: Mapping[str, Any],
    artifacts: Mapping[str, Path],
) -> None:
    extensions = (".script", ".txt", ".urp")
    candidate_sha = candidate.get("package_sha256")
    if not isinstance(candidate_sha, dict) or set(candidate_sha) != set(extensions):
        raise WorkflowStateError("candidate package SHA triplet is incomplete")
    actual_sha = {
        extension: sha256(artifacts[f"controller_readback_{extension[1:]}"])
        for extension in extensions
    }
    if candidate_sha != actual_sha:
        raise WorkflowStateError("stored read-back triplet differs from candidate package")

    readback_manifest = load_json(artifacts["controller_readback_manifest"])
    if (
        readback_manifest.get("status") != "controller read-back verified"
        or readback_manifest.get("delivery_mode") != "full_upload_readback"
        or readback_manifest.get("readback_source") != "fresh_controller_get"
    ):
        raise WorkflowStateError("stored controller read-back manifest is not a fresh verified closure")
    declared = readback_manifest.get("sha256") or {}
    for section in ("local", "controller", "readback"):
        if declared.get(section) != actual_sha:
            raise WorkflowStateError(
                f"stored read-back manifest {section} SHA differs from retained triplet"
            )
    validation = readback_manifest.get("validation") or {}
    program = candidate.get("program")
    target_dir = readback_manifest.get("target_dir")
    if (
        validation.get("program") != program
        or not isinstance(target_dir, str)
        or validation.get("target_dir") != target_dir
        or validation.get("script_node_path") != f"{target_dir}/{program}.script"
        or controller.get("controller_target") != f"{target_dir}/{program}.urp"
    ):
        raise WorkflowStateError("stored read-back program/target identity drift")
    validation_keys = {
        ".script": "script_sha256",
        ".txt": "txt_sha256",
        ".urp": "urp_sha256",
    }
    for extension, key in validation_keys.items():
        if validation.get(key) != actual_sha[extension]:
            raise WorkflowStateError(f"stored read-back validation {key} differs from retained triplet")

    artifact_roles = (controller.get("readback") or {}).get("artifact_roles") or {}
    expected_roles = {
        "controller_readback_manifest": sha256(artifacts["controller_readback_manifest"]),
        **{
            f"controller_readback_{extension[1:]}": digest
            for extension, digest in actual_sha.items()
        },
    }
    if artifact_roles != expected_roles:
        raise WorkflowStateError("controller manifest artifact-role binding mismatch")
    readback_transaction = readback_manifest.get("upload_transaction_id")
    controller_transaction = (controller.get("upload_transaction") or {}).get("id")
    if readback_transaction is not None or controller_transaction is not None:
        if (
            not isinstance(readback_transaction, str)
            or re.fullmatch(r"[0-9a-f]{32}", readback_transaction) is None
            or readback_transaction != controller_transaction
            or not isinstance(controller.get("controller_identity"), str)
            or not controller.get("controller_identity")
            or controller.get("controller_identity") != readback_manifest.get("controller")
        ):
            raise WorkflowStateError("controller upload transaction identity drift")


def verify_current(*, root: Path = ROOT, store: Path | None = None) -> dict[str, Any]:
    current = load_json(root / CURRENT)
    if current.get("schema_version") != "step5d_current_stage_v2":
        raise WorkflowStateError("unknown compact current-stage schema")
    state = current.get("state")
    program = current.get("program")
    decision_digest = current.get("decision_digest")
    if state not in STATES or not isinstance(program, str) or not isinstance(decision_digest, str):
        raise WorkflowStateError("compact current-stage identity is incomplete")
    if state in {"live_ready", "live_accepted"}:
        raise WorkflowStateError(
            "live evidence verification is not implemented; refusing live maturity claim"
        )
    execution = current.get("execution")
    if not isinstance(execution, dict):
        raise WorkflowStateError("live execution state is missing")
    live_authorized = execution.get("live_authorized")
    live_running = execution.get("live_running")
    if not isinstance(live_authorized, bool) or not isinstance(live_running, bool):
        raise WorkflowStateError("live authorization/running state must be explicit booleans")
    if live_running and (not live_authorized or state not in {"live_ready", "live_accepted"}):
        raise WorkflowStateError("live_running requires authorization and live-ready evidence")
    claims = current.get("claims")
    if not isinstance(claims, dict):
        raise WorkflowStateError("workflow claims are missing")
    expected_claims = {
        "candidate": {
            "new_version_complete": False,
            "controller_verified": False,
            "live_ready": False,
            "live_accepted": False,
        },
        "controller_verified": {
            "new_version_complete": True,
            "controller_verified": True,
            "live_ready": False,
            "live_accepted": False,
        },
        "live_ready": {
            "new_version_complete": True,
            "controller_verified": True,
            "live_ready": True,
            "live_accepted": False,
        },
        "live_accepted": {
            "new_version_complete": True,
            "controller_verified": True,
            "live_ready": True,
            "live_accepted": True,
        },
    }
    if state in expected_claims and claims != expected_claims[state]:
        raise WorkflowStateError("workflow claims do not match evidence maturity")

    pointers = current.get("manifests")
    if not isinstance(pointers, dict):
        raise WorkflowStateError("compact current-stage manifest map is missing")
    required = ["candidate"]
    if state in {"controller_verified", "live_ready", "live_accepted"}:
        required.append("controller_verification")
    if state in {"live_ready", "live_accepted"}:
        required.append("live_ready")

    manifests: dict[str, dict[str, Any]] = {}
    paths: dict[str, Path] = {}
    for name in required:
        path, payload = _validate_pointer(root, name, pointers.get(name))
        paths[name], manifests[name] = path, payload
        if payload.get("program") != program or payload.get("decision_digest") != decision_digest:
            raise WorkflowStateError(f"{name} manifest identity drift")
        expected_manifest_state = {
            "candidate": "candidate",
            "controller_verification": "controller_verified",
            "live_ready": "live_ready",
        }[name]
        if payload.get("state") != expected_manifest_state:
            raise WorkflowStateError(f"{name} manifest state mismatch")

    candidate_sha = sha256(paths["candidate"])
    controller = manifests.get("controller_verification")
    if controller is not None:
        binding = controller.get("candidate_manifest") or {}
        if binding.get("sha256") != candidate_sha:
            raise WorkflowStateError("controller manifest candidate binding mismatch")
        if controller.get("fresh_readback_verified") is not True:
            raise WorkflowStateError("controller_verified requires fresh read-back closure")

    live = manifests.get("live_ready")
    if live is not None:
        controller_sha = sha256(paths["controller_verification"])
        binding = live.get("controller_verification_manifest") or {}
        if binding.get("sha256") != controller_sha:
            raise WorkflowStateError("live-ready controller binding mismatch")
        if live.get("live_ready") is not True or live.get("blockers"):
            raise WorkflowStateError("live_ready state requires a blocker-free frozen snapshot")

    locator_pointer = current.get("artifact_locator")
    artifacts: dict[str, Path] = {}
    if locator_pointer is not None:
        if not isinstance(locator_pointer, dict) or not isinstance(locator_pointer.get("path"), str):
            raise WorkflowStateError("artifact locator pointer is incomplete")
        locator_path = root / locator_pointer["path"]
        if sha256(locator_path) != locator_pointer.get("sha256"):
            raise WorkflowStateError("artifact locator pointer hash mismatch")
        locator_payload = load_json(locator_path)
        if locator_payload.get("program") != program:
            raise WorkflowStateError("artifact locator program identity drift")
        selected_roles = (
            CONTROLLER_READBACK_ROLES
            if state in {"controller_verified", "live_ready", "live_accepted"}
            else frozenset()
        )
        artifacts = resolve_artifacts(
            root=root,
            locator_path=locator_path,
            store=store,
            required_roles=selected_roles,
        )
        if state in {"controller_verified", "live_ready", "live_accepted"}:
            missing_roles = sorted(CONTROLLER_READBACK_ROLES - artifacts.keys())
            if missing_roles:
                raise WorkflowStateError(
                    f"controller-verified artifact closure is incomplete: {missing_roles}"
                )
            _verify_controller_artifact_closure(manifests["candidate"], controller, artifacts)
    elif state in {"controller_verified", "live_ready", "live_accepted"}:
        raise WorkflowStateError("controller-verified state requires an artifact locator")
    return {
        "schema_version": "step5d_workflow_status_v1",
        "program": program,
        "state": state,
        "controller_verified": state in {"controller_verified", "live_ready", "live_accepted"},
        "live_ready": state in {"live_ready", "live_accepted"},
        "live_accepted": state == "live_accepted",
        "live_authorized": live_authorized,
        "live_running": live_running,
        "decision_digest": decision_digest,
        "manifest_paths": {name: str(path.relative_to(root)) for name, path in paths.items()},
        "retained_artifacts": {role: str(path) for role, path in artifacts.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("verify", "status", "risk"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--artifact-store", type=Path)
    parser.add_argument("--changed-contract", action="append", default=[])
    parser.add_argument("--owner", action="append", default=[])
    parser.add_argument("--unknown-impact", action="store_true")
    args = parser.parse_args(argv)
    if args.mode == "risk":
        payload = classify_risk(
            changed_contracts=args.changed_contract,
            owners=args.owner,
            impact_known=not args.unknown_impact,
        )
    else:
        payload = verify_current(root=args.root.resolve(), store=args.artifact_store)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
