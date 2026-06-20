#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


WORKSPACE = Path(__file__).resolve().parents[3]
EXPERIMENT = WORKSPACE / "experiments" / "tase-contact-reproduction"
SRC_PACKAGE = WORKSPACE / "src" / "ur10e_example_controllers"
if str(SRC_PACKAGE) not in sys.path:
    sys.path.insert(0, str(SRC_PACKAGE))

from ur10e_example_controllers import ur10e_gazebo_matrix_runner as gazebo  # noqa: E402


STAGES = ("step5a", "step5b", "step5c", "step5d", "step6a", "step6b", "step7", "step8")
VIEWS = ("context_overview", "interaction_view", "close_detail")
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


def run_row(args: argparse.Namespace) -> int:
    workspace = args.workspace.resolve()
    run_dir = args.run_dir.resolve()
    case_dir = row_case_dir(run_dir, args.stage, args.view)
    world_dir = run_dir / "_visual_worlds"
    world = world_dir / f"{args.stage}_visual.sdf"
    gui_config = (args.gui_config_dir or DEFAULT_GUI_CONFIG_DIR) / f"{args.view}.config"
    trace_path = case_dir / "command_trace.txt"
    case_dir.mkdir(parents=True, exist_ok=True)
    world_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["DISPLAY"] = args.display
    if args.local_ros_prefix:
        prefix = str(args.local_ros_prefix.resolve())
        env["AMENT_PREFIX_PATH"] = _prepend(prefix, env.get("AMENT_PREFIX_PATH"))
        env["LD_LIBRARY_PATH"] = _prepend(str(Path(prefix) / "lib"), env.get("LD_LIBRARY_PATH"))
        env["IGN_GAZEBO_SYSTEM_PLUGIN_PATH"] = _prepend(str(Path(prefix) / "lib"), env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH"))
        env["GZ_SIM_SYSTEM_PLUGIN_PATH"] = _prepend(str(Path(prefix) / "lib"), env.get("GZ_SIM_SYSTEM_PLUGIN_PATH"))

    _write_trace(
        trace_path,
        {
            "started_at": _now(),
            "stage": args.stage,
            "view": args.view,
            "world": str(world),
            "gui_config": str(gui_config),
            "display": env["DISPLAY"],
            "local_ros_prefix": str(args.local_ros_prefix.resolve()) if args.local_ros_prefix else "",
            "ign_plugin_path": env.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", ""),
        },
        mode="w",
    )

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

    launch = marker = ffmpeg = None
    runner_rc = None
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
            return 41
        _write_trace(trace_path, {"action_ready": 1, "action_ready_after_s": ready_after})

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
    finally:
        _terminate_process(marker)
        _terminate_process(ffmpeg)
        _terminate_process_group(launch)
        time.sleep(1.0)
        _kill_process(marker)
        _kill_process(ffmpeg)
        _kill_process_group(launch)

    video = case_dir / "gui_recording.mp4"
    duration_s = _video_duration(video)
    _extract_frames(video, duration_s, case_dir)
    row = build_row_summary(
        stage=args.stage,
        view=args.view,
        case_dir=case_dir,
        run_dir=run_dir,
        runner_rc=runner_rc,
        video_duration_s=duration_s,
        gui_config_path=gui_config,
    )
    write_row_summary(row, case_dir / "row_summary.json")
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
    print(json.dumps(row, indent=2, sort_keys=True))
    return int(runner_rc or 0)


def row_case_dir(run_dir: Path, stage: str, view: str) -> Path:
    return run_dir / "matrix_gui_real_aligned" / stage / view


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
    visual_manifest_path = _visual_manifest_path(run_dir, stage) if run_dir is not None else None
    visual_manifest = _read_json(visual_manifest_path, default={}) if visual_manifest_path else {}
    observer_review_path = case_dir / "observer_review.json"
    observer_review = _read_json(observer_review_path, default={}) if observer_review_path.exists() else {}

    force_loop = execution.get("force_loop") or stage_payload.get("force_loop") or {}
    force_success = execution.get("force_closed_loop")
    if stage not in CONTACT_STAGES:
        force_success = None
    settled_fraction = force_loop.get("settled_within_tolerance_fraction")
    state_settled_success = _state_settled_success(stage, execution, settled_fraction)

    start_png = case_dir / "start_root.png"
    mid_png = case_dir / "mid_root.png"
    final_png = case_dir / "final_root.png"
    video_path = case_dir / "gui_recording.mp4"
    gui_evidence = all(path.is_file() and path.stat().st_size > 0 for path in (start_png, mid_png, final_png, video_path))
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
        "state_settled_success": state_settled_success,
        "force_loop_success": force_success,
        "force_contact_source": stage_payload.get("force_contact_source") or gazebo.FORCE_CONTACT_SOURCE,
        "force_contact_physics_proven": bool(stage_payload.get("force_contact_physics_proven") or acceptance.get("force_contact_physics_proven")),
        "force_loop_trace_written": acceptance.get("force_loop_trace_written"),
        "settled_force_within_tolerance_fraction": settled_fraction,
        "real_machine_traceability_status": TRACE_STATUS[stage],
        "gui_evidence_captured": gui_evidence,
        "video_duration_s": video_duration_s,
        "video_path": str(video_path),
        "start_png": str(start_png),
        "mid_png": str(mid_png),
        "final_png": str(final_png),
        "marker_manifest": str(marker_manifest),
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
        "trace_path": stage_payload.get("trace_path"),
        "visual_world_manifest": str(visual_manifest_path) if visual_manifest_path else None,
        "surface_frame": visual_manifest.get("surface_frame"),
        "surface": visual_manifest.get("surface"),
        "final_visual_pose_world": visual_manifest.get("final_visual_pose_world"),
        "contact_target_pose_world": visual_manifest.get("contact_target_pose_world"),
        "surface_tcp_sanity": visual_manifest.get("surface_tcp_sanity"),
        "observer_review_present": review_present,
        "observer_review_path": str(observer_review_path),
        "observer_visual_review_source": review_source,
        "observer_visual_notes": observer_review.get("notes"),
        "observer_visual_reviewed_at": observer_review.get("reviewed_at"),
        "observer_visual_review_status": "row_review_present" if review_present else "pending_observer_review",
        "robot_posture_visible": bool(gui_evidence and _review_flag(observer_review, "robot_posture_visible")),
        "eoat_tooling_visible": bool(gui_evidence and _review_flag(observer_review, "eoat_tooling_visible")),
        "tcp_marker_visible": bool(gui_evidence and marker_pose_valid and _review_flag(observer_review, "tcp_marker_visible")),
        "surface_path_visible": bool(gui_evidence and _review_flag(observer_review, "surface_path_visible")),
        "robot_tool_surface_relation_visible": bool(
            gui_evidence and marker_pose_valid and _review_flag(observer_review, "robot_tool_surface_relation_visible")
        ),
        "obstructive_ui_panels_absent": bool(config_clean or _review_flag(observer_review, "obstructive_ui_panels_absent")),
        "clean_scene_capture": bool(config_clean or _review_flag(observer_review, "clean_scene_capture")),
        "context_ui_allowed": bool(view == "context_overview" and _review_flag(observer_review, "context_ui_allowed")),
        "machine_visual_prerequisites": {
            "gui_evidence_captured": gui_evidence,
            "marker_pose_valid": marker_pose_valid,
            "marker_pose_count": marker_pose_count,
            "marker_pose_source": marker_payload.get("pose_source"),
            "marker_pose_frame": marker_payload.get("pose_frame"),
            "clean_gui_config": config_clean,
            "visual_world_manifest_present": bool(visual_manifest),
        },
    }
    return gazebo.populate_observer_visual_pass(row)


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
                missing_rows.append({"stage": stage, "view": view, "row_summary": str(row_path)})

    expected = len(stages) * len(views)
    observer_pass_rows = [row for row in rows if row.get("observer_visual_pass") is True]
    observer_fail_rows = [row for row in rows if row.get("observer_visual_pass") is not True]
    action_success_rows = [row for row in rows if row.get("action_success") is True]
    gui_evidence_rows = [row for row in rows if row.get("gui_evidence_captured") is True]
    contact_rows = [row for row in rows if row.get("contact_stage") is True]
    contact_force_success_rows = [row for row in contact_rows if row.get("force_loop_success") is True]
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
        "all_expected_rows_present": all_expected_rows_present,
        "observer_visual_pass_count": len(observer_pass_rows),
        "observer_visual_fail_count": len(observer_fail_rows) + len(missing_rows),
        "all_rows_observer_visual_pass": all_observer_pass,
        "all_rows_action_success": all_expected_rows_present and len(action_success_rows) == expected,
        "all_rows_gui_evidence_captured": all_expected_rows_present and len(gui_evidence_rows) == expected,
        "contact_row_count": len(contact_rows),
        "contact_rows_force_loop_success_count": len(contact_force_success_rows),
        "all_contact_rows_force_loop_success": bool(contact_rows) and len(contact_force_success_rows) == len(contact_rows),
        "visual_review_status": "per_row_observer_visual_pass" if all_observer_pass else "per_row_observer_visual_failed_or_missing",
        "representative_failing_images": _representative_failing_images(observer_fail_rows),
        "rows": rows,
    }
    return payload


def write_visual_audit_summary(summary: dict[str, object], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


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
    row.add_argument("--update-summary", action="store_true")
    row.set_defaults(func=run_row)

    summary = subparsers.add_parser("summary", help="build visual audit summary from row summaries")
    summary.add_argument("--run-dir", type=Path, required=True)
    summary.add_argument("--output", type=Path)
    summary.set_defaults(func=run_summary)
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
