"""Strict-RNN-only offline executor for Remote replay preparation."""

from __future__ import annotations

import csv
import json
from dataclasses import MISSING, fields
from pathlib import Path
from typing import Any, Mapping, TextIO

from step5c_strict_rnn import StrictRnnConfig, StrictTaseRnnSolver
from step5d_control_contract import (
    SafetyEnvelope,
    Step5dObservation,
    StrictRnnControlPolicy,
    Vector6,
)

from .contracts import RemoteControlError, RemoteRelease, _unique_object
from .seam import RemoteDeferredDiagnostics, remote_rnn_control_step


_VECTOR6_FIELDS = {
    "q",
    "qd",
    "tcp_pose",
    "tcp_twist",
    "wrench",
    "desired_twist",
    "omega_minus",
    "omega_plus",
    "raw_desired_twist",
    "reference_prior_qdot",
}
_VECTOR3_FIELDS = {"reaction_normal", "approach_normal"}


def observation_from_mapping(payload: Mapping[str, Any]) -> Step5dObservation:
    known = {field.name for field in fields(Step5dObservation)}
    required = {
        field.name
        for field in fields(Step5dObservation)
        if field.default is MISSING and field.default_factory is MISSING
    }
    if not isinstance(payload, Mapping):
        raise RemoteControlError("Remote replay observation must be an object")
    if not required.issubset(payload) or not set(payload).issubset(known):
        raise RemoteControlError(
            "Remote replay observation fields differ; "
            f"missing={sorted(required - set(payload))}, extra={sorted(set(payload) - known)}"
        )
    values = dict(payload)
    for name in _VECTOR6_FIELDS & set(values):
        if values[name] is not None:
            values[name] = tuple(values[name])
    for name in _VECTOR3_FIELDS & set(values):
        values[name] = tuple(values[name])
    if "jacobian" in values:
        values["jacobian"] = tuple(tuple(row) for row in values["jacobian"])
    if values.get("normal_to_command_rotation") is not None:
        values["normal_to_command_rotation"] = tuple(
            tuple(row) for row in values["normal_to_command_rotation"]
        )
    try:
        return Step5dObservation(**values)
    except (TypeError, ValueError) as exc:
        raise RemoteControlError(f"Remote replay observation is invalid: {exc}") from exc


class RnnOnlyExecutor:
    """Own one stateful strict-RNN policy and its authoritative safety seam."""

    def __init__(
        self,
        policy: StrictRnnControlPolicy,
        *,
        safety_envelope: SafetyEnvelope,
        max_slew_rad_s2: float,
        capacity: int,
    ) -> None:
        if not isinstance(policy, StrictRnnControlPolicy):
            raise TypeError("Remote executor requires StrictRnnControlPolicy")
        self.policy = policy
        self.safety_envelope = safety_envelope
        self.max_slew_rad_s2 = float(max_slew_rad_s2)
        self.diagnostics = RemoteDeferredDiagnostics(capacity=capacity)
        self.previous_qdot: Vector6 | None = None

    def reset(self) -> None:
        self.policy.solver.reset_state()
        self.diagnostics.reset_for_trial()
        self.previous_qdot = None

    def step(self, observation: Step5dObservation):
        result = remote_rnn_control_step(
            observation,
            self.policy,
            previous_qdot=self.previous_qdot,
            safety_envelope=self.safety_envelope,
            diagnostics=self.diagnostics,
            max_slew_rad_s2=self.max_slew_rad_s2,
        )
        if result.decision.accepted:
            self.previous_qdot = result.candidate.qdot
        return result


def build_executor(release: RemoteRelease, *, capacity: int) -> RnnOnlyExecutor:
    parameters = release.control_parameters
    try:
        solver = StrictTaseRnnSolver(
            StrictRnnConfig(
                paper_truth_path=(
                    release.experiment_root
                    / release.document["inputs"]["solver_gate"]
                ),
                qdot_limit_rad_s=float(parameters["qdot_limit_rad_s"]),
                epsilon=float(parameters["rnn_epsilon"]),
                sigr_exponent_r=float(parameters["rnn_sigr_exponent_r"]),
                inner_iterations=int(parameters["rnn_inner_iterations"]),
                backend=str(parameters["rnn_backend"]),
            )
        )
    except (ImportError, RuntimeError, ValueError) as exc:
        raise RemoteControlError(
            "inherited V3 RNN runtime is unavailable; backend fallback is forbidden: "
            f"{exc}"
        ) from exc
    solver.reset_state()
    return RnnOnlyExecutor(
        StrictRnnControlPolicy(solver),
        safety_envelope=SafetyEnvelope(
            qdot_cap_rad_s=float(parameters["qdot_limit_rad_s"])
        ),
        max_slew_rad_s2=float(parameters["max_acceleration_rad_s2"]),
        capacity=capacity,
    )


def _load_json_line(line: str, *, number: int) -> Mapping[str, Any]:
    try:
        payload = json.loads(
            line,
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RemoteControlError(
                    f"non-finite constant in replay line {number}: {value}"
                )
            ),
        )
    except (json.JSONDecodeError, RemoteControlError) as exc:
        raise RemoteControlError(f"invalid replay JSON line {number}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RemoteControlError(f"replay line {number} must be a JSON object")
    return payload


def replay_jsonl(
    observations_path: Path,
    *,
    release: RemoteRelease,
    diagnostics_csv: Path,
) -> dict[str, Any]:
    if observations_path.is_symlink() or not observations_path.is_file():
        raise RemoteControlError("Remote replay input must be a real regular file")
    lines = [
        (number, line)
        for number, line in enumerate(
            observations_path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if line.strip()
    ]
    if not lines:
        raise RemoteControlError("Remote replay input is empty")
    executor = build_executor(release, capacity=len(lines))
    accepted = 0
    stopped = 0
    for number, line in lines:
        result = executor.step(
            observation_from_mapping(_load_json_line(line, number=number))
        )
        accepted += int(result.decision.accepted)
        stopped += int(result.command.stop_requested)
    diagnostics_csv.parent.mkdir(parents=True, exist_ok=True)
    with diagnostics_csv.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow((*executor.diagnostics.field_names, "reason", "action"))
        for index in range(executor.diagnostics.count):
            writer.writerow(
                (
                    *executor.diagnostics.numeric[index].tolist(),
                    executor.diagnostics.reasons[index],
                    executor.diagnostics.actions[index],
                )
            )
    return {
        "schema": "step5d.remote-control/replay-summary-v1",
        "ok": stopped == 0 and accepted == len(lines),
        "transport_id": release.transport_id,
        "release_sha256": release.release_sha256,
        "observations": len(lines),
        "accepted": accepted,
        "stop_requests": stopped,
        "diagnostics_rows": executor.diagnostics.count,
        "diagnostics_csv": str(diagnostics_csv.resolve()),
    }


def reject_live_run(release: RemoteRelease, *, output: TextIO) -> int:
    """Fail before any ROS or driver surface can be imported or contacted."""

    payload = {
        "ok": False,
        "state": "offline_ready",
        "live_certified": False,
        "blocker": "remote_release_not_live_authorized",
        "release_sha256": release.release_sha256,
        "transport_id": release.transport_id,
    }
    output.write(json.dumps(payload, sort_keys=True) + "\n")
    return 3
