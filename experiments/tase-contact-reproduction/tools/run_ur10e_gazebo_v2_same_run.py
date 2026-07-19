#!/usr/bin/env python3
"""Coordinate one isolated velocity-only Gazebo v2 same-run capture.

All child processes receive an explicit environment with common SCM token
variables removed.  Cleanup is limited to process groups created here; this
tool never uses broad ``pkill`` and never addresses a real robot/controller.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Sequence

from ur10e_parallel import ResourceProfile, gazebo_headless_lease


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = EXPERIMENT_ROOT.parents[1]
TOOLS = EXPERIMENT_ROOT / "tools"
SCM_SECRET_ENV = frozenset(
    {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GITHUB_PAT",
        "GITLAB_TOKEN",
        "GL_TOKEN",
        "BITBUCKET_TOKEN",
        "AZURE_DEVOPS_EXT_PAT",
    }
)
NATIVE_TOPICS = (
    "/ur10e/gazebo_v2/native_contact",
    "/ur10e/gazebo_v2/native_ft",
    "/world/ur10e_gazebo_v2_fortress/pose/info",
)


def sanitized_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(source or os.environ)
    for name in SCM_SECRET_ENV:
        env.pop(name, None)
    return env


def _popen(
    command: Sequence[str],
    *,
    log_path: Path,
    env: dict[str, str],
) -> tuple[subprocess.Popen[str], IO[str]]:
    handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        list(command),
        cwd=WORKSPACE,
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return process, handle


def _live_process_rows() -> list[tuple[int, int, int]] | None:
    result = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,stat="],
        env=sanitized_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    rows: list[tuple[int, int, int]] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        try:
            pid, pgid = int(fields[0]), int(fields[1])
            sid = os.getsid(pid)
        except (ProcessLookupError, PermissionError, ValueError):
            continue
        if "Z" not in fields[2]:
            rows.append((pid, pgid, sid))
    return rows


def _live_process_group_members(process_group_id: int) -> list[int]:
    """Return non-zombie members of one exact supervisor-owned process group."""

    rows = _live_process_rows()
    if rows is None:
        return [process_group_id]
    return sorted(pid for pid, pgid, _ in rows if pgid == process_group_id)


def _live_owned_session(session_id: int) -> dict[int, list[int]]:
    """Inventory exact PGIDs that remain inside one owned POSIX session."""

    rows = _live_process_rows()
    if rows is None:
        return {session_id: [session_id]}
    groups: dict[int, list[int]] = {}
    for pid, pgid, sid in rows:
        if sid == session_id:
            groups.setdefault(pgid, []).append(pid)
    return {pgid: sorted(pids) for pgid, pids in sorted(groups.items())}


def _wait_for_owned_session_exit(
    process: subprocess.Popen[str],
    *,
    timeout_s: float,
) -> dict[int, list[int]]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        process.poll()  # Reap the owned group leader as soon as it exits.
        groups = _live_owned_session(process.pid)
        if not groups:
            return {}
        time.sleep(0.05)
    return _live_owned_session(process.pid)


def _terminate(
    process: subprocess.Popen[str] | None,
    *,
    timeout_s: float = 8.0,
) -> dict[int, list[int]]:
    """Stop one exact owned session, including child-created process groups."""

    if process is None:
        return {}
    residual = _live_owned_session(process.pid)
    if not residual:
        process.poll()
        return {}
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        # Snapshot exact groups before the leader can exit and orphan them.
        # Signal the leader's group last so it remains available to supervise
        # child-group teardown for as long as possible.
        groups = [pgid for pgid in residual if pgid != process.pid]
        if process.pid in residual:
            groups.append(process.pid)
        for process_group_id in groups:
            try:
                os.killpg(process_group_id, sig)
            except ProcessLookupError:
                pass
        residual = _wait_for_owned_session_exit(process, timeout_s=timeout_s)
        if not residual:
            break
    process.poll()
    return residual


def _run(command: Sequence[str], *, env: dict[str, str], timeout_s: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=WORKSPACE,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _package_prefix(package: str, *, env: dict[str, str]) -> Path:
    result = _run(["ros2", "pkg", "prefix", package], env=env)
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(f"package_prefix_unavailable:{package}")
    prefix = Path(result.stdout.strip()).resolve()
    if not prefix.is_dir():
        raise RuntimeError(f"package_prefix_not_directory:{package}:{prefix}")
    return prefix


def build_gazebo_server_spec(
    base_env: dict[str, str],
    *,
    package_prefixes: dict[str, Path],
) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    """Build one direct Fortress server command owned by this supervisor."""

    own_prefix = package_prefixes["ur10e_example_controllers"].resolve()
    description_prefix = package_prefixes["ur_description"].resolve()
    control_prefix = package_prefixes["ign_ros2_control"].resolve()
    world_path = (
        own_prefix
        / "share"
        / "ur10e_example_controllers"
        / "worlds"
        / "ur10e_gazebo_v2_fortress.sdf"
    )
    if not world_path.is_file():
        raise RuntimeError(f"gazebo_world_missing:{world_path}")

    server_env = sanitized_environment(base_env)
    resources = [
        entry
        for entry in server_env.get("IGN_GAZEBO_RESOURCE_PATH", "").split(os.pathsep)
        if entry
    ]
    resources.extend([str(own_prefix / "share"), str(description_prefix / "share")])
    resources = list(dict.fromkeys(resources))
    plugins = [
        entry
        for entry in server_env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", "").split(os.pathsep)
        if entry
    ]
    plugins.append(str(control_prefix / "lib"))
    plugins = list(dict.fromkeys(plugins))
    server_env["IGN_GAZEBO_RESOURCE_PATH"] = os.pathsep.join(resources)
    server_env["IGN_GAZEBO_SYSTEM_PLUGIN_PATH"] = os.pathsep.join(plugins)
    command = ["ign", "gazebo", str(world_path), "-r", "-s", "--headless-rendering"]
    binding = {
        "world_path": str(world_path),
        "resource_paths": resources,
        "system_plugin_paths": plugins,
        "server_started_directly_by_supervisor": True,
    }
    return command, server_env, binding


def resolve_gazebo_server_spec(
    base_env: dict[str, str],
) -> tuple[list[str], dict[str, str], dict[str, Any]]:
    prefixes = {
        package: _package_prefix(package, env=base_env)
        for package in ("ur10e_example_controllers", "ur_description", "ign_ros2_control")
    }
    return build_gazebo_server_spec(base_env, package_prefixes=prefixes)


def _fortress_abi_preflight(env: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    command = [
        sys.executable,
        "-c",
        (
            "import json; "
            "from ur10e_example_controllers.ur10e_gazebo_v2 import probe_fortress_abi; "
            "report = probe_fortress_abi(); "
            "print(json.dumps(report, sort_keys=True)); "
            "raise SystemExit(0 if report.get('pass') else 1)"
        ),
    ]
    result = _run(command, env=env, timeout_s=15.0)
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {
            "pass": False,
            "issue": "abi_preflight_output_invalid",
            "process_returncode": result.returncode,
        }
    return result.returncode == 0 and report.get("pass") is True, report


def _wait_for_native_topics(env: dict[str, str], timeout_s: float) -> tuple[list[str], list[str]]:
    deadline = time.monotonic() + timeout_s
    last: set[str] = set()
    while time.monotonic() < deadline:
        result = _run(["ign", "topic", "-l"], env=env)
        last = set(result.stdout.splitlines()) if result.returncode == 0 else set()
        missing = [topic for topic in NATIVE_TOPICS if topic not in last]
        if not missing:
            return [], sorted(last)
        time.sleep(0.5)
    return [topic for topic in NATIVE_TOPICS if topic not in last], sorted(last)


def velocity_controller_inventory_pass(text: str) -> bool:
    states = {
        line.split()[0].partition("[")[0]: line.split()[-1]
        for line in text.splitlines()
        if len(line.split()) >= 2
    }
    return bool(
        states.get("joint_state_broadcaster") == "active"
        and states.get("gazebo_v2_velocity_controller") == "active"
        and states.get("gazebo_v2_effort_surrogate_controller") != "active"
    )


def _wait_for_controller(env: dict[str, str], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = _run(
            ["ros2", "control", "list_controllers", "--controller-manager", "/controller_manager"],
            env=env,
        )
        if result.returncode == 0 and velocity_controller_inventory_pass(result.stdout):
            return True
        time.sleep(0.5)
    return False


def bridge_command() -> list[str]:
    mappings = [
        "/ur10e/gazebo_v2/native_contact@ros_gz_interfaces/msg/Contacts[ignition.msgs.Contacts",
        "/ur10e/gazebo_v2/native_ft@geometry_msgs/msg/WrenchStamped[ignition.msgs.Wrench",
    ]
    mappings.extend(
        f"/ur10e/gazebo_v2/camera/{name}@sensor_msgs/msg/Image[ignition.msgs.Image"
        for name in ("wide", "oblique", "close", "contact")
    )
    return ["ros2", "run", "ros_gz_bridge", "parameter_bridge", *mappings]


def run(args: argparse.Namespace) -> int:  # noqa: C901 - process lifecycle stays in one auditable scope.
    if args.backend != "velocity":
        raise SystemExit("same-run supervisor currently supports velocity only")
    if not os.environ.get("ROS_DOMAIN_ID") or not os.environ.get("IGN_PARTITION"):
        raise SystemExit("ROS_DOMAIN_ID and IGN_PARTITION must both be set for isolated execution")
    try:
        ros_domain_id = int(os.environ["ROS_DOMAIN_ID"])
    except ValueError as exc:
        raise SystemExit("ROS_DOMAIN_ID must be an integer") from exc
    if not 0 <= ros_domain_id <= 232:
        raise SystemExit("ROS_DOMAIN_ID must be within the Fast DDS supported range 0..232")
    run_dir = args.run_dir.resolve()
    if run_dir.exists():
        raise SystemExit(f"refusing to reuse run directory: {run_dir}")
    run_dir.mkdir(parents=True)
    env = sanitized_environment()
    run_id = args.run_id or f"gazebo-v2-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    metadata = {
        "schema": "ur10e_gazebo_v2_supervisor_v1",
        "run_id": run_id,
        "backend": "velocity",
        "ros_domain_id": str(ros_domain_id),
        "ign_partition": env["IGN_PARTITION"],
        "scm_secret_environment_removed": sorted(SCM_SECRET_ENV),
        "real_robot_or_controller_address_used": False,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "supervisor_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    processes: dict[str, subprocess.Popen[str]] = {}
    handles: list[IO[str]] = []
    blockers: list[str] = []
    cleanup_residuals: dict[str, dict[int, list[int]]] = {}
    try:
        try:
            server_command, server_env, server_binding = resolve_gazebo_server_spec(env)
        except RuntimeError as exc:
            blockers.append(str(exc))
            return 18
        metadata["gazebo_server_binding"] = server_binding
        preflight_pass, preflight_report = _fortress_abi_preflight(server_env)
        (run_dir / "gazebo_abi_preflight.json").write_text(
            json.dumps(preflight_report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if not preflight_pass:
            blockers.append("gazebo_fortress_abi_preflight_failed")
            return 19
        server, handle = _popen(
            server_command,
            log_path=run_dir / "gazebo_server.log",
            env=server_env,
        )
        processes["server"] = server
        handles.append(handle)
        time.sleep(1.0)
        if server.poll() is not None:
            blockers.append(f"gazebo_server_exited_early:{server.returncode}")
            return 20

        launch, handle = _popen(
            [
                "ros2",
                "launch",
                "ur10e_example_controllers",
                "ur10e_gazebo_v2_fortress.launch.py",
                "backend:=velocity",
                "headless:=true",
                "run_gazebo:=false",
                "spawn_robot:=true",
                "start_controller:=true",
                "abi_preflight:=false",
            ],
            log_path=run_dir / "gazebo_launch.log",
            env=server_env,
        )
        processes["launch"] = launch
        handles.append(handle)
        missing, topic_inventory = _wait_for_native_topics(server_env, args.startup_timeout_s)
        (run_dir / "native_topic_inventory.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "required": list(NATIVE_TOPICS),
                    "missing": missing,
                    "topics": topic_inventory,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if missing:
            blockers.append("native_topics_missing:" + ",".join(missing))
            return 21
        if not _wait_for_controller(server_env, args.startup_timeout_s):
            blockers.append("velocity_controller_not_ready_or_not_exclusive")
            return 22

        bridge, handle = _popen(
            bridge_command(),
            log_path=run_dir / "native_bridge.log",
            env=server_env,
        )
        processes["bridge"] = bridge
        handles.append(handle)
        time.sleep(1.0)
        if bridge.poll() is not None:
            blockers.append("native_bridge_exited_early")
            return 23

        capture_duration = args.duration_s + args.startup_timeout_s + 5.0
        capture, handle = _popen(
            [
                sys.executable,
                str(TOOLS / "capture_ur10e_gazebo_v2_native.py"),
                str(run_dir),
                "--backend",
                "velocity",
                "--duration-s",
                str(capture_duration),
                "--run-id",
                run_id,
                "--external-runtime-artifacts",
                "--allow-existing-run-dir",
            ],
            log_path=run_dir / "native_capture.log",
            env=server_env,
        )
        processes["capture"] = capture
        handles.append(handle)
        ready_deadline = time.monotonic() + args.startup_timeout_s
        while time.monotonic() < ready_deadline and not (run_dir / "capture_ready.json").is_file():
            if capture.poll() is not None:
                blockers.append("native_capture_exited_before_ready")
                return 24
            time.sleep(0.2)
        if not (run_dir / "capture_ready.json").is_file():
            blockers.append("native_capture_ready_timeout")
            return 25

        runtime, handle = _popen(
            [
                sys.executable,
                str(TOOLS / "ur10e_gazebo_v2_runtime_adapter.py"),
                "--run-dir",
                str(run_dir),
                "--run-id",
                run_id,
                "--backend",
                "velocity",
                "--duration-s",
                str(args.duration_s),
                "--startup-timeout-s",
                str(args.startup_timeout_s),
            ],
            log_path=run_dir / "runtime_adapter.log",
            env=server_env,
        )
        processes["runtime"] = runtime
        handles.append(handle)
        try:
            runtime_rc = runtime.wait(timeout=args.duration_s + args.startup_timeout_s + 10.0)
        except subprocess.TimeoutExpired:
            blockers.append("runtime_adapter_timeout")
            return 26
        if runtime_rc != 0:
            blockers.append(f"runtime_adapter_exit:{runtime_rc}")
            return 27
        capture_residuals = _terminate(capture)
        if capture_residuals:
            blockers.append("native_capture_cleanup_residual")
            cleanup_residuals["capture"] = capture_residuals
            return 28
        capture_rc = capture.wait(timeout=10.0)
        if capture_rc != 0:
            blockers.append(f"native_capture_exit:{capture_rc}")
            return 29
    finally:
        for name in ("runtime", "capture", "bridge", "launch", "server"):
            residuals = _terminate(processes.get(name))
            if residuals:
                cleanup_residuals[name] = residuals
        if cleanup_residuals:
            blockers.append("owned_process_cleanup_incomplete")
        for handle in handles:
            handle.close()
        metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        metadata["blockers"] = blockers
        metadata["process_returncodes"] = {
            name: process.poll() for name, process in processes.items()
        }
        metadata["cleanup_residual_processes"] = cleanup_residuals
        (run_dir / "supervisor_metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    required = (
        "capture_manifest.json",
        "runtime_manifest.json",
        "tick_trace.jsonl",
        "native_contact.jsonl",
        "native_ft.jsonl",
        "camera_manifest.json",
    )
    missing = [name for name in required if not (run_dir / name).is_file()]
    if missing:
        metadata["blockers"] = [f"final_artifact_missing:{name}" for name in missing]
        (run_dir / "supervisor_metadata.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return 30
    if cleanup_residuals:
        return 31
    print(run_dir)
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--backend", choices=("velocity", "effort_surrogate"), default="velocity")
    parser.add_argument("--duration-s", type=float, default=10.0)
    parser.add_argument("--startup-timeout-s", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.duration_s <= 0.0 or args.startup_timeout_s <= 0.0:
        parser.error("duration and startup timeout must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    profile = ResourceProfile.from_env()
    lease = gazebo_headless_lease(profile, f"gazebo-v2:{args.run_dir}")
    with lease:
        assert lease.slot is not None
        os.environ["ROS_DOMAIN_ID"] = str(180 + lease.slot)
        partition = f"ur10e-gz-{lease.lease_id}"
        os.environ["IGN_PARTITION"] = partition
        os.environ["GZ_PARTITION"] = partition
        temporary = args.run_dir.resolve().parent / f".{args.run_dir.name}.tmp-{lease.lease_id}"
        temporary.mkdir(parents=True, exist_ok=False)
        os.environ["TMPDIR"] = str(temporary)
        try:
            return run(args)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
