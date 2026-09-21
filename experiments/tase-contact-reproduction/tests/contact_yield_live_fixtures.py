"""Synthetic endpoints and receipts, exclusively for offline tests."""
from pathlib import Path
from typing import Any, Sequence, Mapping, Callable
import hashlib, json, time
from contact_yield_live_contract import *
from step5d_eoat_profiles import load_new_eoat_profile
from step5d_autotune_v4_r004.fake_rtde import FakeLiveRTDETransport
from step5d_autotune_v4_r004.wire import CommandMode, SessionCommand

class YieldLiveRTDEDouble(FakeLiveRTDETransport):
    """Endpoint double for the contact-six wire. Never opens a socket."""

    def __init__(
        self,
        contract: YieldLiveIdentityContract,
        *,
        home_pose: Sequence[float],
        home_q: Sequence[float],
        events: list[str] | None = None,
        flip_rotvec: bool = False,
        stale_receive: bool = False,
        wrong_protocol: bool = False,
        output_queue: Sequence[Mapping[str, Any]] = (),
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(contract, output_queue=output_queue, events=events)
        self.clock = clock
        pose = list(home_pose)
        if flip_rotvec:
            pose[3] = -float(pose[3])
            pose[4] = -float(pose[4])
            pose[5] = -float(pose[5])
        self.home_pose = tuple(float(item) for item in pose)
        self.home_q = tuple(float(item) for item in home_q)
        self.stale_receive = stale_receive
        self.wrong_protocol = wrong_protocol
        self._early_end_sequence: int | None = None
        self._state = 78
        self._polls = 0
        self._search_ticks = 0
        self.command_trace: list[tuple[int, int, int]] = []
        self.polled_states: list[int] = []

    def write_input_integer_register(self, register: int, value: int) -> None:
        if register == 35:
            self._early_end_sequence = int(value) if int(value) > 0 else None

    def read_output_integer_register(self, register):
        if register == 36:
            return self._early_end_sequence or 0
        if register == 35:
            return 0
        if register == 28:
            return self._reason
        raise ValueError(register)

    def _mapping(self) -> dict[str, Any]:
        payload = super()._mapping()
        payload["actual_TCP_pose"] = list(self.home_pose)
        payload["actual_q"] = list(self.home_q)
        if self.wrong_protocol:
            payload["output_int_register_32"] = 606004
            payload["output_int_register_33"] = 13
            payload["output_int_register_34"] = 613013
        else:
            payload["output_int_register_32"] = RUNTIME_PROTOCOL
            payload["output_int_register_33"] = READABLE_RUNTIME_IDENTITY[0]
            payload["output_int_register_34"] = READABLE_RUNTIME_IDENTITY[1]
        payload["runtime_state"] = "PLAYING"
        payload["safety_mode"] = "NORMAL"
        now = float(self.clock()) if self.clock is not None else time.time()
        payload["timestamp"] = now - 1.0 if self.stale_receive else now
        return payload

    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None:
        previous_state = self._state
        super().send_packet(double_values, integer_values)
        now = float(self.clock()) if self.clock is not None else time.monotonic()
        if previous_state != 20 and self._state == 20:
            self._contact_search_start = now
        if previous_state == 20 and self._state == 21:
            # The resident TP requires 80 ms contact confirmation before
            # publishing baseline state 21; a single force sample is not enough.
            if now - getattr(self, "_contact_search_start", now) < .080:
                self._state = 20
        command_mode = self._input_integers[1]
        session_command = self._input_integers[3]
        if len(self.command_trace) < 24:
            self.command_trace.append((self._state, session_command, command_mode))
        if command_mode == int(CommandMode.BASELINE):
            self._state = 21
        if command_mode == int(CommandMode.PATH):
            if self._input_integers[0] < 1:
                self._state, self._reason = 90, 50
            else:
                self._state = 25
        if command_mode == int(CommandMode.RETRACT):
            self._state = 78
            self._return_guard = 127
        if self._state == 25 and self._early_end_sequence is not None:
            self._state = 78
            self._return_guard = 127
        if session_command == int(SessionCommand.STOP):
            self._state = 90
            self._reason = 4

    def poll_output(self, *, wait_s: float = 0.0) -> Any:
        snapshot = super().poll_output(wait_s=wait_s)
        self._polls += 1
        self.polled_states.append(self._state)
        now = float(self.clock()) if self.clock is not None else time.monotonic()
        frame_timestamp = 1000.0 + now
        observed_at = 200.0 + now
        if snapshot is not None and self.stale_receive:
            object.__setattr__(snapshot, "received_monotonic_s", max(0.0, now - 0.1))
            object.__setattr__(snapshot, "observed_at_s", observed_at)
            object.__setattr__(snapshot, "timestamp", frame_timestamp)
        elif snapshot is not None:
            object.__setattr__(snapshot, "received_monotonic_s", now)
            object.__setattr__(snapshot, "observed_at_s", observed_at)
            object.__setattr__(snapshot, "timestamp", frame_timestamp)
        return snapshot


def write_admission_receipts(
    run_dir: Path,
    *,
    contract: YieldLiveIdentityContract,
    route_id: str,
    session_epoch: int,
    resident_session_id: str,
    home_q: Sequence[float],
    observed_controller: float,
    observed_runtime: float,
    observed_home: float,
    program_running: bool = True,
) -> None:
    """Write deliberately synthetic test receipts; never hardware evidence."""

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    profile = load_new_eoat_profile()
    hi, lo = software_identity_limbs(contract)
    triplet = dict(contract.triplet)
    controller = {
        "receipt_sha256": "b" * 64,
        "program": CONTACT_PROGRAM,
        "controller_target": contract.raw["script2"]["controller_target"],
        "script_sha256": triplet["script"],
        "txt_sha256": triplet["txt"],
        "urp_sha256": triplet["urp"],
        "observed_at_s": observed_controller,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_digest_hi": hi,
        "runtime_digest_lo": lo,
        "eoat_identity_sha256": profile.profile_sha256,
        "payload_kg": profile.payload_kg,
        "payload_cog_m": list(profile.cog_m),
        "tcp_offset_m_rad": list(profile.controller_tcp_m_rad),
        "safety_mode": "NORMAL",
        "stationary": True,
        "route_id": route_id,
        "readback": {
            "payload_kg": profile.payload_kg,
            "payload_cog_m": list(profile.cog_m),
            "tcp_offset_m_rad": list(profile.controller_tcp_m_rad),
            "actual_TCP_speed": [0.0] * 6,
        },
        "controller": {"stationary": True, "safety_mode": "NORMAL"},
    }
    runtime = {
        "program": CONTACT_PROGRAM,
        "script_sha256": triplet["script"],
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_digest_hi": hi,
        "runtime_digest_lo": lo,
        "session_epoch": session_epoch,
        "resident_session_id": resident_session_id,
        "program_running": program_running,
        "uninterrupted": True,
        "observed_at_s": observed_runtime,
    }
    home_body = {
        "schema": "yield-live-entry/home-start-receipt-v1",
        "script_sha256": contract.script1_sha256["script"],
        "observed_at_s": observed_home,
        "final_pose": list(contract.home_pose),
        "final_q": [float(item) for item in home_q],
        "stationary": True,
        "safety_mode": "NORMAL",
        "eoat_identity_sha256": profile.profile_sha256,
        "home_profile_id": "yield-live-entry/contact-home-v1",
    }
    home_body["receipt_sha256"] = hashlib.sha256(
        json.dumps(home_body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (run_dir / "controller_receipt.json").write_text(json.dumps(controller, indent=2) + "\n")
    (run_dir / "runtime_evidence.json").write_text(json.dumps(runtime, indent=2) + "\n")
    (run_dir / "home_start_receipt.json").write_text(json.dumps(home_body, indent=2) + "\n")


def write_test_baseline(run_dir, contract, observed_at_s):
    raw = b'{"synthetic_test_only": true}\n'
    (run_dir / "baseline_capture.json").write_bytes(raw)
    (run_dir / "software_baseline_receipt.json").write_text(json.dumps({
        "mean_wrench_n_nm": [0.] * 6,
        "observed_at_s": observed_at_s,
        "stationary": True, "no_contact": True,
        "eoat_identity_sha256": contract.eoat_sha256,
        "capture_file": "baseline_capture.json",
        "capture_sha256": hashlib.sha256(raw).hexdigest(),
    }))
