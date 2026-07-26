from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    ur_type = LaunchConfiguration("ur_type")
    robot_ip = LaunchConfiguration("robot_ip")
    launch_rviz = LaunchConfiguration("launch_rviz")
    kinematics_params_file = LaunchConfiguration("kinematics_params_file")
    headless_mode = LaunchConfiguration("headless_mode")
    reverse_ip = LaunchConfiguration("reverse_ip")
    launch_dashboard_client = LaunchConfiguration("launch_dashboard_client")
    controller_spawner_timeout = LaunchConfiguration("controller_spawner_timeout")
    initial_joint_controller = LaunchConfiguration("initial_joint_controller")
    activate_joint_controller = LaunchConfiguration("activate_joint_controller")

    ur_control = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            PathJoinSubstitution(
                [
                    FindPackageShare("ur_robot_driver"),
                    "launch",
                    "ur_control.launch.py",
                ]
            )
        ),
        launch_arguments={
            "ur_type": ur_type,
            "robot_ip": robot_ip,
            "launch_rviz": launch_rviz,
            "kinematics_params_file": kinematics_params_file,
            "headless_mode": headless_mode,
            "reverse_ip": reverse_ip,
            "launch_dashboard_client": launch_dashboard_client,
            "controller_spawner_timeout": controller_spawner_timeout,
            "initial_joint_controller": initial_joint_controller,
            "activate_joint_controller": activate_joint_controller,
        }.items(),
    )

    return LaunchDescription(
        [
            DeclareLaunchArgument("ur_type", default_value="ur10e"),
            DeclareLaunchArgument("robot_ip", default_value="192.168.1.18"),
            DeclareLaunchArgument("launch_rviz", default_value="false"),
            DeclareLaunchArgument("headless_mode", default_value="true"),
            DeclareLaunchArgument("reverse_ip", default_value="192.168.1.10"),
            DeclareLaunchArgument("launch_dashboard_client", default_value="false"),
            DeclareLaunchArgument("controller_spawner_timeout", default_value="20"),
            DeclareLaunchArgument("initial_joint_controller", default_value="scaled_joint_trajectory_controller"),
            DeclareLaunchArgument("activate_joint_controller", default_value="false"),
            DeclareLaunchArgument(
                "kinematics_params_file",
                default_value=PathJoinSubstitution(
                    [
                        FindPackageShare("ur10e_bringup"),
                        "config",
                        "ur10e_calibration.yaml",
                    ]
                ),
            ),
            ur_control,
        ]
    )
