from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import preflight_step5d_autotune_v3 as gate  # noqa: E402
from step5d_autotune_v3 import preflight_support as support  # noqa: E402


EXPECTED_PROGRAM = (
    "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3_r010.urp"
)
RUNTIME_IDENTITY = {
    "protocol_version": 1,
    "digest_hi": 1234,
    "digest_lo": 5678,
    "registers": {
        "protocol_version": 35,
        "digest_hi": 36,
        "digest_lo": 37,
    },
}


def _program_gate(dashboard, rtde):
    return gate._program_safe_for_bridge(
        dashboard,
        rtde,
        expected_controller_program=EXPECTED_PROGRAM,
        runtime_identity=RUNTIME_IDENTITY,
    )


def test_exact_v3_stopped_or_ready_home_program_binding() -> None:
    assert _program_gate(
        {
            "programState": "STOPPED",
            "get loaded program": f"Loaded program: {EXPECTED_PROGRAM}",
        },
        {"output_int_register_30": 33},
    )["ok"] is True
    assert _program_gate(
        {
            "programState": "STOPPED",
            "get loaded program": (
                "Loaded program: /programs/andyl/kunwei/step5/"
                "step5d_strict_rnn_autotune_v1.urp"
            ),
        },
        {},
    )["ok"] is False
    ready_home = {f"output_int_register_{index}": 0 for index in range(24, 35)}
    ready_home["output_int_register_26"] = 10
    ready_home.update(
        {
            "output_int_register_35": 1,
            "output_int_register_36": 1234,
            "output_int_register_37": 5678,
        }
    )
    result = _program_gate(
        {
            "programState": "PLAYING",
            "get loaded program": f"Loaded program: {EXPECTED_PROGRAM}",
        },
        ready_home,
    )
    assert result["ok"] is True
    assert result["mode"] == "playing_ready_home_zero_identity"


def test_playing_v3_before_bridge_requires_ready_home_zero_identity() -> None:
    base = {f"output_int_register_{index}": 0 for index in range(24, 35)}
    dashboard = {
        "programState": "PLAYING",
        "get loaded program": f"Loaded program: {EXPECTED_PROGRAM}",
    }
    assert _program_gate(dashboard, base)["ok"] is False
    assert _program_gate(
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


def test_prealign_start_clearance_is_checked_before_play() -> None:
    required = gate.tp_v3.PRECONTACT_XYZ_M[2] + gate.tp_v3.MINIMUM_START_ABOVE_ENTRY_M
    passing = gate._prealign_start_clearance(
        {"actual_TCP_pose": [0.46, 0.12, required + 0.001, 3.14, 0.0, 0.0]}
    )
    assert passing["ok"] is True
    assert passing["precontact_entry_z_m"] == gate.tp_v3.PRECONTACT_XYZ_M[2]
    assert gate._prealign_start_clearance(
        {"actual_TCP_pose": [0.46, 0.12, required - 0.000001, 3.14, 0.0, 0.0]}
    )["ok"] is False
    assert gate._prealign_start_clearance({"actual_TCP_pose": [0.0, 0.0]})["ok"] is False


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
        "get loaded program": (
            "Loaded program: /programs/andyl/kunwei/step5/"
            "step5d_strict_rnn_autotune_v3_r009.urp"
        ),
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


def test_repository_support_imports_without_private_path_or_user_site() -> None:
    gate_source = Path(gate.__file__).read_text(encoding="utf-8")
    support_source = Path(support.__file__).read_text(encoding="utf-8")
    for forbidden in ("preflight_readonly", "_ur_common", "/home/andy"):
        assert forbidden not in gate_source
        assert forbidden not in support_source

    code = f"""
import json
import site
import sys
sys.path.insert(0, {str(ROOT / 'tools')!r})
from step5d_autotune_v3 import preflight_support
print(json.dumps({{
    'enable_user_site': site.ENABLE_USER_SITE,
    'module_path': preflight_support.__file__,
    'private_loaded': any(
        name == '_ur_common' or name == 'preflight_readonly'
        for name in sys.modules
    ),
}}))
"""
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }
    completed = subprocess.run(
        ["/usr/bin/python3.10", "-B", "-I", "-c", code],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed == {
        "enable_user_site": False,
        "module_path": str(ROOT / "tools/step5d_autotune_v3/preflight_support.py"),
        "private_loaded": False,
    }
    assert support.RTDE_FIELDS == [
        "actual_TCP_pose",
        "actual_TCP_speed",
        "actual_TCP_force",
        "actual_q",
        "runtime_state",
        "robot_mode",
        "safety_mode",
        "speed_scaling",
        "payload",
        "payload_cog",
        "tcp_offset",
    ]


def test_repository_run_command_preserves_result_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        support.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=" route ok \n", stderr=""
        ),
    )
    result = support.run_command(["ip", "route", "get", "192.0.2.1"])
    assert result["ok"] is True
    assert result["stdout"] == "route ok"

    monkeypatch.setattr(
        support.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=2, stdout="", stderr=" bad arguments \n"
        ),
    )
    nonzero = support.run_command(["ip", "route"])
    assert nonzero["ok"] is False
    assert nonzero["returncode"] == 2
    assert nonzero["stderr"] == "bad arguments"

    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("missing probe")

    monkeypatch.setattr(support.subprocess, "run", unavailable)
    failed = support.run_command(["missing-probe"])
    assert failed["ok"] is False
    assert failed["error"].startswith("FileNotFoundError:")


