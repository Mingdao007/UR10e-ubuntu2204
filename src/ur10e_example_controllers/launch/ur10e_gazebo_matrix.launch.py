from __future__ import annotations

import os
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchContext, LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    OpaqueFunction,
    SetEnvironmentVariable,
    TimerAction,
)
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from ur10e_example_controllers.ur10e_gazebo_matrix_runner import generate_sim_robot_description


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
    command = ["ign", "gazebo", str(world), "-r"]
    if _as_bool(context, headless):
        command.extend(["-s", "--headless-rendering"])
    return [
        ExecuteProcess(
            cmd=command,
            name="ur10e_gazebo_matrix_gz_sim",
            output="screen",
        )
    ]


def _robot_actions(
    context: LaunchContext,
    spawn_robot: LaunchConfiguration,
    start_controllers: LaunchConfiguration,
    calibration_yaml: LaunchConfiguration,
    xacro_path: LaunchConfiguration,
):
    if not _as_bool(context, spawn_robot):
        return []

    share = Path(get_package_share_directory("ur10e_example_controllers"))
    controllers_yaml = share / "config" / "gazebo_matrix_controllers.yaml"
    initial_positions = share / "config" / "gazebo_matrix_initial_positions.yaml"
    robot_description = generate_sim_robot_description(
        controllers_yaml=controllers_yaml,
        initial_positions_yaml=initial_positions,
        calibration_yaml=Path(context.perform_substitution(calibration_yaml)),
        xacro_path=Path(context.perform_substitution(xacro_path)),
    )

    actions = [
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            name="robot_state_publisher",
            output="screen",
            parameters=[{"robot_description": robot_description, "use_sim_time": True}],
        ),
        TimerAction(
            period=3.0,
            actions=[
                Node(
                    package="ros_gz_sim",
                    executable="create",
                    name="ur10e_gazebo_matrix_create",
                    output="screen",
                    arguments=[
                        "-world",
                        "ur10e_step5_table_world",
                        "-name",
                        "ur10e_gazebo_matrix",
                        "-topic",
                        "/robot_description",
                    ],
                )
            ],
        ),
    ]

    if _as_bool(context, start_controllers):
        actions.append(
            TimerAction(
                period=7.0,
                actions=[
                    Node(
                        package="controller_manager",
                        executable="spawner",
                        name="ur10e_gazebo_matrix_spawn_joint_state_broadcaster",
                        output="screen",
                        arguments=["joint_state_broadcaster", "--controller-manager", "/controller_manager"],
                    ),
                    Node(
                        package="controller_manager",
                        executable="spawner",
                        name="ur10e_gazebo_matrix_spawn_joint_trajectory_controller",
                        output="screen",
                        arguments=["joint_trajectory_controller", "--controller-manager", "/controller_manager"],
                    ),
                ],
            )
        )
    return actions


def generate_launch_description() -> LaunchDescription:
    headless = LaunchConfiguration("headless")
    run_gazebo = LaunchConfiguration("run_gazebo")
    spawn_robot = LaunchConfiguration("spawn_robot")
    start_controllers = LaunchConfiguration("start_controllers")
    calibration_yaml = LaunchConfiguration("calibration_yaml")
    xacro_path = LaunchConfiguration("xacro_path")

    own_share_parent = str(Path(get_package_share_directory("ur10e_example_controllers")).parent)
    ur_description_parent = str(Path(get_package_share_directory("ur_description")).parent)
    existing = ""
    gz_resource_path = os.pathsep.join(path for path in [existing, own_share_parent, ur_description_parent] if path)

    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "headless",
                default_value="false",
                description="Run Gazebo server-only. Default false because Step5/6 acceptance requires GUI evidence.",
            ),
            DeclareLaunchArgument("run_gazebo", default_value="true", description="Start Gazebo Sim."),
            DeclareLaunchArgument("spawn_robot", default_value="true", description="Spawn UR10e from sim URDF."),
            DeclareLaunchArgument(
                "start_controllers",
                default_value="true",
                description="Spawn joint_state_broadcaster and joint_trajectory_controller.",
            ),
            DeclareLaunchArgument(
                "calibration_yaml",
                default_value="/home/andy/ur10e_ros2_ws/src/ur10e_bringup/config/ur10e_calibration.yaml",
                description="UR10e calibration YAML used by ur_description.",
            ),
            DeclareLaunchArgument(
                "xacro_path",
                default_value="/opt/ros/humble/share/ur_description/urdf/ur.urdf.xacro",
                description="ur_description UR xacro.",
            ),
            SetEnvironmentVariable("GZ_SIM_RESOURCE_PATH", gz_resource_path),
            OpaqueFunction(function=_gazebo_actions, args=[run_gazebo, headless]),
            OpaqueFunction(
                function=_robot_actions,
                args=[spawn_robot, start_controllers, calibration_yaml, xacro_path],
            ),
        ]
    )
