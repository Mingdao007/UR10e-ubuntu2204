"""Bind a sealed STARS replay to R010 without entering campaign identity."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from step5d_autotune_v4_r010.contracts import load_contract

from ..replay import validate_completion_receipt
from .r008_run_dir import R008RunDirError, canonical_json_bytes, parse_strict_json_bytes


BINDING_SCHEMA = "stars_ft_bias_shadow/r010-analysis-binding-v1"


def _file_binding(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise R008RunDirError(f"binding input is not a regular file: {path}", code="r010_binding_invalid")
    data = path.read_bytes()
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def build_r010_binding(
    shadow_out_dir: Path,
    *,
    contract_path: Path,
    release_identity_path: Path,
) -> dict[str, Any]:
    out_dir = Path(shadow_out_dir)
    summary = validate_completion_receipt(out_dir)
    bundle = load_contract(contract_path, identity_path=release_identity_path)
    receipt_path = out_dir / "completion_receipt.json"
    receipt = parse_strict_json_bytes(receipt_path.read_bytes(), label="completion_receipt.json")
    if not isinstance(receipt, Mapping):
        raise R008RunDirError("STARS completion receipt is not an object", code="r010_binding_invalid")
    output_bindings = {
        name: _file_binding(out_dir / name)
        for name in ("input_manifest.json", "bias_est.jsonl", "summary.json", "completion_receipt.json")
    }
    payload: dict[str, Any] = {
        "schema": BINDING_SCHEMA,
        "r010_release_identity_sha256": bundle.release_identity_sha256,
        "r010_campaign_fingerprint": bundle.campaign_fingerprint,
        "stars_execution_identity_sha256": receipt["execution_identity_sha256"],
        "stars_input_manifest_sha256": receipt["input_manifest_sha256"],
        "stars_completion_receipt_sha256": output_bindings["completion_receipt.json"]["sha256"],
        "stars_output_files": output_bindings,
        "terminal_status": summary["terminal_status"],
        "rows_written": summary["rows_written"],
        "science_not_promoted": True,
        "campaign_identity_member": False,
        "gp_observation": False,
        "force_correction": False,
        "completion_certificate": False,
        "zero_tare_or_config_write": False,
    }
    payload["binding_sha256"] = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    return payload


def persist_r010_binding(path: Path, binding: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink() or target.parent.is_symlink():
        raise R008RunDirError("refusing to overwrite R010 STARS binding", code="r010_binding_exists")
    with target.open("xb") as handle:
        handle.write(canonical_json_bytes(dict(binding)) + b"\n")
    validate_r010_binding(target)
    return target


def validate_r010_binding(path: Path) -> dict[str, Any]:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise R008RunDirError("R010 STARS binding is not a regular file", code="r010_binding_invalid")
    value = parse_strict_json_bytes(target.read_bytes(), label="R010 STARS binding")
    if not isinstance(value, dict) or value.get("schema") != BINDING_SCHEMA:
        raise R008RunDirError("R010 STARS binding schema differs", code="r010_binding_invalid")
    digest = value.get("binding_sha256")
    payload = dict(value)
    payload.pop("binding_sha256", None)
    if digest != hashlib.sha256(canonical_json_bytes(payload)).hexdigest():
        raise R008RunDirError("R010 STARS binding digest differs", code="r010_binding_tampered")
    for key, expected in (
        ("science_not_promoted", True),
        ("campaign_identity_member", False),
        ("gp_observation", False),
        ("force_correction", False),
        ("completion_certificate", False),
        ("zero_tare_or_config_write", False),
    ):
        if value.get(key) is not expected:
            raise R008RunDirError(f"R010 STARS invariant differs: {key}", code="r010_binding_invalid")
    return value


__all__ = [
    "BINDING_SCHEMA",
    "build_r010_binding",
    "persist_r010_binding",
    "validate_r010_binding",
]
