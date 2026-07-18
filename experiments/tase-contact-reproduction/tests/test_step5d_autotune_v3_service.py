from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_contract import ForceCandidate  # noqa: E402
from step5d_autotune_v3 import cli  # noqa: E402
from step5d_autotune_v3.postprocess import DerivedPostprocessQueue  # noqa: E402
from step5d_autotune_v3.service import OfflineService  # noqa: E402
from step5d_autotune_v3.state import (  # noqa: E402
    CampaignPaths,
    StateError,
    control_lock,
    read_service_state,
    read_stop_latch,
    set_stop_latch,
)


def candidate(log2_p: float, log2_damping: float, *, log2_i: float = 0.0) -> dict:
    value = ForceCandidate.from_log2(p=log2_p, damping=log2_damping, i=log2_i)
    return {
        "force_p_gain": value.force_p_gain,
        "force_i_gain": value.force_i_gain,
        "force_damping": value.force_damping,
    }


def write_batch(
    path: Path,
    candidates: list[dict],
    *,
    campaign_id: str = "offline-v3-test",
) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": cli.BATCH_SCHEMA,
                "campaign_id": campaign_id,
                "source": "offline unit test",
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )


def write_trial_batch(path: Path, candidates: list[dict], *, campaign_id: str) -> None:
    from step5d_autotune_v3.runtime_profile import DEFAULT_OVERLAY

    trials = []
    for index, candidate_row in enumerate(candidates):
        row = {**DEFAULT_OVERLAY, **candidate_row}
        row["execution_profile_id"] = (
            "nf020-slew010-a010" if index % 2 else "nf050-slew050-a050"
        )
        row["step5d_preload_hold_s"] = 0.1 + index * 0.01
        trials.append(row)
    path.write_text(
        json.dumps(
            {
                "schema": cli.TRIAL_BATCH_SCHEMA,
                "campaign_id": campaign_id,
                "source": "trial-overlay unit test",
                "candidates": trials,
            }
        ),
        encoding="utf-8",
    )


def cli_args(campaign_root: Path) -> list[str]:
    return [
        "--experiment-root",
        str(ROOT),
        "--campaign-root",
        str(campaign_root),
    ]


def test_enqueue_uses_v1_plan_and_keeps_three_fingerprints_separate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    write_batch(first, [candidate(value, 0.0) for value in (-1.0, -0.75, -0.5, -0.25, 0.0)])
    write_batch(second, [candidate(value, 0.5) for value in (-1.0, -0.75, -0.5, -0.25, 0.0)])
    campaign_root = tmp_path / "campaign"

    assert cli.main([*cli_args(campaign_root), "enqueue", "--batch", str(first)]) == 0
    first_result = json.loads(capsys.readouterr().out)
    assert cli.main([*cli_args(campaign_root), "enqueue", "--batch", str(second)]) == 0
    second_result = json.loads(capsys.readouterr().out)

    assert first_result["queue_revision"] == 1
    assert second_result["queue_revision"] == 2
    assert first_result["control_fingerprint"] == second_result["control_fingerprint"]
    assert (
        first_result["orchestration_fingerprint"]
        == second_result["orchestration_fingerprint"]
    )
    assert first_result["queue_fingerprint"] != second_result["queue_fingerprint"]
    assert second_result["restart_triggered"] is False
    assert second_result["release_gate_triggered"] is False


