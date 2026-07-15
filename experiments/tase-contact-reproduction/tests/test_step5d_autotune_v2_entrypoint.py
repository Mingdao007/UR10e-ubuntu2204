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


def test_start_fails_before_systemd_or_network_when_tp_v2_readback_is_missing(
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
    assert status["primary_blocker"] == "tp_v2_controller_readback_missing"


def test_status_reports_static_readback_blocker_without_starting_service(
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
    assert status["primary_blocker"] == "tp_v2_controller_readback_missing"
