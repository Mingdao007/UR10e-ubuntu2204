"""Manifest-first UR10e experiment runtime (offline scaffolding only)."""

from .batch import (
    BatchFate,
    BatchIdentity,
    BatchJournal,
    BatchRow,
    ExactAckReceipt,
    ReturnReferenceKind,
    SafeClosureReceipt,
)

from .contracts import (
    AutotuneCandidate,
    ExperimentRuntimeError,
    ExperimentSpec,
    ObjectiveContract,
    OutputPathError,
    ResourceLockError,
    RegistryError,
    RunManifest,
    SafetyGateError,
    SpecValidationError,
    UnsupportedExecutionError,
)
from .identity import (
    StrictJSONError,
    canonical_json_bytes,
    canonical_sha256,
    load_strict_json,
    strict_json_loads,
)
from .registry import ComponentKind, ComponentRegistry, build_default_registry
from .runtime import (
    HostResourceLock,
    append_run_state_event,
    create_exclusive_run_directory,
    finalize_run_manifest,
    load_experiment_spec,
    load_run_manifest,
    plan_experiment,
    run_experiment,
    status,
    validate_experiment_spec,
    validate_parallel_run_manifest,
    validate_run_manifest,
)


__all__ = [
    "AutotuneCandidate",
    "BatchFate",
    "BatchIdentity",
    "BatchJournal",
    "BatchRow",
    "ComponentKind",
    "ComponentRegistry",
    "ExperimentRuntimeError",
    "ExperimentSpec",
    "ExactAckReceipt",
    "ObjectiveContract",
    "OutputPathError",
    "ResourceLockError",
    "RegistryError",
    "RunManifest",
    "ReturnReferenceKind",
    "SafeClosureReceipt",
    "SafetyGateError",
    "SpecValidationError",
    "StrictJSONError",
    "UnsupportedExecutionError",
    "build_default_registry",
    "append_run_state_event",
    "canonical_json_bytes",
    "canonical_sha256",
    "create_exclusive_run_directory",
    "finalize_run_manifest",
    "HostResourceLock",
    "load_experiment_spec",
    "load_run_manifest",
    "load_strict_json",
    "plan_experiment",
    "run_experiment",
    "status",
    "strict_json_loads",
    "validate_experiment_spec",
    "validate_parallel_run_manifest",
    "validate_run_manifest",
]

__version__ = "0.1.0"
