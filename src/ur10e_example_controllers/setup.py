from setuptools import find_packages, setup

package_name = "ur10e_example_controllers"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (
            f"share/{package_name}/config",
            [
                "config/no_contact_cycloid.yaml",
                "config/historical_5a_fixed_z.yaml",
                "config/contact_cycloid_shadow.yaml",
                "config/guarded_contact_recovery_shadow.yaml",
                "config/gazebo_matrix_controllers.yaml",
                "config/gazebo_matrix_initial_positions.yaml",
                "config/gazebo_v2_velocity_controllers.yaml",
                "config/gazebo_v2_effort_surrogate_controllers.yaml",
                "config/gazebo_v2_initial_positions.yaml",
            ],
        ),
        (
            f"share/{package_name}/launch",
            [
                "launch/no_contact_cycloid_shadow.launch.py",
                "launch/contact_cycloid_shadow.launch.py",
                "launch/guarded_contact_recovery_shadow.launch.py",
                "launch/step5b_simulation_mvp.launch.py",
                "launch/ur10e_gazebo_matrix.launch.py",
                "launch/ur10e_gazebo_v2_fortress.launch.py",
                "launch/canonical_simulated_ft_runtime.launch.py",
            ],
        ),
        (
            f"share/{package_name}/worlds",
            [
                "worlds/step5_table_world.sdf",
                "worlds/ur10e_gazebo_v2_fortress.sdf",
            ],
        ),
        (
            f"share/{package_name}/meshes/eoat",
            [
                "meshes/eoat/ur5e_ksm8n_ball_transfer_tool_v13_assembly.stl",
            ],
        ),
        (
            f"share/{package_name}/meshes/contact_surface",
            [
                "meshes/contact_surface/two_piece_surface_smooth_v11_3mm_thick.stl",
                "meshes/contact_surface/coupon_v11_smooth_seam_30x30_3mm.stl",
            ],
        ),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="andy",
    maintainer_email="andy@example.com",
    description="UR10e example controller probes and offline shadows for the current bench.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "no_contact_cycloid_shadow = ur10e_example_controllers.no_contact_cycloid_shadow:main",
            "contact_cycloid_shadow = ur10e_example_controllers.contact_cycloid_shadow:main",
            "guarded_contact_recovery_shadow = ur10e_example_controllers.guarded_contact_recovery_shadow:main",
            "no_contact_motion_probe = ur10e_example_controllers.no_contact_motion_probe:main",
            "kunwei_force_gate = ur10e_example_controllers.kunwei_force_gate:main",
            "kunwei_persistent_gate = ur10e_example_controllers.kunwei_persistent_monitor:main",
            "step5b_zero_policy_readiness = ur10e_example_controllers.step5b_zero_policy_readiness:main",
            "step5b_contact_live_runner = ur10e_example_controllers.step5b_contact_live_runner:main",
            "step5b_velocity_admittance_runner = ur10e_example_controllers.step5b_velocity_admittance_runner:main",
            "step5a_driver_readiness_check = ur10e_example_controllers.step5a_driver_readiness_check:main",
            "step5a_cartesian_cycloid_motion = ur10e_example_controllers.step5a_cartesian_cycloid_motion:main",
            "step5a_return_to_anchor_motion = ur10e_example_controllers.step5a_return_to_anchor_motion:main",
            "step5a_historical_fixed_z_motion = ur10e_example_controllers.step5a_historical_fixed_z_motion:main",
            "step5a_gate_a_audit = ur10e_example_controllers.step5a_gate_a_audit:main",
            "step5a_joint_proxy_motion_probe = ur10e_example_controllers.step5a_joint_proxy_motion_probe:main",
            "guarded_contact_recovery_node = ur10e_example_controllers.guarded_contact_recovery_node:main",
            "step5b_simulation_mvp = ur10e_example_controllers.step5b_simulation_mvp:main",
            "step56_simulation_matrix = ur10e_example_controllers.step56_simulation_matrix:main",
            "canonical_simulated_ft_runtime = ur10e_example_controllers.canonical_simulated_ft_runtime:main",
            "ur10e_gazebo_matrix_runner = ur10e_example_controllers.ur10e_gazebo_matrix_runner:main",
        ],
    },
)
