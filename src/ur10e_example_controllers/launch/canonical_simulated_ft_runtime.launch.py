from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from ur10e_example_controllers import canonical_wrench_contract as contract


def generate_launch_description() -> LaunchDescription:
    canonical_wrench_topic = LaunchConfiguration("canonical_wrench_topic")
    simulated_ft_wrench_topic = LaunchConfiguration("simulated_ft_wrench_topic")
    simulated_ft_status_topic = LaunchConfiguration("simulated_ft_status_topic")
    contact_state_topic = LaunchConfiguration("contact_state_topic")
    controller_status_topic = LaunchConfiguration("controller_status_topic")
    run_metadata_topic = LaunchConfiguration("run_metadata_topic")
    dry_run_summary = LaunchConfiguration("dry_run_summary")
    runtime_observation_summary = LaunchConfiguration("runtime_observation_summary")
    publish_hz = LaunchConfiguration("publish_hz")
    max_samples = LaunchConfiguration("max_samples")

    return LaunchDescription(
        [
            DeclareLaunchArgument("canonical_wrench_topic", default_value=contract.CANONICAL_WRENCH_TOPIC),
            DeclareLaunchArgument("simulated_ft_wrench_topic", default_value=contract.SIMULATED_FT_WRENCH_TOPIC),
            DeclareLaunchArgument("simulated_ft_status_topic", default_value=contract.SIMULATED_FT_STATUS_TOPIC),
            DeclareLaunchArgument("contact_state_topic", default_value=contract.CONTACT_STATE_TOPIC),
            DeclareLaunchArgument("controller_status_topic", default_value=contract.CONTROLLER_STATUS_TOPIC),
            DeclareLaunchArgument("run_metadata_topic", default_value=contract.RUN_METADATA_TOPIC),
            DeclareLaunchArgument("dry_run_summary", default_value=""),
            DeclareLaunchArgument("runtime_observation_summary", default_value=""),
            DeclareLaunchArgument("publish_hz", default_value="50.0"),
            DeclareLaunchArgument("max_samples", default_value="100"),
            Node(
                package="ur10e_example_controllers",
                executable="canonical_simulated_ft_runtime",
                name="canonical_simulated_ft_runtime",
                output="screen",
                arguments=[
                    "--canonical-wrench-topic",
                    canonical_wrench_topic,
                    "--simulated-ft-wrench-topic",
                    simulated_ft_wrench_topic,
                    "--simulated-ft-status-topic",
                    simulated_ft_status_topic,
                    "--contact-state-topic",
                    contact_state_topic,
                    "--controller-status-topic",
                    controller_status_topic,
                    "--run-metadata-topic",
                    run_metadata_topic,
                    "--dry-run-summary",
                    dry_run_summary,
                    "--runtime-observation-summary",
                    runtime_observation_summary,
                    "--publish-hz",
                    publish_hz,
                    "--max-samples",
                    max_samples,
                ],
                parameters=[
                    {
                        "live_robot_command_authorized": False,
                        "bridge_start_authorized": False,
                        "payload_tcp_safety_writes_authorized": False,
                        "source_switching_policy": "launch_config_or_remap_only_no_controller_logic",
                    }
                ],
            ),
        ]
    )
