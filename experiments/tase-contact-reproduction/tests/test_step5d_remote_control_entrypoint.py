"""Shell entrypoint contract tests for minimal remote-control operator interface."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "step5d_remote_control.sh"


def run_script(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env is not None:
        merged.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *argv],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=merged,
    )


def test_entrypoint_shell_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_entrypoint_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_entrypoint_help_text() -> None:
    result = run_script("--help")
    assert result.returncode == 0
    assert "step5d_remote_control.sh status" in result.stdout
    assert "--params-file" in result.stdout
    assert "--live" in result.stdout


def test_entrypoint_live_gate_for_static_modes(tmp_path: Path) -> None:
    env = {"STEP5D_REMOTE_RUN_ROOT": str(tmp_path)}
    result = run_script("status", "--live", env=env)
    assert result.returncode == 64
    assert "does not support --live" in result.stderr
    result = run_script("dry-run", "--live", env=env)
    assert result.returncode == 64
    assert "does not support --live" in result.stderr


def test_entrypoint_status_and_dry_run_happy_path(tmp_path: Path) -> None:
    env = {"STEP5D_REMOTE_RUN_ROOT": str(tmp_path)}
    status_result = run_script("status", env=env)
    assert status_result.returncode == 0
    status_roots = list((tmp_path / "status").glob("*"))
    assert status_roots
    assert (status_roots[0] / "summary.json").exists()

    dry_result = run_script("dry-run", env=env)
    assert dry_result.returncode == 0
    dry_roots = list((tmp_path / "dry-run").glob("*"))
    assert dry_roots
    assert (dry_roots[0] / "summary.json").exists()


def test_entrypoint_live_modes_require_live_and_not_extra_flags(tmp_path: Path) -> None:
    env = {"STEP5D_REMOTE_RUN_ROOT": str(tmp_path)}
    result = run_script("canary", env=env)
    assert result.returncode == 64
    assert "canary requires --live" in result.stderr
    result = run_script("run", env=env)
    assert result.returncode == 64
    assert "run requires --live" in result.stderr

    fake_python = tmp_path / "fake-live-python"
    fake_python.write_text("#!/bin/sh\nexit 78\n", encoding="utf-8")
    fake_python.chmod(0o755)
    live_env = {
        "STEP5D_REMOTE_RUN_ROOT": str(tmp_path),
        "STEP5D_REMOTE_PYTHON": str(fake_python),
    }
    result = run_script("canary", "--live", env=live_env)
    assert result.returncode == 78
    result = run_script("run", "--live", env=live_env)
    assert result.returncode == 78


def test_entrypoint_excludes_forbidden_vocabulary() -> None:
    text = SCRIPT.read_text(encoding="utf-8").lower()
    forbidden = ["tp ", "load", "play", "arm", "bridge", "driver"]
    assert all(term not in text for term in forbidden)


def test_entrypoint_rejects_unknown_flags(tmp_path: Path) -> None:
    env = {"STEP5D_REMOTE_RUN_ROOT": str(tmp_path)}
    evidence = run_script("canary", "--live", "--evidence", "x", env=env)
    assert evidence.returncode == 64
    output = (evidence.stderr + evidence.stdout).lower()
    assert "unsupported argument" in output
    output_dir = run_script("run", "--live", "--output-dir", str(tmp_path), env=env)
    assert output_dir.returncode == 64
    output = (output_dir.stderr + output_dir.stdout).lower()
    assert "unsupported argument" in output


def test_entrypoint_source_setup_text_is_minimal() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set +o" not in text
    assert "awk" not in text
    assert "nounset_mode" not in text
    assert "step5d-autotune-v3" not in text
    assert "nvidia/${package}/lib" in text
    assert "step5d-remote/r012/control/bin/python" in text
