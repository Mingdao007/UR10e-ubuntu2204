from __future__ import annotations

import json
from pathlib import Path
import subprocess

from run_contact_ramp_probe import DEFAULT_BINDING, run_campaign


def _fake_runner_factory(*, failed_attempts=(), home_failure=()):
    binding = json.loads(DEFAULT_BINDING.read_text())
    q = binding["home_q_rad"]
    pose = binding["home_pose_m_rad"]
    index = 0

    def fake(command, *, log_path, cwd=None):
        nonlocal index
        command = list(command)
        if "prepare_figure8.py" in command[1]:
            run_dir = Path(command[command.index("--run-dir") + 1])
            run_dir.mkdir(parents=True, exist_ok=False)
            (run_dir / "readback-results.json").write_text(json.dumps({"pass": True}))
            return subprocess.CompletedProcess(command, 0, "prepared", "")
        if "contact-yield-live.sh" in command[0] and "restore-package" in command:
            run_dir = Path(command[command.index("--run-dir") + 1])
            (run_dir / "supervisor-result.json").write_text(json.dumps({
                "success": True,
                "program_stopped": True,
                "body": {"program_loaded": True, "program_started": False},
            }))
            return subprocess.CompletedProcess(command, 0, "restored stopped", "")
        if "contact-yield-live.sh" in command[0]:
            index += 1
            run_dir = Path(command[command.index("--run-dir") + 1])
            run_dir.mkdir(parents=True, exist_ok=True)
            duration = int(command[command.index("--ramp-duration-s") + 1])
            attempt_id = run_dir.parent.name + "/" + run_dir.name
            passed = attempt_id not in failed_attempts
            at_home = attempt_id not in home_failure
            actual_q = list(q)
            if not at_home:
                actual_q[0] += .1
            sample = {
                "actual_q": actual_q,
                "actual_TCP_pose": list(pose),
                "actual_qd": [0.0] * 6,
                "actual_TCP_speed": [0.0] * 6,
                "runtime_state": 1,
                "safety_mode": 1,
                "robot_mode": 7,
            }
            dashboard = {
                "is in remote control": "true",
                "safetymode": "Safetymode: NORMAL",
                "running": "Program running: false",
                "programState": "STOPPED",
            }
            evidence = {
                "qualification_passed": passed,
                "return_gate_passed": True,
                "home_proof": {"home_q_error_rad": 0.0 if at_home else .1},
                "metrics": {
                    "contact_ramp_probe": {"ramp_duration_s": duration},
                },
            }
            dispatch = {
                "armed": True,
                "evidence_eligible": passed,
                "attempts": [{
                    "evidence": evidence,
                    "contact_ramp_probe_diagnostics": {
                        "ramp_duration_s": duration,
                        "target_to_stable_force_s": .5,
                        "force_norm_peak_n": 5.2,
                    },
                }],
            }
            supervisor = {
                "success": passed,
                "dashboard_stop": {"sample": sample, "dashboard": dashboard},
            }
            (run_dir / "dispatch_receipt.json").write_text(json.dumps(dispatch))
            (run_dir / "supervisor-result.json").write_text(json.dumps(supervisor))
            return subprocess.CompletedProcess(command, 0 if passed else 1, "live", "")
        raise AssertionError(command)

    return fake


def test_runner_runs_full_ladder_then_repeats_fastest_and_restores_readback(tmp_path):
    fake = _fake_runner_factory()
    result = run_campaign(
        tmp_path / "ladder",
        binding_path=DEFAULT_BINDING,
        video_policy="evidence-only",
        restore_production_readback=True,
        command_runner=fake,
    )
    assert result["complete"] is True
    assert result["initial_passed_durations_s"] == [8, 4, 3, 2, 1]
    assert result["selected_stable_duration_s"] == 1
    assert [row["ramp_duration_s"] for row in result["attempts"]] == [8, 4, 3, 2, 1, 1, 1]
    assert result["production_readback"]["readback_pass"] is True
    assert all(row["home_verified"] for row in result["attempts"])
    assert result["attempts"][0]["qualification_metrics"]["contact_ramp_probe"]["target_to_stable_force_s"] == .5


def test_runner_stops_descending_after_failed_rung_then_retests_previous_pass(tmp_path):
    fake = _fake_runner_factory(failed_attempts={"ramp-02s/initial"})
    result = run_campaign(
        tmp_path / "ladder",
        binding_path=DEFAULT_BINDING,
        video_policy="evidence-only",
        restore_production_readback=True,
        command_runner=fake,
    )
    assert result["initial_passed_durations_s"] == [8, 4, 3]
    assert [row["ramp_duration_s"] for row in result["attempts"]] == [8, 4, 3, 2, 3, 3]
    assert result["selected_stable_duration_s"] == 3
    assert result["complete"] is True


def test_runner_never_dispatches_another_rung_after_joint_home_is_unverified(tmp_path):
    fake = _fake_runner_factory(home_failure={"ramp-08s/initial"})
    result = run_campaign(
        tmp_path / "ladder",
        binding_path=DEFAULT_BINDING,
        video_policy="evidence-only",
        restore_production_readback=False,
        command_runner=fake,
    )
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["home_verified"] is False
    assert result["selected_stable_duration_s"] is None


def test_runner_rejects_nonempty_resume_directory(tmp_path):
    root = tmp_path / "existing"
    root.mkdir()
    (root / "unsealed-attempt").write_text("preserve")
    try:
        run_campaign(root, command_runner=_fake_runner_factory())
    except FileExistsError:
        pass
    else:
        raise AssertionError("runner should preserve and reject a nonempty resume directory")
