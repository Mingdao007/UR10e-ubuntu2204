#!/usr/bin/env python3
"""Ticket-gated V3 wrapper around the SHA-governed production bridge.

The wrapper changes three V3 integration seams: immutable mailbox reads are
identity-cached, the V1 control profile accepts the separately fingerprinted V3
TP identity, and startup consumes the compact hash-bound calibration artifact
instead of an ignored 19 MB historical CSV.  It never creates a campaign runner
or an ARM command.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from step5d_autotune_v3.runtime_calibration import bootstrap_stable_cuda_runtime


ROOT = Path(__file__).resolve().parents[1]
TICKET_ENV = "STEP5D_V3_RUNTIME_TICKET"
TICKET_SCHEMA = "step5d.autotune-v3/runtime-ticket-v1"


class BridgeTicketError(RuntimeError):
    pass


def _strict_ticket(path: Path, argv: Sequence[str]) -> dict[str, Any]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BridgeTicketError("V3 runtime ticket must be an absolute regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeTicketError(f"V3 runtime ticket is unreadable: {exc}") from exc
    required = {
        "schema",
        "parent_pid",
        "argv_sha256",
        "authorization_id",
        "authorization_scope",
        "identity",
        "launch_profile_fingerprint",
        "trial_overlay_fingerprint",
        "release_stage_id",
        "control_profile_id",
        "tp_program_id",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise BridgeTicketError("V3 runtime ticket fields differ")
    if payload["schema"] != TICKET_SCHEMA or payload["parent_pid"] != os.getppid():
        raise BridgeTicketError("V3 runtime ticket process binding differs")
    encoded_argv = json.dumps(
        list(argv), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if payload["argv_sha256"] != hashlib.sha256(encoded_argv).hexdigest():
        raise BridgeTicketError("V3 runtime ticket argv binding differs")
    expected = {
        "authorization_scope": "hil_full_bridge_hold",
        "release_stage_id": "step5d_strict_rnn_autotune_v3",
        "control_profile_id": "step5d_strict_rnn_autotune_v1",
        "tp_program_id": "step5d_strict_rnn_autotune_v3",
    }
    for key, value in expected.items():
        if payload[key] != value:
            raise BridgeTicketError(f"V3 runtime ticket {key} differs")
    return payload


def install_v3_seams() -> Any:
    import step5d_autotune_live_driver as live
    from step5d_autotune_v3.runtime_calibration import validate_installed_calibration
    from step5d_autotune_v3.runtime_profile import IdentityCachedMailbox

    calibration = validate_installed_calibration()
    original_mailbox = live.AtomicCommandMailbox

    class V3AtomicCommandMailbox(IdentityCachedMailbox):
        def __init__(self, path: Path, *, network_mode: bool = True) -> None:
            super().__init__(original_mailbox(path, network_mode=network_mode))

    live.AtomicCommandMailbox = V3AtomicCommandMailbox

    import kunwei_rtde_bridge as bridge

    original_matcher = bridge.step5d_dashboard_program_identity_matches

    def v3_tp_identity_match(value: Any, control_profile: str) -> bool:
        expected = (
            "step5d_strict_rnn_autotune_v3"
            if control_profile == "step5d_strict_rnn_autotune_v1"
            else control_profile
        )
        return original_matcher(value, expected)

    bridge.step5d_dashboard_program_identity_matches = v3_tp_identity_match

    original_runtime_prewarm = bridge.ensure_step5d_liveprep_runtime

    def v3_runtime_prewarm(state: Any, args: Any) -> None:
        if state.step5d_model_bundle is None:
            state.step5d_model_bundle = bridge.step5d_kin.build_calibrated_model()
        observed_hash = getattr(state.step5d_model_bundle, "calibration_hash", None)
        if observed_hash != calibration.calibration_hash:
            raise RuntimeError(
                "V3 calibrated model identity differs: "
                f"expected={calibration.calibration_hash}, observed={observed_hash}"
            )
        if state.step5d_tcp_offset_tool0 is None:
            state.step5d_tcp_offset_tool0 = bridge.np.asarray(
                calibration.tcp_offset_tool0_m, dtype=float
            )
        original_runtime_prewarm(state, args)

    bridge.ensure_step5d_liveprep_runtime = v3_runtime_prewarm
    return bridge


def check_v3_runtime_prewarm(bridge_argv: Sequence[str]) -> dict[str, Any]:
    """Run the exact local startup prewarm without opening any device socket."""

    started = time.monotonic()
    bridge = install_v3_seams()
    args = bridge.parse_args(list(bridge_argv))
    state = bridge.BridgeState()
    bridge.ensure_step5d_liveprep_runtime(state, args)
    missing = bridge.step5d_liveprep_runtime_missing(state, args)
    if missing:
        raise RuntimeError("V3 production startup prewarm is incomplete: " + ",".join(missing))
    return {
        "ok": True,
        "elapsed_s": time.monotonic() - started,
        "bridge_profile": args.bridge_profile,
        "rnn_backend": args.step5d_rnn_backend,
        "rnn_inner_iterations": args.step5d_rnn_inner_iterations,
        "missing": [],
    }


def main(argv: list[str] | None = None) -> int:
    bridge_argv = list(sys.argv[1:] if argv is None else argv)
    ticket_text = os.environ.get(TICKET_ENV, "")
    if not ticket_text:
        print("refusing: STEP5D_V3_RUNTIME_TICKET is required", file=sys.stderr)
        return 24
    try:
        _strict_ticket(Path(ticket_text), bridge_argv)
        bridge = install_v3_seams()
    except BridgeTicketError as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 24
    return int(bridge.main(bridge_argv))


if __name__ == "__main__":
    if os.environ.get(TICKET_ENV):
        bootstrap_stable_cuda_runtime()
    raise SystemExit(main())
