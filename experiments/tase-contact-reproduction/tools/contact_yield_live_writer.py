"""Native yield writer: R006LiveWriter + injection, contact-six identity.

Construction does not open sockets.  open() is the first transport edge.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Sequence

from contact_yield_live_contract import (
    READABLE_RUNTIME_IDENTITY,
    RUNTIME_PROTOCOL,
    YieldLiveContractError,
    YieldLiveIdentityContract,
    contact_home_binding,
    load_identity_contract,
    software_identity_limbs,
)
from contact_yield_live_path import LivePathRequest, parse_live_duration, require_live_path_request
from contact_yield_method_registry import CONTACT_PROGRAM, resolve_method
from contact_yield_protocol import QP_LIBRARY_PATH
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004.contracts import (
    CONTROLLER_READBACK_MAX_AGE_S,
    SCRIPT1_RECEIPT_MAX_AGE_S,
)
from step5d_autotune_v4_r004.identity import (
    ControllerReadbackReceipt,
    RuntimeIdentityEvidence,
)
from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE
from step5d_autotune_v4_r004.prerequisites import (
    load_controller_receipt,
    load_runtime_evidence,
)
from step5d_autotune_v4_r004.wire import CommandMode, SessionCommand
from step5d_autotune_v4_r005.live_adapter import R005_LIVE_ACK, _R005SessionIdentityGate
from step5d_autotune_v4_r005.runtime import Attempt
from step5d_autotune_v4_r006.live_adapter import (
    R006Candidate,
    R006LiveAdapterError,
    R006LiveWriter,
    R006MatureWriter,
    _R006ScopedRuntimeInjection,
    r006_runtime_path_reference,
)
from step5d_autotune_v4_r006.tp import RUNTIME_PROTOCOL as R006_RUNTIME_PROTOCOL
from step5d_autotune_v4_r012.live_host import PathEarlyEndController
from step5d_eoat_profiles import load_new_eoat_profile
from yield_native_route import create_native_yield_provider, create_native_yield_runtime


class YieldLiveWriterError(RuntimeError):
    """Native live writer admission or lifecycle failed closed."""


@dataclass(frozen=True)
class YieldLivePrerequisites:
    contract: YieldLiveIdentityContract
    controller: ControllerReadbackReceipt
    script1: Any
    runtime: RuntimeIdentityEvidence
    expected_triplet: Mapping[str, str]
    route_id: str
    session_epoch: int
    resident_session_id: str
    input_baseline_ledger_sha256: str
    software_baseline_n: tuple[float, ...]
    baseline_observed_at_s: float

    def validate(self, *, now_s: float) -> None:
        for stamp in (now_s, self.controller.observed_at_s, self.runtime.observed_at_s,
                      self.script1.observed_at_s, self.baseline_observed_at_s):
            if not math.isfinite(float(stamp)):
                raise YieldLiveWriterError("admission timestamp is not finite")
        for label, stamp, maximum in (
            ("runtime", self.runtime.observed_at_s, CONTROLLER_READBACK_MAX_AGE_S),
            ("baseline", self.baseline_observed_at_s, CONTROLLER_READBACK_MAX_AGE_S),
            ("Home", self.script1.observed_at_s, SCRIPT1_RECEIPT_MAX_AGE_S),
        ):
            if not 0 <= now_s - stamp <= maximum:
                raise YieldLiveWriterError(f"{label} evidence is stale or from the future")
        if self.controller.route_id != self.route_id:
            raise YieldLiveWriterError("controller receipt route differs")
        if self.controller.observed_at_s > now_s or self.runtime.observed_at_s > now_s:
            raise YieldLiveWriterError("native receipt timestamp is from the future")
        if now_s - self.controller.observed_at_s > CONTROLLER_READBACK_MAX_AGE_S:
            raise YieldLiveWriterError("controller readback receipt is stale")
        home_observed = float(getattr(self.script1, "observed_at_s", now_s))
        if now_s - home_observed > SCRIPT1_RECEIPT_MAX_AGE_S:
            raise YieldLiveWriterError("Home-start receipt is stale")
        if self.controller.program != CONTACT_PROGRAM:
            raise YieldLiveWriterError("controller receipt program is not step5d_contact_six_qp_v1")
        if self.controller.controller_target != self.contract.raw["script2"]["controller_target"]:
            raise YieldLiveWriterError("controller receipt target is not the contact-six package")
        if dict(self.expected_triplet) != dict(self.contract.triplet):
            raise YieldLiveWriterError("controller triplet is not the local contact-six package")
        if {
            "script": self.controller.script_sha256,
            "txt": self.controller.txt_sha256,
            "urp": self.controller.urp_sha256,
        } != dict(self.contract.triplet):
            raise YieldLiveWriterError("controller readback triplet differs from the native package")
        hi, lo = software_identity_limbs(self.contract)
        if (
            self.controller.runtime_protocol != RUNTIME_PROTOCOL
            or self.runtime.runtime_protocol != RUNTIME_PROTOCOL
            or self.runtime.script_sha256 != self.controller.script_sha256
            or self.runtime.program != CONTACT_PROGRAM
            or not self.runtime.program_running
            or not self.runtime.uninterrupted
            or self.runtime.session_epoch != self.session_epoch
            or self.runtime.resident_session_id != self.resident_session_id
            or self.controller.runtime_digest_hi != hi
            or self.controller.runtime_digest_lo != lo
            or self.runtime.runtime_digest_hi != hi
            or self.runtime.runtime_digest_lo != lo
        ):
            raise YieldLiveWriterError("native resident runtime identity differs")
        if self.controller.safety_mode != "NORMAL" or not self.controller.stationary:
            raise YieldLiveWriterError("native entry is not stationary Safety NORMAL")
        profile = load_new_eoat_profile()
        self.controller.validate_eoat_readback(
            payload_kg=profile.payload_kg,
            payload_cog_m=profile.cog_m,
            tcp_offset_m_rad=profile.controller_tcp_m_rad,
        )


def _require_run_file(run_dir: Path, name: str) -> Path:
    path = Path(run_dir) / name
    if path.is_symlink() or not path.is_file():
        raise YieldLiveWriterError(f"missing admission receipt: {path}")
    return path


def load_run_dir_receipts(
    run_dir: Path,
    *,
    contract: YieldLiveIdentityContract,
    route_id: str,
    attempt_id: str,
    now_s: float,
) -> tuple[YieldLivePrerequisites, Any]:
    del attempt_id
    controller = load_controller_receipt(_require_run_file(run_dir, "controller_receipt.json"))
    runtime = load_runtime_evidence(_require_run_file(run_dir, "runtime_evidence.json"))
    home_path = Path(run_dir) / "home_start_receipt.json"
    script1_path = Path(run_dir) / "script1_receipt.json"
    if home_path.is_file() and not home_path.is_symlink():
        home_document = json.loads(home_path.read_text(encoding="utf-8"))
        if not isinstance(home_document, Mapping):
            raise YieldLiveWriterError("Home-start receipt is not an object")
        if (home_document.get("stationary") is not True
            or home_document.get("safety_mode") != "NORMAL"
            or home_document.get("script_sha256") != contract.script1_sha256["script"]
            or home_document.get("eoat_identity_sha256") != contract.eoat_sha256):
            raise YieldLiveWriterError("Home receipt observation or package identity differs")
        binding = contact_home_binding(
            contract=contract,
            final_pose=home_document["final_pose"],
            final_q=home_document["final_q"],
            observed_at_s=float(home_document["observed_at_s"]),
            receipt_sha256=str(home_document["receipt_sha256"]),
        )
        script1 = binding.entry_receipt
    elif script1_path.is_file() and not script1_path.is_symlink():
        raise YieldLiveWriterError(
            "script1_receipt.json is the RNN Script1 name; contact Home must be home_start_receipt.json"
        )
    else:
        raise YieldLiveWriterError(f"missing admission receipt: {home_path}")
    baseline_path = _require_run_file(run_dir, "software_baseline_receipt.json")
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    values = tuple(float(x) for x in baseline["mean_wrench_n_nm"])
    if len(values) != 6 or not all(math.isfinite(x) for x in values):
        raise YieldLiveWriterError("software baseline requires six finite SI values")
    if (baseline.get("stationary") is not True
        or baseline.get("no_contact") is not True
        or baseline.get("eoat_identity_sha256") != contract.eoat_sha256):
        raise YieldLiveWriterError("software baseline context differs")
    capture_name = baseline["capture_file"]
    if not isinstance(capture_name, str) or Path(capture_name).name != capture_name:
        raise YieldLiveWriterError("baseline capture must be a run-local file")
    capture_path = _require_run_file(run_dir, capture_name)
    if hashlib.sha256(capture_path.read_bytes()).hexdigest() != baseline["capture_sha256"]:
        raise YieldLiveWriterError("software baseline capture digest differs")
    prerequisites = YieldLivePrerequisites(
        contract=contract,
        controller=controller,
        script1=script1,
        runtime=runtime,
        expected_triplet=dict(contract.triplet),
        route_id=route_id,
        session_epoch=int(runtime.session_epoch),
        resident_session_id=str(runtime.resident_session_id),
        input_baseline_ledger_sha256=hashlib.sha256(baseline_path.read_bytes()).hexdigest(),
        software_baseline_n=values,
        baseline_observed_at_s=float(baseline["observed_at_s"]),
    )
    prerequisites.validate(now_s=now_s)
    return prerequisites, binding


def _prewarm_qp(qp_library: Path) -> None:
    import numpy as np
    from contact_qp import NativeContactQp

    if qp_library.is_symlink() or not qp_library.is_file():
        raise YieldLiveWriterError(f"native QP library is missing: {qp_library}")
    solver = NativeContactQp(qp_library, deadline_s=None)
    result = solver.solve(
        np.eye(6, dtype=float),
        np.zeros(6, dtype=float),
        np.full(6, -0.01, dtype=float),
        np.full(6, 0.01, dtype=float),
    )
    if not all(math.isfinite(float(value)) for value in result.qdot):
        raise YieldLiveWriterError("QP prewarm produced a nonfinite command")


from step5d_autotune_contract import ExecutionProfile


@dataclass(frozen=True)
class NativeYieldExecutionProfile(ExecutionProfile):
    """Native TP values, separate from the old RNN profile enum."""
    def __post_init__(self):
        from dataclasses import asdict
        expected = asdict(R004_MOTION_PROFILE.execution_profile)
        expected.update(profile_id="yield-native-live-v1", qdot_cap_rad_s=.05,
                        tp_speedj_accel_rad_s2=5., bridge_angular_limit_rad_s=.05)
        if asdict(self) != expected:
            raise YieldLiveWriterError("native execution profile differs from bound TP limits")


def native_motion_profile():
    """Bind the mature typed seam to the existing native task/TP limits.

    The RNN profile's 2.5 rad/s and 20 rad/s2 are not this TP's limits.
    Native outer-loop caps are 10 mm/s tangent, 3 mm/s normal, 0.05 rad/s
    attitude; the requested figure eight itself peaks at 8.94 mm/s.
    """
    import copy
    from dataclasses import asdict
    profile = copy.copy(R004_MOTION_PROFILE)
    values = {"xy_path_speed_m_s": .01, "total_linear_cap_m_s": math.hypot(.01,.003),
              "normal_linear_cap_m_s": .003, "angular_cap_rad_s": .05,
              "qdot_cap_rad_s": .05, "tp_acceleration_rad_s2": 5.}
    for name, value in values.items():
        object.__setattr__(profile, name, value)
    fields = asdict(profile.execution_profile)
    fields.update(profile_id="yield-native-live-v1", qdot_cap_rad_s=.05,
                  tp_speedj_accel_rad_s2=5., bridge_angular_limit_rad_s=.05)
    object.__setattr__(profile, "execution_profile", NativeYieldExecutionProfile(**fields))
    return profile


def build_native_yield_owner(
    *,
    method: str,
    duration: Any | None,
    command: str,
    prerequisites: YieldLivePrerequisites,
    home_binding: Any,
    qp_library: Path | str | None = None,
    authority_root: Path,
    route_id: str,
    attempt_id: str,
    controller_host: str | None = None,
    kunwei_host: str | None = None,
    kunwei_port: int | None = None,
    controller_transport: Any | None = None,
    kunwei_transport: Any | None = None,
    wall_clock: Callable[[], float] | None = None,
    mono_clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    path_sample_sink: Callable[..., Any] | None = None,
) -> tuple[R006MatureWriter, Any, Any, LivePathRequest | None]:
    if R006_RUNTIME_PROTOCOL != RUNTIME_PROTOCOL:
        raise YieldLiveWriterError("R006 runtime protocol is not 606006")
    record = resolve_method(method)
    request = None if command == "qualify" else parse_live_duration(duration)
    library = Path(qp_library) if qp_library is not None else QP_LIBRARY_PATH
    _prewarm_qp(library)
    import numpy as np

    pose = home_binding.profile.pose
    rotation = rotvec_to_matrix(np.array(pose[3:], dtype=float))
    runtime, binding = create_native_yield_runtime(
        method=record.name,
        qp_library=library,
        anchor_m=pose[:3],
        task_basis=rotation,
        approach_inward_base=rotation[:, 2],
        home_observations={"home_pose": pose, "home_q": home_binding.entry_receipt.final_q},
    )
    try:
        provider = create_native_yield_provider(runtime=runtime, binding=binding)
        if request is not None:
            provider.live_path_request = require_live_path_request(request)

        def factory(**_ignored: Any):
            return provider

        kwargs: dict[str, Any] = {
            "authority_root": Path(authority_root),
            "route_id": route_id,
            "attempt_id": attempt_id,
            "identity_namespace": "r006",
            "runtime_protocol": RUNTIME_PROTOCOL,
            "canonical_runtime_only": True,
            "path_sample_sink": path_sample_sink,
            "software_baseline_n": prerequisites.software_baseline_n,
        }
        if controller_transport is not None:
            kwargs["controller_transport"] = controller_transport
        else:
            kwargs["controller_host"] = controller_host
        if kunwei_transport is not None:
            kwargs["kunwei_transport"] = kunwei_transport
        else:
            kwargs["kunwei_host"] = kunwei_host
            kwargs["kunwei_port"] = kunwei_port
        if wall_clock is not None:
            kwargs["wall_clock"] = wall_clock
        if mono_clock is not None:
            kwargs["mono_clock"] = mono_clock
        if sleep is not None:
            kwargs["sleep"] = sleep
        writer = NativeYieldLiveWriter(prerequisites, **kwargs)
        from step5d_autotune_v4_r012.register_transport import install_r012_register_transport
        controller_transport = install_r012_register_transport(writer)
        writer.live_path_request = request
        writer.session.identity = _R005SessionIdentityGate(prerequisites.contract)
        writer._readable_runtime_identity = READABLE_RUNTIME_IDENTITY
        writer._r013_path_early_end_controller = PathEarlyEndController(
            writer=controller_transport
        )
        injection = _R006ScopedRuntimeInjection(
            motion_profile=native_motion_profile(),
            path_reference=r006_runtime_path_reference,
            contact_command_provider_factory=factory,
            home_binding=home_binding,
        )
        mature = R006MatureWriter(writer, injection=injection)
    except Exception:
        runtime.close()
        raise
    return mature, runtime, provider, request



class NativeYieldLiveWriter(R006LiveWriter):
    """The same sole writer, with native raw-wrench limits before compensation.

    This seam covers HOLD, approach/search and PATH; the proposal kernel alone
    cannot guard the pre-contact phases. Raw limits include gravity and bias.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.raw_observations = []
        self.command_observations = []
        self.robot_observations = []

    @staticmethod
    def _record(buffer, value):
        if len(buffer) >= 150000:
            raise YieldLiveWriterError("attempt evidence capacity reached")
        buffer.append(value)

    def _send_packet(self, sensor, **kwargs):
        proposed = tuple(float(x) for x in kwargs.get("proposed_qdot", (0.,)*6))
        if len(proposed) != 6 or any(not math.isfinite(x) or abs(x) > .05 for x in proposed):
            raise YieldLiveWriterError("native joint velocity limit exceeded (0.05 rad/s)")
        packet = super()._send_packet(sensor, **kwargs)
        self._record(self.command_observations, (self._mono_clock(), packet))
        request = getattr(self, "live_path_request", None)
        phase = kwargs.get("reference_phase")
        phase_time = kwargs.get("reference_time_s")
        end = self._r013_path_early_end_controller
        if (request is not None and request.kind == "diagnostic"
            and phase == "path" and phase_time is not None
            and phase_time >= request.path_duration_s and not end.requested):
            if not end.request_early_end(self._ordinal):
                raise YieldLiveWriterError("diagnostic PATH-end request rejected")
        return packet

    def _poll_checked(self, *args, **kwargs):
        output = super()._poll_checked(*args, **kwargs)
        if self._last_poll_was_fresh:
            self._record(self.robot_observations, output)
        return output

    def _sensor_packet(self, *, raw, observed_at_s):
        if raw is not None:
            values = tuple(float(x) for x in raw)
            if len(values) != 6 or not all(math.isfinite(x) for x in values):
                raise YieldLiveWriterError("native raw wrench is not finite length six")
            if math.hypot(*values[:3]) >= 20.0 or math.hypot(*values[3:]) >= 2.0:
                raise YieldLiveWriterError("native raw wrench limit exceeded (20 N / 2 Nm)")
        sensor = super()._sensor_packet(raw=raw, observed_at_s=observed_at_s)
        if not self._stopped:
            self._record(self.raw_observations, {
                "received_monotonic_s": observed_at_s,
                "host_use_monotonic_s": self._mono_clock(),
                "packet_sequence": self._packet_sequence,
                "raw_wrench_n_nm": None if raw is None else tuple(raw),
                "corrected_wrench_n_nm": sensor.wrench,
                "sensor_fresh": sensor.sensor_fresh,
            })
        return sensor


