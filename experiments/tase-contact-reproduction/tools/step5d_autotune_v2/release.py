"""Executable release gate for the production Step5d autotune v2 bridge."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import ConfigError, load_static_config


DEPLOYMENT_ID = "step5d-autotune-v2-live-20260717-r6"
TP_DELIVERY_ID = "step5d-autotune-v2-readback-20260717-r1"
PROGRAM = "step5d_strict_rnn_autotune_v2"
BRIDGE_ARGV = ("python3", "{root}/tools/run_step5d_autotune_v2_bridge.py")
BRIDGE_ENVIRONMENT = {"STEP5D_AUTOTUNE_V2_ADAPTER": "1"}
PROFILE = {
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
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReleaseError(RuntimeError):
    """The canonical config is valid data but not a frozen live release."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def verify_release_config(
    root: Path,
    *,
    path: Path | None = None,
    verify_fingerprints: bool = True,
) -> dict[str, Any]:
    root = root.resolve()
    config_path = path or root / "config/step5/current.json"
    try:
        config = load_static_config(root, config_path)
    except ConfigError as exc:
        raise ReleaseError(f"static config invalid: {exc}") from exc
    payload = config.payload
    deployment = payload["deployment"]
    bridge = payload["bridge"]
    cutover = payload["live_cutover"]

    _require(deployment["id"] == DEPLOYMENT_ID, "release deployment id is not r2")
    _require(
        deployment["tp_delivery_id"] == TP_DELIVERY_ID,
        "release TP delivery/readback identity differs",
    )
    _require(deployment["authorized"] is True, "release authorization is absent")
    _require(
        deployment["controller_readback_verified"] is True,
        "release controller readback is absent",
    )
    _require(payload["controller"]["program"] == PROGRAM, "release controller program differs")
    _require(bridge["live_enabled"] is True, "release live bridge is disabled")
    _require(tuple(bridge["argv"]) == BRIDGE_ARGV, "release bridge argv is not frozen")
    _require(bridge["environment"] == BRIDGE_ENVIRONMENT, "release bridge environment differs")
    _require(cutover["enabled"] is True, "release cutover is disabled")
    _require(cutover["blocked_until"] == [], "release cutover blocker remains")
    if verify_fingerprints:
        _require(
            _SHA256.fullmatch(str(deployment["code_fingerprint"])) is not None
            and deployment["code_fingerprint"] == config.deployment.code_fingerprint,
            "release code fingerprint is not exact",
        )
        _require(
            _SHA256.fullmatch(str(deployment["guard_fingerprint"])) is not None
            and deployment["guard_fingerprint"] == config.deployment.guard_fingerprint,
            "release guard fingerprint is not exact",
        )
    else:
        _require(deployment["code_fingerprint"] != "auto", "release code fingerprint is auto")
        _require(deployment["guard_fingerprint"] != "auto", "release guard fingerprint is auto")

    execution = payload["execution_profile"]
    observed_profile = {
        "sample_rate_hz": 500,
        "target_force_n": "12",
        **execution,
        "execution_profile_id": 533,
        "raw_normal_guard_n": "60",
        "force_norm_guard_n": "100",
        "torque_norm_guard_nm": "3",
    }
    _require(observed_profile == PROFILE, "release bridge execution profile differs")
    required_sources = {
        "tools/step5d_autotune_v2/live_adapter.py",
        "tools/run_step5d_autotune_v2_bridge.py",
        "tools/step5d_autotune_v2/release.py",
    }
    _require(
        required_sources.issubset(set(payload["code_sources"])),
        "release code fingerprint omits bridge adapter sources",
    )
    current_stage = json.loads(
        (root / "config/current_stage.json").read_text(encoding="utf-8")
    )
    _require(
        current_stage.get("current_stage_id") == PROGRAM
        and current_stage.get("program") == PROGRAM,
        "release current-stage pointer differs",
    )
    stage_table = json.loads(
        (root / "config/step5_stage_table.json").read_text(encoding="utf-8")
    )
    current_rows = [
        row
        for row in stage_table.get("stages", [])
        if isinstance(row, dict)
        and row.get("id") == PROGRAM
        and row.get("active") is True
        and (row.get("current_binding") or {}).get("is_current") is True
    ]
    _require(len(current_rows) == 1, "release stage-table binding differs")
    return {
        "schema": "step5d.autotune.release-verification/v2",
        "ok": True,
        "deployment_id": deployment["id"],
        "tp_delivery_id": deployment["tp_delivery_id"],
        "code_fingerprint": config.deployment.code_fingerprint,
        "guard_fingerprint": config.deployment.guard_fingerprint,
        "bridge_profile": observed_profile,
    }
