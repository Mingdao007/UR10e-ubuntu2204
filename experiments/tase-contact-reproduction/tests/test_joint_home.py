import gzip
import xml.etree.ElementTree as ET
import pytest

from build_joint_home import CONTROLLER_DIR, HOME_POSE, HOME_Q, PROGRAM, build
from run_joint_home import _require_readback_validation


def test_joint_home_accepts_canonical_readback_state_and_rejects_invalid_report():
    _require_readback_validation({"pass": True, "state": "controller read-back verified"})
    _require_readback_validation({"pass": True, "status": "controller read-back verified"})
    with pytest.raises(RuntimeError, match="read-back"):
        _require_readback_validation({"pass": False, "state": "controller read-back verified"})
    with pytest.raises(RuntimeError, match="read-back"):
        _require_readback_validation({"pass": True, "state": "read-back invalid"})


def test_joint_home_binds_historical_joint_target(tmp_path):
    initial = [0.7467, -2.0417, -2.1233, 2.6118, -1.5278, 2.3231]
    binding = build(tmp_path / "package", initial)
    script = (tmp_path / "package" / f"{PROGRAM}.script").read_text()
    assert script.count("movej(target_q, a=0.100, v=0.100, t=0.0, r=0.0)") == 1
    assert "stopj(20.0)" in script
    assert "settled_qd_max" not in script
    assert "movel(" not in script
    assert binding["home_q"] == HOME_Q
    assert binding["home_pose"] == HOME_POSE
    assert binding["initial_q"] == initial


def test_joint_home_urp_carries_exact_script(tmp_path):
    package = tmp_path / "package"
    build(package, [0.7467, -2.0417, -2.1233, 2.6118, -1.5278, 2.3231])
    root = ET.fromstring(gzip.decompress((package / f"{PROGRAM}.urp").read_bytes()))
    cached = next(node.text or "" for node in root.iter() if node.tag == "cachedContents")
    script = (package / f"{PROGRAM}.script").read_text()
    assert root.attrib["name"] == PROGRAM
    assert root.attrib["directory"] == CONTROLLER_DIR
    assert cached == script
