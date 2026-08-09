"""Offline-only Autotune V4 R011 behavior-first primitives."""

from .behavior import (
    DEFAULT_MANUAL_WAVE,
    ManualWaveSchedule,
    R011_LINEAGE,
    R011_PROGRAM,
    R011_RUNTIME_PROTOCOL,
    command_speed_m_s,
    default_manual_wave,
    travel_sigmoid_speed_m_s,
    validate_manual_wave,
)
from .censor import (
    CensoredMAEAccumulator,
    CensorProtocol,
    CensoredObservation,
    ExactObservation,
    legacy_gp_training_rows,
    require_exact_for_legacy_gp,
    causal_lower_bound,
    kappa_for_progress,
)
from .identity import (
    BehaviorManifest,
    ReleaseIdentity,
    SourceClosure,
    build_behavior_manifest,
    build_release_identity,
    default_source_closure,
    validate_behavior_manifest,
    validate_release_identity,
)
from .noise import (
    CALIBRATION_GRID_N2,
    NOISE_FLOOR_N2,
    NoiseObservation,
    NoiseRefit,
    ObservationNoiseAttestation,
    fit_hierarchical_noise,
)
from .safety_filter import SafetyFilterConfig, SafetyFilterResult, SafetyIntervention, SafetyTimingReceipt, benchmark_safety_filter, project_path_command, validate_safety_intervention
from .wave import (
    NormalForceMetrics,
    WaveQualificationReceipt,
    WaveRunTrace,
    canonical_normal_force_metrics,
    qualify_manual_wave,
)

__all__ = [
    "BehaviorManifest", "CALIBRATION_GRID_N2", "CensorProtocol", "CensoredMAEAccumulator", "CensoredObservation",
    "DEFAULT_MANUAL_WAVE", "ExactObservation", "ManualWaveSchedule", "NOISE_FLOOR_N2",
    "NormalForceMetrics", "NoiseObservation", "NoiseRefit", "ObservationNoiseAttestation",
    "ReleaseIdentity", "R011_LINEAGE", "R011_PROGRAM", "R011_RUNTIME_PROTOCOL",
    "SafetyFilterConfig", "SafetyFilterResult", "SafetyIntervention", "SafetyTimingReceipt", "SourceClosure", "WaveQualificationReceipt",
    "WaveRunTrace", "build_behavior_manifest", "build_release_identity", "canonical_normal_force_metrics",
    "command_speed_m_s", "default_manual_wave", "default_source_closure", "fit_hierarchical_noise",
    "benchmark_safety_filter", "causal_lower_bound", "kappa_for_progress", "legacy_gp_training_rows", "project_path_command", "qualify_manual_wave", "require_exact_for_legacy_gp", "validate_safety_intervention",
    "travel_sigmoid_speed_m_s", "validate_behavior_manifest", "validate_manual_wave", "validate_release_identity",
]
