from __future__ import annotations

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchContext, LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
    SetEnvironmentVariable,
    TimerAction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ur10e_example_controllers.step5b_simulation_mvp import strip_ros2_control_blocks


DEFAULT_CALIBRATED_URDF = (
    "/home/andy/ur10e_ros2_ws/experiments/tase-contact-reproduction/runs/"
    "step5c_calibrated_kinematics_audit_20260613_003314/calibrated_ur10e.urdf"
)


def _as_bool(context: LaunchContext, config: LaunchConfiguration) -> bool:
    return context.perform_substitution(config).lower() in {"1", "true", "yes", "on"}


def _build_gz_args(world: Path, headless_enabled: bool) -> str:
    gz_args = f"{world} -r"
    if headless_enabled:
        gz_args += " -s --headless-rendering"
    return gz_args


def _gazebo_actions(context: LaunchContext, run_gazebo: LaunchConfiguration, headless: LaunchConfiguration):
    if not _as_bool(context, run_gazebo):
        return []
    share = get_package_share_directory("ur10e_example_controllers")
    world = Path(share) / "worlds" / "step5_table_world.sdf"
    gz_args = _build_gz_args(world, _as_bool(context, headless))
    ros_gz_sim_share = get_package_share_directory("ros_gz_sim")
    return [
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(os.path.join(ros_gz_sim_share, "launch", "gz_sim.launch.py")),
            launch_arguments={"gz_args": gz_args}.items(),
        )
    ]


def _robot_actions(
    context: LaunchContext,
    spawn_robot: LaunchConfiguration,
    calibrated_urdf: LaunchConfiguration,
):
    if not _as_bool(context, spawn_robot):
        return []
    urdf_path = Path(context.perform_substitution(calibrated_urdf))
    robot_description = strip_ros2_control_blocks(urdf_path.read_text(encoding="utf-8"))
    return [
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            name="ur10e_step5b_sim_robot_state_publisher",
            output="screen",
            parameters=[{"robot_description": robot_description, "use_sim_time": True}],
        ),
        TimerAction(
            period=3.0,
            actions=[
                Node(
                    package="ros_gz_sim",
                    executable="create",
                    name="ur10e_step5b_sim_create",
                    output="screen",
                    arguments=[
                        "-world",
                        "ur10e_step5_table_world",
                        "-name",
                        "ur10e_step5b",
                        "-topic",
                        "/robot_description",
                    ],
                )
            ],
        ),
    ]


def generate_launch_description() -> LaunchDescription:
    headless = LaunchConfiguration("headless")
    run_gazebo = LaunchConfiguration("run_gazebo")
    spawn_robot = LaunchConfiguration("spawn_robot")
    calibrated_urdf = LaunchConfiguration("calibrated_urdf")

    own_share_parent = str(Path(get_package_share_directory("ur10e_example_controllers")).parent)
    ur_description_parent = str(Path(get_package_share_directory("ur_description")).parent)
    existing = os.environ.get("GZ_SIM_RESOURCE_PATH", "")
    gz_resource_path = os.pathsep.join(path for path in [existing, own_share_parent, ur_description_parent] if path)

    return LaunchDescription(
        [
            DeclareLaunchArgument("headless", default_value="true", description="Run Gazebo headless."),
            DeclareLaunchArgument("run_gazebo", default_value="true", description="Start Gazebo Sim."),
            DeclareLaunchArgument("spawn_robot", default_value="true", description="Spawn calibrated UR10e from URDF."),
            DeclareLaunchArgument(
                "calibrated_urdf",
                default_value=DEFAULT_CALIBRATED_URDF,
                description="Flattened calibrated UR10e URDF artifact.",
            ),
            SetEnvironmentVariable("GZ_SIM_RESOURCE_PATH", gz_resource_path),
            OpaqueFunction(function=_gazebo_actions, args=[run_gazebo, headless]),
            OpaqueFunction(function=_robot_actions, args=[spawn_robot, calibrated_urdf]),
        ]
    )
