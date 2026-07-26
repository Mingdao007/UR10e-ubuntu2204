from __future__ import annotations

import ast
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from run_step5d_autotune_v3_live import dispatch_single_session  # noqa: E402
import validate_step5d_retry_supervisor_tests as supervisor_validator  # noqa: E402
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


def test_ast_guard_reports_token_irrelevant_syntax_error(tmp_path: Path) -> None:
    path = tmp_path / "test_syntax_error.py"
    path.write_text("def broken(:\n    pass\n", encoding="utf-8")

    issues = issues_for_file(path)

    assert len(issues) == 1
    assert f"{path}:parse:" in issues[0]


def test_ast_guard_skips_walk_for_irrelevant_valid_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "test_irrelevant.py"
    path.write_text("def test_irrelevant():\n    return 1\n", encoding="utf-8")

    def fail_walk(node: ast.AST):
        raise AssertionError(f"unexpected AST walk for {node!r}")

    monkeypatch.setattr(supervisor_validator.ast, "walk", fail_walk)

    assert issues_for_file(path) == []


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


def test_ast_guard_accepts_explicit_single_session_seam(tmp_path: Path) -> None:
    path = tmp_path / "test_single_session.py"
    path.write_text(
        "from types import SimpleNamespace\n"
        "import run_step5d_autotune_v3_live as live\n"
        "def test_single_session():\n"
        "    live.run(SimpleNamespace(single_session=True))\n",
        encoding="utf-8",
    )
    assert issues_for_file(path) == []


def test_ast_guard_preserves_findings_and_subtree_scan_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "test_scan_budget.py"
    path.write_text(
        "import run_step5d_autotune_v3_live as live\n"
        "def test_guard():\n"
        "    unrelated.deep.call(\n"
        "        '--prepare-only',\n"
        "        nested.unrelated(single_session=True),\n"
        "    )\n"
        "    live.run(None)\n",
        encoding="utf-8",
    )
    original_walk = ast.walk
    call_subtree_roots: list[ast.Call] = []

    def tracked_walk(node: ast.AST):
        if isinstance(node, ast.Call):
            call_subtree_roots.append(node)
        return original_walk(node)

    monkeypatch.setattr(supervisor_validator.ast, "walk", tracked_walk)

    assert supervisor_validator.issues_for_file(path) == [
        f"{path}:7:direct_retry_supervisor_without_deadline:"
        "run_step5d_autotune_v3_live.run"
    ]
    assert len(call_subtree_roots) == 2
    assert all(root.lineno == 7 for root in call_subtree_roots)


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