def native_attempt(*, command: str, epoch: int, sequence: int) -> Attempt:
    kind = "QUALIFICATION" if command == "qualify" else "BOOTSTRAP_PD"
    return Attempt(
        epoch=epoch,
        attempt_sequence=sequence,
        candidate=R006Candidate(),
        kind=kind,
        dispatch_sequence=sequence,
        request_uid=f"yield-live-{command}-{sequence}",
    )



def stop_and_confirm(writer, *, timeout_s=1.0):
    """Request STOP, then independently observe fresh stationary TP STOP state.

    Legacy send_safe_stop deliberately swallows send errors. Its return alone
    therefore cannot be used as an acknowledgement. No new command channel is
    opened here. A missing transport or observation is an unconfirmed stop.
    """
    requested_at = writer._mono_clock()
    prior = getattr(writer, '_last_output', None)
    prior_timestamp = None if prior is None else prior.timestamp
    writer.stop('operator_stop')
    transport = writer._controller_transport
    if transport is None or not writer._opened:
        return {'stop_requested': True, 'stopped': False,
                'reason': 'no_open_transport_for_confirmation'}
    deadline = requested_at + timeout_s
    while writer._mono_clock() <= deadline:
        output = transport.poll_output(wait_s=0.002)
        now = writer._mono_clock()
        if output is not None:
            received = output.received_monotonic_s
            fresh = (received is not None and requested_at <= received <= now
                     and now - received < 0.080
                     and (prior_timestamp is None or output.timestamp > prior_timestamp))
            identity = (output.integer_echoes.get(32) == RUNTIME_PROTOCOL
                        and (output.integer_echoes.get(33), output.integer_echoes.get(34))
                        == READABLE_RUNTIME_IDENTITY)
            if (fresh and identity and output.stationary and output.safety_normal
                and output.integer_echoes.get(26) == 90
                and output.integer_echoes.get(28) == 4):
                return {'stop_requested': True, 'stopped': True,
                        'controller_timestamp': output.timestamp,
                        'received_monotonic_s': received,
                        'state': 90, 'reason': 4,
                        'actual_qd': list(output.qd_rad_s),
                        'actual_tcp_speed': list(output.tcp_speed_m_s_rad_s)}
        writer._sleep(.002)
    return {'stop_requested': True, 'stopped': False,
            'reason': 'fresh_stationary_stop_confirmation_timeout'}
