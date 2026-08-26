from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import socket
import sys
from typing import Any, Mapping

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import step5d_remote_eoat as eoat  # noqa: E402

PROFILE_PATH = ROOT / "config/step5d/legacy_old_eoat_v3_profile.json"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def dashboard(**changes: Any) -> eoat.DashboardObservation:
    value = eoat.DashboardObservation(
        {"is in remote control": "true", "safetymode": "Safetymode: NORMAL", "robotmode": "Robotmode: RUNNING", "running": "Program running: false", "programState": "STOPPED"},
        True, "NORMAL", "RUNNING", False, "STOPPED", "2026-07-30T12:00:00+00:00",
    )
    return replace(value, **changes)


def speed(values: list[float] | None = None) -> eoat.RTDEObservation:
    return eoat.RTDEObservation({eoat.RTDE_SPEED_FIELD: values or [0.0] * 6}, "2026-07-30T12:00:01+00:00")


def readback(profile: eoat.EoatProfile, *, good: bool = True) -> eoat.RTDEObservation:
    return eoat.RTDEObservation(
        {"payload": profile.payload_kg if good else 0.5, "payload_cog": list(profile.cog_m) if good else [0.0] * 3, "tcp_offset": list(profile.tcp_m_rad) if good else [0.0] * 6, eoat.RTDE_SPEED_FIELD: [0.0] * 6},
        "2026-07-30T12:00:02+00:00",
    )


class Lease:
    def __init__(self, owner: Mapping[str, Any] | None = None) -> None:
        self.owner, self.entered = dict(owner or {"task": eoat.WRITER_TASK}), False

    def __enter__(self) -> Mapping[str, Any]:
        self.entered = True
        return self.owner

    def __exit__(self, *_: object) -> None:
        return None


class Dashboard:
    def __init__(self, rows: list[eoat.DashboardObservation]) -> None:
        self.rows, self.calls = iter(rows), 0

    def read(self, *_: Any) -> eoat.DashboardObservation:
        self.calls += 1
        return next(self.rows)


class RTDE:
    def __init__(self, rows: list[eoat.RTDEObservation | Exception]) -> None:
        self.rows, self.calls = iter(rows), []

    def read(self, _host: str, _port: int, fields: tuple[str, ...], _timeout: float) -> eoat.RTDEObservation:
        self.calls.append(tuple(fields))
        row = next(self.rows)
        if isinstance(row, Exception):
            raise row
        return row


class Secondary:
    def __init__(self, error: Exception | None = None) -> None:
        self.error, self.calls = error, []

    def send(self, host: str, port: int, script: str, _timeout: float) -> None:
        self.calls.append((host, port, script))
        if self.error:
            raise self.error


def ack_path(tmp_path: Path, profile: eoat.EoatProfile, logged_at: str = NOW.isoformat()) -> Path:
    path = tmp_path / "physical-install-ack.json"
    path.write_text(json.dumps({"schema": eoat.ACK_SCHEMA, "revision": eoat.ACK_REVISION, "profile_id": profile.profile_id, "profile_sha256": profile.profile_sha256, "release_manifest_sha256": profile.release_manifest_sha256, "acknowledgement": eoat.PHYSICAL_INSTALL_ACK_PREFIX + profile.profile_id, "operator": "unit-test", "logged_at": logged_at}, sort_keys=True), encoding="utf-8")
    return path


def transport(dashboard_reader: Dashboard, rtde_reader: RTDE, secondary: Secondary, lease: Lease) -> eoat.RemoteTransport:
    return eoat.RemoteTransport(dashboard_reader, rtde_reader, secondary, lambda _task: lease)


def live(tmp_path: Path, profile: eoat.EoatProfile, transport_value: eoat.RemoteTransport, **kwargs: Any) -> dict[str, Any]:
    return eoat.execute(profile_path=PROFILE_PATH, root=ROOT, apply=True, physical_install_ack=ack_path(tmp_path, profile), receipt_path=tmp_path / "receipt.json", transport=transport_value, wall_clock=lambda: NOW, **kwargs)


