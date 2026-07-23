"""Focused tests for pure runtime planning/validation contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from tools.step5d_remote_control import runtime as runtime_module
from tools.step5d_remote_control import live as live_module


TEST_ROOT = Path(__file__).resolve().parents[1]


def test_load_runtime_config_defaults_and_schema() -> None:
    cfg, params_path, params_sha = runtime_module.load_runtime_config(TEST_ROOT)
    assert params_path == TEST_ROOT / "config" / "step5d_remote" / "r012.yaml"
    assert params_sha and len(params_sha) == 64
    assert cfg["route"] == "ros2_remote_control_headless"
    assert cfg["sensor"]["connect_timeout_s"] == 3.0
    assert cfg["sensor"]["recv_timeout_s"] == 0.25
    assert cfg["sensor"]["ready_timeout_s"] == 3.0
    assert cfg["sensor"]["baseline_s"] == 1.0
    assert cfg["sensor"]["rezero_s"] == 1.0


def test_runtime_materialized_controller_yaml_maps_stale_timeout() -> None:
    cfg, _params_path, _params_sha = runtime_module.load_runtime_config(TEST_ROOT)
    payload_yaml = runtime_module.materialize_watchdog_param_yaml(cfg)
    payload = yaml.safe_load(payload_yaml)
    cm = payload["controller_manager"]["ros__parameters"]
    assert cm["update_rate"] == cfg["command_rate_hz"]
    assert cm["step5d_watchdog_controller"] == {
        "type": "ur10e_step5d_remote_watchdog/WatchdogController",
    }
    assert set(cm.keys()) == {"update_rate", "step5d_watchdog_controller"}
    assert set(payload["step5d_watchdog_controller"].keys()) == {"ros__parameters"}
    controller = payload["step5d_watchdog_controller"]["ros__parameters"]
    assert controller["stale_timeout_s"] == cfg["watchdog"]["command_stale_s"]
    assert controller["max_abs_velocity_rad_s"] == cfg["watchdog"]["max_abs_velocity_rad_s"]
    assert controller["max_acceleration_rad_s2"] == cfg["watchdog"]["max_acceleration_rad_s2"]


def test_write_runtime_artifacts_has_exact_names_and_status_markers(tmp_path: Path) -> None:
    cfg, params_path, params_sha = runtime_module.load_runtime_config(TEST_ROOT)
    params_yaml = yaml.safe_dump(cfg, sort_keys=True, default_flow_style=False, allow_unicode=False)
    paths = runtime_module.write_runtime_artifacts(
        tmp_path,
        cfg,
        params_path,
        params_yaml,
        params_sha,
        command="status",
    )
    assert {Path(p).name for p in paths.values()} == set(runtime_module.ARTIFACT_FILES)
    assert {path.name for path in Path(tmp_path).iterdir()} == set(runtime_module.ARTIFACT_FILES)
    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["command"] == "status"
    assert summary["schema_version"] == 1
    assert summary["canary_ok"] is False
    monitor = json.loads((tmp_path / "kunwei_monitor.json").read_text(encoding="utf-8"))
    assert monitor["status"] == "not_started"
    assert monitor["live_motion"] is False


def test_runtime_main_status_dry_run_use_timestamped_run_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEP5D_REMOTE_RUN_ROOT", str(tmp_path))
    assert runtime_module.main(["status", "--experiment-root", str(TEST_ROOT)]) == 0
    status_run_root = next((tmp_path / "status").iterdir())
    assert (status_run_root / "summary.json").exists()
    assert (status_run_root / "trace.csv").read_text(encoding="utf-8").strip().startswith("t_s,")
    assert runtime_module.main(["dry-run", "--experiment-root", str(TEST_ROOT)]) == 0
    dry_root = next((tmp_path / "dry-run").iterdir())
    assert (dry_root / "summary.json").exists()
    assert json.loads((dry_root / "summary.json").read_text(encoding="utf-8"))["canary_ok"] is False


def test_runtime_cli_live_and_missing_input_gates() -> None:
    with pytest.raises(ValueError, match="status cannot be run with --live"):
        runtime_module.main(["status", "--experiment-root", str(TEST_ROOT), "--live"])
    with pytest.raises(ValueError, match="canary requires --live"):
        runtime_module.main(["canary", "--experiment-root", str(TEST_ROOT)])


def test_validate_canary_evidence_enforces_strict_schema_and_age(tmp_path: Path) -> None:
    cfg, _params_path, params_sha = runtime_module.load_runtime_config(TEST_ROOT)
    now = 1800000000.0
    good = {
        "ok": True,
        "params_sha256": params_sha,
        "boot_id": "boot-id-1",
        "created_at_epoch_s": now - 1.0,
        "stages": {
            "zero": "passed",
            "free_space": "passed",
            "guarded_contact": "passed",
            "post_canary_safe_pose": "passed",
        },
    }
    evidence_path = tmp_path / "good.json"
    evidence_path.write_text(json.dumps(good), encoding="utf-8")
    runtime_module.validate_canary_evidence(
        cfg, evidence_path, params_sha256=params_sha, current_boot_id="boot-id-1", now_epoch_s=now
    )

    bad_weak = dict(good)
    bad_weak["ok"] = "true"
    bad_weak_path = tmp_path / "bad_weak.json"
    bad_weak_path.write_text(json.dumps(bad_weak), encoding="utf-8")
    with pytest.raises(ValueError, match="not ok"):
        runtime_module.validate_canary_evidence(
            cfg,
            bad_weak_path,
            params_sha256=params_sha,
            current_boot_id="boot-id-1",
            now_epoch_s=now,
        )

    bad = dict(good)
    bad["ok"] = False
    bad["created_at_epoch_s"] = now - 2.0
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="not ok"):
        runtime_module.validate_canary_evidence(
            cfg, bad_path, params_sha256=params_sha, current_boot_id="boot-id-1", now_epoch_s=now
        )

    bad_future = dict(good)
    bad_future["created_at_epoch_s"] = now + 1.0
    future_path = tmp_path / "future.json"
    future_path.write_text(json.dumps(bad_future), encoding="utf-8")
    with pytest.raises(ValueError, match="future timestamp"):
        runtime_module.validate_canary_evidence(
            cfg, future_path, params_sha256=params_sha, current_boot_id="boot-id-1", now_epoch_s=now
        )

    bad_stale = dict(good)
    bad_stale["created_at_epoch_s"] = now - (cfg["canary"]["evidence_max_age_s"] + 1.0)
    stale_path = tmp_path / "stale.json"
    stale_path.write_text(json.dumps(bad_stale), encoding="utf-8")
    with pytest.raises(ValueError, match="expired"):
        runtime_module.validate_canary_evidence(
            cfg, stale_path, params_sha256=params_sha, current_boot_id="boot-id-1", now_epoch_s=now
        )

    bad_stage = dict(good)
    bad_stage["stages"] = {
        "zero": "passed",
        "free_space": "failed",
        "guarded_contact": "passed",
        "post_canary_safe_pose": "passed",
    }
    bad_stage_path = tmp_path / "bad_stage.json"
    bad_stage_path.write_text(json.dumps(bad_stage), encoding="utf-8")
    with pytest.raises(ValueError, match="requires all stages"):
        runtime_module.validate_canary_evidence(
            cfg,
            bad_stage_path,
            params_sha256=params_sha,
            current_boot_id="boot-id-1",
            now_epoch_s=now,
        )

    too_many = dict(good)
    too_many["extra_field"] = "nope"
    too_many_path = tmp_path / "too_many.json"
    too_many_path.write_text(json.dumps(too_many), encoding="utf-8")
    with pytest.raises(ValueError, match="keys mismatch"):
        runtime_module.validate_canary_evidence(
            cfg,
            too_many_path,
            params_sha256=params_sha,
            current_boot_id="boot-id-1",
            now_epoch_s=now,
        )

    bad_current_boot = dict(good)
    with pytest.raises(ValueError, match="current_boot_id"):
        runtime_module.validate_canary_evidence(
            cfg,
            evidence_path,
            params_sha256=params_sha,
            current_boot_id="",
            now_epoch_s=now,
        )

    with pytest.raises(ValueError, match="canary evidence boot_id mismatch"):
        runtime_module.validate_canary_evidence(
            cfg,
            evidence_path,
            params_sha256=params_sha,
            current_boot_id="other-boot-id",
            now_epoch_s=now,
        )


def test_runtime_dispatches_live_modes_without_exercising_physical_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEP5D_REMOTE_RUN_ROOT", str(tmp_path))
    calls: list[str] = []

    def fake_run_live(command: str, *_args, **_kwargs) -> int:
        calls.append(command)
        return 78

    monkeypatch.setattr(live_module, "run_live", fake_run_live)
    assert runtime_module.main([
        "canary",
        "--experiment-root",
        str(TEST_ROOT),
        "--live",
    ]) == 78
    canary_root = next((tmp_path / "canary").iterdir())
    canary_summary = json.loads((canary_root / "summary.json").read_text(encoding="utf-8"))
    assert canary_summary["command"] == "canary"
    assert canary_summary["canary_ok"] is False

    assert runtime_module.main([
        "run",
        "--experiment-root",
        str(TEST_ROOT),
        "--live",
    ]) == 78
    run_root = next((tmp_path / "run").iterdir())
    run_summary = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
    assert run_summary["command"] == "run"
    assert calls == ["canary", "run"]
