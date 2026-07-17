from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.release import ReleaseError, verify_release_config


def _current() -> dict:
    return json.loads((ROOT / "config/step5/current.json").read_text(encoding="utf-8"))


def test_current_config_is_a_frozen_live_release() -> None:
    report = verify_release_config(ROOT)
    assert report["ok"] is True
    assert report["deployment_id"] == "step5d-autotune-v2-live-20260718-r15"
    assert report["bridge_profile"] == {
        "sample_rate_hz": 500,
        "target_force_n": "12",
        "normal_max_rate_rad_s": "0.05",
        "host_qdot_slew_rad_s2": "0.5",
        "tp_speedj_accel_rad_s2": "0.5",
        "qdot_cap_rad_s": "0.5",
        "execution_profile_id": 533,
        "raw_normal_guard_n": "60",
        "force_norm_guard_n": "100",
        "torque_norm_guard_nm": "3",
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda value: value["deployment"].__setitem__("code_fingerprint", "auto"), "code fingerprint"),
        (lambda value: value["deployment"].__setitem__("guard_fingerprint", "auto"), "guard fingerprint"),
        (lambda value: value["deployment"].__setitem__("authorized", False), "authorization"),
        (lambda value: value["bridge"].__setitem__("live_enabled", False), "live bridge"),
        (lambda value: value["bridge"].__setitem__("argv", []), "bridge argv"),
        (
            lambda value: value["bridge"]["environment"].pop(
                "STEP5D_AUTOTUNE_V2_STARTUP_GATE_REQUIRED"
            ),
            "bridge environment",
        ),
        (
            lambda value: value["bridge"]["environment"].__setitem__(
                "STEP5D_AUTOTUNE_V2_STARTUP_STABLE_S", "0.4"
            ),
            "bridge environment",
        ),
        (lambda value: value["bridge"].__setitem__("startup_stable_s", 0.4), "startup stable"),
        (
            lambda value: value["bridge"].__setitem__("operator_play_timeout_s", 60),
            "operator Play timeout",
        ),
        (lambda value: value["live_cutover"].__setitem__("enabled", False), "cutover"),
        (lambda value: value["live_cutover"].__setitem__("blocked_until", ["unfinished"]), "cutover blocker"),
    ],
)
def test_release_gate_rejects_schema_valid_but_unreleasable_config(
    tmp_path: Path, mutate, message: str
) -> None:
    value = copy.deepcopy(_current())
    mutate(value)
    if "fingerprint" not in message:
        value["deployment"]["code_fingerprint"] = "auto"
        value["deployment"]["guard_fingerprint"] = "auto"
    path = tmp_path / "current.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ReleaseError, match=message):
        verify_release_config(ROOT, path=path, verify_fingerprints=False)
