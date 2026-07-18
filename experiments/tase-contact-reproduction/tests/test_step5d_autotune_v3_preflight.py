from __future__ import annotations

import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_step5d_autotune_v3 as gate  # noqa: E402


def test_exact_v3_stopped_or_ready_home_program_binding() -> None:
    assert gate._program_safe_for_bridge(
        {"programState": "STOPPED /programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3.urp"},
        {"output_int_register_30": 33},
    )["ok"] is True
    assert gate._program_safe_for_bridge(
        {"programState": "STOPPED /programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v1.urp"},
        {},
    )["ok"] is False
    ready_home = {f"output_int_register_{index}": 0 for index in range(24, 31)}
    ready_home["output_int_register_26"] = 10
    result = gate._program_safe_for_bridge(
        {"programState": "PLAYING /programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3.urp"},
        ready_home,
    )
    assert result["ok"] is True
    assert result["mode"] == "playing_ready_home_zero_identity"


def test_playing_v3_before_bridge_requires_ready_home_zero_identity() -> None:
    base = {f"output_int_register_{index}": 0 for index in range(24, 31)}
    dashboard = {
        "programState": "PLAYING /programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3.urp"
    }
    assert gate._program_safe_for_bridge(dashboard, base)["ok"] is False
    assert gate._program_safe_for_bridge(
        dashboard,
        {**base, "output_int_register_26": 10, "output_int_register_24": 1},
    )["ok"] is False


def test_stationary_predicate_uses_tcp_and_joint_speed() -> None:
    assert gate._stationary(
        {"actual_TCP_speed": [0.0] * 6, "actual_qd": [0.0] * 6}
    )["ok"] is True
    assert gate._stationary(
        {"actual_TCP_speed": [0.002, 0, 0, 0, 0, 0], "actual_qd": [0.0] * 6}
    )["ok"] is False
    assert gate._stationary(
        {"actual_TCP_speed": [0.0] * 6, "actual_qd": [0.006, 0, 0, 0, 0, 0]}
    )["ok"] is False


def test_mailbox_must_be_absent_before_live_bridge(tmp_path: Path) -> None:
    mailbox = tmp_path / "command.json"
    assert gate._mailbox_initial_zero(mailbox)["ok"] is True
    mailbox.write_text("{}", encoding="utf-8")
    assert gate._mailbox_initial_zero(mailbox)["ok"] is False


def test_controller_identity_is_canonical_and_sensitive() -> None:
    dashboard = {
        "PolyscopeVersion": "5.11",
        "robotmode": "RUNNING",
        "safetymode": "NORMAL",
        "programState": "STOPPED step5d_strict_rnn_autotune_v3.urp",
    }
    rtde = {"robot_mode": 7, "safety_mode": 1, "runtime_state": 1}
    first, first_sha = gate._controller_identity(dashboard, rtde, robot_host="192.168.1.18")
    second, second_sha = gate._controller_identity(dict(reversed(list(dashboard.items()))), rtde, robot_host="192.168.1.18")
    assert first == second
    assert first_sha == second_sha
    _, changed = gate._controller_identity(
        {**dashboard, "safetymode": "PROTECTIVE_STOP"}, rtde, robot_host="192.168.1.18"
    )
    assert changed != first_sha


def test_parallel_probe_dag_runs_each_probe_once_and_concurrently() -> None:
    calls: list[str] = []

    def probe(name: str) -> dict[str, bool]:
        calls.append(name)
        time.sleep(0.08)
        return {"ok": True}

    started = time.monotonic()
    result = gate._parallel(
        {name: (lambda name=name: probe(name)) for name in ("a", "b", "c", "d")}
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.20
    assert sorted(calls) == ["a", "b", "c", "d"]
    assert all(item["value"]["ok"] is True for item in result.values())


def test_connect_observation_requires_open_socket() -> None:
    assert gate._connect_observation_ok({"value": {"open": True}}) is True
    assert gate._connect_observation_ok({"value": {"open": False}}) is False
    assert gate._connect_observation_ok({"value": {"ok": False, "open": True}}) is False
