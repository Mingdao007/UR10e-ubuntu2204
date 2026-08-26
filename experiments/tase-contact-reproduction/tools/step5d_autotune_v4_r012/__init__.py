"""Offline-only Autotune V4 R012 behavior-first primitives."""

from .behavior import (
    DEFAULT_MANUAL_WAVE,
    ManualWaveSchedule,
    R012_LINEAGE,
    R012_PROGRAM,
    R012_RUNTIME_PROTOCOL,
    command_speed_m_s,
    default_manual_wave,
    travel_sigmoid_speed_m_s,
    validate_manual_wave,
)
from .censor import (
    CensoredMAEAccumulator,
    CensorDecision,
    CensorProtocol,
    CensoredObservation,
    ExactObservation,
    FakeRTDE,
    PathEarlyEndHandshake,
    evaluate_censor_prefix,
    legacy_gp_training_rows,
    require_exact_for_legacy_gp,
    causal_lower_bound,
    kappa_for_progress,
    prefix_mean,
)
from .snapshot import LiveSnapshot, R012BehaviorConfig, build_live_snapshot, load_live_snapshot
from .noise import (
    CALIBRATION_GRID_N2,
    NOISE_FLOOR_N2,
    NoiseObservation,
    NoiseRefit,
    ObservationNoiseAttestation,
    fit_hierarchical_noise,
)
from .safety_filter import SafetyFilterConfig, SafetyFilterResult, SafetyIntervention, SafetyTimingReceipt, benchmark_path_error_filter, benchmark_safety_filter, filter_path_error_twist, project_path_command, project_path_error_command, reference_project_path_error_command, validate_safety_intervention
from .qlognei import ProductionGPConfig, ProductionGPFit, ask_qlognei, candidate_to_log_features, candidate_to_normalized, candidate_to_log_features_unbounded, fit_production_gp, fit_test_oracle, physical_candidate_key, TAU_BOUNDS, MODEL_DIMENSIONS
from .scheduler import CandidateAggregate, ConfirmedIncumbentScheduler, REFERENCE_CANDIDATE, ScheduledCandidate
from .wave import (
    NormalForceMetrics,
    WaveQualificationReceipt,
    WaveRunTrace,
    canonical_normal_force_metrics,
    qualify_manual_wave,
)

__all__ = [
    "CALIBRATION_GRID_N2", "CensorDecision", "CensorProtocol", "CensoredMAEAccumulator", "CensoredObservation",
    "DEFAULT_MANUAL_WAVE", "ExactObservation", "ManualWaveSchedule", "MODEL_DIMENSIONS", "NOISE_FLOOR_N2",
    "NormalForceMetrics", "NoiseObservation", "NoiseRefit", "ObservationNoiseAttestation",
    "LiveSnapshot", "R012BehaviorConfig", "R012_LINEAGE", "R012_PROGRAM", "R012_RUNTIME_PROTOCOL", "TAU_BOUNDS",
    "FakeRTDE", "PathEarlyEndHandshake", "ProductionGPConfig", "ProductionGPFit", "CandidateAggregate", "ConfirmedIncumbentScheduler", "REFERENCE_CANDIDATE", "ScheduledCandidate", "SafetyFilterConfig", "SafetyFilterResult", "SafetyIntervention", "SafetyTimingReceipt", "WaveQualificationReceipt",
    "WaveRunTrace", "build_live_snapshot", "canonical_normal_force_metrics",
    "command_speed_m_s", "default_manual_wave", "fit_hierarchical_noise",
    "ask_qlognei", "benchmark_path_error_filter", "benchmark_safety_filter", "candidate_to_log_features", "candidate_to_log_features_unbounded", "candidate_to_normalized", "causal_lower_bound", "evaluate_censor_prefix", "filter_path_error_twist", "fit_production_gp", "fit_test_oracle", "kappa_for_progress", "legacy_gp_training_rows", "physical_candidate_key", "prefix_mean", "project_path_command", "project_path_error_command", "reference_project_path_error_command", "qualify_manual_wave", "require_exact_for_legacy_gp", "validate_safety_intervention",
    "travel_sigmoid_speed_m_s", "validate_manual_wave", "load_live_snapshot",
]
