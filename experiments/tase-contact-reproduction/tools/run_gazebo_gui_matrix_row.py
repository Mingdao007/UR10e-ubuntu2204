#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml


WORKSPACE = Path(__file__).resolve().parents[3]
EXPERIMENT = WORKSPACE / "experiments" / "tase-contact-reproduction"
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import ur10e_gazebo_matrix_runner as gazebo  # noqa: E402
import build_gazebo_contact_wrench_trace as wrench_adapter  # noqa: E402
import build_per_stage_dual_sensor_contact_audit as per_stage_contact_audit  # noqa: E402
import build_stage_dual_sensor_observation_manifest as stage_observation  # noqa: E402
import capture_p2_gazebo_contact_pair_log as contact_capture  # noqa: E402


STAGES = ("step5a", "step5b", "step5c", "step5d", "step6a", "step6b", "step7", "step8")
VIEWS = ("context_overview", "interaction_view", "side_view", "close_detail")
CONTACT_STAGES = set(gazebo.CONTACT_STAGE_IDS)
TRACE_STATUS = {
    "step5a": "matched",
    "step5b": "approximated",
    "step5c": "approximated",
    "step5d": "approximated",
    "step6a": "matched",
    "step6b": "approximated",
    "step7": "approximated",
    "step8": "approximated",
}
DEFAULT_GUI_CONFIG_DIR = EXPERIMENT / "config" / "gazebo_gui_real_aligned_views"
POSE_SOURCE_ACTIVE_TCP = "joint_states_to_runner_fk_active_tcp_base_to_gazebo_world"
DEFAULT_WORLD_NAME = "ur10e_step5_table_world"
DEFAULT_OBSERVER_MARKER_STYLE = "minimal_tcp_dot"
MARKER_STYLES = ("debug", "observer_subtle", "minimal_tcp_dot")
CONTACT_CAPTURE_SCHEMA = "ur10e_gazebo_row_contact_topic_capture_v1"
STAGE_CONTACT_WRENCH_ADAPTER_FILENAME = "stage_contact_wrench_adapter.json"
STAGE_CONTACT_WRENCH_TRACE_FILENAME = "stage_contact_wrench_trace.json"
SAME_RUN_STAGE_OBSERVATION_SCOPE = "same_run_stage_gazebo_row"
STAGE_DUAL_SENSOR_OBSERVATION_DIRNAME = "stage_dual_sensor_observation"
PER_STAGE_DUAL_SENSOR_CONTACT_AUDIT_DIRNAME = "per_stage_dual_sensor_contact"
VISIBLE_GAZEBO_OVERLAP_SCHEMA = "ur10e_visible_gazebo_overlap_preflight_v1"
VISIBLE_GAZEBO_OVERLAP_RC = 43
VISIBLE_GAZEBO_LOCK_SCHEMA = "ur10e_visible_gazebo_row_lock_preflight_v1"
VISIBLE_GAZEBO_LOCK_RC = 44
PLUGIN_PATH_PREFLIGHT_SCHEMA = "ur10e_gazebo_row_plugin_path_preflight_v1"
PLUGIN_PATH_PREFLIGHT_RC = 45
ACTION_READINESS_PREFLIGHT_SCHEMA = "ur10e_gazebo_row_action_readiness_preflight_v1"
ACTION_READINESS_PREFLIGHT_RC = 46
ROS_CONTROL_SYSTEM_PLUGIN_FILENAMES = ("libign_ros2_control-system.so", "libgz_ros2_control-system.so")
ACTION_READINESS_REQUIRED_PACKAGES = (
    "ur10e_example_controllers",
    "ur_description",
    "ign_ros2_control",
    "controller_manager",
    "ros_gz_sim",
    "robot_state_publisher",
    "control_msgs",
)
ACTION_READINESS_REQUIRED_INTERFACE = "control_msgs/action/FollowJointTrajectory"
DEFAULT_VISIBLE_GAZEBO_LOCK_PATH = Path("/tmp/ur10e_gazebo_visible_gui_row.lock")
ENHANCED_MARKER_VISUAL_NAMES = frozenset(
    {
        "tcp_contact_pad_orange",
        "tcp_magenta_sphere",
        "tcp_probe_sleeve_yellow",
        "tcp_white_mast",
        "tcp_tool_plate_silver",
        "tcp_sensor_body_teal",
        "tcp_cyan_crossbar_x",
        "tcp_magenta_crossbar_y",
    }
)
ACTUAL_EOAT_MESH_VISUAL_NAMES = frozenset({gazebo.EOAT_REAL_MESH_VISUAL_NAME})
ACTUAL_CONTACT_SURFACE_MESH_VISUAL_NAMES = frozenset({gazebo.CONTACT_SURFACE_REAL_MESH_VISUAL_NAME})


