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
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from step5d_autotune_v3.runtime_calibration import bootstrap_stable_cuda_runtime


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_SRC = ROOT.parents[1] / "src" / "ur10e_experiment_runtime"
if str(RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(RUNTIME_SRC))

from ur10e_experiment_runtime.physical_prior import STEP5D_V3_PHYSICAL_PRIOR
from ur10e_experiment_runtime.identity import canonical_sha256
from ur10e_experiment_runtime.moving_sphere import MovingSphereKernel
from ur10e_experiment_runtime.stage_adapters import (
    STAGE_ID,
    Stage25ControllerProgressAdapter,
    frozen_step5d_path_reference,
)
from step5d_autotune_v3.arming import (
    ArmingContext,
    load_bridge_start_context,
    load_campaign_arming_context,
)

TICKET_ENV = "STEP5D_V3_RUNTIME_TICKET"
TICKET_SCHEMA = "step5d.autotune-v3/runtime-ticket-v3"
TICKET_SCOPE = "bridge_no_arm"
LIVE_STOPPING_BOUND_VALIDITY_DOMAIN = (
    "ur10e_step5d_autotune_v3_live_500hz_exact_controller_tp_transport_v1"
)
LIVE_MINIMUM_REACTION_LATENCY_S = 0.020


class BridgeTicketError(RuntimeError):
    pass


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


class CampaignArmingContextProvider:
    """Load the immutable campaign context off-loop and publish it once."""

    def __init__(
        self,
        path: Path,
        *,
        expected_static_identity: dict[str, Any],
        poll_interval_s: float = 0.1,
    ) -> None:
        if not path.is_absolute() or path.is_symlink():
            raise BridgeTicketError(
                "campaign arming context path must be absolute and non-symlinked"
            )
        if poll_interval_s <= 0.0:
            raise BridgeTicketError("arming-context poll interval must be positive")
        self.path = path
        self.expected_static_identity = dict(expected_static_identity)
        self.poll_interval_s = poll_interval_s
        self._context: ArmingContext | None = None
        self._error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._watch,
            name="step5d-v3-arming-context-loader",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def __call__(self) -> ArmingContext | None:
        if self._error is not None:
            raise BridgeTicketError(self._error)
        return self._context

    def _watch(self) -> None:
        while not self._stop.is_set():
            if self.path.exists() or self.path.is_symlink():
                try:
                    context = load_campaign_arming_context(
                        self.path,
                        expected_static_identity=self.expected_static_identity,
                    )
                except Exception as exc:
                    self._error = (
                        "campaign arming context failed closed: "
                        f"{type(exc).__name__}:{exc}"
                    )
                    return
                self._context = context
                return
            self._stop.wait(self.poll_interval_s)


