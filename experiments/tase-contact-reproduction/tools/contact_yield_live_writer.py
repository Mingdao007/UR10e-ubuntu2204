"""Native yield writer: R006LiveWriter + injection, contact-six identity.

Construction does not open sockets.  open() is the first transport edge.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import threading
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
from contact_yield_protocol import PATH_SEAM_CONTINUATION_S, QP_LIBRARY_PATH
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
from step5d_autotune_v4_r004_live_writer import RUNTIME_OUTPUT_MAX_AGE_S, LiveWriterError
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


def load_software_baseline(run_dir: Path, contract, *, now_s: float):
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
    observed = float(baseline["observed_at_s"])
    if not 0 <= now_s-observed <= CONTROLLER_READBACK_MAX_AGE_S:
        raise YieldLiveWriterError("software baseline is stale or from the future")
    return baseline_path, baseline, values


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
    baseline_path, baseline, values = load_software_baseline(run_dir, contract, now_s=now_s)
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


PREWARM_PURPOSE = (
    "transport-free warmup of the exact live provider/runtime before endpoints/ARM; "
    "not physical qualification or measured timing evidence"
)
_PREWARM_COMMAND_COUNT = 4


def _capture_freshness(tracker) -> dict[str, Any]:
    return {
        "_ages": list(tracker._ages),
        "_ordered_ages": list(tracker._ordered_ages),
        "_counts": dict(tracker._counts),
        "_held_streak": tracker._held_streak,
        "_longest_hold_samples": tracker._longest_hold_samples,
        "_longest_hold_s": tracker._longest_hold_s,
        "_stale_stop_count": tracker._stale_stop_count,
        "_geometric_latency_reject_count": tracker._geometric_latency_reject_count,
    }


def _restore_freshness(tracker, captured: Mapping[str, Any]) -> None:
    tracker._ages = list(captured["_ages"])
    tracker._ordered_ages = list(captured["_ordered_ages"])
    tracker._counts = dict(captured["_counts"])
    tracker._held_streak = captured["_held_streak"]
    tracker._longest_hold_samples = captured["_longest_hold_samples"]
    tracker._longest_hold_s = captured["_longest_hold_s"]
    tracker._stale_stop_count = captured["_stale_stop_count"]
    tracker._geometric_latency_reject_count = captured["_geometric_latency_reject_count"]


def _prewarm_observation(*, pose, q, monotonic_s):
    """Synthetic, transport-free observation. Never published or recorded as evidence."""
    from types import SimpleNamespace

    from step5d_autotune_v4_r004.wire import SensorPacket

    profile = load_new_eoat_profile()
    output = SimpleNamespace(
        received_monotonic_s=float(monotonic_s),
        timestamp=1000.0 + float(monotonic_s),
        safety_mode=1,
        safety_normal=True,
        tcp_offset_m_rad=tuple(float(value) for value in profile.controller_tcp_m_rad),
        payload_kg=float(profile.payload_kg),
        payload_cog_m=tuple(float(value) for value in profile.cog_m),
        q_rad=tuple(float(value) for value in q),
        qd_rad_s=(0.0,) * 6,
        tcp_pose_m_rad=tuple(float(value) for value in pose),
        tcp_speed_m_s_rad_s=(0.0,) * 6,
    )
    sensor = SensorPacket(
        normal_load_n=1.0,
        force_norm_n=1.0,
        heartbeat=1.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.0,
        wrench=(0.0, 0.0, -1.0, 0.0, 0.0, 0.0),
        filtered_normal_n=1.0,
        observed_at_s=float(monotonic_s),
    )
    return output, sensor


def _prewarm_native_provider(provider, *, pose, q) -> dict[str, Any]:
    """Exercise the exact provider/runtime command path, then restore all application state.

    Temporary deadline disable applies only inside this function. Production
    1.5 ms / 1 ms / 4 ms / 80 ms policies are not changed for live use.
    """
    import copy

    runtime = provider.runtime
    qp = runtime.controller.qp.qp
    before = copy.deepcopy(provider.snapshot())
    freshness_before = _capture_freshness(runtime.freshness)
    runtime_deadline = runtime.deadline_s
    qp_deadline = qp.deadline_s
    dt_s = float(runtime.controller.dt_s)
    record: dict[str, Any] = {
        "purpose": PREWARM_PURPOSE,
        "command_count": 0,
        "command_times_s": [],
        "first_command_s": None,
        "last_command_s": None,
        "wall_s": None,
        "state_changed_during": False,
        "deadline_restored": False,
    }
    started = time.perf_counter()
    try:
        runtime.deadline_s = None
        qp.deadline_s = None
        seed_law = list(before["runtime"]["controller"]["law22"]["values"])
        for index in range(_PREWARM_COMMAND_COUNT):
            now = dt_s * float(index + 1)
            output, sensor = _prewarm_observation(pose=pose, q=q, monotonic_s=now)
            tick_started = time.perf_counter()
            command = provider.command(
                output=output,
                sensor=sensor,
                monotonic_s=now,
                actual_dt_s=dt_s,
                mode="baseline",
                internal_setpoint_n=1.0,
            )
            elapsed = time.perf_counter() - tick_started
            record["command_times_s"].append(elapsed)
            record["command_count"] = index + 1
            if not all(math.isfinite(float(value)) for value in command.qdot):
                raise YieldLiveWriterError("native provider prewarm produced a nonfinite command")
            if index == 0:
                record["state_changed_during"] = (
                    list(runtime.controller.law.snapshot().values) != seed_law
                )
        record["first_command_s"] = record["command_times_s"][0]
        record["last_command_s"] = record["command_times_s"][-1]
        record["wall_s"] = time.perf_counter() - started
    finally:
        runtime.deadline_s = runtime_deadline
        qp.deadline_s = qp_deadline
        provider.restore(before)
        _restore_freshness(runtime.freshness, freshness_before)
        record["deadline_restored"] = (
            runtime.deadline_s == runtime_deadline and qp.deadline_s == qp_deadline
        )
        provider.prewarm_record = record
    return record


def _prewarm_tase_provider(provider, *, pose, q):
    """Warm the real local RNN at Home; restore control and observation state."""
    import copy
    state = provider.snapshot()
    freshness = copy.deepcopy(vars(provider.freshness))
    times = []
    try:
        for index in range(16):
            now = .002*(index+1)
            output, sensor = _prewarm_observation(pose=pose, q=q, monotonic_s=now)
            start = time.perf_counter()
            command = provider.command(output=output, sensor=sensor, monotonic_s=now,
                actual_dt_s=.002, mode="baseline", internal_setpoint_n=1.)
            times.append(time.perf_counter()-start)
            if not all(math.isfinite(x) and abs(x)<=.05 for x in command.qdot):
                raise YieldLiveWriterError("TASE prewarm command exceeds current limits")
    finally:
        provider.restore(state)
        provider.freshness.__dict__.update(freshness)
    provider.prewarm_record = {"purpose":"transport-free mature TASE RNN warmup",
        "command_count":len(times),"command_times_s":times,
        "solver_profile":provider.solver_profile.as_dict(),"state_restored":True}
    return provider.prewarm_record


def _install_command_timing(provider):
    """Bounded real-clock diagnostics; never changes observations or command state."""
    setattr(provider, "_command_timing_installed", True)
    timeline = provider.command_timeline = []
    active = [None]
    original_command = provider.command
    runtime_method_name = (
        "step" if callable(getattr(provider.runtime, "step", None)) else "command"
    )
    original_runtime = getattr(provider.runtime, runtime_method_name, None)

    def measured_provider_call(original, operation, kwargs):
        if len(timeline) >= 512:
            return original(**kwargs)
        row = {
            "operation": operation,
            "provider_enter_ns": time.monotonic_ns(),
            "provider_enter_s": time.monotonic(),
            "sensor_received_s": kwargs["sensor"].observed_at_s,
            "robot_received_s": kwargs["output"].received_monotonic_s,
            "host_use_s": kwargs["monotonic_s"],
            "actual_dt_s": kwargs["actual_dt_s"],
        }
        active[0] = row
        try:
            return original(**kwargs)
        except BaseException as exc:
            row["error"] = type(exc).__name__
            raise
        finally:
            row["provider_exit_ns"] = time.monotonic_ns()
            row["provider_exit_s"] = time.monotonic()
            active[0] = None
            timeline.append(row)

    def measured_runtime(*args, **kwargs):
        row = active[0]
        if row is not None:
            row["runtime_enter_ns"] = time.monotonic_ns()
            row["runtime_enter_s"] = time.monotonic()
        try:
            return original_runtime(*args, **kwargs)
        finally:
            if row is not None:
                row["runtime_exit_ns"] = time.monotonic_ns()
                row["runtime_exit_s"] = time.monotonic()

    def measured_command(**kwargs):
        return measured_provider_call(original_command, "command", kwargs)

    original_pause = getattr(provider, "pause", None)

    def measured_pause(**kwargs):
        return measured_provider_call(original_pause, "pause", kwargs)

    original_late_hold = getattr(provider, "hold_pre_path_late_cycle", None)

    def measured_late_hold(**kwargs):
        return measured_provider_call(original_late_hold, "late_hold", kwargs)

    if callable(original_runtime):
        setattr(provider.runtime, runtime_method_name, measured_runtime)
    provider.command = measured_command
    if callable(original_pause):
        provider.pause = measured_pause
    if callable(original_late_hold):
        provider.hold_pre_path_late_cycle = measured_late_hold


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
    attitude; the requested 80 x 20 mm figure eight itself peaks at 4.47 mm/s.
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
    parameter_file: Path | str | None = None,
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
    capture_command_timing: bool = False,
) -> tuple[R006MatureWriter, Any, Any, LivePathRequest | None]:
    if type(capture_command_timing) is not bool:
        raise YieldLiveWriterError("capture_command_timing must be bool")
    if R006_RUNTIME_PROTOCOL != RUNTIME_PROTOCOL:
        raise YieldLiveWriterError("R006 runtime protocol is not 606006")
    record = resolve_method(method)
    request = None if command == "qualify" else parse_live_duration(duration)
    library = Path(qp_library) if qp_library is not None else QP_LIBRARY_PATH
    if record.family != "tase_mature":
        _prewarm_qp(library)
    import numpy as np

    pose = home_binding.profile.pose
    rotation = rotvec_to_matrix(np.array(pose[3:], dtype=float))
    from contact_yield_task_frame import require_figure8_home
    task_basis = require_figure8_home(pose)
    tase = record.family == "tase_mature"
    if tase:
        from tase_contact_provider import (
            TaseContactProvider,
            current_model_binding,
            load_tase_outer_config,
        )
        from step5d_autotune_v4_r014.solver_profile import LEGACY_R1
        outer_config, parameter_binding = load_tase_outer_config(parameter_file)
        provider = TaseContactProvider(contract=current_model_binding(), candidate=R006Candidate(),
            motion_profile=native_motion_profile(), home_pose=pose, solver_profile=LEGACY_R1,
            outer_loop_config=outer_config, parameter_binding=parameter_binding,
            protocol_id=(None if request is None else request.protocol_id))
        runtime = provider
    else:
        runtime, binding = create_native_yield_runtime(
            method=record.name,
            qp_library=library,
            anchor_m=pose[:3],
            task_basis=task_basis,
            approach_inward_base=rotation[:, 2],
            home_observations={"home_pose": pose, "home_q": home_binding.entry_receipt.final_q},
        )
    try:
        if not tase:
            provider = create_native_yield_provider(runtime=runtime, binding=binding)
        if request is not None:
            provider.live_path_request = require_live_path_request(request)
        if tase:
            _prewarm_tase_provider(provider, pose=pose, q=home_binding.entry_receipt.final_q)
        else:
            _prewarm_native_provider(provider, pose=pose, q=home_binding.entry_receipt.final_q)
        # Command timing wraps every provider call with several wall-clock
        # reads and a list append. Keep it available for offline replay and
        # explicit diagnostics, but never pay that cost on the live 500 Hz
        # path by default.
        if capture_command_timing:
            _install_command_timing(provider)

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
        from contact_yield_transport import install_native_yield_transport
        controller_transport = install_native_yield_transport(writer)
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

    @staticmethod
    def _new_qualification_timing_collector():
        # Reuse the collector already used by the successful R013 live route.
        # Same distinct echoes and 460 Hz threshold; no global runtime patch.
        from step5d_autotune_v4_r013.live_runtime import R013LightweightTimingEvidenceCollector
        return R013LightweightTimingEvidenceCollector()

    @staticmethod
    def _next_publish_deadline(previous_s: float, period_s: float, now_s: float) -> float:
        """Poll the latest due slot without adding a cycle after a small overrun.

        No queued command is replayed: execute_attempt still requires a fresh
        RTDE frame before control/publish and passes the real elapsed interval
        through the unchanged admission checks. Whole obsolete slots are skipped.
        """
        deadline = previous_s + period_s
        if deadline <= now_s:
            deadline += math.floor((now_s - deadline) / period_s) * period_s
        return deadline

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.raw_observations = []
        self.command_observations = []
        self.robot_observations = []
        self.admission_robot_observations = []
        self.rejected_robot_observations = []
        self._service_mode = False
        self._service_observations: dict[str, list[Any]] = {}
        self._service_lock = threading.Lock()
        self._first_output_error = None
        self._host_path_publish_count = 0

    def recovery_lifecycle(self, *, home_transition=None, ownership_registry=None):
        """Expose the offline recovery seam on this existing sole writer.

        The returned object only records identity and recovery decisions.  It
        does not replace this writer, open a transport, unlock, or issue Home.
        """
        from contact_yield_recovery_contract import RecoveryLifecycle

        return RecoveryLifecycle.from_writer(
            self,
            home_transition=home_transition,
            ownership_registry=ownership_registry,
        )

    def _record(self, buffer, value):
        # Attempts and idle service have separate bounded evidence buffers.
        if getattr(self, "_service_mode", False):
            labels = {
                id(self.raw_observations): "raw_sensor",
                id(self.command_observations): "published_packets",
                id(self.robot_observations): "robot_frames",
                id(self.admission_robot_observations): "admission_robot_frames",
                id(self.rejected_robot_observations): "rejected_robot_frames",
            }
            label = labels.get(id(buffer), "service")
            with self._service_lock:
                rows = self._service_observations.setdefault(label, [])
                # A full buffer is a recording failure, never permission to
                # silently delete observations. The session seals each rotation.
                if len(rows) >= 4 * 150000:
                    raise YieldLiveWriterError("resident service evidence capacity reached")
                rows.append(value)
            return
        if len(buffer) >= 4 * 150000:
            raise YieldLiveWriterError("four-attempt run evidence capacity reached")
        buffer.append(value)

    def _send_packet(self, sensor, **kwargs):
        output = getattr(self, '_last_output', None)
        if (output is not None and output.integer_echoes.get(26) == 40
            and kwargs.get('command_mode') is CommandMode.HOLD and not self._stopped):
            # The TP rejects a repeated ARM at READY_HOME_NEXT with reason62.
            # Retire ARM during its announced return, before Home is reached.
            from step5d_autotune_v4_r004.wire import SessionCommand
            self._session_command = SessionCommand.HOLD
        proposed = tuple(float(x) for x in kwargs.get("proposed_qdot", (0.,)*6))
        if len(proposed) != 6 or any(not math.isfinite(x) or abs(x) > .05 for x in proposed):
            raise YieldLiveWriterError("native joint velocity limit exceeded (0.05 rad/s)")
        packet = super()._send_packet(sensor, **kwargs)
        if (
            kwargs.get("command_mode") is CommandMode.PATH
            and kwargs.get("reference_phase") in {"entry", "path"}
        ):
            qualification = getattr(self, "_qualification_control", None)
            provider = getattr(qualification, "contact_command_provider", None)
            confirm = getattr(provider, "confirm_published_packet", None)
            if callable(confirm):
                # Use the qdot values in the packet that was sent, after all
                # host projection and wire construction. This feedback is
                # committed only after send_packet returned successfully.
                confirm(
                    tuple(float(value) for value in packet.double_values[13:19]),
                    packet_sequence=int(packet.sequence),
                    published_at_s=float(self._last_writer_publish_mono_s),
                )
        if (kwargs.get("reference_phase") == "path"
            and kwargs.get("command_mode") is CommandMode.PATH):
            self._host_path_publish_count += 1
        self._record(self.command_observations, (self._mono_clock(), packet))
        request = getattr(self, "live_path_request", None)
        phase = kwargs.get("reference_phase")
        phase_time = kwargs.get("reference_time_s")
        end = self._r013_path_early_end_controller
        # The TP consumes the end register on a later 500 Hz tick.  Requesting
        # it at the exact formal endpoint lets controller/host clock skew end
        # a real PATH a few milliseconds early (the measured 59.994--59.998 s
        # failures).  Keep the formal metric window at [5, 60), but allow the
        # bounded protocol seam to be consumed before publishing RETURNING.
        end_time_s = None
        if request is not None and request.kind in {"diagnostic", "r013_compat_60"}:
            end_time_s = request.path_duration_s
            if request.kind == "r013_compat_60":
                end_time_s += PATH_SEAM_CONTINUATION_S
        if (end_time_s is not None and phase == "path" and phase_time is not None
            and phase_time >= end_time_s and not end.requested):
            if not end.request_early_end(self._ordinal):
                raise YieldLiveWriterError("diagnostic PATH-end request rejected")
            # This is the writer-owned normal end fence, not an active
            # censor request. Keep the base writer's lifecycle flag in sync
            # with the shared register handshake so finalization uses the
            # strict complete-path collector when all 550 bins are present.
            self._r013_path_end_requested = True
            self._r013_path_end_request_sequence = int(self._ordinal)
            self._r013_path_end_request_mono_s = self._mono_clock()
        return packet

    def _validate_output(self, output, **kwargs):
        # open() validates its first frame directly, before _poll_checked.
        # Preserve the actual rejecting frame even when open closes transport.
        if not self._opened:
            self._record(self.admission_robot_observations, output)
        try:
            return super()._validate_output(output, **kwargs)
        except Exception as exc:
            evidence = {
                "output": output, "host_monotonic_s": self._mono_clock(),
                "error": f"{type(exc).__name__}: {exc}",
                "safety_mode": getattr(output, "safety_mode", None),
                "robot_mode": getattr(output, "robot_mode", None),
                "runtime_state": getattr(output, "runtime_state", None),
                "tcp_speed_m_s_rad_s": getattr(output, "tcp_speed_m_s_rad_s", None),
                "qd_rad_s": getattr(output, "qd_rad_s", None),
            }
            if getattr(self, "_first_output_error", None) is None:
                self._first_output_error = evidence
            self._record(self.rejected_robot_observations, evidence)
            raise

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

    def _swallow_safe_stop_send_errors(self) -> bool:
        return False

    def _safe_stop_sensor_packet(self):
        return self._stop_only_sensor_packet()

    def _preserve_runtime_during_sensor_wait(self) -> None:
        transport = self._controller_transport
        if transport is None or not self._runtime_transport_is_open():
            return
        now = self._mono_clock()
        if self._entry_runtime_open_mono_s is None:
            self._entry_runtime_open_mono_s = now
        output = transport.poll_output(wait_s=0.0)
        now = self._mono_clock()
        if output is None:
            origin = self._last_rtde_frame_mono_s
            if origin is None:
                origin = self._entry_runtime_open_mono_s
            if now - origin >= RUNTIME_OUTPUT_MAX_AGE_S:
                raise LiveWriterError(
                    "runtime identity output is stale: missing RTDE output "
                    f"for {now - origin:.3f}s without refreshing cached timestamps"
                )
            return
        if (
            self._last_rtde_frame_sequence is not None
            and output.timestamp <= self._last_rtde_frame_sequence
        ):
            origin = self._last_rtde_frame_mono_s
            if origin is not None and now - origin >= RUNTIME_OUTPUT_MAX_AGE_S:
                raise LiveWriterError(
                    "runtime identity output is stale: RTDE timestamp did not "
                    "advance without refreshing cached timestamps"
                )
            self._last_poll_was_fresh = False
            return
        received = output.received_monotonic_s
        if received is None or not 0 <= now - received < RUNTIME_OUTPUT_MAX_AGE_S:
            raise LiveWriterError('runtime sensor-wait observation has no fresh receive timestamp')
        self._validate_output(output, require_stationary=True, allow_prearm_epoch=True)
        self._last_poll_was_fresh = True
        self._last_rtde_frame_mono_s = received
        self._last_rtde_frame_sequence = output.timestamp
        self._observe_runtime(output)
        self._last_output = output
        self._last_output_seen_wall_s = self._wall_clock()

    def _entry_output_after_sensor_wait(self, rtde):
        del rtde
        self._preserve_runtime_during_sensor_wait()
        output = self._last_output
        if output is None:
            raise LiveWriterError("r004 runtime output is missing at entry")
        origin = self._last_rtde_frame_mono_s
        now = self._mono_clock()
        if origin is None or now - origin >= RUNTIME_OUTPUT_MAX_AGE_S:
            raise LiveWriterError("runtime identity output is stale")
        return output

    def _commit_entry_runtime_freshness(self, output) -> None:
        if (
            self._last_output is output
            and self._last_rtde_frame_sequence == output.timestamp
            and self._last_rtde_frame_mono_s is not None
        ):
            return
        super()._commit_entry_runtime_freshness(output)

    def _expected_runtime_echo(self) -> tuple[int, tuple[int, int]]:
        from step5d_autotune_v4_r004.contracts import runtime_identity_limbs

        readable = getattr(self, "_readable_runtime_identity", None)
        if readable is None:
            expected = runtime_identity_limbs(
                self.contract.raw["program"],
                self.contract.sha256,
                self.contract.campaign_fingerprint,
            )
        else:
            expected = tuple(readable)
        return self._runtime_protocol, expected

    def _fresh_stop_observation(self, output, *, requested_at, prior_timestamp, now):
        received = getattr(output, "received_monotonic_s", None)
        timestamp = getattr(output, "timestamp", None)
        if received is None or timestamp is None:
            return False
        if not (requested_at <= received <= now and now - received < RUNTIME_OUTPUT_MAX_AGE_S):
            return False
        if prior_timestamp is not None and not (timestamp > prior_timestamp):
            return False
        return True

    def _identity_matches(self, output) -> bool:
        protocol, expected = self._expected_runtime_echo()
        echoes = getattr(output, "integer_echoes", {}) or {}
        return echoes.get(32) == protocol and (echoes.get(33), echoes.get(34)) == expected

    def stop_and_observe(self, *, timeout_s=1.0, reason="operator_stop"):
        """Send at most one STOP on the open transport and observe the result."""

        if self._stop_terminal_receipt is not None:
            return dict(self._stop_terminal_receipt)
        requested_at = self._mono_clock()
        prior = self._last_output
        prior_timestamp = None if prior is None else prior.timestamp
        send_error = None
        if not self._stop_command_attempted:
            try:
                self.stop(reason)
            except Exception as exc:
                send_error = exc
                self._stop_send_error = exc
        if send_error is None:
            send_error = self._stop_send_error
        send_ok = self._stop_command_delivered and send_error is None
        requested_session_sequence = self._session_command_sequence
        requested_packet_sequence = self._last_writer_sequence
        receipt = {
            "stop_requested": True,
            "stopped": False,
            "stop_send_ok": send_ok,
            "requested_session_sequence": requested_session_sequence,
            "requested_packet_sequence": requested_packet_sequence,
            "stop_send_error": None if send_error is None else f"{type(send_error).__name__}: {send_error}",
            "tp_ack": False,
            "observed_stationary": False,
            "protective_stop": False,
            "safety_mode": None,
            "robot_mode": None,
            "runtime_state": None,
            "runtime_mode": None,
            "state": None,
            "reason": "no_open_transport_for_confirmation",
            "stop_packet_provenance": self._stop_packet_provenance,
        }
        if not self._runtime_transport_is_open():
            # A closed transport has no fresh STOP observation.  The prior
            # Home/terminal image is retained as raw evidence, but it cannot
            # mint a physical STOP acknowledgement or promote the session.
            receipt["reason"] = "transport_closed_before_fresh_stop_observation"
            self._stop_terminal_receipt = receipt
            return dict(receipt)
        deadline = requested_at + float(timeout_s)
        last_output = None
        while self._mono_clock() <= deadline:
            output = self._controller_transport.poll_output(wait_s=0.002)
            now = self._mono_clock()
            if output is not None:
                last_output = output
                echoes = getattr(output, "integer_echoes", {}) or {}
                safety_normal = bool(getattr(output, "safety_normal", False))
                stationary = bool(getattr(output, "stationary", False))
                state = echoes.get(26)
                stop_reason = echoes.get(28)
                protective = not safety_normal
                consumed_packet = getattr(output, 'consumed_packet_sequence', -1)
                tp_ack = (safety_normal and state == 90
                          and echoes.get(29) == requested_session_sequence
                          and requested_packet_sequence is not None
                          and consumed_packet >= requested_packet_sequence)
                fresh = self._fresh_stop_observation(
                    output,
                    requested_at=requested_at,
                    prior_timestamp=prior_timestamp,
                    now=now,
                )
                identity = self._identity_matches(output)
                receipt.update({
                    "safety_mode": getattr(output, "safety_mode", None),
                    "robot_mode": getattr(output, "robot_mode", None),
                    "runtime_state": getattr(output, "runtime_state", None),
                    "runtime_mode": state,
                    "state": state,
                    "stop_reason": stop_reason,
                    "consumed_session_sequence": echoes.get(29),
                    "consumed_packet_sequence": consumed_packet,
                    "observed_stationary": stationary,
                    "protective_stop": protective,
                    "tp_ack": tp_ack,
                    "controller_timestamp": output.timestamp,
                    "received_monotonic_s": getattr(output, "received_monotonic_s", None),
                    "actual_qd": list(getattr(output, "qd_rad_s", ())),
                    "actual_tcp_speed": list(getattr(output, "tcp_speed_m_s_rad_s", ())),
                })
                if send_ok and fresh and identity and stationary and tp_ack:
                    receipt["stopped"] = True
                    receipt["reason"] = stop_reason
                    self._last_output = output
                    self._stop_terminal_receipt = receipt
                    return dict(receipt)
                if fresh and identity and stationary and protective:
                    receipt["reason"] = "protective_stop_is_not_tp_stop_ack"
                    self._stop_terminal_receipt = receipt
                    return dict(receipt)
                if not send_ok:
                    receipt["reason"] = "stop_send_error"
                    self._stop_terminal_receipt = receipt
                    return dict(receipt)
            self._sleep(0.002)
        if last_output is None:
            receipt["reason"] = "fresh_stationary_stop_confirmation_timeout"
        elif not send_ok:
            receipt["reason"] = "stop_send_error"
        elif receipt.get("protective_stop"):
            receipt["reason"] = "protective_stop_is_not_tp_stop_ack"
        elif not receipt.get("tp_ack"):
            receipt["reason"] = "fresh_stationary_stop_confirmation_timeout"
        else:
            receipt["reason"] = "fresh_stationary_stop_confirmation_timeout"
        self._stop_terminal_receipt = receipt
        return dict(receipt)

    def _observe_stop_before_close(self) -> None:
        self.stop_and_observe(timeout_s=1.0, reason="cleanup_stop")


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
    observe = getattr(writer, "stop_and_observe", None)
    if callable(observe):
        return observe(timeout_s=timeout_s, reason="operator_stop")
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
