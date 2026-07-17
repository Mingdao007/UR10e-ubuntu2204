#!/usr/bin/env python3
"""Validate the release test topology for robot protocol changes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SCHEMA = "robotics.integration-test-matrix/v1"
REQUIRED_LANES = {"small", "medium", "large_ursim", "hil_no_motion"}


class MatrixError(RuntimeError):
    pass


def validate(root: Path, path: Path) -> dict[str, Any]:
    root = root.resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise MatrixError("test matrix schema differs")
    lanes = payload.get("lanes")
    if not isinstance(lanes, dict) or set(lanes) != REQUIRED_LANES:
        raise MatrixError("test matrix must define exact small/medium/URSim/HIL lanes")
    for name, lane in lanes.items():
        command = lane.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            raise MatrixError(f"{name} command must be a non-empty argv list")
    for name in ("large_ursim", "hil_no_motion"):
        lane = lanes[name]
        if lane.get("test_doubles_allowed") is not False or lane.get("serial") is not True:
            raise MatrixError(f"{name} must be serial and forbid test doubles")
        if lane.get("motion_allowed") is not False:
            raise MatrixError(f"{name} must be explicitly no-motion")
    image = lanes["large_ursim"].get("container_image", "")
    if "@sha256:" not in image or len(image.rsplit("@sha256:", 1)[1]) != 64:
        raise MatrixError("URSim image must be pinned by SHA-256 digest")
    if (
        lanes["large_ursim"].get("safety_setup")
        != "official_noVNC_confirmation_persisted"
        or not lanes["large_ursim"].get("programs_volume")
        or not lanes["large_ursim"].get("control_volume")
    ):
        raise MatrixError("URSim safety confirmation must use a declared persistent volume")
    requirements = payload.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        raise MatrixError("test matrix requirements are absent")
    identifiers: set[str] = set()
    for requirement in requirements:
        identifier = requirement.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise MatrixError("test matrix requirement ids must be unique")
        identifiers.add(identifier)
        requirement_lanes = set(requirement.get("lanes", []))
        if "small" not in requirement_lanes or not requirement_lanes <= REQUIRED_LANES:
            raise MatrixError(f"{identifier} lacks a valid small-test lane")
        fixture = requirement.get("incident_fixture")
        if fixture is not None and not (root / fixture).is_file():
            raise MatrixError(f"{identifier} incident fixture is missing")
    startup = next(
        (item for item in requirements if item.get("id") == "tp_play_startup_transition"),
        None,
    )
    if startup is None or set(startup["lanes"]) != REQUIRED_LANES:
        raise MatrixError("TP startup transition must cross every test lane")
    return {
        "schema": "robotics.integration-test-matrix-validation/v1",
        "ok": True,
        "lanes": sorted(lanes),
        "requirements": sorted(identifiers),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--matrix", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    path = args.matrix or root / "config/robotics_integration_test_matrix_v1.json"
    try:
        report = validate(root, path)
    except (MatrixError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
