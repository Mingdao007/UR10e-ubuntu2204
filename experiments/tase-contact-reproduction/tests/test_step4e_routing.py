from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import resolve_step4e_route as resolver  # noqa: E402
import upload_ur_tp_package as uploader  # noqa: E402


def expected_route(number: int) -> dict[str, str]:
    version = f"v{number}"
    if number <= 20:
        basename = f"step4e_line_outerloop_{version}"
    elif number == 21:
        basename = "step4e_detached_movel_minrot_v21"
    else:
        basename = f"step4e_seed_normal_loop_{version}"
    nested = (
        number <= 12
        or number in {14, 15, 17}
        or 19 <= number <= 27
    )
    controller_dir = "/programs/andyl/kunwei/step4"
    if nested:
        controller_dir += "/step4e"
    local_dir = "programs" if number == 31 else "programs/step4e"
    if number == 31:
        lifecycle = "current"
    elif number == 30:
        lifecycle = "previous"
    elif number == 29:
        lifecycle = "fallback"
    elif number in {17, 19, 20} or 23 <= number <= 28:
        lifecycle = "failed_archive"
    else:
        lifecycle = "historical"
    return {
        "version": version,
        "program_basename": basename,
        "local_dir": local_dir,
        "controller_dir": controller_dir,
        "controller_urp": f"{controller_dir}/{basename}.urp",
        "run_label": basename,
        "lifecycle": lifecycle,
    }


def test_compact_route_table_covers_v1_through_v31_exactly() -> None:
    table = resolver.DEFAULT_TABLE
    assert table.stat().st_size < 12 * 1024
    assert len(table.read_text(encoding="utf-8").splitlines()) < 200
    routes, payload = resolver.load_routes()
    assert set(routes) == set(range(1, 32))
    assert payload["current_version"] == "v31"
    for number in range(1, 32):
        assert resolver.resolve(f"v{number}") == expected_route(number)


def test_every_route_matches_local_triplet_and_urp_controller_metadata() -> None:
    for number in range(1, 32):
        route = resolver.resolve(f"v{number}")
        stem = ROOT / route["local_dir"] / route["program_basename"]
        for extension in (".script", ".txt", ".urp"):
            assert stem.with_suffix(extension).is_file(), (number, extension)
        with gzip.open(stem.with_suffix(".urp"), "rb") as handle:
            root = ET.fromstring(handle.read())
        assert root.tag == "URProgram"
        assert root.attrib["name"] == route["program_basename"]
        assert root.attrib["directory"] == route["controller_dir"]


@pytest.mark.parametrize(
    "version",
    ["", "v0", "v32", "31", "current", "v1/../../step5", "step4f_v1"],
)
def test_unknown_or_unsafe_version_fails_closed(version: str) -> None:
    with pytest.raises(resolver.RouteError):
        resolver.resolve(version)


def test_overlapping_family_and_path_traversal_fail_closed(tmp_path: Path) -> None:
    payload = json.loads(resolver.DEFAULT_TABLE.read_text(encoding="utf-8"))
    payload["route_families"].append(
        {
            "selector": {"ranges": [], "versions": [31]},
            "basename_template": "step4e_seed_normal_loop_{version}",
            "local_dir": "programs",
            "controller_dir": "/programs/andyl/kunwei/step4",
        }
    )
    overlap = tmp_path / "overlap.json"
    overlap.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(resolver.RouteError, match="overlaps"):
        resolver.load_routes(overlap)

    payload = json.loads(resolver.DEFAULT_TABLE.read_text(encoding="utf-8"))
    payload["route_families"][0]["local_dir"] = "programs/../step5"
    traversal = tmp_path / "traversal.json"
    traversal.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(resolver.RouteError, match="unsafe local_dir"):
        resolver.load_routes(traversal)


def test_route_table_symlink_fails_closed(tmp_path: Path) -> None:
    table_link = tmp_path / "table.json"
    table_link.symlink_to(resolver.DEFAULT_TABLE)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "resolve_step4e_route.py"),
            "--table",
            str(table_link),
            "--version",
            "v31",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "route table must be a regular file" in completed.stderr


def test_historical_wrappers_are_location_independent_and_version_bound() -> None:
    base = (ROOT / "scripts" / "step4e-line-v1-operator.sh").read_text(
        encoding="utf-8"
    )
    assert "/home/andy/ur10e_ros2_ws" not in base
    assert 'REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"' in base
    for number in range(2, 24):
        path = ROOT / "scripts" / f"step4e-line-v{number}-operator.sh"
        text = path.read_text(encoding="utf-8")
        assert 'readlink -f "${BASH_SOURCE[0]}"' in text
        assert "/home/andy/ur10e_ros2_ws" not in text
        assert f'export STEP4E_VERSION="v{number}"' in text
    current = (ROOT / "scripts" / "step4e-line-operator.sh").read_text(
        encoding="utf-8"
    )
    assert 'STEP4E_VERSION="${STEP4E_VERSION:-v31}"' in current
    assert "/home/andy/ur10e_ros2_ws" not in current


@pytest.mark.parametrize("number", [1, 12, 13, 20, 21, 22, 27, 28, 29, 30, 31])
def test_operator_route_info_is_no_network_and_matches_resolver(number: int) -> None:
    environment = dict(os.environ)
    environment["STEP4E_VERSION"] = f"v{number}"
    completed = subprocess.run(
        [str(ROOT / "scripts" / "step4e-line-v1-operator.sh"), "route-info"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=3.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected_route(number)


def test_current_wrapper_route_info_defaults_to_v31() -> None:
    environment = dict(os.environ)
    environment.pop("STEP4E_VERSION", None)
    completed = subprocess.run(
        [str(ROOT / "scripts" / "step4e-line-operator.sh"), "route-info"],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=3.0,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected_route(31)


@pytest.mark.parametrize("number", [1, 13, 21, 27, 28, 31])
def test_tp_delivery_uses_step4e_owner_route(number: int) -> None:
    expected = expected_route(number)
    resolution = uploader.resolve_table_target(expected["program_basename"])
    assert resolution == {
        "row_id": f"v{number}",
        "source": (
            "config/step4e_stage_table.json"
            f"#route_families[version=v{number}]"
        ),
        "controller_dir": expected["controller_dir"],
        "controller_target": expected["controller_urp"],
    }
