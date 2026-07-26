from glob import glob

from setuptools import find_packages, setup


package_name = "ur10e_experiment_runtime"


setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test", "tests"]),
    package_data={package_name: ["schemas/*.json"]},
    data_files=[
        ("share/ament_index/resource_index/packages", [f"resource/{package_name}"]),
        (f"share/{package_name}", ["package.xml"]),
        (f"share/{package_name}/schemas", glob("ur10e_experiment_runtime/schemas/*.json")),
    ],
    install_requires=["jsonschema>=4.0", "setuptools"],
    zip_safe=True,
    maintainer="andy",
    maintainer_email="andy@example.com",
    description="Manifest-first, offline-only experiment runtime scaffolding for UR10e campaigns.",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "ur-exp = ur10e_experiment_runtime.cli:main",
        ],
    },
)
