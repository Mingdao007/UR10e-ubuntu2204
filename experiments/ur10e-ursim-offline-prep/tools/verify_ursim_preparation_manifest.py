#!/usr/bin/env python3
"""Verify a URSim preparation manifest without promoting protocol claims."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

import build_ursim_preparation_manifest as builder


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_manifest_shape(payload: Mapping[str, Any]) -> None:
    if payload.get("schema") != builder.SCHEMA:
        raise ValueError("unsupported URSim preparation manifest schema")
    if payload.get("status") != "blocked" or payload.get("availability") != "unavailable":
        raise ValueError("preparation manifest must remain blocked/unavailable")
    if payload.get("protocol_execution_performed") is not False:
        raise ValueError("static preparation cannot claim protocol execution")
    if not SHA256_RE.fullmatch(str(payload.get("spec_sha256", ""))):
        raise ValueError("invalid lane spec hash")
    environment = payload.get("runtime_environment")
    if not isinstance(environment, Mapping):
        raise ValueError("runtime environment is missing")
    for key, expected in builder.RUNTIME_ENVIRONMENT_REQUIRED.items():
        if environment.get(key) != expected:
            raise ValueError(f"runtime environment gate drifted: {key}")
    if not str(environment.get("probe_source") or ""):
        raise ValueError("runtime environment probe source is missing")
    claims = payload.get("claims")
    if claims != {
        "p0_ursim_protocol_pass": False,
        "direct_torque_ursim_software_pass": False,
    }:
        raise ValueError("URSim protocol claims must remain false")
    for boundary in (
        "live_motion_authorized",
        "package_accepted",
        "live_accepted",
        "reproduction_complete",
    ):
        if payload.get(boundary) is not False:
            raise ValueError(f"preparation manifest crossed claim boundary: {boundary}")
    lanes = payload.get("lanes")
    if not isinstance(lanes, list) or tuple(lane.get("id") for lane in lanes) != builder.LANE_IDS:
        raise ValueError("URSim preparation lane identity drifted")
    for lane in lanes:
        if (
            lane.get("status") != "blocked"
            or lane.get("runtime_status") != "unavailable"
            or lane.get("protocol_pass") is not False
        ):
            raise ValueError(f"lane must remain blocked/unavailable: {lane.get('id')}")
        image = lane.get("image")
        if not isinstance(image, Mapping) or image != {
            "digest_required": True,
            "digest": None,
            "reference": None,
            "availability": builder.IMAGE_AVAILABILITY,
        }:
            raise ValueError(f"unverified digest-bound image gate drifted: {lane.get('id')}")
        blockers = set(lane.get("blockers", ()))
        required_blockers = set(builder.RUNTIME_BLOCKERS)
        if blockers != required_blockers:
            raise ValueError(f"lane blockers drifted: {lane.get('id')}")
        static_contract = lane.get("static_contract")
        if not isinstance(static_contract, Mapping) or not static_contract:
            raise ValueError(f"static contract is missing: {lane.get('id')}")
        if not all(value is True for value in static_contract.values()):
            raise ValueError(f"static contract is not fully verified: {lane.get('id')}")
        runtime_checks = lane.get("required_runtime_checks")
        if not isinstance(runtime_checks, list) or not runtime_checks:
            raise ValueError(f"runtime checks are missing: {lane.get('id')}")
        if set(runtime_checks) & set(static_contract):
            raise ValueError(
                f"static checks must not masquerade as runtime evidence: {lane.get('id')}"
            )
        for binding in lane.get("bindings", ()):
            if binding.get("verified") is not True or not SHA256_RE.fullmatch(
                str(binding.get("sha256", ""))
            ):
                raise ValueError(f"invalid immutable binding: {lane.get('id')}")


def verify_manifest(
    payload: Mapping[str, Any],
    spec_path: Path,
    root: Path | None = None,
) -> dict[str, Any]:
    validate_manifest_shape(payload)
    expected = builder.build_manifest(spec_path, root)
    if payload != expected:
        raise ValueError("URSim preparation manifest is not a deterministic rebuild")
    return {
        "verified": True,
        "status": "blocked",
        "availability": "unavailable",
        "protocol_execution_performed": False,
        "claims": expected["claims"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--spec", type=Path, default=builder.default_spec_path())
    parser.add_argument("--repository-root", type=Path, default=builder.repository_root())
    args = parser.parse_args(argv)
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    result = verify_manifest(payload, args.spec, args.repository_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
