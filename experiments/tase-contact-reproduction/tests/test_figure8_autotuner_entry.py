from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


SOURCE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "figure8.sh"


def _fake_entry_root(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    script = root / "scripts" / "figure8.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(SOURCE_SCRIPT, script)
    interpreter = root / ".venv-contact-six" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGS"\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    return root, script, tmp_path / "arguments.txt"


def _invoke(script: Path, capture: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CAPTURE_ARGS"] = str(capture)
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=script.parent.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_autotune_starts_existing_resident_tuner_with_unique_default_campaign(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)

    result = _invoke(script, capture, "--autotune")

    assert result.returncode == 0, result.stderr
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[:2] == [str(root / "tools" / "tase_resident_autotuner.py"), "--config"]
    assert args[2] == str(root / "config" / "tase_resident_autotuner_rate400_b_v1.json")
    assert args[3] == "--campaign-dir"
    campaign = Path(args[4])
    assert campaign.parent == root / "runs"
    assert campaign.name.startswith("tase-resident-autotune-")
    assert not campaign.exists()
    assert str(campaign) in result.stderr
    assert "(start)" in result.stderr
    assert "--resume" not in args


def test_resume_dispatches_existing_campaign_to_same_tuner(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)
    campaign = root / "runs" / "existing-campaign"
    campaign.mkdir(parents=True)
    (campaign / "config-frozen.json").write_text("{}\n", encoding="utf-8")
    (campaign / "ledger.jsonl").write_text("{}\n", encoding="utf-8")

    result = _invoke(script, capture, "--resume", "--campaign-dir", str(campaign))

    assert result.returncode == 0, result.stderr
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[0] == str(root / "tools" / "tase_resident_autotuner.py")
    assert args[args.index("--campaign-dir") + 1] == str(campaign)
    assert args[-1] == "--resume"
    assert "(resume)" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--autotune", "--resume"),
        ("--autotune", "--autotune"),
        ("--autotune", "--stop"),
        ("--autotune", "--campaign-dir", "--stop"),
        ("--autotune", "--duration", "full"),
        ("--resume",),
        ("--resume", "--resume"),
        ("--resume", "--campaign-dir", "missing-campaign"),
        ("--autotune", "--campaign-dir", "first", "--campaign-dir", "second"),
        ("--campaign-dir", "campaign-without-mode"),
        ("--stop", "--campaign-dir", "campaign-without-mode"),
    ],
)
def test_invalid_or_mixed_autotuner_options_exit_before_dispatch(tmp_path, args):
    _, script, capture = _fake_entry_root(tmp_path)

    result = _invoke(script, capture, *args)

    assert result.returncode == 64
    assert not capture.exists()


def test_autotune_does_not_reuse_existing_campaign_path(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)
    campaign = root / "runs" / "existing-campaign"
    campaign.mkdir(parents=True)

    result = _invoke(script, capture, "--autotune", "--campaign-dir", str(campaign))

    assert result.returncode == 64
    assert "already exists" in result.stderr
    assert not capture.exists()


def test_stop_still_routes_to_existing_live_owner(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)
    owner = root / "scripts" / "contact-yield-live.sh"
    owner.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGS"\n',
        encoding="utf-8",
    )
    owner.chmod(0o755)

    result = _invoke(script, capture, "--stop", "--run-dir", "/tmp/figure8-owner-run")

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8").splitlines() == [
        "stop", "--run-dir", "/tmp/figure8-owner-run"
    ]
