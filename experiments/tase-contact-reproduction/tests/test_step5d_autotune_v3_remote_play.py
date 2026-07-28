from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.remote_play import (  # noqa: E402
    REMOTE_PLAY_SCHEMA,
    RemotePlayError,
    _dashboard_write,
    governed_remote_play,
)


EXPECTED = "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3_r026.urp"


class _Socket:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = iter(chunks)
        self.sent = b""

    def __enter__(self) -> "_Socket":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def settimeout(self, _timeout: float) -> None:
        return None

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def recv(self, _size: int) -> bytes:
        try:
            return next(self._chunks)
        except StopIteration:
            return b""


def test_dashboard_write_accepts_only_exact_play_success() -> None:
    fake = _Socket(
        [
            b"Connected: Universal Robots Dashboard Server\nStarting pro",
            b"gram\n",
        ]
    )
    result = _dashboard_write(
        "robot",
        "play",
        timeout_s=0.2,
        connector=lambda *_args, **_kwargs: fake,
    )
    assert result == "Starting program"
    assert fake.sent == b"play\n"


def test_dashboard_write_rejects_unbound_commands() -> None:
    with pytest.raises(RemotePlayError, match="unsupported"):
        _dashboard_write("robot", "load /programs/unsafe.urp")


def status(root: Path) -> dict[str, object]:
    predicates = {
        name: True
        for name in (
            "bench_ready",
            "bridge_heartbeat_fresh",
            "bridge_process_alive",
            "canonical_attempt_bound",
            "continuous_ready",
            "controller_fresh",
            "controller_fresh_get",
            "controller_triplet_verified",
            "kunwei_fresh",
            "lease_valid",
            "loaded_program_verified",
            "mailbox_clean",
            "next_arm_published",
            "play_identity_rechecked",
            "play_prompt_ready",
            "release_contract_proven",
            "release_current",
            "rtde_fresh",
            "rtde_recipe_capable",
            "single_writer",
            "tp_runtime_identity_verified",
            "uploaded_identity_verified",
        )
    }
    predicates["play_observed"] = False
    return {
        "schema": "step5d.bridge/governed-status-v3",
        "state": "BENCH_READY",
        "compatibility_phase": "WAITING_FOR_PLAY",
        "generated_at_unix_ns": 10_000,
        "predicates": predicates,
        "blocker": {"class": None, "reason_codes": []},
        "bridge": {"alive": True, "heartbeat_fresh": True},
        "campaign_lease": {"valid": True},
        "release": {"valid": True, "sha256": "a" * 64},
        "controller": {
            "loaded": {
                "expected": EXPECTED,
                "observed": EXPECTED,
                "verified": True,
            }
        },
        "launch_attempt": {
            "present": True,
            "state": "STARTED",
            "attempt_id": "attempt-1",
            "bindings": {"output_root": str(root / "run")},
        },
    }


def dashboard(*, playing: bool = False, remote: bool = True) -> dict[str, str]:
    return {
        "is in remote control": "true" if remote else "false",
        "safetymode": "Safetymode: NORMAL",
        "robotmode": "Robotmode: RUNNING",
        "running": f"Program running: {'true' if playing else 'false'}",
        "programState": (
            "PLAYING step5d_strict_rnn_autotune_v3_r026.urp"
            if playing
            else "STOPPED step5d_strict_rnn_autotune_v3_r026.urp"
        ),
        "get loaded program": f"Loaded program: {EXPECTED}",
    }


def test_governed_remote_play_writes_receipt_only_after_play_observed(
    tmp_path: Path,
) -> None:
    observations = iter([dashboard(), dashboard(playing=True)])
    writes: list[str] = []

    receipt = governed_remote_play(
        tmp_path,
        robot_host="robot",
        status_resolver=lambda root: status(root),
        dashboard_observer=lambda *_args, **_kwargs: next(observations),
        dashboard_writer=lambda _host, command, **_kwargs: (
            writes.append(command) or "Starting program"
        ),
        now_ns=iter([10_100, 10_200]).__next__,
        monotonic=lambda: 1.0,
        sleeper=lambda _seconds: None,
    )

    assert writes == ["play"]
    assert receipt["schema"] == REMOTE_PLAY_SCHEMA
    assert receipt["status"] == "play_observed"
    persisted = json.loads(
        (tmp_path / "run" / "remote-play-receipt.json").read_text(encoding="utf-8")
    )
    assert persisted == receipt


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["predicates"].__setitem__(
                "bridge_heartbeat_fresh", False
            ),
            "bridge_heartbeat_fresh",
        ),
        (
            lambda payload: payload.__setitem__(
                "compatibility_phase", "RUNNING"
            ),
            "not waiting",
        ),
        (
            lambda payload: payload["controller"]["loaded"].__setitem__(
                "observed", "/programs/wrong.urp"
            ),
            "identity differs",
        ),
    ],
)
def test_governed_remote_play_refuses_invalid_status(
    tmp_path: Path,
    mutate,
    message: str,
) -> None:
    payload = status(tmp_path)
    mutate(payload)
    writes: list[str] = []
    with pytest.raises(RemotePlayError, match=message):
        governed_remote_play(
            tmp_path,
            robot_host="robot",
            status_resolver=lambda _root: payload,
            dashboard_observer=lambda *_args, **_kwargs: dashboard(),
            dashboard_writer=lambda _host, command, **_kwargs: (
                writes.append(command) or "Starting program"
            ),
            now_ns=lambda: 10_100,
        )
    assert writes == []


def test_governed_remote_play_requires_remote_control(tmp_path: Path) -> None:
    writes: list[str] = []
    with pytest.raises(RemotePlayError, match="not in Remote"):
        governed_remote_play(
            tmp_path,
            robot_host="robot",
            status_resolver=lambda root: status(root),
            dashboard_observer=lambda *_args, **_kwargs: dashboard(remote=False),
            dashboard_writer=lambda _host, command, **_kwargs: (
                writes.append(command) or "Starting program"
            ),
            now_ns=lambda: 10_100,
        )
    assert writes == []


def test_governed_remote_play_compensates_with_stop_when_play_not_observed(
    tmp_path: Path,
) -> None:
    writes: list[str] = []
    ticks = iter([0.0, 0.0, 1.0])
    with pytest.raises(RemotePlayError, match="compensation=Stopped"):
        governed_remote_play(
            tmp_path,
            robot_host="robot",
            status_resolver=lambda root: status(root),
            dashboard_observer=lambda *_args, **_kwargs: dashboard(),
            dashboard_writer=lambda _host, command, **_kwargs: (
                writes.append(command)
                or ("Starting program" if command == "play" else "Stopped")
            ),
            now_ns=lambda: 10_100,
            observe_timeout_s=0.5,
            monotonic=ticks.__next__,
            sleeper=lambda _seconds: None,
        )
    assert writes == ["play", "stop"]