def test_repository_writer_probe_distinguishes_no_match_from_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        support,
        "run_command",
        lambda _args: {"ok": False, "returncode": 1, "stdout": "", "stderr": ""},
    )
    assert support.no_existing_writer()["ok"] is True

    monkeypatch.setattr(
        support,
        "run_command",
        lambda _args: {
            "ok": True,
            "returncode": 0,
            "stdout": "123 python3 tools/kunwei_rtde_bridge.py",
            "stderr": "",
        },
    )
    occupied = support.no_existing_writer()
    assert occupied["ok"] is False
    assert occupied["active_writers"]

    monkeypatch.setattr(
        support,
        "run_command",
        lambda _args: {"ok": False, "error": "probe unavailable"},
    )
    assert support.no_existing_writer()["ok"] is False


def test_repository_realtime_probe_requires_both_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        support,
        "run_command",
        lambda _args: {"ok": True, "returncode": 0},
    )
    assert support.realtime_capability()["ok"] is True

    monkeypatch.setattr(
        support,
        "run_command",
        lambda args: {"ok": args[0] == "chrt", "returncode": 0},
    )
    assert support.realtime_capability()["ok"] is False


class _FakeSocket:
    def __init__(
        self,
        *,
        failure: OSError | None = None,
        recv_failure: OSError | None = None,
        banner: bytes = b"",
    ) -> None:
        self.failure = failure
        self.recv_failure = recv_failure
        self.banner = banner
        self.connected: tuple[str, int] | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def connect(self, endpoint: tuple[str, int]) -> None:
        if self.failure is not None:
            raise self.failure
        self.connected = endpoint

    def recv(self, _size: int) -> bytes:
        if self.recv_failure is not None:
            raise self.recv_failure
        return self.banner


def test_repository_port_probe_preserves_banner_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        support.socket,
        "socket",
        lambda *_args: _FakeSocket(banner=b"UR dashboard"),
    )
    opened = support.probe_port("192.0.2.1", 30002, timeout=0.1)
    assert opened["open"] is True
    assert opened["banner"] == "UR dashboard"

    monkeypatch.setattr(
        support.socket,
        "socket",
        lambda *_args: _FakeSocket(recv_failure=ConnectionResetError("reset")),
    )
    reset = support.probe_port("192.0.2.1", 30002, timeout=0.1)
    assert reset["open"] is False
    assert reset["error"].startswith("ConnectionResetError:")

    monkeypatch.setattr(
        support.socket,
        "socket",
        lambda *_args: _FakeSocket(failure=ConnectionRefusedError("closed")),
    )
    closed = support.probe_port("192.0.2.1", 30002, timeout=0.1)
    assert closed["open"] is False
    assert closed["error"].startswith("ConnectionRefusedError:")


def test_repository_kunwei_probe_is_connect_only_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened_socket = _FakeSocket()
    monkeypatch.setattr(support.socket, "socket", lambda *_args: opened_socket)
    opened = support.tcp_connect_only("192.0.2.2", 5152, 0.1)
    assert opened["open"] is True
    assert opened_socket.connected == ("192.0.2.2", 5152)

    monkeypatch.setattr(
        support.socket,
        "socket",
        lambda *_args: _FakeSocket(failure=TimeoutError("timeout")),
    )
    failed = support.tcp_connect_only("192.0.2.2", 5152, 0.1)
    assert failed["open"] is False
    assert failed["error"].startswith("TimeoutError:")


def test_repository_rtde_predicate_checks_every_field_and_fails_closed() -> None:
    row = {
        field: [0.0] * length
        for field, length in {
            "actual_TCP_pose": 6,
            "actual_TCP_speed": 6,
            "actual_TCP_force": 6,
            "actual_q": 6,
            "payload_cog": 3,
            "tcp_offset": 6,
        }.items()
    }
    row.update(
        {
            field: 0.0
            for field in support.RTDE_FIELDS
            if field not in row
        }
    )
    assert support.rtde_predicate(row)["ok"] is True
    assert support.rtde_predicate({**row, "payload": float("nan")})["ok"] is False
    assert support.rtde_predicate({**row, "safety_mode": True})["ok"] is False
    assert support.rtde_predicate({"actual_TCP_pose": [0.0] * 6})["ok"] is False
