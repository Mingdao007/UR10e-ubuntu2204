"""Frozen-v1 profile and fail-closed contract helpers for Step5d autotune v3.

This module is deliberately offline-only.  It validates JSON and source hashes;
it never imports a robot client, opens a socket, or starts a process.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "step5d.autotune.v3.control-contract/v1"
CATEGORIES = (
    "control_invariant",
    "safety_invariant",
    "campaign_tunable",
    "runtime_identity",
)
EXPERIMENT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTRACT_PATH = (
    EXPERIMENT_ROOT / "config/step5/step5d_autotune_v3_control_contract.json"
)
DEFAULT_CANDIDATE = {
    "force_p_gain": 0.001,
    "force_i_gain": 0.00001,
    "force_damping": 7.0,
}


class ContractViolation(RuntimeError):
    """The launcher or its effective production configuration drifted."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractViolation(f"contract JSON repeats key {key!r}")
        result[key] = value
    return result


def _sha256(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractViolation(f"{name} must be a lowercase SHA-256")
    return value


def _git_sha1(value: Any, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 40
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ContractViolation(f"{name} must be a lowercase Git SHA-1")
    return value


def _validate_contract_document(payload: Any) -> dict[str, Any]:
    required = {
        "schema",
        "frozen_baseline",
        "execution_profile_id",
        "candidate_schema",
        "source_sha256",
        "tp_artifact_sha256",
        "candidate_tp_artifact_sha256",
        "deployment_tp_identity",
        "promotion_status",
        "cli_arguments",
        "forbidden_cli_flags",
        "forbidden_environment",
        "effective_fields",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise ContractViolation("control contract top-level schema differs")
    if payload["schema"] != SCHEMA:
        raise ContractViolation(f"unsupported control contract schema {payload['schema']!r}")

    baseline = payload["frozen_baseline"]
    if not isinstance(baseline, dict) or set(baseline) != {"tag", "commit", "stage_id"}:
        raise ContractViolation("frozen_baseline schema differs")
    if baseline["tag"] != "archive/step5d-autotune-v1-20260715":
        raise ContractViolation("frozen v1 tag differs")
    _git_sha1(baseline["commit"], name="frozen_baseline.commit")
    if baseline["stage_id"] != "step5d_strict_rnn_autotune_v1":
        raise ContractViolation("frozen v1 stage differs")
    if payload["execution_profile_id"] != "nf050-slew050-a050":
        raise ContractViolation("execution profile identity differs")

    candidate_schema = payload["candidate_schema"]
    candidate_fields = set(DEFAULT_CANDIDATE)
    if (
        not isinstance(candidate_schema, dict)
        or set(candidate_schema) != {"required", "additionalProperties"}
        or candidate_schema["additionalProperties"] is not False
        or candidate_schema["required"] != list(DEFAULT_CANDIDATE)
    ):
        raise ContractViolation(
            "candidate schema must require only force_p_gain/force_i_gain/force_damping"
        )

    sources = payload["source_sha256"]
    if not isinstance(sources, dict) or not sources:
        raise ContractViolation("source_sha256 must be a non-empty object")
    for relative, digest in sources.items():
        path = Path(relative)
        if not isinstance(relative, str) or path.is_absolute() or ".." in path.parts:
            raise ContractViolation(f"unsafe frozen source path {relative!r}")
        _sha256(digest, name=f"source_sha256.{relative}")
    artifacts = payload["tp_artifact_sha256"]
    if not isinstance(artifacts, dict) or set(artifacts) != {".urp", ".script", ".txt"}:
        raise ContractViolation("TP artifact hash set differs")
    for suffix, digest in artifacts.items():
        _sha256(digest, name=f"tp_artifact_sha256.{suffix}")
    candidate_artifacts = payload["candidate_tp_artifact_sha256"]
    if not isinstance(candidate_artifacts, dict) or set(candidate_artifacts) != {".urp", ".script", ".txt"}:
        raise ContractViolation("candidate TP artifact hash set differs")
    for suffix, digest in candidate_artifacts.items():
        _sha256(digest, name=f"candidate_tp_artifact_sha256.{suffix}")
    if payload["promotion_status"] != "requires_attended_tp_upload_readback":
        raise ContractViolation("candidate TP promotion status differs")
    deployment = payload["deployment_tp_identity"]
    if not isinstance(deployment, dict) or set(deployment) != {
        "program",
        "mode",
        "artifact_dir",
        "readback_manifest",
        "readback_manifest_sha256",
        "tp_fingerprint",
    }:
        raise ContractViolation("deployment_tp_identity schema differs")
    if (
        deployment["program"] != "step5d_strict_rnn_autotune_v3"
        or deployment["mode"]
        != "explicit_v3_identity_precontact_pose_frozen_v1_control"
    ):
        raise ContractViolation("deployment TP must be the explicit V3 identity package")
    for name in ("artifact_dir", "readback_manifest"):
        relative = deployment[name]
        path = Path(relative) if isinstance(relative, str) else Path("/")
        if not isinstance(relative, str) or path.is_absolute() or ".." in path.parts:
            raise ContractViolation(f"unsafe deployment TP path {relative!r}")
    _sha256(
        deployment["readback_manifest_sha256"],
        name="deployment_tp_identity.readback_manifest_sha256",
    )
    _sha256(
        deployment["tp_fingerprint"],
        name="deployment_tp_identity.tp_fingerprint",
    )

    forbidden_flags = payload["forbidden_cli_flags"]
    forbidden_environment = payload["forbidden_environment"]
    if (
        not isinstance(forbidden_flags, list)
        or len(forbidden_flags) != len(set(forbidden_flags))
        or any(not isinstance(flag, str) or not flag.startswith("--") for flag in forbidden_flags)
    ):
        raise ContractViolation("forbidden_cli_flags is invalid")
    if (
        not isinstance(forbidden_environment, list)
        or len(forbidden_environment) != len(set(forbidden_environment))
        or any(not isinstance(name, str) or not name for name in forbidden_environment)
    ):
        raise ContractViolation("forbidden_environment is invalid")

    arguments = payload["cli_arguments"]
    if not isinstance(arguments, list) or not arguments:
        raise ContractViolation("cli_arguments must be a non-empty list")
    seen_flags: set[str] = set()
    for row in arguments:
        if (
            not isinstance(row, list)
            or len(row) not in {1, 2}
            or not isinstance(row[0], str)
            or not row[0].startswith("--")
            or any(not isinstance(value, str) for value in row[1:])
        ):
            raise ContractViolation("every CLI row must be [flag] or [flag, value]")
        flag = row[0]
        if flag in seen_flags:
            raise ContractViolation(f"control contract repeats CLI flag {flag}")
        if flag in forbidden_flags:
            raise ContractViolation(f"forbidden CLI flag appears in launcher: {flag}")
        seen_flags.add(flag)

    fields = payload["effective_fields"]
    if not isinstance(fields, dict) or tuple(fields) != CATEGORIES:
        raise ContractViolation("effective field categories or order differ")
    seen_fields: set[str] = set()
    for category in CATEGORIES:
        rows = fields[category]
        if not isinstance(rows, dict) or not rows:
            raise ContractViolation(f"effective category {category} must be non-empty")
        overlap = seen_fields & set(rows)
        if overlap:
            raise ContractViolation(
                f"effective fields have duplicate classifications: {sorted(overlap)}"
            )
        seen_fields.update(rows)
    if set(fields["campaign_tunable"]) != {
        "step5d_autotune_force_p",
        "step5d_autotune_force_i",
        "step5d_autotune_force_damping",
        "step5d_autotune_force_terms",
    }:
        raise ContractViolation("campaign_tunable field set differs")
    if candidate_fields != set(candidate_schema["required"]):
        raise ContractViolation("candidate schema drifted from public candidate fields")
    return payload


def load_contract(path: Path | None = None) -> dict[str, Any]:
    supplied_path = path or DEFAULT_CONTRACT_PATH
    if supplied_path.is_symlink() or not supplied_path.is_file():
        raise ContractViolation(f"control contract must be a regular file: {supplied_path}")
    contract_path = supplied_path.resolve()
    try:
        payload = json.loads(
            contract_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractViolation(f"cannot load control contract: {exc}") from exc
    return _validate_contract_document(payload)


def _numeric_candidate_value(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ContractViolation(f"candidate {name} must be numeric")
    try:
        numeric = float(value)
    except (OverflowError, ValueError) as exc:
        raise ContractViolation(f"candidate {name} must be finite") from exc
    if not math.isfinite(numeric):
        raise ContractViolation(f"candidate {name} must be finite")
    return numeric


def normalize_candidate(candidate: Mapping[str, Any] | None = None) -> dict[str, float]:
    raw: Mapping[str, Any] = DEFAULT_CANDIDATE if candidate is None else candidate
    if not isinstance(raw, Mapping) or set(raw) != set(DEFAULT_CANDIDATE):
        raise ContractViolation(
            "candidate must contain exactly force_p_gain, force_i_gain, and force_damping"
        )
    values = {
        name: _numeric_candidate_value(name, raw[name])
        for name in DEFAULT_CANDIDATE
    }
    try:
        from step5d_autotune_contract import ForceCandidate

        frozen = ForceCandidate(**values)
    except (ImportError, TypeError, ValueError) as exc:
        raise ContractViolation(f"candidate violates the frozen v1 envelope: {exc}") from exc
    return {
        "force_p_gain": frozen.force_p_gain,
        "force_i_gain": frozen.force_i_gain,
        "force_damping": frozen.force_damping,
    }


validate_candidate = normalize_candidate


def candidate_force_terms(candidate: Mapping[str, Any]) -> dict[str, float]:
    values = normalize_candidate(candidate)
    p_gain = values["force_p_gain"]
    i_gain = values["force_i_gain"]
    damping = values["force_damping"]
    return {
        "P": p_gain,
        "I": i_gain,
        "damping": damping,
        "Md": 1.0 / p_gain,
        "kf": i_gain / p_gain,
        "Bd": damping / p_gain,
    }


def runtime_values(runtime_root: Path) -> dict[str, str]:
    if not isinstance(runtime_root, Path) or not runtime_root.is_absolute():
        raise ContractViolation("runtime_root must be an absolute pathlib.Path")
    resolved = runtime_root.resolve(strict=False)
    return {
        "command_mailbox": str(resolved / "command.json"),
        "output_dir": str(resolved / "bridge"),
    }


def resolve_expected_value(
    value: Any,
    *,
    candidate: Mapping[str, Any],
    runtime: Mapping[str, str],
) -> Any:
    candidate_values = normalize_candidate(candidate)
    placeholders: dict[str, Any] = {
        "$candidate.force_p_gain": candidate_values["force_p_gain"],
        "$candidate.force_i_gain": candidate_values["force_i_gain"],
        "$candidate.force_damping": candidate_values["force_damping"],
        "$candidate.force_terms": candidate_force_terms(candidate_values),
        "$runtime.command_mailbox": runtime["command_mailbox"],
        "$runtime.output_dir": runtime["output_dir"],
    }
    if isinstance(value, str) and value.startswith("$"):
        if value not in placeholders:
            raise ContractViolation(f"unknown control contract placeholder {value!r}")
        return placeholders[value]
    if isinstance(value, dict):
        return {
            key: resolve_expected_value(item, candidate=candidate_values, runtime=runtime)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            resolve_expected_value(item, candidate=candidate_values, runtime=runtime)
            for item in value
        ]
    return value


def expected_effective_config(
    contract: Mapping[str, Any],
    *,
    candidate: Mapping[str, Any],
    runtime: Mapping[str, str],
) -> dict[str, Any]:
    expected: dict[str, Any] = {}
    for category in CATEGORIES:
        for name, value in contract["effective_fields"][category].items():
            if name in expected:
                raise ContractViolation(f"effective field {name!r} is classified twice")
            expected[name] = resolve_expected_value(
                value,
                candidate=candidate,
                runtime=runtime,
            )
    return expected


def validate_source_bindings(
    contract: Mapping[str, Any],
    *,
    experiment_root: Path = EXPERIMENT_ROOT,
) -> dict[str, str]:
    observed: dict[str, str] = {}
    for relative, expected in contract["source_sha256"].items():
        path = experiment_root / relative
        if path.is_symlink() or not path.is_file():
            raise ContractViolation(f"governed control source is missing or symlinked: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        observed[relative] = digest
        if digest != expected:
            raise ContractViolation(
                f"governed control source drifted: {relative} "
                f"expected={expected} observed={digest}"
            )
    deployment = contract["deployment_tp_identity"]
    manifest_relative = deployment["readback_manifest"]
    manifest_path = experiment_root / manifest_relative
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ContractViolation("deployment TP readback manifest is missing or symlinked")
    manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    observed[manifest_relative] = manifest_digest
    if manifest_digest != deployment["readback_manifest_sha256"]:
        raise ContractViolation(
            "deployment TP readback manifest drifted: "
            f"expected={deployment['readback_manifest_sha256']} observed={manifest_digest}"
        )
    try:
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractViolation(f"cannot parse deployment TP readback manifest: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "step5d.autotune.controller-readback/v3"
        or manifest.get("verified") is not True
        or manifest.get("program") != deployment["program"]
        or manifest.get("tp_fingerprint") != deployment["tp_fingerprint"]
        or manifest.get("triplet_sha256") != contract["tp_artifact_sha256"]
    ):
        raise ContractViolation("deployment TP readback identity differs from the contract")
    for suffix, expected in contract["candidate_tp_artifact_sha256"].items():
        relative = Path(deployment["artifact_dir"]) / f"{deployment['program']}{suffix}"
        artifact = experiment_root / relative
        if artifact.is_symlink() or not artifact.is_file():
            raise ContractViolation(f"deployment TP artifact is missing or symlinked: {relative}")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        observed[str(relative)] = digest
        if digest != expected:
            raise ContractViolation(
                f"candidate TP artifact drifted: {relative} "
                f"expected={expected} observed={digest}"
            )
    return observed


def contract_sha256(contract: Mapping[str, Any] | None = None) -> str:
    return sha256_json(dict(contract or load_contract()))


def control_fingerprint(
    contract: Mapping[str, Any] | None = None,
    effective_config: Mapping[str, Any] | None = None,
) -> str:
    payload = dict(contract or load_contract())
    governed: dict[str, Any] = {}
    for category in ("control_invariant", "safety_invariant"):
        names = payload["effective_fields"][category]
        governed[category] = (
            {name: effective_config[name] for name in names}
            if effective_config is not None
            else names
        )
    material = {
        "schema": "step5d.autotune.v3.control-fingerprint/v1",
        "frozen_baseline": payload["frozen_baseline"],
        "execution_profile_id": payload["execution_profile_id"],
        "source_sha256": payload["source_sha256"],
        "tp_artifact_sha256": payload["tp_artifact_sha256"],
        "candidate_tp_artifact_sha256": payload["candidate_tp_artifact_sha256"],
        "promotion_status": payload["promotion_status"],
        "deployment_tp_identity": payload["deployment_tp_identity"],
        "governed_effective_fields": governed,
    }
    return sha256_json(material)
