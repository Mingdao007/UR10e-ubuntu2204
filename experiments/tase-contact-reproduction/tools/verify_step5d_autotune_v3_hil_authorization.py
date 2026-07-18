#!/usr/bin/env python3
"""Build and verify the internal V3 HIL launch permit.

The canonical ``step5d-autotune-v3-hil-hold.sh`` invocation is the operator
action.  No thread id, TTL, user-authored JSON, or second confirmation is part
of this interface.  The permit is still process- and fingerprint-bound so a
bridge cannot bypass the current fail-closed readiness state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping

import verify_step5d_autotune_v3_execution_readiness as execution_readiness


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "step5d.autotune-v3/internal-launch-permit-v1"
SCOPE = "hil_full_bridge_hold"
V3_STAGE_ID = "step5d_strict_rnn_autotune_v3"
ALLOWED_ACTIONS = [
    "controller_identity_read",
    "dashboard_state_read",
    "rtde_output_read",
    "program_play_v3_by_operator",
    "kunwei_stream_start_read",
    "production_bridge_start_hold",
    "rtde_hold_heartbeat_write",
    "hold_observation",
    "program_stop",
    "bridge_cleanup",
]
FORBIDDEN_ACTIONS = [
    "arm",
    "trial_dispatch",
    "campaign_runner_start",
    "zero_tare",
    "contact",
    "motion",
    "urscript_send",
    "tp_parameter_edit",
    "payload_tcp_write",
    "safety_write",
    "arbitrary_controller_write",
]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LAUNCH_ID = re.compile(r"^[0-9a-f]{32}$")


class LaunchPermitError(RuntimeError):
    """The internal process permit cannot admit the HIL HOLD lane."""


def build_launch_permit(
    *,
    root: Path = ROOT,
    parent_pid: int | None = None,
    launch_id: str | None = None,
) -> dict[str, Any]:
    readiness = execution_readiness.verify(root)
    if readiness.get("state") != execution_readiness.READY_FOR_HIL:
        raise LaunchPermitError("release is not ready for the V3 HIL HOLD gate")
    if readiness.get("ready_to_execute") is not False:
        raise LaunchPermitError("pre-HIL execution boundary differs")
    pid = os.getpid() if parent_pid is None else parent_pid
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise LaunchPermitError("launch permit parent pid is invalid")
    identifier = uuid.uuid4().hex if launch_id is None else launch_id
    if _LAUNCH_ID.fullmatch(identifier) is None:
        raise LaunchPermitError("launch permit id is invalid")
    return {
        "schema": SCHEMA,
        "launch_id": identifier,
        "parent_pid": pid,
        "issued_by": "canonical_v3_hil_entrypoint",
        "candidate_stage_id": V3_STAGE_ID,
        "scope": SCOPE,
        # Detach the permit from the readiness payload.  Callers may retain or
        # mutate the permit before verification; that must never mutate the
        # verifier's source-of-truth object by aliasing the nested mapping.
        "identity": dict(readiness["identity"]),
        "serial": True,
        "hold_required": True,
        "allowed_actions": ALLOWED_ACTIONS,
        "forbidden_actions": FORBIDDEN_ACTIONS,
    }


def verify_launch_permit(
    payload: Mapping[str, Any],
    *,
    expected_parent_pid: int,
    root: Path = ROOT,
) -> dict[str, Any]:
    required = {
        "schema",
        "launch_id",
        "parent_pid",
        "issued_by",
        "candidate_stage_id",
        "scope",
        "identity",
        "serial",
        "hold_required",
        "allowed_actions",
        "forbidden_actions",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        raise LaunchPermitError("launch permit fields differ")
    expected = build_launch_permit(
        root=root,
        parent_pid=expected_parent_pid,
        launch_id=str(payload.get("launch_id", "")),
    )
    for key in required:
        if payload.get(key) != expected[key]:
            raise LaunchPermitError(f"launch permit {key} differs")
    identity = payload.get("identity")
    if not isinstance(identity, Mapping) or any(
        _SHA256.fullmatch(str(identity.get(field, ""))) is None
        for field in (
            "contract_sha256",
            "control_fingerprint",
            "orchestration_fingerprint",
        )
    ):
        raise LaunchPermitError("launch permit identity is invalid")
    return dict(payload)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = build_launch_permit(root=args.root)
        verified = verify_launch_permit(
            report,
            expected_parent_pid=os.getpid(),
            root=args.root,
        )
    except (
        LaunchPermitError,
        execution_readiness.ReadinessError,
    ) as exc:
        payload = {"schema": SCHEMA, "ok": False, "blocker": str(exc)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    payload = {**verified, "ok": True}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("step5d_v3_hil=ready_for_canonical_hold_entrypoint")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