def test_offline_is_exact_and_never_constructs_or_opens_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    assert document.pop("profile_sha256") == profile.profile_sha256 == eoat.canonical_sha256(document)
    monkeypatch.setattr(eoat, "default_transport", lambda: pytest.fail("offline constructed transport"))
    monkeypatch.setattr(eoat.socket, "create_connection", lambda *_a, **_k: pytest.fail("offline opened socket"))
    result = eoat.execute(profile_path=PROFILE_PATH, root=ROOT)
    assert result["status"] == "offline_validated"
    assert not result["command_attempted"] and not result["command_sent"] and not result["verified"]
    assert result["profile"]["profile_id"] == eoat.PROFILE_ID
    assert result["release_manifest_sha256"] == eoat.RELEASE_MANIFEST_SHA256
    assert result["script"] == "sec legacy_old_eoat_v3():\n  set_target_payload(0.404,[-0.00044,-0.00094,0.0268],[0,0,0,0,0,0])\n  set_tcp(p[0,0,0.1221,0,0,0])\nend\n"


def test_profile_hash_mismatch_precedes_every_seam(tmp_path: Path) -> None:
    document = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    document["payload_kg"] = 0.405
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    lease, secondary = Lease(), Secondary()
    dash, rtde = Dashboard([]), RTDE([])
    result = eoat.execute(profile_path=path, root=ROOT, apply=True, physical_install_ack=tmp_path / "missing", receipt_path=tmp_path / "receipt.json", transport=transport(dash, rtde, secondary, lease), wall_clock=lambda: NOW)
    assert result["status"] == "failed_closed" and result["phase"] == "profile_validation"
    assert not lease.entered and dash.calls == 0 and rtde.calls == [] and secondary.calls == []


@pytest.mark.parametrize("kind", ("remote", "safety", "robotmode", "running", "program_state", "moving", "l2"))
def test_prewrite_gate_failures_send_nothing(kind: str, tmp_path: Path) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    bad = dashboard()
    if kind == "remote": bad = replace(bad, remote_control=False)
    if kind == "safety": bad = replace(bad, safety="PROTECTIVE_STOP")
    if kind == "robotmode": bad = replace(bad, robotmode="IDLE")
    if kind == "running": bad = replace(bad, running=True)
    if kind == "program_state": bad = replace(bad, program_state="PLAYING")
    values = [0.0004, 0.0004, 0.0004, 0.0, 0.0, 0.0] if kind == "l2" else ([0.001] + [0.0] * 5 if kind == "moving" else None)
    if kind == "l2":
        assert max(abs(v) for v in values[:3]) < profile.tolerances.stationary_linear_m_s
        assert math.sqrt(sum(v * v for v in values[:3])) > profile.tolerances.stationary_linear_m_s
    speed_row = speed(values)
    dash, rtde, secondary, lease = Dashboard([bad]), RTDE([speed_row] if kind in {"moving", "l2"} else []), Secondary(), Lease()
    result = live(tmp_path, profile, transport(dash, rtde, secondary, lease))
    assert result["status"] == "failed_closed" and not result["command_attempted"] and secondary.calls == []


@pytest.mark.parametrize("logged_at", ("2026-07-30T11:54:59+00:00", "2026-07-30T12:00:01+00:00", "2026-07-30T12:00:00"))
def test_ack_stale_future_or_naive_precedes_writer_and_send(logged_at: str, tmp_path: Path) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    lease, secondary = Lease(), Secondary()
    ack = ack_path(tmp_path, profile, logged_at)
    result = eoat.execute(profile_path=PROFILE_PATH, root=ROOT, apply=True, physical_install_ack=ack, receipt_path=tmp_path / "receipt.json", transport=transport(Dashboard([]), RTDE([]), secondary, lease), wall_clock=lambda: NOW)
    assert result["status"] == "failed_closed" and result["phase"] == "authorization"
    assert not lease.entered and secondary.calls == []


def test_lease_owner_task_mismatch_sends_nothing(tmp_path: Path) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    lease, secondary = Lease({"task": "other-live-writer"}), Secondary()
    result = live(tmp_path, profile, transport(Dashboard([]), RTDE([]), secondary, lease))
    assert result["status"] == "failed_closed" and result["phase"] == "lease" and secondary.calls == []


def test_checkpoint_write_failure_never_enters_secondary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    real_atomic, count = eoat.atomic_bytes, 0

    def fail_once(path: Path, data: bytes) -> None:
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("checkpoint disk failure")
        real_atomic(path, data)

    monkeypatch.setattr(eoat, "atomic_bytes", fail_once)
    secondary = Secondary()
    result = live(tmp_path, profile, transport(Dashboard([dashboard()]), RTDE([speed()]), secondary, Lease()))
    saved = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert result["phase"] == "send_checkpoint" and result["status"] == "failed_closed"
    assert result["command_attempted"] and not result["send_seam_entered"] and secondary.calls == []
    assert saved["command_attempted"] and saved["status"] == "failed_closed"


