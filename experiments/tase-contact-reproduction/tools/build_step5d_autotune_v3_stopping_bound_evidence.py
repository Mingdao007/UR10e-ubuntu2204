#!/usr/bin/env python3
"""Build the fail-closed Step5d V3 stopping-bound evidence manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ur10e_experiment_runtime.identity import canonical_sha256
from ur10e_experiment_runtime.moving_sphere import (
    ATTENDED_STOPPING_MEASUREMENT_CONTRACT,
    STOPPING_BOUND_EVIDENCE_ROLES,
    STOPPING_BOUND_EVIDENCE_SCHEMA,
    STOPPING_BOUND_VALIDITY_DOMAIN,
    StoppingBoundEvidenceComponent,
    StoppingBoundEvidenceManifest,
)


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = EXPERIMENT_ROOT.parents[1]
DEFAULT_OUTPUT = (
    EXPERIMENT_ROOT
    / "config/step5/step5d_autotune_v3_stopping_bound_evidence.json"
)
SOURCE_PATHS = {
    "bridge": EXPERIMENT_ROOT / "tools/kunwei_rtde_bridge.py",
    "moving_sphere": (
        REPOSITORY_ROOT
        / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/moving_sphere.py"
    ),
    "stage_adapter": (
        REPOSITORY_ROOT
        / "src/ur10e_experiment_runtime/ur10e_experiment_runtime/stage_adapters.py"
    ),
}


def _sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"stopping-bound source is missing or symlinked: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_document() -> dict[str, object]:
    source_sha256 = {name: _sha256(path) for name, path in SOURCE_PATHS.items()}
    center_evidence = (source_sha256["stage_adapter"],)
    missing = {
        "reaction_latency_s": ("s", "attended_exact_stop_latency_upper_bound"),
        "acceleration_growth_m_s2": (
            "m/s^2",
            "attended_exact_stop_speed_growth_upper_bound",
        ),
        "minimum_deceleration_m_s2": (
            "m/s^2",
            "attended_exact_stop_minimum_deceleration_lower_bound",
        ),
        "numeric_margin_m": ("m", "preregistered_numeric_margin"),
    }
    certified = {
        "center_speed_bound_m_s": (
            0.003,
            "m/s",
            "analytic_cycloid_bound_2_amplitude_times_omega",
        ),
        "center_acceleration_bound_m_s2": (
            0.00015,
            "m/s^2",
            "analytic_cycloid_bound_amplitude_times_omega_squared",
        ),
    }
    components: list[StoppingBoundEvidenceComponent] = []
    for role in STOPPING_BOUND_EVIDENCE_ROLES:
        if role in certified:
            value, units, method = certified[role]
            components.append(
                StoppingBoundEvidenceComponent(
                    role=role,
                    status="certified_for_domain",
                    value=value,
                    units=units,
                    frame="base",
                    method=method,
                    evidence_sha256=center_evidence,
                )
            )
        else:
            units, method = missing[role]
            components.append(
                StoppingBoundEvidenceComponent(
                    role=role,
                    status="missing",
                    value=None,
                    units=units,
                    frame="base",
                    method=method,
                    evidence_sha256=(),
                )
            )
    source_binding_sha256 = canonical_sha256(source_sha256)
    manifest = StoppingBoundEvidenceManifest(
        components=tuple(components),
        validity_domain=STOPPING_BOUND_VALIDITY_DOMAIN,
        source_binding_sha256=source_binding_sha256,
        stop_transport_sha256=source_sha256["bridge"],
        deployment_readback_sha256=None,
        certification_binding_sha256=None,
    )
    return {
        "schema": STOPPING_BOUND_EVIDENCE_SCHEMA,
        "status": "incomplete_attended_measurement_required",
        "certified": manifest.certified,
        "optimizer_eligible": False,
        "manifest_fingerprint": manifest.fingerprint,
        "stopping_bound_fingerprint": None,
        "validity_domain": manifest.validity_domain,
        "source_binding_sha256": manifest.source_binding_sha256,
        "stop_transport_sha256": manifest.stop_transport_sha256,
        "deployment_readback_sha256": manifest.deployment_readback_sha256,
        "certification_authorization_sha256": None,
        "certification_binding_sha256": None,
        "plant_epoch": None,
        "measurement_sha256": None,
        "source_sha256": source_sha256,
        "components": [component.document() for component in manifest.components],
        "attended_measurement_contract": ATTENDED_STOPPING_MEASUREMENT_CONTRACT,
        "live_effect": "stopping_bound_none_fail_closed",
    }


def _encoded() -> bytes:
    return (json.dumps(build_document(), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    expected = _encoded()
    output = args.output.resolve()
    if args.check:
        if output.is_symlink() or not output.is_file() or output.read_bytes() != expected:
            raise SystemExit("stopping-bound evidence artifact is missing or stale")
        return 0
    _write_atomic(output, expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
