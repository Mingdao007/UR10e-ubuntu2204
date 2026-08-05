"""Run CPU-heavy seal work in a forked child so the parent releases the GIL.

Linux-only: ``os.fork`` from the async-seal worker thread + ``os.waitpid`` in the
parent lets the motion thread keep feeding RTDE during 10–30 s JSON encode of
raw PATH evidence. The child must not touch robot sockets; it only serializes
and writes durable ledger/sidecar artifacts.
"""

from __future__ import annotations

import os
import pickle
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def run_in_fork(thunk: Callable[[], T]) -> T:
    """Evaluate ``thunk`` in a child process; return its pickled result.

    Parent blocks in ``waitpid`` (GIL released). Child exits via ``os._exit``.
    """

    fd, name = tempfile.mkstemp(prefix="r008-fork-", suffix=".pkl")
    os.close(fd)
    out_path = Path(name)
    try:
        pid = os.fork()
    except OSError:
        # fork unavailable — fall back (holds GIL; caller still works offline).
        return thunk()
    if pid == 0:
        # Child: never return into the threaded parent runtime.
        # Lower scheduling priority so the motion/writer thread keeps meeting
        # the 80 ms TP freshness gate while we JSON-encode ~18 MiB evidence.
        try:
            os.nice(19)
        except OSError:
            pass
        exit_code = 1
        try:
            value = thunk()
            out_path.write_bytes(pickle.dumps(("ok", value), protocol=pickle.HIGHEST_PROTOCOL))
            exit_code = 0
        except BaseException as exc:  # noqa: BLE001
            try:
                out_path.write_bytes(
                    pickle.dumps(("err", exc), protocol=pickle.HIGHEST_PROTOCOL)
                )
            except Exception:
                pass
            exit_code = 1
        finally:
            os._exit(exit_code)
    # Parent
    _, status = os.waitpid(pid, 0)
    raw = out_path.read_bytes()
    out_path.unlink(missing_ok=True)
    kind, payload = pickle.loads(raw)
    if kind == "err":
        raise payload
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise RuntimeError(f"r008 fork seal child failed status={status}")
    return payload  # type: ignore[return-value]


__all__ = ["run_in_fork"]
