#!/usr/bin/env python3
"""Ticket-gated manual-hold wrapper around the frozen production bridge."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence
import sys

import run_step5d_autotune_v3_bridge as r009_bridge
from step5d_autotune_v3.runtime_calibration import bootstrap_stable_cuda_runtime
from step5d_manual_bridge import (
    CONTROL_PROFILE, DEFAULT_PREFLIGHT_MAX_AGE_S, PROGRAM, PROTOCOL,
    RELEASE_STAGE, ROOT, TICKET_SCHEMA, WIRE_PROTOCOL, ManualBridgeError,
    load_context, require_fresh_timestamp, sha256_path, strict_object,
)


TICKET_ENV = "STEP5D_MANUAL_BRIDGE_TICKET"
TICKET_SCOPE = "manual_bridge_no_arm"


def _argv_sha256(argv: Sequence[str]) -> str:
    encoded = json.dumps(list(argv), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def strict_ticket(path: Path, argv: Sequence[str]) -> dict[str, Any]:
    if not path.is_absolute():
        raise ManualBridgeError("manual runtime ticket must be absolute")
    payload = strict_object(path, "manual runtime ticket")
    required = {
        "schema", "parent_pid", "argv_sha256", "launch_id", "scope",
        "program", "protocol", "wire_protocol", "control_profile_id",
        "release_stage_id", "manual_release_manifest_sha256",
        "bridge_start_context", "preflight",
    }
    if set(payload) != required:
        raise ManualBridgeError("manual runtime ticket fields differ")
    if any(
        (
            payload["schema"] != TICKET_SCHEMA,
            payload["parent_pid"] != os.getppid(),
            payload["argv_sha256"] != _argv_sha256(argv),
            payload["scope"] != TICKET_SCOPE,
            payload["program"] != PROGRAM,
            payload["protocol"] != PROTOCOL,
            payload["wire_protocol"] != WIRE_PROTOCOL,
            payload["control_profile_id"] != CONTROL_PROFILE,
            payload["release_stage_id"] != RELEASE_STAGE,
        )
    ):
        raise ManualBridgeError("manual runtime ticket identity differs")
    launch_id = payload["launch_id"]
    if not isinstance(launch_id, str) or len(launch_id) != 32 or any(c not in "0123456789abcdef" for c in launch_id):
        raise ManualBridgeError("manual runtime ticket launch ID differs")
    context_ref = payload["bridge_start_context"]
    preflight_ref = payload["preflight"]
    for reference, role in ((context_ref, "context"), (preflight_ref, "preflight")):
        if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
            raise ManualBridgeError(f"manual runtime ticket {role} reference differs")
        target = Path(str(reference["path"]))
        if not target.is_absolute() or sha256_path(target) != reference["sha256"]:
            raise ManualBridgeError(f"manual runtime ticket {role} digest differs")
    context = load_context(ROOT, Path(context_ref["path"]))
    if payload["manual_release_manifest_sha256"] != context["manual_release_manifest_sha256"]:
        raise ManualBridgeError("manual runtime ticket release differs")
    preflight = strict_object(Path(preflight_ref["path"]), "manual live preflight")
    require_fresh_timestamp(
        preflight.get("created_at"),
        role="manual live preflight",
        max_age_s=DEFAULT_PREFLIGHT_MAX_AGE_S,
    )
    if any(
        (
            preflight.get("ok") is not True,
            preflight.get("fresh") is not True,
            preflight.get("tp_program_id") != PROGRAM,
            preflight.get("manual_release_manifest_sha256") != context["manual_release_manifest_sha256"],
            preflight.get("bridge_start_context_sha256") != context_ref["sha256"],
        )
    ):
        raise ManualBridgeError("manual runtime preflight identity differs")
    return payload


def install_manual_seams(ticket: Mapping[str, Any]) -> Any:
    wire_release = SimpleNamespace(
        release_stage_id=RELEASE_STAGE,
        control_profile_id=CONTROL_PROFILE,
        program_id=PROGRAM,
        protocol_id=WIRE_PROTOCOL,
    )
    bridge = r009_bridge.install_v3_seams(None, release_identity=wire_release)

    def manual_authorization_gate(args: Any, *, root: Path = ROOT) -> dict[str, Any]:
        context_ref = ticket["bridge_start_context"]
        context = load_context(root, Path(context_ref["path"]))
        if any(
            (
                ticket.get("scope") != TICKET_SCOPE,
                ticket.get("program") != PROGRAM,
                ticket.get("protocol") != PROTOCOL,
                ticket.get("wire_protocol") != WIRE_PROTOCOL,
                args.bridge_profile != CONTROL_PROFILE,
                args.step5d_autotune_command_mailbox is None,
                args.step5d_stage25_control_mode != "speedj_rnn_live",
                context.get("arm_authorized") is not False,
                context.get("motion_authorized") is not False,
            )
        ):
            raise SystemExit("manual NO_ARM bridge identity differs")
        return {
            "ok": True,
            "selected_release": RELEASE_STAGE,
            "control_profile_id": CONTROL_PROFILE,
            "tp_program_id": PROGRAM,
            "protocol_id": PROTOCOL,
            "wire_protocol_id": WIRE_PROTOCOL,
            "scope": TICKET_SCOPE,
            "live_motion_authorized": False,
        }

    bridge.require_v29_live_bridge_authorization = manual_authorization_gate
    return bridge


def main(argv: list[str] | None = None) -> int:
    bridge_argv = list(sys.argv[1:] if argv is None else argv)
    ticket_text = os.environ.get(TICKET_ENV, "")
    if not ticket_text:
        print(f"refusing: {TICKET_ENV} is required", file=sys.stderr)
        return 24
    try:
        ticket = strict_ticket(Path(ticket_text), bridge_argv)
        bridge = install_manual_seams(ticket)
    except (OSError, ValueError, ManualBridgeError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 24
    return int(bridge.main(bridge_argv))


if __name__ == "__main__":
    if os.environ.get(TICKET_ENV):
        bootstrap_stable_cuda_runtime()
    raise SystemExit(main())
