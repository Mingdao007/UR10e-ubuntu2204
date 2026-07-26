#!/usr/bin/env python3
"""Build a write-once active-surface equivalence attestation for V3 timing.

This builder does not reinterpret an old capture as a new full acceptance.  It
proves which measured lanes remain reusable after non-subject orchestration or
selector changes.  Subject source drift, incomplete artifact bindings,
non-finite values, and digest/path ambiguity fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Mapping


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]
RUNTIME_SOURCE = REPOSITORY_ROOT / "src/ur10e_experiment_runtime"
if str(RUNTIME_SOURCE) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SOURCE))

from step5d_autotune_v3.identity_layers import (  # noqa: E402
    BOUNDED_HOLD_TIMING_CONTRACT,
    SELECTOR_AND_DOCUMENT_PATHS,
    runtime_environment_fingerprint,
    runtime_environment_manifest,
    timing_harness_fingerprint,
)
from step5d_autotune_v3.profile import active_tick_semantics_fingerprint  # noqa: E402
from step5d_timing_acceptance import (  # noqa: E402
    V3_RUNTIME_SOURCE_BINDING_FILES,
)
from step5d_v30_timing import SOURCE_BINDING_FILES  # noqa: E402
from ur10e_experiment_runtime.identity import (  # noqa: E402
    canonical_sha256,
    load_strict_json,
)


DEFAULT_RAW = (
    EXPERIMENT_ROOT
    / "config/step5/step5d_autotune_v3_formal_timing_raw_ede7bdb5.json"
)

_SHA256_LENGTH = 64
_TICK_FIELDS = frozenset(
    {
        "contact_semantics_sha256",
        "solver_sha256",
        "outer_loop_sha256",
        "control_contract_sha256",
        "runtime_interface_sha256",
        "kinematics_sha256",
        "bridge_sha256",
    }
)
_HARNESS_FIELDS = frozenset(
    {"harness_sha256", "bundler_sha256", "aggregator_sha256"}
)
_PROVENANCE_FIELDS = frozenset({"readiness_builder_sha256"})
if _TICK_FIELDS | _HARNESS_FIELDS | _PROVENANCE_FIELDS != set(
    SOURCE_BINDING_FILES
):
    raise RuntimeError("unclassified legacy timing source binding field")

_CALIBRATION_REPOSITORY_PATH = "src/ur10e_bringup/config/ur10e_calibration.yaml"
_V3_TP_REPOSITORY_PATH = (
    "experiments/tase-contact-reproduction/programs/step5/step5d/"
    "step5d_strict_rnn_autotune_v3.script"
)
_EXPECTED_REUSE = {
    "solver": True,
    "safe_hold": True,
    "full_tick": False,
}


class TimingEquivalenceError(ValueError):
    """The old timing capture cannot support the requested attestation."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = load_strict_json(path)
    except (OSError, ValueError) as exc:
        raise TimingEquivalenceError(f"invalid strict JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TimingEquivalenceError(f"expected JSON object: {path}")
    return value


def _sha256_file(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise TimingEquivalenceError(f"expected regular artifact: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise TimingEquivalenceError(f"{label} is not a lowercase SHA256")
    return value


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TimingEquivalenceError(f"{label} must be an object")
    return value


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TimingEquivalenceError(f"{label} must be a non-negative integer")
    return value


def _require_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimingEquivalenceError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise TimingEquivalenceError(f"{label} must be finite")
    return result


def _compact_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _display_path(experiment_root: Path, path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(experiment_root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _repository_relative(
    experiment_root: Path,
    repository_root: Path,
    experiment_relative: str,
) -> str:
    if not isinstance(experiment_relative, str) or not experiment_relative:
        raise TimingEquivalenceError("empty source path")
    resolved = (experiment_root / experiment_relative).resolve()
    try:
        return resolved.relative_to(repository_root.resolve()).as_posix()
    except ValueError as exc:
        raise TimingEquivalenceError(
            f"source path escapes repository: {experiment_relative}"
        ) from exc


def _current_digest(repository_root: Path, repository_relative: str) -> str:
    candidate = (repository_root / repository_relative).resolve()
    try:
        candidate.relative_to(repository_root.resolve())
    except ValueError as exc:
        raise TimingEquivalenceError(
            f"current source escapes repository: {repository_relative}"
        ) from exc
    return _sha256_file(candidate)


def _artifact_binding_digest(
    raw: Mapping[str, Any],
    label: str,
) -> tuple[str, str]:
    bindings = _require_mapping(raw.get("artifact_binding"), "raw.artifact_binding")
    binding = _require_mapping(bindings.get(label), f"raw.artifact_binding.{label}")
    recorded_path = binding.get("path")
    if not isinstance(recorded_path, str) or not recorded_path:
        raise TimingEquivalenceError(f"raw.artifact_binding.{label}.path missing")
    return recorded_path, _require_sha256(
        binding.get("sha256"), f"raw.artifact_binding.{label}.sha256"
    )


def _validate_artifact_envelope(
    experiment_root: Path,
    raw_path: Path,
    evaluation_path: Path,
    metadata_path: Path,
    raw: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if evaluation_path.resolve() != raw_path.with_suffix(".evaluation.json").resolve():
        raise TimingEquivalenceError("evaluation path is not the raw sidecar path")
    if metadata_path.resolve() != raw_path.with_suffix(".metadata.json").resolve():
        raise TimingEquivalenceError("metadata path is not the raw sidecar path")
    displayed_raw = _display_path(experiment_root, raw_path)
    if evaluation.get("raw_path") != displayed_raw:
        raise TimingEquivalenceError("evaluation raw_path mismatch")
    displayed_workflow_output = _display_path(experiment_root.parents[1], raw_path)
    if metadata.get("output") != displayed_workflow_output:
        raise TimingEquivalenceError("metadata output path mismatch")

    raw_sha256 = _sha256_file(raw_path)
    if evaluation.get("raw_sha256") != raw_sha256:
        raise TimingEquivalenceError("evaluation raw_sha256 mismatch")
    base = _require_mapping(
        evaluation.get("base_evaluation"), "evaluation.base_evaluation"
    )
    if base.get("raw_path") != displayed_raw or base.get("raw_sha256") != raw_sha256:
        raise TimingEquivalenceError("base evaluation raw binding mismatch")
    if raw.get("schema_version") != "step5d_v30_remote_timing_raw_v3":
        raise TimingEquivalenceError("unexpected raw timing schema")
    if (
        evaluation.get("schema_version")
        != "step5d_v3_timing_acceptance_evaluation_v1"
    ):
        raise TimingEquivalenceError("unexpected evaluation schema")
    if evaluation.get("v3_moving_sphere") != raw.get("step5d_v3_moving_sphere"):
        raise TimingEquivalenceError("evaluation moving-sphere binding mismatch")
    if (
        metadata.get("formal") is not True
        or metadata.get("step5d_v3_moving_sphere") is not True
    ):
        raise TimingEquivalenceError("metadata is not a formal Step5d V3 capture")
    if metadata.get("claim_class") != "formal_raw_capture_step5d_v3":
        raise TimingEquivalenceError("metadata claim class mismatch")
    for field in ("bundler_exit_code", "bundler_after_exit_code", "harness_exit_code"):
        if metadata.get(field) != 0:
            raise TimingEquivalenceError(f"metadata {field} is not zero")

    before = _require_mapping(
        metadata.get("source_fingerprint_before"),
        "metadata.source_fingerprint_before",
    )
    after = _require_mapping(
        metadata.get("source_fingerprint_after"),
        "metadata.source_fingerprint_after",
    )
    if metadata.get("source_fingerprint_stable") is not True or before != after:
        raise TimingEquivalenceError("capture source fingerprint was not stable")

    return {
        "raw": {
            "path": displayed_raw,
            "sha256": raw_sha256,
            "bytes": raw_path.stat().st_size,
        },
        "evaluation": {
            "path": _display_path(experiment_root, evaluation_path),
            "sha256": _sha256_file(evaluation_path),
            "bytes": evaluation_path.stat().st_size,
        },
        "metadata": {
            "path": _display_path(experiment_root, metadata_path),
            "sha256": _sha256_file(metadata_path),
            "bytes": metadata_path.stat().st_size,
        },
    }


def _source_identities(
    experiment_root: Path,
    repository_root: Path,
    raw: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    raw_binding = _require_mapping(raw.get("source_binding"), "raw.source_binding")
    if raw_binding.get("delivery") != "stdin_bundle":
        raise TimingEquivalenceError("raw source delivery is not stdin_bundle")
    metadata_before = _require_mapping(
        metadata.get("source_fingerprint_before"),
        "metadata.source_fingerprint_before",
    )
    recorded_files = _require_mapping(
        metadata_before.get("files"), "metadata source files"
    )

    recorded_tick: dict[str, str] = {}
    current_tick: dict[str, str] = {}
    recorded_harness: dict[str, str] = {}
    current_harness: dict[str, str] = {}
    provenance: dict[str, Any] = {}

    for field, experiment_relative in SOURCE_BINDING_FILES.items():
        recorded = _require_sha256(raw_binding.get(field), f"raw.source_binding.{field}")
        metadata_recorded = _require_sha256(
            recorded_files.get(experiment_relative),
            f"metadata.files.{experiment_relative}",
        )
        if recorded != metadata_recorded:
            raise TimingEquivalenceError(f"raw/metadata source digest mismatch: {field}")
        repository_relative = _repository_relative(
            experiment_root, repository_root, experiment_relative
        )
        current = _current_digest(repository_root, repository_relative)
        if field in _TICK_FIELDS:
            if recorded != current:
                raise TimingEquivalenceError(f"tick subject source drift: {repository_relative}")
            recorded_tick[repository_relative] = recorded
            current_tick[repository_relative] = current
        elif field in _HARNESS_FIELDS:
            if recorded != current:
                raise TimingEquivalenceError(f"timing harness source drift: {repository_relative}")
            recorded_harness[repository_relative] = recorded
            current_harness[repository_relative] = current
        else:
            provenance[repository_relative] = {
                "classification": "verifier_or_builder_provenance_only",
                "recorded_sha256": recorded,
                "current_sha256": current,
                "matches": recorded == current,
            }

    sphere = _require_mapping(
        raw.get("step5d_v3_moving_sphere"), "raw.step5d_v3_moving_sphere"
    )
    sphere_binding = _require_mapping(
        sphere.get("source_binding"), "raw moving-sphere source_binding"
    )
    for field, experiment_relative in V3_RUNTIME_SOURCE_BINDING_FILES.items():
        recorded = _require_sha256(
            sphere_binding.get(field), f"raw.moving_sphere.source_binding.{field}"
        )
        metadata_recorded = _require_sha256(
            recorded_files.get(experiment_relative),
            f"metadata.files.{experiment_relative}",
        )
        if recorded != metadata_recorded:
            raise TimingEquivalenceError(
                f"raw/metadata moving-sphere digest mismatch: {field}"
            )
        repository_relative = _repository_relative(
            experiment_root, repository_root, experiment_relative
        )
        current = _current_digest(repository_root, repository_relative)
        if recorded != current:
            raise TimingEquivalenceError(
                f"moving-sphere subject source drift: {repository_relative}"
            )
        recorded_tick[repository_relative] = recorded
        current_tick[repository_relative] = current

    return (
        {
            "recorded": dict(sorted(recorded_tick.items())),
            "current": dict(sorted(current_tick.items())),
        },
        {
            "recorded": dict(sorted(recorded_harness.items())),
            "current": dict(sorted(current_harness.items())),
        },
        dict(sorted(provenance.items())),
    )


def _validate_external_subjects(
    repository_root: Path,
    raw: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, str]:
    recorded_inputs = _require_mapping(
        _require_mapping(
            metadata.get("source_fingerprint_before"),
            "metadata.source_fingerprint_before",
        ).get("external_model_inputs"),
        "metadata external_model_inputs",
    )

    calibration_path, calibration_digest = _artifact_binding_digest(raw, "calibration_yaml")
    if recorded_inputs.get(calibration_path) != calibration_digest:
        raise TimingEquivalenceError("calibration raw/metadata digest mismatch")
    current_calibration = _current_digest(repository_root, _CALIBRATION_REPOSITORY_PATH)
    if current_calibration != calibration_digest:
        raise TimingEquivalenceError("calibration subject drift")

    xacro_path, xacro_digest = _artifact_binding_digest(raw, "ur_xacro")
    if recorded_inputs.get(xacro_path) != xacro_digest:
        raise TimingEquivalenceError("xacro raw/metadata digest mismatch")
    if _sha256_file(Path(xacro_path)) != xacro_digest:
        raise TimingEquivalenceError("xacro subject drift")

    _tp_recorded_path, tp_digest = _artifact_binding_digest(raw, "v3_tp_script")
    current_tp = _current_digest(repository_root, _V3_TP_REPOSITORY_PATH)
    if current_tp != tp_digest:
        raise TimingEquivalenceError("V3 TP watchdog subject drift")

    replay_path, replay_digest = _artifact_binding_digest(raw, "replay_csv")
    replay_metadata = _require_mapping(
        _require_mapping(
            metadata.get("source_fingerprint_before"),
            "metadata.source_fingerprint_before",
        ).get("replay_csv"),
        "metadata replay_csv",
    )
    if replay_metadata.get("path") != replay_path or replay_metadata.get("sha256") != replay_digest:
        raise TimingEquivalenceError("replay capture input binding mismatch")

    return {
        "calibration_yaml_sha256": calibration_digest,
        "replay_csv_sha256": replay_digest,
        "tp_script_sha256": tp_digest,
        "ur_xacro_sha256": xacro_digest,
    }


def _semantic_material(raw: Mapping[str, Any]) -> dict[str, Any]:
    profile = _require_mapping(raw.get("profile"), "raw.profile")
    profile_digest = _require_sha256(raw.get("profile_sha256"), "raw.profile_sha256")
    if _compact_json_sha256(profile) != profile_digest:
        raise TimingEquivalenceError("raw profile_sha256 mismatch")
    pacing = _require_mapping(raw.get("pacing_provenance"), "raw.pacing_provenance")
    if pacing != BOUNDED_HOLD_TIMING_CONTRACT["pacing"]:
        raise TimingEquivalenceError("raw pacing differs from bounded-hold contract")
    transport = _require_mapping(
        raw.get("controller_stale_hold_fault_evidence"),
        "raw.controller_stale_hold_fault_evidence",
    )
    sphere = _require_mapping(
        raw.get("step5d_v3_moving_sphere"), "raw.step5d_v3_moving_sphere"
    )
    for field in (
        "schema",
        "enabled",
        "physical_prior_fingerprint",
        "reference_sha256",
        "fixture_stopping_bound_fingerprint",
        "fixture_stopping_bound_validity_domain",
    ):
        if field not in sphere:
            raise TimingEquivalenceError(f"moving-sphere semantic missing: {field}")
    runtime_path = raw.get("runtime_path")
    if not isinstance(runtime_path, str) or not runtime_path:
        raise TimingEquivalenceError("raw runtime_path missing")
    return {
        "profile": profile,
        "profile_sha256": profile_digest,
        "pacing": pacing,
        "transport": transport,
        "moving_sphere": {field: sphere[field] for field in (
            "schema",
            "enabled",
            "physical_prior_fingerprint",
            "reference_sha256",
            "fixture_stopping_bound_fingerprint",
            "fixture_stopping_bound_validity_domain",
        )},
        "runtime_path": runtime_path,
    }


def _runtime_identity(
    raw: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    environment = _require_mapping(raw.get("runtime_environment"), "raw.runtime_environment")
    source_fingerprint = _require_mapping(
        metadata.get("source_fingerprint_before"),
        "metadata.source_fingerprint_before",
    )
    metadata_affinity = source_fingerprint.get("cpu_affinity")
    if environment.get("cpu_affinity") != metadata_affinity:
        raise TimingEquivalenceError("runtime/metadata CPU affinity mismatch")
    execution = _require_mapping(
        source_fingerprint.get("execution_contract"), "metadata execution_contract"
    )
    if (
        execution.get("scheduler_contract") != "sched_other_0"
        or environment.get("scheduler_policy_name") != "SCHED_OTHER"
        or environment.get("scheduler_policy") != 0
        or environment.get("scheduler_priority") != 0
        or environment.get("nice") != 0
    ):
        raise TimingEquivalenceError("runtime scheduler differs from SCHED_OTHER/0 NI=0")
    effective = _require_mapping(
        source_fingerprint.get("effective_environment"),
        "metadata effective_environment",
    )
    thread_environment = _require_mapping(
        environment.get("thread_environment"), "runtime thread_environment"
    )
    for name, value in thread_environment.items():
        if effective.get(name) != value:
            raise TimingEquivalenceError(f"thread environment mismatch: {name}")

    interpreter = _require_mapping(
        source_fingerprint.get("interpreter"), "metadata interpreter"
    )
    gpu_device = _require_mapping(raw.get("gpu_device"), "raw.gpu_device")
    nvidia = _require_mapping(raw.get("nvidia_smi"), "raw.nvidia_smi")
    nvidia_start = _require_mapping(nvidia.get("start"), "raw.nvidia_smi.start")
    nvidia_end = _require_mapping(nvidia.get("end"), "raw.nvidia_smi.end")
    start_values = _require_mapping(nvidia_start.get("values"), "nvidia start values")
    end_values = _require_mapping(nvidia_end.get("values"), "nvidia end values")
    for field in ("driver_version", "name", "pci.bus_id"):
        if start_values.get(field) != end_values.get(field):
            raise TimingEquivalenceError(f"stable GPU identity mismatch: {field}")

    stable_environment = {
        "capture_runtime": environment,
        "effective_environment": effective,
        "execution_contract": execution,
        "interpreter": {
            "platform": interpreter.get("platform"),
            "sha256": _require_sha256(interpreter.get("sha256"), "interpreter.sha256"),
            "version": interpreter.get("version"),
        },
        "gpu": {
            "device": gpu_device,
            "driver_version": start_values.get("driver_version"),
            "name": start_values.get("name"),
            "pci_bus_id": start_values.get("pci.bus_id"),
        },
    }
    manifest = runtime_environment_manifest(stable_environment)
    return manifest, runtime_environment_fingerprint(stable_environment)


def _lane_result(
    raw: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    lane: str,
) -> dict[str, Any]:
    contract = _require_mapping(
        BOUNDED_HOLD_TIMING_CONTRACT.get(lane), f"timing contract {lane}"
    )
    summary = _require_mapping(raw.get(lane), f"raw.{lane}")
    blockers: list[str] = []
    samples = _require_int(summary.get("samples"), f"raw.{lane}.samples")
    nonfinite = _require_int(
        summary.get("nonfinite_count"), f"raw.{lane}.nonfinite_count"
    )
    p99 = _require_number(summary.get("p99_ms"), f"raw.{lane}.p99_ms")
    compute_miss = _require_int(
        summary.get("compute_deadline_miss_count"),
        f"raw.{lane}.compute_deadline_miss_count",
    )
    if samples != _require_int(contract.get("samples"), f"contract.{lane}.samples"):
        blockers.append("sample_count_mismatch")
    if nonfinite != 0:
        blockers.append("nonfinite_samples")
    if p99 > _require_number(contract.get("p99_max_ms"), f"contract.{lane}.p99_max_ms"):
        blockers.append("p99_exceeds_limit")

    metrics: dict[str, Any] = {
        "samples": samples,
        "nonfinite_count": nonfinite,
        "p99_ms": p99,
        "compute_miss_count": compute_miss,
    }
    if lane == "solver":
        maximum = _require_number(summary.get("max_ms"), "raw.solver.max_ms")
        metrics["max_ms"] = maximum
        if maximum >= _require_number(contract.get("max_exclusive_ms"), "contract.solver.max_exclusive_ms"):
            blockers.append("max_not_below_limit")
        if compute_miss > _require_int(contract.get("deadline_miss_max"), "contract.solver.deadline_miss_max"):
            blockers.append("compute_miss_budget_exceeded")
    else:
        if compute_miss > _require_int(contract.get("compute_miss_max"), f"contract.{lane}.compute_miss_max"):
            blockers.append("compute_miss_budget_exceeded")
        schedule_count = _require_int(
            raw.get(f"{lane}_schedule_deadline_miss_count"),
            f"raw.{lane}_schedule_deadline_miss_count",
        )
        lateness = _require_number(
            raw.get(f"{lane}_schedule_max_lateness_ms"),
            f"raw.{lane}_schedule_max_lateness_ms",
        )
        base = _require_mapping(
            evaluation.get("base_evaluation"), "evaluation.base_evaluation"
        )
        base_evaluation = _require_mapping(base.get("evaluation"), "base evaluation")
        deadline = _require_mapping(
            base_evaluation.get("deadline_robustness"), "deadline robustness"
        )
        diagnostics = _require_mapping(
            deadline.get("deadline_miss_diagnostics"), "deadline miss diagnostics"
        )
        consecutive: dict[str, int] = {}
        for kind in ("compute", "schedule"):
            diagnostic = _require_mapping(
                diagnostics.get(f"{lane}_{kind}"),
                f"deadline diagnostics {lane}_{kind}",
            )
            if diagnostic.get("overflowed") is not False:
                raise TimingEquivalenceError(
                    f"deadline diagnostics overflowed: {lane}_{kind}"
                )
            total = _require_int(
                diagnostic.get("total"), f"deadline diagnostics {lane}_{kind}.total"
            )
            expected_total = compute_miss if kind == "compute" else schedule_count
            if total != expected_total:
                raise TimingEquivalenceError(
                    f"deadline diagnostic total mismatch: {lane}_{kind}"
                )
            consecutive[kind] = _require_int(
                diagnostic.get("max_consecutive"),
                f"deadline diagnostics {lane}_{kind}.max_consecutive",
            )
        maximum_consecutive = max(consecutive.values())
        metrics.update(
            {
                "schedule_miss_count": schedule_count,
                "schedule_max_lateness_ms": lateness,
                "max_consecutive_compute_miss": consecutive["compute"],
                "max_consecutive_schedule_miss": consecutive["schedule"],
                "max_consecutive_miss": maximum_consecutive,
            }
        )
        if schedule_count > _require_int(contract.get("schedule_miss_max"), f"contract.{lane}.schedule_miss_max"):
            blockers.append("schedule_miss_budget_exceeded")
        if maximum_consecutive > _require_int(contract.get("max_consecutive_miss"), f"contract.{lane}.max_consecutive_miss"):
            blockers.append("max_consecutive_miss_exceeded")
        if lateness > _require_number(contract.get("schedule_max_lateness_ms"), f"contract.{lane}.schedule_max_lateness_ms"):
            blockers.append("schedule_lateness_exceeds_limit")

    return {
        "reusable": not blockers,
        "metrics": metrics,
        "blockers": sorted(blockers),
    }


def _provenance_inventory(
    experiment_root: Path,
    repository_root: Path,
    metadata: Mapping[str, Any],
    already_classified: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(already_classified)
    metadata_files = _require_mapping(
        _require_mapping(
            metadata.get("source_fingerprint_before"),
            "metadata.source_fingerprint_before",
        ).get("files"),
        "metadata source files",
    )
    subject_fields = _TICK_FIELDS | _HARNESS_FIELDS
    subject_paths = {
        _repository_relative(experiment_root, repository_root, SOURCE_BINDING_FILES[field])
        for field in subject_fields
    }
    subject_paths.update(
        _repository_relative(experiment_root, repository_root, relative)
        for relative in V3_RUNTIME_SOURCE_BINDING_FILES.values()
    )
    selector_paths = set(SELECTOR_AND_DOCUMENT_PATHS)
    for experiment_relative, raw_digest in metadata_files.items():
        repository_relative = _repository_relative(
            experiment_root, repository_root, experiment_relative
        )
        if repository_relative in subject_paths or repository_relative in result:
            continue
        recorded = _require_sha256(raw_digest, f"metadata.files.{experiment_relative}")
        current = _current_digest(repository_root, repository_relative)
        classification = (
            "selector_or_document_provenance_only"
            if repository_relative in selector_paths
            else "verifier_builder_or_capture_provenance_only"
        )
        result[repository_relative] = {
            "classification": classification,
            "recorded_sha256": recorded,
            "current_sha256": current,
            "matches": recorded == current,
        }
    for repository_relative in sorted(selector_paths):
        if repository_relative in result:
            continue
        result[repository_relative] = {
            "classification": "selector_or_document_provenance_only",
            "recorded_sha256": None,
            "current_sha256": _current_digest(repository_root, repository_relative),
            "matches": None,
        }
    return dict(sorted(result.items()))


def build_attestation(
    experiment_root: Path,
    raw_path: Path,
    evaluation_path: Path | None = None,
    metadata_path: Path | None = None,
) -> dict[str, Any]:
    """Validate one old capture and return its immutable sidecar payload."""

    experiment_root = experiment_root.resolve()
    repository_root = experiment_root.parents[1]
    raw_path = raw_path.resolve()
    evaluation_path = (evaluation_path or raw_path.with_suffix(".evaluation.json")).resolve()
    metadata_path = (metadata_path or raw_path.with_suffix(".metadata.json")).resolve()
    raw = _load_object(raw_path)
    evaluation = _load_object(evaluation_path)
    metadata = _load_object(metadata_path)

    inputs = _validate_artifact_envelope(
        experiment_root,
        raw_path,
        evaluation_path,
        metadata_path,
        raw,
        evaluation,
        metadata,
    )
    tick_sources, harness_sources, initial_provenance = _source_identities(
        experiment_root, repository_root, raw, metadata
    )
    external_subjects = _validate_external_subjects(repository_root, raw, metadata)
    semantics = _semantic_material(raw)

    recorded_tick_manifest = {
        "schema": "step5d.autotune-v3/legacy-capture-tick-active-surface-v1",
        "sources": tick_sources["recorded"],
        "external_inputs": external_subjects,
        "semantics": semantics,
    }
    current_tick_manifest = {
        **recorded_tick_manifest,
        "sources": tick_sources["current"],
    }
    recorded_tick_fingerprint = canonical_sha256(recorded_tick_manifest)
    current_tick_fingerprint = canonical_sha256(current_tick_manifest)
    if recorded_tick_fingerprint != current_tick_fingerprint:
        raise TimingEquivalenceError("tick semantics fingerprint mismatch")

    recorded_harness_manifest = {
        "schema": "step5d.autotune-v3/timing-harness-identity-v1",
        "measurement_sources": harness_sources["recorded"],
        "contract": BOUNDED_HOLD_TIMING_CONTRACT,
    }
    current_harness_manifest = {
        **recorded_harness_manifest,
        "measurement_sources": harness_sources["current"],
    }
    recorded_harness_fingerprint = canonical_sha256(recorded_harness_manifest)
    current_harness_fingerprint = canonical_sha256(current_harness_manifest)
    if recorded_harness_fingerprint != current_harness_fingerprint:
        raise TimingEquivalenceError("timing harness fingerprint mismatch")
    target_harness_fingerprint = timing_harness_fingerprint(repository_root)
    if current_harness_fingerprint != target_harness_fingerprint:
        raise TimingEquivalenceError(
            "current capture harness differs from layered timing harness identity"
        )
    target_tick_fingerprint = active_tick_semantics_fingerprint(
        experiment_root=experiment_root
    )

    runtime_manifest, runtime_fingerprint = _runtime_identity(raw, metadata)
    lanes = {
        lane: _lane_result(raw, evaluation, lane)
        for lane in ("solver", "safe_hold", "full_tick")
    }
    observed_reuse = {
        lane: lane_result["reusable"] for lane, lane_result in lanes.items()
    }
    if observed_reuse != _EXPECTED_REUSE:
        raise TimingEquivalenceError(
            f"capture lane reuse classification changed: {observed_reuse!r}"
        )

    provenance = _provenance_inventory(
        experiment_root,
        repository_root,
        metadata,
        initial_provenance,
    )
    return {
        "schema": "step5d.autotune-v3/timing-active-surface-equivalence-v1",
        "claim_class": "formal_timing_lane_reuse_attestation",
        "inputs": inputs,
        "subject_identity": {
            "tick_semantics": {
                "recorded_fingerprint": recorded_tick_fingerprint,
                "current_fingerprint": current_tick_fingerprint,
                "equivalent": True,
                "target_layered_fingerprint": target_tick_fingerprint,
                "migration_rule": (
                    "the captured hot-path source/model subset is byte-equivalent; "
                    "new artifacts bind the explicit layered target fingerprint"
                ),
                "recorded_manifest": recorded_tick_manifest,
                "current_manifest": current_tick_manifest,
            },
            "timing_harness": {
                "recorded_fingerprint": recorded_harness_fingerprint,
                "current_fingerprint": current_harness_fingerprint,
                "target_layered_fingerprint": target_harness_fingerprint,
                "equivalent": True,
                "recorded_manifest": recorded_harness_manifest,
                "current_manifest": current_harness_manifest,
            },
            "runtime_environment": {
                "fingerprint": runtime_fingerprint,
                "manifest": runtime_manifest,
                "reuse_condition": (
                    "a consuming final capture must present this exact normalized "
                    "runtime_environment_fingerprint"
                ),
            },
        },
        "lane_reuse": lanes,
        "provenance_only": {
            "excluded_from_subject_fingerprints": provenance,
            "rule": (
                "selector, documentation, readiness/evidence verifier, checkout "
                "path, output path, and artifact self-identity cannot invalidate "
                "measured lanes; pacing, delivery, and statistics code remain "
                "timing-harness subjects"
            ),
        },
        "next_required_lane": "full_tick",
        "final_full_tick_required": True,
        "claim_boundary": (
            "solver and safe-hold are reusable only for the identical tick, harness, "
            "and runtime-environment identities above; full-tick remains failed and "
            "must be captured once under the frozen bounded-hold contract. This "
            "attestation is offline evidence and does not authorize bridge, ARM, "
            "motion, contact, or campaign execution"
        ),
    }


def write_immutable_sidecar(path: Path, payload: Mapping[str, Any]) -> None:
    """Create one read-only JSON sidecar without overwriting an existing file."""

    encoded = (
        json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o444)
    except FileExistsError as exc:
        raise TimingEquivalenceError(f"immutable sidecar already exists: {path}") from exc
    try:
        os.fchmod(descriptor, 0o444)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-root", type=Path, default=EXPERIMENT_ROOT)
    parser.add_argument("--raw", type=Path, default=DEFAULT_RAW)
    parser.add_argument("--evaluation", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    raw_path = args.raw.resolve()
    payload = build_attestation(
        args.experiment_root,
        raw_path,
        args.evaluation,
        args.metadata,
    )
    output = (args.output or raw_path.with_suffix(".equivalence.json")).resolve()
    write_immutable_sidecar(output, payload)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
