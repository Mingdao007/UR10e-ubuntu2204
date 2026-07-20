from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

import run_step5d_tacdiffusion_bridge as bridge


def calibration(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(
        json.dumps(
            {
                "status": "verified",
                "frame_token": 5_252_001,
                "normal_force_axis": "fz",
                "normal_force_sign": 1,
                "wrench_transform_sensor_to_tcp_6x6": [
                    [1 if row == column else 0 for column in range(6)] for row in range(6)
                ],
                "evidence": {"sha256": "a" * 64},
            }
        )
    )
    return path


def test_status_fails_closed_without_calibration() -> None:
    result = bridge.status(bridge.ROOT / "config/does-not-exist.json")
    assert result["live_ready"] is False
    assert result["motion_performed"] is False
    assert "calibration_error" in result


def test_package_readiness_is_bound_to_fresh_controller_readback() -> None:
    package = bridge.package_hashes()
    readiness = bridge.validate_package_readiness(bridge.READINESS_DEFAULT, package)
    assert readiness["package"]["controller_readback_verified"] is True
    drifted = dict(package)
    drifted[".script"] = "0" * 64
    with pytest.raises(RuntimeError, match="readiness_package_hash_mismatch"):
        bridge.validate_package_readiness(bridge.READINESS_DEFAULT, drifted)


def test_precontact_and_direct_packets_share_registers_without_arming_early() -> None:
    precontact = bridge.precontact_doubles((0.0,) * 6, heartbeat=1, stage=25.3, sensor_ok=True)
    clear = bridge.precontact_doubles((0.0,) * 6, heartbeat=2, stage=25.95, sensor_ok=True)
    assert len(precontact) == 24
    assert precontact[19] == 1.0
    assert precontact[23] == 521.0
    assert clear[13:] == [0.0] * 11


def test_calibration_and_authorization_are_hash_bound(tmp_path) -> None:
    calibration_path = calibration(tmp_path)
    _, calibration_hash = bridge.validate_calibration(calibration_path)
    package = bridge.package_hashes()
    authorization = tmp_path / "authorization.json"
    authorization.write_text(
        json.dumps(
            {
                "schema": "step5d_tacdiffusion_live_authorization_v1",
                "program": bridge.PROGRAM,
                "motion_scope": "no_contact",
                "explicit_user_authorization": True,
                "controller_verified": True,
                "polyscope_version": "5.26.0",
                "package_readback_verified": True,
                "calibration_sha256": calibration_hash,
                "package_sha256": package,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "review_gate": {"passed": True},
                "shadow": {
                    "kind": "fixture_shadow",
                    "diagnostic_only": True,
                    "command_invariant": True,
                    "runtime_fallback_allowed": False,
                    "checkpoint_bound": False,
                    "model_active": False,
                },
            }
        )
    )
    assert bridge.validate_authorization(
        authorization,
        calibration_sha256=calibration_hash,
        local_package_sha256=package,
    )["motion_scope"] == "no_contact"
    payload = json.loads(authorization.read_text())
    payload["package_sha256"][".script"] = "0" * 64
    authorization.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="package_hash_mismatch"):
        bridge.validate_authorization(
            authorization,
            calibration_sha256=calibration_hash,
            local_package_sha256=package,
        )


def test_bridge_source_has_no_program_load_or_play_command() -> None:
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    forbidden_fragments = (
        '"load"',
        '"play"',
        "dashboard_exchange",
        "zero_ftsensor",
        "set_tcp(",
        "set_payload(",
    )
    assert not [fragment for fragment in forbidden_fragments if fragment in source]


def test_bench_network_defaults_match_existing_kunwei_client_topology() -> None:
    source = bridge.Path(bridge.__file__).read_text(encoding="utf-8")
    assert 'parser.add_argument("--robot-host", default="192.168.1.18")' in source
    assert 'parser.add_argument("--sensor-ip", default="192.168.50.25")' in source
    assert 'parser.add_argument("--sensor-port", type=int, default=5152)' in source
    assert "socket.create_connection" in source
    assert ".listen(" not in source
