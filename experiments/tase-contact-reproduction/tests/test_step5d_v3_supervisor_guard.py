from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_step5d_autotune_v3_live import dispatch_single_session  # noqa: E402
from validate_step5d_retry_supervisor_tests import issues_for_file  # noqa: E402


def test_ast_guard_rejects_direct_retry_supervisor(tmp_path: Path) -> None:
    path = tmp_path / "test_bad.py"
    path.write_text(
        "import run_step5d_autotune_v3_live as live\n"
        "def test_bad():\n"
        "    live.run(None)\n",
        encoding="utf-8",
    )
    assert any("direct_retry_supervisor_without_deadline" in issue for issue in issues_for_file(path))


def test_ast_guard_accepts_deadline_bounded_subprocess(tmp_path: Path) -> None:
    path = tmp_path / "test_good.py"
    path.write_text(
        "import subprocess\n"
        "import run_step5d_autotune_v3_live as live\n"
        "def test_good():\n"
        "    process = subprocess.Popen(['python'])\n"
        "    process.communicate(timeout=1)\n"
        "    live.run(None)\n",
        encoding="utf-8",
    )
    assert issues_for_file(path) == []


def test_single_session_dispatch_does_not_retry_or_leave_owned_work() -> None:
    calls: list[str] = []

    def session() -> dict[str, bool]:
        calls.append("session")
        return {"ok": True}

    assert dispatch_single_session(session, lambda: calls.append("cleanup")) == {"ok": True}
    assert calls == ["session"]


def test_single_session_dispatch_cleans_up_failed_finite_test() -> None:
    calls: list[str] = []

    def session() -> None:
        calls.append("session")
        raise RuntimeError("finite failure")

    with pytest.raises(RuntimeError, match="finite failure"):
        dispatch_single_session(session, lambda: calls.append("cleanup"))
    assert calls == ["session", "cleanup"]
