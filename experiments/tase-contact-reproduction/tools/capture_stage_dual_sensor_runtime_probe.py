#!/usr/bin/env python3
"""Passive no-restart probe for stage dual-sensor runtime readiness.

This tool only lists ROS 2 and Ignition/Gazebo topics/services for an already
running simulation. It does not launch, stop, restart, fork, or mutate Gazebo
and it does not touch real hardware. The artifact is fail-closed: topic
availability can support a capture-path readiness statement, not contact or
force acceptance by itself.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


GOAL_LINEAGE = "/home/andy/codex_handoffs/ur10e-gazebo-17h-sim-ft-rnn-goal-prompt-20260621-0056.md"
SCHEMA = "ur10e_stage_dual_sensor_runtime_probe_v1"
DEFAULT_STAGE_ID = "step5b"
DEFAULT_FILENAME = "stage_dual_sensor_runtime_probe.json"
ENV_KEYS = (
    "ROS_DOMAIN_ID",
    "ROS_LOCALHOST_ONLY",
    "IGN_PARTITION",
    "GZ_PARTITION",
    "IGN_GAZEBO_SYSTEM_PLUGIN_PATH",
    "GZ_SIM_SYSTEM_PLUGIN_PATH",
    "GZ_SIM_RESOURCE_PATH",
)
REQUIRED_ROS_VISUAL_TOPICS = ("/clock", "/joint_states", "/tf", "/tf_static")
CONTACT_TOPIC_HINTS = ("contact", "contacts")
SIM_FT_WRENCH_HINTS = ("simulated_ft", "canonical_wrench", "force_torque", "ft_sensor", "wrench")
SIM_FT_STATUS_HINTS = ("simulated_ft", "canonical_wrench", "ft_sensor", "wrench")
STEP_STATUS_HINTS = ("controller_state", "trajectory_controller/state", "step_status", "rnn")


CommandRunner = Callable[[list[str], dict[str, str], float], dict[str, Any]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_process_env(pid: int | None) -> dict[str, str]:
    if pid is None:
        return {}
    path = Path(f"/proc/{pid}/environ")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"_read_error": f"{type(exc).__name__}: {exc}"}
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if not item or b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        env[key.decode("utf-8", errors="replace")] = value.decode("utf-8", errors="replace")
    return env


def merged_command_env(process_env: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    for key in ENV_KEYS:
        value = process_env.get(key)
        if value:
            env[key] = value
    return env


def run_command(cmd: list[str], env: dict[str, str], timeout_s: float) -> dict[str, Any]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "returncode": None,
            "timed_out": True,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": exc.stderr if isinstance(exc.stderr, str) else "",
        }
    return {
        "command": cmd,
        "returncode": result.returncode,
        "timed_out": False,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def ros2_command(args: str) -> list[str]:
    return ["bash", "-lc", f"source /opt/ros/humble/setup.bash && ros2 {args}"]


def parse_ros_topics(stdout: str) -> list[dict[str, str | None]]:
    rows: list[dict[str, str | None]] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        topic = line.split(" ", 1)[0]
        msg_type = None
        if "[" in line and "]" in line:
            msg_type = line.rsplit("[", 1)[1].split("]", 1)[0]
        rows.append({"name": topic, "type": msg_type})
    return rows


def parse_lines(stdout: str) -> list[str]:
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def _topic_names(ros_topics: list[dict[str, str | None]], ign_topics: list[str]) -> list[str]:
    return [str(row["name"]) for row in ros_topics] + list(ign_topics)


def _contains_hint(topic: str, hints: tuple[str, ...]) -> bool:
    lowered = topic.lower()
    return any(hint in lowered for hint in hints)


def classify_topics(ros_topics: list[dict[str, str | None]], ign_topics: list[str]) -> dict[str, Any]:
    ros_names = [str(row["name"]) for row in ros_topics]
    all_topics = _topic_names(ros_topics, ign_topics)
    missing_ros_visual_topics = [topic for topic in REQUIRED_ROS_VISUAL_TOPICS if topic not in ros_names]
    ign_camera_topics = [
        topic
        for topic in ign_topics
        if topic.endswith("/image") or topic.endswith("/camera_info") or "/camera/" in topic
    ]
    ign_world_state_topics = [topic for topic in ign_topics if "/world/" in topic and "/state" in topic]
    contact_candidates = [topic for topic in all_topics if _contains_hint(topic, CONTACT_TOPIC_HINTS)]
    simulated_ft_wrench_candidates = [
        topic
        for topic in all_topics
        if _contains_hint(topic, SIM_FT_WRENCH_HINTS) and "joint_trajectory" not in topic.lower()
    ]
    simulated_ft_status_candidates = [
        topic
        for topic in all_topics
        if _contains_hint(topic, SIM_FT_STATUS_HINTS) and "status" in topic.lower()
    ]
    step_status_candidates = [topic for topic in all_topics if _contains_hint(topic, STEP_STATUS_HINTS)]
    visual_runtime_supported = bool(
        not missing_ros_visual_topics and (ign_camera_topics or ign_world_state_topics)
    )
    contact_runtime_topics_available = bool(contact_candidates)
    simulated_ft_runtime_topics_available = bool(
        simulated_ft_wrench_candidates and simulated_ft_status_candidates
    )
    step_runtime_topics_available = bool(step_status_candidates)
    capture_path_topics_available = bool(
        visual_runtime_supported
        and contact_runtime_topics_available
        and simulated_ft_runtime_topics_available
        and step_runtime_topics_available
    )
    blockers: list[str] = []
    if missing_ros_visual_topics:
        blockers.append("visual_runtime_ros_topics:missing:" + ",".join(missing_ros_visual_topics))
    if not (ign_camera_topics or ign_world_state_topics):
        blockers.append("visual_runtime_ign_topics:missing")
    if not contact_runtime_topics_available:
        blockers.append("stage_contact_pair_or_state_topic:missing")
    if not simulated_ft_wrench_candidates:
        blockers.append("stage_simulated_ft_wrench_topic:missing")
    if not simulated_ft_status_candidates:
        blockers.append("stage_simulated_ft_status_topic:missing")
    if not step_runtime_topics_available:
        blockers.append("step_or_rnn_status_topic:missing")
    blockers.append("same_run_stage_dual_sensor_observation:not_proven")
    return {
        "visual_runtime_supported": visual_runtime_supported,
        "contact_runtime_topics_available": contact_runtime_topics_available,
        "simulated_ft_runtime_topics_available": simulated_ft_runtime_topics_available,
        "step_runtime_topics_available": step_runtime_topics_available,
        "capture_path_topics_available": capture_path_topics_available,
        "missing_ros_visual_topics": missing_ros_visual_topics,
        "ign_camera_topics": ign_camera_topics,
        "ign_world_state_topics": ign_world_state_topics,
        "contact_topic_candidates": contact_candidates,
        "simulated_ft_wrench_topic_candidates": simulated_ft_wrench_candidates,
        "simulated_ft_status_topic_candidates": simulated_ft_status_candidates,
        "step_status_topic_candidates": step_status_candidates,
        "blockers": blockers,
    }


def build_probe(
    *,
    stage_id: str,
    observation_id: str,
    pids: list[int],
    process_env: dict[str, str],
    command_results: dict[str, dict[str, Any]],
    generated_start: str,
    generated_end: str,
) -> dict[str, Any]:
    ros_topics = parse_ros_topics(command_results["ros_topic_list"]["stdout"])
    ros_nodes = parse_lines(command_results["ros_node_list"]["stdout"])
    ign_topics = parse_lines(command_results["ign_topic_list"]["stdout"])
    ign_services = parse_lines(command_results["ign_service_list"]["stdout"])
    classification = classify_topics(ros_topics, ign_topics)
    command_failures = [
        name
        for name, result in command_results.items()
        if result.get("timed_out") or result.get("returncode") not in (0, None)
    ]
    blockers = list(classification["blockers"])
    if command_failures:
        blockers.append("runtime_probe_command_failures:" + ",".join(command_failures))
    return {
        "schema": SCHEMA,
        "generated_at": generated_end,
        "goal_lineage": GOAL_LINEAGE,
        "mode": "passive_no_restart_runtime_topic_probe",
        "stage_id": stage_id,
        "observation_id": observation_id,
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "allowed_claim": "visual_only runtime topic inventory and future capture-path readiness only",
        "forbidden_claim": (
            "simulated_ft validated; physical Gazebo collision/contact physics; "
            "same-run dual-sensor/integrated binding; real bench/live contact"
        ),
        "same_run_stage_dual_sensor_observation_proven": False,
        "capture_path_topics_available": classification["capture_path_topics_available"],
        "time_window": {
            "start": generated_start,
            "end": generated_end,
            "clock_source": "wall_time_for_probe; ROS /clock listed only if present",
        },
        "runtime": {
            "pids": pids,
            "ros_domain_id": process_env.get("ROS_DOMAIN_ID"),
            "ros_localhost_only": process_env.get("ROS_LOCALHOST_ONLY"),
            "ign_partition": process_env.get("IGN_PARTITION"),
            "gz_partition": process_env.get("GZ_PARTITION"),
        },
        "topic_inventory": {
            "ros_topics": ros_topics,
            "ros_nodes": ros_nodes,
            "ign_topics": ign_topics,
            "ign_services": ign_services,
        },
        "classification": classification,
        "command_results": command_results,
        "blockers": blockers,
        "live_authorization": {
            "robot_motion_authorized": False,
            "bridge_start_authorized": False,
            "tp_play_authorized": False,
            "urscript_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
            "real_bench_live_contact_authorized": False,
        },
    }


def probe_runtime(
    output_dir: Path,
    *,
    stage_id: str,
    observation_id: str,
    pids: list[int],
    timeout_s: float = 5.0,
    runner: CommandRunner = run_command,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_start = _now_iso()
    process_env: dict[str, str] = {}
    for pid in pids:
        candidate = read_process_env(pid)
        for key in ENV_KEYS:
            if candidate.get(key):
                process_env.setdefault(key, candidate[key])
        if candidate.get("_read_error"):
            process_env[f"pid_{pid}_read_error"] = candidate["_read_error"]
    command_env = merged_command_env(process_env)
    command_results = {
        "ros_topic_list": runner(ros2_command("topic list -t"), command_env, timeout_s),
        "ros_node_list": runner(ros2_command("node list"), command_env, timeout_s),
        "ign_topic_list": runner(["ign", "topic", "-l"], command_env, timeout_s),
        "ign_service_list": runner(["ign", "service", "-l"], command_env, timeout_s),
    }
    generated_end = _now_iso()
    payload = build_probe(
        stage_id=stage_id,
        observation_id=observation_id,
        pids=pids,
        process_env=process_env,
        command_results=command_results,
        generated_start=generated_start,
        generated_end=generated_end,
    )
    output_path = output_dir / DEFAULT_FILENAME
    payload["artifact_path"] = str(output_path)
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--stage-id", default=DEFAULT_STAGE_ID)
    parser.add_argument("--observation-id", required=True)
    parser.add_argument("--pid", dest="pids", action="append", type=int, default=[])
    parser.add_argument("--timeout-s", type=float, default=5.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    path = probe_runtime(
        args.output_dir,
        stage_id=args.stage_id,
        observation_id=args.observation_id,
        pids=args.pids,
        timeout_s=args.timeout_s,
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
