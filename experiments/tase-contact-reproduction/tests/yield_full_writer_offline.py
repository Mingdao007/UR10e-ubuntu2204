"""Offline composition of native yield provider + qualification step + writer.

In-memory transport and a deterministic observation clock only. Baseline-success
admission is fixture-provided. Native PATH law/QP, qualification.step, packet
history, and evidence collection are not substituted. Stationary calibrated-home
observations isolate software lifecycle; they are not task-performance evidence
and do not claim transport admission or physical completion.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
import gc
import json
import time

import numpy as np

import step5d_autotune_v4_r004_live_writer as writer_module
from contact_yield_protocol import law_seed_parameters
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
from step5d_autotune_v4_r004 import baseline_runtime
from step5d_autotune_v4_r004.campaign import build_campaign_plan
from step5d_autotune_v4_r004.session import SessionPhase
from step5d_autotune_v4_r004.wire import CommandMode, SessionCommand
from step6_figure8_autotune_v1.live_composition import figure8_motion_profile
from test_contact_qualification_provider import (
    _control,
    _output,
    _sensor,
    _successful_baseline,
)
from test_step5d_autotune_v4_r004_live_boundary import _prerequisites
from yield_contact_provider import YieldContactProvider
from yield_contact_runtime import YieldContactRuntime

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "report/yield-frozen-transfer-v1/protocol.json"
PRESERVED_HOME = ROOT / "report/contact-six-qp-20260917/preserved-home.json"
PRODUCTION_RUNTIME_DEADLINE_S = 0.0015
METHODS = ("SFC", "DSFC", "MSFC")


def protocol_parameters(method):
    payload = json.loads(PROTOCOL_PATH.read_text())
    parameters = dict(payload["parameters"][method])
    seed = law_seed_parameters(method)
    if method == "MSFC" and parameters["g"] == seed["g"]:
        raise ValueError("MSFC g50 parameters must not fall back to the original seed")
    return parameters


def preserved_home():
    return json.loads(PRESERVED_HOME.read_text())


@dataclass
class FullWriterRun:
    method: str
    parameters: dict
    runtime_identity: str
    deadline_s: float | None
    provider: object
    runtime: object
    writer: object
    evidence: object | None
    error: BaseException | None
    samples: list
    published: list
    phases: list
    packet_count: int
    clock: object
    loop_wall_ns: list = field(default_factory=list)
    loop_cpu_ns: list = field(default_factory=list)
    native_wall_ns: list = field(default_factory=list)
    native_cpu_ns: list = field(default_factory=list)
    qualification_wall_ns: list = field(default_factory=list)
    qualification_cpu_ns: list = field(default_factory=list)
    gc_events: list = field(default_factory=list)
    gc_states_in_loop: list = field(default_factory=list)
    gc_enabled_after: bool = True
    motion_profile_xy_path_speed_m_s: float | None = None
    provider_id: int = 0
    start_t: float = 0.0
    stop_packets: list = field(default_factory=list)
    before_fault: dict | None = None
    final_snapshot: dict | None = None
    loop_kinds: list = field(default_factory=list)
    scope: str = (
        "offline unpaced stationary-observation full-writer composition; "
        "in-memory transport; simulated receive clocks; existing writer GC "
        "policy; not formal timing qualification, IO latency, transport "
        "admission, or physical task evidence"
    )


def _home_output(receipt, *, state, monotonic_s, sequence):
    pose = tuple(receipt["home_pose"])
    joints = tuple(receipt["home_q"])
    rtde = receipt["rtde"]
    return replace(
        _output(state=state),
        timestamp=monotonic_s,
        observed_at_s=monotonic_s,
        received_monotonic_s=monotonic_s,
        consumed_packet_sequence=sequence,
        tcp_pose_m_rad=pose,
        tcp_speed_m_s_rad_s=(0.0,) * 6,
        q_rad=joints,
        qd_rad_s=(0.0,) * 6,
        payload_kg=float(rtde["payload"]),
        payload_cog_m=tuple(rtde["payload_cog"]),
        tcp_offset_m_rad=tuple(rtde["tcp_offset"]),
        integer_echoes={26: state, 31: 127},
    )


def _home_sensor(*, monotonic_s):
    return replace(
        _sensor(),
        normal_load_n=5.0,
        force_norm_n=5.0,
        filtered_normal_n=5.0,
        observed_at_s=monotonic_s,
        wrench=(0.0, 0.0, -5.0, 0.0, 0.0, 0.0),
        heartbeat=monotonic_s,
    )


def exercise_full_writer(
    tmp_path,
    monkeypatch,
    *,
    qp_library,
    method="MSFC",
    measure=False,
    stale_after_path=False,
    cache_after_path=False,
    hash_mismatch=False,
    allow_safe_nontrainable=True,
    clock_origin_s=0.0,
):
    """Run the mature execute_attempt loop with the native yield provider.

    Runtime deadline_s=None is an explicit offline fixture so compute outliers
    are recorded. Production YieldContactRuntime deadline remains 1.5 ms.
    """
    if method not in METHODS:
        raise ValueError(f"unsupported method {method}")
    parameters = protocol_parameters(method)
    receipt = preserved_home()
    rotation = rotvec_to_matrix(np.array(receipt["home_pose"][3:]))
    runtime = YieldContactRuntime(
        method=method,
        qp_library=qp_library,
        anchor_m=receipt["home_pose"][:3],
        task_basis=rotation,
        approach_inward_base=rotation[:, 2],
        deadline_s=None,
        law_parameters=parameters,
    )
    provider = YieldContactProvider(
        runtime=runtime,
        model_hashes={"calibration": runtime.model.calibration_hash},
    )
    original_runtime_step = runtime.step
    original_qualification_step = writer_module.CanonicalQualificationControl.step
    published = []
    phases = []
    loop_wall_ns = []
    loop_cpu_ns = []
    native_wall_ns = []
    native_cpu_ns = []
    qualification_wall_ns = []
    qualification_cpu_ns = []
    gc_events = []
    gc_states = []
    tick_wall_start = [None]
    tick_cpu_start = [None]
    active_tick = [None]
    stop_packets = []
    before_fault = [None]
    loop_kinds = []

    def factory(candidate, attempt_id, release_contract, path_requested=False, **_kwargs):
        # Unstubbed CanonicalQualificationControl() fails in this worktree:
        # frozen ur_xacro sha256 does not match /opt/ros/humble/.../ur.urdf.xacro.
        # Use the same object.__new__ initialization as the native qualification
        # fixture so the real step() runs inside execute_attempt.
        control = _control(provider, path_requested=path_requested)
        control.candidate = candidate
        control.attempt_id = attempt_id
        control.release_contract = release_contract
        control.motion_profile = figure8_motion_profile()
        control._contract.model_hashes = dict(provider.model_hashes)
        if hash_mismatch:
            provider.model_hashes = {"calibration": "wrong"}
        control._last_monotonic_s = None
        control._origin_monotonic_s = None
        control._path_origin_monotonic_s = None
        if measure:
            def timed_step(self, **step_kwargs):
                gc_states.append(gc.isenabled())
                started = time.perf_counter_ns()
                cpu_started = time.thread_time_ns()
                try:
                    return original_qualification_step(self, **step_kwargs)
                finally:
                    qualification_cpu_ns.append(time.thread_time_ns() - cpu_started)
                    qualification_wall_ns.append(time.perf_counter_ns() - started)

            control.step = timed_step.__get__(control, type(control))
        return control

    monkeypatch.setattr(writer_module, "CanonicalQualificationControl", factory)
    monkeypatch.setattr(baseline_runtime, "step_baseline", _successful_baseline)

    if measure:
        def timed_native(*args, **kwargs):
            started = time.perf_counter_ns()
            cpu_started = time.thread_time_ns()
            try:
                return original_runtime_step(*args, **kwargs)
            finally:
                native_cpu_ns.append(time.thread_time_ns() - cpu_started)
                native_wall_ns.append(time.perf_counter_ns() - started)

        runtime.step = timed_native

        def trace_gc(phase, info):
            gc_events.append(
                {
                    "phase": phase,
                    "generation": info["generation"],
                    "wall_ns": time.perf_counter_ns(),
                    "cpu_ns": time.thread_time_ns(),
                    "tick": active_tick[0],
                    "collected": info.get("collected", 0),
                    "uncollectable": info.get("uncollectable", 0),
                }
            )

        gc.callbacks.append(trace_gc)

    # Start at 0 so repeated 2 ms advances stay on the same binary64 grid as
    # the writer-loop fixture. Starting at 100 s extra-enters by one tick.
    clock = SimpleNamespace(
        t=float(clock_origin_s),
        ticks=0,
        ended=False,
        cache_injected=False,
        stale_injected=False,
        previous=None,
    )
    pose = tuple(receipt["home_pose"])
    joints = tuple(receipt["home_q"])
    baseline_output = _home_output(receipt, state=21, monotonic_s=clock.t, sequence=0)
    provider.command(
        output=baseline_output,
        sensor=_home_sensor(monotonic_s=clock.t),
        monotonic_s=clock.t,
        actual_dt_s=0.002,
        mode="baseline",
        internal_setpoint_n=5.0,
    )
    transport = SimpleNamespace(send_packet=lambda *_args, **_kwargs: None)
    samples = []
    writer = writer_module.LiveR004Writer(
        _prerequisites(),
        authority_root=tmp_path / "authority",
        route_id="r004-test-route",
        attempt_id="r004-yield-full-writer",
        controller_transport=transport,
        kunwei_transport=object(),
        mono_clock=lambda: clock.t,
        wall_clock=lambda: 1_000_000_000.0 + clock.t,
        sleep=lambda seconds: None,
        path_sample_sink=samples.append,
    )
    attempt = build_campaign_plan(writer.contract)[3]
    writer.session = SimpleNamespace(
        phase=SessionPhase.RUNNING,
        finish_attempt=lambda _decision: None,
    )
    writer.session.stop = lambda *_args, **_kwargs: setattr(writer.session, 'phase', SessionPhase.STOPPED)
    writer._ordinal = attempt.ordinal
    writer._kind = attempt.kind
    writer._candidate_token = 17
    writer._baseline_successes = 3
    writer._session_command = SessionCommand.HOLD
    writer._home = SimpleNamespace(pose=pose, q=joints)
    writer._host_hard_tube = None
    writer._r013_path_early_end_controller = SimpleNamespace(
        request_early_end=lambda _sequence: (setattr(clock, "ended", True) or True),
        requested=False,
    )
    original_send = writer._send_packet

    def send_packet(sensor, **kwargs):
        packet = original_send(sensor, **kwargs)
        if packet.command_mode == CommandMode.STOP:
            stop_packets.append({'sequence':packet.sequence,'qdot':tuple(packet.double_values[13:19])})
        result = provider.last_result
        if (
            kwargs.get("command_mode") == CommandMode.PATH
            and result is not None
            and kwargs.get("reference_phase") in {"entry", "path"}
        ):
            published.append(
                {
                    "sequence": packet.sequence,
                    "qdot": tuple(packet.double_values[13:19]),
                    "native_qdot": tuple(result["qdot_rad_s"]),
                    "phase": result["phase"],
                    "sample_time_s": result.get("sample_time_s"),
                    "reference_phase": kwargs.get("reference_phase"),
                    "reference_time_s": kwargs.get("reference_time_s"),
                    "provider_id": id(provider),
                }
            )
            phases.append(result["phase"])
        return packet

    writer._send_packet = send_packet

    def sleep(seconds):
        if measure and tick_wall_start[0] is not None:
            loop_cpu_ns.append(time.thread_time_ns() - tick_cpu_start[0])
            loop_wall_ns.append(time.perf_counter_ns() - tick_wall_start[0])
            loop_kinds.append('active_to_presleep')
            tick_wall_start[0] = None
            active_tick[0] = None
        clock.t += seconds

    writer._sleep = sleep

    def poll(**_kwargs):
        if measure:
            active_tick[0] = clock.ticks
            tick_wall_start[0] = time.perf_counter_ns()
            tick_cpu_start[0] = time.thread_time_ns()
        state = 78 if clock.ended else 20 if clock.ticks == 0 else 21 if clock.ticks == 1 else 25
        if hash_mismatch and state==25:
            before_fault[0]=provider.snapshot()
        sequence = writer._last_writer_sequence or 0
        if cache_after_path and clock.cache_injected:
            raise RuntimeError("offline cached-poll fixture stop")
        if cache_after_path and state == 25 and phases and phases[-1] == "path" and not clock.cache_injected:
            clock.cache_injected = True
            before_fault[0] = provider.snapshot()
            writer._last_poll_was_fresh = False
            clock.ticks += 1
            return clock.previous
        received = clock.t
        if stale_after_path and state == 25 and phases and phases[-1] == "path" and not clock.stale_injected:
            clock.stale_injected = True
            before_fault[0] = provider.snapshot()
            received = clock.t - 0.1
        clock.ticks += 1
        writer._last_poll_was_fresh = True
        writer._last_rtde_frame_sequence = clock.t
        writer._last_rtde_frame_mono_s = clock.t
        result = replace(
            _home_output(receipt, state=state, monotonic_s=clock.t, sequence=sequence),
            received_monotonic_s=received,
        )
        clock.previous = result
        return result

    def sensor():
        writer._last_kunwei_frame_sequence = clock.ticks
        writer._last_kunwei_observed_s = clock.t
        return _home_sensor(monotonic_s=clock.t)

    writer._poll_checked = poll
    writer._read_sensor = sensor
    # arm() is outside this unit; reproduce only its explicit pre-loop collection.
    gc.collect()
    evidence = None
    error = None
    final_snapshot = None
    try:
        with runtime:
            try:
                evidence = writer.execute_attempt(
                    attempt,
                    timeout_s=70.0,
                    allow_safe_nontrainable=allow_safe_nontrainable,
                )
            finally:
                final_snapshot = provider.snapshot()
    except Exception as exc:
        error = exc
    finally:
        if measure:
            try:
                gc.callbacks.remove(trace_gc)
            except ValueError:
                pass
        if tick_wall_start[0] is not None:
            loop_cpu_ns.append(time.thread_time_ns() - tick_cpu_start[0])
            loop_wall_ns.append(time.perf_counter_ns() - tick_wall_start[0])
            loop_kinds.append('terminal_with_finalization')

    return FullWriterRun(
        method=method,
        parameters=parameters,
        runtime_identity=runtime.identity,
        deadline_s=runtime.deadline_s,
        provider=provider,
        runtime=runtime,
        writer=writer,
        evidence=evidence,
        error=error,
        samples=samples,
        published=published,
        phases=phases,
        packet_count=writer._packet_sequence,
        clock=clock,
        loop_wall_ns=loop_wall_ns,
        loop_cpu_ns=loop_cpu_ns,
        native_wall_ns=native_wall_ns,
        native_cpu_ns=native_cpu_ns,
        qualification_wall_ns=qualification_wall_ns,
        qualification_cpu_ns=qualification_cpu_ns,
        gc_events=gc_events,
        gc_states_in_loop=gc_states,
        gc_enabled_after=gc.isenabled(),
        motion_profile_xy_path_speed_m_s=(
            None
            if writer._qualification_control is None
            else writer._qualification_control.motion_profile.xy_path_speed_m_s
        ),
        provider_id=id(provider),
        start_t=float(clock_origin_s),
        stop_packets=stop_packets,
        before_fault=before_fault[0],
        final_snapshot=final_snapshot,
        loop_kinds=loop_kinds,
    )
