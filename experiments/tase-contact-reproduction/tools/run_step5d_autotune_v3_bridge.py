#!/usr/bin/env python3
"""Ticket-gated V3 wrapper around the SHA-governed production bridge.

The wrapper changes three V3 integration seams: immutable mailbox reads are
identity-cached, the V1 control profile accepts the separately fingerprinted V3
TP identity, and startup consumes the compact hash-bound calibration artifact
without reading the 19 MB historical CSV.  It never creates a campaign runner or
an ARM command.
"""

from __future__ import annotations

import csv
import hashlib
import json
import multiprocessing
import os
import queue
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from ur10e_experiment_runtime.identity import canonical_sha256
from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from step5d_autotune_v3.release_identity import (
    ReleaseIdentity,
    ReleaseIdentityError,
    load_runtime_release,
)
from step5d_autotune_v3.runtime_gate import (
    ArmGateProvider,
    RuntimeGateError,
    load_campaign_lease,
    loaded_program_matches,
    release_runtime_contract,
)
from step5d_autotune_v3.runtime_installation import require_runtime_profile

TICKET_ENV = "STEP5D_V3_RUNTIME_TICKET"
TICKET_SCHEMA = "step5d.autotune-v3/runtime-ticket-v6"
TICKET_SCOPE = "campaign_lease_no_arm_until_observed"


class BridgeTicketError(RuntimeError):
    pass


_V3_COMPACT_EXACT_FIELDS = frozenset(
    {
        "write_index", "t_wall_ns", "t_monotonic_s", "sensor_age_s",
        "rtde_controller_timestamp_s", "rtde_feedback_age_s",
        "rtde_packets_drained", "rtde_feedback_stale_dwell_s",
        "rtde_sent_echo_heartbeat_gap", "normal_force_n", "force_norm_n",
        "torque_norm_nm", "heartbeat", "sensor_ok", "stop_request",
        "target_force_n", "step4e_cmd_vx_m_s", "step4e_cmd_vy_m_s",
        "step4e_cmd_vz_m_s", "step4e_cmd_wx_rad_s", "step4e_cmd_wy_rad_s",
        "step4e_cmd_wz_rad_s", "step4e_cmd_valid", "step4e_progress_m",
        "step4e_force_error_n", "step4e_orientation_error_rad",
        "step4e_controller_state", "campaign_epoch", "trial_id", "command",
        "candidate_token", "execution_profile_id", "command_seq", "guard_reason",
        "baseline_ready", "baseline_epoch", "zero_event_id", "rtde_connected",
        "rtde_reconnects", "_step4e_force_b_x", "_step4e_force_b_y",
        "_step4e_force_b_z", "_step4e_normal_load_n", "_step4e_line_stage_s",
        "_step4e_path_time_s", "_step4e_path_error_x_m", "_step4e_path_error_y_m",
        "_step5d_expected_stage", "_step5d_stage25_control_mode",
        "_step5d_stage25_echo_consumed", "_step5d_stage25_row_gap_s",
        "_step5d_constraint_residual_norm", "_step5d_raw_rnn_residual_norm",
        "_step5d_post_slew_residual_norm", "_step5d_rnn_vs_oracle_qdot_norm",
        "_step5d_qdot_slew_limiter_active", "_step5d_normal_rate_limiter_active",
        "_step5d_qdot_cap_rad_s", "_step5d_rnn_qdot_max_abs_raw_rad_s",
        "_step5d_rnn_accepted", "_step5d_safe_hold_active",
        "_step5d_contact_safety_reason", "_step5d_contact_orientation_error_rad",
        "_step5d_outer_orientation_error_rad",
        "_step5d_outer_xdot_limited_approach_normal_m_s",
        "_step5d_jqdot_cmd_approach_normal_m_s",
        "_bridge_loop_gap_s", "_bridge_loop_deadline_lateness_s",
        "_bridge_loop_compute_deadline_overrun", "_bridge_loop_missed_slots",
        "_bridge_loop_deadline_miss_total", "_bridge_loop_io_wait_s",
        "_bridge_loop_sensor_recv_s", "_bridge_loop_rtde_recv_s",
        "_bridge_loop_compute_s", "_bridge_loop_rtde_send_s",
        "_bridge_loop_csv_write_s", "ur_kinematics_dt_s", "ur_timestamp",
        "ur_runtime_state", "ur_robot_mode", "ur_safety_mode", "ur_speed_scaling",
        "ur_output_double_register_26", "ur_output_double_register_30",
        "ur_output_double_register_35", "ur_output_double_register_36",
        "ur_output_double_register_37", "ur_output_double_register_38",
        "ur_output_double_register_39", "ur_output_double_register_40",
        "ur_output_double_register_41", "ur_output_double_register_42",
        "ur_output_double_register_43", "ur_output_double_register_44",
    }
)
_V3_COMPACT_PREFIXES = (
    "ur_actual_TCP_pose_", "ur_actual_TCP_speed_", "ur_actual_q_",
    "ur_actual_qd_", "ur_actual_qdd_", "ur_output_int_register_",
)
_V3_CAPTURE_IDENTITY_FIELDS = (
    "autotune_trial_uid",
    "autotune_backend_id",
    "autotune_control_candidate_uid",
    "autotune_force_p_gain",
    "autotune_force_i_gain",
    "autotune_force_damping",
    "autotune_orientation_ko",
)
_V3_RUNNER_CLOSURE_FIELDS = frozenset(
    {
        "t_monotonic_s",
        "ur_timestamp",
        "ur_safety_mode",
        *(f"ur_{name}_{index}" for name in ("actual_TCP_pose", "actual_TCP_speed", "actual_q", "actual_qd") for index in range(6)),
        *(f"ur_output_int_register_{index}" for index in range(24, 31)),
        *(f"ur_output_double_register_{index}" for index in range(35, 45)),
    }
)


