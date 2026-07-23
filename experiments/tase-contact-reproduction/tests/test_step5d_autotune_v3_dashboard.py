from __future__ import annotations

import socket
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v3.dashboard import (  # noqa: E402
    DashboardObservationError,
    DashboardProgramLoadError,
    dashboard_exchange,
    ensure_exact_loaded_program,
)


EXPECTED_PROGRAM = (
    "/programs/andyl/kunwei/step5/step5d_strict_rnn_autotune_v3_r012.urp"
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


class _LoopbackDashboard:
    def __init__(
        self,
        loaded_program: str,
        *,
        load_response: str | None = None,
        post_load_program: str | None = None,
    ) -> None:
        self.loaded_program = loaded_program
        self.load_response = load_response
        self.post_load_program = post_load_program
        self.commands: list[str] = []
        self._stop = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(8)
        self._listener.settimeout(0.05)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "_LoopbackDashboard":
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=1.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(0.5)
                connection.sendall(
                    b"Connected: Universal Robots Dashboard Server\n"
                )
                buffered = b""
                while b"\n" not in buffered:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    buffered += chunk
                command = buffered.partition(b"\n")[0].decode("ascii").strip()
                self.commands.append(command)
                if command == "get loaded program":
                    response = f"Loaded program: {self.loaded_program}"
                elif command.startswith("load "):
                    target = command.removeprefix("load ")
                    if self.load_response is None:
                        response = f"Loading program: {target}"
                        self.loaded_program = self.post_load_program or target
                    else:
                        response = self.load_response
                else:
                    response = f"Unknown command: {command}"
                connection.sendall((response + "\n").encode("ascii"))


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


def test_wrong_loaded_program_is_auto_corrected_and_fresh_get_verified() -> None:
    with _LoopbackDashboard("/programs/old.urp") as dashboard:
        observation = ensure_exact_loaded_program(
            "127.0.0.1",
            EXPECTED_PROGRAM,
            port=dashboard.port,
            timeout=0.5,
        )

    assert observation["ok"] is True
    assert observation["load_attempted"] is True
    assert observation["after_get_loaded_program"] == (
        f"Loaded program: {EXPECTED_PROGRAM}"
    )
    assert dashboard.commands == [
        "get loaded program",
        f"load {EXPECTED_PROGRAM}",
        "get loaded program",
    ]
    assert not {"play", "stop"}.intersection(dashboard.commands)


def test_exact_loaded_program_is_rechecked_without_a_load_write() -> None:
    with _LoopbackDashboard(EXPECTED_PROGRAM) as dashboard:
        observation = ensure_exact_loaded_program(
            "127.0.0.1",
            EXPECTED_PROGRAM,
            port=dashboard.port,
            timeout=0.5,
        )

    assert observation["ok"] is True
    assert observation["load_attempted"] is False
    assert observation["load_response"] is None
    assert dashboard.commands == ["get loaded program", "get loaded program"]


def test_dashboard_load_rejection_is_named_external_blocker() -> None:
    with _LoopbackDashboard(
        "/programs/old.urp",
        load_response="Error while loading program: file not found",
    ) as dashboard:
        with pytest.raises(DashboardProgramLoadError) as caught:
            ensure_exact_loaded_program(
                "127.0.0.1",
                EXPECTED_PROGRAM,
                port=dashboard.port,
                timeout=0.5,
            )

    assert caught.value.observation["reason_code"] == "DASHBOARD_LOAD_REJECTED"
    assert caught.value.observation["blocker_class"] == "BLOCKED_EXTERNAL"
    assert caught.value.observation["load_attempted"] is True
    assert dashboard.commands == [
        "get loaded program",
        f"load {EXPECTED_PROGRAM}",
    ]


def test_post_load_mismatch_fails_closed_with_fresh_controller_evidence() -> None:
    wrong_after = "/programs/andyl/kunwei/step5/wrong_r009.urp"
    with _LoopbackDashboard(
        "/programs/old.urp",
        post_load_program=wrong_after,
    ) as dashboard:
        with pytest.raises(DashboardProgramLoadError) as caught:
            ensure_exact_loaded_program(
                "127.0.0.1",
                EXPECTED_PROGRAM,
                port=dashboard.port,
                timeout=0.5,
            )

    observation = caught.value.observation
    assert observation["reason_code"] == "DASHBOARD_POST_LOAD_MISMATCH"
    assert observation["blocker_class"] == "BLOCKED_EXTERNAL"
    assert observation["after_get_loaded_program"] == f"Loaded program: {wrong_after}"
    assert dashboard.commands[-1] == "get loaded program"
