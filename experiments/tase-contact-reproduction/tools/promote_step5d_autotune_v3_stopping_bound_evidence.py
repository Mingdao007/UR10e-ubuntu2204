#!/usr/bin/env python3
"""Promote exact attended no-contact stop telemetry into a live bound."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from build_step5d_autotune_v3_stopping_bound_evidence import SOURCE_PATHS
from step5d_autotune_v3.arming import load_bridge_start_context
from step5d_autotune_v3.profile import active_identity_snapshot
from ur10e_experiment_runtime.authorization import (
    CERTIFICATION_PROCEDURES,
    STEP5D_V3_STAGE_ID,
    load_certification_motion_authorization,
)
from ur10e_experiment_runtime.identity import canonical_sha256, load_strict_json
from ur10e_experiment_runtime.moving_sphere import (
    ATTENDED_STOPPING_MEASUREMENT_CONTRACT,
    STOPPING_BOUND_EVIDENCE_ROLES,
    STOPPING_BOUND_EVIDENCE_SCHEMA,
    STOPPING_BOUND_VALIDITY_DOMAIN,
    StoppingBoundArtifact,
    StoppingBoundEvidenceComponent,
    StoppingBoundEvidenceManifest,
)


ROOT = Path(__file__).resolve().parents[1]
RAW_SCHEMA = "step5d.autotune-v3/stopping-bound-measurement-v2"
STOP_PROCEDURES = CERTIFICATION_PROCEDURES[:2]
MINIMUM_SAMPLES_PER_PROCEDURE = 3
STATIONARY_SPEED_M_S = 0.001


def _regular(path: Path, role: str) -> Path:
    source = path.absolute()
    if not source.is_absolute() or source.is_symlink() or not source.is_file():
        raise ValueError(f"{role} must be an absolute regular file")
    return source


def _sha256_path(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(name: str, value: object, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise ValueError(f"{name} is outside its finite range")
    return result


def _strict_fields(value: object, fields: set[str], role: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"{role} fields differ")
    return value


def _interpolated_speed(
    samples: Sequence[tuple[float, float]], timestamp: float
) -> float:
    for left, right in zip(samples, samples[1:]):
        if left[0] <= timestamp <= right[0]:
            if timestamp == left[0]:
                return left[1]
            if timestamp == right[0]:
                return right[1]
            fraction = (timestamp - left[0]) / (right[0] - left[0])
            return left[1] + fraction * (right[1] - left[1])
    raise ValueError("stopping speed samples do not cover the declared interval")


def _clip_samples(
    samples: Sequence[tuple[float, float]], start: float, end: float
) -> list[tuple[float, float]]:
    clipped = [(start, _interpolated_speed(samples, start))]
    clipped.extend(item for item in samples if start < item[0] < end)
    clipped.append((end, _interpolated_speed(samples, end)))
    return clipped


def _analyze_trial(row: object, *, procedure: str, sample_index: int) -> tuple[float, float, float]:
    trial = _strict_fields(
        row,
        {
            "procedure",
            "sample_index",
            "contact_observed",
            "trigger_controller_timestamp_s",
            "stop_transport_controller_timestamp_s",
            "stationary_controller_timestamp_s",
            "speed_samples",
        },
        "stopping measurement trial",
    )
    if trial["procedure"] != procedure or trial["sample_index"] != sample_index:
        raise ValueError("stopping measurement procedure/index differs")
    if trial["contact_observed"] is not False:
        raise ValueError("stopping certification must remain no-contact")
    trigger = _number("trigger timestamp", trial["trigger_controller_timestamp_s"])
    stop = _number("stop transport timestamp", trial["stop_transport_controller_timestamp_s"])
    stationary = _number("stationary timestamp", trial["stationary_controller_timestamp_s"])
    if not trigger < stop < stationary:
        raise ValueError("stopping measurement timestamps are not ordered")
    raw_samples = trial["speed_samples"]
    if not isinstance(raw_samples, Sequence) or isinstance(raw_samples, (str, bytes)):
        raise ValueError("speed_samples must be an array")
    if len(raw_samples) < 4:
        raise ValueError("stopping measurement needs at least four speed samples")
    samples: list[tuple[float, float]] = []
    for sample in raw_samples:
        item = _strict_fields(
            sample,
            {"controller_timestamp_s", "tcp_speed_m_s"},
            "stopping speed sample",
        )
        samples.append(
            (
                _number("sample timestamp", item["controller_timestamp_s"]),
                _number("sample speed", item["tcp_speed_m_s"]),
            )
        )
    if samples[0][0] > trigger or samples[-1][0] < stationary:
        raise ValueError("stopping speed samples do not cover the declared interval")
    if any(right[0] <= left[0] for left, right in zip(samples, samples[1:])):
        raise ValueError("stopping speed sample timestamps are not strictly increasing")
    samples = _clip_samples(samples, trigger, stationary)
    stop_indexes = [index for index, item in enumerate(samples) if item[0] == stop]
    if not stop_indexes:
        samples.append((stop, _interpolated_speed(samples, stop)))
        samples.sort()
        stop_indexes = [index for index, item in enumerate(samples) if item[0] == stop]
    if len(stop_indexes) != 1:
        raise ValueError("stop transport timestamp is ambiguous in samples")
    stop_index = stop_indexes[0]
    if stop_index < 1 or stop_index >= len(samples) - 1:
        raise ValueError("stop transport sample lacks pre/post observations")
    growth = 0.0
    for left, right in zip(samples[: stop_index + 1], samples[1 : stop_index + 1]):
        growth = max(growth, (right[1] - left[1]) / (right[0] - left[0]))
    decelerations: list[float] = []
    for left, right in zip(samples[stop_index:], samples[stop_index + 1 :]):
        if left[1] > STATIONARY_SPEED_M_S:
            deceleration = (left[1] - right[1]) / (right[0] - left[0])
            if deceleration <= 0.0:
                raise ValueError("post-stop speed is not conservatively decreasing")
            decelerations.append(deceleration)
    if not decelerations or samples[-1][1] > STATIONARY_SPEED_M_S:
        raise ValueError("stopping measurement does not reach certified stillness")
    return stop - trigger, max(0.0, growth), min(decelerations)


def _analyze_measurement(
    payload: object,
    *,
    expected_release_basis_fingerprint: str,
    expected_deployment_fingerprint: str,
    expected_plant_epoch: int,
    expected_deployment_readback_sha256: str,
    expected_authorization_sha256: str,
) -> tuple[float, float, float]:
    measurement = _strict_fields(
        payload,
        {
            "schema",
            "candidate_stage_id",
            "release_basis_fingerprint",
            "deployment_fingerprint",
            "plant_epoch",
            "deployment_readback_sha256",
            "certification_authorization_sha256",
            "all_samples_retained",
            "trials",
        },
        "stopping measurement",
    )
    expected = {
        "schema": RAW_SCHEMA,
        "candidate_stage_id": STEP5D_V3_STAGE_ID,
        "release_basis_fingerprint": expected_release_basis_fingerprint,
        "deployment_fingerprint": expected_deployment_fingerprint,
        "plant_epoch": expected_plant_epoch,
        "deployment_readback_sha256": expected_deployment_readback_sha256,
        "certification_authorization_sha256": expected_authorization_sha256,
        "all_samples_retained": True,
    }
    for field, value in expected.items():
        if measurement[field] != value:
            raise ValueError(f"stopping measurement {field} differs")
    trials = measurement["trials"]
    if not isinstance(trials, Sequence) or isinstance(trials, (str, bytes)):
        raise ValueError("stopping measurement trials must be an array")
    grouped = {procedure: [] for procedure in STOP_PROCEDURES}
    for row in trials:
        if not isinstance(row, Mapping) or row.get("procedure") not in grouped:
            raise ValueError("stopping measurement contains an unknown procedure")
        grouped[str(row["procedure"])].append(row)
    if any(len(rows) < MINIMUM_SAMPLES_PER_PROCEDURE for rows in grouped.values()):
        raise ValueError("both exact-stop paths require at least three samples")
    analyzed: list[tuple[float, float, float]] = []
    for procedure in STOP_PROCEDURES:
        for index, row in enumerate(grouped[procedure], start=1):
            analyzed.append(_analyze_trial(row, procedure=procedure, sample_index=index))
    return (
        max(item[0] for item in analyzed),
        max(item[1] for item in analyzed),
        min(item[2] for item in analyzed),
    )


def _atomic_new(path: Path, payload: Mapping[str, object]) -> None:
    output = path.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("promoted stopping-bound output must be fresh")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def promote(
    *,
    measurement_path: Path,
    bridge_start_context_path: Path,
    authorization_path: Path,
    deployment_readback_path: Path,
    output_path: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    measurement_source = _regular(measurement_path, "stopping measurement")
    bridge_context_source = _regular(
        bridge_start_context_path, "bridge-start context"
    )
    authorization_source = _regular(authorization_path, "certification authorization")
    readback_source = _regular(deployment_readback_path, "deployment readback")
    readback_sha256 = _sha256_path(readback_source)
    bridge_context = load_bridge_start_context(
        bridge_context_source,
        expected_static_identity=active_identity_snapshot(),
        expected_deployment_readback_sha256=readback_sha256,
    )
    authorization = load_certification_motion_authorization(
        authorization_source,
        expected_release_basis_fingerprint=(
            bridge_context.release_basis_fingerprint
        ),
        expected_deployment_fingerprint=bridge_context.deployment_fingerprint,
        expected_plant_epoch=bridge_context.plant_epoch,
        expected_deployment_readback_sha256=readback_sha256,
        now=now,
    )
    authorization_sha256 = authorization.authorization_ref_sha256
    for procedure in STOP_PROCEDURES:
        authorization.require_procedure(procedure, now=now)
    measurement = load_strict_json(measurement_source)
    reaction_latency_s, acceleration_growth_m_s2, minimum_deceleration_m_s2 = (
        _analyze_measurement(
            measurement,
            expected_release_basis_fingerprint=(
                bridge_context.release_basis_fingerprint
            ),
            expected_deployment_fingerprint=(
                bridge_context.deployment_fingerprint
            ),
            expected_plant_epoch=bridge_context.plant_epoch,
            expected_deployment_readback_sha256=readback_sha256,
            expected_authorization_sha256=authorization_sha256,
        )
    )
    measurement_sha256 = _sha256_path(measurement_source)
    source_sha256 = {name: _sha256_path(path) for name, path in SOURCE_PATHS.items()}
    source_binding_sha256 = canonical_sha256(source_sha256)
    certification_binding_sha256 = canonical_sha256(
        {
            "schema": "ur-exp/step5d-stopping-certification-binding-v1",
            "validity_domain": STOPPING_BOUND_VALIDITY_DOMAIN,
            "source_binding_sha256": source_binding_sha256,
            "stop_transport_sha256": source_sha256["bridge"],
            "deployment_readback_sha256": readback_sha256,
            "certification_authorization_sha256": authorization_sha256,
            "plant_epoch": bridge_context.plant_epoch,
            "measurement_sha256": measurement_sha256,
        }
    )
    values = (
        reaction_latency_s,
        acceleration_growth_m_s2,
        minimum_deceleration_m_s2,
        0.003,
        0.00015,
        authorization.numeric_margin_m,
    )
    units = ("s", "m/s^2", "m/s^2", "m/s", "m/s^2", "m")
    methods = (
        "max_trigger_to_exact_stop_transport_latency_all_retained_trials",
        "max_pre_transport_adjacent_speed_growth_all_retained_trials",
        "min_post_transport_adjacent_deceleration_all_retained_trials",
        "analytic_cycloid_bound_2_amplitude_times_omega",
        "analytic_cycloid_bound_amplitude_times_omega_squared",
        "authorization_preregistered_numeric_margin",
    )
    components = tuple(
        StoppingBoundEvidenceComponent(
            role=role,
            status="certified_for_domain",
            value=value,
            units=unit,
            frame="base",
            method=method,
            evidence_sha256=(
                (source_sha256["stage_adapter"],)
                if role.startswith("center_")
                else (
                    (authorization_sha256,)
                    if role == "numeric_margin_m"
                    else (measurement_sha256, authorization_sha256)
                )
            ),
        )
        for role, value, unit, method in zip(
            STOPPING_BOUND_EVIDENCE_ROLES, values, units, methods, strict=True
        )
    )
    manifest = StoppingBoundEvidenceManifest(
        components=components,
        validity_domain=STOPPING_BOUND_VALIDITY_DOMAIN,
        source_binding_sha256=source_binding_sha256,
        stop_transport_sha256=source_sha256["bridge"],
        deployment_readback_sha256=readback_sha256,
        certification_binding_sha256=certification_binding_sha256,
    )
    artifact = StoppingBoundArtifact(
        reaction_latency_s=reaction_latency_s,
        acceleration_growth_m_s2=acceleration_growth_m_s2,
        minimum_deceleration_m_s2=minimum_deceleration_m_s2,
        center_speed_bound_m_s=0.003,
        center_acceleration_bound_m_s2=0.00015,
        numeric_margin_m=authorization.numeric_margin_m,
        evidence_manifest=manifest,
    )
    document = {
        "schema": STOPPING_BOUND_EVIDENCE_SCHEMA,
        "status": "certified_attended_measurement",
        "certified": True,
        "optimizer_eligible": False,
        "manifest_fingerprint": manifest.fingerprint,
        "stopping_bound_fingerprint": artifact.fingerprint,
        "validity_domain": STOPPING_BOUND_VALIDITY_DOMAIN,
        "source_binding_sha256": source_binding_sha256,
        "stop_transport_sha256": source_sha256["bridge"],
        "deployment_readback_sha256": readback_sha256,
        "certification_authorization_sha256": authorization_sha256,
        "certification_binding_sha256": certification_binding_sha256,
        "plant_epoch": bridge_context.plant_epoch,
        "measurement_sha256": measurement_sha256,
        "source_sha256": source_sha256,
        "components": [component.document() for component in components],
        "attended_measurement_contract": ATTENDED_STOPPING_MEASUREMENT_CONTRACT,
        "live_effect": "certified_bound_available_for_explicit_runtime_load",
    }
    _atomic_new(output_path, document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurement", type=Path, required=True)
    parser.add_argument("--bridge-start-context", type=Path, required=True)
    parser.add_argument("--certification-authorization", type=Path, required=True)
    parser.add_argument("--deployment-readback", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    promote(
        measurement_path=args.measurement,
        bridge_start_context_path=args.bridge_start_context,
        authorization_path=args.certification_authorization,
        deployment_readback_path=args.deployment_readback,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
