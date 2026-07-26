from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            DeclareLaunchArgument("enable_motion", default_value="false"),
            DeclareLaunchArgument(
                "config",
                default_value="src/ur10e_example_controllers/config/guarded_contact_recovery_shadow.yaml",
            ),
            Node(
                package="ur10e_example_controllers",
                executable="guarded_contact_recovery_node",
                name="guarded_contact_recovery_shadow",
                output="screen",
                parameters=[
                    {
                        "enable_motion": LaunchConfiguration("enable_motion"),
                        "config": LaunchConfiguration("config"),
                    }
                ],
            ),
        ]
    )
