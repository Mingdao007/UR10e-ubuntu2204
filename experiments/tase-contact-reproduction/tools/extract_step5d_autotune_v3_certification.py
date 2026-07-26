#!/usr/bin/env python3
"""Extract strict V3 stopping/return inputs from one source-exact bridge CSV."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

import build_step5d_autotune_v3_return_route_evidence as return_baseline
from promote_step5d_autotune_v3_stopping_bound_evidence import RAW_SCHEMA
from step5d_autotune_v3.arming import load_bridge_start_context
from step5d_autotune_v3.certification import (
    EXECUTION_PROFILE_ID,
    CertificationStep,
    certification_steps,
)
from step5d_autotune_v3.profile import active_identity_snapshot
from ur10e_experiment_runtime.authorization import (
    load_certification_motion_authorization,
)
from ur10e_experiment_runtime.return_route import RETURN_TELEMETRY_SCHEMA


ROOT = Path(__file__).resolve().parents[1]
NO_CONTACT_NORMAL_LIMIT_N = 0.75
NO_CONTACT_FORCE_NORM_LIMIT_N = 2.0
NO_CONTACT_TORQUE_NORM_LIMIT_NM = 0.5


class CertificationExtractionError(RuntimeError):
    """The retained bridge capture cannot prove a no-contact procedure."""


def _regular(path: Path, role: str) -> Path:
    source = path.expanduser().absolute()
    if source.is_symlink() or not source.is_file():
        raise CertificationExtractionError(f"{role} must be an absolute regular file")
    return source


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _number(row: Mapping[str, Any], name: str) -> float:
    try:
        value = float(row[name])
    except (KeyError, TypeError, ValueError) as exc:
        raise CertificationExtractionError(f"capture field {name} is missing") from exc
    if not math.isfinite(value):
        raise CertificationExtractionError(f"capture field {name} is nonfinite")
    return value


def _integer(row: Mapping[str, Any], name: str) -> int:
    value = _number(row, name)
    parsed = int(value)
    if value != parsed:
        raise CertificationExtractionError(f"capture field {name} is not integral")
    return parsed


def _optional_integer(row: Mapping[str, Any], name: str) -> int | None:
    raw = row.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    return _integer(row, name)


def _load_rows(path: Path) -> tuple[dict[str, str], ...]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "ur_timestamp",
            "normal_force_n",
            "force_norm_n",
            "torque_norm_nm",
            "sensor_ok",
            "guard_reason",
            "command",
            "campaign_epoch",
            "trial_id",
            "execution_profile_id",
            "_step5d_certification_trigger_controller_timestamp_s",
            *(f"ur_actual_TCP_speed_{index}" for index in range(3)),
            *(f"ur_output_int_register_{index}" for index in range(24, 34)),
            *(f"ur_output_double_register_{index}" for index in range(35, 48)),
            "ur_safety_mode",
        }
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            missing = sorted(required - set(reader.fieldnames or ()))
            raise CertificationExtractionError(
                f"bridge capture schema lacks certification fields: {missing}"
            )
        return tuple(dict(row) for row in reader)


def _step_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    step: CertificationStep,
    plant_epoch: int,
) -> tuple[Mapping[str, Any], ...]:
    selected = tuple(
        row
        for row in rows
        if _optional_integer(row, "campaign_epoch") == plant_epoch
        and _optional_integer(row, "trial_id") == step.trial_id
        and _optional_integer(row, "execution_profile_id") == EXECUTION_PROFILE_ID
        and _optional_integer(row, "command") == step.command
        and _optional_integer(row, "ur_output_int_register_26")
        in ({80, 81, 82, 85} if step.command in {4, 5} else {83, 84, 85})
    )
    if not selected:
        raise CertificationExtractionError(
            f"capture lacks {step.procedure} sample {step.sample_index}"
        )
    states = {_integer(row, "ur_output_int_register_26") for row in selected}
    required_states = {80, 81, 82, 85} if step.command in {4, 5} else {83, 84, 85}
    if not required_states.issubset(states):
        raise CertificationExtractionError(
            f"{step.procedure} sample {step.sample_index} lacks safe state closure"
        )
    for row in selected:
        if (
            _integer(row, "ur_output_int_register_24") != plant_epoch
            or _integer(row, "ur_output_int_register_25") != step.trial_id
            or _integer(row, "ur_output_int_register_27") != step.candidate_token
            or _integer(row, "ur_output_int_register_29") != EXECUTION_PROFILE_ID
            or _integer(row, "ur_output_int_register_31") != step.sample_index
            or _integer(row, "ur_output_int_register_32") != 3
        ):
            raise CertificationExtractionError("capture certification echo differs")
        if (
            _integer(row, "sensor_ok") != 1
            or _integer(row, "ur_safety_mode") != 1
            or str(row.get("guard_reason", "")).strip()
            or abs(_number(row, "normal_force_n")) > NO_CONTACT_NORMAL_LIMIT_N
            or _number(row, "force_norm_n") > NO_CONTACT_FORCE_NORM_LIMIT_N
            or _number(row, "torque_norm_nm") > NO_CONTACT_TORQUE_NORM_LIMIT_NM
        ):
            raise CertificationExtractionError(
                f"{step.procedure} sample {step.sample_index} is not no-contact/NORMAL"
            )
    return tuple(sorted(selected, key=lambda row: _number(row, "ur_timestamp")))


def _tcp_speed(row: Mapping[str, Any]) -> float:
    xyz = tuple(_number(row, f"ur_actual_TCP_speed_{index}") for index in range(3))
    return sum(value * value for value in xyz) ** 0.5


def _stop_trial(
    rows: Sequence[Mapping[str, Any]],
    *,
    step: CertificationStep,
) -> dict[str, Any]:
    host_triggers = {
        _number(row, "_step5d_certification_trigger_controller_timestamp_s")
        for row in rows
        if str(
            row.get("_step5d_certification_trigger_controller_timestamp_s", "")
        ).strip()
    }
    if len(host_triggers) != 1:
        raise CertificationExtractionError(
            "stopping capture lacks one exact host trigger timestamp"
        )
    trigger = host_triggers.pop()
    tp_trigger = max(
        _number(row, "ur_output_double_register_45") for row in rows
    )
    stop = max(_number(row, "ur_output_double_register_46") for row in rows)
    stationary = max(_number(row, "ur_output_double_register_47") for row in rows)
    if not 0.0 < trigger <= tp_trigger < stop < stationary:
        raise CertificationExtractionError("stopping timestamps are incomplete")
    samples = [
        {
            "controller_timestamp_s": _number(row, "ur_timestamp"),
            "tcp_speed_m_s": _tcp_speed(row),
        }
        for row in rows
        if trigger - 0.010
        <= _number(row, "ur_timestamp")
        <= stationary + 0.010
    ]
    if (
        len(samples) < 4
        or samples[0]["controller_timestamp_s"] > trigger
        or samples[-1]["controller_timestamp_s"] < stationary
    ):
        raise CertificationExtractionError("stopping samples do not cover timestamps")
    return {
        "procedure": step.procedure,
        "sample_index": step.sample_index,
        "contact_observed": False,
        "trigger_controller_timestamp_s": trigger,
        "stop_transport_controller_timestamp_s": stop,
        "stationary_controller_timestamp_s": stationary,
        "speed_samples": samples,
    }


def _return_telemetry_rows(
    rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for row in rows:
        state = _integer(row, "ur_output_int_register_26")
        segment = _integer(row, "ur_output_double_register_39")
        gap = _number(row, "ur_output_double_register_44")
        if state not in {84, 85} or segment not in {1, 2, 3} or gap <= 0.0:
            continue
        samples.append(
            {
                "controller_timestamp_s": _number(row, "ur_timestamp"),
                "return_phase_echo": _number(row, "ur_output_double_register_35"),
                "return_segment_id": segment,
                "angular_speed_rad_s": _number(
                    row, "ur_output_double_register_40"
                ),
                "angular_acceleration_rad_s2": _number(
                    row, "ur_output_double_register_41"
                ),
                "max_angular_speed_rad_s": _number(
                    row, "ur_output_double_register_42"
                ),
                "max_angular_acceleration_rad_s2": _number(
                    row, "ur_output_double_register_43"
                ),
                "max_sample_gap_s": gap,
                "guard_reason": 0,
                "safety_mode": "NORMAL",
            }
        )
    if len(samples) < 20:
        raise CertificationExtractionError("return telemetry sample count is insufficient")
    return samples


def extract(
    *,
    csv_path: Path,
    bridge_start_context_path: Path,
    authorization_path: Path,
    deployment_readback_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = _regular(csv_path, "bridge certification CSV")
    readback = _regular(deployment_readback_path, "deployment readback")
    readback_sha = _sha256(readback)
    context = load_bridge_start_context(
        _regular(bridge_start_context_path, "bridge-start context"),
        expected_static_identity=active_identity_snapshot(),
        expected_deployment_readback_sha256=readback_sha,
    )
    authorization = load_certification_motion_authorization(
        _regular(authorization_path, "certification authorization"),
        expected_release_basis_fingerprint=context.release_basis_fingerprint,
        expected_deployment_fingerprint=context.deployment_fingerprint,
        expected_plant_epoch=context.plant_epoch,
        expected_deployment_readback_sha256=readback_sha,
    )
    rows = _load_rows(source)
    grouped = [
        (step, _step_rows(rows, step=step, plant_epoch=context.plant_epoch))
        for step in certification_steps(authorization)
    ]
    stop_trials = [
        _stop_trial(step_rows, step=step)
        for step, step_rows in grouped
        if step.command in {4, 5}
    ]
    return_step, return_rows = grouped[-1]
    if return_step.command != 6:
        raise CertificationExtractionError("fixed certification sequence differs")
    return_samples = _return_telemetry_rows(return_rows)
    final_rows = [
        row
        for row in return_rows
        if _integer(row, "ur_output_int_register_26") == 85
    ]
    if not final_rows:
        raise CertificationExtractionError("return route lacks final safe closure")
    final_row = final_rows[-1]
    base = return_baseline.build_document(include_ursim_trace=False)
    authorization_sha = authorization.authorization_ref_sha256
    stopping = {
        "schema": RAW_SCHEMA,
        "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
        "release_basis_fingerprint": context.release_basis_fingerprint,
        "deployment_fingerprint": context.deployment_fingerprint,
        "plant_epoch": context.plant_epoch,
        "deployment_readback_sha256": readback_sha,
        "certification_authorization_sha256": authorization_sha,
        "all_samples_retained": True,
        "trials": stop_trials,
    }
    telemetry = {
        "schema": RETURN_TELEMETRY_SCHEMA,
        "candidate_stage_id": "step5d_strict_rnn_autotune_v3",
        "release_basis_fingerprint": context.release_basis_fingerprint,
        "deployment_fingerprint": context.deployment_fingerprint,
        "source_binding_sha256": base["source_binding_sha256"],
        "triplet_sha256": base["local_triplet_sha256"],
        "plant_epoch": context.plant_epoch,
        "deployment_readback_sha256": readback_sha,
        "certification_authorization_sha256": authorization_sha,
        "all_samples_retained": True,
        "no_contact": True,
        "samples": return_samples,
        "final_readback": {
            "completed": _integer(final_row, "ur_output_int_register_26") == 85,
            "return_phase_echo": _number(
                final_row, "ur_output_double_register_35"
            ),
            "return_segment_id": _integer(
                final_row, "ur_output_double_register_39"
            ),
            "return_guard_mask": _integer(
                final_row, "ur_output_int_register_33"
            ),
            "max_angular_speed_rad_s": _number(
                final_row, "ur_output_double_register_42"
            ),
            "max_angular_acceleration_rad_s2": _number(
                final_row, "ur_output_double_register_43"
            ),
            "max_sample_gap_s": _number(
                final_row, "ur_output_double_register_44"
            ),
            "safety_mode": "NORMAL",
        },
    }
    return stopping, telemetry


def _write_once(path: Path, payload: Mapping[str, Any]) -> None:
    destination = path.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise CertificationExtractionError("certification output must be fresh")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--bridge-start-context", type=Path, required=True)
    parser.add_argument("--certification-authorization", type=Path, required=True)
    parser.add_argument("--deployment-readback", type=Path, required=True)
    parser.add_argument("--stopping-output", type=Path, required=True)
    parser.add_argument("--return-output", type=Path, required=True)
    args = parser.parse_args(argv)
    stopping, telemetry = extract(
        csv_path=args.csv,
        bridge_start_context_path=args.bridge_start_context,
        authorization_path=args.certification_authorization,
        deployment_readback_path=args.deployment_readback,
    )
    _write_once(args.stopping_output, stopping)
    _write_once(args.return_output, telemetry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