def test_trial_batch_v2_persists_exact_overlay_plan_without_restarting_bridge(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = tmp_path / "trial-batch.json"
    write_trial_batch(
        batch,
        [candidate(value, 0.75) for value in (-1.0, -0.75, -0.5, -0.25, 0.0)],
        campaign_id="v3-overlay-plan",
    )
    campaign_root = tmp_path / "campaign"

    assert cli.main([*cli_args(campaign_root), "enqueue", "--batch", str(batch)]) == 0
    result = json.loads(capsys.readouterr().out)
    overlay_plan = json.loads(
        (campaign_root / "control/v3_trial_overlays.json").read_text(encoding="utf-8")
    )
    assert result["execution_profile_id"] == "per_trial_overlay"
    assert result["restart_triggered"] is False
    assert overlay_plan["schema"] == "step5d.autotune-v3/trial-overlay-plan-v1"
    assert overlay_plan["revision"] == 1
    assert overlay_plan["candidate_count"] == 5
    assert len(overlay_plan["batches"][0]["trials"][0]["overlay"]) == 11
    assert overlay_plan["fingerprint"] == result["trial_overlay_plan_fingerprint"]


def test_enqueue_is_visible_to_process_boundary_status_within_one_second(
    tmp_path: Path,
) -> None:
    batch = tmp_path / "five.json"
    write_batch(
        batch,
        [candidate(value, 1.0) for value in (-1.0, -0.75, -0.5, -0.25, 0.0)],
        campaign_id="latency-process-boundary",
    )
    campaign = tmp_path / "campaign"
    script = ROOT / "scripts" / "step5d-autotune-v3.sh"
    process_environment = None
    command_prefix = [str(script)]
    if os.environ.get("STEP5D_V3_HERMETIC_PARSER_CI") == "1":
        process_environment = dict(os.environ)
        inherited_pythonpath = process_environment.get("PYTHONPATH")
        test_pythonpath = os.pathsep.join((str(ROOT / "tests"), str(ROOT / "tools")))
        process_environment["PYTHONPATH"] = (
            test_pythonpath
            if not inherited_pythonpath
            else os.pathsep.join((test_pythonpath, inherited_pythonpath))
        )
        command_prefix = [
            sys.executable,
            "-m",
            "step5d_v3_parser_ci_stubs",
            "--experiment-root",
            str(ROOT),
        ]
    started = time.perf_counter()
    enqueued = subprocess.run(
        [*command_prefix, "--campaign-root", str(campaign), "enqueue", "--batch", str(batch)],
        check=False,
        capture_output=True,
        text=True,
        env=process_environment,
    )
    status = subprocess.run(
        [*command_prefix, "--campaign-root", str(campaign), "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
        env=process_environment,
    )
    elapsed = time.perf_counter() - started
    assert enqueued.returncode == 0, enqueued.stderr
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["queue"]["revision"] == 1
    assert elapsed < 1.0


def test_enqueue_rejects_attempt_ledger_tuple_before_plan_publication(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = tmp_path / "attempted.json"
    attempted_g10 = {
        "force_p_gain": 0.001681792830507429,
        "force_i_gain": 0.00001,
        "force_damping": 8.324449805019047,
    }
    write_batch(
        batch,
        [attempted_g10]
        + [candidate(value, -0.5) for value in (-1.0, -0.75, -0.5, -0.25)],
    )
    campaign_root = tmp_path / "campaign"

    assert cli.main([*cli_args(campaign_root), "enqueue", "--batch", str(batch)]) == 2
    error = capsys.readouterr().err
    assert "G10" in error
    assert not (campaign_root / "control" / "candidate_plan.json").exists()


def test_batch_rejects_unknown_candidate_fields(tmp_path: Path) -> None:
    batch = tmp_path / "bad.json"
    rows = [candidate(value, 0.0) for value in (-1.0, -0.75, -0.5, -0.25, 0.0)]
    rows[0]["epsilon"] = 0.01
    write_batch(batch, rows)
    with pytest.raises(cli.CliError, match="candidate fields differ"):
        cli._load_batch(batch)


def test_stop_after_current_waits_for_active_trial_then_stops_at_home(
    tmp_path: Path,
) -> None:
    campaign_root = tmp_path / "campaign"
    paths = CampaignPaths(campaign_root)
    set_stop_latch(paths, armed=True)
    snapshots = iter(
        (
            {
                "phase": "trial_active",
                "trial_active": True,
                "safe_to_stop": False,
                "integrity_error": None,
            },
            {
                "phase": "home",
                "trial_active": False,
                "safe_to_stop": True,
                "integrity_error": None,
            },
        )
    )
    service = OfflineService(
        experiment_root=ROOT,
        campaign_root=campaign_root,
        check_provider=lambda: {"ok": True, "control_fingerprint": "a" * 64},
        phase_provider=lambda _paths: next(snapshots),
        poll_interval_s=0.001,
        sleep=lambda _seconds: None,
    )

    assert service.run(max_cycles=3) == 0
    state = read_service_state(paths)
    assert state["phase"] == "stopped_after_current"
    assert state["hardware_enabled"] is False
    assert state["details"]["bridge_started"] is False
    assert read_stop_latch(paths)["armed"] is True


def test_resume_clears_latch_only_inside_checked_service(tmp_path: Path) -> None:
    paths = CampaignPaths(tmp_path / "campaign")
    set_stop_latch(paths, armed=True)
    service = OfflineService(
        experiment_root=ROOT,
        campaign_root=paths.root,
        check_provider=lambda: {"ok": True, "control_fingerprint": "b" * 64},
        phase_provider=lambda _paths: {
            "phase": None,
            "trial_active": False,
            "safe_to_stop": True,
            "integrity_error": None,
        },
        poll_interval_s=0.001,
        sleep=lambda _seconds: None,
    )

    assert service.run(resume=True, max_cycles=1) == 0
    assert read_stop_latch(paths)["armed"] is False
    assert read_service_state(paths)["phase"] == "stopped"


def test_service_owner_lock_rejects_second_process_owner(tmp_path: Path) -> None:
    paths = CampaignPaths(tmp_path / "campaign")
    with control_lock(paths, owner=True):
        with pytest.raises(StateError, match="already owns"):
            with control_lock(paths, owner=True):
                pass


def test_postprocess_failure_is_terminal_per_job_and_does_not_block_next(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    bad = campaign / "bad.json"
    good = campaign / "good.json"
    for path in (bad, good):
        path.write_text(json.dumps({"evaluation": {"eligible": False}}), encoding="utf-8")
    queue = DerivedPostprocessQueue(
        campaign / "postprocess", allowed_capture_root=campaign
    )
    bad_id = queue.submit(capture=bad, trial_id="bad")
    good_id = queue.submit(capture=good, trial_id="good")

    def analyzer(_capture: Path, trial_id: str, _output: Path) -> dict:
        if trial_id == "bad":
            raise RuntimeError("plot backend unavailable")
        return {"derived": True}

    run = queue.run_pending(analyzer=analyzer)
    assert run.total == 2
    assert run.failed == 1
    assert run.succeeded == 1
    assert json.loads((queue.failed / f"{bad_id}.json").read_text())["status"] == "analysis_failed"
    assert json.loads((queue.complete / f"{good_id}.json").read_text())["status"] == "complete"
    assert not list(queue.pending.glob("*.json"))


def test_postprocess_worker_failure_does_not_stop_service_lifecycle(tmp_path: Path) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    unsafe_target = tmp_path / "elsewhere"
    unsafe_target.mkdir()
    (campaign / "postprocess").symlink_to(unsafe_target, target_is_directory=True)
    service = OfflineService(
        experiment_root=ROOT,
        campaign_root=campaign,
        check_provider=lambda: {"ok": True, "control_fingerprint": "c" * 64},
        phase_provider=lambda _paths: {
            "phase": None,
            "trial_active": False,
            "safe_to_stop": True,
            "integrity_error": None,
        },
        poll_interval_s=0.001,
        sleep=lambda _seconds: None,
    )

    assert service.run(max_cycles=2) == 0
    state = read_service_state(CampaignPaths(campaign))
    assert state["phase"] == "stopped"
    assert state["details"]["last_cycle"]["derived_postprocess"]["worker_error"]


def test_slow_derived_postprocess_cannot_block_ready_home_lifecycle(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    started = threading.Event()
    release = threading.Event()

    def slow_analysis(_queue, *, limit):
        assert limit == 8
        started.set()
        release.wait(timeout=5.0)
        return SimpleNamespace(total=0, succeeded=0, failed=0)

    service = OfflineService(
        experiment_root=ROOT,
        campaign_root=campaign,
        check_provider=lambda: {"ok": True, "control_fingerprint": "e" * 64},
        phase_provider=lambda _paths: {
            "phase": "ready_home",
            "trial_active": False,
            "safe_to_stop": True,
            "integrity_error": None,
        },
        poll_interval_s=0.001,
        sleep=lambda _seconds: None,
    )
    result: list[int] = []
    with patch.object(DerivedPostprocessQueue, "run_pending", slow_analysis):
        thread = threading.Thread(
            target=lambda: result.append(service.run(max_cycles=2)),
            daemon=True,
        )
        before = time.perf_counter()
        thread.start()
        assert started.wait(timeout=1.0)
        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            if read_service_state(CampaignPaths(campaign))["phase"] == "stopped":
                break
            time.sleep(0.005)
        assert read_service_state(CampaignPaths(campaign))["phase"] == "stopped"
        assert time.perf_counter() - before < 0.2
        release.set()
        thread.join(timeout=2.0)
    assert result == [0]


def test_systemd_start_is_offline_and_restart_disabled(tmp_path: Path) -> None:
    inactive = subprocess.CompletedProcess([], 3)
    started = subprocess.CompletedProcess([], 0)
    with patch.object(cli.subprocess, "run", side_effect=[inactive, started]) as run:
        result = cli._systemd_start(ROOT, tmp_path / "campaign", resume=True)
    command = run.call_args_list[1].args[0]
    assert result["started"] is True
    assert "--property=Restart=no" in command
    assert "--property=RestrictAddressFamilies=AF_UNIX" in command
    assert "--property=IPAddressDeny=any" in command
    assert command[-1] == "--_service-resume"

    unit = (ROOT / "config/systemd/step5d-autotune-v3.service").read_text()
    assert "Restart=no" in unit
    assert "KillMode=control-group" in unit
    assert "RestrictAddressFamilies=AF_UNIX" in unit
    assert "IPAddressDeny=any" in unit


def test_owned_child_lifecycle_smoke_keeps_process_identity_and_fingerprint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    batch = tmp_path / "five.json"
    write_batch(
        batch,
        [candidate(value, 0.75) for value in (-1.0, -0.75, -0.5, -0.25, 0.0)],
    )
    campaign = tmp_path / "campaign"
    assert cli.main([*cli_args(campaign), "enqueue", "--batch", str(batch)]) == 0
    capsys.readouterr()

    bridge_ready = tmp_path / "fake-bridge-ready.json"
    watchdog_ready = tmp_path / "fake-watchdog-ready.json"

    def child_command(path: Path, payload: dict) -> tuple[str, ...]:
        code = (
            "from pathlib import Path\n"
            "import time\n"
            f"Path({str(path)!r}).write_text({json.dumps(payload)!r}, encoding='utf-8')\n"
            "time.sleep(60)\n"
        )
        return (sys.executable, "-c", code)

    snapshots: list[dict] = []

    def observe(_seconds: float) -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not (
            bridge_ready.is_file() and watchdog_ready.is_file()
        ):
            time.sleep(0.01)
        snapshots.append(read_service_state(CampaignPaths(campaign)))

    service = OfflineService(
        experiment_root=ROOT,
        campaign_root=campaign,
        check_provider=lambda: {"ok": True, "control_fingerprint": "d" * 64},
        phase_provider=lambda _paths: {
            "phase": "ready_home",
            "trial_active": False,
            "safe_to_stop": True,
            "integrity_error": None,
        },
        poll_interval_s=0.001,
        sleep=observe,
        owned_child_commands={
            "bridge": child_command(
                bridge_ready, {"role": "fake_bridge", "play_count": 1}
            ),
            "watchdog": child_command(
                watchdog_ready, {"role": "fake_watchdog"}
            ),
        },
    )
    assert service.run(max_cycles=5) == 0
    snapshots.append(read_service_state(CampaignPaths(campaign)))

    assert len(snapshots) == 5
    assert json.loads(bridge_ready.read_text())["play_count"] == 1
    runner_pids = {row["details"]["runner_pid"] for row in snapshots}
    bridge_pids = {
        row["details"]["child_pids"]["bridge"] for row in snapshots
    }
    watchdog_pids = {
        row["details"]["child_pids"]["watchdog"] for row in snapshots
    }
    control_fingerprints = {row["control_fingerprint"] for row in snapshots}
    assert len(runner_pids) == len(bridge_pids) == len(watchdog_pids) == 1
    assert control_fingerprints == {"d" * 64}
    assert all(row["details"]["queue"]["candidate_count"] == 5 for row in snapshots)
    assert all(
        row["details"]["child_start_count"] == {"bridge": 1, "watchdog": 1}
        for row in snapshots
    )


def test_resume_clears_latch_when_offline_service_is_already_active(
    tmp_path: Path,
) -> None:
    campaign = tmp_path / "campaign"
    paths = CampaignPaths(campaign)
    set_stop_latch(paths, armed=True)
    with (
        patch.object(
            cli.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch.object(cli, "_active_service_matches_campaign", return_value=True),
    ):
        result = cli._systemd_start(ROOT, campaign, resume=True)
    assert result["already_active"] is True
    assert result["resume_cleared"] is True
    assert read_stop_latch(paths)["armed"] is False


def test_active_systemd_unit_for_another_campaign_fails_closed(tmp_path: Path) -> None:
    campaign = tmp_path / "requested"
    with (
        patch.object(
            cli.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ),
        patch.object(cli, "_active_service_matches_campaign", return_value=False),
    ):
        with pytest.raises(cli.CliError, match="exactly bound"):
            cli._systemd_start(ROOT, campaign, resume=True)
    assert not CampaignPaths(campaign).stop_latch.exists()


def test_shell_entrypoint_is_location_independent_thin_wrapper() -> None:
    script = (ROOT / "scripts/step5d-autotune-v3.sh").read_text()
    assert "set -euo pipefail" in script
    assert "BASH_SOURCE[0]" in script
    assert "readlink -f" in script
    assert 'dirname -- "${SCRIPT_PATH}"' in script
    assert "python3 -m step5d_autotune_v3.cli" in script
    assert "STEP5D_V3_HERMETIC_PARSER_CI" not in script
    assert "step5d_v3_parser_ci_stubs" not in script
    assert "bridge-line-operator" not in script
    assert "systemctl" not in script