def compact_v3_fieldnames(fieldnames: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(
        name
        for name in fieldnames
        if name in _V3_COMPACT_EXACT_FIELDS
        or any(name.startswith(prefix) for prefix in _V3_COMPACT_PREFIXES)
    )
    required = _V3_RUNNER_CLOSURE_FIELDS | {
        "rtde_feedback_age_s",
        "_step5d_stage25_echo_consumed",
        "ur_output_double_register_35",
    }
    if not required.issubset(selected):
        missing = sorted(required - set(selected))
        raise BridgeTicketError(f"V3 compact capture lacks required fields: {missing}")
    return selected


def _v3_capture_worker(
    commands: Any,
    errors: Any,
    root_text: str,
    fieldnames: tuple[str, ...],
) -> None:
    root = Path(root_text)
    handle: Any | None = None
    writer: csv.DictWriter | None = None
    partial: Path | None = None
    final: Path | None = None
    active_uid: str | None = None

    def close_partial(sync_bytes: bool) -> None:
        nonlocal handle, writer
        if handle is not None:
            handle.flush()
            if sync_bytes:
                os.fsync(handle.fileno())
            handle.close()
        handle = None
        writer = None

    try:
        while True:
            message = commands.get()
            kind = message[0]
            if kind == "close":
                close_partial(True)
                return
            if kind == "row":
                trial_uid, payload = message[1], message[2]
                if active_uid != trial_uid:
                    close_partial(True)
                    trial_dir = root / trial_uid
                    trial_dir.mkdir(parents=True, exist_ok=True)
                    if trial_dir.is_symlink():
                        raise BridgeTicketError("V3 capture directory is a symlink")
                    partial = trial_dir / "capture.csv.part"
                    final = trial_dir / "capture.csv"
                    if final.exists() or partial.exists():
                        raise BridgeTicketError("V3 capture identity already exists")
                    handle = partial.open("x", newline="", encoding="utf-8")
                    writer = csv.DictWriter(handle, fieldnames=fieldnames)
                    writer.writeheader()
                    handle.flush()
                    os.fsync(handle.fileno())
                    active_uid = trial_uid
                assert writer is not None
                writer.writerow(payload)
                continue
            if kind == "seal":
                trial_uid = message[1]
                if trial_uid != active_uid or partial is None or final is None:
                    raise BridgeTicketError("V3 capture seal identity differs")
                close_partial(True)
                os.replace(partial, final)
                directory_fd = os.open(final.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                continue
            raise BridgeTicketError("unknown V3 capture worker command")
    except BaseException as exc:
        try:
            errors.put_nowait(f"{type(exc).__name__}:{exc}")
        except Exception:
            pass
        close_partial(False)


class V3AsyncBridgeTrialCsvRotator:
    """Bounded compact capture queue; disk I/O never runs in the control loop."""

    IDENTITY_COLUMNS = _V3_CAPTURE_IDENTITY_FIELDS[:2]

    def __init__(self, root: Path, fieldnames: Sequence[str]) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise BridgeTicketError("V3 capture root must be absolute")
        compact = compact_v3_fieldnames(tuple(str(name) for name in fieldnames))
        self.root = root
        self.fieldnames = (*compact, *_V3_CAPTURE_IDENTITY_FIELDS)
        context = multiprocessing.get_context("spawn")
        self._commands = context.Queue(maxsize=4096)
        self._errors = context.Queue(maxsize=1)
        self._worker = context.Process(
            target=_v3_capture_worker,
            args=(self._commands, self._errors, str(root), self.fieldnames),
            name="step5d-v3-capture-writer",
            daemon=True,
        )
        self._worker.start()
        self._sealed: set[str] = set()

    def _check_worker(self) -> None:
        try:
            error = self._errors.get_nowait()
        except queue.Empty:
            error = None
        if error is not None:
            raise BridgeTicketError(f"V3 capture worker failed: {error}")
        if not self._worker.is_alive() and self._worker.exitcode not in {None, 0}:
            raise BridgeTicketError(
                f"V3 capture worker exited rc={self._worker.exitcode}"
            )

    def _enqueue(self, message: tuple[Any, ...]) -> None:
        self._check_worker()
        try:
            self._commands.put_nowait(message)
        except queue.Full as exc:
            raise BridgeTicketError("V3 capture queue overflow") from exc

    def observe(self, row: Any, *, active: Any, rtde_output: Any) -> bool:
        if active is None or rtde_output is None:
            return False
        from step5d_autotune_live_driver import tp_packet_from_rtde
        from step5d_autotune_state_machine import TpLoopState

        snapshot = tp_packet_from_rtde(rtde_output)
        binding = active.binding
        matches = (
            snapshot.campaign_epoch_echo == binding.campaign_epoch
            and snapshot.trial_id_echo == binding.trial_id
            and snapshot.candidate_token_echo == binding.candidate_token
            and snapshot.execution_profile_id_echo == binding.execution_profile_id
            and snapshot.consumed_command_seq >= binding.arm_command_seq
            and (
                getattr(binding, "logical_batch_sequence", None) is None
                or snapshot.logical_batch_sequence_echo
                == binding.logical_batch_sequence
            )
        )
        if snapshot.state is TpLoopState.READY_HOME or not matches:
            return False
        if binding.trial_uid in self._sealed:
            return False
        overlay = binding.trial_overlay
        if overlay is None:
            raise BridgeTicketError("V3 capture lacks trial overlay")
        payload = {name: row.get(name, "") for name in self.fieldnames}
        payload.update(
            {
                "autotune_trial_uid": binding.trial_uid,
                "autotune_backend_id": binding.backend_id,
                "autotune_control_candidate_uid": overlay["control_candidate_uid"],
                "autotune_force_p_gain": overlay["force_p_gain"],
                "autotune_force_i_gain": overlay["force_i_gain"],
                "autotune_force_damping": overlay["force_damping"],
                "autotune_orientation_ko": overlay["orientation_ko"],
            }
        )
        self._enqueue(("row", binding.trial_uid, payload))
        if snapshot.state in {
            TpLoopState.WAIT_ACK,
            TpLoopState.READY_NEAR,
            TpLoopState.READY_HOME_CLOSED,
            TpLoopState.READY_HOME_NEXT,
            TpLoopState.WAIT_INFRA_READY,
            TpLoopState.FAULT,
        }:
            self._enqueue(("seal", binding.trial_uid))
            self._sealed.add(binding.trial_uid)
        return True

    def close(self) -> None:
        if self._worker.exitcode is None:
            self._enqueue(("close",))
            self._worker.join(timeout=5.0)
        self._check_worker()
        if self._worker.is_alive():
            raise BridgeTicketError("V3 capture worker did not close")


def _apply_v3_arm_runtime(
    bridge: Any,
    args: Any,
    binding: Any,
    arming_context: Any | None,
    original_apply: Any,
    launch_profile: Any,
) -> None:
    """Apply the real V3 control candidate once, at the ARM boundary."""

    original_apply(args, binding, arming_context)
    overlay = binding.trial_overlay
    if overlay is None:
        raise BridgeTicketError("V3 ARM requires a bound trial overlay")
    from step5d_autotune_contract import ForceCandidate
    from step5d_autotune_v3.runtime_profile import normalize_trial_overlay

    normalized = normalize_trial_overlay(
        overlay,
        profile=launch_profile,
    )
    candidate = ForceCandidate(
        force_p_gain=normalized["force_p_gain"],
        force_i_gain=normalized["force_i_gain"],
        force_damping=normalized["force_damping"],
    )
    args.step5d_autotune_force_p = candidate.force_p_gain
    args.step5d_autotune_force_i = candidate.force_i_gain
    args.step5d_autotune_force_damping = candidate.force_damping
    args.step5d_autotune_force_terms = {
        "P": candidate.force_p_gain,
        "I": candidate.force_i_gain,
        "damping": candidate.force_damping,
        **candidate.native_mapping,
    }
    args.step5d_autotune_control_candidate_uid = normalized[
        "control_candidate_uid"
    ]
    args.step5d_autotune_orientation_ko = normalized["orientation_ko"]
    bridge.STEP5D_V33_ORIENTATION_KO = normalized["orientation_ko"]
    args.step5d_physical_prior_reaction_normal_b = STEP5D_V3_PHYSICAL_PRIOR.reaction_normal_b
    args.step5d_physical_prior_approach_axis_b = STEP5D_V3_PHYSICAL_PRIOR.approach_axis_b
    args.step5d_physical_prior_precontact_rotvec_rad = STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad
    args.step5d_physical_prior_identity_payload = STEP5D_V3_PHYSICAL_PRIOR.identity_payload()
    args.step5d_physical_prior_sha256 = STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    args.step5d_physical_prior_binding_valid = (
        canonical_sha256(args.step5d_physical_prior_identity_payload)
        == args.step5d_physical_prior_sha256
    )
    args.step5d_live_normal_load_gate_n = STEP5D_V3_PHYSICAL_PRIOR.load_gate_n
    args.step5d_live_normal_load_gate_dwell_s = STEP5D_V3_PHYSICAL_PRIOR.load_gate_dwell_s
    args.bridge_normal_max_rate_rad_s = STEP5D_V3_PHYSICAL_PRIOR.normal_rate_limit_rad_s
    args.step4e_normal_max_rate_rad_s = STEP5D_V3_PHYSICAL_PRIOR.normal_rate_limit_rad_s
    args.step5d_moving_sphere_enabled = False


def _strict_ticket(
    path: Path,
    argv: Sequence[str],
    *,
    release_identity: ReleaseIdentity | None = None,
    root: Path = ROOT,
) -> dict[str, Any]:
    try:
        release = release_identity or load_runtime_release(root)
    except ReleaseIdentityError as exc:
        raise BridgeTicketError(f"active release manifest is invalid: {exc}") from exc
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
        "launch_id",
        "scope",
        "launch_profile",
        "launch_profile_fingerprint",
        "trial_overlay_fingerprint",
        "release_stage_id",
        "control_profile_id",
        "tp_program_id",
        "manifest_sha256",
        "safety_envelope_sha256",
        "campaign_binding",
        "campaign_lease",
        "arm_gate_path",
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
        "release_stage_id": release.release_stage_id,
        "control_profile_id": release.control_profile_id,
        "tp_program_id": release.program_id,
        "manifest_sha256": release.manifest_sha256,
    }
    for key, value in expected.items():
        if payload[key] != value:
            raise BridgeTicketError(f"V3 runtime ticket {key} differs")
    if payload["scope"] != TICKET_SCOPE:
        raise BridgeTicketError("V3 runtime ticket scope differs")
    launch_id = payload["launch_id"]
    if (
        not isinstance(launch_id, str)
        or len(launch_id) != 32
        or any(character not in "0123456789abcdef" for character in launch_id)
    ):
        raise BridgeTicketError("V3 runtime ticket launch id differs")
    for value in (
        payload["launch_profile_fingerprint"],
        payload["trial_overlay_fingerprint"],
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise BridgeTicketError("V3 runtime ticket fingerprint differs")
    launch_reference = payload["launch_profile"]
    if not isinstance(launch_reference, dict) or set(launch_reference) != {
        "path",
        "sha256",
    }:
        raise BridgeTicketError("V3 runtime ticket launch-profile reference differs")
    launch_path = Path(str(launch_reference["path"]))
    if (
        not launch_path.is_absolute()
        or launch_path.is_symlink()
        or not launch_path.is_file()
    ):
        raise BridgeTicketError("V3 runtime ticket launch profile is unavailable")
    release_root = (root / release.manifest_path).resolve(strict=True).parent
    resolved_launch = launch_path.resolve(strict=True)
    try:
        launch_relative = resolved_launch.relative_to(release_root).as_posix()
    except ValueError as exc:
        raise BridgeTicketError("V3 runtime ticket launch profile is not immutable") from exc
    expected_launch_sha256 = release.generated_files.get(launch_relative)
    observed_launch_sha256 = hashlib.sha256(resolved_launch.read_bytes()).hexdigest()
    if (
        expected_launch_sha256 is None
        or launch_reference["sha256"] != expected_launch_sha256
        or observed_launch_sha256 != expected_launch_sha256
    ):
        raise BridgeTicketError("V3 runtime ticket launch-profile binding differs")
    binding = payload["campaign_binding"]
    if (
        not isinstance(binding, dict)
        or set(binding)
        != {
            "campaign_id",
            "campaign_epoch",
            "campaign_fingerprint",
            "candidate_plan_revision",
            "candidate_plan_sha256",
            "trial_overlay_plan_sha256",
            "machine_binding_sha256",
        }
        or not isinstance(binding["campaign_id"], str)
        or not binding["campaign_id"]
        or isinstance(binding["campaign_epoch"], bool)
        or not isinstance(binding["campaign_epoch"], int)
        or binding["campaign_epoch"] < 1
        or isinstance(binding["candidate_plan_revision"], bool)
        or not isinstance(binding["candidate_plan_revision"], int)
        or binding["candidate_plan_revision"] < 1
    ):
        raise BridgeTicketError("live runtime ticket campaign binding differs")
    for key in (
        "campaign_fingerprint",
        "candidate_plan_sha256",
        "trial_overlay_plan_sha256",
        "machine_binding_sha256",
    ):
        value = binding[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise BridgeTicketError("live runtime ticket plan fingerprint differs")
    try:
        contract = release_runtime_contract(root, release)
    except RuntimeGateError as exc:
        raise BridgeTicketError(f"release runtime contract differs: {exc}") from exc
    if payload["safety_envelope_sha256"] != contract["safety_envelope_sha256"]:
        raise BridgeTicketError("runtime ticket safety envelope differs")
    lease_reference = payload["campaign_lease"]
    if (
        not isinstance(lease_reference, Mapping)
        or set(lease_reference) != {"path", "sha256"}
        or not isinstance(lease_reference["path"], str)
    ):
        raise BridgeTicketError("runtime ticket campaign lease reference differs")
    lease_path = Path(lease_reference["path"])
    try:
        lease = load_campaign_lease(
            lease_path,
            expected_sha256=lease_reference["sha256"],
        )
    except RuntimeGateError as exc:
        raise BridgeTicketError(f"runtime ticket campaign lease differs: {exc}") from exc
    if (
        lease.launch_id != payload["launch_id"]
        or lease.manifest_sha256 != release.manifest_sha256
        or lease.safety_envelope_sha256 != payload["safety_envelope_sha256"]
        or lease.campaign_id != binding["campaign_id"]
        or lease.campaign_epoch != binding["campaign_epoch"]
        or lease.campaign_fingerprint != binding["campaign_fingerprint"]
    ):
        raise BridgeTicketError("runtime ticket campaign lease binding differs")
    gate_path_text = payload["arm_gate_path"]
    if not isinstance(gate_path_text, str) or not gate_path_text:
        raise BridgeTicketError("runtime ticket ARM gate path differs")
    gate_path = Path(gate_path_text)
    if not gate_path.is_absolute() or gate_path.is_symlink():
        raise BridgeTicketError("runtime ticket ARM gate path is unsafe")
    return payload


def _require_v3_no_arm_bridge(
    args: Any,
    ticket: Mapping[str, Any],
    *,
    root: Path = ROOT,
    release_identity: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    """Replace the legacy V1-selection gate with the ticket-bound V3 gate."""

    release = release_identity or load_runtime_release(root)
    if (
        ticket.get("scope") != TICKET_SCOPE
        or ticket.get("release_stage_id") != release.release_stage_id
        or ticket.get("control_profile_id") != release.control_profile_id
        or ticket.get("tp_program_id") != release.program_id
        or args.bridge_profile != release.control_profile_id
    ):
        raise BridgeTicketError("V3 NO_ARM bridge identity differs")
    if args.step5d_autotune_command_mailbox is None:
        raise BridgeTicketError("V3 NO_ARM bridge requires the continuous command mailbox")
    if args.step5d_stage25_control_mode != "speedj_rnn_live":
        raise BridgeTicketError("V3 NO_ARM bridge requires the frozen V1 Stage25 kernel")
    return {
        "ok": True,
        "selected_release": release.release_stage_id,
        "control_profile_id": release.control_profile_id,
        "tp_program_id": release.program_id,
        "protocol_id": release.protocol_id,
        "scope": TICKET_SCOPE,
        "campaign_lease_bound": True,
        "live_motion_authorized": False,
    }


def _release_runtime_protocol(
    release: ReleaseIdentity | None,
    *,
    completion_protocol: str | None,
    error_type: type[Exception] = BridgeTicketError,
) -> str:
    if release is None:
        raise error_type(
            "V3 bridge runtime requires an explicit SHA-pinned release protocol"
        )
    if completion_protocol is not None and completion_protocol != release.protocol_id:
        raise error_type("V3 bridge completion protocol differs from release")
    return release.protocol_id


def build_release_mailbox_runtime(
    runtime_type: type[Any],
    path: Path,
    *,
    release: ReleaseIdentity,
    completion_protocol: str | None = None,
) -> Any:
    """Construct the exact wrapper-owned runtime without installing global seams."""

    protocol = _release_runtime_protocol(
        release,
        completion_protocol=completion_protocol,
    )
    return runtime_type(
        path,
        completion_protocol=protocol,
        arming_context_provider=lambda *_args, **_kwargs: None,
    )


def install_v3_seams(
    ticket: Mapping[str, Any] | None = None,
    *,
    release_identity: ReleaseIdentity | None = None,
) -> Any:
    import step5d_autotune_live_driver as live
    from step5d_autotune_v3.runtime_calibration import validate_installed_calibration
    from step5d_autotune_v3.runtime_profile import IdentityCachedMailbox

    release = release_identity
    if ticket is not None and release is None:
        release = load_runtime_release(ROOT)
    immutable_launch_profile = None
    if ticket is not None:
        from step5d_autotune_v3.runtime_profile import load_launch_profile

        immutable_launch_profile = load_launch_profile(
            Path(ticket["launch_profile"]["path"])
        )
        if (
            immutable_launch_profile.fingerprint
            != ticket["launch_profile_fingerprint"]
        ):
            raise BridgeTicketError("V3 runtime launch-profile fingerprint differs")
    calibration = validate_installed_calibration()
    original_mailbox = live.AtomicCommandMailbox
    original_runtime = live.BridgeMailboxRuntime

    class V3AtomicCommandMailbox(IdentityCachedMailbox):
        def __init__(
            self,
            path: Path,
            *,
            network_mode: bool = True,
            launch_profile: Any | None = None,
        ) -> None:
            if immutable_launch_profile is None:
                raise live.MailboxError("V3 immutable launch profile is unavailable")
            if (
                launch_profile is not None
                and launch_profile.fingerprint
                != immutable_launch_profile.fingerprint
            ):
                raise live.MailboxError("V3 alternate launch profile is forbidden")
            super().__init__(
                original_mailbox(
                    path,
                    network_mode=network_mode,
                    launch_profile=immutable_launch_profile,
                )
            )

    live.AtomicCommandMailbox = V3AtomicCommandMailbox

    class V3BridgeMailboxRuntime(original_runtime):
        def __init__(
            self,
            path: Path,
            *,
            campaign_home_reference_path: Path | None = None,
            arming_context_provider: Callable[..., Any | None] | None = None,
            completion_protocol: str | None = None,
        ) -> None:
            protocol = _release_runtime_protocol(
                release,
                completion_protocol=completion_protocol,
                error_type=live.MailboxError,
            )
            if ticket is None or release is None:
                raise live.MailboxError("V3 runtime ARM gate ticket is unavailable")
            if arming_context_provider is not None:
                raise live.MailboxError(
                    "V3 runtime forbids an alternate arming-context provider"
                )
            lease_reference = ticket["campaign_lease"]
            try:
                self._v3_arm_gate = ArmGateProvider(
                    root=ROOT,
                    gate_path=Path(ticket["arm_gate_path"]),
                    lease_path=Path(lease_reference["path"]),
                    lease_sha256=lease_reference["sha256"],
                    release=release,
                )
            except RuntimeGateError as exc:
                raise live.MailboxError(f"V3 ARM gate initialization failed: {exc}") from exc
            super().__init__(
                path,
                campaign_home_reference_path=campaign_home_reference_path,
                arming_context_provider=self._v3_arm_gate,
                completion_protocol=protocol,
                launch_profile=immutable_launch_profile,
            )

        def poll(
            self,
            args: Any,
            output: Mapping[str, Any] | None,
            *,
            connection_epoch: int = 0,
        ) -> bool:
            try:
                self._v3_arm_gate.observe_rtde(
                    output,
                    connection_epoch=connection_epoch,
                )
                return super().poll(
                    args,
                    output,
                    connection_epoch=connection_epoch,
                )
            except RuntimeGateError as exc:
                raise live.MailboxError(f"V3 ARM gate failed closed: {exc}") from exc

    live.BridgeMailboxRuntime = V3BridgeMailboxRuntime
    live.BridgeTrialCsvRotator = V3AsyncBridgeTrialCsvRotator

    import kunwei_rtde_bridge as bridge
    from step5d_autotune_v3.dashboard import dashboard_exchange

    bridge.dashboard_exchange = dashboard_exchange

    original_authorization_gate = bridge.require_v29_live_bridge_authorization

    def v3_authorization_gate(
        args: Any,
        *,
        root: Path = ROOT,
    ) -> dict[str, Any] | None:
        if release is None or args.bridge_profile != release.control_profile_id:
            return original_authorization_gate(args, root=root)
        if ticket is None:
            raise BridgeTicketError("V3 runtime ticket is unavailable at bridge gate")
        try:
            return _require_v3_no_arm_bridge(
                args,
                ticket,
                root=root,
                release_identity=release,
            )
        except BridgeTicketError as exc:
            raise SystemExit(f"V3 NO_ARM bridge gate failed: {exc}") from exc

    bridge.require_v29_live_bridge_authorization = v3_authorization_gate

    original_dict_writer = bridge.csv.DictWriter

    def v3_dict_writer(handle: Any, fieldnames: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        fields = tuple(fieldnames)
        if "_step5d_stage25_echo_consumed" in fields:
            fields = compact_v3_fieldnames(fields)
            kwargs["extrasaction"] = "ignore"
        return original_dict_writer(handle, fieldnames=fields, *args, **kwargs)

    bridge.csv.DictWriter = v3_dict_writer

    original_apply_arm_runtime = live.BridgeMailboxRuntime._apply_arm_runtime

    def v3_apply_arm_runtime(
        args: Any,
        binding: Any,
        arming_context: Any | None = None,
    ) -> None:
        if immutable_launch_profile is None:
            raise BridgeTicketError("V3 runtime launch profile is unavailable")
        _apply_v3_arm_runtime(
            bridge,
            args,
            binding,
            arming_context,
            original_apply_arm_runtime,
            immutable_launch_profile,
        )

    live.BridgeMailboxRuntime._apply_arm_runtime = staticmethod(v3_apply_arm_runtime)

    original_matcher = bridge.step5d_dashboard_program_identity_matches
    runtime_contract = (
        None if release is None else release_runtime_contract(ROOT, release)
    )

    def v3_tp_identity_match(value: Any, control_profile: str) -> bool:
        if release is not None and control_profile == release.control_profile_id:
            assert runtime_contract is not None
            return loaded_program_matches(
                value,
                runtime_contract["expected_loaded_program"],
            )
        return original_matcher(value, control_profile)

    bridge.step5d_dashboard_program_identity_matches = v3_tp_identity_match

    def v3_runtime_prewarm(state: Any, args: Any) -> None:
        if state.step5d_model_bundle is None:
            state.step5d_model_bundle = bridge.step5d_kin.build_calibrated_model()
        observed_hash = getattr(state.step5d_model_bundle, "calibration_hash", None)
        if observed_hash != calibration.calibration_hash:
            raise RuntimeError(
                "V3 calibrated model identity differs: "
                f"expected={calibration.calibration_hash}, observed={observed_hash}"
            )
        expected_offset = bridge.np.asarray(
            calibration.tcp_offset_tool0_m, dtype=float
        )
        if state.step5d_tcp_offset_tool0 is None:
            state.step5d_tcp_offset_tool0 = expected_offset
        elif not bridge.np.array_equal(
            bridge.np.asarray(state.step5d_tcp_offset_tool0, dtype=float),
            expected_offset,
        ):
            raise RuntimeError("V3 compact TCP calibration value differs")
        bridge.ensure_step5d_liveprep_control_runtime(state, args)

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
        "tcp_offset_tool0_m": [
            float(value) for value in state.step5d_tcp_offset_tool0
        ],
        "missing": [],
    }


def main(argv: list[str] | None = None) -> int:
    bridge_argv = list(sys.argv[1:] if argv is None else argv)
    ticket_text = os.environ.get(TICKET_ENV, "")
    if not ticket_text:
        print("refusing: STEP5D_V3_RUNTIME_TICKET is required", file=sys.stderr)
        return 24
    try:
        require_runtime_profile("control")
        release = load_runtime_release(ROOT)
        ticket = _strict_ticket(
            Path(ticket_text),
            bridge_argv,
            release_identity=release,
        )
        bridge = install_v3_seams(
            ticket,
            release_identity=release,
        )
    except (BridgeTicketError, ReleaseIdentityError, RuntimeGateError) as exc:
        print(f"refusing: {exc}", file=sys.stderr)
        return 24
    return int(bridge.main(bridge_argv))


if __name__ == "__main__":
    raise SystemExit(main())
