#!/usr/bin/env python3
"""Isolated V4/r004 register writer with an explicit fail-closed live gate.

The live path assumes Script1 and Script2 were separately delivered and
Remote-loaded/played by the owner route.  It verifies receipts and a resident
session; it never opens Dashboard and never performs Load or Play.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import time
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import step5d_bridge_authority as canonical_authority  # noqa: E402
from step5d_autotune_v4_r004.campaign import Attempt, build_campaign_plan  # noqa: E402
from step5d_autotune_v4_r004.contracts import Candidate, PACKET_STALE_S, load_contract, RUNTIME_PROTOCOL, runtime_identity_limbs  # noqa: E402
from step5d_autotune_v4_r004.evidence import (  # noqa: E402
    AttemptEvidence,
    PathEvidenceCollector,
    PathSample,
    QualificationEvidence,
    QualificationEvidenceCollector,
    QualificationSample,
)
from step5d_autotune_v4_r004.fake_rtde import FakeRTDE  # noqa: E402
from step5d_autotune_v4_r004.home import (  # noqa: E402
    HomeReference,
    ReturnEvidence,
    evaluate_entry_home,
    evaluate_return,
)
from step5d_autotune_v4_r004.home_profile import IKBranchLock, load_fixed_home_profile  # noqa: E402
from step5d_autotune_v4_r004.identity import RuntimeIdentityEvidence  # noqa: E402
from step5d_autotune_v4_r004.ledger import DurableCampaignLedger, LedgerError, verify_ledger_hash_chain  # noqa: E402
from step5d_autotune_v4_r004.prerequisites import LivePrerequisites, PrerequisiteError, load_prerequisites  # noqa: E402
from step5d_autotune_v4_r004.qualification import CanonicalQualificationControl  # noqa: E402
from step5d_autotune_v4_r004.motion_profile import R004_MOTION_PROFILE  # noqa: E402
from step5d_autotune_v4_r004.session import ArmRequest, ResidentSession, SessionPhase, SessionProtocolError  # noqa: E402
from step5d_autotune_v4_r004.transport import (  # noqa: E402
    LiveR004KunweiTransport,
    LiveR004RTDETransport,
    R004KunweiTransport,
    R004OutputSnapshot,
    R004RTDETransport,
    TransportError,
    expected_eoat,
)
from step5d_autotune_v4_r004.timing import TimingEvidenceCollector  # noqa: E402
from step5d_autotune_v4_r004.wire import (  # noqa: E402
    CommandMode,
    SensorPacket,
    SessionCommand,
    SessionInput,
    AttemptKind,
    WirePacket,
    build_wire_packet,
)
from step5d_autotune_v3.governance import read_proc_starttime_ticks  # noqa: E402
from step5d_autotune_v4_r004.path_reference import step5_path_reference  # noqa: E402


WRITER_SCHEMA = "step5d.autotune-v4/r004-offline-writer-v1"
LIVE_WRITER_SCHEMA = "step5d.autotune-v4/r004-live-writer-v1"
LIVE_ACK = "RUN_LIVE_R004_WITH_EXPLICIT_ACK"
# A controller frame stall is a hard freshness failure, not a relaxed runtime
# identity grace period.  Equal cached frames may be held by the resident TP
# only until this same 80 ms boundary.
RUNTIME_OUTPUT_MAX_AGE_S = PACKET_STALE_S
KUNWEI_STREAM_WARMUP_S = 0.500
PREARM_RECONNECT_MAX = 1
# A reopened RTDE connection may need a few receive polls before its first
# recipe sample.  This is the same small freshness horizon used by the V3
# entry gate; the poll bound keeps the contract deterministic for injected
# clocks as well as real monotonic clocks.
PREARM_FIRST_FRAME_WAIT_S = PACKET_STALE_S
PREARM_FIRST_FRAME_POLL_S = 0.002
PREARM_FIRST_FRAME_MAX_POLLS = math.ceil(
    PREARM_FIRST_FRAME_WAIT_S / PREARM_FIRST_FRAME_POLL_S
)


@dataclass(frozen=True)
class WriterTick:
    packet_sequence: int
    session_command_sequence: int
    session_epoch: int
    logical_attempt_ordinal: int
    attempt_kind: AttemptKind
    candidate_token: int
    command_mode: CommandMode
    packet: WirePacket


class OfflineR004Writer:
    """Single-owner typed writer for local deterministic tests only."""

    def __init__(self, *, session_epoch: int = 1) -> None:
        if isinstance(session_epoch, bool) or not isinstance(session_epoch, int) or session_epoch <= 0:
            raise ValueError("session_epoch must be a positive integer")
        self.contract = load_contract()
        self.fake_rtde = FakeRTDE(tp_hz=500.0, writer_hz=500.0)
        self.session_epoch = session_epoch
        self.packet_sequence = 0
        self.session_command_sequence = 0
        self.session_command = SessionCommand.HOLD
        self.logical_attempt_ordinal = 0
        self.attempt_kind = AttemptKind.QUALIFICATION
        self.candidate_token = 0
        self.baseline_consecutive_successes = 0

    def set_session(
        self,
        command: SessionCommand,
        *,
        ordinal: int | None = None,
        kind: AttemptKind | None = None,
        candidate_token: int | None = None,
    ) -> None:
        if not isinstance(command, SessionCommand):
            raise TypeError("session command must be typed")
        # A session command sequence identifies the command write, not merely
        # its enum value; ARM for the next ordinal must be newer even when the
        # previous command was also ARM.
        self.session_command_sequence += 1
        self.session_command = command
        if ordinal is not None:
            self.logical_attempt_ordinal = ordinal
        if kind is not None:
            if not isinstance(kind, AttemptKind):
                raise TypeError("attempt kind must be typed")
            self.attempt_kind = kind
        if candidate_token is not None:
            if isinstance(candidate_token, bool) or not isinstance(candidate_token, int) or candidate_token < 0:
                raise ValueError("candidate token must be a non-negative integer")
            self.candidate_token = candidate_token

    def publish(
        self,
        *,
        sensor: SensorPacket,
        proposed_qdot: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        internal_setpoint_n: float = 1.0,
        command_mode: CommandMode = CommandMode.HOLD,
        structural_stop: bool = False,
    ) -> WriterTick:
        if not isinstance(command_mode, CommandMode):
            raise TypeError("command_mode must be typed")
        session = SessionInput(
            baseline_consecutive_successes=self.baseline_consecutive_successes,
            command_mode=command_mode,
            sticky_one_newton_latched=0,
            session_command=self.session_command,
            session_command_sequence=self.session_command_sequence,
            session_epoch=self.session_epoch,
            logical_attempt_ordinal=self.logical_attempt_ordinal,
            attempt_kind=self.attempt_kind,
            candidate_token=self.candidate_token,
        )
        packet = build_wire_packet(
            self.contract,
            Candidate(),
            sensor=sensor,
            proposed_qdot=proposed_qdot,
            internal_setpoint_n=internal_setpoint_n,
            packet_sequence=self.packet_sequence,
            session=session,
            structural_stop=structural_stop,
            motion_profile=R004_MOTION_PROFILE,
        )
        self.fake_rtde.write_packet(packet)
        tick = WriterTick(
            packet_sequence=self.packet_sequence,
            session_command_sequence=self.session_command_sequence,
            session_epoch=self.session_epoch,
            logical_attempt_ordinal=self.logical_attempt_ordinal,
            attempt_kind=self.attempt_kind,
            candidate_token=self.candidate_token,
            command_mode=packet.command_mode,
            packet=packet,
        )
        self.packet_sequence += 1
        return tick

    def arm(self, ordinal: int, kind: AttemptKind, candidate_token: int) -> None:
        if ordinal < 1 or ordinal > 16:
            raise ValueError("r004 ordinal is outside 1..16")
        self.set_session(
            SessionCommand.ARM,
            ordinal=ordinal,
            kind=kind,
            candidate_token=candidate_token,
        )

    def hold(self) -> None:
        self.set_session(SessionCommand.HOLD)

    def complete(self) -> None:
        self.set_session(SessionCommand.COMPLETE)

    def stop(self) -> None:
        self.set_session(SessionCommand.STOP)


class LiveWriterError(RuntimeError):
    """The r004 live boundary failed closed."""


@dataclass(frozen=True)
class LiveWriterTick:
    packet_sequence: int
    session_command_sequence: int
    output: R004OutputSnapshot
    packet: WirePacket


class LiveR004Writer:
    """Single route-bound r004 writer for a separately resident TP program.

    Construction is side-effect free.  ``open`` is the only method that can
    create the RTDE/Kunwei transports, and it requires the exact acknowledgement
    token plus all fresh receipt/session gates before doing so.
    """

    def __init__(
        self,
        prerequisites: LivePrerequisites,
        *,
        authority_root: Path,
        route_id: str,
        attempt_id: str,
        controller_host: str | None = None,
        kunwei_host: str | None = None,
        kunwei_port: int | None = None,
        controller_transport: R004RTDETransport | None = None,
        kunwei_transport: R004KunweiTransport | None = None,
        software_baseline_n: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        candidate: Candidate | None = None,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        path_sample_sink: Callable[[PathSample], None] | None = None,
        identity_namespace: str = "r004",
        runtime_protocol: int = RUNTIME_PROTOCOL,
        canonical_runtime_only: bool = False,
    ) -> None:
        if route_id != prerequisites.route_id:
            raise LiveWriterError(f"{identity_namespace} route id differs from receipt bundle")
        if not isinstance(identity_namespace, str) or not identity_namespace:
            raise LiveWriterError("writer identity namespace is required")
        if not isinstance(route_id, str) or identity_namespace.lower() not in route_id.lower():
            raise LiveWriterError(f"{identity_namespace} route identity is required")
        if not isinstance(attempt_id, str) or not attempt_id or identity_namespace.lower() not in attempt_id.lower():
            raise LiveWriterError(f"distinct {identity_namespace} attempt identity is required")
        if isinstance(runtime_protocol, bool) or not isinstance(runtime_protocol, int) or runtime_protocol <= 0:
            raise LiveWriterError("writer runtime protocol is invalid")
        if not isinstance(canonical_runtime_only, bool):
            raise LiveWriterError("canonical runtime-only policy is not typed")
        baseline = tuple(float(value) for value in software_baseline_n)
        if len(baseline) != 6 or not all(math.isfinite(value) for value in baseline):
            raise LiveWriterError("software baseline must contain six finite SI values")
        self.contract = prerequisites.contract
        self.prerequisites = prerequisites
        self.authority_root = Path(authority_root)
        self.route_id = route_id
        self.attempt_id = attempt_id
        self.identity_namespace = identity_namespace
        self._runtime_protocol = runtime_protocol
        self._canonical_runtime_only = canonical_runtime_only
        self.controller_host = controller_host
        self.kunwei_host = kunwei_host
        self.kunwei_port = kunwei_port
        self._controller_transport = controller_transport
        self._kunwei_transport = kunwei_transport
        self.software_baseline_n = baseline
        self.candidate = candidate or Candidate()
        self._wall_clock = wall_clock
        self._mono_clock = mono_clock
        self._sleep = sleep
        # Additive r005 evidence seam.  The default is None, so the mature
        # r004 writer retains its existing evidence and timing behavior.
        self._path_sample_sink = path_sample_sink
        self.authority: canonical_authority.AuthorityFence | None = None
        self._authority_owner_pid: int | None = None
        self._authority_owner_starttime_ticks: int | None = None
        self._authority_active = False
        self._failed_closed = False
        self.session = ResidentSession(self.contract)
        self._opened = False
        self._stopped = False
        self._packet_sequence = 0
        self._session_command_sequence = 0
        self._session_command = SessionCommand.HOLD
        self._ordinal = 0
        self._kind = AttemptKind.QUALIFICATION
        self._candidate_token = 0
        self._baseline_successes = 0
        self._sticky_latched = 0
        self._last_sensor: tuple[tuple[float, ...], float] | None = None
        self._last_output: R004OutputSnapshot | None = None
        self._last_identity_observed_s = self.prerequisites.runtime.observed_at_s
        self._last_output_seen_wall_s = self._wall_clock()
        self._home: HomeReference | None = None
        self._entry_linear_speed_m_s = 0.0
        self._entry_angular_speed_rad_s = 0.0
        self._qualification_control: CanonicalQualificationControl | None = None
        self._last_writer_publish_mono_s: float | None = None
        self._last_writer_sequence: int | None = None
        self._writer_publish_times: dict[int, float] = {}
        self._qdot_by_packet_sequence: dict[int, tuple[float, ...]] = {}
        self._last_rtde_frame_mono_s: float | None = None
        self._last_rtde_frame_sequence: float | None = None
        self._last_poll_was_fresh = False
        self._last_kunwei_observed_s: float | None = None
        self._last_kunwei_frame_sequence: int | None = None
        self._path_command_started_mono_s: float | None = None
        self._path_rtde_origin_s: float | None = None
        try:
            from step5d_autotune_v4_r008.host_hard_tube import HostHardTubeGuard

            self._host_hard_tube = HostHardTubeGuard.from_environ()
        except Exception:
            # Host-side hard-tube guard must never block writer construction; stay off.
            self._host_hard_tube = None
        self._last_host_hard_tube: dict[str, Any] | None = None

    @property
    def active(self) -> bool:
        return self._opened and not self._stopped

    @property
    def controller_receipt_sha256(self) -> str:
        return self.prerequisites.controller.receipt_sha256

    @property
    def script1_receipt_sha256(self) -> str:
        return self.prerequisites.script1.receipt_sha256

    @property
    def session_epoch(self) -> int:
        return self.prerequisites.session_epoch

    @property
    def input_baseline_ledger_sha256(self) -> str:
        return self.prerequisites.input_baseline_ledger_sha256

    @property
    def authority_active(self) -> bool:
        return self._authority_active

    def _begin_authority(self) -> None:
        """Begin the canonical bridge lease bound to the r004 contract."""

        if self._authority_active:
            raise LiveWriterError("canonical r004 authority is already active")
        owner_pid = os.getppid()
        owner_starttime_ticks = read_proc_starttime_ticks(owner_pid)
        if owner_starttime_ticks is None:
            raise LiveWriterError("canonical authority owner starttime is unavailable")
        resource_id = canonical_authority.DEFAULT_RESOURCE_ID
        lease_started = False
        try:
            lease = canonical_authority.begin(
                self.authority_root,
                self.attempt_id,
                owner_pid,
                owner_starttime_ticks,
                worktree_root=str(ROOT),
                launch_basis_path=str(self.contract.path),
                launch_basis_sha256=self.contract.sha256,
                resource_id=resource_id,
            )
            lease_started = True
            if lease.get("resource_id") != resource_id or lease.get("attempt_id") != self.attempt_id:
                raise LiveWriterError("canonical r004 authority receipt identity differs")
            sequence = lease.get("sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise LiveWriterError("canonical r004 authority sequence is invalid")
            fence = canonical_authority.AuthorityFence(
                self.authority_root,
                attempt_id=self.attempt_id,
                sequence=sequence,
                owner_pid=owner_pid,
                owner_starttime_ticks=owner_starttime_ticks,
                launch_basis_path=str(self.contract.path),
                launch_basis_sha256=self.contract.sha256,
                require_basis_bound=True,
                resource_id=resource_id,
            )
            fence.assert_active()
        except LiveWriterError:
            if lease_started:
                try:
                    canonical_authority.revoke(
                        self.authority_root,
                        self.attempt_id,
                        owner_pid,
                        owner_starttime_ticks,
                        reason="failed",
                        resource_id=resource_id,
                    )
                except Exception:
                    pass
            raise
        except Exception as exc:
            if lease_started:
                try:
                    canonical_authority.revoke(
                        self.authority_root,
                        self.attempt_id,
                        owner_pid,
                        owner_starttime_ticks,
                        reason="failed",
                        resource_id=resource_id,
                    )
                except Exception:
                    pass
            raise LiveWriterError(f"canonical r004 authority begin failed: {exc}") from exc
        self.authority = fence
        self._authority_owner_pid = owner_pid
        self._authority_owner_starttime_ticks = owner_starttime_ticks
        self._authority_active = True

    def _revoke_authority(self, *, reason: str) -> None:
        if not self._authority_active:
            return
        if reason not in canonical_authority.ALLOWED_REASONS:
            raise LiveWriterError("canonical authority revoke reason is invalid")
        if self._authority_owner_pid is None or self._authority_owner_starttime_ticks is None:
            raise LiveWriterError("canonical authority owner binding is missing")
        try:
            canonical_authority.revoke(
                self.authority_root,
                self.attempt_id,
                self._authority_owner_pid,
                self._authority_owner_starttime_ticks,
                reason=reason,
                resource_id=canonical_authority.DEFAULT_RESOURCE_ID,
            )
            if self.authority is None:
                raise LiveWriterError("canonical authority fence is missing")
            self.authority.assert_revoked()
        except LiveWriterError:
            raise
        except Exception as exc:
            raise LiveWriterError(f"canonical r004 authority revoke failed: {exc}") from exc
        finally:
            self._authority_active = False

    def _transport_pair(self) -> tuple[R004RTDETransport, R004KunweiTransport]:
        rtde = self._controller_transport
        kunwei = self._kunwei_transport
        if rtde is None:
            if not self.controller_host:
                raise LiveWriterError("live controller host must be explicit")
            rtde = LiveR004RTDETransport(self.controller_host)
            self._controller_transport = rtde
        if kunwei is None:
            if not self.kunwei_host or self.kunwei_port is None:
                raise LiveWriterError("live Kunwei host and port must be explicit")
            kunwei = LiveR004KunweiTransport(self.kunwei_host, port=self.kunwei_port)
            self._kunwei_transport = kunwei
        return rtde, kunwei

    def _sensor_packet(self, *, raw: Sequence[float] | None, observed_at_s: float | None) -> SensorPacket:
        if raw is None:
            wrench = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
            fresh = False
        else:
            values = tuple(float(value) - baseline for value, baseline in zip(raw, self.software_baseline_n, strict=True))
            wrench = values  # type: ignore[assignment]
            fresh = observed_at_s is not None and math.isfinite(float(observed_at_s))
            if fresh and observed_at_s is not None and observed_at_s > 0.0:
                age_s = self._mono_clock() - float(observed_at_s)
                fresh = 0.0 <= age_s < 0.080
        normal = -float(wrench[2])
        force_norm = math.sqrt(sum(value * value for value in wrench[:3]))
        torque_norm = math.sqrt(sum(value * value for value in wrench[3:]))
        return SensorPacket(
            normal_load_n=normal,
            force_norm_n=force_norm,
            heartbeat=float(self._packet_sequence),
            sensor_fresh=fresh,
            stop_request=self._stopped,
            eoat_get_ack=self._last_output is not None,
            torque_norm_nm=torque_norm,
            wrench=wrench,
            filtered_normal_n=normal,
        )

    def _session_input(self, command_mode: CommandMode) -> SessionInput:
        return SessionInput(
            baseline_consecutive_successes=self._baseline_successes,
            command_mode=command_mode,
            sticky_one_newton_latched=self._sticky_latched,
            session_command=self._session_command,
            session_command_sequence=self._session_command_sequence,
            session_epoch=self.prerequisites.session_epoch,
            logical_attempt_ordinal=self._ordinal,
            attempt_kind=self._kind,
            candidate_token=self._candidate_token,
        )

    def _send_packet(
        self,
        sensor: SensorPacket,
        *,
        command_mode: CommandMode,
        proposed_qdot: Sequence[float] = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        internal_setpoint_n: float = 1.0,
        structural_stop: bool = False,
    ) -> WirePacket:
        rtde, _kunwei = self._transport_pair()
        mode = CommandMode.STOP if self._stopped else command_mode
        packet = build_wire_packet(
            self.contract,
            self.candidate,
            sensor=sensor,
            proposed_qdot=(0.0,) * 6 if mode in {CommandMode.HOLD, CommandMode.STOP} else proposed_qdot,
            internal_setpoint_n=internal_setpoint_n,
            packet_sequence=self._packet_sequence,
            session=self._session_input(mode),
            structural_stop=structural_stop or self._stopped,
            motion_profile=R004_MOTION_PROFILE,
        )
        rtde.send_packet(packet.double_values, packet.integer_values)
        published_at = self._mono_clock()
        self._last_writer_publish_mono_s = published_at
        self._last_writer_sequence = packet.sequence
        self._writer_publish_times[packet.sequence] = published_at
        self._qdot_by_packet_sequence[packet.sequence] = tuple(
            float(value) for value in packet.double_values[13:19]
        )
        self._packet_sequence += 1
        return packet

    def _send_safe_stop(self) -> None:
        rtde = self._controller_transport
        if rtde is None:
            return
        self._session_command_sequence += 1
        self._session_command = SessionCommand.STOP
        self._stopped = True
        sensor = self._sensor_packet(raw=(0.0,) * 6, observed_at_s=self._mono_clock())
        try:
            self._send_packet(sensor, command_mode=CommandMode.STOP, structural_stop=True)
        except Exception:
            # Cleanup must continue through Kunwei, RTDE, and the lease even if
            # the final safe packet cannot be delivered.
            pass

    def _validate_output(
        self,
        output: R004OutputSnapshot,
        *,
        require_stationary: bool = False,
        allow_prearm_epoch: bool = False,
    ) -> None:
        payload, cog, tcp = expected_eoat()
        if not math.isclose(output.payload_kg, payload, rel_tol=0.0, abs_tol=0.0005):
            raise LiveWriterError("runtime V4 payload readback differs")
        if any(not math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.00005) for actual, expected in zip(output.payload_cog_m, cog, strict=True)):
            raise LiveWriterError("runtime V4 CoG readback differs")
        if any(not math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.00005) for actual, expected in zip(output.tcp_offset_m_rad, tcp, strict=True)):
            raise LiveWriterError("runtime V4 TCP readback differs")
        if not output.safety_normal or (require_stationary and not output.stationary):
            raise LiveWriterError("runtime Safety/stationary gate failed")
        if not output.program_running:
            raise LiveWriterError("r004 resident program is not running")
        hi, lo = runtime_identity_limbs(
            self.contract.raw["program"], self.contract.sha256, self.contract.campaign_fingerprint
        )
        prearm_ready = (
            allow_prearm_epoch
            and output.integer_echoes.get(24) == 0
            and output.integer_echoes.get(25) == 0
            and output.integer_echoes.get(26) == 78
            and output.integer_echoes.get(27) == 0
        )
        if output.integer_echoes.get(24) != self.prerequisites.session_epoch and not prearm_ready:
            raise LiveWriterError("runtime epoch echo differs")
        if output.integer_echoes.get(32) != self._runtime_protocol or output.integer_echoes.get(33) != hi or output.integer_echoes.get(34) != lo:
            raise LiveWriterError("runtime digest echo differs")
        if output.integer_echoes.get(26) == 90:
            raise LiveWriterError(
                "resident TP is STOPPED: "
                f"reason={output.integer_echoes.get(28)} "
                f"return_guard={output.integer_echoes.get(31)}"
            )

    def _observe_runtime(self, output: R004OutputSnapshot) -> None:
        if output.integer_echoes.get(24) == 0 and output.integer_echoes.get(26) == 78:
            return
        if output.observed_at_s <= self._last_identity_observed_s:
            return
        evidence = RuntimeIdentityEvidence(
            program=self.contract.raw["program"],
            script_sha256=self.prerequisites.controller.script_sha256,
            runtime_protocol=output.integer_echoes[32],
            runtime_digest_hi=output.integer_echoes[33],
            runtime_digest_lo=output.integer_echoes[34],
            session_epoch=output.integer_echoes[24],
            resident_session_id=self.prerequisites.resident_session_id,
            program_running=output.program_running,
            uninterrupted=True,
            observed_at_s=output.observed_at_s,
        )
        if not self.session.observe_runtime(evidence):
            raise LiveWriterError(self.session.identity.invalidated_reason or "runtime identity rejected")
        self._last_identity_observed_s = output.observed_at_s

    def _poll_checked(
        self,
        *,
        require_stationary: bool = False,
        allow_prearm_epoch: bool = False,
        allow_empty_cache: bool = False,
    ) -> R004OutputSnapshot | None:
        if allow_empty_cache and not allow_prearm_epoch:
            raise LiveWriterError("empty RTDE output allowance is pre-ARM only")
        rtde, _kunwei = self._transport_pair()
        output = rtde.poll_output()
        if output is None:
            self._last_poll_was_fresh = False
            if self._wall_clock() - self._last_output_seen_wall_s >= RUNTIME_OUTPUT_MAX_AGE_S:
                raise LiveWriterError("runtime identity output is stale")
            if self._last_output is None:
                if allow_empty_cache:
                    return None
                raise LiveWriterError("r004 runtime output is missing")
            return self._last_output
        self._last_poll_was_fresh = True
        self._validate_output(
            output,
            require_stationary=require_stationary,
            allow_prearm_epoch=allow_prearm_epoch,
        )
        if self._last_rtde_frame_sequence is not None:
            if output.timestamp < self._last_rtde_frame_sequence:
                raise LiveWriterError("RTDE controller timestamp regressed")
            if output.timestamp == self._last_rtde_frame_sequence:
                # A transport test double or a non-draining adapter may hand
                # back the same frame object instead of returning None.  It is
                # still a cached frame: preserve the prior TP image and do not
                # let the host mint a second control packet for it.
                self._last_poll_was_fresh = False
                if self._wall_clock() - self._last_output_seen_wall_s >= RUNTIME_OUTPUT_MAX_AGE_S:
                    raise LiveWriterError("runtime identity output is stale")
                if self._last_output is None:
                    if allow_empty_cache:
                        return None
                    raise LiveWriterError("r004 runtime output is missing")
                return self._last_output
        self._last_rtde_frame_mono_s = self._mono_clock()
        self._last_rtde_frame_sequence = output.timestamp
        self._observe_runtime(output)
        self._last_output = output
        self._last_output_seen_wall_s = self._wall_clock()
        return output

    def _wait_prearm_fresh_output(self) -> R004OutputSnapshot:
        """Wait for one real post-reconnect RTDE frame, never for cache."""

        deadline = self._mono_clock() + PREARM_FIRST_FRAME_WAIT_S
        for poll_index in range(PREARM_FIRST_FRAME_MAX_POLLS):
            output = self._poll_checked(
                allow_prearm_epoch=True,
                allow_empty_cache=True,
            )
            if output is not None and self._last_poll_was_fresh:
                return output
            if poll_index + 1 >= PREARM_FIRST_FRAME_MAX_POLLS:
                break
            now = self._mono_clock()
            if now >= deadline:
                break
            self._sleep(min(PREARM_FIRST_FRAME_POLL_S, deadline - now))
        raise LiveWriterError("r004 pre-ARM fresh runtime output timeout")

    def _assert_prearm_state(self) -> None:
        """Require the resident writer to still own a verified Home boundary."""

        if not self.active:
            raise LiveWriterError("r004 pre-ARM recovery requires an active writer")
        if self.session.phase is not SessionPhase.READY_HOME_NEXT:
            raise LiveWriterError("r004 pre-ARM recovery is only valid at READY_HOME_NEXT")
        if self.authority is None or not self._authority_active:
            raise LiveWriterError("r004 pre-ARM recovery lost the single-writer authority")
        try:
            self.authority.assert_active()
        except Exception as exc:
            raise LiveWriterError("r004 pre-ARM recovery single-writer gate failed") from exc
        if self._home is None or self._home.q is None:
            raise LiveWriterError("fixed Home reference is unavailable before ARM")

    def _assert_prearm_home_boundary(
        self,
        output: R004OutputSnapshot | None = None,
    ) -> None:
        """Revalidate identity, Safety, stationary, and fixed-Home evidence."""

        self._assert_prearm_state()
        candidate = self._last_output if output is None else output
        if candidate is None:
            raise LiveWriterError("verified Home/no-motion output is missing before ARM")
        self._validate_output(candidate, require_stationary=True, allow_prearm_epoch=True)
        home_decision = evaluate_entry_home(
            load_fixed_home_profile(),
            tuple(candidate.tcp_pose_m_rad),
            tuple(candidate.q_rad),
            locked_branch=IKBranchLock(tuple(self._home.q)),
        )
        if not home_decision.passed:
            raise LiveWriterError(
                "ATTEMPT_ENTRY_NOT_CAPTURED_HOME: fixed Home pose/q gate failed before ARM"
            )

    @staticmethod
    def _is_prearm_transport_fault(exc: BaseException) -> bool:
        """Only canonical RTDE transport loss may enter the one-shot recovery."""

        if isinstance(exc, (ConnectionError, OSError)):
            return True
        if not isinstance(exc, TransportError):
            return False
        detail = str(exc).lower()
        return any(
            marker in detail
            for marker in (
                "canonical r004 rtde output read failed",
                "rtde socket closed",
                "rtde transport is not open",
            )
        )

    def _reopen_prearm_rtde(self) -> None:
        """Close and reopen the one existing RTDE transport at verified Home."""

        self._assert_prearm_state()
        rtde = self._controller_transport
        if rtde is None:
            raise LiveWriterError("r004 pre-ARM recovery has no canonical RTDE transport")
        # Do not let a post-reconnect empty poll fall back to a stale cached
        # output.  The retry must bind to one fresh canonical RTDE frame.
        self._last_output = None
        self._last_rtde_frame_mono_s = None
        self._last_rtde_frame_sequence = None
        self._last_output_seen_wall_s = self._wall_clock()
        rtde.close()
        rtde.open()
        self._assert_prearm_state()

    def _prearm_output(self, *, allow_reconnect: bool) -> R004OutputSnapshot:
        """Read a stationary Home frame, with at most one r005-only retry."""

        if not isinstance(allow_reconnect, bool):
            raise LiveWriterError("pre-ARM reconnect policy is not typed")
        self._assert_prearm_home_boundary()
        reconnects = 0
        stationary_deadline = self._mono_clock() + 0.250
        while True:
            try:
                output = self._poll_checked(allow_prearm_epoch=True)
            except Exception as exc:
                if (
                    not allow_reconnect
                    or reconnects >= PREARM_RECONNECT_MAX
                    or not self._is_prearm_transport_fault(exc)
                ):
                    raise
                reconnects += 1
                self._reopen_prearm_rtde()
                # The V3 connection boundary invalidates the old frame cache;
                # receive a distinct post-reopen frame before any Home check
                # can lead to the ARM mutation below.  Exceptions here are
                # deliberately outside the retry loop: no second reconnect.
                output = self._wait_prearm_fresh_output()
            if output.stationary:
                self._assert_prearm_home_boundary(output)
                return output
            if self._mono_clock() >= stationary_deadline:
                raise LiveWriterError("runtime pre-ARM stationary dwell timeout")
            self._sleep(0.010)

    def open(self, *, live_ack: str, now_s: float | None = None) -> None:
        if live_ack != LIVE_ACK:
            raise LiveWriterError("explicit r004 live acknowledgement is required")
        if self._opened:
            raise LiveWriterError("r004 live writer is already open")
        entry_now = self._wall_clock() if now_s is None else float(now_s)
        try:
            self.prerequisites.validate(now_s=entry_now)
            self._begin_authority()
            rtde, kunwei = self._transport_pair()
            rtde.open()
            kunwei.open()
            raw, observed_at = kunwei.poll()
            first_frame_deadline = self._mono_clock() + PACKET_STALE_S
            while raw is None and self._mono_clock() < first_frame_deadline:
                self._sleep(0.002)
                raw, observed_at = kunwei.poll()
            if raw is None:
                raise LiveWriterError("Kunwei stream has no fresh wrench before entry")
            # Reuse V3's pre-control settling semantics: Kunwei's TCP stream can
            # deliver 50-frame startup batches for roughly 0.4 s even though
            # its steady stream is 1 kHz.  Settle before any attempt timing is
            # collected; this is not a motion or sensor-configuration write.
            warmup_deadline = self._mono_clock() + KUNWEI_STREAM_WARMUP_S
            while self._mono_clock() < warmup_deadline:
                candidate_raw, candidate_observed_at = kunwei.poll()
                if candidate_raw is not None:
                    raw, observed_at = candidate_raw, candidate_observed_at
                self._sleep(0.002)
            # Reconcile a resident TP packet/cache echo before the first write.
            # The controller may retain this register across Play/restart; the
            # first new host packet must therefore be strictly newer than it.
            output = rtde.poll_output()
            if output is None:
                raise LiveWriterError("r004 runtime output is missing at entry")
            self._validate_output(output, require_stationary=True, allow_prearm_epoch=True)
            if output.consumed_packet_sequence < -1:
                raise LiveWriterError("resident TP packet sequence echo is invalid")
            self._last_poll_was_fresh = True
            self._last_rtde_frame_mono_s = self._mono_clock()
            self._last_rtde_frame_sequence = output.timestamp
            self._session_command_sequence = max(
                self._session_command_sequence,
                output.integer_echoes[29],
            )
            self._packet_sequence = max(
                self._packet_sequence,
                output.consumed_packet_sequence + 1,
            )
            self._send_packet(self._sensor_packet(raw=raw, observed_at_s=observed_at), command_mode=CommandMode.HOLD)
            self._last_output = output
            self._last_output_seen_wall_s = self._wall_clock()
            home_profile = load_fixed_home_profile()
            home_decision = evaluate_entry_home(
                home_profile,
                tuple(output.tcp_pose_m_rad),
                tuple(output.q_rad),
            )
            if not home_decision.passed or home_decision.branch is None:
                raise LiveWriterError(
                    "ATTEMPT_ENTRY_NOT_CAPTURED_HOME: fixed Cartesian Home differs at Play"
                )
            self.session.play(
                controller_receipt=self.prerequisites.controller,
                script1_receipt=self.prerequisites.script1,
                now_s=self.prerequisites.controller.observed_at_s,
                epoch=self.prerequisites.session_epoch,
                session_id=self.prerequisites.resident_session_id,
                expected_triplet=dict(self.prerequisites.expected_triplet),
                home=HomeReference(tuple(home_profile.pose), tuple(home_decision.branch.q)),
            )
            self._home = self.session.home
            self._observe_runtime(output)
            self._opened = True
        except Exception as exc:
            self._failed_closed = True
            try:
                self.close()
            except Exception as cleanup_exc:
                raise LiveWriterError(
                    f"r004 live entry failed and cleanup failed: {exc}; {cleanup_exc}"
                ) from exc
            raise LiveWriterError(str(exc)) from exc

    def _read_sensor(self) -> SensorPacket:
        _rtde, kunwei = self._transport_pair()
        raw, observed_at = kunwei.poll()
        transport_sequence = getattr(kunwei, "distinct_frame_sequence", None)
        if isinstance(transport_sequence, int) and not isinstance(transport_sequence, bool):
            if transport_sequence < 0:
                raise LiveWriterError("Kunwei cumulative frame sequence is negative")
            if self._last_kunwei_frame_sequence is not None and transport_sequence < self._last_kunwei_frame_sequence:
                raise LiveWriterError("Kunwei cumulative frame sequence regressed")
            if transport_sequence != self._last_kunwei_frame_sequence:
                self._last_kunwei_frame_sequence = transport_sequence
                if observed_at is not None:
                    self._last_kunwei_observed_s = float(observed_at)
        elif observed_at is not None and (
            self._last_kunwei_observed_s is None
            or float(observed_at) > self._last_kunwei_observed_s
        ):
            # Compatibility for injected test transports without the explicit
            # cumulative counter; never refresh a cached observation.
            self._last_kunwei_frame_sequence = (
                1 if self._last_kunwei_frame_sequence is None else self._last_kunwei_frame_sequence + 1
            )
            self._last_kunwei_observed_s = float(observed_at)
        return self._sensor_packet(raw=raw, observed_at_s=observed_at)

    def set_baseline_state(self, *, consecutive_successes: int, sticky_one_newton_latched: int) -> None:
        if not 0 <= consecutive_successes <= 3 or sticky_one_newton_latched not in (0, 1):
            raise LiveWriterError("baseline wire state is outside the bounded r004 contract")
        self._baseline_successes = int(consecutive_successes)
        self._sticky_latched = int(sticky_one_newton_latched)

    def bind_committed_qualifications(self, ledger: DurableCampaignLedger) -> int:
        """Expose only fsynced/cold-read/hash-verified qualification successes."""

        rows = verify_ledger_hash_chain(ledger.path) if ledger.path.exists() else ()
        completed = [
            row
            for row in rows
            if row.get("phase") == "QUALIFICATION"
            and row.get("status") == "completed"
            and row.get("qualification_passed") is True
        ]
        ordinals = [int(row["logical_attempt_ordinal"]) for row in completed]
        if ordinals != list(range(1, len(ordinals) + 1)) or len(ordinals) > 3:
            raise LiveWriterError("durable qualification sequence is not contiguous 1..3")
        self._baseline_successes = len(ordinals)
        return self._baseline_successes

    def commit_qualification_success(
        self,
        *,
        ordinal: int,
        ledger: DurableCampaignLedger,
    ) -> None:
        committed = self.bind_committed_qualifications(ledger)
        if committed != ordinal:
            raise LiveWriterError("qualification count changed before durable row verification")

    def arm(
        self,
        *,
        ordinal: int,
        kind: AttemptKind,
        candidate_token: int,
        ledger: DurableCampaignLedger | None,
        resume: bool = False,
        unbounded: bool = False,
        allow_prearm_reconnect: bool = False,
    ) -> LiveWriterTick:
        if not isinstance(unbounded, bool):
            raise LiveWriterError("unbounded ARM policy is not typed")
        if not isinstance(allow_prearm_reconnect, bool):
            raise LiveWriterError("pre-ARM reconnect policy is not typed")
        if unbounded:
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
                raise LiveWriterError("unbounded ARM ordinal must be positive")
            if not isinstance(kind, AttemptKind):
                raise LiveWriterError("unbounded ARM kind is not typed")
        else:
            expected_kind = (
                AttemptKind.QUALIFICATION
                if ordinal <= 3
                else AttemptKind.BATCH_A
                if ordinal <= 8
                else AttemptKind.BATCH_B
                if ordinal <= 13
                else AttemptKind.RETEST
            )
            if not 1 <= ordinal <= 16 or kind is not expected_kind:
                raise LiveWriterError("r004 ARM ordinal/kind differs from the 16-slot campaign")
        if not self.active:
            raise LiveWriterError("r004 writer is not active")
        try:
            if not unbounded and ledger is None:
                raise LiveWriterError("r004 ARM requires its durable campaign ledger")
            if not unbounded and not resume and not ledger.next_arm_allowed():
                raise LedgerError("next ARM is forbidden before durable row verification")
            if not unbounded and ledger.path.exists():
                verify_ledger_hash_chain(ledger.path)
            if resume and ledger is not None:
                rows = ledger.rows
                if not rows or rows[-1].get("status") != "interrupted" or rows[-1].get("gp_eligible") is True:
                    raise LedgerError("resume ARM requires the verified interrupted row boundary")
            # Match the proven V3 lifecycle: finish any explicit cyclic GC
            # collection while still at the verified pre-ARM Home boundary.
            # This must remain before every ARM/session-command mutation; a
            # long collection after ARM can consume the TP freshness window.
            gc.collect()
            # This is deliberately before any ARM/session command mutation.
            # Once the session enters ARMING, all later faults remain
            # fail-closed and never reconnect.
            output = self._prearm_output(allow_reconnect=allow_prearm_reconnect)
            self._session_command_sequence += 1
            self._session_command = SessionCommand.ARM
            self._ordinal = ordinal
            self._kind = kind
            self._candidate_token = candidate_token
            self._sticky_latched = 0
            self._qualification_control = None
            self._path_command_started_mono_s = None
            self._path_rtde_origin_s = None
            request = ArmRequest(
                command=SessionCommand.ARM,
                command_sequence=self._session_command_sequence,
                session_epoch=self.prerequisites.session_epoch,
                logical_attempt_ordinal=ordinal,
                attempt_kind=kind,
                candidate_token=candidate_token,
            )
            self.session.arm(request, ledger_ready=True, allow_unbounded=unbounded)
            packet = self._send_packet(
                self._read_sensor(),
                command_mode=CommandMode.HOLD,
            )
            expected_echoes = {
                24: self.prerequisites.session_epoch,
                25: ordinal,
                27: candidate_token,
                29: self._session_command_sequence,
                30: int(kind),
            }
            echo_deadline = self._mono_clock() + 0.200
            while True:
                output = self._poll_checked(allow_prearm_epoch=True)
                if self._last_poll_was_fresh:
                    # The resident TP enters its stationary dwell as soon as
                    # the ARM packet is consumed.  Keep the same ARM/session
                    # identity alive, but bind every keepalive to one distinct
                    # controller frame.  A cached RTDE image must never mint a
                    # new packet sequence (or hide the 80 ms TP freshness
                    # failure).
                    packet = self._send_packet(
                        self._read_sensor(),
                        command_mode=CommandMode.HOLD,
                    )
                    if output.integer_echoes.get(29) == self._session_command_sequence:
                        if any(
                            output.integer_echoes.get(register) != expected
                            for register, expected in expected_echoes.items()
                        ):
                            raise LiveWriterError("ARM output echo differs")
                        # The echo frame itself also needs a fresh HOLD before
                        # execute_attempt starts its TP stationary dwell.
                        break
                if self._mono_clock() >= echo_deadline:
                    raise LiveWriterError("ARM output echo timeout")
                self._sleep(0.002)
            self._entry_linear_speed_m_s = math.sqrt(sum(value * value for value in output.tcp_speed_m_s_rad_s[:3]))
            self._entry_angular_speed_rad_s = math.sqrt(sum(value * value for value in output.tcp_speed_m_s_rad_s[3:]))
            return LiveWriterTick(self._packet_sequence - 1, self._session_command_sequence, output, packet)
        except Exception as exc:
            self._fail_closed(str(exc))
            raise LiveWriterError(str(exc)) from exc

    def arm_unbounded(
        self,
        *,
        ordinal: int,
        kind: AttemptKind,
        candidate_token: int,
    ) -> LiveWriterTick:
        """r005 extension seam: retain the mature ARM/echo path without 16-slot policy."""

        return self.arm(
            ordinal=ordinal,
            kind=kind,
            candidate_token=candidate_token,
            ledger=None,
            unbounded=True,
            allow_prearm_reconnect=True,
        )

    def execute_attempt(
        self,
        attempt: Attempt,
        *,
        timeout_s: float = 180.0,
        allow_safe_nontrainable: bool = False,
    ) -> AttemptEvidence | QualificationEvidence:
        if not isinstance(allow_safe_nontrainable, bool):
            raise LiveWriterError("safe-nontrainable policy is not typed")
        if self.session.phase is not SessionPhase.RUNNING or self._ordinal != attempt.ordinal:
            raise LiveWriterError("attempt execution identity is not armed")
        if attempt.kind is not self._kind:
            raise LiveWriterError("armed attempt kind differs from campaign plan")
        self.candidate = attempt.candidate
        path_requested = attempt.kind is not AttemptKind.QUALIFICATION
        self._qualification_control = CanonicalQualificationControl(
            self.candidate,
            attempt_id=f"{self.attempt_id}-e{self.session_epoch}-o{attempt.ordinal}",
            release_contract=self.contract,
            path_requested=path_requested,
            canonical_runtime_only=self._canonical_runtime_only,
        )
        path_collector = (
            PathEvidenceCollector(require_path_boundary=True) if path_requested else None
        )
        qualification_collector = QualificationEvidenceCollector() if not path_requested else None
        qualification_timing = TimingEvidenceCollector()
        baseline_diag: dict[str, Any] = {
            "samples": 0,
            "readiness_samples": 0,
            "raw_min_n": math.inf,
            "raw_max_n": -math.inf,
            "filtered_min_n": math.inf,
            "filtered_max_n": -math.inf,
            "force_norm_max_n": 0.0,
            "torque_norm_max_nm": 0.0,
        }
        # The explicit collection was completed in arm(), before the ARM
        # session mutation. Keep cyclic GC out of the 500 Hz control path.
        gc_was_enabled = gc.isenabled()
        gc.disable()
        start = self._mono_clock()
        publish_period_s = 1.0 / 500.0
        next_publish_s = start
        seen_states: set[int] = set()
        terminal: R004OutputSnapshot | None = None
        try:
            while True:
                if self._mono_clock() - start >= timeout_s:
                    raise LiveWriterError("r004 attempt execution timeout")
                output = self._poll_checked()
                state = output.integer_echoes[26]
                seen_states.add(state)
                if not self._last_poll_was_fresh:
                    # The RTDE adapter deliberately returns no object for an
                    # equal controller timestamp; the writer's cached output
                    # is only a TP hold image.  Do not read a new sensor,
                    # advance control, publish, or manufacture duplicate
                    # PATH evidence from that cached frame.  The 80 ms check
                    # above remains the hard fail-closed boundary.
                    if state in {78, 80, 90}:
                        terminal = output
                        break
                    next_publish_s += publish_period_s
                    now_after_cached = self._mono_clock()
                    if next_publish_s <= now_after_cached:
                        missed = math.floor(
                            (now_after_cached - next_publish_s) / publish_period_s
                        ) + 1
                        next_publish_s += missed * publish_period_s
                    self._sleep(max(0.0, next_publish_s - now_after_cached))
                    continue
                sensor = self._read_sensor()
                now = self._mono_clock()
                path_elapsed_s: float | None = None
                path_clock_time_s: float | None = None
                path_end_fence = False
                if state == 25 and self._path_command_started_mono_s is not None:
                    path_elapsed_s = max(0.0, now - self._path_command_started_mono_s)
                    path_end_fence = path_elapsed_s >= 60.0
                if state == 25 and self._path_rtde_origin_s is not None:
                    path_clock_time_s = max(0.0, output.timestamp - self._path_rtde_origin_s)
                if state in {21, 25}:
                    if path_end_fence:
                        mode = CommandMode.PATH
                        setpoint = 5.0
                        qdot = (0.0,) * 6
                    else:
                        command = self._qualification_control.step(
                            output=output,
                            sensor=sensor,
                            monotonic_s=now,
                            command_sequence=self._packet_sequence,
                        )
                        self._sticky_latched = command.sticky_one_newton_latched
                        sensor = replace(sensor, filtered_normal_n=command.filtered_normal_n)
                        mode = command.command_mode
                        setpoint = command.internal_setpoint_n
                        qdot = command.qdot
                    # V3 starts trajectory progress only after the controller
                    # reports stage 25.  PATH is intentionally published near
                    # the end of state 21 so the TP can transition, but the TP
                    # still owns a bounded stationary dwell before state 25.
                    # Starting here during state 21 skips that dwell from the
                    # trajectory and under-reports the observed 60 s window.
                    if (
                        state == 25
                        and mode is CommandMode.PATH
                        and self._path_command_started_mono_s is None
                    ):
                        self._path_command_started_mono_s = now
                        path_elapsed_s = 0.0
                        path_clock_time_s = 0.0
                        self._path_rtde_origin_s = output.timestamp
                        if self._host_hard_tube is not None:
                            self._host_hard_tube.reset()
                        if path_collector is not None:
                            path_collector.mark_path_start(
                                observed_at_s=now,
                                rtde_timestamp_s=output.timestamp,
                                tp_sequence=output.consumed_packet_sequence,
                            )
                    # Host-side HardTube (B3): progress = host path_time_s, not TP float31.
                    self._last_host_hard_tube = None
                    if (
                        self._host_hard_tube is not None
                        and self._host_hard_tube.enabled
                        and state == 25
                        and mode is CommandMode.PATH
                        and not path_end_fence
                    ):
                        ht_path_time = (
                            path_clock_time_s
                            if path_clock_time_s is not None
                            else path_elapsed_s
                        )
                        ht = self._host_hard_tube.evaluate(
                            tp_state=state,
                            path_time_s=ht_path_time,
                            tcp_pose_m_rad=output.tcp_pose_m_rad,
                            monotonic_ns=time.monotonic_ns(),
                            controller_timestamp_s=output.timestamp,
                        )
                        # as_dict() is JSON-safe (None, never NaN/Inf).
                        self._last_host_hard_tube = ht.as_dict()
                        if ht.stop:
                            self._send_safe_stop()
                            raise LiveWriterError(
                                f"host HardTube stop: {ht.reason} "
                                f"dist_m={ht.actual_distance_m}"
                            )
                    if state == 21:
                        baseline_diag["samples"] += 1
                        baseline_diag["raw_min_n"] = min(baseline_diag["raw_min_n"], sensor.normal_load_n)
                        baseline_diag["raw_max_n"] = max(baseline_diag["raw_max_n"], sensor.normal_load_n)
                        baseline_diag["filtered_min_n"] = min(baseline_diag["filtered_min_n"], sensor.filtered_normal_n)
                        baseline_diag["filtered_max_n"] = max(baseline_diag["filtered_max_n"], sensor.filtered_normal_n)
                        baseline_diag["force_norm_max_n"] = max(baseline_diag["force_norm_max_n"], sensor.force_norm_n)
                        baseline_diag["torque_norm_max_nm"] = max(baseline_diag["torque_norm_max_nm"], sensor.torque_norm_nm)
                        baseline_diag["last"] = {
                            "raw_normal_n": sensor.normal_load_n,
                            "filtered_normal_n": sensor.filtered_normal_n,
                            "setpoint_n": setpoint,
                            "force_norm_n": sensor.force_norm_n,
                            "torque_norm_nm": sensor.torque_norm_nm,
                            "mode": int(mode),
                        }
                        if (
                            sensor.sensor_fresh
                            and 4.0 <= sensor.filtered_normal_n <= 6.0
                            and 3.0 <= sensor.normal_load_n <= 7.0
                            and sensor.force_norm_n <= 7.0
                            and sensor.torque_norm_nm <= 0.30
                        ):
                            baseline_diag["readiness_samples"] += 1
                else:
                    mode = CommandMode.HOLD
                    setpoint = 1.0
                    qdot = (0.0,) * 6
                packet: WirePacket | None = None
                if state not in {78, 80, 90}:
                    packet = self._send_packet(
                        sensor,
                        command_mode=mode,
                        proposed_qdot=qdot,
                        internal_setpoint_n=setpoint,
                    )
                source_sequences: dict[str, Any] = {}
                source_ages: dict[str, float] = {}
                if packet is not None and self._last_writer_publish_mono_s is not None:
                    source_sequences["writer"] = packet.sequence
                    source_ages["writer"] = max(0.0, now - self._last_writer_publish_mono_s)
                if self._last_rtde_frame_sequence is not None and self._last_rtde_frame_mono_s is not None:
                    source_sequences["rtde"] = self._last_rtde_frame_sequence
                    source_ages["rtde"] = max(0.0, now - self._last_rtde_frame_mono_s)
                if self._last_kunwei_frame_sequence is not None and self._last_kunwei_observed_s is not None:
                    source_sequences["kunwei"] = self._last_kunwei_frame_sequence
                    source_ages["kunwei"] = max(0.0, now - self._last_kunwei_observed_s)
                consumed_sequence = output.consumed_packet_sequence
                consumed_at = self._writer_publish_times.get(consumed_sequence)
                if consumed_sequence >= 0 and consumed_at is not None:
                    source_sequences["tp"] = consumed_sequence
                    source_ages["tp"] = max(0.0, now - consumed_at)

                if qualification_collector is not None:
                    qualification_collector.observe(
                        QualificationSample(
                            observed_at_s=now,
                            filtered_normal_n=sensor.filtered_normal_n,
                            internal_setpoint_n=setpoint,
                            sensor_fresh=sensor.sensor_fresh,
                            safety_normal=output.safety_normal,
                            state=state,
                            command_mode=int(mode),
                            sticky_one_newton_latched=self._sticky_latched,
                        )
                    )
                    # Qualification's host-driven closed loop is state 21.
                    # State 20 is the TP-owned contact search; RETRACT and
                    # return-home use bounded blocking TP primitives and are
                    # proven by separate contact/return gates.
                    if (
                        state == 21
                        and mode is CommandMode.BASELINE
                        and len(source_sequences) == 4
                        and len(source_ages) == 4
                    ):
                        qualification_timing.observe_layered_sample(
                            now,
                            source_sequences=source_sequences,
                            source_ages_s=source_ages,
                            epoch=self.session_epoch,
                        )
                elif path_collector is not None:
                    sample_kwargs: dict[str, Any] = {}
                    if (
                        state == 25
                        and self._path_command_started_mono_s is not None
                        and path_elapsed_s is not None
                        and path_elapsed_s < 60.0
                        and path_clock_time_s is not None
                        and path_clock_time_s < 60.0
                        and len(source_sequences) == 4
                        and len(source_ages) == 4
                    ):
                        path_time_s = path_clock_time_s
                        reference = step5_path_reference(
                            "step5d_strict_rnn_autotune_v1",
                            tuple(output.tcp_pose_m_rad[:2]),
                            path_time_s,
                        )
                        sample_kwargs = {
                            "desired_xy_m": tuple(reference["desired_xy"]),
                            "actual_xy_m": tuple(output.tcp_pose_m_rad[:2]),
                            "path_time_s": float(reference["path_time_s"]),
                            "path_phase": min(6, max(0, int(round(float(reference["phase_rad"]))))),
                            "desired_velocity_m_s": tuple(reference["desired_velocity_xy"]),
                            "actual_velocity_m_s": tuple(output.tcp_speed_m_s_rad_s[:2]),
                            "qdot": self._qdot_by_packet_sequence.get(
                                consumed_sequence, tuple(float(value) for value in qdot)
                            ),
                            "actual_qd": tuple(output.qd_rad_s),
                            "source_ages_s": source_ages,
                            "source_sequences": source_sequences,
                            "tube_cbf": getattr(
                                self._qualification_control, "last_tube_cbf", None
                            ),
                            "host_hard_tube": self._last_host_hard_tube,
                            "qd_lag_s": source_ages["tp"],
                        }
                    if state != 25 or (
                        sample_kwargs
                        and sample_kwargs.get("path_time_s") is not None
                        and float(sample_kwargs["path_time_s"]) < 60.0
                    ):
                        observed_path_sample = PathSample(
                            observed_at_s=now,
                            filtered_normal_n=sensor.filtered_normal_n,
                            force_norm_n=sensor.force_norm_n,
                            torque_norm_nm=sensor.torque_norm_nm,
                            sensor_fresh=sensor.sensor_fresh,
                            safety_normal=output.safety_normal,
                            state=state,
                            **sample_kwargs,
                        )
                        accepted_path_sample = path_collector.observe(observed_path_sample)
                        if (
                            accepted_path_sample
                            and self._path_sample_sink is not None
                            and observed_path_sample.state == 25
                            and observed_path_sample.path_time_s is not None
                            and observed_path_sample.path_time_s < 60.0
                        ):
                            self._path_sample_sink(observed_path_sample)
                if state in {78, 80, 90}:
                    terminal = output
                    break
                # V3/r034-style absolute-deadline scheduler. Never add a fixed
                # sleep to compute/transport time; skip missed slots instead of
                # bursting stale packets, while actual_dt remains measured.
                next_publish_s += publish_period_s
                now_after_tick = self._mono_clock()
                if next_publish_s <= now_after_tick:
                    missed = math.floor((now_after_tick - next_publish_s) / publish_period_s) + 1
                    next_publish_s += missed * publish_period_s
                self._sleep(max(0.0, next_publish_s - now_after_tick))
            if terminal is None or terminal.integer_echoes[26] == 90 or self._home is None:
                raise LiveWriterError("r004 attempt terminated without a valid return")
            pose_error = math.dist(terminal.tcp_pose_m_rad[:3], self._home.pose[:3])
            orientation_error = math.dist(terminal.tcp_pose_m_rad[3:], self._home.pose[3:])
            q_error = max(abs(actual - expected) for actual, expected in zip(terminal.q_rad, self._home.q or (0.0,) * 6, strict=True))
            guard = terminal.integer_echoes[31]
            return_evidence = ReturnEvidence(
                stationary=terminal.stationary,
                retract_z_m=0.005 if guard & 2 else 0.0,
                transfer_floor_z_m=0.0,
                entry_linear_speed_m_s=self._entry_linear_speed_m_s,
                return_linear_speed_m_s=math.sqrt(sum(value * value for value in terminal.tcp_speed_m_s_rad_s[:3])),
                entry_angular_speed_rad_s=self._entry_angular_speed_rad_s,
                return_angular_speed_rad_s=math.sqrt(sum(value * value for value in terminal.tcp_speed_m_s_rad_s[3:])),
                descended_to_captured_home=bool(guard & 32),
                home_pose_error_m=pose_error,
                home_orientation_error_rad=orientation_error,
                home_q_error_rad=q_error,
                safety_gate_passed=terminal.safety_normal,
                contact_gate_passed={20, 21}.issubset(seen_states),
            )
            decision = evaluate_return(return_evidence)
            self.session.finish_attempt(decision)
            home_proof = {
                "stationary": terminal.stationary,
                "retract_z_m": return_evidence.retract_z_m,
                "fixed_home_route": True,
                "home_pose_error_m": pose_error,
                "home_orientation_error_rad": orientation_error,
                "home_q_error_rad": q_error,
                "return_guard": guard,
            }
            if qualification_collector is not None:
                evidence: AttemptEvidence | QualificationEvidence = qualification_collector.finalize(
                    return_gate_passed=decision.passed,
                    home_proof=home_proof,
                    timing_evidence=qualification_timing.finalize(),
                )
            elif path_collector is not None:
                evidence = path_collector.finalize(
                    return_gate_passed=decision.passed,
                    home_proof=home_proof,
                    contact_gate_passed={20, 21, 25}.issubset(seen_states),
                )
            else:  # pragma: no cover - exhaustive construction above
                raise LiveWriterError("r004 attempt evidence collector is unavailable")
            if not decision.passed or (not evidence.eligible and not allow_safe_nontrainable):
                self._stopped = True
                timing_detail = (
                    None
                    if evidence.timing_evidence is None
                    else evidence.timing_evidence.as_dict()
                )
                raise LiveWriterError(
                    f"{decision.reason or 'r004 attempt evidence gate failed'}; "
                    f"eligible={evidence.eligible}; metrics={json.dumps(dict(evidence.metrics), sort_keys=True)}; "
                    f"timing={json.dumps(timing_detail, sort_keys=True)}"
                )
            return evidence
        except Exception as exc:
            diagnostic = {
                key: value
                for key, value in baseline_diag.items()
                if not isinstance(value, float) or math.isfinite(value)
            }
            detail = f"{exc}; baseline_diag={json.dumps(diagnostic, sort_keys=True, allow_nan=False)}"
            self._fail_closed(detail)
            raise LiveWriterError(detail) from exc
        finally:
            if gc_was_enabled and not gc.isenabled():
                gc.enable()

    def stop(self, reason: str = "operator_stop") -> None:
        if self._stopped:
            return
        self._failed_closed = True
        self._stopped = True
        if self.session.phase is not SessionPhase.STOPPED:
            self._session_command_sequence += 1
            self._session_command = SessionCommand.STOP
            try:
                self.session.stop(self._session_command_sequence, reason_code=4)
            except SessionProtocolError:
                self.session.fault(reason, reason_code=4)
        self._send_safe_stop()

    def _fail_closed(self, reason: str) -> None:
        self._failed_closed = True
        self.stop(reason)

    def close(self) -> None:
        errors: list[Exception] = []
        revoke_reason = "failed" if self._failed_closed else "completed"
        try:
            self._send_safe_stop()
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            errors.append(exc)
        # STOP/HOLD-safe RTDE input must be attempted while RTDE is still open;
        # then stop the sensor stream, close RTDE, and only then release the
        # canonical single-writer lease.
        for transport in (self._kunwei_transport, self._controller_transport):
            if transport is not None:
                try:
                    transport.close()
                except Exception as exc:  # pragma: no cover - defensive cleanup path
                    errors.append(exc)
        try:
            self._revoke_authority(reason=revoke_reason)
        except Exception as exc:  # pragma: no cover - defensive cleanup path
            errors.append(exc)
        self._opened = False
        if errors:
            raise LiveWriterError("r004 cleanup encountered a transport/lease error") from errors[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-self-check", action="store_true")
    parser.add_argument("--mode", default="offline")
    parser.add_argument("--live-ack")
    parser.add_argument("--controller-host")
    parser.add_argument("--kunwei-host")
    parser.add_argument("--kunwei-port", type=int)
    parser.add_argument("--controller-receipt", type=Path)
    parser.add_argument("--script1-receipt", type=Path)
    parser.add_argument("--runtime-evidence", type=Path)
    parser.add_argument("--authority-root", type=Path)
    parser.add_argument("--route-id")
    parser.add_argument("--attempt-id")
    parser.add_argument("--session-id")
    parser.add_argument("--session-epoch", type=int)
    parser.add_argument("--input-baseline-ledger-sha256")
    parser.add_argument("--software-baseline-n", nargs=6, type=float)
    args = parser.parse_args(argv)
    if args.mode == "live":
        if args.live_ack != LIVE_ACK:
            parser.error(f"live mode requires --live-ack {LIVE_ACK}")
        required = {
            "--controller-host": args.controller_host,
            "--kunwei-host": args.kunwei_host,
            "--kunwei-port": args.kunwei_port,
            "--controller-receipt": args.controller_receipt,
            "--script1-receipt": args.script1_receipt,
            "--runtime-evidence": args.runtime_evidence,
            "--authority-root": args.authority_root,
            "--route-id": args.route_id,
            "--attempt-id": args.attempt_id,
            "--session-id": args.session_id,
            "--session-epoch": args.session_epoch,
            "--input-baseline-ledger-sha256": args.input_baseline_ledger_sha256,
            "--software-baseline-n": args.software_baseline_n,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error("live mode requires explicit " + ", ".join(missing))
        contract = load_contract()
        triplet = {
            suffix: hashlib.sha256(
                (ROOT / f"programs/step5/step5d/step5d_strict_rnn_autotune_v4_r004{suffix}").read_bytes()
            ).hexdigest()
            for suffix in (".script", ".txt", ".urp")
        }
        prerequisites = load_prerequisites(
            contract,
            controller_path=args.controller_receipt,
            script1_path=args.script1_receipt,
            runtime_path=args.runtime_evidence,
            expected_triplet={"script": triplet[".script"], "txt": triplet[".txt"], "urp": triplet[".urp"]},
            route_id=args.route_id,
            session_epoch=args.session_epoch,
            resident_session_id=args.session_id,
            input_baseline_ledger_sha256=args.input_baseline_ledger_sha256,
        )
        writer = LiveR004Writer(
            prerequisites,
            authority_root=args.authority_root,
            route_id=args.route_id,
            attempt_id=args.attempt_id,
            controller_host=args.controller_host,
            kunwei_host=args.kunwei_host,
            kunwei_port=args.kunwei_port,
            software_baseline_n=args.software_baseline_n,
        )
        try:
            writer.open(live_ack=args.live_ack)
            print(json.dumps({"schema": LIVE_WRITER_SCHEMA, "live_boundary_verified": True, "dashboard_actions": False, "load": False, "play": False}, sort_keys=True))
        finally:
            writer.close()
        return 0
    if args.mode != "offline":
        parser.error("--mode must be offline or live")
    if not args.offline_self_check:
        parser.error("use --offline-self-check; controller/Kunwei transport is deliberately unavailable")
    writer = OfflineR004Writer()
    sensor = SensorPacket(
        normal_load_n=0.0,
        force_norm_n=0.0,
        heartbeat=1.0,
        sensor_fresh=True,
        stop_request=False,
        eoat_get_ack=True,
        torque_norm_nm=0.0,
        wrench=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
        filtered_normal_n=0.0,
    )
    tick = writer.publish(sensor=sensor)
    print(
        json.dumps(
            {
                "schema": WRITER_SCHEMA,
                "offline": True,
                "packet_sequence": tick.packet_sequence,
                "session_command_sequence": tick.session_command_sequence,
                "wire_layout": 606,
                "delivery": {"controller": False, "kunwei": False},
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LIVE_ACK",
    "LIVE_WRITER_SCHEMA",
    "LiveR004Writer",
    "LiveWriterError",
    "OfflineR004Writer",
    "WriterTick",
    "WRITER_SCHEMA",
]
