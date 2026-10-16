"""Canonical Figure-eight Home identity shared by the live entry and recovery."""
from __future__ import annotations

import json
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "figure8_home_v1.json"
EXPECTED_PROFILE_ID = "step6.autotune/figure8-contact-derived-home-v1"
EXPECTED_POSE = (
    0.4620551816,
    0.1778825964,
    0.03408876139925415,
    3.120752062,
    0.0,
    0.068626833,
)
EXPECTED_HOME_Q = (
    0.7451654076576233,
    -1.8181091747679652,
    -2.5626940727233887,
    -0.3122711938670655,
    1.5276236534118652,
    -0.8238123098956507,
)


def load_canonical_figure8_home(path: Path = CONFIG_PATH) -> tuple[float, ...]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema") != "tase.figure8/canonical-home-v1":
        raise ValueError("Figure-eight canonical Home schema differs")
    if document.get("status") != "canonical_for_figure8_and_autotuner":
        raise ValueError("Figure-eight canonical Home is not active")
    if document.get("home_profile_id") != EXPECTED_PROFILE_ID:
        raise ValueError("Figure-eight canonical Home profile differs")
    pose = tuple(float(value) for value in document.get("pose_m_rad", ()))
    if len(pose) != 6 or not all(math.isfinite(value) for value in pose):
        raise ValueError("Figure-eight canonical Home pose is not finite six-dimensional")
    if pose != EXPECTED_POSE:
        raise ValueError("Figure-eight canonical Home pose differs from the reviewed R013 receipt")
    orientation = tuple(float(value) for value in document.get("orientation_rotvec_rad", ()))
    if orientation != EXPECTED_POSE[3:]:
        raise ValueError("Figure-eight canonical orientation differs from the reviewed invariant")
    return pose


def load_canonical_figure8_home_q(path: Path = CONFIG_PATH) -> tuple[float, ...]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema") != "tase.figure8/canonical-home-v1":
        raise ValueError("Figure-eight canonical Home schema differs")
    joints = tuple(float(value) for value in document.get("joint_positions_rad", ()))
    if len(joints) != 6 or not all(math.isfinite(value) for value in joints):
        raise ValueError("Figure-eight canonical Home joints are not finite six-dimensional")
    if joints != EXPECTED_HOME_Q:
        raise ValueError("Figure-eight canonical Home joints differ from the approved target")
    return joints


CANONICAL_FIGURE8_HOME_POSE = load_canonical_figure8_home()
CANONICAL_FIGURE8_HOME_Q = load_canonical_figure8_home_q()
