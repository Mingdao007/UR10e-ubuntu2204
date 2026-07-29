"""Production admission adapter for shared TrialSpec and TrialResult contracts.

The legacy supervisor remains the control state machine.  This adapter binds
each actual ARM admission to a shared TrialSpec before the ARM packet is
returned, then admits exactly one TrialResult at terminal close.  Both records
are immutable sidecars under the legacy trial directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import secrets
from typing import Any, Mapping

from step5d_autotune_contract import (
    MIN_PRODUCTION_FORCE_DAMPING,
    CODEX_I_SCALE_MULTIPLIERS,
    SEED_FORCE_DAMPING,
    SEED_FORCE_I_GAIN,
    SEED_FORCE_P_GAIN,
    CaptureManifest,
    Evaluation,
    SearchTier,
    TrialDisposition as LegacyDisposition,
    TrialSpec as LegacyTrialSpec,
)

from .shared_contracts import (
    ArtifactReference,
    MetricValue,
    SafetyObservation,
    TerminalDisposition,
    TrialResult,
    TrialSpec,
    UnitParameter,
    accept_terminal_result,
    canonical_bytes,
    canonical_sha256,
)


ADMISSION_SCHEMA = "step5d.autotune-v3/trial-contract-admission-v1"
ADMISSION_NAME = "shared_trial_admission.json"
RESULT_NAME = "trial_result.json"
DEFAULT_DEADLINE_WINDOW_NS = 30 * 60 * 1_000_000_000
REQUIRED_OBSERVATIONS = (
    "completion_marker",
    "feedback_fresh",
    "safety_normal",
    "returned_safe",
    "fingerprint_closed",
)
PARAMETER_UNITS = {
    "target_force_n": "N",
    "force_p_gain": "m/(s*N)",
    "force_i_gain": "m/(s^2*N)",
    "force_damping": "1",
}


class TrialContractAdmissionError(RuntimeError):
    pass


def _strict_json(path: Path, role: str) -> Mapping[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise TrialContractAdmissionError(
                    f"{role} repeats JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        payload = json.loads(
            path.read_text(encoding="ascii"),
            object_pairs_hook=unique,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"forbidden constant {value!r}")
            ),
        )
    except TrialContractAdmissionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise TrialContractAdmissionError(f"{role} is invalid: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise TrialContractAdmissionError(f"{role} is not an object")
    return payload


def _atomic_create(path: Path, payload: Mapping[str, Any], role: str) -> None:
    encoded = canonical_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise TrialContractAdmissionError(f"{role} path is a symlink")
    if path.exists():
        if path.read_bytes() != encoded:
            raise TrialContractAdmissionError(
                f"{role} already exists with different bytes"
            )
        return
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise TrialContractAdmissionError(
                f"{role} concurrently acquired different bytes"
            )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _parameter_bounds() -> dict[str, tuple[float, float]]:
    radius = SearchTier.T3.p_d_radius_octaves
    i_values = [
        0.0,
        SEED_FORCE_I_GAIN * (2.0**-SearchTier.T3.positive_i_radius_octaves),
        SEED_FORCE_I_GAIN * (2.0**SearchTier.T3.positive_i_radius_octaves),
        *(SEED_FORCE_I_GAIN * value for value in CODEX_I_SCALE_MULTIPLIERS),
    ]
    return {
        "target_force_n": (12.0, 12.0),
        "force_p_gain": (
            SEED_FORCE_P_GAIN * (2.0**-radius),
            SEED_FORCE_P_GAIN * (2.0**radius),
        ),
        "force_i_gain": (min(i_values), max(i_values)),
        "force_damping": (
            MIN_PRODUCTION_FORCE_DAMPING,
            SEED_FORCE_DAMPING * (2.0**radius),
        ),
    }


def _parameters(trial: LegacyTrialSpec) -> dict[str, UnitParameter]:
    values = trial.candidate.payload()
    bounds = _parameter_bounds()
    return {
        name: UnitParameter(
            value=values[name],
            unit=PARAMETER_UNITS[name],
            lower_bound=bounds[name][0],
            upper_bound=bounds[name][1],
        )
        for name in PARAMETER_UNITS
    }


def build_trial_spec(
    trial: LegacyTrialSpec,
    *,
    selection: Mapping[str, Any],
    admitted_at_unix_ns: int,
    deadline_window_ns: int = DEFAULT_DEADLINE_WINDOW_NS,
) -> TrialSpec:
    if not isinstance(trial, LegacyTrialSpec):
        raise TrialContractAdmissionError("legacy TrialSpec differs")
    if (
        isinstance(admitted_at_unix_ns, bool)
        or not isinstance(admitted_at_unix_ns, int)
        or admitted_at_unix_ns <= 0
        or isinstance(deadline_window_ns, bool)
        or not isinstance(deadline_window_ns, int)
        or deadline_window_ns <= 0
    ):
        raise TrialContractAdmissionError("trial admission timing differs")
    suggestion_id = canonical_sha256(
        {
            "legacy_trial_uid": trial.trial_uid,
            "selection": dict(selection),
            "candidate": trial.candidate.payload(),
        }
    )
    return TrialSpec(
        trial_id=trial.trial_uid,
        suggestion_id=suggestion_id,
        parameters=_parameters(trial),
        release_id=trial.source_fingerprint,
        safety_id=trial.config_fingerprint,
        deadline_unix_ns=admitted_at_unix_ns + deadline_window_ns,
        required_observations=REQUIRED_OBSERVATIONS,
    )


def persist_trial_admission(
    trial_dir: Path,
    trial: LegacyTrialSpec,
    *,
    selection: Mapping[str, Any],
    admitted_at_unix_ns: int,
) -> TrialSpec:
    spec = build_trial_spec(
        trial,
        selection=selection,
        admitted_at_unix_ns=admitted_at_unix_ns,
    )
    payload = {
        "schema": ADMISSION_SCHEMA,
        "admitted_at_unix_ns": admitted_at_unix_ns,
        "legacy_trial_spec_digest": hashlib.sha256(
            canonical_bytes(trial.payload())
        ).hexdigest(),
        "trial_spec": spec.to_payload(),
    }
    _atomic_create(trial_dir / ADMISSION_NAME, payload, "shared trial admission")
    return spec


def load_trial_admission(
    trial_dir: Path,
    trial: LegacyTrialSpec,
) -> tuple[TrialSpec, int]:
    payload = _strict_json(trial_dir / ADMISSION_NAME, "shared trial admission")
    expected_fields = {
        "schema",
        "admitted_at_unix_ns",
        "legacy_trial_spec_digest",
        "trial_spec",
    }
    legacy_digest = hashlib.sha256(canonical_bytes(trial.payload())).hexdigest()
    if (
        set(payload) != expected_fields
        or payload.get("schema") != ADMISSION_SCHEMA
        or payload.get("legacy_trial_spec_digest") != legacy_digest
    ):
        raise TrialContractAdmissionError("shared trial admission binding differs")
    try:
        spec = TrialSpec.from_payload(payload["trial_spec"])
        admitted_at = int(payload["admitted_at_unix_ns"])
    except (TypeError, ValueError) as exc:
        raise TrialContractAdmissionError(
            f"shared trial admission contract differs: {exc}"
        ) from exc
    if spec.trial_id != trial.trial_uid or admitted_at <= 0:
        raise TrialContractAdmissionError("shared TrialSpec identity differs")
    return spec, admitted_at


def _terminal_disposition(value: LegacyDisposition) -> TerminalDisposition:
    return {
        LegacyDisposition.OBJECTIVE: TerminalDisposition.SUCCEEDED,
        LegacyDisposition.SAFETY_STOP: TerminalDisposition.SAFETY_STOP,
        LegacyDisposition.PARAMETER_EVENT: TerminalDisposition.REJECTED,
        LegacyDisposition.OPERATOR_STOP: TerminalDisposition.REJECTED,
        LegacyDisposition.WAIT_INFRA_READY: TerminalDisposition.PROCESS_FAILURE,
        LegacyDisposition.CODE_CONTRACT_BUG: TerminalDisposition.PROCESS_FAILURE,
        LegacyDisposition.MANUAL_RECOVERY: TerminalDisposition.SAFETY_STOP,
        LegacyDisposition.FAIL_CLOSED: TerminalDisposition.REJECTED,
    }[value]


def _metrics(evaluation: Evaluation) -> dict[str, MetricValue]:
    values = {
        "objective_mae": (evaluation.objective_mae_n, "N"),
        "force_bias": (evaluation.force_bias_n, "N"),
        "force_std": (evaluation.force_std_n, "N"),
        "coverage_12_plus_minus_1": (
            evaluation.coverage_12_plus_minus_1_ratio,
            "1",
        ),
    }
    return {
        name: MetricValue(value=float(value), unit=unit)
        for name, (value, unit) in values.items()
        if isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    }


def build_trial_result(
    spec: TrialSpec,
    manifest: CaptureManifest,
    evaluation: Evaluation,
    *,
    admitted_at_unix_ns: int,
    completed_at_unix_ns: int,
    immutable_bundle: Path | None,
    runner_version: str,
) -> TrialResult:
    if evaluation.trial_uid != spec.trial_id or manifest.trial_uid != spec.trial_id:
        raise TrialContractAdmissionError("terminal evidence trial identity differs")
    observations = (
        ("completion_marker", manifest.completion_marker),
        ("feedback_fresh", manifest.feedback_fresh),
        ("safety_normal", manifest.safety_normal),
        ("returned_safe", manifest.returned_safe),
        ("fingerprint_closed", manifest.fingerprint_closed),
    )
    evidence_digest = manifest.terminal_manifest_sha256
    artifacts: tuple[ArtifactReference, ...] = ()
    if immutable_bundle is not None:
        if (
            immutable_bundle.is_symlink()
            or not immutable_bundle.is_file()
            or immutable_bundle.name != "immutable_trial_bundle.json"
        ):
            raise TrialContractAdmissionError("immutable bundle reference differs")
        encoded = immutable_bundle.read_bytes()
        artifacts = (
            ArtifactReference(
                role="immutable_trial_bundle",
                uri=(
                    "artifact://sha256/"
                    f"{hashlib.sha256(encoded).hexdigest()}/immutable_trial_bundle"
                ),
                sha256=hashlib.sha256(encoded).hexdigest(),
                size_bytes=len(encoded),
            ),
        )
    return TrialResult(
        trial_id=spec.trial_id,
        disposition=_terminal_disposition(evaluation.disposition),
        metrics=_metrics(evaluation),
        stop_reason=evaluation.disposition.value,
        started_at_unix_ns=admitted_at_unix_ns,
        completed_at_unix_ns=completed_at_unix_ns,
        safety_observations=tuple(
            SafetyObservation(
                name=name,
                passed=passed,
                observed_at_unix_ns=completed_at_unix_ns,
                evidence_sha256=evidence_digest,
            )
            for name, passed in observations
        ),
        artifacts=artifacts,
        runner_version=runner_version,
        trial_spec_digest=spec.digest,
    )


def persist_terminal_result(
    trial_dir: Path,
    trial: LegacyTrialSpec,
    manifest: CaptureManifest,
    evaluation: Evaluation,
    *,
    completed_at_unix_ns: int,
    immutable_bundle: Path | None,
) -> TrialResult:
    spec, admitted_at = load_trial_admission(trial_dir, trial)
    path = trial_dir / RESULT_NAME
    current = None
    if path.exists():
        try:
            current = TrialResult.from_payload(
                _strict_json(path, "terminal TrialResult")
            )
        except (TypeError, ValueError) as exc:
            raise TrialContractAdmissionError(
                f"terminal TrialResult differs: {exc}"
            ) from exc
        completed_at_unix_ns = current.completed_at_unix_ns
    proposed = build_trial_result(
        spec,
        manifest,
        evaluation,
        admitted_at_unix_ns=admitted_at,
        completed_at_unix_ns=completed_at_unix_ns,
        immutable_bundle=immutable_bundle,
        runner_version=f"{trial.backend_id}:{trial.source_fingerprint}",
    )
    accepted = accept_terminal_result(spec, current, proposed)
    _atomic_create(path, accepted.to_payload(), "terminal TrialResult")
    return accepted


__all__ = [
    "ADMISSION_NAME",
    "ADMISSION_SCHEMA",
    "DEFAULT_DEADLINE_WINDOW_NS",
    "RESULT_NAME",
    "TrialContractAdmissionError",
    "build_trial_result",
    "build_trial_spec",
    "load_trial_admission",
    "persist_terminal_result",
    "persist_trial_admission",
]
