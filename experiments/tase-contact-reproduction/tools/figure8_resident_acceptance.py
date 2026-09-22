"""Offline resident Figure-eight infrastructure acceptance.

Only transports and file read-back are synthetic.  The public ``run_live``
entry still constructs and executes the mature provider/writer path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from contact_yield_live import _parse_args, run_live
from contact_yield_live_contract import (
    CONTACT_PROGRAM,
    HOME_PROGRAM,
    load_identity_contract,
    software_identity_limbs,
)
from contact_yield_live_writer import load_run_dir_receipts
from contact_yield_method_registry import resolve_method
from contact_yield_live_contract import RUNTIME_PROTOCOL
from step5d_autotune_v4_r004.fake_rtde import FakeLiveKunweiTransport, FakeLiveRTDETransport
from step5d_autotune_v4_r004.contracts import runtime_identity_limbs
from tase_contact_provider import TASE_PARAMETER_SCHEMA


INCUMBENT_MD = 9.565272137974492
INCUMBENT_BD = 693.6559295653944


class OfflineClock:
    def __init__(self, wall_s: float = 100.0) -> None:
        self.wall_s = float(wall_s)
        self.mono_s = 0.0

    def wall(self) -> float:
        return self.wall_s

    def mono(self) -> float:
        return self.mono_s

    def sleep(self, duration: float) -> None:
        step = max(0.0, float(duration))
        self.mono_s += step
        self.wall_s += step


class OfflineResidentRTDE(FakeLiveRTDETransport):
    def __init__(self, contract, *, home_pose, home_q, clock: OfflineClock):
        super().__init__(contract)
        self.home_pose = tuple(float(value) for value in home_pose)
        self.home_q = tuple(float(value) for value in home_q)
        self.clock = clock
        self.dashboard_stopped = False
        self._early_end_sequence = None
        self.command_trace: list[dict[str, Any]] = []

    def write_input_integer_register(self, register: int, value: int) -> None:
        if register == 35:
            self._early_end_sequence = int(value) if int(value) > 0 else None

    def read_output_integer_register(self, register: int) -> int:
        if register == 36:
            return int(self._early_end_sequence or 0)
        if register == 35:
            return 0
        if register == 28:
            return int(self._reason)
        raise ValueError(register)

    def _mapping(self) -> dict[str, Any]:
        payload = super()._mapping()
        payload["actual_TCP_pose"] = list(self.home_pose)
        payload["actual_q"] = list(self.home_q)
        payload["runtime_state"] = 1 if self.dashboard_stopped else "PLAYING"
        payload["safety_mode"] = "NORMAL"
        payload["timestamp"] = self.clock.mono()
        payload["output_int_register_32"] = RUNTIME_PROTOCOL
        payload["output_int_register_33"] = 25
        payload["output_int_register_34"] = 618001
        if self._state == 25:
            payload["actual_qd"] = list(self._input_doubles[13:19])
            import numpy as np
            from tase_figure8_protocol import Figure8Window60Task
            from contact_yield_task_frame import figure8_task_basis
            if getattr(self, "path_origin", None) is None:
                self.path_origin = self.clock.mono()
            elapsed = self.clock.mono() - self.path_origin
            task = Figure8Window60Task()
            ref = (task.entry_reference(elapsed) if elapsed < 1.0
                   else task.reference(min(elapsed - 1.0, 60.0)))
            basis = figure8_task_basis()
            payload["actual_TCP_pose"][:3] = (
                np.asarray(self.home_pose[:3]) + basis @ np.asarray(ref["position_m"])
            ).tolist()
            payload["actual_TCP_speed"] = [*(basis @ np.asarray(ref["velocity_m_s"])), 0., 0., 0.]
        else:
            self.path_origin = None
        return payload

    def send_packet(self, double_values: Sequence[float], integer_values: Sequence[int]) -> None:
        previous = self._state
        super().send_packet(double_values, integer_values)
        command_mode = int(self._input_integers[1])
        session_command = int(self._input_integers[3])
        now = self.clock.mono()
        if previous != 20 and self._state == 20:
            self._contact_search_start = now
        if previous == 20 and self._state == 21:
            if now - getattr(self, "_contact_search_start", now) < .080:
                self._state = 20
        if command_mode == 1:
            self._state = 21
        if command_mode == 2:
            self._state = 25
        if command_mode == 3:
            self._state = 78
            self._return_guard = 127
        if self._state == 25 and self._early_end_sequence is not None:
            self._state = 78
            self._return_guard = 127
        if session_command == 3:
            self._state = 90
            self._reason = 4
        self.command_trace.append(
            {
                "previous_state": previous,
                "state": self._state,
                "command_mode": command_mode,
                "session_command": session_command,
                "session_sequence": int(self._input_integers[4]),
                "packet_sequence": int(round(self._input_doubles[22])),
            }
        )

    def dashboard_stop(self) -> None:
        self.dashboard_stopped = True

    def poll_output(self, *, wait_s: float = 0.0):
        snapshot = super().poll_output(wait_s=wait_s)
        if snapshot is not None:
            object.__setattr__(snapshot, "received_monotonic_s", self.clock.mono())
            object.__setattr__(snapshot, "observed_at_s", self.clock.wall())
            object.__setattr__(snapshot, "timestamp", self.clock.mono())
        return snapshot


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _baseline(path: Path, observed_at_s: float, eoat_sha256: str, *, index: int = 0) -> None:
    capture_name = f"baseline-capture-{index:04d}.json"
    capture = path / capture_name
    rows = [{"sample": row, "wrench_n_nm": [0.0] * 6} for row in range(100)]
    capture.write_text(json.dumps(rows) + "\n", encoding="utf-8")
    _write_json(
        path / "software_baseline_receipt.json",
        {
            "mean_wrench_n_nm": [0.0] * 6,
            "std_wrench_n_nm": [0.01] * 6,
            "observed_at_s": observed_at_s,
            "stationary": True,
            "no_contact": True,
            "eoat_identity_sha256": eoat_sha256,
            "capture_file": capture_name,
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "steady_samples": 100,
            "acquisition_duration_s": 10.0,
            "initial_exclusion_s": 0.5,
            "variance_n2": [0.0001] * 6,
            "context": "offline injected Home baseline; no hardware claim",
        },
    )


def _write_receipts(run_dir: Path, contract, observed_at_s: float, *, index: int = 0) -> None:
    hi, lo = software_identity_limbs(contract)
    profile = __import__("step5d_eoat_profiles", fromlist=["load_new_eoat_profile"]).load_new_eoat_profile()
    triplet = dict(contract.triplet)
    controller = {
        "receipt_sha256": hashlib.sha256(f"controller-{index}-{observed_at_s}".encode()).hexdigest(),
        "program": CONTACT_PROGRAM,
        "controller_target": contract.raw["script2"]["controller_target"],
        "script_sha256": triplet["script"], "txt_sha256": triplet["txt"], "urp_sha256": triplet["urp"],
        "observed_at_s": observed_at_s,
        "runtime_protocol": RUNTIME_PROTOCOL,
        "runtime_digest_hi": hi, "runtime_digest_lo": lo,
        "eoat_identity_sha256": profile.profile_sha256,
        "payload_kg": profile.payload_kg, "payload_cog_m": list(profile.cog_m),
        "tcp_offset_m_rad": list(profile.controller_tcp_m_rad),
        "actual_tcp_speed_m_s_rad_s": [0.0] * 6,
        "safety_mode": "NORMAL", "stationary": True, "route_id": "r006-yield-live",
    }
    runtime = {
        "program": CONTACT_PROGRAM, "script_sha256": triplet["script"],
        "runtime_protocol": RUNTIME_PROTOCOL, "runtime_digest_hi": hi, "runtime_digest_lo": lo,
        "session_epoch": 1, "resident_session_id": "offline-resident-session",
        "program_running": True, "uninterrupted": True, "observed_at_s": observed_at_s,
    }
    home = {
        "schema": "yield-live-entry/home-start-receipt-v1",
        "script_sha256": contract.script1_sha256["script"], "observed_at_s": observed_at_s,
        "final_pose": list(contract.home_pose), "final_q": list(contract.home_q),
        "stationary": True, "safety_mode": "NORMAL", "eoat_identity_sha256": profile.profile_sha256,
        "home_profile_id": "yield-live-entry/contact-home-v1",
    }
    home["receipt_sha256"] = hashlib.sha256(json.dumps(home, sort_keys=True).encode()).hexdigest()
    _write_json(run_dir / "controller_receipt.json", controller)
    _write_json(run_dir / "runtime_evidence.json", runtime)
    _write_json(run_dir / "home_start_receipt.json", home)
    _baseline(run_dir, observed_at_s, profile.profile_sha256, index=index)


def _parameter_file(path: Path) -> Path:
    payload = {
        "schema": TASE_PARAMETER_SCHEMA,
        "candidate_id": "offline-resident-incumbent",
        "stage": "infrastructure_acceptance",
        "index": 0,
        "Md_scalar": INCUMBENT_MD,
        "Bd_scalar": INCUMBENT_BD,
        "protocol_id": "figure8_window60_r013_compat_v1",
        "duration_token": "r013_60",
        "path_duration_s": 60.0,
        "frozen": {
            "kp": 4.0, "ko": 5.0, "kf": 1.0, "force_target_n": 5.0,
            "force_sign_convention": "step5_step6_positive_normal_load",
            "force_integral_limit_n_s": 0.1,
            "force_integral_policy": "legacy-clamp-v1",
            "force_integral_authority_error_n": 0.5,
        },
    }
    _write_json(path, payload)
    return path


def run(*, run_dir: Path, attempts: int = 2,
        vary_parameters: bool = False) -> dict[str, Any]:
    if attempts < 2:
        raise ValueError("resident acceptance requires at least two attempts")
    run_dir = Path(run_dir).expanduser().resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    contract = load_identity_contract()
    resolve_method("TASE_RNN_MATURE")
    clock = OfflineClock()
    _write_receipts(run_dir, contract, clock.wall())
    parameter_file = _parameter_file(run_dir / "incumbent.json")
    controller = OfflineResidentRTDE(contract, home_pose=contract.home_pose, home_q=contract.home_q, clock=clock)
    sensor = FakeLiveKunweiTransport(observed_clock=clock.mono,
                                   wrench_n_nm=(0., 0., -5., 0., 0., 0.))

    def dashboard_stop_and_verify(*, reason, protocol_stop, home_proof):
        del reason, protocol_stop, home_proof
        controller.dashboard_stop()
        clock.sleep(.002)
        output = controller.poll_output(wait_s=0.004)
        if output.runtime_state != 1 or not output.stationary:
            raise RuntimeError("offline Dashboard STOPPED did not produce fresh stopped RTDE")
        return {
            "program_stopped": True,
            "stopped": True,
            "dashboard": {
                "running": "Program running: false",
                "programState": "STOPPED step5d_contact_six_qp_v1.urp",
            },
            "sample": {
                "runtime_state": output.runtime_state,
                "timestamp": output.timestamp,
                "home_verified": True,
            },
        }

    def refresh_readback(*, session, home, now_s):
        _write_receipts(run_dir, contract, now_s, index=len(session.refreshes) + 1)
        prerequisites, _ = load_run_dir_receipts(
            run_dir,
            contract=contract,
            route_id="r006-yield-live",
            attempt_id="offline-refresh",
            now_s=now_s,
        )
        return {
            "prerequisites": prerequisites,
            "baseline": {
                "acquisition_duration_s": 10.0, "initial_exclusion_s": 0.5,
                "sample_count": 100, "variance_n2": [0.0001] * 6,
                "stationary": True, "no_contact": True,
                "eoat_identity_sha256": contract.eoat_sha256,
            },
            "readback_validation": {"fresh_files": True, "triplet_validated": True},
        }

    from tase_contact_provider import load_tase_outer_config

    parameter_files = [parameter_file] * attempts
    if vary_parameters:
        varied = json.loads(parameter_file.read_text(encoding='utf-8'))
        varied['candidate_id'] = 'offline-resident-second'
        varied['Md_scalar'] = 10.0
        varied['Bd_scalar'] = 700.0
        varied['frozen']['force_integral_limit_n_s'] = 0.5
        second_path = run_dir / 'second.json'
        _write_json(second_path, varied)
        parameter_files[1] = second_path
    bindings = [load_tase_outer_config(path)[1] for path in parameter_files]
    args = _parse_args([
        "pilot", "--method", "TASE_RNN_MATURE", "--duration", "r013_60",
        "--run-dir", str(run_dir), "--qp-library", str(Path(__file__).resolve().parents[1] / "build/contact-qp/libcontact_qp.so"),
        "--route-id", "r006-yield-live", "--attempt-id", "r006-offline-resident-pilot",
        "--authority-root", str(run_dir / "authority"), "--control-cpu", "4",
        "--parameter-file", str(parameter_file),
    ])
    return run_live(
        args,
        controller_transport=controller,
        kunwei_transport=sensor,
        wall_clock=clock.wall,
        mono_clock=clock.mono,
        sleep=clock.sleep,
        now_s=clock.wall(),
        attempt_count=attempts,
        parameter_bindings=bindings,
        parameter_files=parameter_files,
        refresh_readback=refresh_readback,
        dashboard_stop_and_verify=dashboard_stop_and_verify,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--attempts", type=int, default=6)
    parser.add_argument("--vary-parameters", action="store_true")
    args = parser.parse_args(argv)
    receipt = run(run_dir=args.run_dir, attempts=args.attempts,
                  vary_parameters=args.vary_parameters)
    print(json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False))
    return 0 if receipt.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
