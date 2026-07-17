from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_v1_operator_is_mechanically_frozen() -> None:
    environment = dict(os.environ)
    environment["BRIDGE_PROFILE"] = "step5d_strict_rnn_autotune_v1"
    completed = subprocess.run(
        [str(ROOT / "scripts/bridge-line-operator.sh"), "status"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 78
    assert "autotune v1 is frozen" in completed.stdout


def test_start_fails_before_systemd_or_network_without_pending_candidate(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/step5d-autotunectl.py"),
            "--root",
            str(ROOT),
            "--database",
            str(tmp_path / "campaign.sqlite3"),
            "start",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 78
    status = json.loads(completed.stdout)
    assert status["runtime_ready"] is False
    assert status["controller_readback_verified"] is True
    assert status["deployment_authorized"] is True
    assert status["primary_blocker"] == "no_pending_candidate"


def test_status_reports_missing_candidate_without_starting_service(
    tmp_path: Path,
) -> None:
    database = tmp_path / "campaign.sqlite3"
    prepared = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/step5d-autotunectl.py"),
            "--root",
            str(ROOT),
            "--database",
            str(database),
            "start",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert prepared.returncode == 78
    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
        if path.is_file()
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/step5d-autotunectl.py"),
            "--root",
            str(ROOT),
            "--database",
            str(database),
            "status",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0
    status = json.loads(completed.stdout)
    assert status["runtime_ready"] is False
    assert status["controller_readback_verified"] is True
    assert status["deployment_authorized"] is True
    assert status["primary_blocker"] == "no_pending_candidate"
    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
        if path.is_file()
    }
    assert after == before


def test_status_missing_database_fails_without_creating_it(tmp_path: Path) -> None:
    database = tmp_path / "missing.sqlite3"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/step5d-autotunectl.py"),
            "--root",
            str(ROOT),
            "--database",
            str(database),
            "status",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    failure = json.loads(completed.stdout)
    assert failure["runtime_ready"] is False
    assert failure["primary_blocker"] == "repository_state_unavailable"
    assert "missing or unsafe" in completed.stderr
    assert not database.exists()


def test_status_corrupt_database_is_machine_readable_and_does_not_mutate_it(
    tmp_path: Path,
) -> None:
    database = tmp_path / "corrupt.sqlite3"
    database.write_bytes(b"not a sqlite database\n")
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/step5d-autotunectl.py"),
            "--root",
            str(ROOT),
            "--database",
            str(database),
            "status",
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    failure = json.loads(completed.stdout)
    assert failure["runtime_ready"] is False
    assert failure["primary_blocker"] == "repository_state_unavailable"
    assert "refusing:" in completed.stderr
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [database.name]
