#!/usr/bin/env python3
"""Build the content-addressed offline source closure for V4 r005."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from step5d_autotune_v4_r005.contracts import load_contract
from step5d_optimizer_runtime import resolve_optimizer_runtime


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "config/step5d/autotune_v4_r005_offline_closure.json"
SCHEMA = "step5d.autotune-v4/r005-offline-closure-v2"
PROGRAM = "step5d_strict_rnn_autotune_v4_r005"


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _regular(path: Path, role: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{role} is missing or unsafe: {path}")
    return path


def _bytes(path: Path, overrides: Mapping[str, bytes] | None = None) -> bytes:
    key = _relative(path)
    if overrides is not None and key in overrides:
        return overrides[key]
    return _regular(path, "closure source").read_bytes()


def _sha256(path: Path, overrides: Mapping[str, bytes] | None = None) -> str:
    return hashlib.sha256(_bytes(path, overrides)).hexdigest()


def _paths() -> tuple[Path, ...]:
    fixed = (
        "STEP5D_FLOW.md",
        "config/step5_stage_table.json",
        "config/step5d/autotune_v4_r005.json",
        "config/step5d/autotune_v4_r005_runtime_manifest.json",
        "config/step5d/autotune_v4_live_writer_r005.json",
        "config/step5/step5d_autotune_v4_r005_launch_profile.json",
        "tools/build_step5d_autotune_v4_r005.py",
        "tools/build_step5d_autotune_v4_r005_offline_closure.py",
        "tools/prepare_step5d_autotune_v4_r005_live.py",
        "tools/launch_step5d_autotune_v4_r005_control.py",
        "tools/launch_step5d_managed_runtime.py",
        "tools/run_step5d_autotune_v4_r005.py",
        "tools/step5d_force_objective.py",
        "tools/step5d_x_pending.py",
        "tools/step5d_optimizer_runtime.py",
        "tools/step5d_managed_runtime.py",
        "tools/step5d_autotune_optimizer.py",
        "tools/step5d_parameter_queue.py",
        "tools/step5d_autotune_v3/runtime_installation.py",
        "tools/step5d_autotune_v3/runtime_environment.py",
        "config/step5/step5d_v3_runtime_contract.json",
        "config/step5d/current.json",
        "config/step5d/autotune_v4_r004.json",
        "config/step5d/autotune_v4_live_writer_r004.json",
        "config/step5d/autotune_v4_r004_offline_closure.json",
        "config/step5d/autotune_v4_r005_baseline_ledger_genesis.json",
        "tools/step5d_autotune_v4_r004_live_writer.py",
        "tools/step5d_autotune_v4_r004/session.py",
    )
    package = tuple(sorted((ROOT / "tools/step5d_autotune_v4_r005").glob("*.py")))
    mature = tuple(sorted((ROOT / "tools/step5d_autotune_v4_r004").glob("*.py")))
    artifacts = tuple(
        ROOT / "programs/step5/step5d" / f"{PROGRAM}{suffix}"
        for suffix in (
            ".script",
            ".txt",
            ".urp",
            ".numeric-sanity.json",
            ".deploy-manifest.json",
        )
    )
    paths = tuple(ROOT / relative for relative in fixed) + package + mature + artifacts
    unique: dict[str, Path] = {}
    for path in paths:
        unique[_relative(path)] = path
    return tuple(unique[key] for key in sorted(unique))


def _load_json(
    path: Path,
    role: str,
    overrides: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(_bytes(path, overrides).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{role} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{role} must be an object")
    return value


def build_document(
    *,
    contract: Any | None = None,
    overrides: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    contract = load_contract() if contract is None else contract
    runtime = resolve_optimizer_runtime()
    source_hashes = {
        _relative(path): _sha256(path, overrides)
        for path in _paths()
    }
    artifacts = {
        suffix.lstrip("."): {
            "path": _relative(path),
            "sha256": _sha256(path, overrides),
        }
        for suffix, path in (
            ("script", ROOT / "programs/step5/step5d" / f"{PROGRAM}.script"),
            ("txt", ROOT / "programs/step5/step5d" / f"{PROGRAM}.txt"),
            ("urp", ROOT / "programs/step5/step5d" / f"{PROGRAM}.urp"),
            (
                "numeric_sanity",
                ROOT / "programs/step5/step5d" / f"{PROGRAM}.numeric-sanity.json",
            ),
            (
                "deploy_manifest",
                ROOT / "programs/step5/step5d" / f"{PROGRAM}.deploy-manifest.json",
            ),
        )
    }
    numeric = _load_json(
        ROOT / "programs/step5/step5d" / f"{PROGRAM}.numeric-sanity.json",
        "r005 numeric sanity",
        overrides,
    )
    return {
        "schema": SCHEMA,
        "program": PROGRAM,
        "release_contract": {
            "path": "config/step5d/autotune_v4_r005.json",
            "sha256": contract.sha256,
            "campaign_fingerprint": contract.campaign_fingerprint,
        },
        "lineage": {
            "parent": "step5d_strict_rnn_autotune_v4_r004",
            "r004_identity_preserved": True,
            "r004_qualifications_imported": False,
            "parent_hashes": dict(contract.r004_parent_sha256),
            "selector_path": "config/step5d/lineage_selector_v4_r005.json",
            "baseline_genesis_path": "config/step5d/autotune_v4_r005_baseline_ledger_genesis.json",
            "selector_and_baseline_are_bound_after_closure": True,
        },
        "force_objective": {
            "schema": "step5d.force-objective/v2",
            "version": "force_mae_v2",
            "target_force_n": 5.0,
            "target_is_optimizer_dimension": False,
            "formal_window_s": [5.0, 60.0],
            "legacy_shadow_window_s": [0.0, 55.0],
            "bin_width_s": 0.1,
            "required_complete_bins": 550,
            "legacy_shadow_audit_only": True,
            "raw_path_stage": 25,
            "semantic_fingerprint": "r005.force-mae-v2|stage=25|formal=[5,60)|legacy-shadow=[0,55)|bin=0.1s|bins=550|stat=mean(abs(mean(force_in_bin)-5N))|legacy=shadow-only",
            "receipt_version": "r005-sealed-sufficient-statistics-v1",
            "persist_raw_sample_arrays": True,
            "persist_raw_sample_identity_ids": False,
        },
        "path_reference": {
            "module": "tools/step5d_autotune_v4_r004/path_reference.py",
            "stage_id": "step5d_strict_rnn_autotune_v1",
            "stage_row_semantic_sha256": "3ba6468db434b6c5e5eca7ca387d01ad608d195f507caec27f56f081af40a925",
            "frame_semantic_sha256": "0a9b800b718c4058340961a4164eb2e5e48b1225b5c5b89f83f6da3a96dbfd82",
        },
        "runtime": {
            "control_host_separate_from_optimizer": True,
            "resolver": "step5d_optimizer_runtime.resolve_optimizer_runtime",
            "canonical_v3_pointer": "config/step5d/current.json",
            "pointer_sha256": _sha256(
                ROOT / "config/step5d/current.json",
                overrides,
            ),
            "profile": runtime.declaration.profile,
            "expected_child_attestation": runtime.expected_attestation(),
            "cuda_required": True,
            "local_or_degraded_fallback": False,
        },
        "async": {
            "pending_max": 2,
            "physical_inflight_max": 1,
            "refill_base": "pending_tail_else_last_physically_sealed_cursor",
            "x_pending_is_full_current_pending_set": True,
        },
        "artifacts": artifacts,
        "numeric_sanity": {
            "path": _relative(ROOT / "programs/step5/step5d" / f"{PROGRAM}.numeric-sanity.json"),
            "sha256": artifacts["numeric_sanity"]["sha256"],
            "passed": numeric.get("passed") is True,
        },
        "source_closure": {
            "file_count": len(source_hashes),
            "sha256": source_hashes,
        },
        "safety_boundary": {
            "controller_upload": False,
            "controller_readback": False,
            "dashboard_load": False,
            "dashboard_play": False,
            "arm": False,
            "motion": False,
            "contact": False,
            "zero_or_tare": False,
            "network": False,
            "live_evidence": False,
        },
        "claim_boundary": "offline source/package closure only; formal live MAE acceptance remains unclaimed",
    }


def write(*, replace: bool = False) -> dict[str, Any]:
    document = build_document()
    encoded = (json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    if OUTPUT.exists() and not replace and OUTPUT.read_bytes() != encoded:
        raise FileExistsError(f"closure differs; use --replace: {OUTPUT}")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{OUTPUT.name}.", dir=OUTPUT.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, OUTPUT)
        directory_fd = os.open(OUTPUT.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "path": _relative(OUTPUT),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "source_count": document["source_closure"]["file_count"],
        "live_evidence": False,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)
    print(json.dumps(write(replace=args.replace), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
