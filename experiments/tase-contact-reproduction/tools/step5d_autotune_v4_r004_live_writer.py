#!/usr/bin/env python3
"""Isolated V4/r004 register writer with an explicit fail-closed live gate.

The live path assumes Script1 and Script2 were separately delivered and
Remote-loaded/played by the owner route.  It verifies receipts and a resident
session; it never opens Dashboard and never performs Load or Play.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import time
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import step5d_bridge_authority as canonical_authority  # noqa: E402
from step5d_autotune_v4_r004.campaign import Attempt, build_campaign_plan  # noqa: E402
from step5d_autotune_v4_r004.contracts import Candidate, load_contract, RUNTIME_PROTOCOL, runtime_identity_limbs  # noqa: E402
from step5d_autotune_v4_r004.evidence import AttemptEvidence, EvidenceError, PathEvidenceCollector, PathSample  # noqa: E402
from step5d_autotune_v4_r004.fake_rtde import FakeRTDE  # noqa: E402
from step5d_autotune_v4_r004.home import HomeReference, ReturnEvidence, evaluate_return  # noqa: E402
from step5d_autotune_v4_r004.identity import RuntimeIdentityEvidence  # noqa: E402
from step5d_autotune_v4_r004.ledger import DurableCampaignLedger, LedgerError, verify_ledger_hash_chain  # noqa: E402
from step5d_autotune_v4_r004.prerequisites import LivePrerequisites, PrerequisiteError, load_prerequisites  # noqa: E402
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


WRITER_SCHEMA = "step5d.autotune-v4/r004-offline-writer-v1"
LIVE_WRITER_SCHEMA = "step5d.autotune-v4/r004-live-writer-v1"
LIVE_ACK = "RUN_LIVE_R004_WITH_EXPLICIT_ACK"
RUNTIME_OUTPUT_MAX_AGE_S = 0.300


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
        self.fake_rtde = FakeRTDE(tp_hz=500.0, writer_hz=125.0)
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
    ) -> None:
        if route_id != prerequisites.route_id:
            raise LiveWriterError("r004 route id differs from receipt bundle")
        if not isinstance(route_id, str) or "r004" not in route_id.lower():
            raise LiveWriterError("r004 route identity is required")
        if not isinstance(attempt_id, str) or not attempt_id or "r004" not in attempt_id.lower():
            raise LiveWriterError("distinct r004 attempt identity is required")
        baseline = tuple(float(value) for value in software_baseline_n)
        if len(baseline) != 6 or not all(math.isfinite(value) for value in baseline):
            raise LiveWriterError("software baseline must contain six finite SI values")
        self.contract = prerequisites.contract
        self.prerequisites = prerequisites
        self.authority_root = Path(authority_root)
        self.route_id = route_id
        self.attempt_id = attempt_id
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
                fresh = self._mono_clock() - float(observed_at_s) < 0.080
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
        structural_stop: bool = False,
    ) -> WirePacket:
        rtde, _kunwei = self._transport_pair()
        mode = CommandMode.STOP if self._stopped else command_mode
        packet = build_wire_packet(
            self.contract,
            self.candidate,
            sensor=sensor,
            proposed_qdot=(0.0,) * 6 if mode in {CommandMode.HOLD, CommandMode.STOP} else proposed_qdot,
            internal_setpoint_n=1.0,
            packet_sequence=self._packet_sequence,
            session=self._session_input(mode),
            structural_stop=structural_stop or self._stopped,
        )
        rtde.send_packet(packet.double_values, packet.integer_values)
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

    def _validate_output(self, output: R004OutputSnapshot, *, require_stationary: bool = False) -> None:
        payload, cog, tcp = expected_eoat()
        if not math.isclose(output.payload_kg, payload, rel_tol=0.0, abs_tol=0.0005):
            raise LiveWriterError("runtime V4 payload readback differs")
        if any(not math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.00005) for actual, expected in zip(output.payload_cog_m, cog, strict=True)):
            raise LiveWriterError("runtime V4 CoG readback differs")
        if any(not math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.00005) for actual, expected in zip(output.tcp_offset_m_rad, tcp, strict=True)):
            raise LiveWriterError("runtime V4 TCP readback differs")
        if not output.safety_normal or (require_stationary and not output.stationary):
            raise LiveWriterError("runtime Safety/stationary gate failed")
        hi, lo = runtime_identity_limbs(
            self.contract.raw["program"], self.contract.sha256, self.contract.campaign_fingerprint
        )
        if output.integer_echoes.get(24) != self.prerequisites.session_epoch:
            raise LiveWriterError("runtime epoch echo differs")
        if output.integer_echoes.get(32) != RUNTIME_PROTOCOL or output.integer_echoes.get(33) != hi or output.integer_echoes.get(34) != lo:
            raise LiveWriterError("runtime digest echo differs")
        if output.integer_echoes.get(26) == 90:
            raise LiveWriterError("resident TP is STOPPED")

    def _observe_runtime(self, output: R004OutputSnapshot) -> None:
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
            program_running=True,
            uninterrupted=True,
            observed_at_s=output.observed_at_s,
        )
        if not self.session.observe_runtime(evidence):
            raise LiveWriterError(self.session.identity.invalidated_reason or "runtime identity rejected")
        self._last_identity_observed_s = output.observed_at_s

    def _poll_checked(self, *, require_stationary: bool = False) -> R004OutputSnapshot:
        rtde, _kunwei = self._transport_pair()
        output = rtde.poll_output()
        if output is None:
            if self._wall_clock() - self._last_output_seen_wall_s >= RUNTIME_OUTPUT_MAX_AGE_S:
                raise LiveWriterError("runtime identity output is stale")
            if self._last_output is None:
                raise LiveWriterError("r004 runtime output is missing")
            return self._last_output
        self._validate_output(output, require_stationary=require_stationary)
        self._observe_runtime(output)
        self._last_output = output
        self._last_output_seen_wall_s = self._wall_clock()
        return output

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
            if raw is None:
                raise LiveWriterError("Kunwei stream has no fresh wrench before entry")
            self._send_packet(self._sensor_packet(raw=raw, observed_at_s=observed_at), command_mode=CommandMode.HOLD)
            output = rtde.poll_output()
            if output is None:
                raise LiveWriterError("r004 runtime output is missing at entry")
            self._validate_output(output, require_stationary=True)
            self._last_output = output
            self._last_output_seen_wall_s = self._wall_clock()
            self.session.play(
                controller_receipt=self.prerequisites.controller,
                script1_receipt=self.prerequisites.script1,
                now_s=self.prerequisites.controller.observed_at_s,
                epoch=self.prerequisites.session_epoch,
                session_id=self.prerequisites.resident_session_id,
                expected_triplet=dict(self.prerequisites.expected_triplet),
                home=HomeReference(tuple(output.tcp_pose_m_rad), tuple(output.q_rad)),
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
        return self._sensor_packet(raw=raw, observed_at_s=observed_at)

    def set_baseline_state(self, *, consecutive_successes: int, sticky_one_newton_latched: int) -> None:
        if not 0 <= consecutive_successes <= 3 or sticky_one_newton_latched not in (0, 1):
            raise LiveWriterError("baseline wire state is outside the bounded r004 contract")
        self._baseline_successes = int(consecutive_successes)
        self._sticky_latched = int(sticky_one_newton_latched)

    def arm(
        self,
        *,
        ordinal: int,
        kind: AttemptKind,
        candidate_token: int,
        ledger: DurableCampaignLedger,
        resume: bool = False,
    ) -> LiveWriterTick:
        if not self.active:
            raise LiveWriterError("r004 writer is not active")
        try:
            if not resume and not ledger.next_arm_allowed():
                raise LedgerError("next ARM is forbidden before durable row verification")
            if ledger.path.exists():
                verify_ledger_hash_chain(ledger.path)
            if resume:
                rows = ledger.rows
                if not rows or rows[-1].get("status") != "interrupted" or rows[-1].get("gp_eligible") is True:
                    raise LedgerError("resume ARM requires the verified interrupted row boundary")
            self._session_command_sequence += 1
            self._session_command = SessionCommand.ARM
            self._ordinal = ordinal
            self._kind = kind
            self._candidate_token = candidate_token
            output = self._last_output or self._poll_checked(require_stationary=True)
            request = ArmRequest(
                command=SessionCommand.ARM,
                command_sequence=self._session_command_sequence,
                session_epoch=self.prerequisites.session_epoch,
                logical_attempt_ordinal=ordinal,
                attempt_kind=kind,
                candidate_token=candidate_token,
            )
            self.session.arm(request, ledger_ready=True)
            packet = self._send_packet(self._sensor_packet(raw=(0.0,) * 6, observed_at_s=self._mono_clock()), command_mode=CommandMode.HOLD)
            output = self._poll_checked()
            if any(
                output.integer_echoes.get(register) != expected
                for register, expected in {
                    24: self.prerequisites.session_epoch,
                    25: ordinal,
                    27: candidate_token,
                    29: self._session_command_sequence,
                    30: int(kind),
                }.items()
            ):
                raise LiveWriterError("ARM output echo differs")
            self._entry_linear_speed_m_s = math.sqrt(sum(value * value for value in output.tcp_speed_m_s_rad_s[:3]))
            self._entry_angular_speed_rad_s = math.sqrt(sum(value * value for value in output.tcp_speed_m_s_rad_s[3:]))
            return LiveWriterTick(self._packet_sequence - 1, self._session_command_sequence, output, packet)
        except Exception as exc:
            self._fail_closed(str(exc))
            raise LiveWriterError(str(exc)) from exc

    def execute_attempt(self, attempt: Attempt, *, timeout_s: float = 180.0) -> AttemptEvidence:
        if self.session.phase is not SessionPhase.RUNNING or self._ordinal != attempt.ordinal:
            raise LiveWriterError("attempt execution identity is not armed")
        collector = PathEvidenceCollector()
        start = self._mono_clock()
        seen_states: set[int] = set()
        terminal: R004OutputSnapshot | None = None
        try:
            while True:
                if self._mono_clock() - start >= timeout_s:
                    raise LiveWriterError("r004 attempt execution timeout")
                output = self._poll_checked()
                seen_states.add(output.integer_echoes[26])
                sensor = self._read_sensor()
                mode = CommandMode.PATH if output.integer_echoes[26] == 25 else CommandMode.HOLD
                self._send_packet(sensor, command_mode=mode)
                collector.observe(
                    PathSample(
                        observed_at_s=self._mono_clock(),
                        filtered_normal_n=sensor.filtered_normal_n,
                        force_norm_n=sensor.force_norm_n,
                        torque_norm_nm=sensor.torque_norm_nm,
                        sensor_fresh=sensor.sensor_fresh,
                        state=output.integer_echoes[26],
                        safety_normal=output.safety_normal,
                    )
                )
                if output.integer_echoes[26] in {78, 80, 90}:
                    terminal = output
                    break
                self._sleep(0.008)
            if terminal is None or terminal.integer_echoes[26] == 90 or self._home is None:
                raise LiveWriterError("r004 attempt terminated without a valid return")
            pose_error = math.dist(terminal.tcp_pose_m_rad[:3], self._home.pose[:3])
            orientation_error = math.dist(terminal.tcp_pose_m_rad[3:], self._home.pose[3:])
            q_error = max(abs(actual - expected) for actual, expected in zip(terminal.q_rad, self._home.q or (0.0,) * 6, strict=True))
            guard = terminal.integer_echoes[31]
            return_evidence = ReturnEvidence(
                stationary=terminal.stationary,
                retract_z_m=0.005 if guard & 2 else 0.0,
                transfer_floor_z_m=0.062863519 if guard & 4 else 0.0,
                entry_linear_speed_m_s=self._entry_linear_speed_m_s,
                return_linear_speed_m_s=math.sqrt(sum(value * value for value in terminal.tcp_speed_m_s_rad_s[:3])),
                entry_angular_speed_rad_s=self._entry_angular_speed_rad_s,
                return_angular_speed_rad_s=math.sqrt(sum(value * value for value in terminal.tcp_speed_m_s_rad_s[3:])),
                descended_to_captured_home=bool(guard & 32),
                home_pose_error_m=pose_error,
                home_orientation_error_rad=orientation_error,
                home_q_error_rad=q_error,
                safety_gate_passed=terminal.safety_normal and collector.safety_gate_passed,
                contact_gate_passed={20, 21, 25}.issubset(seen_states),
            )
            decision = evaluate_return(return_evidence)
            self.session.finish_attempt(decision)
            evidence = collector.finalize(
                return_gate_passed=decision.passed,
                contact_gate_passed=return_evidence.contact_gate_passed,
                home_proof={
                    "stationary": terminal.stationary,
                    "retract_z_m": return_evidence.retract_z_m,
                    "transfer_floor_z_m": return_evidence.transfer_floor_z_m,
                    "home_pose_error_m": pose_error,
                    "home_orientation_error_rad": orientation_error,
                    "home_q_error_rad": q_error,
                    "return_guard": guard,
                },
            )
            if not decision.passed:
                self._stopped = True
                raise LiveWriterError(decision.reason)
            return evidence
        except Exception as exc:
            self._fail_closed(str(exc))
            raise LiveWriterError(str(exc)) from exc

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