def run_row(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    run_dir = args.run_dir.resolve()
    case_dir = row_case_dir(run_dir, args.stage, args.view)
    world_dir = run_dir / "_visual_worlds"
    world = world_dir / f"{args.stage}_visual.sdf"
    gui_config = (args.gui_config_dir or DEFAULT_GUI_CONFIG_DIR) / f"{args.view}.config"
    trace_path = case_dir / "command_trace.txt"
    row_started_at = _now()
    stage_observation_id = f"{args.stage}-{args.view}-{row_started_at}"
    case_dir.mkdir(parents=True, exist_ok=True)
    world_dir.mkdir(parents=True, exist_ok=True)

    env = build_row_environment(display=args.display, local_ros_prefix=args.local_ros_prefix)

    _write_trace(
        trace_path,
        {
            "started_at": _now(),
            "row_started_at": row_started_at,
            "stage": args.stage,
            "view": args.view,
            "world": str(world),
            "gui_config": str(gui_config),
            "display": env["DISPLAY"],
            "local_ros_prefix": str(args.local_ros_prefix.resolve()) if args.local_ros_prefix else "",
            "ign_plugin_path": env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", ""),
            "marker_style": args.marker_style,
        },
        mode="w",
    )
    visible_lock_handle = acquire_visible_gazebo_row_lock(args.visible_gazebo_lock_path)
    if visible_lock_handle is None:
        payload = write_visible_gazebo_lock_preflight(
            case_dir,
            stage=args.stage,
            view=args.view,
            run_dir=run_dir,
            display=args.display,
            lock_path=args.visible_gazebo_lock_path,
        )
        _write_trace(
            trace_path,
            {
                "visible_gazebo_lock_preflight": "failed",
                "visible_gazebo_lock_artifact": payload["path"],
                "visible_gazebo_lock_path": str(args.visible_gazebo_lock_path),
                "blocker": payload["blocker"],
                "finished_at": _now(),
            },
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return VISIBLE_GAZEBO_LOCK_RC
    _write_trace(
        trace_path,
        {
            "visible_gazebo_lock_acquired": 1,
            "visible_gazebo_lock_path": str(args.visible_gazebo_lock_path),
        },
    )
    if not args.allow_existing_gazebo:
        overlapping_gazebo = existing_visible_gazebo_processes()
        if overlapping_gazebo:
            payload = write_visible_gazebo_overlap_preflight(
                case_dir,
                stage=args.stage,
                view=args.view,
                run_dir=run_dir,
                display=args.display,
                processes=overlapping_gazebo,
            )
            _write_trace(
                trace_path,
                {
                    "visible_gazebo_overlap_preflight": "failed",
                    "visible_gazebo_overlap_artifact": payload["path"],
                    "visible_gazebo_overlap_process_count": len(overlapping_gazebo),
                    "blocker": payload["blocker"],
                    "finished_at": _now(),
                },
            )
            print(json.dumps(payload, indent=2, sort_keys=True))
            release_visible_gazebo_row_lock(visible_lock_handle)
            return VISIBLE_GAZEBO_OVERLAP_RC

    build_log = case_dir / "build_visual_world.log"
    with build_log.open("w", encoding="utf-8") as handle:
        subprocess.run(
            [
                sys.executable,
                str(workspace / "experiments/tase-contact-reproduction/tools/build_gazebo_visual_world.py"),
                "--stage",
                args.stage,
                "--output",
                str(world),
            ],
            cwd=workspace,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=True,
            text=True,
        )

    launch = marker = ffmpeg = contact_capture_proc = None
    runner_rc = None
    abort_rc = None
    abort_blocker = None
    contact_topic = gazebo_visual_contact_topic(args.stage)
    try:
        launch = _popen(
            [
                "ros2",
                "launch",
                "ur10e_example_controllers",
                "ur10e_gazebo_matrix.launch.py",
                "headless:=false",
                f"world_path:={world}",
                f"gui_config_path:={gui_config}",
            ],
            case_dir / "ros2_launch.log",
            cwd=workspace,
            env=env,
            new_session=True,
        )
        _write_trace(trace_path, {"launch_pid": launch.pid})
        ready_after = _wait_for_action(workspace, env, args.action_ready_timeout_s)
        if ready_after is None:
            _write_trace(trace_path, {"action_ready": 0})
            abort_rc = 41
            abort_blocker = "action_ready_timeout"
        else:
            _write_trace(trace_path, {"action_ready": 1, "action_ready_after_s": ready_after})
            if args.stage in CONTACT_STAGES and not args.disable_contact_capture:
                contact_capture_proc = start_contact_topic_capture(
                    case_dir,
                    topic=contact_topic,
                    env=env,
                    max_messages=args.contact_capture_max_messages,
                    observation_id=stage_observation_id,
                )
                _write_trace(
                    trace_path,
                    {
                        "contact_capture_pid": contact_capture_proc.pid if contact_capture_proc is not None else None,
                        "contact_capture_topic": contact_topic,
                    },
                )

            marker = _popen(
                [
                    sys.executable,
                    str(workspace / "experiments/tase-contact-reproduction/tools/gazebo_tcp_marker_follower.py"),
                    "--stage",
                    args.stage,
                    "--output-dir",
                    str(case_dir / "marker"),
                    "--duration-s",
                    str(args.max_record_s),
                    "--marker-style",
                    args.marker_style,
                ],
                case_dir / "marker_follower.log",
                cwd=workspace,
                env=env,
                new_session=True,
            )
            _write_trace(trace_path, {"marker_pid": marker.pid})
            ffmpeg = _popen(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "x11grab",
                    "-framerate",
                    "10",
                    "-video_size",
                    args.capture_size,
                    "-i",
                    f"{args.display}.0",
                    "-t",
                    str(args.max_record_s),
                    "-pix_fmt",
                    "yuv420p",
                    str(case_dir / "gui_recording.mp4"),
                ],
                case_dir / "ffmpeg.log",
                cwd=workspace,
                env=env,
                new_session=True,
            )
            _write_trace(trace_path, {"ffmpeg_pid": ffmpeg.pid})
            runner_rc = _run_logged(
                [
                    sys.executable,
                    "-m",
                    "ur10e_example_controllers.ur10e_gazebo_matrix_runner",
                    "--stage",
                    args.stage,
                    "--output-dir",
                    str(case_dir / "runner"),
                    "--execute",
                ],
                case_dir / "runner.log",
                cwd=workspace,
                env=env,
            )
            _write_trace(trace_path, {"runner_rc": runner_rc})
            camera_capture = capture_scripted_camera_image(
                case_dir,
                stage=args.stage,
                view=args.view,
                env=env,
                timeout_s=args.scripted_camera_timeout_s,
            )
            _write_trace(trace_path, {"scripted_camera_capture": camera_capture})
            capture_scene_introspection(
                case_dir,
                env=env,
                world_name=DEFAULT_WORLD_NAME,
                robot_model_name="ur10e_gazebo_matrix",
            )
    finally:
        if args.stage in CONTACT_STAGES and not args.disable_contact_capture:
            finish_contact_topic_capture(
                contact_capture_proc,
                case_dir,
                stage=args.stage,
                topic=contact_topic,
                world_path=world,
                max_messages=args.contact_capture_max_messages,
                observation_id=stage_observation_id,
                time_start=row_started_at,
            )
        _terminate_process(marker)
        _terminate_process(ffmpeg)
        _terminate_process_group(launch)
        time.sleep(1.0)
        _kill_process(marker)
        _kill_process(ffmpeg)
        _kill_process_group(launch)

    if abort_rc is not None:
        row_finished_at = _now()
        failure_paths = write_action_ready_failure_artifacts(
            case_dir,
            run_dir=run_dir,
            stage=args.stage,
            view=args.view,
            runner_rc=abort_rc,
            blocker=abort_blocker or "action_ready_timeout",
            stage_simulated_ft_manifest_path=args.stage_simulated_ft_manifest,
            step_status_audit_path=args.step_status_audit,
            observation_id=stage_observation_id,
            time_start=row_started_at,
            time_end=row_finished_at,
            clock_source=args.stage_observation_clock_source,
            trace_path=trace_path,
            include_observation_manifest=not args.disable_stage_observation_manifest,
            include_per_stage_audit=not args.disable_per_stage_contact_audit,
            correlation_tolerance_s=args.per_stage_contact_correlation_tolerance_s,
        )
        _write_trace(
            trace_path,
            {
                "finished_at": row_finished_at,
                "row_summary": str(failure_paths["row_summary_path"]),
                "same_run_stage_dual_sensor_observation_manifest": failure_paths.get(
                    "same_run_stage_dual_sensor_observation_manifest_path"
                ),
                "per_stage_dual_sensor_contact_audit": failure_paths.get("per_stage_dual_sensor_contact_audit_path"),
                "action_success": 0,
                "blocker": abort_blocker or "action_ready_timeout",
            },
        )
        release_visible_gazebo_row_lock(visible_lock_handle)
        print(json.dumps(failure_paths["row_summary"], indent=2, sort_keys=True))
        return abort_rc

    video = case_dir / "gui_recording.mp4"
    duration_s = _video_duration(video)
    _extract_frames(video, duration_s, case_dir)
    row_finished_at = _now()
    row = build_row_summary(
        stage=args.stage,
        view=args.view,
        case_dir=case_dir,
        run_dir=run_dir,
        runner_rc=runner_rc,
        video_duration_s=duration_s,
        gui_config_path=gui_config,
    )
    row_summary_path = case_dir / "row_summary.json"
    row = annotate_contact_stage_evidence_paths(
        row,
        case_dir=case_dir,
        stage=args.stage,
        include_observation_manifest=not args.disable_stage_observation_manifest,
        include_per_stage_audit=not args.disable_per_stage_contact_audit,
    )
    write_row_summary(row, row_summary_path)
    observation_path = None
    if args.stage in CONTACT_STAGES and not args.disable_stage_observation_manifest:
        observation_path = write_stage_dual_sensor_observation_manifest(
            case_dir,
            stage=args.stage,
            row=row,
            row_summary_path=row_summary_path,
            stage_simulated_ft_manifest_path=args.stage_simulated_ft_manifest,
            step_status_audit_path=args.step_status_audit,
            observation_id=stage_observation_id,
            time_start=row_started_at,
            time_end=row_finished_at,
            clock_source=args.stage_observation_clock_source,
        )
        _write_trace(
            trace_path,
            {
                "same_run_stage_dual_sensor_observation_manifest": str(observation_path),
            },
        )
    if args.stage in CONTACT_STAGES and not args.disable_per_stage_contact_audit:
        audit_path = write_per_stage_dual_sensor_contact_audit(
            case_dir,
            stage=args.stage,
            row=row,
            row_summary_path=row_summary_path,
            stage_simulated_ft_manifest_path=args.stage_simulated_ft_manifest,
            step_status_audit_path=args.step_status_audit,
            same_run_observation_manifest_path=observation_path,
            correlation_tolerance_s=args.per_stage_contact_correlation_tolerance_s,
        )
        _write_trace(
            trace_path,
            {
                "per_stage_dual_sensor_contact_audit": str(audit_path),
            },
        )
    if args.update_summary:
        write_visual_audit_summary(build_visual_audit_summary(run_dir), run_dir / "visual_audit_summary.json")
    _write_trace(
        trace_path,
        {
            "finished_at": _now(),
            "video_duration_s": duration_s,
            "marker_pose_count": row["marker_pose_count"],
            "action_success": row["action_success"],
            "observer_visual_pass": row["observer_visual_pass"],
            "blocker": row["blocker"],
        },
    )
    release_visible_gazebo_row_lock(visible_lock_handle)
    print(json.dumps(row, indent=2, sort_keys=True))
    return int(runner_rc or 0)


def row_case_dir(run_dir: Path, stage: str, view: str) -> Path:
    return run_dir / "matrix_gui_real_aligned" / stage / view


def workspace_install_prefix_dirs(*, local_ros_prefix: Path | None) -> list[Path]:
    dirs: list[Path] = []
    if local_ros_prefix is not None:
        dirs.append(local_ros_prefix.resolve())
    install_dir = WORKSPACE / "install"
    if install_dir.is_dir():
        for candidate in sorted(install_dir.iterdir(), key=lambda path: path.name):
            package_index = candidate / "share" / "ament_index" / "resource_index" / "packages"
            if candidate.is_dir() and package_index.is_dir():
                dirs.append(candidate.resolve())
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in dirs:
        key = str(path)
        if key not in seen:
            deduped.append(path)
            seen.add(key)
    return deduped


def row_ros_prefix_dirs(*, local_ros_prefix: Path | None) -> list[Path]:
    dirs = workspace_install_prefix_dirs(local_ros_prefix=local_ros_prefix)
    for candidate in (Path("/opt/ros/humble"),):
        package_index = candidate / "share" / "ament_index" / "resource_index" / "packages"
        if candidate.is_dir() and package_index.is_dir():
            dirs.append(candidate.resolve())
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in dirs:
        key = str(path)
        if key not in seen:
            deduped.append(path)
            seen.add(key)
    return deduped


def build_row_environment(*, display: str, local_ros_prefix: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["PYTHONPATH"] = _prepend(str(SRC_PACKAGE), env.get("PYTHONPATH"))
    install_prefixes = row_ros_prefix_dirs(local_ros_prefix=local_ros_prefix)
    if install_prefixes:
        prefix_entries = [str(prefix) for prefix in install_prefixes]
        lib_entries = [str(prefix / "lib") for prefix in install_prefixes if (prefix / "lib").is_dir()]
        env["AMENT_PREFIX_PATH"] = _prepend_path_entries(prefix_entries, env.get("AMENT_PREFIX_PATH"))
        env["COLCON_PREFIX_PATH"] = _prepend_path_entries(prefix_entries, env.get("COLCON_PREFIX_PATH"))
        if lib_entries:
            env["LD_LIBRARY_PATH"] = _prepend_path_entries(lib_entries, env.get("LD_LIBRARY_PATH"))
    for plugin_dir in gazebo_system_plugin_lib_dirs(local_ros_prefix):
        env["IGN_GAZEBO_SYSTEM_PLUGIN_PATH"] = _prepend_path_entries(
            [str(plugin_dir)], env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH")
        )
        env["GZ_SIM_SYSTEM_PLUGIN_PATH"] = _prepend_path_entries(
            [str(plugin_dir)], env.get("GZ_SIM_SYSTEM_PLUGIN_PATH")
        )
    return env


def run_plugin_path_preflight(args: argparse.Namespace) -> int:
    payload = build_plugin_path_preflight(display=args.display, local_ros_prefix=args.local_ros_prefix)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ready_for_row_plugin_load"] else PLUGIN_PATH_PREFLIGHT_RC


def build_plugin_path_preflight(*, display: str, local_ros_prefix: Path | None) -> dict[str, object]:
    env = build_row_environment(display=display, local_ros_prefix=local_ros_prefix)
    ign_entries = _env_path_entries(env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH"))
    gz_entries = _env_path_entries(env.get("GZ_SIM_SYSTEM_PLUGIN_PATH"))
    required_plugins = {
        filename: {
            "ign_matches": _plugin_matches(ign_entries, filename),
            "gz_matches": _plugin_matches(gz_entries, filename),
        }
        for filename in ROS_CONTROL_SYSTEM_PLUGIN_FILENAMES
    }
    available = any(
        details["ign_matches"] or details["gz_matches"]
        for details in required_plugins.values()
    )
    return {
        "schema": PLUGIN_PATH_PREFLIGHT_SCHEMA,
        "generated_at": _now(),
        "mode": "offline_no_gazebo_plugin_path_preflight",
        "claim_tier": "visual_only",
        "target": "isolated_gazebo_row_controller_plugin_load_readiness",
        "ready_for_row_plugin_load": available,
        "blocker": None if available else "ros2_control_system_plugin_not_found_in_env_path",
        "display": display,
        "local_ros_prefix": str(local_ros_prefix.resolve()) if local_ros_prefix else "",
        "plugin_dirs": [str(path) for path in gazebo_system_plugin_lib_dirs(local_ros_prefix)],
        "env": {
            "IGN_GAZEBO_SYSTEM_PLUGIN_PATH": env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", ""),
            "GZ_SIM_SYSTEM_PLUGIN_PATH": env.get("GZ_SIM_SYSTEM_PLUGIN_PATH", ""),
        },
        "required_plugins": required_plugins,
        "starts_gazebo": False,
        "starts_xvfb": False,
        "starts_bridge": False,
        "live_robot_command_authorized": False,
        "tp_load_play_authorized": False,
        "urscript_send_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "forbidden_claim": (
            "Gazebo action readiness; row-local contact pair/log; native plus total Gazebo contact wrench; "
            "same-run dual-sensor binding; real bench/live contact"
        ),
    }


def run_action_readiness_preflight(args: argparse.Namespace) -> int:
    payload = build_action_readiness_preflight(display=args.display, local_ros_prefix=args.local_ros_prefix)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ready_for_static_action_readiness"] else ACTION_READINESS_PREFLIGHT_RC


def build_action_readiness_preflight(*, display: str, local_ros_prefix: Path | None) -> dict[str, object]:
    env = build_row_environment(display=display, local_ros_prefix=local_ros_prefix)
    plugin_preflight = build_plugin_path_preflight(display=display, local_ros_prefix=local_ros_prefix)
    package_checks = {
        package: _ros2_pkg_prefix_check(package, env)
        for package in ACTION_READINESS_REQUIRED_PACKAGES
    }
    interface_check = _ros2_interface_show_check(ACTION_READINESS_REQUIRED_INTERFACE, env)
    robot_description_check = build_robot_description_readiness_check(env=env)
    controller_yaml_check = build_controller_yaml_readiness_check()
    blockers = action_readiness_blockers(
        plugin_preflight=plugin_preflight,
        package_checks=package_checks,
        interface_check=interface_check,
        robot_description_check=robot_description_check,
        controller_yaml_check=controller_yaml_check,
    )
    return {
        "schema": ACTION_READINESS_PREFLIGHT_SCHEMA,
        "generated_at": _now(),
        "mode": "offline_no_gazebo_action_readiness_preflight",
        "claim_tier": "visual_only",
        "target": "isolated_gazebo_row_static_action_readiness",
        "ready_for_static_action_readiness": not blockers,
        "blockers": blockers,
        "display": display,
        "local_ros_prefix": str(local_ros_prefix.resolve()) if local_ros_prefix else "",
        "workspace_install_prefixes": [
            str(path) for path in workspace_install_prefix_dirs(local_ros_prefix=local_ros_prefix)
        ],
        "ros_prefixes": [str(path) for path in row_ros_prefix_dirs(local_ros_prefix=local_ros_prefix)],
        "env": {
            "AMENT_PREFIX_PATH": env.get("AMENT_PREFIX_PATH", ""),
            "COLCON_PREFIX_PATH": env.get("COLCON_PREFIX_PATH", ""),
            "LD_LIBRARY_PATH": env.get("LD_LIBRARY_PATH", ""),
            "IGN_GAZEBO_SYSTEM_PLUGIN_PATH": env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", ""),
            "GZ_SIM_SYSTEM_PLUGIN_PATH": env.get("GZ_SIM_SYSTEM_PLUGIN_PATH", ""),
        },
        "plugin_path_preflight": plugin_preflight,
        "package_prefix_checks": package_checks,
        "interface_check": interface_check,
        "robot_description_check": robot_description_check,
        "controller_yaml_check": controller_yaml_check,
        "starts_gazebo": False,
        "starts_xvfb": False,
        "starts_bridge": False,
        "live_robot_command_authorized": False,
        "tp_load_play_authorized": False,
        "urscript_send_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "forbidden_claim": (
            "Gazebo action server online readiness; row-local contact pair/log; native plus total Gazebo contact wrench; "
            "simulated FT validation; same-run dual-sensor binding; physical Gazebo contact physics; real bench/live contact"
        ),
    }


def action_readiness_blockers(
    *,
    plugin_preflight: dict[str, object],
    package_checks: dict[str, dict[str, object]],
    interface_check: dict[str, object],
    robot_description_check: dict[str, object],
    controller_yaml_check: dict[str, object],
) -> list[str]:
    blockers: list[str] = []
    if not plugin_preflight.get("ready_for_row_plugin_load"):
        blockers.append(str(plugin_preflight.get("blocker") or "plugin_path_preflight_not_ready"))
    for package, check in package_checks.items():
        if not check.get("available"):
            reason = check.get("stderr") or check.get("exception") or f"returncode={check.get('returncode')}"
            blockers.append(f"ros2_pkg_prefix:{package}:{reason}")
    if not interface_check.get("available"):
        reason = interface_check.get("stderr") or interface_check.get("exception") or (
            f"returncode={interface_check.get('returncode')}"
        )
        blockers.append(f"ros2_interface_show:{ACTION_READINESS_REQUIRED_INTERFACE}:{reason}")
    for name, check in (
        ("robot_description", robot_description_check),
        ("controller_yaml", controller_yaml_check),
    ):
        if not check.get("ready"):
            blockers.extend(f"{name}:{issue}" for issue in check.get("issues", ["not_ready"]))
    return blockers


def _ros2_pkg_prefix_check(package: str, env: dict[str, str]) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["ros2", "pkg", "prefix", package],
            cwd=WORKSPACE,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except FileNotFoundError as exc:
        return {"package": package, "available": False, "prefix": "", "returncode": None, "exception": str(exc)}
    except subprocess.TimeoutExpired as exc:
        return {"package": package, "available": False, "prefix": "", "returncode": None, "exception": str(exc)}
    prefix = completed.stdout.strip()
    return {
        "package": package,
        "available": completed.returncode == 0 and bool(prefix),
        "prefix": prefix if completed.returncode == 0 else "",
        "returncode": completed.returncode,
        "stderr": completed.stderr.strip(),
    }


def _ros2_interface_show_check(interface: str, env: dict[str, str]) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["ros2", "interface", "show", interface],
            cwd=WORKSPACE,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except FileNotFoundError as exc:
        return {"interface": interface, "available": False, "returncode": None, "exception": str(exc)}
    except subprocess.TimeoutExpired as exc:
        return {"interface": interface, "available": False, "returncode": None, "exception": str(exc)}
    return {
        "interface": interface,
        "available": completed.returncode == 0 and "trajectory_msgs/JointTrajectory" in completed.stdout,
        "returncode": completed.returncode,
        "stdout_excerpt": completed.stdout[:400],
        "stderr": completed.stderr.strip(),
    }


def build_robot_description_readiness_check(env: dict[str, str] | None = None) -> dict[str, object]:
    try:
        robot_description = _generate_sim_robot_description_with_env(env)
        root = ET.fromstring(robot_description)
    except Exception as exc:  # pragma: no cover - exercised by artifact path on local setup failures.
        return {
            "ready": False,
            "generated": False,
            "issues": [f"{type(exc).__name__}:{exc}"],
        }
    checks = {
        "uses_sim_hardware": "ign_ros2_control/IgnitionSystem" in robot_description,
        "has_ros2_control_plugin_filename": "libign_ros2_control-system.so" in robot_description,
        "excludes_real_ur_driver": "ur_robot_driver/URPositionHardwareInterface" not in robot_description,
        "has_eoat_visual_link": root.find(f"./link[@name='{gazebo.EOAT_VISUAL_LINK}']") is not None,
    }
    issues = [name for name, passed in checks.items() if not passed]
    return {
        "ready": not issues,
        "generated": True,
        "robot_name": root.attrib.get("name", ""),
        "checks": checks,
        "issues": issues,
    }


def _generate_sim_robot_description_with_env(env: dict[str, str] | None) -> str:
    if env is None:
        return gazebo.generate_sim_robot_description()
    original = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        return gazebo.generate_sim_robot_description()
    finally:
        os.environ.clear()
        os.environ.update(original)


def build_controller_yaml_readiness_check() -> dict[str, object]:
    try:
        payload = yaml.safe_load(gazebo.CONTROLLERS_YAML.read_text(encoding="utf-8"))
        manager = payload["controller_manager"]["ros__parameters"]
        controller = payload["joint_trajectory_controller"]["ros__parameters"]
    except Exception as exc:
        return {
            "ready": False,
            "path": str(gazebo.CONTROLLERS_YAML),
            "issues": [f"{type(exc).__name__}:{exc}"],
        }
    checks = {
        "has_joint_state_broadcaster": manager.get("joint_state_broadcaster", {}).get("type")
        == "joint_state_broadcaster/JointStateBroadcaster",
        "has_joint_trajectory_controller": manager.get("joint_trajectory_controller", {}).get("type")
        == "joint_trajectory_controller/JointTrajectoryController",
        "has_expected_joints": controller.get("joints") == gazebo.JOINT_NAMES,
        "uses_position_command_interface": controller.get("command_interfaces") == ["position"],
    }
    issues = [name for name, passed in checks.items() if not passed]
    return {
        "ready": not issues,
        "path": str(gazebo.CONTROLLERS_YAML),
        "checks": checks,
        "issues": issues,
    }


def _env_path_entries(value: str | None) -> list[str]:
    if not value:
        return []
    return [entry for entry in value.split(os.pathsep) if entry]


def _prepend_path_entries(values: list[str], current: str | None) -> str:
    entries: list[str] = []
    seen: set[str] = set()
    for value in [*values, *_env_path_entries(current)]:
        if value and value not in seen:
            entries.append(value)
            seen.add(value)
    return os.pathsep.join(entries)


def _plugin_matches(entries: list[str], filename: str) -> list[str]:
    return [str(Path(entry) / filename) for entry in entries if (Path(entry) / filename).is_file()]


def write_action_ready_failure_artifacts(
    case_dir: Path,
    *,
    run_dir: Path,
    stage: str,
    view: str,
    runner_rc: int,
    blocker: str,
    stage_simulated_ft_manifest_path: Path | None,
    step_status_audit_path: Path | None,
    observation_id: str,
    time_start: str,
    time_end: str,
    clock_source: str,
    trace_path: Path | None = None,
    row_failure_metadata: dict[str, object] | None = None,
    include_observation_manifest: bool,
    include_per_stage_audit: bool,
    correlation_tolerance_s: float,
) -> dict[str, object]:
    row_summary_path = case_dir / "row_summary.json"
    row = build_action_ready_failure_row(
        case_dir,
        run_dir=run_dir,
        stage=stage,
        view=view,
        runner_rc=runner_rc,
        blocker=blocker,
        trace_path=trace_path,
        row_failure_metadata=row_failure_metadata,
    )
    row = annotate_contact_stage_evidence_paths(
        row,
        case_dir=case_dir,
        stage=stage,
        include_observation_manifest=include_observation_manifest,
        include_per_stage_audit=include_per_stage_audit,
    )
    write_row_summary(row, row_summary_path)
    observation_path = None
    if stage in CONTACT_STAGES and include_observation_manifest:
        observation_path = write_stage_dual_sensor_observation_manifest(
            case_dir,
            stage=stage,
            row=row,
            row_summary_path=row_summary_path,
            stage_simulated_ft_manifest_path=stage_simulated_ft_manifest_path,
            step_status_audit_path=step_status_audit_path,
            observation_id=observation_id,
            time_start=time_start,
            time_end=time_end,
            clock_source=clock_source,
        )
    audit_path = None
    if stage in CONTACT_STAGES and include_per_stage_audit:
        audit_path = write_per_stage_dual_sensor_contact_audit(
            case_dir,
            stage=stage,
            row=row,
            row_summary_path=row_summary_path,
            stage_simulated_ft_manifest_path=stage_simulated_ft_manifest_path,
            step_status_audit_path=step_status_audit_path,
            same_run_observation_manifest_path=observation_path,
            correlation_tolerance_s=correlation_tolerance_s,
        )
    return {
        "row_summary": row,
        "row_summary_path": row_summary_path,
        "same_run_stage_dual_sensor_observation_manifest_path": str(observation_path) if observation_path else None,
        "per_stage_dual_sensor_contact_audit_path": str(audit_path) if audit_path else None,
    }


def run_action_ready_failure_backfill(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    case_dir = row_case_dir(run_dir, args.stage, args.view)
    if not case_dir.is_dir():
        raise FileNotFoundError(f"row case directory not found: {case_dir}")
    trace_path = args.trace_path or (case_dir / "command_trace.txt")
    trace_values = read_key_value_trace(trace_path)
    contact_pair_path = case_dir / "contact_capture" / "gazebo_contact_pair_log.json"
    stage_wrench_adapter_path = case_dir / "contact_capture" / STAGE_CONTACT_WRENCH_ADAPTER_FILENAME
    contact_pair_payload = _read_json(contact_pair_path, default={}) if contact_pair_path.is_file() else {}
    stage_wrench_payload = _read_json(stage_wrench_adapter_path, default={}) if stage_wrench_adapter_path.is_file() else {}
    time_window = first_time_window(contact_pair_payload, stage_wrench_payload)
    time_start = args.time_start or trace_values.get("row_started_at") or trace_values.get("started_at") or time_window.get("start") or _now()
    time_end = (
        args.time_end
        or trace_values.get("finished_at")
        or time_window.get("end")
        or newest_mtime_iso(
            [
                case_dir / "ros2_launch.log",
                contact_pair_path,
                stage_wrench_adapter_path,
                trace_path,
            ]
        )
        or _now()
    )
    clock_source = args.clock_source or time_window.get("clock_source") or args.stage_observation_clock_source
    observation_id = (
        args.observation_id
        or str(contact_pair_payload.get("observation_id") or "")
        or str(stage_wrench_payload.get("observation_id") or "")
        or f"{args.stage}-{args.view}-{time_start}"
    )
    blocker = args.blocker or trace_values.get("blocker") or infer_action_ready_failure_blocker(case_dir)
    stage_simulated_ft_manifest_path = args.stage_simulated_ft_manifest or first_existing_path(
        [
            run_dir / "simulated_ft_pack" / "step_simulated_ft_evidence_manifest.json",
            run_dir / "simulated_ft_runtime" / "canonical_simulated_ft_runtime_observation.json",
        ]
    )
    step_status_audit_path = args.step_status_audit or first_existing_path(
        [
            run_dir / "step_status" / "step_status_rnn_audit.json",
        ]
    )
    metadata = {
        "row_failure_backfilled": True,
        "row_failure_backfill_generated_at": _now(),
        "row_failure_backfill_source": "existing_allowed_offline_gazebo_row_artifacts",
        "row_failure_backfill_inputs": {
            "command_trace": str(trace_path),
            "contact_pair_log": str(contact_pair_path),
            "stage_contact_wrench_adapter": str(stage_wrench_adapter_path),
            "stage_simulated_ft_manifest": str(stage_simulated_ft_manifest_path)
            if stage_simulated_ft_manifest_path
            else None,
            "step_status_audit": str(step_status_audit_path) if step_status_audit_path else None,
        },
        "row_failure_backfill_forbidden_claim": (
            "new Gazebo evidence run; Gazebo action readiness; row-local physical contact success; "
            "same-run dual-sensor binding; real bench/live contact"
        ),
    }
    failure_paths = write_action_ready_failure_artifacts(
        case_dir,
        run_dir=run_dir,
        stage=args.stage,
        view=args.view,
        runner_rc=args.runner_rc,
        blocker=blocker,
        stage_simulated_ft_manifest_path=stage_simulated_ft_manifest_path,
        step_status_audit_path=step_status_audit_path,
        observation_id=observation_id,
        time_start=time_start,
        time_end=time_end,
        clock_source=clock_source,
        trace_path=trace_path,
        row_failure_metadata=metadata,
        include_observation_manifest=not args.disable_stage_observation_manifest,
        include_per_stage_audit=not args.disable_per_stage_contact_audit,
        correlation_tolerance_s=args.per_stage_contact_correlation_tolerance_s,
    )
    if trace_path:
        _write_trace(
            trace_path,
            {
                "backfilled_at": metadata["row_failure_backfill_generated_at"],
                "backfill_row_summary": str(failure_paths["row_summary_path"]),
                "backfill_same_run_stage_dual_sensor_observation_manifest": failure_paths.get(
                    "same_run_stage_dual_sensor_observation_manifest_path"
                ),
                "backfill_per_stage_dual_sensor_contact_audit": failure_paths.get(
                    "per_stage_dual_sensor_contact_audit_path"
                ),
                "backfill_blocker": blocker,
            },
        )
    print(json.dumps(failure_paths, indent=2, sort_keys=True, default=str))
    return 0


def read_key_value_trace(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def first_time_window(*payloads: dict[str, object]) -> dict[str, str]:
    for payload in payloads:
        time_window = payload.get("time_window") if isinstance(payload.get("time_window"), dict) else {}
        start = str(time_window.get("start") or "")
        end = str(time_window.get("end") or "")
        clock_source = str(time_window.get("clock_source") or "")
        if start or end or clock_source:
            return {"start": start, "end": end, "clock_source": clock_source}
    return {}


def newest_mtime_iso(paths: list[Path]) -> str | None:
    mtimes = [path.stat().st_mtime for path in paths if path.is_file()]
    if not mtimes:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(max(mtimes)))


def first_existing_path(paths: list[Path]) -> Path | None:
    return next((path for path in paths if path.is_file()), None)


def infer_action_ready_failure_blocker(case_dir: Path) -> str:
    launch_log = case_dir / "ros2_launch.log"
    if launch_log.is_file():
        source = launch_log.read_text(encoding="utf-8", errors="replace")
        if "Failed to load system plugin" in source and "libign_ros2_control-system.so" in source:
            return "action_ready_timeout_ros2_control_plugin_load_failure"
        if "/controller_manager/list_controllers" in source and "Could not contact service" in source:
            return "action_ready_timeout_controller_manager_unavailable"
    return "action_ready_timeout"


def build_action_ready_failure_row(
    case_dir: Path,
    *,
    run_dir: Path,
    stage: str,
    view: str,
    runner_rc: int,
    blocker: str,
    trace_path: Path | None = None,
    row_failure_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    contact_pair_log_path = case_dir / "contact_capture" / "gazebo_contact_pair_log.json"
    contact_pair_summary = summarize_contact_pair_log(contact_pair_log_path)
    stage_wrench_adapter_path = case_dir / "contact_capture" / STAGE_CONTACT_WRENCH_ADAPTER_FILENAME
    stage_wrench_adapter_summary = summarize_stage_contact_wrench_adapter(stage_wrench_adapter_path)
    visual_manifest_path = _visual_manifest_path(run_dir, stage)
    visual_manifest = _read_json(visual_manifest_path, default={}) if visual_manifest_path else {}
    surface_mesh_visual = (
        visual_manifest.get("surface_mesh_visual")
        if isinstance(visual_manifest.get("surface_mesh_visual"), dict)
        else {}
    )
    row = {
        "schema": "ur10e_gazebo_real_aligned_gui_matrix_row_v2",
        "stage": stage,
        "view": view,
        "view_role": _view_role(view),
        "runner_rc": runner_rc,
        "contact_stage": stage in CONTACT_STAGES,
        "action_accepted": False,
        "action_result_status": None,
        "action_result_error_code": None,
        "action_result_error_string": blocker,
        "action_success": False,
        "observed_motion": False,
        "blocker": blocker,
        "state_settled_success": False,
        "force_loop_success": False if stage in CONTACT_STAGES else None,
        "force_contact_source": gazebo.FORCE_CONTACT_SOURCE,
        "force_contact_physics_proven": False,
        "gazebo_contact_pair_log_path": str(contact_pair_log_path),
        "gazebo_contact_pair_log_captured": contact_pair_summary["captured"],
        "gazebo_contact_pair_log_evidence": contact_pair_summary["contact_pair_log_evidence"],
        "gazebo_contact_pair_log_row_count": contact_pair_summary["row_count"],
        "gazebo_contact_pair_matching_row_count": contact_pair_summary["matching_row_count"],
        "gazebo_contact_native_wrench_row_count": contact_pair_summary["native_wrench_row_count"],
        "gazebo_contact_pair_log_claim_tier": contact_pair_summary["claim_tier"],
        "gazebo_contact_pair_log_target_claim_tier": contact_pair_summary["target_claim_tier"],
        "gazebo_contact_pair_log_status": contact_pair_summary["status"],
        "gazebo_contact_pair_log_validation_issues": contact_pair_summary["validation_issues"],
        "gazebo_contact_wrench_contact_correlation_proven": False,
        "stage_contact_wrench_adapter_path": str(stage_wrench_adapter_path),
        "stage_contact_wrench_adapter_present": stage_wrench_adapter_summary["present"],
        "stage_contact_wrench_adapter_claim_tier": stage_wrench_adapter_summary["claim_tier"],
        "stage_contact_wrench_trace_written": stage_wrench_adapter_summary["trace_written"],
        "stage_contact_wrench_trace_path": stage_wrench_adapter_summary["trace_path"],
        "stage_contact_wrench_force_source": stage_wrench_adapter_summary["force_source"],
        "stage_total_contact_wrench_proven": stage_wrench_adapter_summary["total_contact_wrench_proven"],
        "stage_total_contact_wrench_row_count": stage_wrench_adapter_summary["total_contact_wrench_row_count"],
        "stage_contact_wrench_validation_issues": stage_wrench_adapter_summary["validation_issues"],
        "stage_contact_wrench_forbidden_claim": (
            "real bench/live contact; simulated_ft; per-stage physical Gazebo contact unless total contact "
            "wrench and correlation gates pass"
        ),
        "gazebo_contact_pair_log_forbidden_claim": "force_contact_physics_proven; total contact wrench; real bench/live contact",
        "gui_evidence_captured": False,
        "scripted_camera_evidence_captured": False,
        "visual_evidence_captured": False,
        "scripted_camera_final_png": str(case_dir / "scripted_camera_final.png"),
        "scripted_camera_capture_path": str(case_dir / "scripted_camera_capture.json"),
        "video_duration_s": None,
        "video_path": str(case_dir / "gui_recording.mp4"),
        "start_png": str(case_dir / "start_root.png"),
        "mid_png": str(case_dir / "mid_root.png"),
        "final_png": str(case_dir / "final_root.png"),
        "marker_manifest": str(case_dir / "marker" / "tcp_marker_manifest.json"),
        "marker_pose_count": 0,
        "marker_spawned": False,
        "marker_pose_source": None,
        "marker_pose_frame": None,
        "pose_source": None,
        "pose_frame": None,
        "matrix_summary": str(case_dir / "runner" / "matrix_summary.json"),
        "trace_path": str(trace_path) if trace_path else None,
        "visual_world_manifest": str(visual_manifest_path) if visual_manifest_path else None,
        "surface_frame": visual_manifest.get("surface_frame"),
        "surface": visual_manifest.get("surface"),
        "final_visual_pose_world": None,
        "contact_target_pose_world": visual_manifest.get("contact_target_pose_world"),
        "surface_tcp_sanity": visual_manifest.get("surface_tcp_sanity"),
        "observer_review_present": False,
        "observer_review_path": str(case_dir / "observer_review.json"),
        "observer_visual_review_status": "not_reached_action_ready",
        "robot_posture_visible": False,
        "eoat_tooling_visible": False,
        "tcp_marker_visible": False,
        "surface_path_visible": False,
        "robot_tool_surface_relation_visible": False,
        "model_composition_audit": None,
        "actual_eoat_mesh_visual_present": False,
        "actual_contact_surface_mesh_visual_present": bool(surface_mesh_visual.get("primary_visual_uses_real_mesh")),
        "actual_contact_surface_mesh_uri": surface_mesh_visual.get("mesh_uri"),
        "primitive_proxy_not_primary_visual": False,
        "primitive_proxy_not_main_visual_cue": False,
        "observer_level_demo_realism": False,
        "observer_visual_pass": False,
        "observer_visual_claim_tier": "visual_only",
        "observer_visual_forbidden_claim": (
            "physical Gazebo collision/contact physics; simulated_ft; real bench/live contact; "
            "observer demo acceptance"
        ),
        "row_failure_schema": "ur10e_gazebo_real_aligned_gui_matrix_action_ready_failure_v1",
        "row_failure_claim_tier": "visual_only",
        "row_failure_forbidden_claim": (
            "Gazebo action readiness; row-local contact pair/log; native plus total Gazebo contact wrench; "
            "same-run dual-sensor binding; real bench/live contact"
        ),
    }
    if row_failure_metadata:
        row.update(row_failure_metadata)
    return row


def build_row_summary(
    *,
    stage: str,
    view: str,
    case_dir: Path,
    run_dir: Path | None = None,
    runner_rc: int | None = None,
    video_duration_s: float | None = None,
    gui_config_path: Path | None = None,
) -> dict[str, object]:
    matrix_summary = case_dir / "runner" / "matrix_summary.json"
    payload = _read_json(matrix_summary)
    stage_payload = _select_stage_payload(payload, stage)
    execution = stage_payload.get("execution") or {}
    acceptance = stage_payload.get("acceptance") or {}
    marker_manifest = case_dir / "marker" / "tcp_marker_manifest.json"
    marker_payload = _read_json(marker_manifest, default={})
    contact_pair_log_path = case_dir / "contact_capture" / "gazebo_contact_pair_log.json"
    contact_pair_summary = summarize_contact_pair_log(contact_pair_log_path)
    stage_wrench_adapter_path = case_dir / "contact_capture" / STAGE_CONTACT_WRENCH_ADAPTER_FILENAME
    stage_wrench_adapter_summary = summarize_stage_contact_wrench_adapter(stage_wrench_adapter_path)
    visual_manifest_path = _visual_manifest_path(run_dir, stage) if run_dir is not None else None
    visual_manifest = _read_json(visual_manifest_path, default={}) if visual_manifest_path else {}
    observer_review_path = case_dir / "observer_review.json"
    observer_review = _read_json(observer_review_path, default={}) if observer_review_path.exists() else {}
    scene_introspection_dir = case_dir / "scene_introspection"
    scene_info_path = scene_introspection_dir / "scene_info.json"
    model_list_path = scene_introspection_dir / "model_list.txt"
    introspection_summary_path = scene_introspection_dir / "introspection_summary.json"
    introspection_summary = _read_json(introspection_summary_path, default={})
    marker_style = str(marker_payload.get("marker_style") or "")
    live_scene_content = summarize_live_scene_content(scene_introspection_dir, marker_style=marker_style)
    model_composition_audit = payload.get("model_composition_audit") if isinstance(payload.get("model_composition_audit"), dict) else {}
    surface_mesh_visual = (
        visual_manifest.get("surface_mesh_visual")
        if isinstance(visual_manifest.get("surface_mesh_visual"), dict)
        else {}
    )
    actual_eoat_mesh_visual_present = bool(model_composition_audit.get("actual_eoat_mesh_visual_present"))
    actual_contact_surface_mesh_visual_present = bool(surface_mesh_visual.get("primary_visual_uses_real_mesh"))

    force_loop = execution.get("force_loop") or stage_payload.get("force_loop") or {}
    force_success = execution.get("force_closed_loop")
    if stage not in CONTACT_STAGES:
        force_success = None
    timing_evidence = execution.get("timing_evidence") or stage_payload.get("timing_evidence") or {}
    settled_fraction = force_loop.get("settled_within_tolerance_fraction")
    state_settled_success = _state_settled_success(stage, execution, settled_fraction)

    start_png = case_dir / "start_root.png"
    mid_png = case_dir / "mid_root.png"
    final_png = case_dir / "final_root.png"
    scripted_camera_png = case_dir / "scripted_camera_final.png"
    scripted_camera_capture_path = case_dir / "scripted_camera_capture.json"
    scripted_camera_capture = _read_json(scripted_camera_capture_path, default={})
    video_path = case_dir / "gui_recording.mp4"
    gui_evidence = all(path.is_file() and path.stat().st_size > 0 for path in (start_png, mid_png, final_png, video_path))
    scripted_camera_evidence = bool(
        scripted_camera_capture.get("captured")
        and scripted_camera_png.is_file()
        and scripted_camera_png.stat().st_size > 0
    )
    visual_evidence = gui_evidence or scripted_camera_evidence
    marker_pose_count = _int_or_none(marker_payload.get("pose_count"))
    marker_pose_valid = bool(
        marker_payload.get("spawned")
        and marker_pose_count is not None
        and marker_pose_count > 0
        and marker_payload.get("pose_source") == POSE_SOURCE_ACTIVE_TCP
        and marker_payload.get("pose_frame") == gazebo.GAZEBO_WORLD_FRAME
    )
    review_present = bool(observer_review)
    review_source = str(
        observer_review.get("observer_visual_review_source")
        or observer_review.get("source")
        or ("pending_observer_review" if not review_present else "observer_review_v1")
    )
    config_clean = gui_config_is_clean(gui_config_path) if gui_config_path else False

    row: dict[str, object] = {
        "schema": "ur10e_gazebo_real_aligned_gui_matrix_row_v2",
        "stage": stage,
        "view": view,
        "view_role": _view_role(view),
        "runner_rc": runner_rc,
        "contact_stage": stage in CONTACT_STAGES,
        "action_accepted": execution.get("action_accepted"),
        "action_result_status": execution.get("result_status"),
        "action_result_error_code": execution.get("result_error_code"),
        "action_result_error_string": execution.get("result_error_string"),
        "action_success": execution.get("ok"),
        "observed_motion": execution.get("observed_motion"),
        "blocker": execution.get("blocker"),
        "result_timeout_s": execution.get("result_timeout_s"),
        "result_wait_elapsed_s": execution.get("result_wait_elapsed_s"),
        "timing_evidence": timing_evidence or None,
        "actual_vs_commanded_duration_ratio": timing_evidence.get("actual_vs_commanded_duration_ratio"),
        "inferred_speed_scale_from_action_result": timing_evidence.get("inferred_speed_scale_from_action_result"),
        "inferred_speed_scale_interpretation": timing_evidence.get("inferred_speed_scale_interpretation"),
        "duration_ratio_clock_domain": timing_evidence.get("duration_ratio_clock_domain"),
        "sim_time_real_time_factor_confound": timing_evidence.get("sim_time_real_time_factor_confound"),
        "rtf_or_controller_speed_unresolved": timing_evidence.get("rtf_or_controller_speed_unresolved"),
        "timing_root_cause_status": timing_evidence.get("timing_root_cause_status"),
        "controller_speed_scaling_measured": timing_evidence.get("controller_speed_scaling_measured"),
        "state_settled_success": state_settled_success,
        "force_loop_success": force_success,
        "force_contact_source": stage_payload.get("force_contact_source") or gazebo.FORCE_CONTACT_SOURCE,
        "force_contact_physics_proven": bool(stage_payload.get("force_contact_physics_proven") or acceptance.get("force_contact_physics_proven")),
        "gazebo_contact_pair_log_path": str(contact_pair_log_path),
        "gazebo_contact_pair_log_captured": contact_pair_summary["captured"],
        "gazebo_contact_pair_log_evidence": contact_pair_summary["contact_pair_log_evidence"],
        "gazebo_contact_pair_log_row_count": contact_pair_summary["row_count"],
        "gazebo_contact_pair_matching_row_count": contact_pair_summary["matching_row_count"],
        "gazebo_contact_native_wrench_row_count": contact_pair_summary["native_wrench_row_count"],
        "gazebo_contact_pair_log_claim_tier": contact_pair_summary["claim_tier"],
        "gazebo_contact_pair_log_target_claim_tier": contact_pair_summary["target_claim_tier"],
        "gazebo_contact_pair_log_status": contact_pair_summary["status"],
        "gazebo_contact_pair_log_validation_issues": contact_pair_summary["validation_issues"],
        "gazebo_contact_wrench_contact_correlation_proven": False,
        "stage_contact_wrench_adapter_path": str(stage_wrench_adapter_path),
        "stage_contact_wrench_adapter_present": stage_wrench_adapter_summary["present"],
        "stage_contact_wrench_adapter_claim_tier": stage_wrench_adapter_summary["claim_tier"],
        "stage_contact_wrench_trace_written": stage_wrench_adapter_summary["trace_written"],
        "stage_contact_wrench_trace_path": stage_wrench_adapter_summary["trace_path"],
        "stage_contact_wrench_force_source": stage_wrench_adapter_summary["force_source"],
        "stage_total_contact_wrench_proven": stage_wrench_adapter_summary["total_contact_wrench_proven"],
        "stage_total_contact_wrench_row_count": stage_wrench_adapter_summary["total_contact_wrench_row_count"],
        "stage_contact_wrench_validation_issues": stage_wrench_adapter_summary["validation_issues"],
        "stage_contact_wrench_forbidden_claim": "real bench/live contact; simulated_ft; per-stage physical Gazebo contact unless total contact wrench and correlation gates pass",
        "gazebo_contact_pair_log_forbidden_claim": "force_contact_physics_proven; total contact wrench; real bench/live contact",
        "force_loop_trace_written": acceptance.get("force_loop_trace_written"),
        "settled_force_within_tolerance_fraction": settled_fraction,
        "real_machine_traceability_status": TRACE_STATUS[stage],
        "gui_evidence_captured": gui_evidence,
        "scripted_camera_evidence_captured": scripted_camera_evidence,
        "visual_evidence_captured": visual_evidence,
        "scripted_camera_final_png": str(scripted_camera_png),
        "scripted_camera_capture_path": str(scripted_camera_capture_path),
        "scripted_camera_capture": scripted_camera_capture or None,
        "scripted_camera_topic": scripted_camera_capture.get("topic"),
        "scripted_camera_sha256": scripted_camera_capture.get("sha256"),
        "video_duration_s": video_duration_s,
        "video_path": str(video_path),
        "start_png": str(start_png),
        "mid_png": str(mid_png),
        "final_png": str(final_png),
        "marker_manifest": str(marker_manifest),
        "marker_style": marker_style,
        "marker_pose_count": marker_pose_count,
        "marker_spawned": marker_payload.get("spawned"),
        "marker_pose_source": marker_payload.get("pose_source"),
        "marker_pose_frame": marker_payload.get("pose_frame"),
        "pose_source": marker_payload.get("pose_source"),
        "pose_frame": marker_payload.get("pose_frame"),
        "marker_source_frame": marker_payload.get("source_frame"),
        "marker_tool_frame": marker_payload.get("tool_frame"),
        "marker_final_x_m": (marker_payload.get("final_pose") or {}).get("x_m"),
        "marker_final_y_m": (marker_payload.get("final_pose") or {}).get("y_m"),
        "marker_final_z_m": (marker_payload.get("final_pose") or {}).get("z_m"),
        "matrix_summary": str(matrix_summary),
        "git_provenance": payload.get("git_provenance"),
        "trace_path": stage_payload.get("trace_path"),
        "visual_world_manifest": str(visual_manifest_path) if visual_manifest_path else None,
        "surface_frame": visual_manifest.get("surface_frame"),
        "surface": visual_manifest.get("surface"),
        "final_visual_pose_world": visual_manifest.get("final_visual_pose_world"),
        "contact_target_pose_world": visual_manifest.get("contact_target_pose_world"),
        "surface_tcp_sanity": visual_manifest.get("surface_tcp_sanity"),
        "observer_review_present": review_present,
        "observer_review_path": str(observer_review_path),
        "scene_introspection_dir": str(scene_introspection_dir),
        "scene_introspection_summary_path": str(introspection_summary_path),
        "scene_info_path": str(scene_info_path),
        "scene_info_captured": _introspection_output_captured(introspection_summary, "scene_info.json"),
        "scene_model_list_path": str(model_list_path),
        "scene_model_list_captured": _introspection_output_captured(introspection_summary, "model_list.txt"),
        "live_scene_content": live_scene_content,
        "live_scene_content_branch": live_scene_content["branch"],
        "live_scene_enhanced_marker_visuals_present": live_scene_content["enhanced_marker_visuals_present"],
        "live_scene_tool0_eoat_visuals_present": live_scene_content["tool0_eoat_visuals_present"],
        "live_scene_eoat_affordance_visuals_present": live_scene_content["eoat_affordance_visuals_present"],
        "live_scene_actual_eoat_mesh_visuals_present": live_scene_content[
            "actual_eoat_mesh_visuals_present"
        ],
        "live_scene_actual_contact_surface_mesh_visuals_present": live_scene_content[
            "actual_contact_surface_mesh_visuals_present"
        ],
        "live_scene_marker_visual_role": live_scene_content["marker_visual_role"],
        "model_composition_audit": model_composition_audit or None,
        "actual_eoat_mesh_visual_present": actual_eoat_mesh_visual_present,
        "actual_eoat_mesh_visual_name": model_composition_audit.get("eoat_primary_visual_mesh_name"),
        "actual_eoat_mesh_visual_uri": model_composition_audit.get("eoat_primary_visual_mesh_uri"),
        "eoat_primitive_visual_remnants": model_composition_audit.get("eoat_primitive_visual_remnants", []),
        "surface_mesh_visual": surface_mesh_visual or None,
        "actual_contact_surface_mesh_visual_present": actual_contact_surface_mesh_visual_present,
        "actual_contact_surface_mesh_uri": surface_mesh_visual.get("mesh_uri"),
        "primitive_proxy_not_primary_visual": bool(
            actual_eoat_mesh_visual_present
            and actual_contact_surface_mesh_visual_present
            and live_scene_content["actual_eoat_mesh_visuals_present"]
            and live_scene_content["actual_contact_surface_mesh_visuals_present"]
        ),
        "primitive_proxy_not_main_visual_cue": bool(
            visual_evidence
            and _review_flag(observer_review, "primitive_proxy_not_main_visual_cue")
            and marker_style == "minimal_tcp_dot"
            and live_scene_content["actual_eoat_mesh_visuals_present"]
            and live_scene_content["actual_contact_surface_mesh_visuals_present"]
        ),
        "observer_level_demo_realism": bool(
            visual_evidence and _review_flag(observer_review, "observer_level_demo_realism")
        ),
        "observer_visual_review_source": review_source,
        "observer_visual_notes": observer_review.get("notes"),
        "observer_visual_reviewed_at": observer_review.get("reviewed_at"),
        "observer_visual_review_status": "row_review_present" if review_present else "pending_observer_review",
        "robot_posture_visible": bool(visual_evidence and _review_flag(observer_review, "robot_posture_visible")),
        "eoat_tooling_visible": bool(visual_evidence and _review_flag(observer_review, "eoat_tooling_visible")),
        "tcp_marker_visible": bool(visual_evidence and marker_pose_valid and _review_flag(observer_review, "tcp_marker_visible")),
        "surface_path_visible": bool(visual_evidence and _review_flag(observer_review, "surface_path_visible")),
        "robot_tool_surface_relation_visible": bool(
            visual_evidence and marker_pose_valid and _review_flag(observer_review, "robot_tool_surface_relation_visible")
        ),
        "obstructive_ui_panels_absent": bool(
            scripted_camera_evidence or config_clean or _review_flag(observer_review, "obstructive_ui_panels_absent")
        ),
        "clean_scene_capture": bool(scripted_camera_evidence or config_clean or _review_flag(observer_review, "clean_scene_capture")),
        "context_ui_allowed": bool(view == "context_overview" and _review_flag(observer_review, "context_ui_allowed")),
        "machine_visual_prerequisites": {
            "gui_evidence_captured": gui_evidence,
            "scripted_camera_evidence_captured": scripted_camera_evidence,
            "visual_evidence_captured": visual_evidence,
            "scripted_camera_topic": scripted_camera_capture.get("topic"),
            "marker_pose_valid": marker_pose_valid,
            "marker_pose_count": marker_pose_count,
            "marker_pose_source": marker_payload.get("pose_source"),
            "marker_pose_frame": marker_payload.get("pose_frame"),
            "clean_gui_config": config_clean,
            "visual_world_manifest_present": bool(visual_manifest),
        },
    }
    return gazebo.populate_observer_visual_pass(row)


def summarize_live_scene_content(scene_introspection_dir: Path, *, marker_style: str = "") -> dict[str, object]:
    pose_info_path = scene_introspection_dir / "pose_info.json"
    pose_names, pose_by_name = _load_pose_info_names(pose_info_path)
    if not pose_names:
        return {
            "schema": "ur10e_gazebo_live_scene_content_v1",
            "pose_info_path": str(pose_info_path),
            "pose_info_captured": False,
            "branch": "pose_info_missing_or_empty",
            "does_not_override_observer_visual_gate": True,
            "required_enhanced_marker_visuals": sorted(ENHANCED_MARKER_VISUAL_NAMES),
            "present_enhanced_marker_visuals": [],
            "missing_enhanced_marker_visuals": sorted(ENHANCED_MARKER_VISUAL_NAMES),
            "enhanced_marker_visuals_present": False,
            "required_actual_eoat_mesh_visuals": sorted(ACTUAL_EOAT_MESH_VISUAL_NAMES),
            "present_actual_eoat_mesh_visuals": [],
            "missing_actual_eoat_mesh_visuals": sorted(ACTUAL_EOAT_MESH_VISUAL_NAMES),
            "actual_eoat_mesh_visuals_present": False,
            "required_actual_contact_surface_mesh_visuals": sorted(ACTUAL_CONTACT_SURFACE_MESH_VISUAL_NAMES),
            "present_actual_contact_surface_mesh_visuals": [],
            "missing_actual_contact_surface_mesh_visuals": sorted(ACTUAL_CONTACT_SURFACE_MESH_VISUAL_NAMES),
            "actual_contact_surface_mesh_visuals_present": False,
            "marker_visual_role": _marker_visual_role(marker_style),
            "required_tool0_eoat_visuals": sorted(gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES),
            "present_tool0_eoat_visuals": [],
            "missing_tool0_eoat_visuals": sorted(gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES),
            "tool0_eoat_visuals_present": False,
            "required_eoat_affordance_visuals": sorted(gazebo.EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES),
            "present_eoat_affordance_visuals": [],
            "missing_eoat_affordance_visuals": sorted(gazebo.EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES),
            "eoat_affordance_visuals_present": False,
            "active_tcp_marker_model_present": False,
            "wrist_3_link_present": False,
            "active_tcp_marker_pose": None,
            "wrist_3_link_pose": None,
        }

    present_marker = _present_name_fragments(pose_names, ENHANCED_MARKER_VISUAL_NAMES)
    present_actual_eoat_mesh = _present_name_fragments(pose_names, ACTUAL_EOAT_MESH_VISUAL_NAMES)
    present_actual_surface_mesh = _present_name_fragments(pose_names, ACTUAL_CONTACT_SURFACE_MESH_VISUAL_NAMES)
    present_tool0 = _present_name_fragments(pose_names, gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES)
    present_eoat = _present_name_fragments(pose_names, gazebo.EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES)
    missing_marker = sorted(ENHANCED_MARKER_VISUAL_NAMES - set(present_marker))
    missing_actual_eoat_mesh = sorted(ACTUAL_EOAT_MESH_VISUAL_NAMES - set(present_actual_eoat_mesh))
    missing_actual_surface_mesh = sorted(ACTUAL_CONTACT_SURFACE_MESH_VISUAL_NAMES - set(present_actual_surface_mesh))
    missing_tool0 = sorted(gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES - set(present_tool0))
    missing_eoat = sorted(gazebo.EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES - set(present_eoat))
    enhanced_marker_present = bool(ENHANCED_MARKER_VISUAL_NAMES) and not missing_marker
    actual_eoat_mesh_present = bool(ACTUAL_EOAT_MESH_VISUAL_NAMES) and not missing_actual_eoat_mesh
    actual_surface_mesh_present = bool(ACTUAL_CONTACT_SURFACE_MESH_VISUAL_NAMES) and not missing_actual_surface_mesh
    tool0_present = bool(gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES) and not missing_tool0
    eoat_present = bool(gazebo.EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES) and not missing_eoat
    if actual_eoat_mesh_present and actual_surface_mesh_present and marker_style == "minimal_tcp_dot":
        branch = "actual_meshes_present_with_auxiliary_tcp_dot"
    elif actual_eoat_mesh_present and actual_surface_mesh_present and enhanced_marker_present:
        branch = "actual_meshes_present_but_enhanced_marker_proxy_requires_observer_downgrade"
    elif enhanced_marker_present and not (actual_eoat_mesh_present and actual_surface_mesh_present):
        branch = "enhanced_marker_present_but_actual_meshes_incomplete_in_live_ecm"
    elif actual_eoat_mesh_present or actual_surface_mesh_present or tool0_present or eoat_present:
        branch = "partial_actual_mesh_live_ecm"
    else:
        branch = "actual_meshes_missing_from_live_ecm"

    return {
        "schema": "ur10e_gazebo_live_scene_content_v1",
        "pose_info_path": str(pose_info_path),
        "pose_info_captured": True,
        "branch": branch,
        "does_not_override_observer_visual_gate": True,
        "required_enhanced_marker_visuals": sorted(ENHANCED_MARKER_VISUAL_NAMES),
        "present_enhanced_marker_visuals": present_marker,
        "missing_enhanced_marker_visuals": missing_marker,
        "enhanced_marker_visuals_present": enhanced_marker_present,
        "required_actual_eoat_mesh_visuals": sorted(ACTUAL_EOAT_MESH_VISUAL_NAMES),
        "present_actual_eoat_mesh_visuals": present_actual_eoat_mesh,
        "missing_actual_eoat_mesh_visuals": missing_actual_eoat_mesh,
        "actual_eoat_mesh_visuals_present": actual_eoat_mesh_present,
        "required_actual_contact_surface_mesh_visuals": sorted(ACTUAL_CONTACT_SURFACE_MESH_VISUAL_NAMES),
        "present_actual_contact_surface_mesh_visuals": present_actual_surface_mesh,
        "missing_actual_contact_surface_mesh_visuals": missing_actual_surface_mesh,
        "actual_contact_surface_mesh_visuals_present": actual_surface_mesh_present,
        "marker_visual_role": _marker_visual_role(marker_style),
        "required_tool0_eoat_visuals": sorted(gazebo.TOOL0_EOAT_VIEWER_VISUAL_NAMES),
        "present_tool0_eoat_visuals": present_tool0,
        "missing_tool0_eoat_visuals": missing_tool0,
        "tool0_eoat_visuals_present": tool0_present,
        "required_eoat_affordance_visuals": sorted(gazebo.EOAT_VIEWER_AFFORDANCE_VISUAL_NAMES),
        "present_eoat_affordance_visuals": present_eoat,
        "missing_eoat_affordance_visuals": missing_eoat,
        "eoat_affordance_visuals_present": eoat_present,
        "active_tcp_marker_model_present": "active_tcp_marker" in pose_by_name,
        "wrist_3_link_present": "wrist_3_link" in pose_by_name,
        "active_tcp_marker_pose": pose_by_name.get("active_tcp_marker"),
        "wrist_3_link_pose": pose_by_name.get("wrist_3_link"),
        "note": "Entity names and poses prove live ECM content only; observer_visual_pass still requires human-visible coherent render evidence.",
    }


def _marker_visual_role(marker_style: str) -> str:
    if marker_style == "minimal_tcp_dot":
        return "auxiliary_tcp_pose_reference_only"
    if marker_style in {"debug", "observer_subtle"}:
        return "primitive_proxy_marker_not_acceptance_evidence"
    return "unknown_marker_style_not_acceptance_evidence"


def _load_pose_info_names(path: Path) -> tuple[list[str], dict[str, dict[str, object]]]:
    payload = _read_pose_info_payload(path) if path.exists() else {}
    names: list[str] = []
    pose_by_name: dict[str, dict[str, object]] = {}
    for pose in payload.get("pose", []) if isinstance(payload, dict) else []:
        if not isinstance(pose, dict):
            continue
        name = str(pose.get("name") or "")
        if not name:
            continue
        names.append(name)
        pose_by_name[name] = {
            "id": pose.get("id"),
            "position": pose.get("position"),
            "orientation": pose.get("orientation"),
        }
    return names, pose_by_name


def _read_pose_info_payload(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {}
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict) and isinstance(candidate.get("pose"), list):
                payload = candidate
    return payload if isinstance(payload, dict) else {}


def _present_name_fragments(pose_names: list[str], required: frozenset[str]) -> list[str]:
    return sorted(fragment for fragment in required if any(fragment in name for name in pose_names))


def gazebo_system_plugin_lib_dirs(local_ros_prefix: Path | None) -> list[Path]:
    dirs: list[Path] = []
    if local_ros_prefix is not None:
        dirs.append(local_ros_prefix.resolve() / "lib")
    for candidate in (Path("/opt/ros/humble/lib"),):
        if (candidate / "libign_ros2_control-system.so").is_file() or (
            candidate / "libgz_ros2_control-system.so"
        ).is_file():
            dirs.append(candidate)
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in dirs:
        key = str(path)
        if key not in seen:
            deduped.append(path)
            seen.add(key)
    return deduped


def acquire_visible_gazebo_row_lock(lock_path: Path) -> Any | None:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.seek(0)
    handle.truncate()
    handle.write(
        json.dumps(
            {
                "schema": "ur10e_visible_gazebo_row_lock_owner_v1",
                "pid": os.getpid(),
                "started_at": _now(),
                "lock_path": str(lock_path),
                "claim_tier": "visual_only",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    handle.flush()
    return handle


def release_visible_gazebo_row_lock(handle: Any | None) -> None:
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def existing_visible_gazebo_processes() -> list[dict[str, object]]:
    completed = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,etimes=,stat=,args="],
        capture_output=True,
        text=True,
        check=False,
    )
    return visible_gazebo_process_records_from_ps(completed.stdout, current_pid=os.getpid())


def visible_gazebo_process_records_from_ps(ps_output: str, *, current_pid: int) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for raw_line in ps_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=4)
        if len(parts) < 5:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            elapsed_s = int(parts[2])
        except ValueError:
            continue
        if pid == current_pid:
            continue
        cmd = parts[4]
        role = visible_gazebo_process_role(cmd)
        if role is None:
            continue
        records.append(
            {
                "pid": pid,
                "ppid": ppid,
                "elapsed_s": elapsed_s,
                "stat": parts[3],
                "role": role,
                "cmd": cmd,
            }
        )
    return records


def visible_gazebo_process_role(cmd: str) -> str | None:
    executable = cmd.split(maxsplit=1)[0] if cmd.strip() else ""
    executable_name = Path(executable).name
    if (
        "run_gazebo_gui_matrix_row.py" in cmd
        and " row " in f" {cmd} "
        and (executable_name.startswith("python") or executable_name == "run_gazebo_gui_matrix_row.py")
    ):
        return "gui_matrix_row_runner"
    if "ur10e_gazebo_matrix.launch.py" in cmd and "headless:=false" in cmd:
        return "ros2_visible_gazebo_launch"
    if "ign gazebo --gui-config" in cmd:
        return "ign_visible_gazebo_parent"
    if "gz sim" in cmd and (" --gui-config" in cmd or " -g" in cmd):
        return "gz_visible_gazebo_parent"
    if "ign gazebo gui" in cmd or "gz sim gui" in cmd:
        return "gazebo_gui_process"
    if "x11grab" in cmd and "gui_recording.mp4" in cmd:
        return "gui_capture_process"
    return None


def write_visible_gazebo_overlap_preflight(
    case_dir: Path,
    *,
    stage: str,
    view: str,
    run_dir: Path,
    display: str,
    processes: list[dict[str, object]],
) -> dict[str, object]:
    path = case_dir / "visible_gazebo_overlap_preflight.json"
    payload: dict[str, object] = {
        "schema": VISIBLE_GAZEBO_OVERLAP_SCHEMA,
        "generated_at": _now(),
        "stage": stage,
        "view": view,
        "run_dir": str(run_dir),
        "display": display,
        "process_count": len(processes),
        "processes": processes,
        "blocker": "existing_visible_gazebo_processes_present",
        "action": "refused_to_start_new_visible_gazebo_row",
        "claim_tier": "visual_only",
        "target_claim_tier": "post-checkpoint visible Gazebo GUI row",
        "allowed_claim": "preflight_blocker_evidence_only",
        "forbidden_claim": "observer-level visual demo success; simulated_ft; physical Gazebo collision/contact physics; real bench/live contact",
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "path": str(path),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def write_visible_gazebo_lock_preflight(
    case_dir: Path,
    *,
    stage: str,
    view: str,
    run_dir: Path,
    display: str,
    lock_path: Path,
) -> dict[str, object]:
    path = case_dir / "visible_gazebo_lock_preflight.json"
    payload: dict[str, object] = {
        "schema": VISIBLE_GAZEBO_LOCK_SCHEMA,
        "generated_at": _now(),
        "stage": stage,
        "view": view,
        "run_dir": str(run_dir),
        "display": display,
        "lock_path": str(lock_path),
        "pid": os.getpid(),
        "blocker": "visible_gazebo_row_lock_held",
        "action": "refused_to_start_new_visible_gazebo_row",
        "claim_tier": "visual_only",
        "target_claim_tier": "post-checkpoint visible Gazebo GUI row",
        "allowed_claim": "lock_preflight_blocker_evidence_only",
        "forbidden_claim": "observer-level visual demo success; simulated_ft; physical Gazebo collision/contact physics; real bench/live contact",
        "live_robot_command_authorized": False,
        "bridge_start_authorized": False,
        "urscript_send_authorized": False,
        "tp_load_play_authorized": False,
        "zero_ftsensor_authorized": False,
        "payload_tcp_safety_writes_authorized": False,
        "path": str(path),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def visible_gazebo_overlap_preflight_for_row(run_dir: Path, stage: str, view: str) -> dict[str, object] | None:
    path = row_case_dir(run_dir, stage, view) / "visible_gazebo_overlap_preflight.json"
    if not path.is_file():
        return None
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {
            "schema": VISIBLE_GAZEBO_OVERLAP_SCHEMA,
            "path": str(path),
            "blocker": "visible_gazebo_overlap_preflight_unreadable",
            "claim_tier": "visual_only",
            "action": "row_summary_missing_preflight_unreadable",
            "process_count": 0,
        }
    return payload if isinstance(payload, dict) else None


def visible_gazebo_lock_preflight_for_row(run_dir: Path, stage: str, view: str) -> dict[str, object] | None:
    path = row_case_dir(run_dir, stage, view) / "visible_gazebo_lock_preflight.json"
    if not path.is_file():
        return None
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return {
            "schema": VISIBLE_GAZEBO_LOCK_SCHEMA,
            "path": str(path),
            "blocker": "visible_gazebo_lock_preflight_unreadable",
            "claim_tier": "visual_only",
            "action": "row_summary_missing_lock_preflight_unreadable",
            "lock_path": "",
        }
    return payload if isinstance(payload, dict) else None


def _introspection_output_captured(summary: dict[str, object], key: str) -> bool:
    item = summary.get(key) if isinstance(summary, dict) else None
    if not isinstance(item, dict):
        return False
    return bool(item.get("returncode") == 0 and not item.get("timed_out") and int(item.get("bytes") or 0) > 0)


def write_row_summary(row: dict[str, object], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def build_visual_audit_summary(
    run_dir: Path,
    *,
    stages: tuple[str, ...] = STAGES,
    views: tuple[str, ...] = VIEWS,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    missing_rows: list[dict[str, str]] = []
    for stage in stages:
        for view in views:
            row_path = row_case_dir(run_dir, stage, view) / "row_summary.json"
            if row_path.exists():
                rows.append(_read_json(row_path))
            else:
                missing = {"stage": stage, "view": view, "row_summary": str(row_path)}
                preflight = visible_gazebo_overlap_preflight_for_row(run_dir, stage, view)
                if preflight is not None:
                    missing["visible_gazebo_overlap_preflight"] = str(
                        row_case_dir(run_dir, stage, view) / "visible_gazebo_overlap_preflight.json"
                    )
                    missing["blocker"] = str(preflight.get("blocker") or "")
                    missing["claim_tier"] = str(preflight.get("claim_tier") or "")
                    missing["action"] = str(preflight.get("action") or "")
                    missing["process_count"] = str(preflight.get("process_count") or 0)
                lock_preflight = visible_gazebo_lock_preflight_for_row(run_dir, stage, view)
                if lock_preflight is not None:
                    missing["visible_gazebo_lock_preflight"] = str(
                        row_case_dir(run_dir, stage, view) / "visible_gazebo_lock_preflight.json"
                    )
                    missing["blocker"] = str(lock_preflight.get("blocker") or "")
                    missing["claim_tier"] = str(lock_preflight.get("claim_tier") or "")
                    missing["action"] = str(lock_preflight.get("action") or "")
                    missing["lock_path"] = str(lock_preflight.get("lock_path") or "")
                missing_rows.append(missing)

    expected = len(stages) * len(views)
    observer_pass_rows = [row for row in rows if row.get("observer_visual_pass") is True]
    observer_fail_rows = [row for row in rows if row.get("observer_visual_pass") is not True]
    action_success_rows = [row for row in rows if row.get("action_success") is True]
    gui_evidence_rows = [row for row in rows if row.get("gui_evidence_captured") is True]
    timing_rows = [row for row in rows if row.get("timing_evidence")]
    actual_timing_rows = [row for row in rows if row.get("actual_vs_commanded_duration_ratio") is not None]
    contact_rows = [row for row in rows if row.get("contact_stage") is True]
    contact_force_success_rows = [row for row in contact_rows if row.get("force_loop_success") is True]
    contact_pair_evidence_rows = [row for row in contact_rows if row.get("gazebo_contact_pair_log_evidence") is True]
    native_wrench_component_rows = [
        row for row in contact_rows if int(row.get("gazebo_contact_native_wrench_row_count") or 0) > 0
    ]
    stage_total_wrench_rows = [row for row in contact_rows if row.get("stage_total_contact_wrench_proven") is True]
    overlap_preflight_rows = [row for row in missing_rows if row.get("visible_gazebo_overlap_preflight")]
    lock_preflight_rows = [row for row in missing_rows if row.get("visible_gazebo_lock_preflight")]
    all_expected_rows_present = not missing_rows and len(rows) == expected
    all_observer_pass = all_expected_rows_present and len(observer_pass_rows) == expected
    payload = {
        "schema": "ur10e_gazebo_real_aligned_visual_audit_summary_v2",
        "run_dir": str(run_dir),
        "stages": list(stages),
        "views": list(views),
        "expected_rows": expected,
        "row_count": len(rows),
        "missing_row_count": len(missing_rows),
        "missing_rows": missing_rows,
        "visible_gazebo_overlap_preflight_count": len(overlap_preflight_rows),
        "visible_gazebo_overlap_preflight_rows": overlap_preflight_rows,
        "visible_gazebo_lock_preflight_count": len(lock_preflight_rows),
        "visible_gazebo_lock_preflight_rows": lock_preflight_rows,
        "all_expected_rows_present": all_expected_rows_present,
        "observer_visual_pass_count": len(observer_pass_rows),
        "observer_visual_fail_count": len(observer_fail_rows) + len(missing_rows),
        "all_rows_observer_visual_pass": all_observer_pass,
        "all_rows_action_success": all_expected_rows_present and len(action_success_rows) == expected,
        "all_rows_gui_evidence_captured": all_expected_rows_present and len(gui_evidence_rows) == expected,
        "timing_evidence_row_count": len(timing_rows),
        "actual_vs_commanded_timing_row_count": len(actual_timing_rows),
        "representative_timing_rows": _representative_timing_rows(actual_timing_rows),
        "contact_row_count": len(contact_rows),
        "contact_rows_force_loop_success_count": len(contact_force_success_rows),
        "all_contact_rows_force_loop_success": bool(contact_rows) and len(contact_force_success_rows) == len(contact_rows),
        "contact_rows_contact_pair_log_evidence_count": len(contact_pair_evidence_rows),
        "all_contact_rows_contact_pair_log_evidence": bool(contact_rows) and len(contact_pair_evidence_rows) == len(contact_rows),
        "contact_rows_native_wrench_component_count": len(native_wrench_component_rows),
        "contact_rows_stage_total_wrench_proven_count": len(stage_total_wrench_rows),
        "visual_review_status": "per_row_observer_visual_pass" if all_observer_pass else "per_row_observer_visual_failed_or_missing",
        "representative_failing_images": _representative_failing_images(observer_fail_rows),
        "rows": rows,
    }
    return payload


def write_visual_audit_summary(summary: dict[str, object], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def summarize_contact_pair_log(path: Path) -> dict[str, object]:
    base: dict[str, object] = {
        "path": str(path),
        "captured": False,
        "contact_pair_log_evidence": False,
        "row_count": 0,
        "matching_row_count": 0,
        "native_wrench_row_count": 0,
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "status": "missing",
        "validation_issues": ["contact_pair_log:missing"],
    }
    if not path.is_file():
        return base
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            **base,
            "captured": True,
            "status": "unreadable",
            "validation_issues": [f"contact_pair_log:unreadable:{type(exc).__name__}"],
        }

    rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
    parse_issues = payload.get("parse_issues") if isinstance(payload.get("parse_issues"), list) else []
    matching_rows = [row for row in rows if _row_has_eoat_surface_contact_pair(row)]
    native_wrench_rows = [row for row in matching_rows if isinstance(row.get("native_gazebo_contact_wrench"), dict)]
    validation_issues: list[str] = []
    if parse_issues:
        validation_issues.append("contact_pair_log:parse_issues")
    if not rows:
        validation_issues.append("contact_pair_log:no_rows")
    if not matching_rows:
        validation_issues.append("contact_pair_log:no_eoat_surface_pair")
    return {
        **base,
        "captured": True,
        "contact_pair_log_evidence": bool(rows and matching_rows and not parse_issues),
        "row_count": len(rows),
        "matching_row_count": len(matching_rows),
        "native_wrench_row_count": len(native_wrench_rows),
        "claim_tier": payload.get("claim_tier") or "visual_only",
        "target_claim_tier": payload.get("target_claim_tier") or "physical Gazebo collision/contact physics",
        "status": "component_evidence_present" if rows and matching_rows and not parse_issues else "not_ready",
        "validation_issues": validation_issues,
    }


def summarize_stage_contact_wrench_adapter(path: Path) -> dict[str, object]:
    base: dict[str, object] = {
        "path": str(path),
        "present": False,
        "claim_tier": "visual_only",
        "trace_written": False,
        "trace_path": None,
        "force_source": None,
        "total_contact_wrench_proven": False,
        "total_contact_wrench_row_count": 0,
        "validation_issues": ["stage_contact_wrench_adapter:missing"],
    }
    if not path.is_file():
        return base
    try:
        payload = _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return {
            **base,
            "present": True,
            "validation_issues": [f"stage_contact_wrench_adapter:unreadable:{type(exc).__name__}"],
        }

    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    total_blockers = payload.get("total_contact_wrench_blockers")
    if not isinstance(total_blockers, list):
        total_blockers = []
    trace = payload.get("wrench_trace") if isinstance(payload.get("wrench_trace"), dict) else {}
    trace_rows = trace.get("rows") if isinstance(trace.get("rows"), list) else []
    trace_path = payload.get("wrench_trace_path")
    total_contact_wrench_row_count = int(payload.get("total_contact_wrench_row_count") or 0)
    verified_native_wrench_row_count = int(payload.get("verified_native_wrench_row_count") or 0)
    validation_issues: list[str] = []
    if payload.get("schema") != wrench_adapter.REPORT_SCHEMA:
        validation_issues.append("stage_contact_wrench_adapter.schema:unsupported_or_missing")
    if payload.get("trace_written") is not True:
        validation_issues.append("stage_contact_wrench_adapter.trace:not_written")
    if payload.get("total_contact_wrench_proven") is not True:
        validation_issues.append("stage_total_contact_wrench:not_proven")
    if payload.get("force_source") != "gazebo_contact":
        validation_issues.append("stage_contact_wrench_adapter.force_source:not_gazebo_contact")
    if not trace_path:
        validation_issues.append("stage_contact_wrench_adapter.trace_path:missing")
    if verified_native_wrench_row_count <= 0:
        validation_issues.append("stage_contact_wrench_adapter.verified_native_wrench_row_count:zero")
    if total_contact_wrench_row_count <= 0:
        validation_issues.append("stage_contact_wrench_adapter.total_contact_wrench_row_count:zero")
    if not trace_rows:
        validation_issues.append("stage_contact_wrench_adapter.trace_rows:missing")
    valid_trace_rows = [row for row in trace_rows if isinstance(row, dict) and _valid_stage_adapter_trace_row(row)]
    if trace_rows and not valid_trace_rows:
        validation_issues.append("stage_contact_wrench_adapter.trace_rows:no_valid_total_contact_wrench_row")
    validation_issues.extend(str(item) for item in blockers)
    validation_issues.extend(str(item) for item in total_blockers)
    total_contact_wrench_proven = bool(
        payload.get("schema") == wrench_adapter.REPORT_SCHEMA
        and payload.get("trace_written") is True
        and payload.get("total_contact_wrench_proven") is True
        and payload.get("wrench_aggregation_policy") == "total_contact_wrench"
        and payload.get("force_source") == "gazebo_contact"
        and trace_path
        and verified_native_wrench_row_count > 0
        and total_contact_wrench_row_count > 0
        and valid_trace_rows
        and not blockers
        and not total_blockers
    )
    return {
        **base,
        "present": True,
        "claim_tier": payload.get("claim_tier") or "visual_only",
        "trace_written": bool(payload.get("trace_written")),
        "trace_path": trace_path,
        "force_source": payload.get("force_source"),
        "total_contact_wrench_proven": total_contact_wrench_proven,
        "total_contact_wrench_row_count": total_contact_wrench_row_count,
        "validation_issues": validation_issues,
    }


def _valid_stage_adapter_trace_row(row: dict[str, object]) -> bool:
    header = row.get("header") if isinstance(row.get("header"), dict) else {}
    try:
        normal_load_n = float(row.get("normal_load_n") or 0.0)
    except (TypeError, ValueError):
        normal_load_n = 0.0
    flags = row.get("diagnostic_flags") if isinstance(row.get("diagnostic_flags"), list) else []
    return bool(
        header.get("stamp_s") is not None
        and header.get("frame_id")
        and row.get("source") == "gazebo_contact"
        and row.get("status") == "valid"
        and row.get("contact_state") == "contact"
        and row.get("baseline_policy")
        and normal_load_n > 0.0
        and "total_contact_wrench" in {str(flag) for flag in flags}
    )


def _row_has_eoat_surface_contact_pair(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    collision1 = str(row.get("collision1") or "")
    collision2 = str(row.get("collision2") or "")
    return (_is_eoat_collision(collision1) and _is_surface_collision(collision2)) or (
        _is_eoat_collision(collision2) and _is_surface_collision(collision1)
    )


def _is_eoat_collision(name: str) -> bool:
    return "eoat" in name and "collision" in name


def _is_surface_collision(name: str) -> bool:
    return "contact_surface" in name or "surface::collision" in name


def gazebo_visual_contact_topic(stage: str) -> str:
    return f"/ur10e/contact/gazebo/{stage}/contacts"


def start_contact_topic_capture(
    case_dir: Path,
    *,
    topic: str,
    env: dict[str, str],
    max_messages: int,
    observation_id: str | None = None,
    transport: str = "ignition",
) -> subprocess.Popen[str] | None:
    output_dir = case_dir / "contact_capture"
    output_dir.mkdir(parents=True, exist_ok=True)
    if transport == "ignition":
        command = ["ign", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"]
    elif transport == "gz":
        command = ["gz", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"]
    else:
        raise ValueError(f"unsupported contact capture transport: {transport}")
    metadata = {
        "schema": CONTACT_CAPTURE_SCHEMA,
        "topic": topic,
        "sim_transport": transport,
        "command": command,
        "max_messages": max_messages,
        "observation_id": observation_id,
        "started_at": _now(),
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "allowed_claim": "contact_pair_log_component_only_until_wrench_contact_correlation_gate_passes",
        "forbidden_claim": "force_contact_physics_proven; total contact wrench; real bench/live contact",
    }
    (output_dir / "contact_capture_start.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        return subprocess.Popen(
            command,
            cwd=WORKSPACE,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )
    except OSError as exc:
        metadata.update({"start_error": f"{type(exc).__name__}: {exc}", "finished_at": _now()})
        (output_dir / "contact_capture_start.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return None


def finish_contact_topic_capture(
    process: subprocess.Popen[str] | None,
    case_dir: Path,
    *,
    stage: str,
    topic: str,
    world_path: Path,
    max_messages: int,
    observation_id: str | None = None,
    time_start: str | None = None,
    transport: str = "ignition",
) -> dict[str, object]:
    output_dir = case_dir / "contact_capture"
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_jsonl = output_dir / "contact_topic_stdout.jsonl"
    stderr_log = output_dir / "contact_topic_stderr.log"
    if process is None:
        stdout = ""
        stderr = "contact capture process was not started\n"
        returncode = None
        timed_out = False
    else:
        if process.poll() is None:
            _terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=5.0)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            stdout, stderr = process.communicate(timeout=5.0)
            timed_out = True
        returncode = process.returncode
    raw_jsonl.write_text(stdout or "", encoding="utf-8")
    stderr_log.write_text(stderr or "", encoding="utf-8")
    finished_at = _now()
    payload = contact_capture.contact_pair_log_from_json_lines(
        (stdout or "").splitlines(),
        topic=topic,
        world_path=str(world_path),
        raw_jsonl_path=str(raw_jsonl),
        transport=transport,
        sensor_collision_role="surface",
        generated_at=finished_at,
    )
    payload["stage_id"] = stage
    payload["observation_id"] = observation_id
    payload["observation_scope"] = SAME_RUN_STAGE_OBSERVATION_SCOPE
    time_window = {
        "start": time_start,
        "end": finished_at,
        "clock_source": "ignition_transport_contact_topic_capture",
    }
    payload["time_window"] = time_window
    payload["capture"] = {
        "schema": CONTACT_CAPTURE_SCHEMA,
        "sim_transport": transport,
        "topic": topic,
        "stage_id": stage,
        "observation_id": observation_id,
        "observation_scope": SAME_RUN_STAGE_OBSERVATION_SCOPE,
        "command": (
            ["ign", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"]
            if transport == "ignition"
            else ["gz", "topic", "-e", "-t", topic, "-n", str(max_messages), "--json-output"]
        ),
        "returncode": returncode,
        "timed_out_during_shutdown": timed_out,
        "max_messages": max_messages,
        "stdout_path": str(raw_jsonl),
        "stderr_path": str(stderr_log),
        "claim_tier": "visual_only",
        "target_claim_tier": "physical Gazebo collision/contact physics",
        "forbidden_claim": "force_contact_physics_proven; total contact wrench; real bench/live contact",
        "finished_at": finished_at,
    }
    path = output_dir / "gazebo_contact_pair_log.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_stage_contact_wrench_adapter(
        output_dir,
        contact_pair_path=path,
        stage=stage,
        source_topic=topic,
        observation_id=observation_id,
        time_window=time_window,
        observation_scope=SAME_RUN_STAGE_OBSERVATION_SCOPE,
    )
    return payload


def write_stage_contact_wrench_adapter(
    output_dir: Path,
    *,
    contact_pair_path: Path,
    stage: str,
    source_topic: str | None = None,
    observation_id: str | None = None,
    time_window: dict[str, object] | None = None,
    observation_scope: str | None = SAME_RUN_STAGE_OBSERVATION_SCOPE,
) -> Path | None:
    try:
        return wrench_adapter.write_wrench_trace_or_report(
            output_dir,
            contact_pair_path=contact_pair_path,
            generated_at=_now(),
            source_topic=source_topic or gazebo_visual_contact_topic(stage),
            report_filename=STAGE_CONTACT_WRENCH_ADAPTER_FILENAME,
            trace_filename=STAGE_CONTACT_WRENCH_TRACE_FILENAME,
            stage_id=stage,
            observation_id=observation_id,
            time_window=time_window,
            observation_scope=observation_scope,
        )
    except Exception as exc:  # noqa: BLE001 - artifact generation must downgrade, not crash row cleanup.
        path = output_dir / STAGE_CONTACT_WRENCH_ADAPTER_FILENAME
        payload = {
            "schema": wrench_adapter.REPORT_SCHEMA,
            "generated_at": _now(),
            "mode": "offline_no_live_gazebo_contact_wrench_adapter",
            "stage_id": stage,
            "observation_id": observation_id,
            "time_window": time_window,
            "observation_scope": observation_scope,
            "trace_written": False,
            "claim_tier": "visual_only",
            "target_claim_tier": "physical Gazebo collision/contact physics",
            "total_contact_wrench_proven": False,
            "total_contact_wrench_row_count": 0,
            "force_source": None,
            "wrench_trace_path": None,
            "blockers": [f"stage_contact_wrench_adapter_generation_error:{type(exc).__name__}"],
            "error": str(exc),
            "live_robot_command_authorized": False,
            "bridge_start_authorized": False,
            "urscript_send_authorized": False,
            "tp_load_play_authorized": False,
            "zero_ftsensor_authorized": False,
            "payload_tcp_safety_writes_authorized": False,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def stage_dual_sensor_observation_manifest_path(case_dir: Path, stage: str) -> Path:
    return (
        case_dir
        / STAGE_DUAL_SENSOR_OBSERVATION_DIRNAME
        / stage_observation.DEFAULT_FILENAME_TEMPLATE.format(stage_id=stage)
    )


def per_stage_dual_sensor_contact_audit_path(case_dir: Path, stage: str) -> Path:
    return (
        case_dir
        / PER_STAGE_DUAL_SENSOR_CONTACT_AUDIT_DIRNAME
        / f"{stage}_per_stage_dual_sensor_contact_audit.json"
    )


def annotate_contact_stage_evidence_paths(
    row: dict[str, object],
    *,
    case_dir: Path,
    stage: str,
    include_observation_manifest: bool,
    include_per_stage_audit: bool,
) -> dict[str, object]:
    if stage not in CONTACT_STAGES:
        return row
    if include_observation_manifest:
        row["same_run_stage_dual_sensor_observation_manifest_path"] = str(
            stage_dual_sensor_observation_manifest_path(case_dir, stage)
        )
        row["same_run_stage_dual_sensor_observation_target_claim_tier"] = "physical Gazebo collision/contact physics"
        row["same_run_stage_dual_sensor_observation_forbidden_claim"] = (
            "real bench/live contact; per-stage physical Gazebo contact unless manifest content validation, "
            "stage total contact wrench, and wrench/contact correlation gates pass"
        )
    if include_per_stage_audit:
        row["per_stage_dual_sensor_contact_audit_path"] = str(
            per_stage_dual_sensor_contact_audit_path(case_dir, stage)
        )
        row["per_stage_dual_sensor_contact_audit_target_claim_tier"] = "physical Gazebo collision/contact physics"
        row["per_stage_dual_sensor_contact_audit_forbidden_claim"] = (
            "real bench/live contact; per-stage physical Gazebo contact unless contact pair/log, "
            "total contact wrench, frame/normal evidence, and wrench/contact correlation are proven"
        )
    return row


def write_stage_dual_sensor_observation_manifest(
    case_dir: Path,
    *,
    stage: str,
    row: dict[str, object],
    row_summary_path: Path,
    stage_simulated_ft_manifest_path: Path | None,
    step_status_audit_path: Path | None,
    observation_id: str,
    time_start: str,
    time_end: str,
    clock_source: str,
) -> Path:
    output_dir = case_dir / STAGE_DUAL_SENSOR_OBSERVATION_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        return stage_observation.write_manifest(
            output_dir,
            stage_id=stage,
            observation_id=observation_id,
            time_start=time_start,
            time_end=time_end,
            clock_source=clock_source,
            surfaces={
                "stage_row_summary": row_summary_path,
                "stage_contact_pair_log": _path_from_row(row, "gazebo_contact_pair_log_path"),
                "stage_contact_wrench_adapter": _path_from_row(row, "stage_contact_wrench_adapter_path"),
                "stage_simulated_ft_manifest": stage_simulated_ft_manifest_path,
                "step_status_rnn_audit": step_status_audit_path,
                "visual_evidence": _path_from_row(row, "scripted_camera_final_png", "final_png"),
                "tcp_path_evidence": _path_from_row(row, "trace_path"),
            },
            generated_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001 - observation binding must fail closed without hiding row evidence.
        path = stage_dual_sensor_observation_manifest_path(case_dir, stage)
        payload = {
            "schema": "ur10e_stage_dual_sensor_observation_manifest_v1",
            "generated_at": _now(),
            "goal_lineage": stage_observation.GOAL_LINEAGE,
            "mode": "offline_report_level_stage_dual_sensor_observation_binder",
            "stage_id": stage,
            "claim_tier": "visual_only",
            "observation_id": observation_id,
            "explicit": True,
            "time_window": {
                "start": time_start,
                "end": time_end,
                "clock_source": clock_source,
            },
            "same_run_stage_dual_sensor_observation_proven": False,
            "validation_issues": [f"stage_dual_sensor_observation_manifest_generation_error:{type(exc).__name__}"],
            "blockers": ["same_run_stage_dual_sensor_observation:not_proven"],
            "error": str(exc),
            "live_authorization": {
                "robot_motion_authorized": False,
                "bridge_start_authorized": False,
                "tp_play_authorized": False,
                "urscript_authorized": False,
                "zero_ftsensor_authorized": False,
                "payload_tcp_safety_writes_authorized": False,
                "real_bench_live_contact_authorized": False,
            },
            "forbidden_claim": "real bench/live contact; per-stage physical Gazebo contact",
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def write_per_stage_dual_sensor_contact_audit(
    case_dir: Path,
    *,
    stage: str,
    row: dict[str, object],
    row_summary_path: Path,
    stage_simulated_ft_manifest_path: Path | None,
    step_status_audit_path: Path | None,
    same_run_observation_manifest_path: Path | None,
    correlation_tolerance_s: float,
) -> Path:
    output_dir = case_dir / PER_STAGE_DUAL_SENSOR_CONTACT_AUDIT_DIRNAME
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        return per_stage_contact_audit.write_audit(
            output_dir,
            stage_id=stage,
            generated_at=_now(),
            stage_row_summary_path=row_summary_path,
            stage_simulated_ft_manifest_path=stage_simulated_ft_manifest_path,
            step_status_audit_path=step_status_audit_path,
            stage_contact_pair_log_path=_path_from_row(row, "gazebo_contact_pair_log_path"),
            stage_contact_wrench_adapter_path=_path_from_row(row, "stage_contact_wrench_adapter_path"),
            same_run_observation_manifest_path=same_run_observation_manifest_path,
            correlation_tolerance_s=correlation_tolerance_s,
        )
    except Exception as exc:  # noqa: BLE001 - per-stage audit generation must fail closed.
        path = per_stage_dual_sensor_contact_audit_path(case_dir, stage)
        payload = {
            "schema": "ur10e_per_stage_dual_sensor_contact_audit_v1",
            "generated_at": _now(),
            "goal_lineage": per_stage_contact_audit.GOAL_LINEAGE,
            "mode": "offline_report_level_per_stage_dual_sensor_contact_gate",
            "stage_id": stage,
            "claim_tier": "visual_only",
            "per_stage_physical_gazebo_contact": {
                "claim_tier": "visual_only",
                "per_stage_physical_gazebo_contact_proven": False,
            },
            "same_run_stage_dual_sensor_observation": {
                "same_run_stage_dual_sensor_observation_proven": False,
            },
            "validation_issues": [f"per_stage_dual_sensor_contact_audit_generation_error:{type(exc).__name__}"],
            "blockers": [
                "per_stage_physical_gazebo_contact:not_proven",
                "same_run_stage_dual_sensor_observation:not_proven",
            ],
            "error": str(exc),
            "live_authorization": {
                "robot_motion_authorized": False,
                "bridge_start_authorized": False,
                "tp_play_authorized": False,
                "urscript_authorized": False,
                "zero_ftsensor_authorized": False,
                "payload_tcp_safety_writes_authorized": False,
                "real_bench_live_contact_authorized": False,
            },
            "forbidden_claim": "real bench/live contact; per-stage physical Gazebo contact",
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def _path_from_row(row: dict[str, object], *keys: str) -> Path | None:
    for key in keys:
        value = row.get(key)
        if value:
            return Path(str(value))
    return None


def capture_scripted_camera_image(
    case_dir: Path,
    *,
    stage: str,
    view: str,
    env: dict[str, str],
    timeout_s: float,
) -> dict[str, object]:
    topic = f"/ur10e_visual_audit/{stage}/{view}/image"
    output = case_dir / "scripted_camera_final.png"
    metadata_path = case_dir / "scripted_camera_capture.json"
    payload: dict[str, object] = {
        "schema": "ur10e_gazebo_scripted_camera_capture_v1",
        "topic": topic,
        "output": str(output),
        "captured": False,
        "clean_scene_capture": True,
        "observer_review_still_required": True,
    }
    bridge = None
    try:
        bridge = _popen(
            ["ros2", "run", "ros_gz_image", "image_bridge", topic],
            case_dir / "scripted_camera_bridge.log",
            cwd=WORKSPACE,
            env=env,
            new_session=True,
        )
        time.sleep(1.0)
        payload.update(_save_ros_image(topic, output, timeout_s=timeout_s))
    except Exception as exc:  # noqa: BLE001 - evidence capture must downgrade, not crash the row.
        payload.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        _terminate_process_group(bridge)
        time.sleep(0.2)
        _kill_process_group(bridge)
    payload["captured"] = bool(payload.get("ok") and output.is_file() and output.stat().st_size > 0)
    if payload["captured"]:
        payload["sha256"] = _sha256_file(output)
    metadata_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def _save_ros_image(topic: str, output: Path, *, timeout_s: float) -> dict[str, object]:
    from cv_bridge import CvBridge  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415
    import rclpy  # noqa: PLC0415
    from rclpy.qos import qos_profile_sensor_data  # noqa: PLC0415
    from sensor_msgs.msg import Image as ImageMsg  # noqa: PLC0415

    result: dict[str, object] = {}
    bridge = CvBridge()
    rclpy.init(args=None)
    node = rclpy.create_node("ur10e_visual_audit_image_saver")

    def callback(msg: ImageMsg) -> None:
        try:
            array = bridge.imgmsg_to_cv2(msg, desired_encoding="rgb8")
            Image.fromarray(array).save(output)
            result.update(
                {
                    "ok": True,
                    "height": int(msg.height),
                    "width": int(msg.width),
                    "encoding": str(msg.encoding),
                    "frame_id": str(msg.header.frame_id),
                }
            )
        except Exception as exc:  # noqa: BLE001
            result.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    subscription = node.create_subscription(ImageMsg, topic, callback, qos_profile_sensor_data)
    deadline = time.monotonic() + timeout_s
    try:
        while "ok" not in result and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()
    if "ok" not in result:
        result.update({"ok": False, "error": f"timeout waiting for {topic}", "timeout_s": timeout_s})
    return result


def capture_scene_introspection(
    case_dir: Path,
    *,
    env: dict[str, str],
    world_name: str,
    robot_model_name: str,
) -> Path:
    output_dir = case_dir / "scene_introspection"
    output_dir.mkdir(parents=True, exist_ok=True)
    commands = {
        "topic_list.txt": ["ign", "topic", "-l"],
        "scene_info.json": [
            "ign",
            "topic",
            "-e",
            "--json-output",
            "-n",
            "1",
            "-t",
            f"/world/{world_name}/scene/info",
        ],
        "pose_info.json": [
            "ign",
            "topic",
            "-e",
            "--json-output",
            "-n",
            "1",
            "-t",
            f"/world/{world_name}/pose/info",
        ],
        "model_list.txt": ["ign", "model", "--list"],
        "active_tcp_marker_model.txt": ["ign", "model", "-m", "active_tcp_marker"],
        "active_tcp_marker_links.txt": ["ign", "model", "-m", "active_tcp_marker", "-l"],
        "robot_model.txt": ["ign", "model", "-m", robot_model_name],
        "robot_links.txt": ["ign", "model", "-m", robot_model_name, "-l"],
    }
    summary: dict[str, object] = {}
    for name, command in commands.items():
        output = output_dir / name
        summary[name] = _run_introspection_logged(command, output, env=env)
    (output_dir / "introspection_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_dir


def _run_introspection_logged(command: list[str], output: Path, *, env: dict[str, str]) -> dict[str, object]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            env=env,
            timeout=6.0,
        )
        text = completed.stdout
        returncode = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        if exc.stderr:
            text += exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode(errors="replace")
        returncode = None
        timed_out = True
    output.write_text(text, encoding="utf-8")
    return {
        "command": command,
        "output": str(output),
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_s": round(time.monotonic() - started, 3),
        "bytes": len(text.encode("utf-8")),
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def gui_config_is_clean(path: Path | None) -> bool:
    if path is None or not path.exists():
        return False
    source = path.read_text(encoding="utf-8")
    blocked = ("ComponentInspector", "EntityTree")
    return all(token not in source for token in blocked)


def _select_stage_payload(payload: dict[str, Any], stage: str) -> dict[str, Any]:
    for item in payload.get("stages", []):
        if item.get("stage_id") == stage:
            return item
    stages = payload.get("stages") or []
    if len(stages) == 1:
        return stages[0]
    raise KeyError(f"stage {stage!r} not found in matrix summary")


def _visual_manifest_path(run_dir: Path | None, stage: str) -> Path | None:
    if run_dir is None:
        return None
    return run_dir / "_visual_worlds" / f"{stage}_visual.manifest.json"


def _state_settled_success(stage: str, execution: dict[str, Any], settled_fraction: Any) -> bool | None:
    if stage in CONTACT_STAGES:
        return bool(settled_fraction is not None and float(settled_fraction) >= 0.85)
    return bool(execution.get("ok") and execution.get("observed_motion"))


def _view_role(view: str) -> str:
    if view == "context_overview":
        return "context"
    if view == "interaction_view":
        return "interaction"
    if view == "side_view":
        return "side_view"
    if view == "close_detail":
        return "close_detail"
    return "unknown"


def _review_flag(review: dict[str, Any], key: str) -> bool:
    return bool(review.get(key))


def _representative_failing_images(rows: list[dict[str, object]], limit: int = 8) -> list[dict[str, object]]:
    images: list[dict[str, object]] = []
    for row in rows[:limit]:
        images.append(
            {
                "stage": row.get("stage"),
                "view": row.get("view"),
                "final_png": row.get("final_png"),
                "failure_reasons": row.get("observer_visual_failure_reasons"),
            }
        )
    return images


def _representative_timing_rows(rows: list[dict[str, object]], limit: int = 8) -> list[dict[str, object]]:
    return [
        {
            "stage": row.get("stage"),
            "view": row.get("view"),
            "actual_vs_commanded_duration_ratio": row.get("actual_vs_commanded_duration_ratio"),
            "inferred_speed_scale_from_action_result": row.get("inferred_speed_scale_from_action_result"),
            "duration_ratio_clock_domain": row.get("duration_ratio_clock_domain"),
            "sim_time_real_time_factor_confound": row.get("sim_time_real_time_factor_confound"),
            "rtf_or_controller_speed_unresolved": row.get("rtf_or_controller_speed_unresolved"),
            "timing_root_cause_status": row.get("timing_root_cause_status"),
            "controller_speed_scaling_measured": row.get("controller_speed_scaling_measured"),
        }
        for row in rows[:limit]
    ]


def _read_json(path: Path | None, *, default: Any | None = None) -> Any:
    if path is None or not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_frames(video: Path, duration_s: float | None, case_dir: Path) -> None:
    if not video.exists() or not duration_s:
        return
    points = {
        "start_root.png": max(0.2, min(1.0, duration_s / 10.0)),
        "mid_root.png": max(0.2, duration_s / 2.0),
        "final_root.png": max(0.2, duration_s - 1.0),
    }
    for name, timestamp in points.items():
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                str(case_dir / name),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def _video_duration(video: Path) -> float | None:
    if not video.exists():
        return None
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _wait_for_action(cwd: Path, env: dict[str, str], timeout_s: int) -> int | None:
    for second in range(1, timeout_s + 1):
        completed = subprocess.run(["ros2", "action", "list"], cwd=cwd, env=env, capture_output=True, text=True, check=False)
        if "/joint_trajectory_controller/follow_joint_trajectory" in completed.stdout.splitlines():
            return second
        time.sleep(1.0)
    return None


def _popen(command: list[str], log: Path, *, cwd: Path, env: dict[str, str], new_session: bool = False) -> subprocess.Popen[bytes]:
    handle = log.open("wb")
    return subprocess.Popen(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, preexec_fn=os.setsid if new_session else None)


def _run_logged(command: list[str], log: Path, *, cwd: Path, env: dict[str, str]) -> int:
    with log.open("wb") as handle:
        completed = subprocess.run(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, check=False)
    return completed.returncode


def _terminate_process(process: subprocess.Popen[bytes] | None) -> None:
    if process and process.poll() is None:
        process.send_signal(signal.SIGINT)


def _kill_process(process: subprocess.Popen[bytes] | None) -> None:
    if process and process.poll() is None:
        process.terminate()


def _terminate_process_group(process: subprocess.Popen[bytes] | None) -> None:
    if process and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass


def _kill_process_group(process: subprocess.Popen[bytes] | None) -> None:
    if process and process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _write_trace(path: Path, values: dict[str, object], *, mode: str = "a") -> None:
    with path.open(mode, encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _prepend(value: str, current: str | None) -> str:
    return value if not current else f"{value}:{current}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run or summarize one UR10e Gazebo GUI matrix row.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    row = subparsers.add_parser("row", help="run one GUI matrix row")
    row.add_argument("--workspace", type=Path, default=WORKSPACE)
    row.add_argument("--run-dir", type=Path, required=True)
    row.add_argument("--stage", choices=STAGES, required=True)
    row.add_argument("--view", choices=VIEWS, required=True)
    row.add_argument("--gui-config-dir", type=Path, default=DEFAULT_GUI_CONFIG_DIR)
    row.add_argument("--local-ros-prefix", type=Path)
    row.add_argument("--display", default=":0")
    row.add_argument("--capture-size", default="1280x900")
    row.add_argument("--action-ready-timeout-s", type=int, default=75)
    row.add_argument("--max-record-s", type=float, default=240.0)
    row.add_argument("--scripted-camera-timeout-s", type=float, default=8.0)
    row.add_argument("--marker-style", choices=MARKER_STYLES, default=DEFAULT_OBSERVER_MARKER_STYLE)
    row.add_argument("--contact-capture-max-messages", type=int, default=20)
    row.add_argument("--disable-contact-capture", action="store_true")
    row.add_argument("--stage-simulated-ft-manifest", type=Path)
    row.add_argument("--step-status-audit", type=Path)
    row.add_argument("--stage-observation-clock-source", default="/clock")
    row.add_argument("--disable-stage-observation-manifest", action="store_true")
    row.add_argument("--per-stage-contact-correlation-tolerance-s", type=float, default=0.02)
    row.add_argument("--disable-per-stage-contact-audit", action="store_true")
    row.add_argument("--allow-existing-gazebo", action="store_true")
    row.add_argument("--visible-gazebo-lock-path", type=Path, default=DEFAULT_VISIBLE_GAZEBO_LOCK_PATH)
    row.add_argument("--update-summary", action="store_true")
    row.set_defaults(func=run_row)

    summary = subparsers.add_parser("summary", help="build visual audit summary from row summaries")
    summary.add_argument("--run-dir", type=Path, required=True)
    summary.add_argument("--output", type=Path)
    summary.set_defaults(func=run_summary)

    plugin_preflight = subparsers.add_parser(
        "plugin-path-preflight",
        help="write an offline Gazebo row plugin-path readiness artifact without launching Gazebo",
    )
    plugin_preflight.add_argument("--output", type=Path, required=True)
    plugin_preflight.add_argument("--local-ros-prefix", type=Path)
    plugin_preflight.add_argument("--display", default=":0")
    plugin_preflight.set_defaults(func=run_plugin_path_preflight)

    action_preflight = subparsers.add_parser(
        "action-readiness-preflight",
        help="write an offline Gazebo row static action-readiness artifact without launching Gazebo",
    )
    action_preflight.add_argument("--output", type=Path, required=True)
    action_preflight.add_argument("--local-ros-prefix", type=Path)
    action_preflight.add_argument("--display", default=":0")
    action_preflight.set_defaults(func=run_action_readiness_preflight)

    failure_backfill = subparsers.add_parser(
        "action-ready-failure-backfill",
        help="write fail-closed row artifacts from an existing action_ready=0 Gazebo row without launching Gazebo",
    )
    failure_backfill.add_argument("--run-dir", type=Path, required=True)
    failure_backfill.add_argument("--stage", choices=STAGES, required=True)
    failure_backfill.add_argument("--view", choices=VIEWS, required=True)
    failure_backfill.add_argument("--trace-path", type=Path)
    failure_backfill.add_argument("--runner-rc", type=int, default=41)
    failure_backfill.add_argument("--blocker")
    failure_backfill.add_argument("--stage-simulated-ft-manifest", type=Path)
    failure_backfill.add_argument("--step-status-audit", type=Path)
    failure_backfill.add_argument("--observation-id")
    failure_backfill.add_argument("--time-start")
    failure_backfill.add_argument("--time-end")
    failure_backfill.add_argument("--clock-source")
    failure_backfill.add_argument("--stage-observation-clock-source", default="/clock")
    failure_backfill.add_argument("--per-stage-contact-correlation-tolerance-s", type=float, default=0.02)
    failure_backfill.add_argument("--disable-stage-observation-manifest", action="store_true")
    failure_backfill.add_argument("--disable-per-stage-contact-audit", action="store_true")
    failure_backfill.set_defaults(func=run_action_ready_failure_backfill)
    return parser.parse_args(argv)


def run_summary(args: argparse.Namespace) -> int:
    output = args.output or args.run_dir / "visual_audit_summary.json"
    payload = build_visual_audit_summary(args.run_dir.resolve())
    write_visual_audit_summary(payload, output.resolve())
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
