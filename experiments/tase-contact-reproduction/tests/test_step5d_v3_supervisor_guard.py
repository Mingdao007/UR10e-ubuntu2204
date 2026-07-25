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
    assert any(
        "direct_retry_supervisor_without_deadline" in issue
        for issue in issues_for_file(path)
    )


def test_ast_guard_accepts_deadline_bounded_child_process(tmp_path: Path) -> None:
    path = tmp_path / "test_good.py"
    path.write_text(
        "import subprocess\n"
        "def test_good():\n"
        "    process = subprocess.Popen([\n"
        "        'python', '-c',\n"
        "        'import run_step5d_autotune_v3_live as live; live.run(None)',\n"
        "    ])\n"
        "    process.communicate(timeout=1)\n",
        encoding="utf-8",
    )
    assert issues_for_file(path) == []


def test_ast_guard_rejects_unrelated_deadline_before_direct_supervisor(tmp_path: Path) -> None:
    path = tmp_path / "test_false_guard.py"
    path.write_text(
        "import subprocess\n"
        "import run_step5d_autotune_v3_live as live\n"
        "def test_bad():\n"
        "    subprocess.run(['true'], timeout=1)\n"
        "    live.main()\n",
        encoding="utf-8",
    )
    assert any(
        "direct_retry_supervisor_without_deadline" in issue
        for issue in issues_for_file(path)
    )


def test_ast_guard_binds_cleanup_to_each_popen_object(tmp_path: Path) -> None:
    path = tmp_path / "test_unclean.py"
    path.write_text(
        "import subprocess\n"
        "def test_bad():\n"
        "    first = subprocess.Popen(['true'])\n"
        "    second = subprocess.Popen(['true'])\n"
        "    second.communicate(timeout=1)\n",
        encoding="utf-8",
    )
    issues = issues_for_file(path)
    assert any("subprocess_lifecycle_has_no_cleanup:first" in issue for issue in issues)
    assert not any("subprocess_lifecycle_has_no_cleanup:second" in issue for issue in issues)


def test_ast_guard_binds_deadline_to_each_popen_object(tmp_path: Path) -> None:
    path = tmp_path / "test_unbounded.py"
    path.write_text(
        "import subprocess\n"
        "def test_bad():\n"
        "    process = subprocess.Popen(['true'])\n"
        "    process.terminate()\n"
        "    subprocess.run(['true'], timeout=1)\n",
        encoding="utf-8",
    )
    assert any(
        "subprocess_lifecycle_has_no_deadline:process" in issue
        for issue in issues_for_file(path)
    )


def test_ast_guard_accepts_prepare_only_seam(tmp_path: Path) -> None:
    path = tmp_path / "test_prepare_only.py"
    path.write_text(
        "import run_step5d_autotune_v3_live as live\n"
        "def test_prepare_only():\n"
        "    live.main(['--prepare-only'])\n",
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