def _apply_v3_arm_runtime(
    bridge: Any,
    args: Any,
    binding: Any,
    arming_context: Any | None,
    original_apply: Any,
) -> None:
    """Apply the real V3 control candidate once, at the ARM boundary."""

    if not isinstance(arming_context, ArmingContext):
        raise BridgeTicketError("V3 ARM requires a strict campaign arming context")
    if (
        binding.campaign_epoch != arming_context.campaign_epoch
        or binding.campaign_fingerprint != arming_context.campaign_fingerprint
    ):
        raise BridgeTicketError("V3 ARM binding differs from the arming context")
    original_apply(args, binding, arming_context)
    overlay = binding.trial_overlay
    if overlay is None:
        raise BridgeTicketError("V3 ARM requires a bound trial overlay")
    from step5d_autotune_contract import ForceCandidate
    from step5d_autotune_v3.runtime_profile import (
        DEFAULT_LAUNCH_PROFILE,
        load_launch_profile,
        normalize_trial_overlay,
    )

    launch_profile = load_launch_profile(DEFAULT_LAUNCH_PROFILE)
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
    args.step5d_controller_progress_adapter = Stage25ControllerProgressAdapter(
        physical_prior_sha256=STEP5D_V3_PHYSICAL_PRIOR.fingerprint
    )
    args.step5d_moving_sphere_reference_sha256 = (
        args.step5d_controller_progress_adapter.reference_sha256
    )
    args.step5d_moving_sphere_kernel = MovingSphereKernel(
        reference_sha256=str(
            launch_profile.document["moving_sphere_reference_sha256"]
        ),
        stopping_bound=arming_context.stopping_bound,
        required_validity_domain=LIVE_STOPPING_BOUND_VALIDITY_DOMAIN,
        minimum_reaction_latency_s=LIVE_MINIMUM_REACTION_LATENCY_S,
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
        "bridge_start_context",
        "campaign_arming_context_path",
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
        "tick_semantics_fingerprint",
        "timing_harness_fingerprint",
        "runtime_environment_fingerprint",
        "deployment_fingerprint",
        "orchestration_fingerprint",
        "release_basis_fingerprint",
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
    bridge_reference = payload["bridge_start_context"]
    if (
        not isinstance(bridge_reference, dict)
        or set(bridge_reference) != {"path", "sha256"}
        or not isinstance(bridge_reference["path"], str)
        or not bridge_reference["path"]
    ):
        raise BridgeTicketError("V3 runtime ticket bridge-start reference differs")
    bridge_context_path = Path(bridge_reference["path"])
    bridge_sha256 = bridge_reference["sha256"]
    if (
        not bridge_context_path.is_absolute()
        or bridge_context_path.is_symlink()
        or not bridge_context_path.is_file()
        or not isinstance(bridge_sha256, str)
        or _file_sha256(bridge_context_path) != bridge_sha256
    ):
        raise BridgeTicketError("V3 runtime ticket bridge-start digest differs")
    try:
        bridge_context = load_bridge_start_context(
            bridge_context_path,
            expected_static_identity=identity,
        )
    except Exception as exc:
        raise BridgeTicketError(f"V3 bridge-start context differs: {exc}") from exc
    if bridge_context.identity != identity:
        raise BridgeTicketError("V3 ticket identity differs from bridge-start context")
    arming_path_text = payload["campaign_arming_context_path"]
    if not isinstance(arming_path_text, str) or not arming_path_text:
        raise BridgeTicketError("V3 campaign arming context path differs")
    arming_path = Path(arming_path_text)
    if not arming_path.is_absolute() or arming_path.is_symlink():
        raise BridgeTicketError(
            "V3 campaign arming context path must be absolute and non-symlinked"
        )
    return payload


def install_v3_seams(
    arming_context_provider: Callable[[], ArmingContext | None] | None = None,
) -> Any:
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
    original_runtime = live.BridgeMailboxRuntime
    provider = arming_context_provider or (lambda: None)

    class V3BridgeMailboxRuntime(original_runtime):
        def __init__(
            self,
            path: Path,
            *,
            campaign_home_reference_path: Path | None = None,
        ) -> None:
            super().__init__(
                path,
                campaign_home_reference_path=campaign_home_reference_path,
                arming_context_provider=provider,
            )

    live.BridgeMailboxRuntime = V3BridgeMailboxRuntime

    import kunwei_rtde_bridge as bridge

    original_bridge_authorization = bridge.require_v29_live_bridge_authorization

    def v3_no_arm_bridge_authorization(
        args: Any,
        *,
        root: Path = ROOT,
    ) -> dict[str, Any] | None:
        if (
            args.bridge_profile == "step5d_strict_rnn_autotune_v1"
            and args.step5d_autotune_command_mailbox is not None
        ):
            return {
                "ok": True,
                "scope": TICKET_SCOPE,
                "release_stage_id": "step5d_strict_rnn_autotune_v3",
                "control_profile_provenance": args.bridge_profile,
                "motion_authorized": False,
            }
        return original_bridge_authorization(args, root=root)

    bridge.require_v29_live_bridge_authorization = v3_no_arm_bridge_authorization

    original_dict_writer = bridge.csv.DictWriter

    def v3_dict_writer(handle: Any, fieldnames: Sequence[str], *args: Any, **kwargs: Any) -> Any:
        fields = tuple(fieldnames)
        if "_step5d_stage25_echo_consumed" in fields:
            fields = compact_v3_fieldnames(fields)
            kwargs["extrasaction"] = "ignore"
        return original_dict_writer(handle, fieldnames=fields, *args, **kwargs)

    bridge.csv.DictWriter = v3_dict_writer

    original_path_reference = bridge.step5_contact_path_reference

    def v3_path_reference(
        pose_xy: tuple[float, float],
        elapsed_s: float,
        stage_id: str = STAGE_ID,
    ) -> dict[str, Any]:
        if stage_id == STAGE_ID:
            return frozen_step5d_path_reference(pose_xy, elapsed_s)
        return original_path_reference(pose_xy, elapsed_s, stage_id=stage_id)

    bridge.step5_contact_path_reference = v3_path_reference

    original_apply_arm_runtime = original_runtime._apply_arm_runtime

    def v3_apply_arm_runtime(
        args: Any,
        binding: Any,
        arming_context: Any | None = None,
    ) -> None:
        _apply_v3_arm_runtime(
            bridge,
            args,
            binding,
            arming_context,
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
    provider: CampaignArmingContextProvider | None = None
    try:
        ticket = _strict_ticket(Path(ticket_text), bridge_argv)
        provider = CampaignArmingContextProvider(
            Path(ticket["campaign_arming_context_path"]),
            expected_static_identity=dict(ticket["identity"]),
        )
        provider.start()
        bridge = install_v3_seams(provider)
    except BridgeTicketError as exc:
        if provider is not None:
            provider.close()
        print(f"refusing: {exc}", file=sys.stderr)
        return 24
    try:
        return int(bridge.main(bridge_argv))
    finally:
        provider.close()


if __name__ == "__main__":
    if os.environ.get(TICKET_ENV):
        bootstrap_stable_cuda_runtime()
    raise SystemExit(main())
