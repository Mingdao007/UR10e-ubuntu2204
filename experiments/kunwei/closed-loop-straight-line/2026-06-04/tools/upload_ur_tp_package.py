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

    checks = {
        "script stamp": stamp in script,
        "txt stamp": stamp in txt,
        "URProgram name": root.attrib.get("name") == program,
        "controller directory": root.attrib.get("directory") == target_dir,
        "Script-node path": script_node_path == expected_script_path,
        "cachedContents stamp": stamp in cached_script,
        "cachedContents program": f"def codex_{program}" in cached_script
        if program in {"step4e_seed_normal_loop_v29", "step4e_seed_normal_loop_v30", "step4e_seed_normal_loop_v31"}
        else True,
        "cachedContents exact script": cached_script == script if require_exact_cached_script else True,
    }
    if program in {"step4e_seed_normal_loop_v29", "step4e_seed_normal_loop_v30", "step4e_seed_normal_loop_v31"}:
        version = program.rsplit("_", 1)[-1]
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
                "force norm guard": "force_norm > 50.0" in script,
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
    failed = [label for label, ok in checks.items() if not ok]
    if failed:
        die(f"{program} validation failed: {failed}")
    return {
        "stamp": stamp,
        "program": program,
        "target_dir": target_dir,
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
    remote_paths: list[str] = []
    for ext in EXTENSIONS:
        remote_path = controller_path(target_dir, files[ext].name)
        remote_paths.append(remote_path)
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

    local_sha = {ext: sha256(path) for ext, path in files.items()}
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
    args = parser.parse_args(argv)

    program = normalize_program(args.program)
    target_dir = normalize_target_dir(args.target_dir)
    files = triplet(args.local_dir, program)
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
    )
    print(f"controller read-back verified: {readback_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"upload blocked: {exc}", file=sys.stderr)
        raise SystemExit(2)
