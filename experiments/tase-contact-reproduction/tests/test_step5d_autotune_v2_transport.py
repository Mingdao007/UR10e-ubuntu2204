from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v2.dashboard import DashboardClient
from step5d_autotune_v2.mailbox import AtomicMailbox


def test_atomic_replace_reader_accepts_consistent_old_or_new_inode(tmp_path: Path) -> None:
    mailbox = AtomicMailbox((tmp_path / "command.json").resolve())
    errors: list[Exception] = []
    observed: set[int] = set()

    def writer() -> None:
        try:
            for sequence in range(1, 301):
                mailbox.publish(sequence=sequence, payload={"value": sequence})
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    while thread.is_alive():
        try:
            snapshot = mailbox.read_latest()
            if snapshot:
                assert snapshot.payload["value"] == snapshot.sequence
                observed.add(snapshot.sequence)
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)
    thread.join()
    final = mailbox.read_latest()
    assert final is not None and final.sequence == 300
    assert observed
    assert errors == []


def test_dashboard_ignores_delayed_fragmented_greeting() -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    received: list[bytes] = []

    def serve() -> None:
        connection, _ = server.accept()
        with connection:
            received.append(connection.recv(1024))
            for chunk in (
                b"Connected: Universal ",
                b"Robots Dashboard Server\nSafe",
                b"tymode: NOR",
                b"MAL\nignored trailing line\n",
            ):
                connection.sendall(chunk)
                time.sleep(0.005)
        server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    response = DashboardClient(
        "127.0.0.1", port, connect_timeout_s=1, response_timeout_s=1
    ).command("safetymode", ("Safetymode:",))
    thread.join()
    assert received == [b"safetymode\n"]
    assert response.matched_line == "Safetymode: NORMAL"
    assert response.lines[0].startswith("Connected:")
