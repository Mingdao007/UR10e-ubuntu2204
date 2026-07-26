from __future__ import annotations

import json
import inspect
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src" / "ur10e_experiment_runtime"))

import measure_step5d_startup_timing as measure


def test_offline_runtime_identity_is_sandbox_scoped(tmp_path: Path) -> None:
    from step5d_autotune_v3.runtime_installation import (
        current_pointer_path,
        load_runtime_pointer_identity,
    )

    environment = measure._seed_offline_runtime_identity(tmp_path)
    pointer = load_runtime_pointer_identity(environ=environment)

    assert current_pointer_path(environment).is_relative_to(tmp_path)
    assert Path(pointer["attestation_path"]).is_relative_to(tmp_path)
    assert all(
        Path(profile["root"]).is_relative_to(tmp_path)
        for profile in pointer["profiles"].values()
    )


def test_persistent_worker_campaign_prepare_uses_canonical_request(
    tmp_path: Path, monkeypatch
) -> None:
    fixture_root = tmp_path / "harness"
    monkeypatch.setattr(measure.tempfile, "mkdtemp", lambda **_: str(fixture_root))
    fixture = measure._make_fixture(0)
    worker = subprocess.Popen(
        [sys.executable, str(measure.SCRIPT), "--persistent-worker"],
        cwd=measure.ROOT,
        env=measure._python_env(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        assert worker.stdin is not None and worker.stdout is not None
        worker.stdin.write(
            json.dumps(
                {
                    "operation": "campaign_prepare",
                    "fixture": fixture["experiment_root"],
                }
            )
            + "\n"
        )
        worker.stdin.flush()
        response = json.loads(worker.stdout.readline())
        assert response["ok"] is True, response
        assert response["result"]["ok"] is True
        assert response["result"]["campaign_fingerprint"] == fixture["campaign_fingerprint"]
    finally:
        if worker.stdin is not None:
            worker.stdin.close()
        worker.wait(timeout=10.0)
        shutil.rmtree(fixture["root"], ignore_errors=True)


def test_formal_finding_9_requires_production_path_proof_and_offline_compact_result(
    tmp_path: Path, monkeypatch
) -> None:
    source = inspect.getsource(measure)
    assert not re.search(r"coordinator\._basis\s*=", source)
    assert not re.search(r"coordinator\._lane_commands\s*=", source)
    assert not re.search(
        r"setattr\(\s*coordinator\s*,\s*[\"']_(?:basis|lane_commands)[\"']", source
    )
    assert "--coordinator-lane" not in source

    fixture_root = tmp_path / "harness"
    monkeypatch.setattr(measure.tempfile, "mkdtemp", lambda **_: str(fixture_root))
    fixture = measure._make_fixture(0)
    try:
        result = measure._run_handoff(fixture)

        assert result["schema"] == "step5d.autotune-v3/startup-timing-v1"
        assert result["compact"] is True
        assert result["ok"] is True
        assert result["live_handoff_to_bridge_popen_ms"] > 0.0
        assert result["bridge_ready_elapsed_ms"] > 0.0
        assert result["threshold_pass"] is True

        proof = result["production_path_proof"]
        assert proof["production_coordinator_run"] is True
        assert proof["production_launch_basis_digest_validation"] >= 1
        assert proof["production_launch_basis_freshness_validation"] >= 1
        assert proof["production_launch_basis_owner_binding_validation"] >= 1
        assert proof["production_lane_command_builder"] >= 1
        assert proof["campaign_output_validator"] >= 1
        assert proof["preflight_output_validator"] >= 1

        events = result["spawn_events"]
        assert not any("--coordinator-lane" in item["command"] for item in events)
        coordinator_event_indexes = [
            index for index, item in enumerate(events)
            if any("preflight_step5d_autotune_v3.py" in value for value in item["requested_command"])
        ]
        bridge_event_indexes = [
            index for index, item in enumerate(events) if "--stub-bridge" in item["command"]
        ]
        assert coordinator_event_indexes
        assert bridge_event_indexes
        assert max(coordinator_event_indexes) < min(bridge_event_indexes)

        bridge_ready = Path(fixture["root"]) / "stub-bridge" / "bridge_ready.json"
        assert bridge_ready.exists()
        assert Path(fixture["root"]).resolve().is_relative_to(tmp_path.resolve())
        assert all(
            path.resolve().is_relative_to(tmp_path.resolve())
            for path in Path(fixture["root"]).resolve().rglob("*")
        )
    finally:
        shutil.rmtree(fixture["root"], ignore_errors=True)
