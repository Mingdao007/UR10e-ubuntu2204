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
import re
import shutil
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path, PurePosixPath


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PROGRAM_DIR = EXPERIMENT_ROOT / "programs"
RUN_ROOT = EXPERIMENT_ROOT / "runs"
DEFAULT_CONTROLLER = "root@192.168.1.18"
DEFAULT_HELPER = Path(
    "/home/andy/codex-private-skills-shared-main/skills/ur10e-controller-access/scripts/ur10e_controller_ssh.py"
)
EXTENSIONS = (".script", ".txt", ".urp")
LOCAL_CANDIDATE_MARKER = ".local_tp_candidate.json"


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


def load_local_candidate_marker(local_dir: Path) -> dict | None:
    marker_path = local_dir / LOCAL_CANDIDATE_MARKER
    if not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"invalid local candidate marker {marker_path}: {exc}")
    if marker.get("local_only") is not True:
        die(f"local candidate marker does not declare local_only=true: {marker_path}")
    return marker


def validate_local_candidate_marker(
    marker: dict,
    *,
    files: dict[str, Path],
    program: str,
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
    if program in {"step5d_strict_rnn_ablation_v25", "step5d_strict_rnn_ablation_v26"}:
        version_label = program.rsplit("_", 1)[-1]
        expected_angular_cap = "0.150" if version_label == "v25" else "0.015"
        expected_default_mode = "speedl_cartesian_oracle" if version_label == "v25" else "speedj_rnn_live"
        expected_preload_min = "10.500" if version_label == "v25" else "7.000"
        expected_preload_max = "12.800" if version_label == "v25" else "18.000"
        expected_raw_min_text = "9.5" if version_label == "v25" else "5.0"
        expected_raw_max_text = "13.5" if version_label == "v25" else "20.0"
        expected_preload_min_text = "10.5" if version_label == "v25" else "7.0"
        expected_preload_max_text = "12.8" if version_label == "v25" else "18.0"
        expected_recovery_max = "20.000" if version_label == "v25" else "24.000"
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
                and "local line_entry_default_force_norm_max_n = 25.000" in script
                and "local line_entry_default_required_s = 0.100" in script
                and f"local line_entry_recovery_normal_load_max_n = {expected_recovery_max}" in script
                and "local line_entry_force_norm_stop_n = 25.000" in script
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
                and "RNN is shadow-only" in txt
                and "register 47=523.0" in txt
                and "register 47=524.0" in txt,
                f"step5d {version_label} contact safety": "STAGE25_CONTACT_SAFETY" in script
                and "cage margin exhaustion" in script + txt
                and "stop_request" in script + txt
                and "25 N raw-normal/force-norm and 4.0 Nm torque" in txt,
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
                f"step5d {version_label} raw guards": "codex_abs(normal_force) > 25.0" in script
                and "force_norm > 25.0" in script
                and "torque_norm > 4.0" in script,
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


def helper_cmd(helper: Path, *args: str) -> list[str]:
    if not helper.is_file():
        die(f"controller helper not found: {helper}")
    return [sys.executable, str(helper), *args]


def controller_path(target_dir: str, filename: str) -> str:
    return str(PurePosixPath(target_dir) / filename)


def remote_paths_for(files: dict[str, Path], target_dir: str) -> list[str]:
    return [controller_path(target_dir, files[ext].name) for ext in EXTENSIONS]


def package_sha(files: dict[str, Path]) -> dict[str, str]:
    return {ext: sha256(path) for ext, path in files.items()}


def remote_sha256(helper: Path, remote_paths: list[str], *, dry_run: bool) -> dict[str, str]:
    if dry_run:
        for remote_path in remote_paths:
            run(helper_cmd(helper, "run", "--", "sha256sum", remote_path), dry_run=True)
        return {}
    output = run(
        helper_cmd(helper, "run", "--", "sha256sum", *remote_paths),
        dry_run=False,
        capture=True,
    )
    shas: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"^([0-9a-fA-F]{64})\s+(.+)$", line.strip())
        if not match:
            continue
        digest, path = match.groups()
        shas[path] = digest.lower()
    missing = [path for path in remote_paths if path not in shas]
    if missing:
        die(f"could not parse controller sha256sum for: {missing}")
    return shas


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
    remote_paths = remote_paths_for(files, target_dir)
    controller_sha = remote_sha256(helper, remote_paths, dry_run=False)
    controller_sha_by_ext = {
        ext: controller_sha[controller_path(target_dir, files[ext].name)] for ext in EXTENSIONS
    }
    remote_mismatches = [ext for ext in EXTENSIONS if controller_sha_by_ext[ext] != local_sha[ext]]
    if remote_mismatches:
        print(
            "Controller SHA does not match reusable read-back; "
            f"falling back to full upload/read-back for: {remote_mismatches}"
        )
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
    dry_run: bool,
) -> dict[str, dict[str, str]]:
    if controller != DEFAULT_CONTROLLER:
        die(f"{Path(__file__).name} uses the bench helper for {DEFAULT_CONTROLLER}; got {controller!r}")

    run(helper_cmd(helper, "run", "--", "mkdir", "-p", target_dir), dry_run=dry_run)
    remote_paths = remote_paths_for(files, target_dir)
    for ext in EXTENSIONS:
        remote_path = controller_path(target_dir, files[ext].name)
        run(helper_cmd(helper, "put", str(files[ext]), remote_path), dry_run=dry_run)

    run(helper_cmd(helper, "run", "--", "chown", "1000:1000", *remote_paths), dry_run=dry_run)
    run(helper_cmd(helper, "run", "--", "chmod", "664", *remote_paths), dry_run=dry_run)
    run(helper_cmd(helper, "run", "--", "ls", "-l", *remote_paths), dry_run=dry_run)
    controller_sha = remote_sha256(helper, remote_paths, dry_run=dry_run)

    if dry_run:
        for ext in EXTENSIONS:
            remote_path = controller_path(target_dir, files[ext].name)
            run(helper_cmd(helper, "get", remote_path, str(readback_dir / files[ext].name)), dry_run=True)
        return {}

    readback_dir.mkdir(parents=True, exist_ok=False)
    for ext in EXTENSIONS:
        remote_path = controller_path(target_dir, files[ext].name)
        run(helper_cmd(helper, "get", remote_path, str(readback_dir / files[ext].name)), dry_run=False)

    local_sha = package_sha(files)
    readback_sha = {ext: sha256(readback_dir / path.name) for ext, path in files.items()}
    mismatches = [ext for ext in EXTENSIONS if local_sha[ext] != readback_sha[ext]]
    if mismatches:
        die(f"controller read-back SHA mismatch for: {mismatches}")
    remote_mismatches = [
        ext
        for ext in EXTENSIONS
        if controller_sha[controller_path(target_dir, files[ext].name)] != local_sha[ext]
    ]
    if remote_mismatches:
        die(f"controller sha256sum mismatch for: {remote_mismatches}")
    controller_sha_by_ext = {
        ext: controller_sha[controller_path(target_dir, files[ext].name)] for ext in EXTENSIONS
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
) -> None:
    manifest = {
        "status": "dry-run" if dry_run else "controller read-back verified",
        "controller": controller,
        "target_dir": target_dir,
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
    if reused_from_manifest is not None:
        manifest["skip_basis_manifest"] = str(reused_from_manifest)
    if fresh_controller_sha_verified is not None:
        manifest["fresh_controller_sha_verified"] = fresh_controller_sha_verified
    if fresh_controller_sha_verified:
        manifest["fresh_controller_checked_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    if readback_source is not None:
        manifest["readback_source"] = readback_source
    if local_candidate_marker is not None:
        manifest["promoted_from_local_candidate"] = {
            "marker_schema": local_candidate_marker.get("schema"),
            "semantic_fingerprint": local_candidate_marker.get("semantic_fingerprint"),
            "stamp": local_candidate_marker.get("stamp"),
        }
    if not dry_run:
        (readback_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("program", help="program basename, for example step4e_seed_normal_loop_v29")
    parser.add_argument(
        "--target-dir",
        required=True,
        help="explicit controller directory, for example /programs/andyl/kunwei/step4/",
    )
    parser.add_argument("--controller", default=DEFAULT_CONTROLLER, help=f"default: {DEFAULT_CONTROLLER}")
    parser.add_argument(
        "--controller-helper",
        type=Path,
        default=DEFAULT_HELPER,
        help=f"password-capable controller helper, default: {DEFAULT_HELPER}",
    )
    parser.add_argument("--local-dir", type=Path, default=PROGRAM_DIR, help=f"default: {PROGRAM_DIR}")
    parser.add_argument("--readback-root", type=Path, default=RUN_ROOT, help=f"default: {RUN_ROOT}")
    parser.add_argument("--dry-run", action="store_true", help="print SSH/SCP plan and validate local files only")
    parser.add_argument(
        "--force-upload-readback",
        action="store_true",
        help="disable SHA-matched read-back reuse and force put/get verification",
    )
    parser.add_argument(
        "--allow-local-candidate-promote",
        action="store_true",
        help="explicitly promote a directory marked local_only=true; ignored for --dry-run",
    )
    args = parser.parse_args(argv)

    program = normalize_program(args.program)
    target_dir = normalize_target_dir(args.target_dir)
    files = triplet(args.local_dir, program)
    local_sha = package_sha(files)
    local_candidate_marker = load_local_candidate_marker(args.local_dir)
    if local_candidate_marker is not None:
        validate_local_candidate_marker(
            local_candidate_marker,
            files=files,
            program=program,
            target_dir=target_dir,
            local_sha=local_sha,
        )
        if not args.dry_run and not args.allow_local_candidate_promote:
            die(
                "refusing to upload local-only TP candidate without "
                "--allow-local-candidate-promote; run a local dev-loop dry-run or promote explicitly"
            )
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
    if not args.dry_run and not args.force_upload_readback:
        reuse_result = reuse_readback_if_remote_sha_matches(
            files,
            program,
            args.controller,
            target_dir,
            args.readback_root,
            readback_dir,
            helper=args.controller_helper,
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
            helper=args.controller_helper,
            dry_run=args.dry_run,
        )
    if args.dry_run:
        write_manifest(
            readback_dir,
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
        )
        return 0

    readback_files = {ext: readback_dir / files[ext].name for ext in EXTENSIONS}
    readback_validation = validate_package(
        readback_files,
        program,
        target_dir,
        require_exact_cached_script=True,
    )
    write_manifest(
        readback_dir,
        controller=args.controller,
        target_dir=target_dir,
        local_dir=args.local_dir,
        validation=readback_validation,
        shas=shas,
        dry_run=False,
        delivery_mode=delivery_mode,
        reused_from_manifest=reused_from_manifest,
        fresh_controller_sha_verified=reused_from_manifest is not None,
        readback_source=readback_source,
        local_candidate_marker=local_candidate_marker,
    )
    if reused_from_manifest is None:
        print(f"controller read-back verified: {readback_dir}")
    else:
        print(f"controller read-back verified via SHA-matched reuse: {readback_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"upload blocked: {exc}", file=sys.stderr)
        raise SystemExit(2)
