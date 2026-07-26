from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from prepare_step5d_autotune_launch import LaunchPreparationRequest  # noqa: E402
from step5d_autotune_backend import PreparedFingerprint, PreparedTrial  # noqa: E402
from step5d_autotune_v3.release_identity import ReleaseIdentity  # noqa: E402
from step5d_p0_v8_control_core import press_only_outer_output  # noqa: E402
from step5d_paper_outer_loop import Step5dOuterLoopOutput  # noqa: E402


def test_launch_profile_is_required_at_launch_request_constructor() -> None:
    with pytest.raises(TypeError, match="launch_profile_path"):
        LaunchPreparationRequest(
            experiment_root=ROOT,
            campaign_root=ROOT / "campaign",
            binding_file=ROOT / "binding.json",
            binding_source="incident-regression",
            candidate_batch_size=5,
            rolling_plan=True,
        )


def test_manifest_path_is_required_at_release_constructor() -> None:
    fields = inspect.signature(ReleaseIdentity).parameters
    invalid = {
        name: None
        for name, parameter in fields.items()
        if name != "manifest_path" and parameter.default is inspect.Parameter.empty
    }
    with pytest.raises(TypeError, match="manifest_path"):
        ReleaseIdentity(**invalid)


def test_p0_outer_output_requires_and_propagates_cmd_valid() -> None:
    output = press_only_outer_output(
        reaction_normal_b=(0.0, 0.0, 1.0),
        force_error_n=1.0,
    )
    assert isinstance(output, Step5dOuterLoopOutput)
    assert output.cmd_valid is True
    with pytest.raises(TypeError, match="cmd_valid"):
        Step5dOuterLoopOutput(
            xdot_p=(0.0, 0.0, 0.0),
            xdot_o=(0.0, 0.0, 0.0),
            xdot_c=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            next_state=output.next_state,
            diagnostics={},
        )


def test_production_mailbox_requires_shared_prepared_trial_contract() -> None:
    parameters = inspect.signature(PreparedTrial).parameters
    assert parameters["trial"].default is inspect.Parameter.empty
    assert parameters["frozen"].default is inspect.Parameter.empty
    assert parameters["environment"].default is inspect.Parameter.empty
    assert parameters["runner_arguments"].default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="environment"):
        PreparedTrial(
            trial=None,
            frozen=PreparedFingerprint("a" * 64, "b" * 64, "c" * 64),
            runner_arguments=(),
        )


def test_active_live_adapters_do_not_use_untyped_namespace_boundaries() -> None:
    for relative in (
        "tools/run_step5d_autotune_v3_live.py",
        "tools/run_step5d_manual_live_campaign.py",
        "tools/step5d_autotune_live_driver.py",
        "tools/prepare_step5d_autotune_launch.py",
        "tools/step5d_p0_v8_control_core.py",
        "tools/kunwei_rtde_bridge.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "SimpleNamespace" not in source
    mailbox_source = (ROOT / "tools/step5d_autotune_live_driver.py").read_text(
        encoding="utf-8"
    )
    assert 'getattr(prepared_trial,' not in mailbox_source
