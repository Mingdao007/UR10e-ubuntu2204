#!/usr/bin/env python3
"""Upload a TP-openable UR package triplet and verify controller read-back.

This tool only copies files with SSH/SCP and verifies the fetched-back bytes.
It does not send URScript, load a program, start a program, or open the live
bridge.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterator

from resolve_step4e_route import load_routes as load_step4e_routes


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PROGRAM_DIR = EXPERIMENT_ROOT / "programs"
RUN_ROOT = EXPERIMENT_ROOT / "runs"
DEFAULT_CONTROLLER = "root@192.168.1.18"
EXTENSIONS = (".script", ".txt", ".urp")
LOCAL_CANDIDATE_MARKER = ".local_tp_candidate.json"
INACTIVE_PRELIVE_DELIVERY_PROGRAMS = frozenset(
    {
        "step5d_strict_rnn_ablation_v30",
        "step5d_strict_rnn_ablation_v31",
        "step5d_strict_rnn_no_contact_p0_v8",
    }
)


def die(message: str) -> None:
    raise RuntimeError(message)


def normalize_program(value: str) -> str:
    name = Path(value).name
    for ext in EXTENSIONS:
        if name.endswith(ext):
            name = name[: -len(ext)]
    if not name or name in {".", ".."} or "/" in name:
        die(f"invalid program basename: {value!r}")
    return name


def normalize_target_dir(value: str) -> str:
    target = "/" + value.strip().strip("/")
    if target in {"", "/"}:
        die("target directory must be an absolute /programs/... path")
    if not target.startswith("/programs/"):
        die(f"refusing controller target outside /programs: {target}")
    return target


def load_json_if_present(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"invalid JSON in {path}: {exc}")


def controller_dir_from_target(controller_target: str) -> str:
    target = "/" + str(controller_target).strip().strip("/")
    if not target.startswith("/programs/") or not target.endswith(".urp"):
        die(f"invalid controller target in table: {controller_target}")
    return normalize_target_dir(str(PurePosixPath(target).parent))


def _target_resolution(
    *,
    target: str | None,
    target_dir: str | None,
    row_id: str,
    source: str,
) -> dict | None:
    if target:
        resolved_dir = controller_dir_from_target(target)
        resolved_target = "/" + str(target).strip().strip("/")
    elif target_dir:
        resolved_dir = normalize_target_dir(target_dir)
        resolved_target = ""
    else:
        return None
    return {
        "row_id": row_id,
        "source": source,
        "controller_dir": resolved_dir,
        "controller_target": resolved_target,
    }


def resolve_table_target(
    program: str,
    *,
    root: Path = EXPERIMENT_ROOT,
    required: bool = True,
    local_dir: Path | None = None,
) -> dict | None:
    step4e_table = root / "config" / "step4e_stage_table.json"
    if step4e_table.is_file():
        routes, _ = load_step4e_routes(step4e_table)
        for route in routes.values():
            if route["program_basename"] == program:
                return _target_resolution(
                    target=route["controller_urp"],
                    target_dir=None,
                    row_id=route["version"],
                    source=(
                        "config/step4e_stage_table.json"
                        f"#route_families[version={route['version']}]"
                    ),
                )
    current = load_json_if_present(root / "config" / "current_stage.json")
    table = load_json_if_present(root / "config" / "step5_stage_table.json")

    current_program = str(current.get("program") or current.get("current_stage_id") or "")
    if current_program == program:
        resolution = _target_resolution(
            target=current.get("controller_target"),
            target_dir=None,
            row_id=program,
            source="config/current_stage.json",
        )
        if resolution is not None:
            return resolution

    for capture_key in ("no_contact_p0_v8_capture", "no_contact_p0_capture"):
        capture = current.get("bridge_trigger", {}).get(capture_key, {})
        if capture.get("profile") == program:
            resolution = _target_resolution(
                target=capture.get("controller_target") or capture.get("planned_controller_target"),
                target_dir=None,
                row_id=program,
                source=f"config/current_stage.json#bridge_trigger.{capture_key}",
            )
            if resolution is not None:
                return resolution

    for row in table.get("stages", []):
        row_id = str(row.get("id") or "")
        sections = [
            row.get("package_delivery", {}),
            row.get("local_delivery_evidence", {}),
            row.get("current_binding", {}),
            row.get("operator_lifecycle", {}),
        ]
        row_programs = {
            row_id,
            *(str(section.get("program_basename") or section.get("program") or "") for section in sections),
        }
        if program not in row_programs:
            continue
        for section in sections:
            resolution = _target_resolution(
                target=(
                    section.get("controller_target")
                    or section.get("planned_controller_target")
                    or section.get("expected_program")
                ),
                target_dir=section.get("controller_dir"),
                row_id=row_id,
                source=f"config/step5_stage_table.json#stages[id={row_id}]",
            )
            if resolution is not None:
                return resolution
    if local_dir is not None and program in INACTIVE_PRELIVE_DELIVERY_PROGRAMS:
        marker = load_local_candidate_marker(local_dir, program)
        if marker is not None and marker.get("program") == program:
            resolution = _target_resolution(
                target=marker.get("controller_urp"),
                target_dir=marker.get("target_dir"),
                row_id=program,
                source=f"{local_dir}/.{program}.local_candidate.json",
            )
            if resolution is not None:
                return resolution
    if required:
        die(f"no table controller target found for {program}; use --override-table with --override-reason only for audited recovery")
    return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def triplet(local_dir: Path, program: str) -> dict[str, Path]:
    files = {ext: local_dir / f"{program}{ext}" for ext in EXTENSIONS}
    missing = [str(path) for path in files.values() if not path.is_file()]
    if missing:
        die(f"missing local package file(s): {missing}")
    return files


def load_local_candidate_marker(local_dir: Path, program: str | None = None) -> dict | None:
    marker_path = (
        local_dir / f".{program}.local_candidate.json"
        if program and (local_dir / f".{program}.local_candidate.json").is_file()
        else local_dir / LOCAL_CANDIDATE_MARKER
    )
    if not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"invalid local candidate marker {marker_path}: {exc}")
    if marker.get("local_only") is not True and marker.get("controller_readback_verified") is not True:
        die(f"candidate marker has neither staged-local nor verified-readback state: {marker_path}")
    return marker


def promote_local_candidate_marker_after_readback(
    local_dir: Path,
    program: str,
    marker: dict,
    *,
    controller: str,
    target_dir: str,
    readback_dir: Path,
    delivery_mode: str,
) -> Path:
    """Persist the successful upload/read-back state beside the exact triplet."""

    marker_path = (
        local_dir / f".{program}.local_candidate.json"
        if (local_dir / f".{program}.local_candidate.json").is_file()
        else local_dir / LOCAL_CANDIDATE_MARKER
    )
    manifest_path = readback_dir / "manifest.json"
    try:
        manifest_ref = str(manifest_path.resolve().relative_to(EXPERIMENT_ROOT.resolve()))
    except ValueError:
        manifest_ref = str(manifest_path.resolve())
    promoted = dict(marker)
    promoted.update(
        {
            "status": "controller read-back verified",
            "local_only": False,
            "not_delivered": False,
            "controller": controller,
            "controller_target": str(PurePosixPath(target_dir) / f"{program}.urp"),
            "controller_readback_verified": True,
            "controller_readback_manifest": manifest_ref,
            "delivery_mode": delivery_mode,
            "safety_boundary": [
                "controller package upload and fresh read-back only",
                "not current_stage",
                "no live bridge or TP Play performed by delivery",
            ],
        }
    )
    marker_path.write_text(json.dumps(promoted, indent=2) + "\n", encoding="utf-8")
    return marker_path


def enforce_offline_candidate_delivery_block(
    program: str,
    *,
    root: Path = EXPERIMENT_ROOT,
) -> dict | None:
    """Return the exact inactive-delivery policy or reject non-allowlisted candidates."""

    table = load_json_if_present(root / "config" / "step5_stage_table.json")
    row = next((item for item in table.get("stages", []) if item.get("id") == program), None)
    if not isinstance(row, dict):
        return None
    delivery = row.get("package_delivery") or {}
    if delivery.get("status") in {
        "local_offline_candidate_only",
        "controller_readback_verified_inactive",
    }:
        if program not in INACTIVE_PRELIVE_DELIVERY_PROGRAMS:
            die(f"refusing controller delivery for inactive offline candidate {program}")
        if program == "step5d_strict_rnn_ablation_v30" and delivery.get(
            "delivery_preparation_allowed_before_p0_v8"
        ) is not True:
            die("v30 inactive package delivery preparation is not enabled by the stage table")
        if program == "step5d_strict_rnn_no_contact_p0_v8" and row.get("gate_for") != (
            "step5d_strict_rnn_ablation_v30"
        ):
            die("P0 v8 inactive package is not bound as the v30 pre-live gate")
        return {
            "program": program,
            "status": "inactive_prelive_delivery_preparation",
            "stage_row": row,
            "package_delivery": delivery,
            "promotion_performed": False,
            "program_start_performed": False,
            "bridge_start_performed": False,
        }
    return None


def validate_inactive_candidate_delivery_binding(
    policy: dict,
    marker: dict,
    *,
    program: str,
    target_dir: str,
    local_sha: dict[str, str],
    root: Path = EXPERIMENT_ROOT,
) -> dict:
    """Bind an inactive upload/read-back preparation to table, marker, bytes, and target."""

    row = policy["stage_row"]
    delivery = policy["package_delivery"]
    if delivery.get("program_basename") != program:
        die("inactive candidate stage-table program basename mismatch")
    if delivery.get("sha256") != local_sha:
        die("inactive candidate stage-table sha256 does not match current package")
    if delivery.get("semantic_fingerprint") != marker.get("semantic_fingerprint"):
        die("inactive candidate marker/stage-table semantic fingerprint mismatch")
    marker_target = str(marker.get("controller_urp") or "")
    expected_target = str(PurePosixPath(target_dir) / f"{program}.urp")
    if marker_target != expected_target:
        die(f"inactive candidate marker controller_urp is {marker_target!r}, expected {expected_target!r}")
    planned_target = str(delivery.get("planned_controller_target") or "")
    if planned_target and planned_target != expected_target:
        die("inactive candidate stage-table planned controller target mismatch")
    current = load_json_if_present(root / "config" / "current_stage.json")
    if program in {
        str(current.get("program") or ""),
        str(current.get("current_stage_id") or ""),
    }:
        die("inactive candidate delivery preparation cannot target the current program")
    current_binding = row.get("current_binding") or {}
    if current_binding.get("is_current") is True:
        die("inactive candidate stage-table current binding must remain false")
    return {
        "policy": "manifest_bound_inactive_prelive_delivery_v1",
        "program": program,
        "stage_status": delivery.get("status"),
        "semantic_fingerprint": marker.get("semantic_fingerprint"),
        "package_sha256": local_sha,
        "controller_target": expected_target,
        "promotion_performed": False,
        "program_start_performed": False,
        "bridge_start_performed": False,
    }


def validate_local_candidate_marker(
    marker: dict,
    *,
    files: dict[str, Path],
    program: str | None = None,
    target_dir: str,
    local_sha: dict[str, str],
) -> None:
    if marker.get("program") != program:
        die(f"local candidate marker program is {marker.get('program')}, expected {program}")
    if marker.get("target_dir") != target_dir:
        die(f"local candidate marker target_dir is {marker.get('target_dir')}, expected {target_dir}")
    marker_sha = marker.get("sha256", {})
    missing = [ext for ext in EXTENSIONS if marker_sha.get(ext) != local_sha[ext]]
    if missing:
        die(f"local candidate marker sha256 does not match current files for: {missing}")
    for ext in EXTENSIONS:
        expected_name = f"{program}{ext}"
        if files[ext].name != expected_name:
            die(f"local candidate file name mismatch for {ext}: {files[ext].name}")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def extract_stamp(script: str) -> str:
    m = re.search(r"^# VERSION:\s*(\S+)\s*$", script, flags=re.M)
    if not m:
        die("script does not contain a '# VERSION:' stamp")
    return m.group(1)


def parse_urp(path: Path) -> tuple[ET.Element, str]:
    try:
        xml = gzip.decompress(path.read_bytes()).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        die(f"{path} is not a readable gzip-compressed .urp: {exc}")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        die(f"{path} did not parse as URP XML: {exc}")
    return root, xml


def find_script_node(root: ET.Element) -> tuple[str, str]:
    cached = None
    script_file = None
    for node in root.iter():
        if node.tag == "cachedContents":
            cached = node.text or ""
        elif node.tag == "file" and node.attrib.get("resolves-to") == "file":
            script_file = (node.text or "").strip()
    if cached is None:
        die("URP XML has no Script cachedContents node")
    if not script_file:
        die("URP XML has no Script file path node")
    return html.unescape(cached), script_file


def installation_relative_path(controller_dir: str) -> str:
    path = PurePosixPath(controller_dir)
    try:
        programs_idx = path.parts.index("programs")
    except ValueError as exc:
        raise ValueError(f"controller_dir must be under /programs: {controller_dir}") from exc
    parent_levels = len(path.parts) - programs_idx - 1
    return "/".join([".."] * parent_levels + ["default"])


def validate_package(
    files: dict[str, Path],
    program: str,
    target_dir: str,
    *,
    require_exact_cached_script: bool,
) -> dict[str, str]:
    script = read_text(files[".script"])
    txt = read_text(files[".txt"])
    stamp = extract_stamp(script)
    root, xml = parse_urp(files[".urp"])
    cached_script, script_node_path = find_script_node(root)
    expected_script_path = str(PurePosixPath(target_dir) / f"{program}.script")
    expected_installation_relative_path = installation_relative_path(target_dir)

    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "URProgram name": root.attrib.get("name") == program,
        "controller directory": root.attrib.get("directory") == target_dir,
        "installationRelativePath": root.attrib.get("installationRelativePath") == expected_installation_relative_path,
        "Script-node path": script_node_path == expected_script_path,
        "cachedContents stamp": stamp in cached_script,
        "cachedContents program": f"def codex_{program}" in cached_script
        if program in {"step4e_seed_normal_loop_v29", "step4e_seed_normal_loop_v30", "step4e_seed_normal_loop_v31"}
        else True,
        "cachedContents exact script": cached_script == script if require_exact_cached_script else True,
    }
    if program in {"step4e_seed_normal_loop_v29", "step4e_seed_normal_loop_v30", "step4e_seed_normal_loop_v31"}:
        version = program.rsplit("_", 1)[-1]
        force_norm_guard = "60.0" if program == "step4e_seed_normal_loop_v31" else "50.0"
        checks.update(
            {
                f"{version} function": f"codex_step4e_seed_normal_loop_{version}" in script,
                "first contact z": "local first_contact_z_m = 0.008044839" in script,
                "near threshold margin": "local first_near_start_z_m = first_contact_z_m + 0.020" in script,
                "near descent speed": "40.000, -0.015, -0.0025)" in script,
                "stage25.2 linear zero settle": "codex_wait_for_stage_linear_zero(25.2, 1.000)" in script,
                "lift 20mm": "p_lift[2] + 0.020" in script,
                "stage25.3 line-entry gate": "line-entry-gate release" in script
                and "local line_entry_required_s = 0.100" in script
                and "local line_entry_timeout_s = 1.000" in script
                and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" not in script,
                "raw normal guard": "codex_abs(normal_force) > 50.0" in script,
                "force norm guard": f"force_norm > {force_norm_guard}" in script,
                "torque norm guard": "torque_norm > 3.0" in script,
                f"URP cached {version} stamp": stamp in xml
                and f"STEP4E_SEED_NORMAL_LOOP_{version.upper()}" in xml,
            }
        )
    if program == "step4e_seed_normal_loop_v30":
        checks.update(
            {
                "v30 filtered live normal control": "filtered-live-normal" in script
                and "step4e-normal-follow-mode=filtered_live" in script
                and "keeps 25.2 and 25.3 on locked-normal behavior" in script,
            }
        )
    if program == "step4e_seed_normal_loop_v31":
        checks.update(
            {
                "v31 simple alpha normal control": "simple-alpha-normal-follow" in script
                and "step4e-normal-follow-mode=filtered_live" in script
                and "step4e-normal-filter-alpha=0.35" in script
                and "step4e-normal-min-force-n=2.0" in script
                and "direct alpha EMA during 25.0 line control" in script
                and "no slew-rate, latch-angle, or candidate-angle gate" in script
                and "keeps 25.2 and 25.3 on locked-normal behavior" in script,
            }
        )
    if program in {"step4f_cycloid_seed_normal_v1", "step4g_eight_seed_normal_v1"}:
        path_shape = "cycloid" if program.startswith("step4f_") else "eight"
        function_prefix = "step4f" if program.startswith("step4f_") else "step4g"
        checks.update(
            {
                f"{function_prefix} function": f"def codex_{program}()" in script
                and f"codex_{function_prefix}_down_search" in script,
                "path shape bridge contract": f"--step4e-path-shape {path_shape}" in script
                and f"step4e-version={function_prefix}_v1" in script,
                "60s path-shape runtime": "local line_runtime_limit_s = 65.000" in script
                and "local line_success_progress_m = 60.000000000" in script
                and "for 60 s" in script + txt,
                "v31 force/normal/orientation scaffold": "first-contact normal latch" in script
                and "25.2 attitude correction" in script
                and "25.3 line-entry gate" in script
                and "normal projection and force-loop composition" in script
                and "force_norm > 60.0" in script
                and "torque_norm > 3.0" in script,
                "path-shape semantics": path_shape in script
                and ("paper-derived cycloid XY reference" in script + txt if path_shape == "cycloid" else "paper-derived 8-shaped XY reference" in script + txt),
            }
        )
    if program in {
        "step5b_contact_cycloid_baseline_v1",
        "step5b_contact_cycloid_baseline_v2",
        "step5b_contact_cycloid_baseline_v3",
    }:
        is_v2 = program.endswith("_v2")
        is_v3 = program.endswith("_v3")
        bridge_version = "step5b_v3" if is_v3 else "step5b_v2" if is_v2 else "step5b_v1"
        target_force = "12.0" if is_v3 else "15.0" if is_v2 else "5.0"
        filter_alpha = "0.55" if is_v3 else "0.70" if is_v2 else "0.35"
        checks.update(
            {
                "step5b function": f"def codex_{program}()" in script
                and "codex_step5b_down_search" in script,
                "step5b bridge contract": f"step4e-version={bridge_version}" in script
                and f"--step4e-version {bridge_version}" in txt
                and "--step4e-path-shape cycloid" in txt,
                "step5b filter contract": f"step4e-normal-filter-alpha={filter_alpha}" in script
                and f"--step4e-normal-filter-alpha {filter_alpha}" in txt,
                "step5 table source": "STEP5_FLOW.md" in script
                and "STEP5_TABLE_SOURCE: config/step5_stage_table.json" in script
                and "step5_contact_cycloid_baseline_v1" in script + txt,
                "executor and guard only": "TP_ROLE: executor_and_guard_only" in script
                and "Stage 25.0 consumes bridge command registers 37..44 only" in txt,
                "60s contact runtime": "local line_runtime_limit_s = 65.000" in script
                and "local line_success_progress_m = 60.000000000" in script,
                "v31 contact scaffold": "first-contact normal latch" in script
                and "25.3 line-entry gate" in script
                and "normal projection and force-loop composition" in script,
                "step5b v2 orientation skip gate": (not is_v2)
                or (
                    "local skip_lift_attitude = 0" in script
                    and "write_output_float_register(35, 25.15)" in script
                    and "local orientation_skip_error_rad = 0.069813" in script
                    and "skip_lift_attitude == 0" in script
                ),
                "step5b v3 no lift/attitude/second search": (not is_v3)
                or (
                    "write_output_float_register(35, 25.15)" in script
                    and "write_output_float_register(35, 25.1)" not in script
                    and "write_output_float_register(35, 25.2)" not in script
                    and "codex_step5b_down_search(24.3, 24.4" not in script
                    and "local skip_lift_attitude = 0" not in script
                ),
                "fast non-contact movel": "movel(entry_xy_pose, a=0.060, v=0.040, r=0.0)" in script
                and (is_v3 or "movel(lift_pose, a=0.060, v=0.040, r=0.0)" in script)
                and "local short_retract_speed_m_s = 0.040" in script
                and "movel(short_retract_pose, a=0.060, v=short_retract_speed_m_s, r=0.0)" in script
                and (
                    "Non-contact movel speed: entry/retract 0.040 m/s" in txt
                    if is_v3
                    else "Non-contact movel speed: entry/lift/retract 0.040 m/s" in txt
                ),
                "raw contact guards": "codex_abs(normal_force) > 50.0" in script
                and "force_norm > 60.0" in script
                and "torque_norm > 3.0" in script,
                "step5b force target": f"--target-force-n {target_force}" in txt,
                "no stale step4 route": "step4f_cycloid_seed_normal_v1" not in script
                and "step4g_eight_seed_normal_v1" not in script,
            }
        )
    if program in {"step5c_speedj_dryrun_v1", "step5c_joint_rnn_cycloid_v1"}:
        is_dry = program == "step5c_speedj_dryrun_v1"
        bridge_version = "step5c_speedj_dryrun_v1" if is_dry else "step5c_joint_rnn_cycloid_v1"
        stage_id = bridge_version
        checks.update(
            {
                "step5c function": f"def codex_{program}()" in script,
                "step5c bridge contract": "quarantine" in (script + txt).lower(),
                "step5c table source": "STEP5_FLOW.md" in script
                and "STEP5_TABLE_SOURCE: config/step5_stage_table.json" in script
                and stage_id in script + txt,
                "joint register contract": True,
                "speedj active command": True,
                "no cartesian active command": "speedl([cmd_vx" not in script
                and "speedl([cmd_qd0" not in script,
                "no stale step5b route": "step5b_contact_cycloid_baseline_v1" not in script + txt,
            }
        )
        if is_dry:
            checks.update(
                {
                    "step5c dry quarantine": "stop_only_quarantine" in script
                    and "wrong XY/Z direction" in txt,
                    "step5c dry no motion": "speedj(" not in script
                    and "speedl(" not in script
                    and "force_mode(" not in script
                    and "zero_ftsensor" not in script.replace("no zero_ftsensor()", ""),
                }
            )
        else:
            checks.update(
                {
                    "step5c contact quarantine": "stop_only_quarantine" in script
                    and "strict TASE RNN is blocked" in txt
                    and "previous misnamed" in txt,
                    "step5c contact no motion": "speedj(" not in script
                    and "speedl(" not in script
                    and "force_mode(" not in script
                    and "zero_ftsensor" not in script.replace("no zero_ftsensor()", ""),
                }
            )
    if program in {
        "step5d_strict_rnn_liveprep_v1",
        "step5d_strict_rnn_liveprep_v2",
        "step5d_strict_rnn_liveprep_v3",
        "step5d_strict_rnn_liveprep_v4",
        "step5d_strict_rnn_liveprep_v5",
        "step5d_strict_rnn_liveprep_v6",
        "step5d_strict_rnn_liveprep_v7",
    }:
        is_v3 = program.endswith("_v3")
        is_v4 = program.endswith("_v4")
        is_v5 = program.endswith("_v5")
        is_v6 = program.endswith("_v6")
        is_v7 = program.endswith("_v7")
        tolerant_contact_window = is_v4 or is_v5 or is_v6 or is_v7
        window_max_load = "40.000" if is_v6 else "15.000"
        window_max_force_norm = "45.000" if is_v6 else "25.000"
        raw_guard = "100.0" if (is_v3 or tolerant_contact_window) else "50.0"
        force_guard = "100.0" if (is_v3 or tolerant_contact_window) else "60.0"
        checks.update(
            {
                "step5d function": f"def codex_{program}()" in script
                and "codex_step5d_down_search" in script,
                "step5d bridge contract": f"step4e-version={program}" in script
                and f"--step4e-version {program}" in txt
                and "--step4e-path-shape cycloid" in txt,
                "step5d force contract": "--target-force-n 5.0" in txt,
                "step5 table source": "STEP5_FLOW.md" in script
                and "STEP5_TABLE_SOURCE: config/step5_stage_table.json" in script
                and program in script + txt,
                "joint executor and guard only": "joint_executor_and_guard_only" in script
                and "37..42 as qd0..qd5 rad/s" in txt,
                "speedj line control": "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]" in script
                and "local qdot_cap_rad_s = 0.300" in script,
                "orientation skip gate": (program.endswith("_v1"))
                or (
                    "local skip_lift_attitude = 0" in script
                    and (
                        "local orientation_skip_error_rad = 0.052360" in script
                        or "local orientation_skip_error_rad = 0.069813" in script
                    )
                    and "skip_lift_attitude == 0" in script
                ),
                "force-settle entry gate": (not (is_v3 or tolerant_contact_window))
                or (
                    (
                        (
                            is_v3
                            and "local line_entry_force_error_abs_n = 3.000" in script
                            and "local line_entry_force_norm_max_n = 12.000" in script
                        )
                        or (
                            tolerant_contact_window
                            and "local line_entry_normal_load_min_n = 2.000" in script
                            and f"local line_entry_normal_load_max_n = {window_max_load}" in script
                            and f"local line_entry_force_norm_max_n = {window_max_force_norm}" in script
                            and "local line_entry_required_s = 0.050" in script
                            and "local line_entry_timeout_s = 10.000" in script
                        )
                    )
                    and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" in script
                ),
                "line no cartesian speedl": "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy" not in script,
                "v31 contact scaffold": "first-contact normal latch" in script
                and "25.2 attitude correction" in script
                and (
                    "25.3 force-settle line-entry gate" in script
                    if is_v3
                    else "25.3 tolerant contact-window line-entry gate" in script
                    if tolerant_contact_window
                    else "25.3 line-entry gate" in script
                ),
                "raw contact guards": f"codex_abs(normal_force) > {raw_guard}" in script
                and f"force_norm > {force_guard}" in script
                and "torque_norm > 3.0" in script,
                "not quarantine": "stop_only_quarantine" not in script + txt,
                "no stale step5bc route": "step5b_contact_cycloid_baseline_v1" not in script + txt
                and "step5c_joint_rnn_cycloid_v1" not in script + txt,
            }
        )
        if program.endswith("_v2"):
            checks["no stale step5d v1 route"] = "step5d_strict_rnn_liveprep_v1" not in script + txt
        if program.endswith("_v3"):
            checks["no stale step5d v1/v2 route"] = "step5d_strict_rnn_liveprep_v1" not in script + txt and "step5d_strict_rnn_liveprep_v2" not in script + txt
        if program.endswith("_v4"):
            checks["no stale step5d v1/v2/v3 route"] = (
                "step5d_strict_rnn_liveprep_v1" not in script + txt
                and "step5d_strict_rnn_liveprep_v2" not in script + txt
                and "step5d_strict_rnn_liveprep_v3" not in script + txt
            )
        if program.endswith("_v5") or program.endswith("_v6") or program.endswith("_v7"):
            checks["force-frame semantic contract"] = (
                "UR_FORCE_FRAME_CONTRACT.md" in script + txt
                and "reaction normal for load" in script + txt
                and "approach normal for posture" in script + txt
            )
        if program.endswith("_v5"):
            checks["no stale step5d v1/v2/v3/v4 route"] = (
                "step5d_strict_rnn_liveprep_v1" not in script + txt
                and "step5d_strict_rnn_liveprep_v2" not in script + txt
                and "step5d_strict_rnn_liveprep_v3" not in script + txt
                and "step5d_strict_rnn_liveprep_v4" not in script + txt
            )
        if program.endswith("_v6"):
            checks["no stale step5d v1/v2/v3/v4/v5 route"] = (
                "step5d_strict_rnn_liveprep_v1" not in script + txt
                and "step5d_strict_rnn_liveprep_v2" not in script + txt
                and "step5d_strict_rnn_liveprep_v3" not in script + txt
                and "step5d_strict_rnn_liveprep_v4" not in script + txt
                and "step5d_strict_rnn_liveprep_v5" not in script + txt
            )
        if program.endswith("_v7"):
            checks["second contact slow search"] = "codex_step5d_down_search(24.3, 24.4, 0.035, 0.000, 45.000, -0.0025, -0.0025)" in script
            checks["no stale step5d v1/v2/v3/v4/v5/v6 route"] = (
                "step5d_strict_rnn_liveprep_v1" not in script + txt
                and "step5d_strict_rnn_liveprep_v2" not in script + txt
                and "step5d_strict_rnn_liveprep_v3" not in script + txt
                and "step5d_strict_rnn_liveprep_v4" not in script + txt
                and "step5d_strict_rnn_liveprep_v5" not in script + txt
                and "step5d_strict_rnn_liveprep_v6" not in script + txt
            )
    if program in {
        "step5d_strict_rnn_liveprep_v8",
        "step5d_strict_rnn_liveprep_v9",
        "step5d_strict_rnn_liveprep_v10",
        "step5d_strict_rnn_liveprep_v11",
        "step5d_strict_rnn_liveprep_v12",
        "step5d_strict_rnn_liveprep_v13",
        "step5d_strict_rnn_liveprep_v14",
        "step5d_strict_rnn_liveprep_v15",
        "step5d_strict_rnn_liveprep_v15a",
        "step5d_strict_rnn_liveprep_v16",
        "step5d_strict_rnn_liveprep_v17",
        "step5d_strict_rnn_liveprep_v18",
        "step5d_strict_rnn_liveprep_v19",
        "step5d_strict_rnn_liveprep_v20",
        "step5d_strict_rnn_liveprep_v21",
        "step5d_strict_rnn_liveprep_v22",
        "step5d_strict_rnn_liveprep_v23",
        "step5d_strict_rnn_liveprep_v24",
    }:
        is_v9 = program.endswith("_v9")
        is_v10 = program.endswith("_v10")
        is_v11 = program.endswith("_v11")
        is_v12 = program.endswith("_v12")
        is_v13 = program.endswith("_v13")
        is_v14 = program.endswith("_v14")
        is_v15 = program.endswith("_v15")
        is_v15a = program.endswith("_v15a")
        is_v16 = program.endswith("_v16")
        is_v17 = program.endswith("_v17")
        is_v18 = program.endswith("_v18")
        is_v19 = program.endswith("_v19")
        is_v20 = program.endswith("_v20")
        is_v21 = program.endswith("_v21")
        is_v22 = program.endswith("_v22")
        is_v23 = program.endswith("_v23")
        is_v24 = program.endswith("_v24")
        guarded_v9_plus = (
            is_v9
            or is_v10
            or is_v11
            or is_v12
            or is_v13
            or is_v14
            or is_v15
            or is_v15a
            or is_v16
            or is_v17
            or is_v18
            or is_v19
            or is_v20
            or is_v21
            or is_v22
            or is_v23
            or is_v24
        )
        recovery_min = "0.000" if guarded_v9_plus else "0.500"
        recovery_max = "20.000" if is_v24 else "40.000"
        recovery_force_stop = "25.000" if is_v24 else "100.000" if is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 else "25.000" if is_v14 or is_v15 or is_v15a or is_v16 or is_v17 else "100.000"
        settle_label = (
            "Stage 25.3 consumes 37..39 as Cartesian admittance settle vx/vy/vz"
            if is_v10
            else "Stage 25.3 consumes 37..39 as Cartesian deadband-acquire vx/vy/vz"
            if is_v11 or is_v12 or is_v13 or is_v14 or is_v15 or is_v15a or is_v16 or is_v17 or is_v18 or is_v19 or is_v20
            else "Stage 25.3 consumes 37..39 as Cartesian force-PID settle vx/vy/vz"
        )
        if is_v21 or is_v22 or is_v23 or is_v24:
            settle_label = (
                "Stage 25.3 consumes 37..39 as Cartesian deadband-acquire vx/vy/vz, "
                f"plus {'v24' if is_v24 else 'v23' if is_v23 else 'v22' if is_v22 else 'v21'} preload overrides in 40/41/42/44/46/47"
            )
        qdot_cap = "0.050" if is_v12 or is_v13 or is_v14 or is_v15 or is_v15a or is_v16 or is_v17 or is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 or is_v24 else "0.300"
        min_load = "8.000" if (is_v17 or is_v18) else "5.000" if is_v16 else "2.000" if is_v11 or is_v12 or is_v13 or is_v14 or is_v15 or is_v15a else "3.000"
        if is_v19 or is_v20:
            min_load = "8.000"
        if is_v21 or is_v22 or is_v23 or is_v24:
            min_load = "7.500"
        max_load = "13.000" if is_v19 or is_v20 else "18.000" if (is_v17 or is_v18) else "20.000" if is_v16 else "15.000" if is_v11 or is_v12 or is_v13 or is_v14 or is_v15 or is_v15a else "8.000"
        if is_v21 or is_v22 or is_v23 or is_v24:
            max_load = "14.000"
        required_s = "0.100" if (is_v17 or is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 or is_v24) else "0.150" if is_v11 or is_v12 or is_v13 or is_v14 or is_v15 or is_v15a or is_v16 else "0.300" if is_v10 else "0.200"
        target_force = "12.0" if is_v16 or is_v17 or is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 or is_v24 else "5.0"
        force_settle_gate = (
            "local line_entry_default_normal_load_min_n = 7.500" in script
            and "local line_entry_default_normal_load_max_n = 14.000" in script
            and "local line_entry_default_force_norm_max_n = 25.000" in script
            and "local line_entry_default_required_s = 0.100" in script
            and "local line_entry_default_timeout_s = 10.000" in script
            and "local line_entry_param_valid_code = 521.000" in script
            and "read_input_float_register(40)" in script
            and "read_input_float_register(41)" in script
            and "read_input_float_register(42)" in script
            and "read_input_float_register(44)" in script
            and "read_input_float_register(46)" in script
            and "read_input_float_register(47)" in script
            and "local line_entry_recovery_normal_load_min_n = 0.000" in script
            and f"local line_entry_recovery_normal_load_max_n = {recovery_max}" in script
            and f"local line_entry_force_norm_stop_n = {recovery_force_stop}" in script
            and "local normal_load = target_force - force_error" in script
            and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" in script
        ) if is_v21 or is_v22 or is_v23 or is_v24 else (
            f"local line_entry_normal_load_min_n = {min_load}" in script
            and f"local line_entry_normal_load_max_n = {max_load}" in script
            and "local line_entry_force_norm_max_n = 25.000" in script
            and f"local line_entry_required_s = {required_s}" in script
            and "local line_entry_timeout_s = 10.000" in script
            and f"local line_entry_recovery_normal_load_min_n = {recovery_min}" in script
            and f"local line_entry_recovery_normal_load_max_n = {recovery_max}" in script
            and f"local line_entry_force_norm_stop_n = {recovery_force_stop}" in script
            and "local normal_load = target_force - force_error" in script
            and "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" in script
        )
        checks.update(
            {
                "step5d function": f"def codex_{program}()" in script
                and "codex_step5d_down_search" in script,
                "step5d bridge contract": f"step4e-version={program}" in script
                and f"--step4e-version {program}" in txt
                and "--step4e-path-shape cycloid" in txt,
                "step5d force contract": f"--target-force-n {target_force}" in txt,
                "step5 table source": "STEP5_FLOW.md" in script
                and "STEP5_TABLE_SOURCE: config/step5_stage_table.json" in script
                and program in script + txt,
                "joint executor and guard only": "joint_executor_and_guard_only" in script
                and "37..42 as qd0..qd5 rad/s" in txt,
                "stage split register contract": settle_label in script,
                "speedj line control": "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]" in script
                and f"local qdot_cap_rad_s = {qdot_cap}" in script,
                "orientation skip gate": (
                    "local skip_lift_attitude = 0" not in script
                    and "write_output_float_register(35, 25.1)" not in script
                    and "write_output_float_register(35, 25.2)" not in script
                    if is_v16 or is_v17 or is_v18 or is_v19
                    or is_v20 or is_v21 or is_v22 or is_v23 or is_v24
                    else "local skip_lift_attitude = 0" in script
                    and "local orientation_skip_error_rad = 0.069813" in script
                    and "skip_lift_attitude == 0" in script
                ),
                "force settle gate": force_settle_gate,
                "second contact slow search": (
                    "codex_step5d_down_search(24.3, 24.4" not in script
                    if is_v16 or is_v17 or is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 or is_v24
                    else "codex_step5d_down_search(24.3, 24.4, 0.035, 0.000, 45.000, -0.0025, -0.0025)" in script
                ),
                "line no cartesian speedl": "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy" not in script,
                "force-frame semantic contract": "UR_FORCE_FRAME_CONTRACT.md" in script + txt
                and "reaction normal for load" in script + txt
                and "approach normal for posture" in script + txt,
                "raw contact guards": (
                    "codex_abs(normal_force) > 50.0" in script
                    and "force_norm > 60.0" in script
                    if is_v14 or is_v15 or is_v15a or is_v16 or is_v17
                    else "codex_abs(normal_force) > 25.0" in script
                    and "force_norm > 25.0" in script
                    if is_v24
                    else "codex_abs(normal_force) > 100.0" in script
                    and "force_norm > 100.0" in script
                )
                and ("torque_norm > 4.0" in script if is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 or is_v24 else "torque_norm > 3.0" in script),
                "not quarantine": "stop_only_quarantine" not in script + txt,
                "no stale step5bc route": "step5b_contact_cycloid_baseline_v1" not in script + txt
                and "step5c_joint_rnn_cycloid_v1" not in script + txt,
            }
        )
        if guarded_v9_plus:
            checks["low-load does not stop"] = "or normal_load < line_entry_recovery_normal_load_min_n" not in script
            checks["force envelope auto-home"] = "elif stop_reason == 17.0:\n    return True" in script
            stale_range_end = (
                24
                if is_v24
                else 23
                if is_v23
                else 22
                if is_v22
                else 21
                if is_v21
                else 20
                if is_v20
                else 19
                if is_v19
                else 18
                if is_v18
                else 17
                if is_v17
                else 16
                if is_v16
                else 16
                if is_v15a
                else 15
                if is_v15
                else 14
                if is_v14
                else 13
                if is_v13
                else 12
                if is_v12
                else 11
                if is_v11
                else 10
                if is_v10
                else 9
            )
            checks["no stale older step5d route"] = all(
                re.search(rf"step5d_strict_rnn_liveprep_v{idx}(?![A-Za-z0-9])", script + txt) is None
                for idx in range(1, stale_range_end)
            )
            if is_v10:
                checks["v10 settle velocity release gate"] = (
                    "local line_entry_settle_cmd_max_m_s = 0.001" in script
                    and "codex_abs(cmd_vx) <= line_entry_settle_cmd_max_m_s" in script
                    and "scalar admittance settle" in txt
                )
            if is_v11 or is_v12 or is_v13 or is_v14 or is_v15 or is_v15a or is_v16 or is_v17 or is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 or is_v24:
                checks["v11/v12/v13/v14/v15/v15a/v16/v17/v18/v19/v20/v21/v22/v23/v24 deadband acquire release gate"] = (
                    "local line_entry_settle_cmd_max_m_s" not in script
                    and "codex_abs(cmd_vx) <= line_entry_settle_cmd_max_m_s" not in script
                    and "deadband contact acquire" in txt
                    and ("7.5 N" in txt if (is_v21 or is_v22 or is_v23 or is_v24) else "8.0 N" in txt if (is_v17 or is_v18 or is_v19 or is_v20) else "5.0 N" in txt if is_v16 else "2.0 N" in txt)
                    and ("14.0 N" in txt if (is_v21 or is_v22 or is_v23 or is_v24) else "13.0 N" in txt if (is_v19 or is_v20) else "18.0 N" in txt if (is_v17 or is_v18) else "20.0 N" in txt if is_v16 else "15.0 N" in txt)
                )
            if is_v17 or is_v18 or is_v19 or is_v20 or is_v21 or is_v22 or is_v23 or is_v24:
                checks["v17/v18/v19/v20/v21/v22/v23/v24 raw sanity release note"] = (
                    ("7.0 N" in txt and "15.0 N" in txt)
                    if is_v21 or is_v22 or is_v23 or is_v24
                    else "7.5 N" in txt and ("14.0 N" in txt if (is_v19 or is_v20) else "19.0 N" in txt)
                )
            if is_v12:
                checks["v12 Stage25 guard note"] = (
                    "STAGE25_GUARD" in script
                    and "clears cmd_valid on lost contact" in script
                    and "actual TCP speed exceeds 0.050 m/s" in txt
                )
            if is_v13:
                checks["v13 contact safety hold/stop note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "cmd_valid=1 zero-qdot hold" in script + txt
                    and "predicted TCP speed" in script + txt
                    and "actual speed dwell" in script + txt
                    and "stop_request" in script + txt
                )
            if is_v14:
                checks["v14 contact safety hold/stop note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "cmd_valid=1 zero-qdot hold" in script + txt
                    and "first-sample actual speed dwell" in script + txt
                    and "predicted TCP speed" in script + txt
                    and "actual speed dwell" in script + txt
                    and "stop_request" in script + txt
                )
            if is_v15:
                checks["v15 permissive recovery contact safety note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "cmd_valid=1 zero-qdot hold" in script + txt
                    and "recoverable predicted-speed/contact uncertainty" in script + txt
                    and "cage margin" in script + txt
                    and "actual speed dwell" in script + txt
                    and "stop_request" in script + txt
                )
            if is_v15a:
                checks["v15a online cage bounded recovery note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "cmd_valid=1 zero-qdot hold" in script + txt
                    and "online broad AABB TCP cage" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "bounded" in script + txt
                    and "hold duty/event/consecutive exhaustion" in script + txt
                    and "stop_request" in script + txt
                )
            if is_v16:
                checks["v16 no-lift 12n online cage bounded recovery note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "cmd_valid=1 zero-qdot hold" in script + txt
                    and "online broad AABB TCP cage" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "bounded" in script + txt
                    and "hold duty/event/consecutive exhaustion" in script + txt
                    and "no lift" in txt
                    and "no second contact search" in txt
                    and "write_output_float_register(35, 25.1)" not in script
                    and "write_output_float_register(35, 25.2)" not in script
                    and "step5d_strict_rnn_liveprep_v15a" not in script + txt
                    and "stop_request" in script + txt
                )
            if is_v17:
                checks["v17 no-lift 12n online cage bounded recovery note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "cmd_valid=1 zero-qdot hold" in script + txt
                    and "online broad AABB TCP cage" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "bounded" in script + txt
                    and "hold duty/event/consecutive exhaustion" in script + txt
                    and "no lift" in txt
                    and "no second contact search" in txt
                    and "8.0 N" in txt
                    and "18.0 N" in txt
                    and "7.5 N" in txt
                    and "19.0 N" in txt
                    and "write_output_float_register(35, 25.1)" not in script
                    and "write_output_float_register(35, 25.2)" not in script
                    and "step5d_strict_rnn_liveprep_v16" not in script + txt
                    and "stop_request" in script + txt
                )
            if is_v18:
                checks["v18 cage-primary active reacquire note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "active_reacquire_solver" in script + txt
                    and "low-load or" in txt.lower()
                    and "no-contact" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "_step5d_active_reacquire_s" in txt
                    and "_step5d_no_contact_s" in txt
                    and "100 N raw-normal/force-norm and 4.0 Nm torque" in txt
                    and "local line_runtime_limit_s = 15.000" in script
                    and "local line_success_progress_m = 10.000000000" in script
                    and "step5d_strict_rnn_liveprep_v17" not in script + txt
                    and "stop_request" in script + txt
                )
            if is_v19:
                checks["v19 cage-primary active reacquire speed-cap note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "active_reacquire_solver" in script + txt
                    and "reacquire predicted-speed cap" in script + txt
                    and "low-load or" in txt.lower()
                    and "no-contact" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "_step5d_active_reacquire_s" in txt
                    and "_step5d_no_contact_s" in txt
                    and "100 N raw-normal/force-norm and 4.0 Nm torque" in txt
                    and "local line_runtime_limit_s = 15.000" in script
                    and "local line_success_progress_m = 10.000000000" in script
                    and "movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)" in script
                    and "40.000, -0.0225, -0.0025)" in script
                    and "step5d_strict_rnn_liveprep_v18" not in script + txt
                    and "stop_request" in script + txt
                )
            if is_v20:
                checks["v20 gravity-down cage-primary active reacquire speed-cap note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1" in script
                    and "config/step_pose_contract_table.json" in script + txt
                    and "local target_rx = 3.141592654" in script
                    and "local target_ry = 0.000000000" in script
                    and "local target_rz = 0.000000000" in script
                    and "TCP +Z targets base -Z" in script + txt
                    and "active_reacquire_solver" in script + txt
                    and "reacquire predicted-speed cap" in script + txt
                    and "normal_filter_source" in txt
                    and "low-load or" in txt.lower()
                    and "no-contact" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "_step5d_active_reacquire_s" in txt
                    and "_step5d_no_contact_s" in txt
                    and "_step5d_reacquire_speed_cap_*" in txt
                    and "100 N raw-normal/force-norm and 4.0 Nm torque" in txt
                    and "local line_runtime_limit_s = 15.000" in script
                    and "local line_success_progress_m = 10.000000000" in script
                    and "movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)" in script
                    and "40.000, -0.0225, -0.0025)" in script
                    and "step5d_strict_rnn_liveprep_v19" not in script + txt
                    and "stop_request" in script + txt
                )
            if is_v21:
                checks["v21 gravity-down preload-param cage-primary note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1" in script
                    and "config/step_pose_contract_table.json" in script + txt
                    and "local target_rx = 3.141592654" in script
                    and "local target_ry = 0.000000000" in script
                    and "local target_rz = 0.000000000" in script
                    and "TCP +Z targets base -Z" in script + txt
                    and "active_reacquire_solver" in script + txt
                    and "reacquire predicted-speed cap" in script + txt
                    and "normal_filter_source" in txt
                    and "low-load or" in txt.lower()
                    and "no-contact" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "_step5d_active_reacquire_s" in txt
                    and "_step5d_no_contact_s" in txt
                    and "_step5d_reacquire_speed_cap_*" in txt
                    and "100 N raw-normal/force-norm and 4.0 Nm torque" in txt
                    and "local line_runtime_limit_s = 15.000" in script
                    and "local line_success_progress_m = 10.000000000" in script
                    and "movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)" in script
                    and "40.000, -0.0225, -0.0025)" in script
                    and "step5d_strict_rnn_liveprep_v20" not in script + txt
                    and "stop_request" in script + txt
                    and "47 equals 521.0" in txt
                )
            if is_v22:
                checks["v22 gravity-down preload-param qdot-clear cage-primary note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1" in script
                    and "config/step_pose_contract_table.json" in script + txt
                    and "local target_rx = 3.141592654" in script
                    and "local target_ry = 0.000000000" in script
                    and "local target_rz = 0.000000000" in script
                    and "TCP +Z targets base -Z" in script + txt
                    and "active_reacquire_solver" in script + txt
                    and "reacquire predicted-speed cap" in script + txt
                    and "normal_filter_source" in txt
                    and "low-load or" in txt.lower()
                    and "no-contact" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "_step5d_active_reacquire_s" in txt
                    and "_step5d_no_contact_s" in txt
                    and "_step5d_reacquire_speed_cap_*" in txt
                    and "100 N raw-normal/force-norm and 4.0 Nm torque" in txt
                    and "local line_runtime_limit_s = 15.000" in script
                    and "local line_success_progress_m = 10.000000000" in script
                    and "movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)" in script
                    and "40.000, -0.0225, -0.0025)" in script
                    and "step5d_strict_rnn_liveprep_v21" not in script + txt
                    and "stop_request" in script + txt
                    and "write_output_float_register(35, 25.95)" in script
                    and "local qdot_clear_required_s = 0.006" in script
                    and "qdot_layout_ok == 0" in script
                    and "Stage 25.95 requires the bridge to clear registers 37..47" in txt
                    and "47 equals 521.0" in txt
                )
            if is_v23:
                checks["v23 gravity-down preload-param near-zero-qdot-clear normal-guard note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1" in script
                    and "config/step_pose_contract_table.json" in script + txt
                    and "local target_rx = 3.141592654" in script
                    and "local target_ry = 0.000000000" in script
                    and "local target_rz = 0.000000000" in script
                    and "TCP +Z targets base -Z" in script + txt
                    and "active_reacquire_solver" in script + txt
                    and "reacquire predicted-speed cap" in script + txt
                    and "no-contact" in script + txt
                    and "_step5d_tcp_cage_*" in txt
                    and "_step5d_active_reacquire_s" in txt
                    and "_step5d_no_contact_s" in txt
                    and "_step5d_reacquire_speed_cap_*" in txt
                    and "_step5d_rnn_raw_qd*" in txt
                    and "_step5d_post_rnn_normal_guard_action" in txt
                    and "post-RNN normal-direction guard" in script + txt
                    and "100 N raw-normal/force-norm and 4.0 Nm torque" in txt
                    and "local line_runtime_limit_s = 15.000" in script
                    and "local line_success_progress_m = 10.000000000" in script
                    and "movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)" in script
                    and "40.000, -0.0225, -0.0025)" in script
                    and "step5d_strict_rnn_liveprep_v22" not in script + txt
                    and "stop_request" in script + txt
                    and "write_output_float_register(35, 25.95)" in script
                    and "local qdot_clear_required_s = 0.006" in script
                    and "local qdot_clear_zero_tol_rad_s = 0.000500" in script
                    and "qdot_clear_cap_rad_s" not in script
                    and "qdot registers 37..42 near zero" in txt
                    and "47 equals 521.0" in txt
                )
            if is_v24:
                checks["v24 low-load-stop tracking-guard note"] = (
                    "STAGE25_CONTACT_SAFETY" in script
                    and "PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1" in script
                    and "config/step_pose_contract_table.json" in script + txt
                    and "local target_rx = 3.141592654" in script
                    and "local target_ry = 0.000000000" in script
                    and "local target_rz = 0.000000000" in script
                    and "TCP +Z targets base -Z" in script + txt
                    and "active_reacquire_solver qdot" in script + txt
                    and "low-load/no-contact" in script + txt
                    and "commands zero qdot" in txt
                    and "post-RNN normal-direction/tracking guard" in script
                    and "reversed unload" in script
                    and "tracking reversal detection" in txt
                    and "_step5d_tcp_cage_*" in txt
                    and "_step5d_no_contact_s" in txt
                    and "_step5d_rnn_raw_qd*" in txt
                    and "_step5d_post_rnn_normal_guard_action" in txt
                    and "25 N raw-normal/force-norm and 4.0 Nm torque" in txt
                    and "local line_entry_recovery_normal_load_max_n = 20.000" in script
                    and "local line_entry_force_norm_stop_n = 25.000" in script
                    and "codex_abs(normal_force) > 25.0" in script
                    and "force_norm > 25.0" in script
                    and "local line_runtime_limit_s = 15.000" in script
                    and "local line_success_progress_m = 10.000000000" in script
                    and "movel(entry_xy_pose, a=0.090, v=0.060, r=0.0)" in script
                    and "40.000, -0.0225, -0.0025)" in script
                    and "step5d_strict_rnn_liveprep_v23" not in script + txt
                    and "stop_request" in script + txt
                    and "write_output_float_register(35, 25.95)" in script
                    and "local qdot_clear_required_s = 0.006" in script
                    and "local qdot_clear_zero_tol_rad_s = 0.000500" in script
                    and "qdot_clear_cap_rad_s" not in script
                    and "qdot registers 37..42 near zero" in txt
                    and "47 equals 521.0" in txt
                )
        else:
            checks["no stale step5d v1-v7 route"] = all(
                f"step5d_strict_rnn_liveprep_v{idx}" not in script + txt for idx in range(1, 8)
            )
    if program in {
        "step5d_strict_rnn_ablation_v25",
        "step5d_strict_rnn_ablation_v26",
        "step5d_strict_rnn_ablation_v27",
        "step5d_strict_rnn_ablation_v28",
        "step5d_strict_rnn_ablation_v29",
    }:
        version_label = program.rsplit("_", 1)[-1]
        expected_angular_cap = "0.150" if version_label == "v25" else "0.015"
        expected_default_mode = "speedj_rnn_live" if version_label == "v29" else "speedl_cartesian_oracle"
        if version_label == "v25":
            expected_preload_min = "10.500"
            expected_preload_max = "12.800"
            expected_raw_min_text = "9.5"
            expected_raw_max_text = "13.5"
            expected_preload_min_text = "10.5"
            expected_preload_max_text = "12.8"
            expected_recovery_max = "20.000"
            expected_preload_force_norm = "25.000"
            expected_hard_normal_guard = "25"
            expected_hard_force_guard = "25"
            expected_hard_normal_guard_decimal = "25.0"
            expected_hard_force_guard_decimal = "25.0"
            expected_torque_guard_decimal = "4.0"
            expected_runtime_s = "15.000"
            expected_success_s = "10.000000000"
        elif version_label == "v27":
            expected_preload_min = "5.000"
            expected_preload_max = "22.000"
            expected_raw_min_text = "3.0"
            expected_raw_max_text = "25.0"
            expected_preload_min_text = "5.0"
            expected_preload_max_text = "22.0"
            expected_recovery_max = "35.000"
            expected_preload_force_norm = "35.000"
            expected_hard_normal_guard = "35"
            expected_hard_force_guard = "35"
            expected_hard_normal_guard_decimal = "35.0"
            expected_hard_force_guard_decimal = "35.0"
            expected_torque_guard_decimal = "4.0"
            expected_runtime_s = "15.000"
            expected_success_s = "10.000000000"
        elif version_label in {"v28", "v29"}:
            expected_preload_min = "5.000"
            expected_preload_max = "22.000"
            expected_raw_min_text = "3.0"
            expected_raw_max_text = "25.0"
            expected_preload_min_text = "5.0"
            expected_preload_max_text = "22.0"
            expected_recovery_max = "35.000"
            expected_preload_force_norm = "35.000"
            expected_hard_normal_guard = "50"
            expected_hard_force_guard = "60"
            expected_hard_normal_guard_decimal = "50.0"
            expected_hard_force_guard_decimal = "60.0"
            expected_torque_guard_decimal = "3.0"
            expected_runtime_s = "65.000"
            expected_success_s = "60.000000000"
        else:
            expected_preload_min = "7.000"
            expected_preload_max = "18.000"
            expected_raw_min_text = "5.0"
            expected_raw_max_text = "20.0"
            expected_preload_min_text = "7.0"
            expected_preload_max_text = "18.0"
            expected_recovery_max = "24.000"
            expected_preload_force_norm = "25.000"
            expected_hard_normal_guard = "25"
            expected_hard_force_guard = "25"
            expected_hard_normal_guard_decimal = "25.0"
            expected_hard_force_guard_decimal = "25.0"
            expected_torque_guard_decimal = "4.0"
            expected_runtime_s = "15.000"
            expected_success_s = "10.000000000"
        checks.update(
            {
                f"step5d {version_label} ablation function": f"def codex_{program}()" in script
                and "codex_step5d_down_search" in script,
                f"step5d {version_label} bridge contract": f"step4e-version={program}" in script
                and f"--step4e-version {program}" in txt
                and "--step4e-path-shape cycloid" in txt,
                f"step5d {version_label} force contract": "--target-force-n 12.0" in txt,
                f"step5d {version_label} table source": "STEP5_FLOW.md" in script
                and "STEP5_TABLE_SOURCE: config/step5_stage_table.json" in script
                and program in script + txt,
                f"step5d {version_label} bridge start wait timeout": "codex_wait_for_fresh_heartbeat(60.0)" in script
                and "to 60.0 s for a fresh bridge heartbeat" in txt,
                f"step5d {version_label} multimode executor": "multimode_executor_and_guard_only" in script
                and "local stage25_layout_tag = read_input_float_register(47)" in script
                and "local cartesian_layout_code = 523.000" in script
                and "local joint_layout_code = 524.000" in script,
                f"step5d {version_label} speedl speedj": "speedl([cmd_vx, cmd_vy, cmd_vz, cmd_wx, cmd_wy, cmd_wz]" in script
                and "speedj([cmd_qd0, cmd_qd1, cmd_qd2, cmd_qd3, cmd_qd4, cmd_qd5]" in script
                and "local cartesian_linear_cap_m_s = 0.004" in script
                and f"local cartesian_angular_cap_rad_s = {expected_angular_cap}" in script
                and "local qdot_cap_rad_s = 0.050" in script,
                f"step5d {version_label} preload gate": f"local line_entry_default_normal_load_min_n = {expected_preload_min}" in script
                and f"local line_entry_default_normal_load_max_n = {expected_preload_max}" in script
                and f"local line_entry_default_force_norm_max_n = {expected_preload_force_norm}" in script
                and "local line_entry_default_required_s = 0.100" in script
                and f"local line_entry_recovery_normal_load_max_n = {expected_recovery_max}" in script
                and f"local line_entry_force_norm_stop_n = {expected_preload_force_norm}" in script
                and "local line_entry_param_valid_code = 521.000" in script
                and "local candidate_min_n = read_input_float_register(40)" in script
                and "local candidate_required_s = read_input_float_register(44)" in script
                and "local candidate_timeout_s = read_input_float_register(46)" in script
                and f"filtered normal_load between {expected_preload_min_text} N and {expected_preload_max_text} N" in txt
                and f"raw normal_load is sanity-checked between {expected_raw_min_text} N and {expected_raw_max_text} N" in txt,
                f"step5d {version_label} register clear barrier": "write_output_float_register(35, 25.95)" in script
                and "local register_clear_required_s = 0.006" in script
                and "local register_clear_zero_tol = 0.000500" in script
                and "clear_cmd_valid < 0.5" in script
                and "codex_abs(clear_layout_tag - line_entry_param_valid_code) >= 0.001" in script
                and "codex_abs(clear_layout_tag - 523.000) >= 0.001" in script
                and "codex_abs(clear_layout_tag - 524.000) >= 0.001" in script
                and "qdot_clear_required_s" not in script
                and "qdot_clear_cap_rad_s" not in script
                and "Stage 25.95 requires the bridge to clear registers 37..47" in txt,
                f"step5d {version_label} ablation modes": "speedl_cartesian_oracle" in script + txt
                and "speedj_dls_oracle" in txt
                and "speedj_rnn_live" in txt
                and expected_default_mode in txt
                and ("strict RNN live candidate" in txt if version_label == "v29" else "RNN is shadow-only" in txt)
                and "register 47=523.0" in txt
                and "register 47=524.0" in txt,
                f"step5d {version_label} contact safety": "STAGE25_CONTACT_SAFETY" in script
                and "cage margin exhaustion" in script + txt
                and "stop_request" in script + txt
                and (
                    f"{expected_hard_normal_guard} N raw-normal/{expected_hard_force_guard} N force-norm "
                    f"and {expected_torque_guard_decimal} Nm torque"
                )
                in txt,
                f"step5d {version_label} gravity-down": "PRECONTACT_POSE_CONTRACT: pre_contact_search_gravity_down_v1" in script
                and "config/step_pose_contract_table.json" in script + txt
                and "local target_rx = 3.141592654" in script
                and "local target_ry = 0.000000000" in script
                and "local target_rz = 0.000000000" in script
                and "TCP +Z targets base -Z" in script + txt,
                f"step5d {version_label} no lift or second search": "local skip_lift_attitude = 0" not in script
                and "write_output_float_register(35, 25.1)" not in script
                and "write_output_float_register(35, 25.2)" not in script
                and "codex_step5d_down_search(24.3, 24.4" not in script,
                f"step5d {version_label} raw guards": f"codex_abs(normal_force) > {expected_hard_normal_guard_decimal}" in script
                and f"force_norm > {expected_hard_force_guard_decimal}" in script
                and f"torque_norm > {expected_torque_guard_decimal}" in script,
                f"step5d {version_label} Stage25 target window": f"local line_runtime_limit_s = {expected_runtime_s}" in script
                and f"local line_success_progress_m = {expected_success_s}" in script,
                f"step5d {version_label} v27 consumption instrumentation": (
                    version_label not in {"v27", "v28", "v29"}
                    or (
                        (
                            "# STAGE25_V27_SCAFFOLD: step5b_v3_scaffold_min_delta" in script
                            or "# STAGE25_V28_SCAFFOLD: v27_step5b_speedl_live_shadow_boundary_60s_full_run"
                            in script
                            or "# STAGE25_V29_SCAFFOLD: v28_envelope_strict_rnn_live_candidate_60s"
                            in script
                        )
                        and "STAGE25_CADENCE_CONSUMPTION" in script
                        and "write_output_float_register(47, stage25_command_consumed)" in script
                        and "Stage25.0 cadence/command-consumption instrumentation" in txt
                    )
                ),
                f"step5d {version_label} no stale route": "step5b_contact_cycloid_baseline_v1" not in script + txt
                and "step5b_contact_cycloid_baseline_v2" not in script + txt
                and "step5b_contact_cycloid_baseline_v3" not in script + txt
                and "step5c_joint_rnn_cycloid_v1" not in script + txt
                and "step5d_strict_rnn_liveprep_v24" not in script + txt
                and "STEP5D_STRICT_RNN_LIVEPREP_V24" not in script + txt
                and "stop_only_quarantine" not in script + txt,
                f"step5d {version_label} no stale ablation identity": (
                    program == "step5d_strict_rnn_ablation_v25"
                    or (
                        "step5d_strict_rnn_ablation_v25" not in script + txt
                        and "STEP5D_STRICT_RNN_ABLATION_V25" not in script + txt
                    )
                )
                and (
                    program == "step5d_strict_rnn_ablation_v26"
                    or (
                        "step5d_strict_rnn_ablation_v26" not in script + txt
                        and "STEP5D_STRICT_RNN_ABLATION_V26" not in script + txt
                    )
                ),
            }
        )
    if program == "step6a_eight_no_contact_v1":
        checks.update(
            {
                "step6a function": f"def codex_{program}()" in script,
                "step6 flow": "STEP6_FLOW.md" in script
                and "STEP6_FLOW.md" in txt
                and "STEP6_STAGE_ID: step6a_eight_no_contact_v1" in script,
                "step6 table and safe frame": "STEP6_TABLE_SOURCE: config/step6_stage_table.json" in script
                and "STEP6_SAFE_FRAME_SOURCE: config/step6_eight_safe_frame.json" in script,
                "step6 waypoint calibration": "STEP6_WAYPOINTS: five-point read-only RTDE calibration" in script,
                "step6 no-contact policy": "no force control" in script
                and "no contact search" in script
                and "no Kunwei/bridge requirement" in script,
                "step6 fixed-z and runtime": "local fixed_base_z_m =" in script
                and "local path_duration_s = 30.000" in script,
                "step6 elapsed timing": "STEP6_TIMING" in script
                and "t = t + get_steptime()" in script,
                "step6 8-shaped formula": "along=0.04*sin(0.2t)" in script
                and "lateral=0.01*sin(0.4t)" in script
                and "local along_amplitude_m = 0.040000000" in script
                and "local lateral_amplitude_m = 0.010000000" in script,
                "step6 speedl no input-register dependency": "speedl([cmd_vx, cmd_vy, cmd_vz, 0.0, 0.0, 0.0]" in script
                and "read_input_float_register" not in script,
                "step6 no live force/tcp writes": "force_mode" not in script
                and "zero_ftsensor" not in script.replace("no zero_ftsensor()", "")
                and "set_tcp" not in script
                and "set_payload" not in script,
                "step6 no stale step4 route": "step4g_eight_seed_normal_v1" not in script
                and "/programs/andyl/kunwei/step4" not in script + txt,
            }
        )
    if program in {"step6b_contact_eight_baseline_v1", "step6b_contact_eight_baseline_v2"}:
        is_v2 = program.endswith("_v2")
        bridge_version = "step6b_v2" if is_v2 else "step6b_v1"
        stage_id = "step6_contact_eight_baseline_v2" if is_v2 else "step6_contact_eight_baseline_v1"
        checks.update(
            {
                "step6b function": f"def codex_{program}()" in script
                and "codex_step6b_down_search" in script,
                "step6b bridge contract": f"step4e-version={bridge_version}" in script
                and f"--step4e-version {bridge_version}" in txt
                and "--step4e-path-shape eight" in txt,
                "step6b force contract": "--target-force-n 5.0" in script
                and "--target-force-n 5.0" in txt,
                "step6 flow": "STEP6_FLOW.md" in script
                and "STEP6_FLOW.md" in txt
                and f"STEP6_STAGE_ID: {stage_id}" in script,
                "step6 table and safe frame": "STEP6_TABLE_SOURCE: config/step6_stage_table.json" in script
                and "STEP6_SAFE_FRAME_SOURCE: config/step6_eight_safe_frame.json" in script,
                "executor and guard only": "TP_ROLE: executor_and_guard_only" in script
                and "Stage 25.0 consumes bridge command registers 37..44 only" in txt,
                "step6b v2 speed contract": (not is_v2)
                or (
                    "step4e-motion-limit-m-s=0.015" in script
                    and "step4e-total-linear-limit-m-s=0.015" in script
                    and "step4e-angular-limit-rad-s=0.060" in script
                    and "motion 15.0 mm/s" in txt
                    and "25.2 angular 0.060 rad/s" in txt
                    and "Offline feasibility:" in txt
                ),
                "30s contact runtime": "local line_runtime_limit_s = 35.000" in script
                and "local line_success_progress_m = 30.000000000" in script,
                "step6 8-shaped formula": "along=0.04*sin(0.2t)" in script + txt
                and "lateral=0.01*sin(0.4t)" in script + txt,
                "v31 contact scaffold": "first-contact normal latch" in script
                and "25.2 attitude correction" in script
                and "25.3 line-entry gate" in script
                and "normal projection and force-loop composition" in script,
                "raw contact guards": "codex_abs(normal_force) > 50.0" in script
                and "force_norm > 60.0" in script
                and "torque_norm > 3.0" in script,
                "no stale step4 step5 route": "step4f_cycloid_seed_normal_v1" not in script
                and "step4g_eight_seed_normal_v1" not in script
                and "step5b_contact_cycloid_baseline_v1" not in script
                and "/programs/andyl/kunwei/step4" not in script + txt
                and "/programs/andyl/kunwei/step5" not in script + txt,
            }
        )
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        die(f"{program} validation failed: bridge contract mismatch: {failed}")
    return {
        "stamp": stamp,
        "program": program,
        "target_dir": target_dir,
        "installation_relative_path": expected_installation_relative_path,
        "script_node_path": script_node_path,
        "urp_sha256": sha256(files[".urp"]),
        "script_sha256": sha256(files[".script"]),
        "txt_sha256": sha256(files[".txt"]),
    }


def run(cmd: list[str], *, dry_run: bool, capture: bool = False) -> str:
    printable = " ".join(shlex.quote(part) for part in cmd)
    if dry_run:
        print(f"DRY-RUN {printable}")
        return ""
    print(f"RUN {printable}")
    try:
        completed = subprocess.run(
            cmd,
            check=True,
            text=True,
            capture_output=capture,
        )
    except subprocess.CalledProcessError as exc:
        die(f"command failed with exit {exc.returncode}: {printable}")
    if capture:
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        return completed.stdout
    return ""


def _read_verified_controller_helper(helper: Path, expected_sha256: str) -> bytes:
    if not helper.is_absolute():
        die("controller helper path must be absolute")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        die("controller helper SHA-256 is invalid")
    try:
        descriptor = os.open(
            helper,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError:
        die(f"controller helper is missing or unsafe: {helper}")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            die(f"controller helper is missing or unsafe: {helper}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
    finally:
        os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        die("controller helper SHA-256 differs")
    return content


def validate_controller_helper(helper: Path, expected_sha256: str) -> None:
    _read_verified_controller_helper(helper, expected_sha256)


@contextmanager
def controller_helper_snapshot(
    helper: Path,
    expected_sha256: str,
) -> Iterator[Path]:
    content = _read_verified_controller_helper(helper, expected_sha256)
    with tempfile.TemporaryDirectory(prefix="ur10e-controller-helper-") as temporary:
        private_root = Path(temporary)
        os.chmod(private_root, 0o700)
        snapshot = private_root / "controller-helper.py"
        descriptor = os.open(
            snapshot,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(snapshot, 0o400)
        if _read_verified_controller_helper(snapshot, expected_sha256) != content:
            die("controller helper snapshot differs")
        os.chmod(private_root, 0o500)
        try:
            yield snapshot
        finally:
            os.chmod(private_root, 0o700)


def _verified_owner_dependency(name: str) -> dict[str, str]:
    try:
        from step5d_autotune_v3.runtime_installation import (
            RuntimeInstallationError,
            owner_dependency,
        )
    except ImportError as exc:
        die(f"controller owner dependency gate is unavailable: {exc}")
    try:
        return owner_dependency(name)
    except RuntimeInstallationError as exc:
        die(
            "controller owner dependency gate failed: "
            f"{exc.reason_code}: {exc.detail}"
        )


def resolve_live_controller_helper(
    requested_helper: Path | None,
    requested_sha256: str | None,
) -> tuple[Path, str]:
    binding = _verified_owner_dependency("controller_helper")
    path_value = binding.get("path")
    sha_value = binding.get("sha256")
    if (
        not isinstance(path_value, str)
        or not Path(path_value).is_absolute()
        or not isinstance(sha_value, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha_value)
    ):
        die("verified runtime attestation returned an invalid controller helper binding")
    attested_helper = Path(path_value)
    if requested_helper is not None and (
        str(requested_helper) != path_value or requested_sha256 != sha_value
    ):
        die("controller helper CLI binding differs from verified runtime attestation")
    return attested_helper, sha_value


def helper_cmd(
    helper: Path,
    *args: str,
    expected_sha256: str | None,
    require_exists: bool = True,
) -> list[str]:
    if require_exists:
        if expected_sha256 is None:
            die("controller helper SHA-256 is required")
        validate_controller_helper(helper, expected_sha256)
    return [sys.executable, str(helper), *args]


def controller_path(target_dir: str, filename: str) -> str:
    return str(PurePosixPath(target_dir) / filename)


def remote_paths_for(files: dict[str, Path], target_dir: str) -> list[str]:
    return [controller_path(target_dir, files[ext].name) for ext in EXTENSIONS]


def package_sha(files: dict[str, Path]) -> dict[str, str]:
    return {ext: sha256(path) for ext, path in files.items()}


def helper_deployment_manifest(
    files: dict[str, Path], program: str, target_dir: str
) -> dict:
    return {
        "schema_version": 1,
        "basename": program,
        "controller_directory": target_dir,
        "artifacts": [
            {
                "filename": files[ext].name,
                "source": str(files[ext].resolve()),
                "sha256": sha256(files[ext]),
            }
            for ext in EXTENSIONS
        ],
    }


def write_helper_deployment_manifest(
    path: Path, files: dict[str, Path], program: str, target_dir: str
) -> None:
    path.write_text(
        json.dumps(
            helper_deployment_manifest(files, program, target_dir),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def parse_helper_json(output: str) -> dict:
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    die("controller helper returned no JSON object")


def remote_sha256(
    helper: Path,
    remote_paths: list[str],
    *,
    helper_sha256: str,
    expected_sha: dict[str, str],
    dry_run: bool,
) -> dict[str, str]:
    parents = {str(PurePosixPath(path).parent) for path in remote_paths}
    basenames = {PurePosixPath(path).stem for path in remote_paths}
    if len(parents) != 1 or len(basenames) != 1 or len(remote_paths) != len(EXTENSIONS):
        die("controller SHA check must bind one exact triplet")
    target_dir = parents.pop()
    program = basenames.pop()
    with tempfile.TemporaryDirectory(prefix="ur10e-controller-sha-") as tmp:
        temp_root = Path(tmp)
        manifest_path = temp_root / "deployment-manifest.json"
        manifest = {
            "schema_version": 1,
            "basename": program,
            "controller_directory": target_dir,
            "artifacts": [
                {
                    "filename": PurePosixPath(remote_paths[index]).name,
                    "source": str(Path("/nonexistent") / PurePosixPath(remote_paths[index]).name),
                    "sha256": expected_sha[ext],
                }
                for index, ext in enumerate(EXTENSIONS)
            ],
        }

        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        output_dir = temp_root / "readback"
        command = helper_cmd(
            helper,
            "readback",
            "--manifest",
            str(manifest_path),
            "--output-dir",
            str(output_dir),
            expected_sha256=helper_sha256,
            require_exists=not dry_run,
        )
        if dry_run:
            run(command, dry_run=True)
            return {}
        with controller_helper_snapshot(helper, helper_sha256) as snapshot:
            command = helper_cmd(
                snapshot,
                "readback",
                "--manifest",
                str(manifest_path),
                "--output-dir",
                str(output_dir),
                expected_sha256=helper_sha256,
            )
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
            )
        if completed.returncode != 0:
            diagnostic = "\n".join((completed.stdout, completed.stderr))
            if "SHA256 mismatch" in diagnostic:
                return {}
            die(f"controller readback check failed with exit {completed.returncode}")
        payload = parse_helper_json(completed.stdout)
        rows = payload.get("files", [])
        sha_by_filename = {
            row.get("filename"): row.get("sha256")
            for row in rows
            if isinstance(row, dict)
        }
        shas = {
            path: sha_by_filename.get(PurePosixPath(path).name, "")
            for path in remote_paths
        }
        if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in shas.values()):
            die("controller helper readback receipt is incomplete")
        return shas


def triplet_sha_sets_match(shas: dict[str, dict[str, str]]) -> bool:
    return all(
        shas["local"][ext]
        == shas["controller"][ext]
        == shas["readback"][ext]
        for ext in EXTENSIONS
    )


def readback_controller_sha256(
    helper: Path,
    files: dict[str, Path],
    program: str,
    target_dir: str,
    *,
    helper_sha256: str,
    local_sha: dict[str, str],
) -> dict[str, str] | None:
    """Verify the remote triplet through the manifest-bound readback surface."""
    remote_paths = remote_paths_for(files, target_dir)
    controller_sha = remote_sha256(
        helper,
        remote_paths,
        helper_sha256=helper_sha256,
        expected_sha=local_sha,
        dry_run=False,
    )
    if not controller_sha:
        return None
    controller_sha_by_ext = {
        ext: controller_sha[controller_path(target_dir, files[ext].name)]
        for ext in EXTENSIONS
    }
    if any(
        controller_sha_by_ext[ext] != local_sha[ext]
        for ext in EXTENSIONS
    ):
        return None
    return controller_sha_by_ext


def manifest_matches_package(
    manifest: dict,
    *,
    program: str,
    controller: str,
    target_dir: str,
    local_sha: dict[str, str],
) -> bool:
    if manifest.get("status") != "controller read-back verified":
        return False
    if manifest.get("controller") != controller or manifest.get("target_dir") != target_dir:
        return False
    validation = manifest.get("validation", {})
    if validation.get("program") != program or validation.get("target_dir") != target_dir:
        return False
    validation_keys = {
        ".script": "script_sha256",
        ".txt": "txt_sha256",
        ".urp": "urp_sha256",
    }
    for ext, key in validation_keys.items():
        if validation.get(key) != local_sha[ext]:
            return False
    manifest_sha = manifest.get("sha256", {})
    for section in ("local", "controller", "readback"):
        section_sha = manifest_sha.get(section, {})
        if any(section_sha.get(ext) != local_sha[ext] for ext in EXTENSIONS):
            return False
    return True


def readback_triplet_matches(
    manifest_path: Path,
    *,
    program: str,
    local_sha: dict[str, str],
) -> bool:
    readback_dir = manifest_path.parent
    for ext in EXTENSIONS:
        path = readback_dir / f"{program}{ext}"
        if not path.is_file() or sha256(path) != local_sha[ext]:
            return False
    return True


def find_reusable_readback_manifest(
    readback_root: Path,
    *,
    program: str,
    controller: str,
    target_dir: str,
    local_sha: dict[str, str],
) -> tuple[Path, dict] | None:
    candidates = sorted(
        readback_root.glob(f"controller_readback_{program}_*/manifest.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for manifest_path in candidates:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not manifest_matches_package(
            manifest,
            program=program,
            controller=controller,
            target_dir=target_dir,
            local_sha=local_sha,
        ):
            continue
        if not readback_triplet_matches(manifest_path, program=program, local_sha=local_sha):
            continue
        return manifest_path, manifest
    return None


def reuse_readback_if_remote_sha_matches(
    files: dict[str, Path],
    program: str,
    controller: str,
    target_dir: str,
    readback_root: Path,
    readback_dir: Path,
    *,
    helper: Path,
    helper_sha256: str,
    local_sha: dict[str, str],
) -> tuple[dict[str, dict[str, str]], Path] | None:
    if controller != DEFAULT_CONTROLLER:
        die(f"{Path(__file__).name} uses the bench helper for {DEFAULT_CONTROLLER}; got {controller!r}")
    reusable = find_reusable_readback_manifest(
        readback_root,
        program=program,
        controller=controller,
        target_dir=target_dir,
        local_sha=local_sha,
    )
    if reusable is None:
        return None

    manifest_path, _manifest = reusable
    controller_sha_by_ext = readback_controller_sha256(
        helper,
        files,
        program,
        target_dir,
        helper_sha256=helper_sha256,
        local_sha=local_sha,
    )
    if controller_sha_by_ext is None:
        return None

    readback_dir.mkdir(parents=True, exist_ok=False)
    prior_readback_dir = manifest_path.parent
    for ext in EXTENSIONS:
        shutil.copy2(prior_readback_dir / files[ext].name, readback_dir / files[ext].name)
    readback_sha = {ext: sha256(readback_dir / files[ext].name) for ext in EXTENSIONS}
    mismatches = [ext for ext in EXTENSIONS if readback_sha[ext] != local_sha[ext]]
    if mismatches:
        die(f"reused read-back SHA mismatch for: {mismatches}")
    return {"local": local_sha, "controller": controller_sha_by_ext, "readback": readback_sha}, manifest_path


def upload_and_readback(
    files: dict[str, Path],
    program: str,
    controller: str,
    target_dir: str,
    readback_dir: Path,
    *,
    helper: Path,
    helper_sha256: str | None,
    dry_run: bool,
) -> dict[str, dict[str, str]]:
    if controller != DEFAULT_CONTROLLER:
        die(f"{Path(__file__).name} uses the bench helper for {DEFAULT_CONTROLLER}; got {controller!r}")

    local_sha = package_sha(files)
    manifest = {
        "schema_version": 1,
        "basename": program,
        "controller_directory": target_dir,
        "artifacts": [
            {
                "filename": files[ext].name,
                "source": str(files[ext].resolve()),
                "sha256": local_sha[ext],
            }
            for ext in EXTENSIONS
        ],
    }
    with tempfile.TemporaryDirectory(prefix="ur10e-tp-deploy-") as tmp:
        manifest_path = Path(tmp) / "deploy-manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if dry_run:
            run(
                helper_cmd(
                    helper,
                    "deploy-triplet",
                    "--manifest",
                    str(manifest_path),
                    "--confirm-deploy",
                    expected_sha256=helper_sha256,
                    require_exists=False,
                ),
                dry_run=True,
            )
            run(
                helper_cmd(
                    helper,
                    "readback",
                    "--manifest",
                    str(manifest_path),
                    "--output-dir",
                    str(readback_dir),
                    expected_sha256=helper_sha256,
                    require_exists=False,
                ),
                dry_run=True,
            )
            return {}
        if helper_sha256 is None:
            die("controller helper SHA-256 is required")
        with controller_helper_snapshot(helper, helper_sha256) as snapshot:
            deploy_output = run(
                helper_cmd(
                    snapshot,
                    "deploy-triplet",
                    "--manifest",
                    str(manifest_path),
                    "--confirm-deploy",
                    expected_sha256=helper_sha256,
                ),
                dry_run=False,
                capture=True,
            )
            deploy_result = json.loads(deploy_output.strip().splitlines()[-1])
            if deploy_result.get("ok") is not True:
                die("controller helper did not verify deployed triplet")
            readback_dir.mkdir(parents=True, exist_ok=False)
            readback_output = run(
                helper_cmd(
                    snapshot,
                    "readback",
                    "--manifest",
                    str(manifest_path),
                    "--output-dir",
                    str(readback_dir),
                    expected_sha256=helper_sha256,
                ),
                dry_run=False,
                capture=True,
            )
            readback_result = json.loads(readback_output.strip().splitlines()[-1])
            if readback_result.get("ok") is not True:
                die("controller helper did not verify fetched-back triplet")

    readback_sha = {ext: sha256(readback_dir / path.name) for ext, path in files.items()}
    mismatches = [ext for ext in EXTENSIONS if local_sha[ext] != readback_sha[ext]]
    if mismatches:
        die(f"controller read-back SHA mismatch for: {mismatches}")
    controller_sha_by_name = {
        item["filename"]: item["sha256"]
        for item in deploy_result.get("verified_readback", [])
        if isinstance(item, dict)
    }
    remote_mismatches = [
        ext for ext in EXTENSIONS
        if controller_sha_by_name.get(files[ext].name) != local_sha[ext]
    ]
    if remote_mismatches:
        die(f"controller sha256sum mismatch for: {remote_mismatches}")
    controller_sha_by_ext = {
        ext: controller_sha_by_name[files[ext].name] for ext in EXTENSIONS
    }
    return {"local": local_sha, "controller": controller_sha_by_ext, "readback": readback_sha}


def write_manifest(
    readback_dir: Path,
    *,
    controller: str,
    target_dir: str,
    local_dir: Path,
    validation: dict[str, str],
    shas: dict[str, dict[str, str]],
    dry_run: bool,
    delivery_mode: str | None = None,
    reused_from_manifest: Path | None = None,
    fresh_controller_sha_verified: bool | None = None,
    readback_source: str | None = None,
    local_candidate_marker: dict | None = None,
    target_source: str = "table",
    target_resolution: dict | None = None,
    target_override_reason: str | None = None,
    program: str | None = None,
    inactive_candidate_delivery: dict | None = None,
    upload_transaction_id: str | None = None,
) -> Path | None:
    manifest = {
        "status": "dry-run" if dry_run else "controller read-back verified",
        "controller": controller,
        "target_dir": target_dir,
        "target_source": target_source,
        "local_dir": str(local_dir),
        "validation": validation,
        "sha256": shas,
        "safety_boundary": [
            "ssh/scp file deploy only",
            "no URScript send",
            "no program load",
            "no program start",
            "no live bridge",
            "no robot motion command",
        ],
    }
    if delivery_mode is not None:
        manifest["delivery_mode"] = delivery_mode
    if target_resolution is not None:
        manifest["target_resolution"] = target_resolution
    if target_override_reason is not None:
        manifest["target_override_reason"] = target_override_reason
    if reused_from_manifest is not None:
        manifest["skip_basis_manifest"] = str(reused_from_manifest)
    if fresh_controller_sha_verified is not None:
        manifest["fresh_controller_sha_verified"] = fresh_controller_sha_verified
    if fresh_controller_sha_verified:
        manifest["fresh_controller_checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if readback_source is not None:
        manifest["readback_source"] = readback_source
    if inactive_candidate_delivery is not None:
        manifest["inactive_candidate_delivery"] = inactive_candidate_delivery
        manifest["promotion_performed"] = False
    elif program is not None and local_candidate_marker is not None and local_candidate_marker.get("program") == program:
        manifest["promoted_from_local_candidate"] = {
            "marker_schema": local_candidate_marker.get("schema"),
            "semantic_fingerprint": local_candidate_marker.get("semantic_fingerprint"),
            "stamp": local_candidate_marker.get("stamp"),
        }
    if upload_transaction_id is not None:
        manifest["upload_transaction_id"] = upload_transaction_id
    if not dry_run:
        manifest_path = readback_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    else:
        manifest_path = None
    print(json.dumps(manifest, indent=2))
    return manifest_path


def write_manifest_result(
    output: Path,
    *,
    manifest_path: Path,
    upload_transaction_id: str,
) -> None:
    if output.exists():
        die(f"refusing to overwrite manifest-path output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "ur10e_upload_result_v1",
        "upload_transaction_id": upload_transaction_id,
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": sha256(manifest_path),
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", help="program basename, for example step4e_seed_normal_loop_v29")
    parser.add_argument(
        "--target-dir",
        default=None,
        help="override controller directory; requires --override-table and --override-reason",
    )
    parser.add_argument(
        "--override-table",
        action="store_true",
        help="allow an explicit --target-dir instead of the table-resolved controller directory",
    )
    parser.add_argument(
        "--override-reason",
        default="",
        help="required audit reason when --override-table is used",
    )
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER, help=f"default: {DEFAULT_CONTROLLER}")
    parser.add_argument(
        "--controller-helper",
        type=Path,
        default=None,
        help="optional exact assertion of the helper bound by the runtime attestation",
    )
    parser.add_argument(
        "--controller-helper-sha256",
        default=None,
        help="optional exact SHA assertion paired with --controller-helper",
    )
    parser.add_argument("--local-dir", type=Path, default=PROGRAM_DIR, help=f"default: {PROGRAM_DIR}")
    parser.add_argument("--readback-root", type=Path, default=RUN_ROOT, help=f"default: {RUN_ROOT}")
    parser.add_argument(
        "--upload-transaction-id",
        default=None,
        help="opaque identity supplied by a serialized transaction coordinator",
    )
    parser.add_argument(
        "--manifest-path-output",
        type=Path,
        default=None,
        help="write an exact manifest-path handoff for the transaction coordinator",
    )
    parser.add_argument("--dry-run", action="store_true", help="print SSH/SCP plan and validate local files only")
    parser.add_argument(
        "--force-upload-readback",
        action="store_true",
        help="compatibility flag; fresh put/get verification is already the default",
    )
    parser.add_argument(
        "--allow-readback-reuse",
        action="store_true",
        help="development-only optimization; controller_verified promotion still requires fresh put/get",
    )
    args = parser.parse_args(argv)

    if (args.upload_transaction_id is None) != (args.manifest_path_output is None):
        die("--upload-transaction-id and --manifest-path-output must be supplied together")
    if args.dry_run and args.manifest_path_output is not None:
        die("dry-run cannot publish a controller read-back manifest path")
    if (args.controller_helper is None) != (args.controller_helper_sha256 is None):
        die("--controller-helper and --controller-helper-sha256 must be supplied together")
    if args.dry_run:
        helper = args.controller_helper or Path("<controller-helper>")
        helper_sha256 = args.controller_helper_sha256
        if args.controller_helper is not None:
            validate_controller_helper(helper, args.controller_helper_sha256)
    else:
        helper, helper_sha256 = resolve_live_controller_helper(
            args.controller_helper,
            args.controller_helper_sha256,
        )

    program = normalize_program(args.program)
    inactive_delivery_policy = enforce_offline_candidate_delivery_block(program)
    table_resolution = (
        None
        if args.override_table
        else resolve_table_target(
            program,
            required=True,
            local_dir=args.local_dir,
        )
    )
    target_source = "table"
    target_override_reason = None
    if args.target_dir:
        if not args.override_table:
            die("explicit --target-dir requires --override-table and --override-reason")
        if not args.override_reason.strip():
            die("--override-reason is required when --override-table is used")
        target_dir = normalize_target_dir(args.target_dir)
        target_source = "override"
        target_override_reason = args.override_reason.strip()
    else:
        if args.override_table:
            die("--override-table requires --target-dir and --override-reason")
        assert table_resolution is not None
        target_dir = table_resolution["controller_dir"]
    files = triplet(args.local_dir, program)
    local_sha = package_sha(files)
    local_candidate_marker = load_local_candidate_marker(args.local_dir, program)
    inactive_candidate_delivery: dict | None = None
    if local_candidate_marker is not None and local_candidate_marker.get("program") == program:
        validate_local_candidate_marker(
            local_candidate_marker,
            files=files,
            program=program,
            target_dir=target_dir,
            local_sha=local_sha,
        )
        if inactive_delivery_policy is not None:
            inactive_candidate_delivery = validate_inactive_candidate_delivery_binding(
                inactive_delivery_policy,
                local_candidate_marker,
                program=program,
                target_dir=target_dir,
                local_sha=local_sha,
            )
    elif inactive_delivery_policy is not None:
        die("inactive pre-live delivery requires the exact program-specific local candidate marker")
    local_validation = validate_package(files, program, target_dir, require_exact_cached_script=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    readback_dir = args.readback_root / f"controller_readback_{program}_{stamp}"

    print("Local package:")
    for ext in EXTENSIONS:
        print(f"  {ext}: {files[ext]}")
    print("Controller target:")
    for ext in EXTENSIONS:
        print(f"  {ext}: {args.controller}:{PurePosixPath(target_dir) / files[ext].name}")
    print(f"Read-back directory: {readback_dir}")

    reused_from_manifest: Path | None = None
    delivery_mode = "full_upload_readback"
    readback_source = "fresh_controller_get"
    reuse_result = None
    if args.allow_readback_reuse and not args.dry_run and not args.force_upload_readback:
        reuse_result = reuse_readback_if_remote_sha_matches(
            files,
            program,
            args.controller,
            target_dir,
            args.readback_root,
            readback_dir,
            helper=helper,
            helper_sha256=helper_sha256,
            local_sha=local_sha,
        )
    if reuse_result is not None:
        shas, reused_from_manifest = reuse_result
        delivery_mode = "content_addressed_reuse"
        readback_source = "prior_full_readback"
        print(f"Reused verified read-back bytes from: {reused_from_manifest}")
    else:
        shas = upload_and_readback(
            files,
            program,
            args.controller,
            target_dir,
            readback_dir,
            helper=helper,
            helper_sha256=helper_sha256,
            dry_run=args.dry_run,
        )
    if args.dry_run:
        write_manifest(
            readback_dir,
            program=program,
            controller=args.controller,
            target_dir=target_dir,
            local_dir=args.local_dir,
            validation=local_validation,
            shas={},
            dry_run=True,
            delivery_mode="dry-run",
            fresh_controller_sha_verified=None,
            readback_source=None,
            local_candidate_marker=local_candidate_marker,
            target_source=target_source,
            target_resolution=table_resolution,
            target_override_reason=target_override_reason,
            inactive_candidate_delivery=inactive_candidate_delivery,
            upload_transaction_id=args.upload_transaction_id,
        )
        return 0

    readback_files = {ext: readback_dir / files[ext].name for ext in EXTENSIONS}
    readback_validation = validate_package(
        readback_files,
        program,
        target_dir,
        require_exact_cached_script=True,
    )
    manifest_path = write_manifest(
        readback_dir,
        program=program,
        controller=args.controller,
        target_dir=target_dir,
        local_dir=args.local_dir,
        validation=readback_validation,
        shas=shas,
        dry_run=False,
        delivery_mode=delivery_mode,
        reused_from_manifest=reused_from_manifest,
        fresh_controller_sha_verified=triplet_sha_sets_match(shas),
        readback_source=readback_source,
        local_candidate_marker=local_candidate_marker,
        target_source=target_source,
        target_resolution=table_resolution,
        target_override_reason=target_override_reason,
        inactive_candidate_delivery=inactive_candidate_delivery,
        upload_transaction_id=args.upload_transaction_id,
    )
    assert manifest_path is not None
    if args.manifest_path_output is not None:
        assert args.upload_transaction_id is not None
        write_manifest_result(
            args.manifest_path_output,
            manifest_path=manifest_path,
            upload_transaction_id=args.upload_transaction_id,
        )
    if local_candidate_marker is not None and local_candidate_marker.get("program") == program:
        promote_local_candidate_marker_after_readback(
            args.local_dir,
            program,
            local_candidate_marker,
            controller=args.controller,
            target_dir=target_dir,
            readback_dir=readback_dir,
            delivery_mode=delivery_mode,
        )
    if reused_from_manifest is None:
        print(f"controller read-back verified: {readback_dir}")
    else:
        print(f"controller read-back verified via SHA-matched reuse: {readback_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(argv) if argv is not None else list(sys.argv[1:])
    if "--dry-run" in values:
        return _main(values)
    from ur10e_mutation_lock import acquire_controller_mutation_locks, release_controller_mutation_locks
    handles = acquire_controller_mutation_locks()
    try:
        return _main(values)
    finally:
        release_controller_mutation_locks(handles)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"upload blocked: {exc}", file=sys.stderr)
        raise SystemExit(2)
