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
import step5d_autotune_live_driver as live_driver
from step5d_autotune_contract import ForceCandidate
from step5d_autotune_v3.profile import canonical_sha256
from step5d_autotune_v3.runtime_profile import normalize_trial_overlay
from step5d_manual_bridge import (
    CONTROL_PROFILE, DEFAULT_PREFLIGHT_MAX_AGE_S, PROGRAM, PROTOCOL,
    RELEASE_STAGE, ROOT, TICKET_SCHEMA, WIRE_PROTOCOL, ManualBridgeError,
    load_context, require_fresh_timestamp, sha256_path, strict_object,
)
from step5d_manual_profile import (
    DEFAULT_LAUNCH_PROFILE,
    load_manual_launch_profile as load_launch_profile,
)


TICKET_ENV = "STEP5D_MANUAL_BRIDGE_TICKET"
TICKET_SCOPE = "manual_bridge_no_arm"
_BASE_BRIDGE_MAILBOX_RUNTIME = live_driver.BridgeMailboxRuntime
MANUAL_HARD_GUARDS = {
    "max_normal_force_n": 60.0,
    "max_force_norm_n": 100.0,
    "max_torque_norm_nm": 3.0,
    "qdot_cap_rad_s": 0.5,
    "sensor_stale_s": 2.0,
}


class ManualBridgeMailboxRuntime(_BASE_BRIDGE_MAILBOX_RUNTIME):
    """Manual route uses the canonical production identity-commit FSM."""


def require_manual_guard_semantics(bridge: Any) -> None:
    """Fail closed if the inherited production guard contract drifts."""

    observed = {
        "permissive_contact": CONTROL_PROFILE
        in bridge.STEP5D_PERMISSIVE_CONTACT_PROFILE_IDS,
        "qdot_cap_rad_s": float(bridge.STEP5D_V31_QDOT_CAP_RAD_S),
        "sensor_stale_s": float(bridge.STEP5D_V31_SENSOR_STALE_S),
        "preload_param_valid_code": float(bridge.STEP5D_LINE_ENTRY_PARAM_VALID_CODE),
    }
    expected = {
        "permissive_contact": True,
        "qdot_cap_rad_s": MANUAL_HARD_GUARDS["qdot_cap_rad_s"],
        "sensor_stale_s": MANUAL_HARD_GUARDS["sensor_stale_s"],
        "preload_param_valid_code": 521.0,
    }
    if observed != expected:
        raise ManualBridgeError(
            f"manual production guard semantics differ: expected={expected!r} observed={observed!r}"
        )


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


def apply_manual_arm_runtime(
    bridge: Any,
    args: Any,
    binding: Any,
    _arming_context: Any | None = None,
) -> None:
    """Apply one validated manual overlay without the frozen r009 UID typo."""

    overlay = binding.trial_overlay
    if overlay is None:
        raise ManualBridgeError("manual ARM requires a bound trial overlay")
    normalized = normalize_trial_overlay(
        overlay,
        profile=load_launch_profile(DEFAULT_LAUNCH_PROFILE),
    )
    candidate = ForceCandidate(
        force_p_gain=normalized["force_p_gain"],
        force_i_gain=normalized["force_i_gain"],
        force_damping=normalized["force_damping"],
    )
    profile = binding.profile
    args.step5d_autotune_force_p = candidate.force_p_gain
    args.step5d_autotune_force_i = candidate.force_i_gain
    args.step5d_autotune_force_damping = candidate.force_damping
    args.step5d_autotune_force_terms = {
        "P": candidate.force_p_gain,
        "I": candidate.force_i_gain,
        "damping": candidate.force_damping,
        **candidate.native_mapping,
    }
    args.step5d_autotune_normal_rate_rad_s = profile.normal_max_rate_rad_s
    args.step5d_autotune_host_slew_rad_s2 = profile.host_qdot_slew_rad_s2
    args.step5d_autotune_speedj_acceleration_rad_s2 = profile.tp_speedj_accel_rad_s2
    args.max_normal_force_n = MANUAL_HARD_GUARDS["max_normal_force_n"]
    args.max_force_norm_n = MANUAL_HARD_GUARDS["max_force_norm_n"]
    args.max_torque_norm_nm = MANUAL_HARD_GUARDS["max_torque_norm_nm"]
    args.step5d_qdot_limit_rad_s = MANUAL_HARD_GUARDS["qdot_cap_rad_s"]
    args.sensor_stale_s = MANUAL_HARD_GUARDS["sensor_stale_s"]
    args.bridge_normal_max_rate_rad_s = profile.normal_max_rate_rad_s
    args.step4e_normal_max_rate_rad_s = profile.normal_max_rate_rad_s
    args.step5d_autotune_profile_eligibility = "live_eligible"
    args.step5d_autotune_batch_row_index = binding.batch_row_index or 0
    args.step5d_autotune_logical_batch_sequence = binding.logical_batch_sequence or 0
    for field in (
        "step5d_preload_filtered_min_n",
        "step5d_preload_filtered_max_n",
        "step5d_preload_raw_min_n",
        "step5d_preload_raw_max_n",
        "step5d_preload_force_norm_max_n",
        "step5d_preload_hold_s",
        "step5d_preload_timeout_s",
    ):
        setattr(args, field, float(normalized[field]))
    args.step5d_autotune_control_candidate_uid = normalized["control_candidate_uid"]
    args.step5d_autotune_orientation_ko = normalized["orientation_ko"]
    bridge.STEP5D_V33_ORIENTATION_KO = normalized["orientation_ko"]
    prior = r009_bridge.STEP5D_V3_PHYSICAL_PRIOR
    args.step5d_physical_prior_reaction_normal_b = prior.reaction_normal_b
    args.step5d_physical_prior_approach_axis_b = prior.approach_axis_b
    args.step5d_physical_prior_precontact_rotvec_rad = prior.precontact_rotvec_rad
    args.step5d_physical_prior_identity_payload = prior.identity_payload()
    args.step5d_physical_prior_sha256 = prior.fingerprint
    args.step5d_physical_prior_binding_valid = (
        canonical_sha256(args.step5d_physical_prior_identity_payload)
        == args.step5d_physical_prior_sha256
    )
    args.step5d_live_normal_load_gate_n = prior.load_gate_n
    args.step5d_live_normal_load_gate_dwell_s = prior.load_gate_dwell_s
    args.bridge_normal_max_rate_rad_s = prior.normal_rate_limit_rad_s
    args.step4e_normal_max_rate_rad_s = prior.normal_rate_limit_rad_s
    args.step5d_moving_sphere_enabled = False


def install_manual_seams(ticket: Mapping[str, Any]) -> Any:
    wire_release = SimpleNamespace(
        release_stage_id=RELEASE_STAGE,
        control_profile_id=CONTROL_PROFILE,
        program_id=PROGRAM,
        protocol_id=WIRE_PROTOCOL,
    )
    if live_driver.BridgeMailboxRuntime is not _BASE_BRIDGE_MAILBOX_RUNTIME:
        raise ManualBridgeError("manual mailbox runtime seam was already installed")
    live_driver.BridgeMailboxRuntime = ManualBridgeMailboxRuntime
    bridge = r009_bridge.install_v3_seams(None, release_identity=wire_release)
    require_manual_guard_semantics(bridge)

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
    live_driver.BridgeMailboxRuntime._apply_arm_runtime = staticmethod(
        lambda args, binding, arming_context=None: apply_manual_arm_runtime(
            bridge, args, binding, arming_context
        )
    )
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
    raise SystemExit(main())
