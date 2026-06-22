#!/usr/bin/env python3
"""Capture independent Step5b Gazebo pose/info transform evidence.

This tool runs the non-formal Step5b probe world in Gazebo server mode and
captures one `/world/<world>/pose/info` message with `gz topic`. It does not
start ROS controllers, a bridge, TP programs, URScript, or real robot motion.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
TOOLS = EXPERIMENT_ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import run_step5b_gz_transport_probe as transport_probe  # noqa: E402


SCHEMA = "ur10e_step5b_gazebo_pose_info_transform_audit_v1"
STAGE_ID = "step5b"
OBSERVATION_SCOPE = "non_formal_step5b_transport_probe"


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def probe_manifest_path(run_dir: Path) -> Path:
    candidates = sorted((run_dir / "probe_worlds").glob("step5b_forced_contact_probe_world.manifest.json"))
    if not candidates:
        raise FileNotFoundError(f"missing Step5b forced-contact probe manifest under {run_dir}")
    return candidates[-1]


def probe_world_path(run_dir: Path) -> Path:
    manifest_path = probe_manifest_path(run_dir)
    payload = load_json(manifest_path)
    value = payload.get("probe_world")
    if not value:
        raise ValueError(f"probe_world missing from {manifest_path}")
    path = Path(str(value))
    return path if path.is_absolute() else manifest_path.parent / path


def world_name(world_path: Path) -> str:
    root = ET.parse(world_path).getroot()
    world = root.find("./world")
    if world is None or not world.get("name"):
        raise ValueError(f"world name missing from {world_path}")
    return str(world.get("name"))


def first_json_line(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return json.loads(stripped)
    raise ValueError("no JSON pose/info message captured")


def pose_xyz(pose: dict[str, Any]) -> list[float]:
    position = pose.get("position") if isinstance(pose.get("position"), dict) else {}
    return [
        float(position.get("x") or 0.0),
        float(position.get("y") or 0.0),
        float(position.get("z") or 0.0),
    ]


def pose_xyzw(pose: dict[str, Any]) -> list[float]:
    orientation = pose.get("orientation") if isinstance(pose.get("orientation"), dict) else {}
    return [
        float(orientation.get("x") or 0.0),
        float(orientation.get("y") or 0.0),
        float(orientation.get("z") or 0.0),
        float(orientation.get("w") if orientation.get("w") is not None else 1.0),
    ]


def close(values: list[float], expected: list[float], *, tolerance: float = 1e-9) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(values, expected))


def extract_named_poses(payload: dict[str, Any], names: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for pose in payload.get("pose") or []:
        if not isinstance(pose, dict):
            continue
        name = str(pose.get("name") or "")
        if name not in names:
            continue
        xyz = pose_xyz(pose)
        xyzw = pose_xyzw(pose)
        found[name] = {
            "name": name,
            "id": pose.get("id"),
            "position_xyz_m": xyz,
            "orientation_xyzw": xyzw,
            "world_identity": close(xyz, [0.0, 0.0, 0.0]) and close(xyzw, [0.0, 0.0, 0.0, 1.0]),
        }
    return found


def build_audit_payload(
    *,
    generated_at: str,
    run_dir: Path,
    world_path: Path,
    pose_info_path: Path,
    pose_info_payload: dict[str, Any],
    gz_returncode: int,
    gz_stderr_path: Path,
    server_stderr_path: Path,
) -> dict[str, Any]:
    names = ["ur10e_base_frame", "base_link", "step5b_nonformal_forced_eoat_probe", "step5_contact_surface"]
    named = extract_named_poses(pose_info_payload, names)
    blockers: list[str] = []
    for required in ["ur10e_base_frame", "base_link"]:
        if required not in named:
            blockers.append(f"missing_pose_info_entity:{required}")
        elif not named[required]["world_identity"]:
            blockers.append(f"pose_info_entity_not_world_identity:{required}")
    if gz_returncode != 0:
        blockers.append(f"gz_topic_returncode={gz_returncode}")
    independent = not blockers
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "stage_id": STAGE_ID,
        "observation_scope": OBSERVATION_SCOPE,
        "claim_tier": "visual_only",
        "run_dir": str(run_dir),
        "world_path": str(world_path),
        "world_name": world_name(world_path),
        "source": "gazebo_runtime_pose_info_topic",
        "transport": "gz",
        "pose_info_topic": f"/world/{world_name(world_path)}/pose/info",
        "pose_info_path": str(pose_info_path),
        "pose_info_sha256": sha256_file(pose_info_path),
        "gz_stderr_path": str(gz_stderr_path),
        "gz_stderr_sha256": sha256_file(gz_stderr_path),
        "server_stderr_path": str(server_stderr_path),
        "server_stderr_sha256": sha256_file(server_stderr_path),
        "gz_topic_returncode": gz_returncode,
        "named_poses": named,
        "independent_gazebo_runtime_transform_evidence": independent,
        "native_frame_interpreted_as": "gazebo_world",
        "from_frame": "gazebo_contact_message_native_frame",
        "to_frame": "base",
        "transform": {"translation_xyz_m": [0.0, 0.0, 0.0], "rpy_rad": [0.0, 0.0, 0.0]},
        "formal_step5b_transform_proof": False,
        "formal_transform_blocker": "non_formal_probe_evidence_only_formal_step5b_same_run_not_attempted",
        "blockers": blockers,
        "step5b_attempt_spent": False,
        "formal_step5b_attempt": False,
        "safety_boundary": [
            "offline Gazebo server pose/info capture only",
            "no bridge start",
            "no controller upload",
            "no TP Play",
            "no URScript",
            "no robot motion",
            "no zero_ftsensor",
            "no payload/TCP/safety writes",
        ],
        "allowed_claim": (
            "independent Gazebo runtime base/world identity evidence for the non-formal Step5b forced probe"
            if independent
            else "visual_only blocked/not_proven"
        ),
        "forbidden_claim": "formal Step5b acceptance; same-run formal transform proof; real bench/live contact",
    }


def capture_pose_info(output_dir: Path, *, run_dir: Path, timeout_s: float = 6.0) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = now()
    world_path = probe_world_path(run_dir)
    name = world_name(world_path)
    env = transport_probe.gazebo_env()
    server_stdout_path = output_dir / "gazebo_pose_info_server_stdout.log"
    server_stderr_path = output_dir / "gazebo_pose_info_server_stderr.log"
    gz_stdout_path = output_dir / "gz_pose_info_raw.json"
    gz_stderr_path = output_dir / "gz_pose_info_stderr.log"
    server = subprocess.Popen(
        transport_probe.server_command("gz", world_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    gz_stdout = ""
    gz_stderr = ""
    gz_returncode = 124
    try:
        time.sleep(1.0)
        result = subprocess.run(
            ["gz", "topic", "-e", "--json-output", "-n", "1", "-t", f"/world/{name}/pose/info"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        gz_stdout = result.stdout
        gz_stderr = result.stderr
        gz_returncode = result.returncode
    except subprocess.TimeoutExpired as exc:
        gz_stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        gz_stderr = (exc.stderr if isinstance(exc.stderr, str) else "") + "\ngz pose/info capture timed out\n"
    finally:
        transport_probe._terminate_process_group(server)
        server_stdout, server_stderr = server.communicate(timeout=5.0)
    server_stdout_path.write_text(server_stdout or "", encoding="utf-8")
    server_stderr_path.write_text(server_stderr or "", encoding="utf-8")
    gz_stdout_path.write_text(gz_stdout or "", encoding="utf-8")
    gz_stderr_path.write_text(gz_stderr or "", encoding="utf-8")
    pose_payload = first_json_line(gz_stdout) if gz_stdout.strip() else {"pose": []}
    audit = build_audit_payload(
        generated_at=generated_at,
        run_dir=run_dir,
        world_path=world_path,
        pose_info_path=gz_stdout_path,
        pose_info_payload=pose_payload,
        gz_returncode=gz_returncode,
        gz_stderr_path=gz_stderr_path,
        server_stderr_path=server_stderr_path,
    )
    audit["server_stdout_path"] = str(server_stdout_path)
    audit["server_stdout_sha256"] = sha256_file(server_stdout_path)
    path = write_json(output_dir / "step5b_transform_pose_info_audit.json", audit)
    print(path)
    return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-s", type=float, default=6.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = capture_pose_info(args.output_dir, run_dir=args.run_dir, timeout_s=args.timeout_s)
    payload = load_json(path)
    return 0 if payload.get("independent_gazebo_runtime_transform_evidence") else 2


if __name__ == "__main__":
    raise SystemExit(main())
