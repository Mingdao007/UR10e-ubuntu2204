"""Focused offline R011 contract tests."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import statistics
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from step5d_autotune_v4_r011.async_ts import AsyncTSConfig, AsyncTSShadowError, TheoryCompletedObservation, joint_posterior_sample, propose_async_ts, theory_shadow_receipt  # noqa: E402
from step5d_autotune_v4_r011.behavior import default_manual_wave, validate_manual_wave  # noqa: E402
from step5d_autotune_v4_r011.censor import (  # noqa: E402
    CensoredMAEAccumulator, CensoredObservation, CensorProtocol, CensoringError,
    ExactObservation, causal_lower_bound, kappa_for_progress, legacy_gp_training_rows,
)
from step5d_autotune_v4_r011.common import digest  # noqa: E402
from step5d_autotune_v4_r011.contracts import load_contract, persist_contract  # noqa: E402
from step5d_autotune_v4_r011.identity import build_behavior_manifest, default_source_closure  # noqa: E402
from step5d_autotune_v4_r011.ledger import Ledger, R011IdentityAdmissionError, R011LedgerError, HistoricalLedgerResumeError  # noqa: E402
from step5d_autotune_v4_r011.noise import (  # noqa: E402
    CALIBRATION_GRID_N2, NoiseObservation, NoiseRefit, attest_stars_drift, fit_hierarchical_noise,
)
from step5d_autotune_v4_r011.qlognei import QLogNEIReuseError, production_qlognei_observations  # noqa: E402
from step5d_autotune_v4_r011.safety_filter import (  # noqa: E402
    SafetyFilterConfig, SafetyFilterResult, SafetyIntervention, SafetyFilterError, benchmark_safety_filter, filter_path_twist,
    project_path_command, reference_project_path_command, validate_safety_intervention,
)
from step5d_autotune_v4_r011.stars_admission import StarsAdmissionRequest, admit_stars_batch_replay  # noqa: E402
from step5d_autotune_v4_r011.wave import WaveRunTrace, canonical_normal_force_metrics, qualify_manual_wave  # noqa: E402


IDENTITY = "a" * 64
CANDIDATE_IDENTITY = "c" * 64
BASE_SCHEDULE = digest(default_manual_wave().as_dict())
CONDITION = "d" * 64


def _wave_trace(run_id: str, matched: str, repeat: int, overshoot: float, *, release: str = IDENTITY, schedule: str = BASE_SCHEDULE, execution: str = IDENTITY, ok: bool = True, condition: str = CONDITION) -> WaveRunTrace:
    samples = [{"time_s": 0.0, "normal_force_n": 0.0, "travel_m": 0.0, "contact_confirmed": False}, {"time_s": 0.1, "normal_force_n": 5.0, "travel_m": 0.01, "contact_confirmed": True}, {"time_s": 0.2, "normal_force_n": 5.0 + overshoot, "travel_m": 0.01, "contact_confirmed": False}]
    samples.extend({"time_s": 0.2 + index * 0.1, "normal_force_n": 5.0, "travel_m": 0.01, "contact_confirmed": False} for index in range(1, 7))
    return WaveRunTrace(run_id, matched, repeat, tuple(samples), schedule, release, execution, condition, hashlib.sha256(run_id.encode()).hexdigest(), ok, ok, ok, ok)


def _exact(candidate: dict[str, object], value: float = 1.0, *, identity: str = IDENTITY, dispatch: str = IDENTITY) -> ExactObservation:
    return ExactObservation("exact-" + str(candidate), candidate, value, dispatch, identity)


def test_release_load_is_pure_and_identity_is_sensitive() -> None:
    contract_path = ROOT / "config/step5d/autotune_v4_r011.json"; identity_path = ROOT / "config/step5d/autotune_v4_r011.release-identity.json"
    before = (contract_path.stat().st_mtime_ns, identity_path.stat().st_mtime_ns, hashlib.sha256(contract_path.read_bytes()).hexdigest())
    loaded = load_contract(contract_path, identity_path=identity_path)
    assert before == (contract_path.stat().st_mtime_ns, identity_path.stat().st_mtime_ns, hashlib.sha256(contract_path.read_bytes()).hexdigest())
    changed = copy.deepcopy(default_manual_wave().as_dict()); changed["v_near_m_s"] = 0.0045
    assert build_behavior_manifest(schedule=changed).campaign_fingerprint != loaded.campaign_fingerprint
    assert loaded.behavior_manifest.raw["stars_admission"]["campaign_identity_member"] is False
    assert "tools/step5d_autotune_v4_r011/stars_admission.py" not in loaded.behavior_manifest.source_closure.files


def test_runtime_composition_is_offline_only() -> None:
    from step5d_autotune_v4_r011.runtime_composition import R011RuntimeCompositionError, build_readiness_report, require_live_ready
    report = build_readiness_report(load_contract())
    assert report["offline_analysis_ready"] is True and report["live_ready"] is False
    with pytest.raises(R011RuntimeCompositionError): require_live_ready(report)


def test_old_r010_ledger_cannot_resume_r011() -> None:
    with pytest.raises(HistoricalLedgerResumeError): Ledger.load(ROOT / "config/step5d/autotune_v4_r010_empty_ledger.jsonl")


def test_r011_empty_ledger_and_failure_atomic_pair(tmp_path: Path) -> None:
    bundle = load_contract(); ledger = Ledger.load(ROOT / "config/step5d/autotune_v4_r011_empty_ledger.jsonl", expected_release_identity=bundle.release_identity)
    assert ledger.observations == ()
    contract_path, identity_path = tmp_path / "contract.json", tmp_path / "identity.json"
    assert persist_contract(bundle, path=contract_path, identity_path=identity_path).sha256 == bundle.sha256


def test_wave_metrics_stop_impulse_at_settling_and_use_candidate_medians() -> None:
    metric = canonical_normal_force_metrics(_wave_trace("a0", "m0", 0, 1.0).samples, target_force_n=5.0)
    assert metric.peak_overshoot_n == pytest.approx(1.0); assert metric.settling_onset_time_s == pytest.approx(0.3)
    assert metric.positive_overshoot_impulse_n_s == pytest.approx(0.1)
    baseline = tuple(_wave_trace(f"b{i}", f"m{i}", i, 1.0, execution=IDENTITY) for i in range(3))
    candidate = tuple(_wave_trace(f"c{i}", f"m{i}", i, value, release=CANDIDATE_IDENTITY, schedule="e" * 64, execution=CANDIDATE_IDENTITY) for i, value in enumerate((0.8, 0.8, 20.0)))
    receipt = qualify_manual_wave(baseline, candidate, baseline_release_identity_sha256=IDENTITY, candidate_release_identity_sha256=CANDIDATE_IDENTITY, baseline_schedule_sha256=BASE_SCHEDULE, candidate_schedule_sha256="e" * 64, baseline_execution_identity_sha256=IDENTITY, candidate_execution_identity_sha256=CANDIDATE_IDENTITY, matched_condition_identity_sha256=CONDITION, target_force_n=5.0)
    assert receipt.qualified is True and receipt.wave_is_bo_variable is False
    assert receipt.baseline_release_identity_sha256 != receipt.candidate_release_identity_sha256
    unsettled = list(candidate[0].samples); unsettled[-1] = {**unsettled[-1], "normal_force_n": 8.0}
    bad = _wave_trace("u", "mu", 0, 1.0, release=CANDIDATE_IDENTITY, schedule="e" * 64, execution=CANDIDATE_IDENTITY)
    bad = WaveRunTrace(bad.run_id, bad.matched_key, bad.repeat_index, tuple(unsettled), bad.schedule_sha256, bad.release_identity_sha256, bad.execution_identity_sha256, bad.matched_condition_identity_sha256, bad.raw_trace_sha256)
    rejected = qualify_manual_wave(baseline, (bad,) + candidate[1:], baseline_release_identity_sha256=IDENTITY, candidate_release_identity_sha256=CANDIDATE_IDENTITY, baseline_schedule_sha256=BASE_SCHEDULE, candidate_schedule_sha256="e" * 64, baseline_execution_identity_sha256=IDENTITY, candidate_execution_identity_sha256=CANDIDATE_IDENTITY, matched_condition_identity_sha256=CONDITION, target_force_n=5.0)
    assert not rejected.qualified and any("settling" in reason or "trace" in reason for reason in rejected.rejection_reasons)


def test_wave_identity_repeat_and_gate_failures_are_explicit() -> None:
    rows = tuple(_wave_trace(f"b{i}", f"m{i}", i, 1.0) for i in range(3))
    duplicate = _wave_trace("b0", "m0", 0, 0.8, release=CANDIDATE_IDENTITY, schedule="e" * 64, execution=CANDIDATE_IDENTITY)
    result = qualify_manual_wave(rows, (duplicate,) + rows[1:], baseline_release_identity_sha256=IDENTITY, candidate_release_identity_sha256=CANDIDATE_IDENTITY, baseline_schedule_sha256=BASE_SCHEDULE, candidate_schedule_sha256="e" * 64, baseline_execution_identity_sha256=IDENTITY, candidate_execution_identity_sha256=CANDIDATE_IDENTITY, matched_condition_identity_sha256=CONDITION, target_force_n=5.0)
    assert not result.qualified and "duplicate_run_id" in result.rejection_reasons


def test_hierarchical_noise_variance_strata_clip_and_consecutive_freeze() -> None:
    rows = [NoiseObservation(f"{group}-{repeat}", "BO_TRIAL", "epoch-1", (group,), value, True, (0.0, "off")) for group in range(8) for repeat, value in enumerate((1.0, 2.0))]
    rows.extend(NoiseObservation(f"single-{index}", "ANCHOR", "epoch-1", (100 + index,), 1.0, True, (0.0, "off")) for index in range(14))
    refits = (NoiseRefit({"lengthscale": 1.0}, 0.005, 4), NoiseRefit({"lengthscale": 1.1}, 0.005, 5))
    attestation = fit_hierarchical_noise(rows, identifiable_lengthscales={"lengthscale": 1.1}, refits=refits)
    assert attestation.selected_noise_n2 in CALIBRATION_GRID_N2 and all(item["selected_noise_n2"] in CALIBRATION_GRID_N2 for item in attestation.strata.values())
    assert attestation.total_full_observations == 30 and attestation.repeat_group_count == 8 and attestation.freeze_eligible
    assert attestation.i_axes["force_i_gain"]["status"] == "not_identifiable"
    assert attest_stars_drift({"drift_n": 0.3})["affects_campaign_fingerprint"] is False
    assert statistics.variance((1.0, 2.0)) == 0.5


def test_i_axes_need_varying_support_in_each_of_five_folds() -> None:
    ids: list[list[int]] = [[] for _ in range(5)]; number = 0
    while any(len(bucket) < 2 for bucket in ids):
        value = number; bucket = int(digest(["BO", "e", [value]])[:8], 16) % 5
        if len(ids[bucket]) < 2: ids[bucket].append(value)
        number += 1
    rows = [NoiseObservation(f"row-{index}-{repeat}", "BO", "e", (item,), 1.0, True, (float(repeat % 2), "on" if repeat % 2 else "off")) for index, bucket in enumerate(ids) for repeat, item in enumerate(bucket)]
    result = fit_hierarchical_noise(rows)
    assert result.i_axes["force_i_gain"]["status"] == "identifiable"
    assert all(count >= 2 for count in result.i_axes["force_i_gain"]["fold_varying_support"])


def test_noise_preserves_non_i_lengthscales_and_ignores_nonfull_strata() -> None:
    rows = [NoiseObservation("full-0", "BO", "e", (0,), 1.0), NoiseObservation("full-1", "BO", "e", (0,), 2.0)]
    rows.append(NoiseObservation("partial", "IGNORED", "e", (1,), 9.0, False, (), False))
    result = fit_hierarchical_noise(rows, identifiable_lengthscales={"lengthscale": 1.25, "another_continuous_axis": 2.5, "force_i_gain": 3.0})
    assert result.identifiable_lengthscales["lengthscale"] == pytest.approx(1.25)
    assert result.identifiable_lengthscales["another_continuous_axis"] == pytest.approx(2.5)
    assert "force_i_gain" not in result.identifiable_lengthscales
    assert "IGNORED|e" not in result.strata


def test_censored_rows_fixed_denominator_watermark_kappa_and_gp_isolation() -> None:
    accumulator = CensoredMAEAccumulator().close_bin(1.0).close_bin(2.0)
    assert accumulator.closed_bin_count == 2 and accumulator.lower_bound_n == pytest.approx(3.0 / 550.0)
    assert causal_lower_bound((1.0, 2.0), 2) <= causal_lower_bound((1.0, 2.0, 3.0), 3)
    assert kappa_for_progress(0.0) > kappa_for_progress(1.0); assert CensorProtocol().denominator_bins == 550
    exact = _exact({"x": 1}, 0.5)
    censored = CensoredObservation("censored-1", {"x": 2}, 2.0, 2.5, 2, 550, 3.0, 1.0, IDENTITY, IDENTITY)
    assert len(legacy_gp_training_rows((exact, censored))) == 1
    assert production_qlognei_observations((exact,))
    with pytest.raises(QLogNEIReuseError): production_qlognei_observations((censored,))
    from step5d_autotune_v4_r011.censor import validate_observation
    with pytest.raises(CensoringError): validate_observation({"schema": "step5d.autotune-v4/r011-censored-observation-v1", "watermark_n": 1.0})


def test_typed_ledger_nested_identity_and_separate_safety_event(tmp_path: Path) -> None:
    bundle = load_contract(); path = tmp_path / "ledger.jsonl"; ledger = Ledger.create(path, bundle.release_identity)
    exact = ExactObservation("ledger-exact", {"x": 1}, 0.4, IDENTITY, bundle.release_identity_sha256)
    ledger.append_observation(exact); assert ledger.typed_observations == (exact,)
    with pytest.raises(R011LedgerError): ledger.append({"objective_n": 1.0})
    intervention = SafetyIntervention(bundle.release_identity_sha256, IDENTITY, (0.2, 0.0), (0.1, 0.0), 0.1, 0.0)
    record = ledger.append_safety_intervention(intervention)
    assert record["record_type"] == "r011_safety_intervention" and record["intervention"]["objective_penalty"] is False
    assert validate_safety_intervention(record["intervention"]).release_identity_sha256 == bundle.release_identity_sha256


def test_tampered_safety_event_is_rejected_even_with_recomputed_outer_hash(tmp_path: Path) -> None:
    bundle = load_contract()
    for tamper in ("release_identity_sha256", "objective_penalty"):
        path = tmp_path / f"tampered-{tamper}.jsonl"
        ledger = Ledger.create(path, bundle.release_identity)
        event = SafetyIntervention(bundle.release_identity_sha256, IDENTITY, (0.2, 0.0), (0.1, 0.0), 0.1, 0.0)
        ledger.append_safety_intervention(event)
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if tamper == "release_identity_sha256":
            rows[1]["intervention"][tamper] = "b" * 64
        else:
            rows[1]["intervention"][tamper] = True
        rows[1]["record_sha256"] = digest({key: value for key, value in rows[1].items() if key != "record_sha256"})
        path.write_text("".join(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
        with pytest.raises((R011LedgerError, R011IdentityAdmissionError)):
            Ledger.load(path, expected_release_identity=bundle.release_identity)


def test_async_ts_gaussian_shadow_identity_pending_seed_and_malformed_delay() -> None:
    lattice = tuple({"x": index, "mode": "a" if index % 2 else "b"} for index in range(5)); pending = (lattice[0], lattice[1])
    with pytest.raises(AsyncTSShadowError): propose_async_ts((), pending, lattice, config=AsyncTSConfig(seed=1))
    first = propose_async_ts((), pending, lattice, config=AsyncTSConfig(seed=1), expected_release_identity_sha256=IDENTITY); second = propose_async_ts((), pending, lattice, config=AsyncTSConfig(seed=1), expected_release_identity_sha256=IDENTITY)
    assert first.as_dict() == second.as_dict() and first.branch == "exploration" and first.force_full_evaluation and first.candidate not in pending
    exact = _exact(lattice[2], 0.2)
    shadow = propose_async_ts((exact,), pending, lattice, config=AsyncTSConfig(seed=99), expected_release_identity_sha256=IDENTITY)
    assert shadow.candidate not in pending and shadow.completed_count == 1 and theory_shadow_receipt(shadow)["kernel_schema"] == "r011-rbf-positive-definite-v1"
    delayed = {**exact.as_dict(), "sealing_delay_s": 61.0}
    with pytest.raises(AsyncTSShadowError): propose_async_ts((delayed,), pending, lattice, config=AsyncTSConfig(max_sealing_delay_s=60.0), expected_release_identity_sha256=IDENTITY)
    malformed = dict(exact.as_dict()); malformed["censored"] = True
    with pytest.raises(AsyncTSShadowError): propose_async_ts((malformed,), pending, lattice, expected_release_identity_sha256=IDENTITY)


def test_async_ts_distinct_dispatches_censor_disable_and_joint_covariance() -> None:
    lattice = ({"x": 0.0}, {"x": 1e-6}, {"x": 1.0})
    first = _exact(lattice[0], 0.2, dispatch="1" * 64)
    second = ExactObservation("exact-second", lattice[2], 0.3, "2" * 64, IDENTITY)
    proposal = propose_async_ts((first, second), (), lattice, config=AsyncTSConfig(seed=99), expected_release_identity_sha256=IDENTITY)
    assert proposal.completed_count == 2 and proposal.release_identity_sha256 == IDENTITY
    first_theory = TheoryCompletedObservation.from_observation(first, max_sealing_delay_s=60.0, expected_release_identity_sha256=IDENTITY)
    second_theory = TheoryCompletedObservation.from_observation(second, max_sealing_delay_s=60.0, expected_release_identity_sha256=IDENTITY)
    assert proposal.completed_observations_sha256 == digest(sorted((first_theory.as_dict(), second_theory.as_dict()), key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"))))
    censored = CensoredObservation("censor", lattice[1], 0.4, 1.0, 2, 550, 2.0, 0.5, "3" * 64, IDENTITY)
    with pytest.raises(AsyncTSShadowError): propose_async_ts((censored,), (), lattice, config=AsyncTSConfig(allow_hypothetical_censoring=False), expected_release_identity_sha256=IDENTITY)
    samples, covariance = joint_posterior_sample(lattice[:2], (), lattice, config=AsyncTSConfig(seed=5), seed=5)
    assert len(samples) == 2 and covariance[0][1] > 0.9 * math.sqrt(covariance[0][0] * covariance[1][1])
    assert theory_shadow_receipt(proposal)["joint_posterior_sampling"] is True


def test_manual_wave_force_fuse_and_confirmation_bounds_are_relational() -> None:
    unsafe = copy.deepcopy(default_manual_wave().as_dict()); unsafe["force_fuse_n"] = 1.6
    with pytest.raises(Exception): validate_manual_wave(unsafe)
    unsafe = copy.deepcopy(default_manual_wave().as_dict()); unsafe["confirm_force_norm_n"] = 1.4
    with pytest.raises(Exception): validate_manual_wave(unsafe)
    unsafe = copy.deepcopy(default_manual_wave().as_dict()); unsafe["v_far_m_s"] = 0.2
    with pytest.raises(Exception): validate_manual_wave(unsafe)


def test_safety_filter_independent_reference_boundaries_and_path_scope() -> None:
    config = SafetyFilterConfig(); args = ((0.019, 0.006), (0.15, 0.15), (0.0, 0.0)); edge_config = SafetyFilterConfig(acceleration_min_m_s2=(-100.0, -100.0), acceleration_max_m_s2=(100.0, 100.0))
    result = project_path_command(*args, state_age_s=0.0, config=edge_config); reference = reference_project_path_command(*args, config=edge_config)
    assert result.valid and result.command_m_s == pytest.approx(reference) and result.remaining_margin is not None and "tightened_ellipsoid" in result.active_set
    assert result.as_dict()["feasible"] is True and result.as_dict()["feasible"] is result.as_dict()["valid"]
    stale = project_path_command((0.0, 0.0), (0.05, 0.0), result.command_m_s, state_age_s=1.0, config=config)
    assert not stale.valid and stale.as_dict()["feasible"] is False and stale.remaining_margin is None and stale.reason == "stale_state"
    uncertain = project_path_command((0.0, 0.0), (0.05, 0.0), (0.0, 0.0), state_age_s=0.0, config=SafetyFilterConfig(tracking_error_bound_m=0.03))
    assert not uncertain.valid and uncertain.remaining_margin is None
    infeasible = project_path_command((0.0, 0.0), (0.0, 0.0), (0.0, 0.0), state_age_s=0.0, config=SafetyFilterConfig(velocity_min_m_s=(1.0, 1.0), velocity_max_m_s=(1.0, 1.0)))
    assert not infeasible.valid and infeasible.remaining_margin is None
    twist, _ = filter_path_twist((0.0, 0.0), (0.05, 0.01, 7.0, 8.0, 9.0, 10.0), (0.0, 0.0), state_age_s=0.0)
    assert twist[2:] == (7.0, 8.0, 9.0, 10.0)
    receipt = benchmark_safety_filter(sample_count=40, host_qualified=False)
    assert receipt.qualified is False and receipt.algorithm_bound_ok is True
    with pytest.raises(SafetyFilterError): SafetyFilterResult(True, (0.0, 0.0), None, (), 0, 0.0, 0.0)


def test_stars_admission_strict_idle_sealed_analysis_only(tmp_path: Path) -> None:
    path = tmp_path / "sealed.jsonl"; path.write_text("{}\n", encoding="utf-8")
    base = dict(input_paths=(path,), sealed_inputs={str(path): True}, explicit_batch_replay=True, live_writer_lease=False, active_attempt=False, gpu_requested=False, concurrent_worker=False, release_identity_sha256=IDENTITY, execution_identity_sha256=IDENTITY)
    decision = admit_stars_batch_replay(StarsAdmissionRequest(**base))
    assert decision.admitted and decision.job["auto_launch"] is False and decision.as_dict()["analysis_only"] is True
    assert decision.release_identity_sha256 == IDENTITY and str(path) in decision.input_hashes
    for field in ("live_writer_lease", "active_attempt", "gpu_requested", "concurrent_worker"):
        assert not admit_stars_batch_replay(StarsAdmissionRequest(**dict(base, **{field: True}))).admitted
    with pytest.raises(Exception): StarsAdmissionRequest(**dict(base, gpu_requested=1))


def test_source_closure_has_parent_runtime_and_excludes_stars() -> None:
    closure = default_source_closure()
    assert "tools/build_step5d_autotune_v4_r011.py" in closure.files
    assert "tools/step5d_autotune_v4_r010/kernel.py" in closure.files
    assert "tools/step5d_autotune_v4_r010/optimizer_worker_loop.py" in closure.files
    assert "tools/step5d_autotune_v4_r011/stars_admission.py" not in closure.files
