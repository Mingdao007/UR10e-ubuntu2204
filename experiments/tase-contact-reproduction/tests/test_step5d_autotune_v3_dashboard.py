from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.dashboard import (  # noqa: E402
    DashboardObservationError,
    dashboard_exchange,
)


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


def test_dashboard_exchange_ignores_banner_and_reassembles_fragmented_response() -> None:
    sockets = iter(
        [
            _Socket(
                [
                    b"Connected: Universal Robots Dashboard Server\nLoaded pro",
                    b"gram: /programs/andyl/kunwei/step5/",
                    b"step5d_strict_rnn_autotune_v3_r009.urp\n",
                ]
            ),
            _Socket([b"Connected: Universal Robots Dashboard Server\nSTOP", b"PED\n"]),
        ]
    )

    result = dashboard_exchange(
        "robot",
        ["get loaded program", "programState"],
        timeout=0.2,
        connector=lambda *_args, **_kwargs: next(sockets),
    )

    assert result == {
        "get loaded program": (
            "Loaded program: /programs/andyl/kunwei/step5/"
            "step5d_strict_rnn_autotune_v3_r009.urp"
        ),
        "programState": "STOPPED",
    }


def test_dashboard_exchange_rejects_stale_unmatched_lines() -> None:
    fake = _Socket([b"PLAYING stale.urp\n", b""])
    with pytest.raises(DashboardObservationError, match="expected"):
        dashboard_exchange(
            "robot",
            ["get loaded program"],
            timeout=0.05,
            connector=lambda *_args, **_kwargs: fake,
        )


def test_dashboard_exchange_rejects_write_commands() -> None:
    with pytest.raises(DashboardObservationError, match="unsupported read-only"):
        dashboard_exchange("robot", ["load unsafe.urp"])
