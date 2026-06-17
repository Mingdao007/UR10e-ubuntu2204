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
                "config/contact_cycloid_shadow.yaml",
                "config/guarded_contact_recovery_shadow.yaml",
            ],
        ),
        (
            f"share/{package_name}/launch",
            [
                "launch/no_contact_cycloid_shadow.launch.py",
                "launch/contact_cycloid_shadow.launch.py",
                "launch/guarded_contact_recovery_shadow.launch.py",
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
            "step5a_driver_readiness_check = ur10e_example_controllers.step5a_driver_readiness_check:main",
            "step5a_cartesian_cycloid_motion = ur10e_example_controllers.step5a_cartesian_cycloid_motion:main",
            "step5a_gate_a_audit = ur10e_example_controllers.step5a_gate_a_audit:main",
            "step5a_joint_proxy_motion_probe = ur10e_example_controllers.step5a_joint_proxy_motion_probe:main",
            "guarded_contact_recovery_node = ur10e_example_controllers.guarded_contact_recovery_node:main",
        ],
    },
)
