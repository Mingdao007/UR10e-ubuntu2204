import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
V1 = "step5d_strict_rnn_autotune_v1"
V3 = "step5d_strict_rnn_autotune_v3"


def _load(relative: str) -> dict:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_v3_is_the_only_current_selector_surface() -> None:
    current = _load("config/current_stage.json")
    stages = _load("config/step5_stage_table.json")["stages"]
    protocol = _load("config/tase_protocol_table.json")
    compatibility = _load("config/step5d/current.json")
    flow = (ROOT / "STEP5_FLOW.md").read_text(encoding="utf-8")
    rows = {row["id"]: row for row in stages}

    assert current["current_stage_id"] == V3
    assert current["program"] == V3
    assert current["selection_state"] == "current"
    assert current["execution_state"] == "pre_live_blocked"
    assert current["readiness"]["selected_release"] == V3
    assert protocol["experiment_profiles"]["Step5.step5d_rnn"]["current_program"] == V3
    assert compatibility["program"] == V3
    assert compatibility["selection_state"] == "current"
    assert f"currently selects `{V3}`" in flow

    assert rows[V3]["active"] is True
    assert rows[V3]["bridge"] is True
    assert rows[V3]["current_binding"]["is_current"] is True
    assert rows[V3]["current_binding"]["live_authorized"] is False
    assert rows[V3]["control_profile_id"] == V1

    assert rows[V1]["active"] is False
    assert rows[V1]["bridge"] is False
    assert rows[V1]["current_binding"]["is_current"] is False
    assert rows[V1]["current_binding"]["live_authorized"] is False


def test_selected_v3_quarantines_r005_despite_exact_readback() -> None:
    current = _load("config/current_stage.json")

    assert current["controller_readback_verified_for_selected_triplet"] is True
    assert current["readiness"]["deployment_ready"] is True
    assert current["readiness"]["bridge_start_ready"] is False
    assert current["readiness"]["blockers"] == [
        "r005_batch_bootstrap_cold_read_incompatible",
        "requires_r006_second_lap_certificate",
    ]
    assert current["bridge_trigger"]["live_motion_authorized"] is False
    for field in (
        "bridge_process_ready",
        "motion_arm_ready",
        "campaign_ready",
    ):
        assert current["readiness"][field] is False


def test_retired_v1_and_generic_autotune_routes_fail_before_hardware_access() -> None:
    retired = subprocess.run(
        [str(ROOT / "scripts/step5d-autotune-live.sh")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert retired.returncode == 64
    assert "V1 launcher is retired" in retired.stderr

    for profile in (V1, V3):
        environment = os.environ.copy()
        environment["BRIDGE_PROFILE"] = profile
        generic = subprocess.run(
            [str(ROOT / "scripts/bridge-line-operator.sh")],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        assert generic.returncode == 64
        assert "generic Step5d autotune route is retired" in generic.stderr
