from __future__ import annotations

import os
import json
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
    config = root / "config" / "tase_resident_autotuner_rate460_e_v1.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"selected_screening_arm": "E"}) + "\n", encoding="utf-8")
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


def test_autotune_defaults_to_the_selected_rate460_screening_arm(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)

    result = _invoke(script, capture, "--autotune")

    assert result.returncode == 0, result.stderr
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[:2] == [str(root / "tools" / "tase_resident_autotuner.py"), "--config"]
    assert args[2] == str(root / "config" / "tase_resident_autotuner_rate460_e_v1.json")
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
    assert args[args.index("--config") + 1] == str(campaign / "config-frozen.json")
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
        ("--tuner-config", "config.json"),
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


def test_autotune_arm_a_requires_a_sealed_baseline_before_dispatch(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)
    a_config = root / "config" / "arm-a.json"
    a_config.write_text(json.dumps({"selected_screening_arm": "A"}) + "\n", encoding="utf-8")

    result = _invoke(script, capture, "--autotune", "--tuner-config", str(a_config))

    assert result.returncode == 64
    assert "arm A requires --baseline-attempt-result" in result.stderr
    assert not capture.exists()


def test_autotune_arm_a_forwards_its_baseline_and_explicit_config(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)
    a_config = root / "config" / "arm-a.json"
    a_config.write_text(json.dumps({"selected_screening_arm": "A"}) + "\n", encoding="utf-8")
    baseline = tmp_path / "baseline" / "attempt-result.json"

    result = _invoke(script, capture, "--autotune", "--tuner-config", str(a_config),
                     "--baseline-attempt-result", str(baseline))

    assert result.returncode == 0, result.stderr
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--config") + 1] == str(a_config)
    assert args[-2:] == ["--baseline-attempt-result", str(baseline)]


def test_autotune_screening_arm_rejects_an_unrelated_a_baseline(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)
    baseline = tmp_path / "baseline" / "attempt-result.json"

    result = _invoke(script, capture, "--autotune", "--baseline-attempt-result", str(baseline))

    assert result.returncode == 64
    assert "uses its sealed screening source" in result.stderr
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


def test_single_attempt_forwards_verified_observer_rtsp_url(tmp_path):
    root, script, capture = _fake_entry_root(tmp_path)
    live_owner = root / "scripts" / "contact-yield-live.sh"
    live_owner.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGS"\n',
        encoding="utf-8",
    )
    live_owner.chmod(0o755)
    run_dir = root / "runs" / "a-repeat"
    run_dir.mkdir(parents=True)

    result = _invoke(
        script, capture,
        "--run-dir", str(run_dir),
        "--duration", "r013_60_rate460",
        "--parameter-file", str(root / "config" / "tase_figure8_integral_1p0_rate460.json"),
        "--video-policy", "evidence-only",
        "--video-url", "rtsp://100.116.236.33:8554/arm",
    )

    assert result.returncode == 0, result.stderr
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[:2] == ["supervise", "--action"]
    assert args[args.index("--video-policy") + 1] == "evidence-only"
    assert args[args.index("--video-url") + 1] == "rtsp://100.116.236.33:8554/arm"


def test_figure8_rejects_non_rtsp_video_url_before_dispatch(tmp_path):
    _, script, capture = _fake_entry_root(tmp_path)
    result = _invoke(script, capture, "--video-url", "http://camera/stream.m3u8")
    assert result.returncode == 64
    assert "must use rtsp://" in result.stderr
    assert not capture.exists()


def test_resident_start_check_rejects_reused_run_and_prepared_directories(tmp_path):
    _, script, capture = _fake_entry_root(tmp_path)
    existing_run = tmp_path / "existing-run"
    existing_run.mkdir()

    result = _invoke(
        script, capture, "--resident-start-check", "--run-dir", str(existing_run),
        "--duration", "r013_60_rate460",
    )
    assert result.returncode == 64
    assert "requires a new run directory" in result.stderr
    assert not capture.exists()

    prepared = tmp_path / "prepared"
    prepared.mkdir()
    result = _invoke(
        script, capture, "--resident-start-check", "--prepared-dir", str(prepared),
        "--duration", "r013_60_rate460",
    )
    assert result.returncode == 64
    assert "requires fresh preparation" in result.stderr
    assert not capture.exists()


@pytest.mark.parametrize(
    "duration,parameter_name",
    [
        ("r013_60_rate400", "tase_figure8_integral_1p0_rate400.json"),
        ("r013_60_rate460", "tase_figure8_integral_1p0_rate460.json"),
    ],
)
def test_resident_start_check_accepts_each_separately_bound_rate_protocol(
    tmp_path, duration, parameter_name
):
    root, script, capture = _fake_entry_root(tmp_path)
    run_dir = tmp_path / f"fresh-start-check-{duration}"
    parameter_file = root / "config" / parameter_name
    contact_six = root / "scripts" / "contact-six.sh"
    contact_six.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    contact_six.chmod(0o755)
    live_entry = root / "scripts" / "contact-yield-live.sh"
    live_entry.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$@" > "$CAPTURE_ARGS"\n',
        encoding="utf-8",
    )
    live_entry.chmod(0o755)

    result = _invoke(
        script,
        capture,
        "--resident-start-check",
        "--method",
        "TASE_RNN_MATURE",
        "--duration",
        duration,
        "--resident-attempts",
        "1",
        "--parameter-file",
        str(parameter_file),
        "--run-dir",
        str(run_dir),
        "--control-cpu",
        "4",
        "--video-policy",
        "evidence-only",
    )

    assert result.returncode == 0, result.stderr
    args = capture.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--action") + 1] == "resident-start-check"
    assert args[args.index("--duration") + 1] == duration
    assert args[args.index("--parameter-file") + 1] == str(parameter_file)
