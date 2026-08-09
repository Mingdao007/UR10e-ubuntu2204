"""Offline R010 Wave7, GP calibration, identity, ledger, and STARS gates."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(REPOSITORY_ROOT / "src" / "ur10e_experiment_runtime"))

from step5d_autotune_v4_r009.early_abort import (  # noqa: E402
    R009ActiveModeRejected,
    resolve_early_abort_mode,
)
from step5d_autotune_v4_r010.behavior import (  # noqa: E402
    command_speed_m_s,
    default_wave7_schedule,
    travel_sigmoid_speed_m_s,
)
from step5d_autotune_v4_r010.contracts import (  # noqa: E402
    DEFAULT_CONTRACT_PATH,
    DEFAULT_IDENTITY_PATH,
    build_contract,
    load_contract,
)
from step5d_autotune_v4_r010.gp_calibration import (  # noqa: E402
    CALIBRATION_SCHEMA,
    CV_FOLDS,
    CalibrationRow,
    FoldMetric,
    NOISE_FLOOR_GRID_N2,
    NoiseCandidateResult,
    admit_phase5_ledger,
    canonical_bytes,
    deterministic_grouped_folds,
    fold_assignment_sha256,
    grouped_training_rows,
    load_calibration_artifact,
    select_noise_floor,
    sha256_file,
)
from step5d_autotune_v4_r010.identity import (  # noqa: E402
    DEFAULT_CALIBRATION_PATH,
    SourceClosure,
    build_behavior_manifest,
    default_source_closure,
    sha256_bytes,
)
from step5d_autotune_v4_r010.ledger import (  # noqa: E402
    HistoricalLedgerResumeError,
    Ledger,
)
from step5d_autotune_v4_r010.tp import numeric_sanity, render_script  # noqa: E402


def _candidate(index: int = 0) -> dict[str, object]:
    return {
        "force_p_gain": 0.00025 * (1.0 + index / 100.0),
        "force_damping": 20.0 + index,
        "force_i_gain": 0.0,
        "normal_filter_tau_s": 0.2 + 0.01 * index,
        "orientation_ko": 0.05 + 0.005 * index,
        "motion_kp": 1.0 + 0.05 * index,
        "i_off": True,
        "target_force_n": 5.0,
    }


def _historical_row(sequence: int, *, eligible: bool, timing: bool) -> dict[str, object]:
    objective = 0.4 + sequence / 1000.0
    from step5d_force_objective import FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT

    return {
        "record_type": "observation",
        "attempt_sequence": sequence,
        "campaign_fingerprint": "a" * 64,
        "sealed": True,
        "eligible": eligible,
        "binding_ok": True,
        "identity_gate": True,
        "timing_gate": timing,
        "contact_gate": True,
        "safety_gate": True,
        "return_gate": True,
        "safe_return": True,
        "kind": "BO_TRIAL",
        "candidate": _candidate(sequence),
        "objective": objective,
        "mae_n": objective,
        "force_objective": {
            "version": "force_mae_v2",
            "semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT,
            "force_mae_v2": objective,
        },
        "raw_artifact": {
            "verification_state": "verified_fresh_subprocess",
            "verified_receipt": {"semantic_fingerprint": FORCE_OBJECTIVE_SEMANTIC_FINGERPRINT},
        },
    }


def test_wave7_speed_is_monotone_bounded_and_latch_precedence() -> None:
    schedule = default_wave7_schedule()
    travels = [schedule.travel_blend0_m - 0.001 + index * 0.0001 for index in range(80)]
    speeds = [travel_sigmoid_speed_m_s(travel, schedule) for travel in travels]
    assert all(float(schedule.v_near_m_s) <= speed <= float(schedule.v_far_m_s) for speed in speeds)
    assert all(left >= right for left, right in zip(speeds, speeds[1:]))
    assert command_speed_m_s(0.0, force_latched=True, schedule=schedule) == schedule.v_near_m_s
    assert command_speed_m_s(0.0, force_latched=True, creep_latched=True, schedule=schedule) == schedule.v_creep_m_s
    assert schedule.raw["travel_sigmoid"]["input"] == "travel_m"
    assert schedule.raw["travel_sigmoid"]["midpoint"] == "0.5 span"
    assert schedule.raw["force_latched_creep"]["is_sigmoid"] is False


def test_kernel_masked_diag_matches_full_covariance_diag() -> None:
    torch = pytest.importorskip("torch")
    gpytorch = pytest.importorskip("gpytorch")
    from step5d_autotune_v4_r010.kernel import build_conditional_matern52_kernel

    kernel = build_conditional_matern52_kernel(gpytorch).double()
    x1 = torch.tensor(
        [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
        dtype=torch.double,
    )
    x2 = torch.tensor(
        [[0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0], [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]],
        dtype=torch.double,
    )
    assert torch.allclose(kernel(x1, x2).to_dense().diagonal(), kernel(x1, x2, diag=True))


def test_production_admission_rejects_exactly_thirteen_timing_rows() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "phase5.jsonl"
        records = [{"record_type": "header", "campaign_fingerprint": "a" * 64}]
        records.append(_historical_row(1, eligible=True, timing=True))
        records.extend(
            _historical_row(sequence, eligible=False, timing=False)
            for sequence in range(2, 15)
        )
        path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in records))
        admission = admit_phase5_ledger(path, expected_rows=1, expected_timing_ineligible=13)
    assert len(admission.rows) == 1
    assert admission.rejection_counts["timing_ineligible"] == 13


def test_repeat_noise_uses_sample_variance() -> None:
    candidate = _candidate()
    rows = (
        CalibrationRow(1, "ANCHOR", candidate, 1.0, "a" * 64),
        CalibrationRow(2, "ANCHOR", candidate, 2.0, "a" * 64),
    )
    grouped = grouped_training_rows(rows, noise_floor_n2=2.5e-5)
    assert grouped[0]["noise_n2"] == statistics_variance_reference(1.0, 2.0)


def statistics_variance_reference(left: float, right: float) -> float:
    mean = (left + right) / 2.0
    return ((left - mean) ** 2 + (right - mean) ** 2) / 1.0


def test_cv_split_and_noise_selection_are_deterministic() -> None:
    rows = tuple(
        CalibrationRow(index + 1, "BO_TRIAL" if index % 2 else "SPACEFILL", _candidate(index), 0.1 + index / 100.0, "a" * 64)
        for index in range(50)
    )
    first = deterministic_grouped_folds(rows)
    second = deterministic_grouped_folds(tuple(reversed(rows)))
    assert first == second
    assert fold_assignment_sha256(first) == fold_assignment_sha256(second)
    assert len(first) == CV_FOLDS

    candidates = []
    for floor in NOISE_FLOOR_GRID_N2:
        coverage = 0.8
        nlpd = 3.0
        if floor == 0.005:
            coverage, nlpd = 0.95, 0.5
        elif floor == 0.01:
            coverage, nlpd = 0.96, 0.505
        metrics = tuple(
            FoldMetric(fold, coverage, nlpd, (1.0,) * 7, (False,) * 7, 10)
            for fold in range(CV_FOLDS)
        )
        candidates.append(NoiseCandidateResult(floor, metrics))
    assert select_noise_floor(candidates).noise_floor_n2 == 0.005


def test_canonical_calibration_artifact_and_unidentifiable_i_axes() -> None:
    artifact = load_calibration_artifact(DEFAULT_CALIBRATION_PATH)
    assert artifact["schema"] == CALIBRATION_SCHEMA
    assert artifact["source"]["admitted_rows"] == 567
    assert artifact["source"]["rejection_counts"]["timing_ineligible"] == 13
    assert artifact["noise"]["selected_floor_n2"] == 0.005
    assert artifact["noise"]["variance_estimator"] == "statistics.variance"
    assert artifact["lengthscales"]["not_identifiable_indices"] == [2, 6]
    confidence = {row["feature"]: row["status"] for row in artifact["lengthscales"]["confidence"]}
    assert confidence["log2_i_over_p"] == "not_identifiable"
    assert confidence["i_mode"] == "not_identifiable"
    assert artifact["kernel"]["calibration_fit_view"] == "i_off_tied_shared_same_effective_matern52"
    assert artifact["kernel"]["shared_same_initialization_tied"] is True
    assert artifact["kernel"]["implementation_sha256"] == sha256_file(
        ROOT / "tools/step5d_autotune_v4_r010/kernel.py"
    )
    payload = {key: value for key, value in artifact.items() if key != "calibration_sha256"}
    assert artifact["calibration_sha256"] == sha256_bytes(canonical_bytes(payload))


def test_behavior_bytes_drive_identity_but_stars_is_excluded() -> None:
    base_closure = default_source_closure()
    base = build_behavior_manifest(source_closure=base_closure)
    for path in (
        "config/step5d/autotune_v4_r010_wave7_schedule.json",
        "tools/step5d_autotune_v4_r010/behavior.py",
        "tools/step5d_autotune_v4_r010/kernel.py",
        "tools/step5d_autotune_v4_r010/optimizer_keepalive.py",
        "tools/step5d_autotune_v4_r008/optimizer_worker_batched.py",
    ):
        files = dict(base_closure.files)
        files[path] = "f" * 64 if files[path] != "f" * 64 else "e" * 64
        changed = build_behavior_manifest(source_closure=SourceClosure(files))
        assert changed.campaign_fingerprint != base.campaign_fingerprint
    assert not any(path.startswith("tools/stars_ft_bias_shadow/") for path in base_closure.files)
    assert not any(path.startswith("config/ft_bias/") for path in base_closure.files)


def test_calibration_semantics_change_campaign_identity() -> None:
    base = build_behavior_manifest()
    calibration = load_calibration_artifact(DEFAULT_CALIBRATION_PATH)
    calibration["noise"]["selected_floor_n2"] = 0.01
    payload = {key: value for key, value in calibration.items() if key != "calibration_sha256"}
    calibration["calibration_sha256"] = sha256_bytes(canonical_bytes(payload))
    changed = build_behavior_manifest(calibration=calibration)
    assert changed.campaign_fingerprint != base.campaign_fingerprint


def test_contract_cold_load_is_read_only_and_triplet_changes_release_identity() -> None:
    before = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (DEFAULT_CONTRACT_PATH, DEFAULT_IDENTITY_PATH)
    }
    cold = load_contract()
    after = {
        path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (DEFAULT_CONTRACT_PATH, DEFAULT_IDENTITY_PATH)
    }
    assert before == after
    changed = build_contract(
        behavior_manifest=cold.behavior_manifest,
        controller_triplet_sha256={"script": "1" * 64, "txt": "2" * 64, "urp": "3" * 64},
    )
    assert changed.release_identity_sha256 != cold.release_identity_sha256


def test_r009_ledger_cannot_resume_r010() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "r009.jsonl"
        path.write_text(json.dumps({"schema": "step5d.autotune-v4/r009-ledger-v1", "record_type": "r009_header"}) + "\n")
        with pytest.raises(HistoricalLedgerResumeError):
            Ledger.load(path)


def test_empty_r010_ledger_matches_release_and_has_no_observations() -> None:
    contract = load_contract()
    ledger = Ledger.load(
        ROOT / "config/step5d/autotune_v4_r010_empty_ledger.jsonl",
        expected_release_identity=contract.release_identity,
    )
    assert ledger.observations == ()


def test_tp_keeps_reason43_protocol_and_wave7_only() -> None:
    script = render_script(build_behavior_manifest())
    sanity = numeric_sanity(script)
    assert sanity["passed"] is True
    assert "write_output_integer_register(32, 609009)" in script
    assert "local v_far_m_s = 0.015000000" in script
    assert "local f_soft_n = 1.200000000" in script
    assert "r009" not in script.lower()


def test_early_abort_remains_shadow_only() -> None:
    assert resolve_early_abort_mode({}) == "shadow"
    with pytest.raises(R009ActiveModeRejected):
        resolve_early_abort_mode({"R009_EARLY_ABORT_MODE": "active"})


def test_worker_response_attests_calibration_fields() -> None:
    from step5d_autotune_v4_r010 import optimizer_worker

    calibration = load_calibration_artifact(DEFAULT_CALIBRATION_PATH)
    request = {
        "schema": optimizer_worker.REQUEST_SCHEMA,
        "profile": "optimizer",
        "expected_attestation": {"fixture": True},
        "payload": {
            "calibration": {
                "path": str(DEFAULT_CALIBRATION_PATH.resolve()),
                "file_sha256": sha256_file(DEFAULT_CALIBRATION_PATH),
                "calibration_sha256": calibration["calibration_sha256"],
            },
            "r008_payload": {"fixture": True},
        },
    }
    with mock.patch.object(optimizer_worker._r006_worker, "_request", return_value=({"fixture": True}, {"fixture": True})), mock.patch.object(
        optimizer_worker._batched,
        "_fit_and_ask",
        return_value={"attestation": {"gpu": "fixture"}, "metadata": {}},
    ) as fit_and_ask:
        response = optimizer_worker.run(request)
    assert fit_and_ask.call_args.kwargs["r010_calibration"]["calibration_sha256"] == calibration[
        "calibration_sha256"
    ]
    attestation = response["payload"]["attestation"]
    assert attestation["calibration"] == {
        "calibration_sha256": calibration["calibration_sha256"],
        "noise_floor_n2": 0.005,
        "variance_estimator": "statistics.variance",
        "lengthscale_policy": "five_fold_log_space_median_with_span_fallback",
        "kernel_implementation_sha256": calibration["kernel"]["implementation_sha256"],
    }


def test_builder_cold_check_has_no_live_side_effects() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/build_step5d_autotune_v4_r010.py"), "--check"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "tools")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["controller_upload"] is False
    assert result["controller_readback"] is False
    assert result["current_pointer_switch"] is False
    assert result["live_evidence"] is False
