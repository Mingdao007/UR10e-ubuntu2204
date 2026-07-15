"""Best-effort asynchronous transfer worker backed by the SQLite retry queue."""

from __future__ import annotations

import hashlib
import re
import subprocess
import threading
from pathlib import Path

from .repository import Repository


HOST_RE = re.compile(r"[A-Za-z0-9_.@-]+\Z")
REMOTE_RE = re.compile(r"/[A-Za-z0-9_./-]*\Z")


class TransferWorker:
    def __init__(self, repository: Repository, *, timeout_s: float) -> None:
        self.repository = repository
        self.timeout_s = timeout_s
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempted: set[str] = set()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("transfer worker is already running")
        self._thread = threading.Thread(
            target=self._run,
            name="step5d-autotune-v2-transfer",
            daemon=True,
        )
        self._thread.start()
        self.wake()

    def wake(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        while True:
            self._wake.wait(0.25)
            self._wake.clear()
            rows = [
                row
                for row in self.repository.pending_transfers()
                if row["transfer_id"] not in self._attempted
            ]
            for row in rows:
                transfer_id = row["transfer_id"]
                self._attempted.add(transfer_id)
                try:
                    self._transfer(row)
                except Exception as exc:
                    self.repository.record_transfer_attempt(
                        transfer_id,
                        success=False,
                        error=f"{type(exc).__name__}:{exc}"[:1000],
                    )
                else:
                    self.repository.record_transfer_attempt(transfer_id, success=True)
            if self._stop.is_set():
                return

    def _transfer(self, row: dict) -> None:
        source = Path(row["path"])
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("queued transfer artifact is missing or unsafe")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != row["sha256"]:
            raise RuntimeError("queued transfer artifact digest differs")
        destination = str(row["destination"])
        if ":" not in destination:
            raise RuntimeError("transfer destination must use host:/absolute/path/")
        host, remote_dir = destination.split(":", 1)
        remote_dir = remote_dir.rstrip("/")
        if (
            host.startswith("-")
            or HOST_RE.fullmatch(host) is None
            or REMOTE_RE.fullmatch(remote_dir) is None
        ):
            raise RuntimeError("transfer destination contains unsafe characters")
        subprocess.run(
            ["ssh", host, "mkdir", "-p", remote_dir],
            check=True,
            timeout=self.timeout_s,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["scp", str(source), f"{host}:{remote_dir}/{source.name}"],
            check=True,
            timeout=self.timeout_s,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=self.timeout_s * 2 + 5)
        self._thread = None
