from __future__ import annotations

import json
import os
from pathlib import Path

from ament_index_python.packages import get_package_prefix, get_package_share_directory
from launch import LaunchContext, LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction, SetEnvironmentVariable, TimerAction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from ur10e_example_controllers.ur10e_gazebo_matrix_runner import generate_sim_robot_description
from ur10e_example_controllers.ur10e_gazebo_v2 import backend_spec, configure_robot_description, probe_fortress_abi


WORLD_NAME = "ur10e_gazebo_v2_fortress"
ROBOT_NAME = "ur10e_gazebo_v2"


def _bool(context: LaunchContext, value: LaunchConfiguration) -> bool:
    return context.perform_substitution(value).lower() in {"1", "true", "yes", "on"}


def _gazebo_actions(
    context: LaunchContext,
    run_gazebo: LaunchConfiguration,
    headless: LaunchConfiguration,
    world_path: LaunchConfiguration,
    abi_preflight: LaunchConfiguration,
):
    if not _bool(context, run_gazebo):
        return []
    if _bool(context, abi_preflight):
        report = probe_fortress_abi()
        if not report["pass"]:
            raise RuntimeError("Gazebo v2 Fortress ABI preflight failed: " + json.dumps(report, sort_keys=True))
    world = Path(context.perform_substitution(world_path))
    command = ["ign", "gazebo", str(world), "-r"]
    if _bool(context, headless):
        command.extend(["-s", "--headless-rendering"])
    return [ExecuteProcess(cmd=command, name="ur10e_gazebo_v2_fortress_server", output="screen")]


def _robot_actions(
    context: LaunchContext,
    spawn_robot: LaunchConfiguration,
    start_controller: LaunchConfiguration,
    backend: LaunchConfiguration,
    calibration_yaml: LaunchConfiguration,
    xacro_path: LaunchConfiguration,
):
    if not _bool(context, spawn_robot):
        return []
    selected = backend_spec(context.perform_substitution(backend))
    share = Path(get_package_share_directory("ur10e_example_controllers"))
    robot_description = generate_sim_robot_description(
        controllers_yaml=share / "config" / selected.controllers_filename,
        initial_positions_yaml=share / "config" / "gazebo_v2_initial_positions.yaml",
        calibration_yaml=Path(context.perform_substitution(calibration_yaml)),
        xacro_path=Path(context.perform_substitution(xacro_path)),
    )
    robot_description = configure_robot_description(robot_description, backend=selected.name)
    actions = [
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            # ign_ros2_control 0.7.x resolves the default
            # /robot_state_publisher/get_parameters service by node name.
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
                    name="gazebo_v2_create_robot",
                    output="screen",
                    arguments=[
                        "-world",
                        WORLD_NAME,
                        "-name",
                        ROBOT_NAME,
                        "-topic",
                        "/robot_description",
                    ],
                )
            ],
        ),
    ]
    if _bool(context, start_controller):
        actions.append(
            TimerAction(
                period=7.0,
                actions=[
                    Node(
                        package="controller_manager",
                        executable="spawner",
                        name="gazebo_v2_spawn_joint_state_broadcaster",
                        output="screen",
                        arguments=["joint_state_broadcaster", "--controller-manager", "/controller_manager"],
                    ),
                    Node(
                        package="controller_manager",
                        executable="spawner",
                        name=f"gazebo_v2_spawn_{selected.controller_name}",
                        output="screen",
                        arguments=[selected.controller_name, "--controller-manager", "/controller_manager"],
                    ),
                ],
            )
        )
    return actions


def generate_launch_description() -> LaunchDescription:
    headless = LaunchConfiguration("headless")
    run_gazebo = LaunchConfiguration("run_gazebo")
    spawn_robot = LaunchConfiguration("spawn_robot")
    start_controller = LaunchConfiguration("start_controller")
    backend = LaunchConfiguration("backend")
    calibration_yaml = LaunchConfiguration("calibration_yaml")
    xacro_path = LaunchConfiguration("xacro_path")
    world_path = LaunchConfiguration("world_path")
    abi_preflight = LaunchConfiguration("abi_preflight")

    own_share = Path(get_package_share_directory("ur10e_example_controllers"))
    bringup_share = Path(get_package_share_directory("ur10e_bringup"))
    description_share = Path(get_package_share_directory("ur_description"))
    resource_entries = [
        entry for entry in os.environ.get("IGN_GAZEBO_RESOURCE_PATH", "").split(os.pathsep) if entry
    ]
    resource_entries.extend(
        [
            str(own_share.parent),
            str(Path(get_package_share_directory("ur_description")).parent),
        ]
    )
    resource_path = os.pathsep.join(dict.fromkeys(resource_entries))
    plugin_entries = [
        entry for entry in os.environ.get("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", "").split(os.pathsep) if entry
    ]
    plugin_entries.append(str(Path(get_package_prefix("ign_ros2_control")) / "lib"))
    plugin_path = os.pathsep.join(dict.fromkeys(plugin_entries))

    return LaunchDescription(
        [
            DeclareLaunchArgument("backend", default_value="velocity", description="velocity or effort_surrogate"),
            DeclareLaunchArgument("headless", default_value="true", description="Run Fortress server-only."),
            DeclareLaunchArgument("run_gazebo", default_value="true", description="Start ign gazebo 6.x."),
            DeclareLaunchArgument("spawn_robot", default_value="true", description="Spawn the calibrated attached robot."),
            DeclareLaunchArgument(
                "start_controller",
                default_value="true",
                description="Start JSB and exactly one selected Gazebo-only controller.",
            ),
            DeclareLaunchArgument(
                "abi_preflight",
                default_value="true",
                description="Fail before startup unless Fortress 6 and ign_ros2_control are selected.",
            ),
            DeclareLaunchArgument(
                "calibration_yaml",
                default_value=str(bringup_share / "config" / "ur10e_calibration.yaml"),
                description="Read-only calibrated UR10e kinematics source.",
            ),
            DeclareLaunchArgument(
                "xacro_path",
                default_value=str(description_share / "urdf" / "ur.urdf.xacro"),
                description="Humble ur_description xacro.",
            ),
            DeclareLaunchArgument(
                "world_path",
                default_value=str(own_share / "worlds" / "ur10e_gazebo_v2_fortress.sdf"),
                description="Fortress-only v2 world.",
            ),
            SetEnvironmentVariable("IGN_GAZEBO_RESOURCE_PATH", resource_path),
            SetEnvironmentVariable("IGN_GAZEBO_SYSTEM_PLUGIN_PATH", plugin_path),
            OpaqueFunction(
                function=_gazebo_actions,
                args=[run_gazebo, headless, world_path, abi_preflight],
            ),
            OpaqueFunction(
                function=_robot_actions,
                args=[spawn_robot, start_controller, backend, calibration_yaml, xacro_path],
            ),
        ]
    )
