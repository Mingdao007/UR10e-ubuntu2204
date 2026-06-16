from setuptools import find_packages, setup

package_name = "ur10e_step5d_remote"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/config", ["config/default_step5d_remote.yaml"]),
        (f"share/{package_name}/launch", ["launch/step5d_remote_shadow.launch.py"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="andy",
    maintainer_email="andy@example.com",
    description="Offline Step5d ROS2 remote-control shadow replay for the UR10e bench.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "replay_shadow = ur10e_step5d_remote.replay_shadow:main",
            "step5d_remote_control_node = ur10e_step5d_remote.step5d_remote_control_node:main",
        ],
    },
)
