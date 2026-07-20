#!/usr/bin/env python3
"""Verify the current Step5d package pointer has controller read-back evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

from step5d_workflow_state import WorkflowStateError, verify_current


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_DELIVERY_MODES = {
    None,
    "full_upload_readback",
    "content_addressed_reuse",
}


def fail(message: str) -> None:
    raise RuntimeError(f"refusing live Step5d bridge start: {message}")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"missing JSON file: {path}")
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON file {path}: {exc}")


def expected_installation_relative_path(target_dir: str) -> str:
    path = PurePosixPath(target_dir)
    try:
        programs_idx = path.parts.index("programs")
    except ValueError as exc:
        raise RuntimeError(f"target_dir must be under /programs: {target_dir}") from exc
    parent_levels = len(path.parts) - programs_idx - 1
    return "/".join([".."] * parent_levels + ["default"])


def manifest_path_from_current(root: Path, current: dict[str, Any], program: str) -> Path:
    for key in ("delivery_manifest", "controller_readback_manifest", "readback_manifest"):
        value = current.get(key)
        if value:
            return root / str(value)

    evidence = current.get("evidence", {})
    suffix = program.rsplit("_", 1)[-1]
    value = evidence.get(f"{suffix}_controller_readback_manifest")
    if value:
        return root / str(value)
    fail(f"controller read-back manifest is missing for {program}")


def current_sha(current: dict[str, Any]) -> dict[str, str]:
    sha = current.get("sha256") or current.get("evidence", {}).get("sha256") or {}
    return {ext: sha.get(ext, "") for ext in (".script", ".txt", ".urp")}


def selected_tp_program(root: Path, current: dict[str, Any], release: str) -> str:
    """Resolve the immutable TP package separately from the selected release."""

    compatibility_path = root / "config" / "step5d" / "current.json"
    if not compatibility_path.is_file():
        return release
    compatibility = load_json(compatibility_path)
    if (
        compatibility.get("selection_state") != "current"
        or compatibility.get("program") != release
        or compatibility.get("release_stage_id", release) != release
    ):
        fail("Step5d compatibility pointer does not bind the selected release")
    tp_program = compatibility.get("tp_program_id", release)
    if not isinstance(tp_program, str) or not tp_program:
        fail("Step5d compatibility pointer has no valid tp_program_id")
    return tp_program


def retained_manifest_path(root: Path, program: str) -> Path:
    """Resolve an ignored historical readback through the canonical locator."""
    try:
        status = verify_current(root=root)
    except WorkflowStateError as exc:
        fail(f"canonical retained-artifact binding is invalid: {exc}")
    if status.get("program") != program or status.get("controller_verified") is not True:
        fail(f"canonical retained-artifact binding does not verify {program}")
    retained = status.get("retained_artifacts") or {}
    value = retained.get("controller_readback_manifest")
    if not isinstance(value, str):
        fail("canonical retained-artifact binding has no controller read-back manifest")
    path = Path(value)
    if not path.is_file():
        fail(f"canonical retained controller read-back is missing: {path}")
    return path


def verify(
    root: Path,
    program: str | None = None,
    target_dir: str | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    current_path = root / "config" / "current_stage.json"
    current = load_json(current_path)
    selected_release = program or current.get("program") or current.get("current_stage_id")
    if not selected_release:
        fail("current_stage.json has no program/current_stage_id")
    if current.get("current_stage_id") != selected_release or current.get("program") != selected_release:
        fail(
            "current_stage.json points to "
            f"{current.get('current_stage_id')}/{current.get('program')}, not {selected_release}"
        )
    tp_program = selected_tp_program(root, current, selected_release)

    expected_target_dir = target_dir or str(PurePosixPath(str(current.get("controller_target", ""))).parent)
    if not expected_target_dir or expected_target_dir == ".":
        fail("current_stage.json has no controller_target and no --target-dir was provided")
    expected_urp = f"{expected_target_dir}/{tp_program}.urp"
    expected_script = f"{expected_target_dir}/{tp_program}.script"
    if current.get("controller_target") and current.get("controller_target") != expected_urp:
        fail(f"controller_target is {current.get('controller_target')}, expected {expected_urp}")
    if current.get("controller_script") and current.get("controller_script") != expected_script:
        fail(f"controller_script is {current.get('controller_script')}, expected {expected_script}")
    if "controller_readback_verified" not in str(current.get("status", "")):
        fail(f"current status is not read-back verified: {current.get('status')}")

    evidence = current.get("evidence", {})
    suffix = selected_release.rsplit("_", 1)[-1]
    verified_key = f"{suffix}_controller_readback_verified"
    if verified_key in evidence and evidence.get(verified_key) is not True:
        fail(f"{verified_key} is not true")

    current_manifest_path = manifest_path_from_current(root, current, selected_release)
    if manifest_path is None:
        manifest_path = current_manifest_path
        if not manifest_path.exists():
            manifest_path = retained_manifest_path(root, selected_release)
    else:
        manifest_path = manifest_path.resolve()
        expected_current = current_manifest_path.resolve()
        if expected_current != manifest_path:
            fail(
                "explicit transaction manifest does not match the promoted current-stage pointer: "
                f"{manifest_path} != {expected_current}"
            )
    manifest = load_json(manifest_path)
    if manifest.get("status") != "controller read-back verified":
        fail(f"manifest status is {manifest.get('status')}")

    delivery_manifest = manifest
    if manifest.get("schema") == "step5d.autotune.controller-readback/v3":
        if (
            manifest.get("verified") is not True
            or manifest.get("program") != tp_program
            or manifest.get("controller_target") != expected_urp
            or manifest.get("triplet_sha256") != current_sha(current)
        ):
            fail("canonical controller read-back identity differs")
        raw_relative = manifest.get("fresh_readback_source")
        raw_expected_sha = manifest.get("fresh_readback_manifest_sha256")
        if not isinstance(raw_relative, str) or not raw_relative:
            fail("canonical controller read-back lacks fresh source")
        raw_path = root / raw_relative
        if raw_path.is_symlink() or not raw_path.is_file():
            fail("canonical controller read-back fresh source is missing")
        try:
            raw_path.resolve().relative_to(root.resolve())
        except ValueError:
            fail("canonical controller read-back fresh source escapes root")
        raw_actual_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if raw_actual_sha != raw_expected_sha:
            fail("canonical controller read-back fresh source digest differs")
        delivery_manifest = load_json(raw_path)
        if delivery_manifest.get("status") != "controller read-back verified":
            fail("fresh controller read-back status differs")

    delivery_mode = delivery_manifest.get("delivery_mode")
    if delivery_mode not in ALLOWED_DELIVERY_MODES:
        fail(f"manifest delivery_mode is not recognized: {delivery_mode}")
    if delivery_mode == "content_addressed_reuse":
        if delivery_manifest.get("fresh_controller_sha_verified") is not True:
            fail("content_addressed_reuse manifest lacks fresh_controller_sha_verified=true")
        if not delivery_manifest.get("fresh_controller_checked_at"):
            fail("content_addressed_reuse manifest lacks fresh_controller_checked_at")
        if not delivery_manifest.get("skip_basis_manifest"):
            fail("content_addressed_reuse manifest lacks skip_basis_manifest")
        if delivery_manifest.get("readback_source") != "prior_full_readback":
            fail("content_addressed_reuse manifest must use readback_source=prior_full_readback")

    validation = delivery_manifest.get("validation", {})
    if validation.get("program") != tp_program:
        fail(f"manifest program is {validation.get('program')}, expected {tp_program}")
    if validation.get("target_dir") != expected_target_dir:
        fail(f"manifest target_dir is {validation.get('target_dir')}, expected {expected_target_dir}")
    if validation.get("script_node_path") != expected_script:
        fail(f"manifest script_node_path is {validation.get('script_node_path')}")
    expected_install = expected_installation_relative_path(expected_target_dir)
    if validation.get("installation_relative_path") not in (None, expected_install):
        fail(
            "manifest installation_relative_path is "
            f"{validation.get('installation_relative_path')}, expected {expected_install}"
        )

    expected_sha = current_sha(current)
    manifest_sha = delivery_manifest.get("sha256", {})
    validation_keys = {
        ".script": "script_sha256",
        ".txt": "txt_sha256",
        ".urp": "urp_sha256",
    }
    for ext, validation_key in validation_keys.items():
        expected = expected_sha.get(ext) or validation.get(validation_key)
        if not expected:
            fail(f"expected sha256 for {ext} is missing")
        if validation.get(validation_key) != expected:
            fail(f"manifest validation {validation_key} does not match expected sha256")
        for section in ("local", "controller", "readback"):
            if manifest_sha.get(section, {}).get(ext) != expected:
                fail(f"manifest sha256 {section} {ext} does not match expected sha256")

    return {
        "ok": True,
        "program": selected_release,
        "tp_program": tp_program,
        "target_dir": expected_target_dir,
        "manifest": (
            str(manifest_path.relative_to(root))
            if manifest_path.is_relative_to(root)
            else str(manifest_path)
        ),
        "delivery_mode": delivery_mode or "legacy_full_upload_readback",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--program", default=None)
    parser.add_argument("--target-dir", default=None)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = verify(args.root, args.program, args.target_dir, args.manifest)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"[operator] read-back gate passed for {result['program']}: {result['manifest']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(24)