def test_success_checkpoint_then_exact_readback_receipt_and_canonical_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    lease, secondary = Lease(), Secondary()
    dash, rtde = Dashboard([dashboard(), dashboard()]), RTDE([speed(), readback(profile)])
    lease_calls, snapshots = [], []
    real_atomic = eoat.atomic_bytes

    def fake_lease(profile_arg: Any, task: str, *, blocking: bool) -> Lease:
        lease_calls.append((profile_arg, task, blocking))
        return lease

    class ResourceProfile:
        @classmethod
        def from_env(cls) -> dict[str, bool]:
            return {"canonical": True}

    def spy_atomic(path: Path, data: bytes) -> None:
        snapshots.append(json.loads(data))
        real_atomic(path, data)

    monkeypatch.setattr(eoat, "writer_lease", fake_lease)
    monkeypatch.setattr(eoat, "ResourceProfile", ResourceProfile)
    monkeypatch.setattr(eoat, "atomic_bytes", spy_atomic)
    result = live(tmp_path, profile, eoat.RemoteTransport(dash, rtde, secondary, eoat.canonical_writer_lease))
    assert result["status"] == "applied_and_verified" and result["verified"]
    assert result["command_attempted"] and result["command_sent"] and result["command_send_state"] == "sendall_completed"
    assert result["verification_basis"].startswith("exact RTDE readback")
    assert snapshots[0]["phase"] == "send_secondary_script" and snapshots[0]["status"] == "uncertain_controller_state"
    assert snapshots[0]["command_attempted"] and not snapshots[0]["command_sent"] and not snapshots[0]["verified"]
    assert lease_calls == [({"canonical": True}, eoat.WRITER_TASK, False)]
    assert secondary.calls == [(eoat.DEFAULT_ROBOT_HOST, eoat.DEFAULT_SECONDARY_PORT, result["script"])]
    saved = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert saved == result
    unsigned = dict(saved)
    assert unsigned.pop("receipt_sha256") == eoat.canonical_sha256(unsigned)


def test_partial_send_is_durable_uncertain_and_not_verified(tmp_path: Path) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    secondary = Secondary(OSError("partial send"))
    result = live(tmp_path, profile, transport(Dashboard([dashboard()]), RTDE([speed()]), secondary, Lease()))
    saved = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert result["status"] == saved["status"] == "uncertain_controller_state"
    assert result["phase"] == saved["phase"] == "send_secondary_script"
    assert result["command_attempted"] and result["send_seam_entered"] and not result["command_sent"] and not result["verified"]
    assert result["command_send_state"] == "unknown" and result["verification_basis"] is None


def test_readback_mismatch_is_durable_uncertain(tmp_path: Path) -> None:
    profile = eoat.load_profile(PROFILE_PATH, root=ROOT)
    result = live(tmp_path, profile, transport(Dashboard([dashboard(), dashboard()]), RTDE([speed(), readback(profile, good=False), readback(profile, good=False)]), Secondary(), Lease()), policy=eoat.RuntimePolicy(readback_timeout_s=1.0, readback_poll_interval_s=0.0, max_readback_polls=2), monotonic=lambda: 0.0, sleep=lambda _delay: None)
    saved = json.loads((tmp_path / "receipt.json").read_text(encoding="utf-8"))
    assert result["status"] == saved["status"] == "uncertain_controller_state"
    assert result["phase"] == saved["phase"] == "post_rtde_readback"
    assert result["command_sent"] and not result["verified"] and len(result["observations"]["post"]["rtde_readback"]) == 2


def test_secondary_socket_delimits_one_shot_script(monkeypatch: pytest.MonkeyPatch) -> None:
    class SocketFake:
        def __init__(self) -> None:
            self.sent, self.shutdowns = [], []

        def __enter__(self) -> "SocketFake": return self
        def __exit__(self, *_: object) -> None: return None
        def sendall(self, data: bytes) -> None: self.sent.append(data)
        def shutdown(self, how: int) -> None: self.shutdowns.append(how)

    fake = SocketFake()
    monkeypatch.setattr(eoat.socket, "create_connection", lambda *_a, **_k: fake)
    eoat.SecondarySocketWriter().send("127.0.0.1", eoat.DEFAULT_SECONDARY_PORT, "sec x():\nend\n", 1.0)
    assert fake.sent == [b"sec x():\nend\n"] and fake.shutdowns == [socket.SHUT_WR]
