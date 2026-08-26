from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from step6_figure8_autotune_v1.v5_sidecar_bundle import (  # noqa: E402
    V5PostHomeSidecarFanoutV1,
    V5SidecarBundleConfigV1,
    V5SidecarBundleError,
)
from test_step6_autotuner_v5_lifecycle_ledger import _write_artifact  # noqa: E402


CONFIG = ROOT / "config" / "step6" / "autotuner_v5_sidecars_v1.json"


def test_sidecar_bundle_contract_is_explicit_launch_and_never_campaign_authority():
    config = V5SidecarBundleConfigV1.from_path(CONFIG)
    receipt = config.receipt()
    assert receipt["status"] == "implemented_explicit_launch_only"
    assert receipt["auto_start"] is False
    assert receipt["physical_campaign_dependency"] is False
    assert receipt["control_authority"] is False
    assert receipt["sidecars"] == ["filter_shadow", "camera_observer", "ros2_observation_mirror"]


def test_sidecar_bundle_config_mutations_fail_closed():
    raw = json.loads(CONFIG.read_text(encoding="utf-8"))
    for path, value in (
        (("auto_start",), True),
        (("filter_shadow", "queue_capacity"), 3),
        (("filter_shadow", "notch_50_hz_enabled"), True),
        (("camera_observer", "stale_status"), "NORMAL"),
        (("ros2_observation_mirror", "command_topics_allowed"), True),
    ):
        changed = copy.deepcopy(raw)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(V5SidecarBundleError):
            V5SidecarBundleConfigV1(changed)


def test_sidecar_cli_cold_validates_and_prints_contract():
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "run_autotuner_v5_sidecars.py"), "--config", str(CONFIG), "contract"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    receipt = json.loads(completed.stdout)
    assert receipt["physical_campaign_dependency"] is False
    assert receipt["control_authority"] is False


def test_sidecar_entrypoint_has_no_implicit_launch_or_physical_transport():
    source = (ROOT / "tools" / "run_autotuner_v5_sidecars.py").read_text(encoding="utf-8").lower()
    for forbidden in ("import rtde", "import kunwei", "ur_robot_driver", "ros2_control", "tell_exact(", "controller_write"):
        assert forbidden not in source
    assert 'if __name__ == "__main__":' in source


def test_post_home_filter_fanout_is_durable_bounded_and_idempotent(tmp_path: Path):
    config = V5SidecarBundleConfigV1.from_path(CONFIG)
    fanout = V5PostHomeSidecarFanoutV1(tmp_path / "sidecars", config)
    submissions = []
    for index in range(3):
        artifact = tmp_path / f"chain-{index}.r013life"
        receipt, events = _write_artifact(artifact, attempt_count=1)
        submissions.append(
            fanout.try_submit_filter_shadow(
                chain_id=f"v5-chain-{index}",
                artifact_path=artifact,
                lifecycle_receipt=receipt,
                event_bundle=events,
            )
        )
    assert [row["disposition"] for row in submissions] == [
        "ENQUEUED",
        "ENQUEUED",
        "DROP_NEWEST",
    ]
    assert all(row["physical_campaign_dependency"] is False for row in submissions)
    replay = fanout.try_submit_filter_shadow(
        chain_id="v5-chain-0",
        artifact_path=tmp_path / "does-not-matter.r013life",
        lifecycle_receipt={"artifact_sha256": "f" * 64},
        event_bundle={},
    )
    assert replay == submissions[0]

    cold = V5PostHomeSidecarFanoutV1(tmp_path / "sidecars", config)
    assert cold.cold_verify() == submissions[-1]["row_sha256"]
    snapshot = cold.snapshot()
    assert snapshot["filter_shadow"]["submission_count"] == 3
    assert snapshot["filter_shadow"]["queue_counts"]["pending"] == 2
    assert snapshot["camera_observer"]["launch"] == "explicit_only"
    assert snapshot["ros2_observation_mirror"]["launch"] == "explicit_only"


def test_post_home_filter_failure_is_receipted_and_never_raises(tmp_path: Path):
    config = V5SidecarBundleConfigV1.from_path(CONFIG)
    fanout = V5PostHomeSidecarFanoutV1(tmp_path / "sidecars", config)
    receipt = fanout.try_submit_filter_shadow(
        chain_id="v5-chain-invalid-source",
        artifact_path=tmp_path / "missing.r013life",
        lifecycle_receipt={"artifact_sha256": "not-a-hash"},
        event_bundle={},
    )
    assert receipt["accepted"] is False
    assert receipt["disposition"] == "FAILED_ISOLATED"
    assert receipt["error_type"] == "V5SidecarBundleError"
    assert receipt["physical_campaign_dependency"] is False
    assert fanout.cold_verify() == receipt["row_sha256"]
