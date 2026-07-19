#!/usr/bin/env python3
"""Ticket-gated V3 wrapper around the SHA-governed production bridge.

The wrapper changes three V3 integration seams: immutable mailbox reads are
identity-cached, the V1 control profile accepts the separately fingerprinted V3
TP identity, and startup consumes the compact hash-bound calibration artifact
instead of an ignored 19 MB historical CSV.  It never creates a campaign runner
or an ARM command.
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
from typing import Any, Sequence

from step5d_autotune_v3.runtime_calibration import bootstrap_stable_cuda_runtime


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from ur10e_experiment_runtime.identity import canonical_sha256
from ur10e_experiment_runtime.moving_sphere import MovingSphereKernel
from ur10e_experiment_runtime.stage_adapters import TRAJECTORY_PARAMETERS_SHA256

TICKET_ENV = "STEP5D_V3_RUNTIME_TICKET"
TICKET_SCHEMA = "step5d.autotune-v3/runtime-ticket-v2"
TICKET_SCOPE = "live_continuous_campaign"


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
        *(f"ur_output_double_register_{index}" for index in range(36, 39)),
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
        if snapshot.state is TpLoopState.WAIT_ACK:
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
    original_apply: Any,
) -> None:
    """Apply the real V3 control candidate once, at the ARM boundary."""

    original_apply(args, binding)
    overlay = binding.trial_overlay
    if overlay is None:
        raise BridgeTicketError("V3 ARM requires a bound trial overlay")
    from step5d_autotune_contract import ForceCandidate
    from step5d_autotune_v3.runtime_profile import (
        DEFAULT_LAUNCH_PROFILE,
        load_launch_profile,
        normalize_trial_overlay,
    )

    normalized = normalize_trial_overlay(
        overlay,
        profile=load_launch_profile(DEFAULT_LAUNCH_PROFILE),
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
    args.step5d_physical_prior_reaction_normal_b = (
        STEP5D_V3_PHYSICAL_PRIOR.reaction_normal_b
    )
    args.step5d_physical_prior_approach_axis_b = (
        STEP5D_V3_PHYSICAL_PRIOR.approach_axis_b
    )
    args.step5d_physical_prior_precontact_rotvec_rad = (
        STEP5D_V3_PHYSICAL_PRIOR.precontact_rotvec_rad
    )
    args.step5d_physical_prior_identity_payload = (
        STEP5D_V3_PHYSICAL_PRIOR.identity_payload()
    )
    args.step5d_physical_prior_sha256 = STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    args.step5d_physical_prior_binding_valid = (
        canonical_sha256(args.step5d_physical_prior_identity_payload)
        == args.step5d_physical_prior_sha256
    )
    args.step5d_live_normal_load_gate_n = STEP5D_V3_PHYSICAL_PRIOR.load_gate_n
    args.step5d_live_normal_load_gate_dwell_s = (
        STEP5D_V3_PHYSICAL_PRIOR.load_gate_dwell_s
    )
    args.bridge_normal_max_rate_rad_s = (
        STEP5D_V3_PHYSICAL_PRIOR.normal_rate_limit_rad_s
    )
    args.step4e_normal_max_rate_rad_s = (
        STEP5D_V3_PHYSICAL_PRIOR.normal_rate_limit_rad_s
    )
    args.step5d_moving_sphere_reference_sha256 = canonical_sha256(
        {
            "schema": "step5d.moving-sphere-reference/v1",
            "trajectory_parameters_sha256": TRAJECTORY_PARAMETERS_SHA256,
            "physical_prior_sha256": STEP5D_V3_PHYSICAL_PRIOR.fingerprint,
        }
    )
    # No certified reaction/braking artifact exists in this offline tranche.
    # Stage25 therefore fails closed until attended evidence supplies one.
    args.step5d_moving_sphere_kernel = MovingSphereKernel(
        reference_sha256=args.step5d_moving_sphere_reference_sha256,
        stopping_bound=None,
    )


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
        "launch_id",
        "scope",
        "identity",
        "launch_profile_fingerprint",
        "trial_overlay_fingerprint",
        "release_stage_id",
        "control_profile_id",
        "tp_program_id",
        "campaign_binding",
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
        "release_stage_id": "step5d_strict_rnn_autotune_v3",
        "control_profile_id": "step5d_strict_rnn_autotune_v1",
        "tp_program_id": "step5d_strict_rnn_autotune_v3",
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
    identity = payload["identity"]
    if not isinstance(identity, dict) or set(identity) != {
        "contract_sha256",
        "control_fingerprint",
        "orchestration_fingerprint",
    }:
        raise BridgeTicketError("V3 runtime ticket identity differs")
    for value in (
        *identity.values(),
        payload["launch_profile_fingerprint"],
        payload["trial_overlay_fingerprint"],
    ):
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise BridgeTicketError("V3 runtime ticket fingerprint differs")
    binding = payload["campaign_binding"]
    if (
        not isinstance(binding, dict)
        or set(binding)
        != {
            "campaign_id",
            "campaign_epoch",
            "candidate_plan_revision",
            "candidate_plan_sha256",
            "trial_overlay_plan_sha256",
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
    for key in ("candidate_plan_sha256", "trial_overlay_plan_sha256"):
        value = binding[key]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise BridgeTicketError("live runtime ticket plan fingerprint differs")
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
    live.BridgeTrialCsvRotator = V3AsyncBridgeTrialCsvRotator

    import kunwei_rtde_bridge as bridge

    original_dict_writer = bridge.csv.DictWriter

    def v3_dict_writer(handle: Any, fieldnames: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        fields = tuple(fieldnames)
        if "_step5d_stage25_echo_consumed" in fields:
            fields = compact_v3_fieldnames(fields)
            kwargs["extrasaction"] = "ignore"
        return original_dict_writer(handle, fieldnames=fields, *args, **kwargs)

    bridge.csv.DictWriter = v3_dict_writer

    original_apply_arm_runtime = live.BridgeMailboxRuntime._apply_arm_runtime

    def v3_apply_arm_runtime(args: Any, binding: Any) -> None:
        _apply_v3_arm_runtime(
            bridge,
            args,
            binding,
            original_apply_arm_runtime,
        )

    live.BridgeMailboxRuntime._apply_arm_runtime = staticmethod(v3_apply_arm_runtime)

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
