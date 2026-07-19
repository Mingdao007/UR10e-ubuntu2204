from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT.parents[1] / "src/ur10e_experiment_runtime"))

from step5d_timing_acceptance import (  # noqa: E402
    V3_RUNTIME_SOURCE_BINDING_FILES,
    evaluate_step5d_v3_timing_raw,
)
from ur10e_experiment_runtime.physical_prior import (  # noqa: E402
    STEP5D_V3_PHYSICAL_PRIOR,
)
from ur10e_experiment_runtime.stage_adapters import (  # noqa: E402
    Stage25ControllerProgressAdapter,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _raw() -> dict[str, object]:
    return {
        "runtime_path": (
            "Step5dObservation->step5d_v30_contract_pipeline->"
            "apply_step5d_moving_sphere_guard->ExactStopTransport->"
            "DeferredV30Diagnostics"
        ),
        "step5d_v3_moving_sphere": {
            "schema": "step5d.autotune-v3/formal-moving-sphere-timing-v1",
            "enabled": True,
            "physical_prior_fingerprint": STEP5D_V3_PHYSICAL_PRIOR.fingerprint,
            "reference_sha256": Stage25ControllerProgressAdapter(
                physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
            ).reference_sha256,
            "fixture_stopping_bound_fingerprint": "a" * 64,
            "fixture_stopping_bound_validity_domain": (
                "formal_timing_fixture_only_not_live_stopping_bound_certification"
            ),
            "source_binding": {
                field: _sha256(ROOT / relative)
                for field, relative in V3_RUNTIME_SOURCE_BINDING_FILES.items()
            },
            "warmup": {
                "execute_samples": 1000,
                "execute_ok_count": 1000,
                "execute_unexpected_stop_count": 0,
                "safe_hold_samples": 100,
                "safe_hold_predicted_stop_count": 100,
                "safe_hold_exact_stop_transport_count": 100,
                "safe_hold_unexpected_stop_count": 0,
            },
            "full_tick": {
                "samples": 30_000,
                "sphere_ok_count": 30_000,
                "unexpected_stop_count": 0,
            },
            "safe_hold": {
                "samples": 30_000,
                "predicted_stop_count": 30_000,
                "exact_stop_transport_count": 30_000,
                "unexpected_stop_count": 0,
            },
        },
    }


def _accepted_base() -> dict[str, object]:
    return {
        "accepted": True,
        "raw_path": "timing.json",
        "raw_sha256": "b" * 64,
        "evaluation": {
            "deadline_robustness": {
                "hard_realtime_pass": False,
                "production_scheduler_zero_miss_pass": True,
                "scheduler_contract": "sched_other_0",
            },
        },
    }


def test_v3_timing_accepts_only_complete_production_scheduler_sphere_contract(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "timing.json"
    raw_path.write_text(json.dumps(_raw()), encoding="utf-8")
    with patch(
        "step5d_timing_acceptance.evaluate_timing_raw",
        return_value=_accepted_base(),
    ):
        result = evaluate_step5d_v3_timing_raw(ROOT, raw_path)
    assert result["accepted"] is True
    assert result["blockers"] == []


def test_v3_timing_rejects_missing_exact_stop_and_bounded_hold_base(
    tmp_path: Path,
) -> None:
    raw = _raw()
    raw["step5d_v3_moving_sphere"]["safe_hold"][
        "exact_stop_transport_count"
    ] = 29_999
    raw_path = tmp_path / "timing.json"
    raw_path.write_text(json.dumps(raw), encoding="utf-8")
    base = _accepted_base()
    deadline = base["evaluation"]["deadline_robustness"]
    deadline["production_scheduler_zero_miss_pass"] = False
    deadline["bounded_last_command_hold_pass"] = False
    deadline["bounded_last_command_hold_contract_proven"] = False
    with patch(
        "step5d_timing_acceptance.evaluate_timing_raw",
        return_value=base,
    ):
        result = evaluate_step5d_v3_timing_raw(ROOT, raw_path)
    assert result["accepted"] is False
    assert (
        "v3_requires_production_sched_other_timing_contract"
        in result["blockers"]
    )
    assert (
        "v3_sphere_safe_hold_mismatch:exact_stop_transport_count"
        in result["blockers"]
    )


def test_v3_timing_accepts_exact_bounded_last_command_hold_contract(
    tmp_path: Path,
) -> None:
    raw_path = tmp_path / "timing.json"
    raw_path.write_text(json.dumps(_raw()), encoding="utf-8")
    base = _accepted_base()
    deadline = base["evaluation"]["deadline_robustness"]
    deadline["production_scheduler_zero_miss_pass"] = False
    deadline["bounded_last_command_hold_pass"] = True
    deadline["bounded_last_command_hold_contract_proven"] = True
    with patch(
        "step5d_timing_acceptance.evaluate_timing_raw",
        return_value=base,
    ):
        result = evaluate_step5d_v3_timing_raw(ROOT, raw_path)
    assert result["accepted"] is True
    assert "bounded_last_command_hold" in result["classification"]
