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
                default_value="src/ur10e_example_controllers/config/no_contact_cycloid.yaml",
            ),
            Node(
                package="ur10e_example_controllers",
                executable="no_contact_cycloid_shadow",
                name="no_contact_cycloid_shadow",
                output="screen",
                arguments=["--config", LaunchConfiguration("config")],
                parameters=[
                    {
                        "enable_motion": LaunchConfiguration("enable_motion"),
                        "config": LaunchConfiguration("config"),
                    }
                ],
            ),
        ]
    )
