#!/usr/bin/env python3
"""Promote isolated URSim plus attended return telemetry into V3 evidence."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping

import build_step5d_autotune_v3_return_route_evidence as baseline
from step5d_autotune_v3.profile import control_fingerprint, load_contract
from step5d_autotune_v3.state import orchestration_fingerprint
from ur10e_experiment_runtime.authorization import (
    load_certification_motion_authorization,
)
from ur10e_experiment_runtime.identity import canonical_sha256, load_strict_json
from ur10e_experiment_runtime.return_route import (
    RETURN_ROUTE_EVIDENCE_SCHEMA,
    analyze_source_exact_return_telemetry,
    validate_motion_capable_ursim_return_trace,
)


ROOT = Path(__file__).resolve().parents[1]


def _regular(path: Path, role: str) -> Path:
    source = path.absolute()
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"{role} must be an absolute regular file")
    return source


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_new(path: Path, payload: Mapping[str, object]) -> None:
    output = path.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("promoted return-route output must be fresh")
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
    ursim_trace_path: Path,
    telemetry_path: Path,
    authorization_path: Path,
    deployment_readback_path: Path,
    plant_epoch: int,
    output_path: Path,
    now: datetime | None = None,
) -> dict[str, object]:
    ursim_source = _regular(ursim_trace_path, "URSim return trace")
    telemetry_source = _regular(telemetry_path, "return telemetry")
    authorization_source = _regular(
        authorization_path, "certification authorization"
    )
    readback_source = _regular(deployment_readback_path, "deployment readback")
    if isinstance(plant_epoch, bool) or not isinstance(plant_epoch, int) or plant_epoch < 1:
        raise ValueError("plant_epoch must be positive")
    control = control_fingerprint(load_contract())
    orchestration = orchestration_fingerprint(ROOT)
    readback_sha256 = _sha256(readback_source)
    authorization = load_certification_motion_authorization(
        authorization_source,
        expected_control_fingerprint=control,
        expected_orchestration_fingerprint=orchestration,
        expected_plant_epoch=plant_epoch,
        expected_deployment_readback_sha256=readback_sha256,
        now=now,
    )
    authorization_sha256 = authorization.authorization_ref_sha256
    authorization.require_procedure("return_route", now=now)
    base = baseline.build_document(include_ursim_trace=False)
    source_binding_sha256 = str(base["source_binding_sha256"])
    triplet_sha256 = dict(base["local_triplet_sha256"])
    validate_motion_capable_ursim_return_trace(
        load_strict_json(ursim_source),
        expected_control_fingerprint=control,
        expected_orchestration_fingerprint=orchestration,
        expected_source_binding_sha256=source_binding_sha256,
        expected_triplet_sha256=triplet_sha256,
    )
    telemetry_summary = analyze_source_exact_return_telemetry(
        load_strict_json(telemetry_source),
        expected_control_fingerprint=control,
        expected_orchestration_fingerprint=orchestration,
        expected_source_binding_sha256=source_binding_sha256,
        expected_triplet_sha256=triplet_sha256,
        expected_plant_epoch=plant_epoch,
        expected_deployment_readback_sha256=readback_sha256,
        expected_certification_authorization_sha256=authorization_sha256,
    )
    ursim_sha256 = _sha256(ursim_source)
    telemetry_sha256 = _sha256(telemetry_source)
    controller_readback_sha256 = str(
        telemetry_summary["controller_readback_sha256"]
    )
    certification_binding_sha256 = canonical_sha256(
        {
            "schema": "ur-exp/step5d-return-route-certification-binding-v1",
            "source_binding_sha256": source_binding_sha256,
            "triplet_sha256": triplet_sha256,
            "deployment_readback_sha256": readback_sha256,
            "certification_authorization_sha256": authorization_sha256,
            "plant_epoch": plant_epoch,
            "motion_capable_ursim_trace_sha256": ursim_sha256,
            "attended_controller_readback_sha256": controller_readback_sha256,
            "source_exact_return_telemetry_sha256": telemetry_sha256,
        }
    )
    document = {
        **base,
        "schema": RETURN_ROUTE_EVIDENCE_SCHEMA,
        "status": "certified_attended_return_measurement",
        "certified": True,
        "optimizer_eligible": False,
        "certification_authorization_sha256": authorization_sha256,
        "certification_binding_sha256": certification_binding_sha256,
        "plant_epoch": plant_epoch,
        "deployment_readback_sha256": readback_sha256,
        "motion_capable_ursim_trace_sha256": ursim_sha256,
        "attended_controller_readback_sha256": controller_readback_sha256,
        "source_exact_return_telemetry_sha256": telemetry_sha256,
        "telemetry_summary": telemetry_summary,
        "live_effect": "return_route_angular_envelope_certified_for_exact_epoch",
    }
    document.pop("missing_certification", None)
    _atomic_new(output_path, document)
    return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ursim-trace", type=Path, required=True)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--certification-authorization", type=Path, required=True)
    parser.add_argument("--deployment-readback", type=Path, required=True)
    parser.add_argument("--plant-epoch", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    promote(
        ursim_trace_path=args.ursim_trace,
        telemetry_path=args.telemetry,
        authorization_path=args.certification_authorization,
        deployment_readback_path=args.deployment_readback,
        plant_epoch=args.plant_epoch,
        output_path=args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
