#!/usr/bin/env python3
"""Finalize one sealed V3 certification capture into both evidence classes."""

from __future__ import annotations

import argparse
from pathlib import Path

import extract_step5d_autotune_v3_certification as extractor
import promote_step5d_autotune_v3_return_route_evidence as return_promotion
import promote_step5d_autotune_v3_stopping_bound_evidence as stop_promotion


def finalize(
    *,
    capture_path: Path,
    bridge_start_context_path: Path,
    authorization_path: Path,
    deployment_readback_path: Path,
    ursim_trace_path: Path,
    stopping_measurement_path: Path,
    return_telemetry_path: Path,
    stopping_artifact_path: Path,
    return_artifact_path: Path,
) -> None:
    stopping, telemetry = extractor.extract(
        csv_path=capture_path,
        bridge_start_context_path=bridge_start_context_path,
        authorization_path=authorization_path,
        deployment_readback_path=deployment_readback_path,
    )
    extractor._write_once(stopping_measurement_path, stopping)
    extractor._write_once(return_telemetry_path, telemetry)
    stop_promotion.promote(
        measurement_path=stopping_measurement_path,
        bridge_start_context_path=bridge_start_context_path,
        authorization_path=authorization_path,
        deployment_readback_path=deployment_readback_path,
        output_path=stopping_artifact_path,
    )
    return_promotion.promote(
        ursim_trace_path=ursim_trace_path,
        telemetry_path=return_telemetry_path,
        bridge_start_context_path=bridge_start_context_path,
        authorization_path=authorization_path,
        deployment_readback_path=deployment_readback_path,
        output_path=return_artifact_path,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--bridge-start-context", type=Path, required=True)
    parser.add_argument("--certification-authorization", type=Path, required=True)
    parser.add_argument("--deployment-readback", type=Path, required=True)
    parser.add_argument("--ursim-trace", type=Path, required=True)
    parser.add_argument("--stopping-measurement", type=Path, required=True)
    parser.add_argument("--return-telemetry", type=Path, required=True)
    parser.add_argument("--stopping-artifact", type=Path, required=True)
    parser.add_argument("--return-artifact", type=Path, required=True)
    args = parser.parse_args(argv)
    finalize(
        capture_path=args.capture,
        bridge_start_context_path=args.bridge_start_context,
        authorization_path=args.certification_authorization,
        deployment_readback_path=args.deployment_readback,
        ursim_trace_path=args.ursim_trace,
        stopping_measurement_path=args.stopping_measurement,
        return_telemetry_path=args.return_telemetry,
        stopping_artifact_path=args.stopping_artifact,
        return_artifact_path=args.return_artifact,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
