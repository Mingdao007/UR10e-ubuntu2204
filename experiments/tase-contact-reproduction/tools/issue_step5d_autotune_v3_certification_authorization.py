#!/usr/bin/env python3
"""Issue one explicit-reference, bounded no-contact V3 authorization."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping

from step5d_autotune_v3.arming import load_bridge_start_context
from step5d_autotune_v3.certification import (
    RETURN_ANGULAR_ACCELERATION_RAD_S2,
    RETURN_ANGULAR_SPEED_RAD_S,
    RETURN_LINEAR_ACCELERATION_M_S2,
    RETURN_LINEAR_SPEED_M_S,
)
from step5d_autotune_v3.profile import active_identity_snapshot
from ur10e_experiment_runtime.authorization import (
    CERTIFICATION_PROCEDURES,
    STEP5D_V3_STAGE_IDENTITY,
    CertificationMotionAuthorization,
)


AUTHORIZATION_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
VALIDITY_MINUTES_MAX = 30


class CertificationIssueError(RuntimeError):
    """The explicit authorization reference or release binding is invalid."""


def _write_once(path: Path, payload: Mapping[str, object]) -> None:
    destination = path.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise CertificationIssueError("certification authorization output must be fresh")
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


def issue(
    *,
    bridge_start_context_path: Path,
    authorization_ref: str,
    validity_minutes: int,
    output_path: Path,
    now: datetime | None = None,
) -> CertificationMotionAuthorization:
    if AUTHORIZATION_REF.fullmatch(authorization_ref) is None:
        raise CertificationIssueError(
            "authorization ref must be an explicit 8-128 character owner reference"
        )
    if (
        isinstance(validity_minutes, bool)
        or not isinstance(validity_minutes, int)
        or not 1 <= validity_minutes <= VALIDITY_MINUTES_MAX
    ):
        raise CertificationIssueError("validity minutes must be in [1, 30]")
    context = load_bridge_start_context(
        bridge_start_context_path.expanduser().absolute(),
        expected_static_identity=active_identity_snapshot(),
    )
    issued = now if now is not None else datetime.now(timezone.utc)
    if issued.tzinfo is None or issued.utcoffset() is None:
        raise CertificationIssueError("authorization issue time must be zoned")
    authorization = CertificationMotionAuthorization(
        stage_identity=STEP5D_V3_STAGE_IDENTITY,
        release_basis_fingerprint=context.release_basis_fingerprint,
        deployment_fingerprint=context.deployment_fingerprint,
        plant_epoch=context.plant_epoch,
        deployment_readback_sha256=context.deployment_readback_sha256,
        allowed_procedures=CERTIFICATION_PROCEDURES,
        max_linear_speed_m_s=RETURN_LINEAR_SPEED_M_S,
        max_linear_acceleration_m_s2=RETURN_LINEAR_ACCELERATION_M_S2,
        max_angular_speed_rad_s=RETURN_ANGULAR_SPEED_RAD_S,
        max_angular_acceleration_rad_s2=RETURN_ANGULAR_ACCELERATION_RAD_S2,
        numeric_margin_m=0.001,
        authorization_source=f"explicit_user_ref:{authorization_ref}",
        authorized_at=issued.isoformat(),
        expires_at=(issued + timedelta(minutes=validity_minutes)).isoformat(),
    )
    _write_once(output_path, authorization.document())
    return authorization


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-start-context", type=Path, required=True)
    parser.add_argument("--authorization-ref", required=True)
    parser.add_argument("--validity-minutes", type=int, default=15)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    authorization = issue(
        bridge_start_context_path=args.bridge_start_context,
        authorization_ref=args.authorization_ref,
        validity_minutes=args.validity_minutes,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "schema": "step5d.autotune-v3/certification-authorization-issued-v1",
                "authorization_ref_sha256": authorization.authorization_ref_sha256,
                "campaign_allowed": False,
                "optimizer_eligible": False,
                "no_contact": True,
                "output": str(args.output.expanduser().absolute()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
