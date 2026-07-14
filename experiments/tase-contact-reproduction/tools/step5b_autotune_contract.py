#!/usr/bin/env python3
"""Shared, no-hardware contract for the isolated Step5b autotune loop."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = EXPERIMENT_ROOT / "config" / "step5b_autotune_loop_v1.json"
PROGRAM_BASENAME = "step5b_contact_cycloid_bayes_loop_v1"
CONTROLLER_DIRECTORY = "/programs/andyl/kunwei/step5/autotune"
CONTROLLER_PROGRAM = f"{CONTROLLER_DIRECTORY}/{PROGRAM_BASENAME}.urp"
BRIDGE_PROFILE = "step5b_v2"
CONFIRMATION_TOKEN = "LIVE STEP5B AUTOTUNE SESSION"

TARGET_CONTEXTS_N = (10.0, 12.0, 15.0)

COMMAND_HOLD = 0
COMMAND_ARM = 1
COMMAND_STOP = 2
COMMAND_ABORT_RETURN = 3

TP_BOOT = 0
TP_WAIT_ARM = 10
TP_RUN = 20
TP_RETRACT = 30
TP_RETURN = 40
TP_HOME = 50
TP_FAULT = 90

TP_STATE_NAMES = {
    TP_BOOT: "BOOT",
    TP_WAIT_ARM: "WAIT_ARM",
    TP_RUN: "RUN",
    TP_RETRACT: "RETRACT",
    TP_RETURN: "RETURN",
    TP_HOME: "HOME",
    TP_FAULT: "FAULT",
}

RECOVERABLE_AUTO_HOME_REASONS = frozenset({1, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14})
CONSTRAINT_VIOLATION_REASONS = frozenset({5, 6, 7, 8, 10, 12, 13})
FATAL_SESSION_REASONS = frozenset({2, 3, 14, 15})


@dataclass(frozen=True)
class Candidate:
    target_force_n: float
    force_p_gain: float = 0.001
    force_i_gain: float = 0.00001
    force_damping: float = 7.0
    normal_filter_alpha: float = 0.55

    def validate(self, *, tier2_unlocked: bool = False) -> None:
        if self.target_force_n not in TARGET_CONTEXTS_N:
            raise ValueError(f"unsupported target context: {self.target_force_n}")
        assert_grid("force_p_gain", self.force_p_gain, 0.001, 0.0015, 0.0001)
        assert_grid("force_damping", self.force_damping, 5.0, 8.5, 0.5)
        assert_grid("normal_filter_alpha", self.normal_filter_alpha, 0.55, 0.70, 0.05)
        if tier2_unlocked:
            assert_grid("force_i_gain", self.force_i_gain, 0.00001, 0.00002, 0.000002)
        elif not math.isclose(self.force_i_gain, 0.00001, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("force_i_gain is locked to 1e-5 until Tier 2 unlocks")

    def payload(self) -> dict[str, float]:
        return asdict(self)


def load_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def assert_grid(name: str, value: float, lo: float, hi: float, step: float) -> None:
    value = float(value)
    if value < lo - 1e-12 or value > hi + 1e-12:
        raise ValueError(f"{name}={value} is outside [{lo}, {hi}]")
    index = round((value - lo) / step)
    snapped = lo + index * step
    if not math.isclose(value, snapped, rel_tol=0.0, abs_tol=max(1e-12, step * 1e-8)):
        raise ValueError(f"{name}={value} is not on the {step} grid")


def grid_values(lo: float, hi: float, step: float) -> tuple[float, ...]:
    count = int(round((hi - lo) / step))
    return tuple(lo + index * step for index in range(count + 1))


def candidate_token_low31(session_epoch: int, trial_id: int, candidate: Candidate) -> int:
    payload = {
        "session_epoch": int(session_epoch),
        "trial_id": int(trial_id),
        "candidate": candidate.payload(),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).digest()
    token = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return token or 1


def normalized_candidate(candidate: Candidate) -> tuple[float, ...]:
    return (
        (candidate.target_force_n - 10.0) / 5.0,
        (candidate.force_p_gain - 0.001) / 0.0005,
        (candidate.force_i_gain - 0.00001) / 0.00001,
        (candidate.force_damping - 5.0) / 3.5,
        (candidate.normal_filter_alpha - 0.55) / 0.15,
    )


def one_step_neighbors(candidate: Candidate, *, tier2_unlocked: bool) -> list[Candidate]:
    candidate.validate(tier2_unlocked=tier2_unlocked)
    dimensions: list[tuple[str, Iterable[float]]] = [
        ("force_p_gain", grid_values(0.001, 0.0015, 0.0001)),
        ("force_damping", grid_values(5.0, 8.5, 0.5)),
        ("normal_filter_alpha", grid_values(0.55, 0.70, 0.05)),
    ]
    if tier2_unlocked:
        dimensions.append(("force_i_gain", grid_values(0.00001, 0.00002, 0.000002)))
    result = {candidate}
    for name, values in dimensions:
        current = float(getattr(candidate, name))
        values_tuple = tuple(values)
        index = min(range(len(values_tuple)), key=lambda idx: abs(values_tuple[idx] - current))
        for neighbor_index in (index - 1, index + 1):
            if 0 <= neighbor_index < len(values_tuple):
                payload = candidate.payload()
                payload[name] = values_tuple[neighbor_index]
                result.add(Candidate(**payload))
    return sorted(
        result,
        key=lambda item: (
            item.force_p_gain,
            item.force_i_gain,
            item.force_damping,
            item.normal_filter_alpha,
        ),
    )


def bridge_command(candidate: Candidate, output_dir: Path, *, python_executable: str) -> list[str]:
    candidate.validate(tier2_unlocked=True)
    bridge = EXPERIMENT_ROOT / "tools" / "kunwei_rtde_bridge.py"
    return [
        python_executable,
        str(bridge),
        "--allow-kunwei-stream-command",
        "--write-rtde-inputs",
        "--baseline-s", "5",
        "--rezero-s", "1",
        "--duration-s", "180",
        "--rtde-hz", "500",
        "--socket-timeout-s", "0.0",
        "--sensor-stale-s", "0.10",
        "--target-force-n", f"{candidate.target_force_n:.8g}",
        "--normal-axis", "fz",
        "--normal-sign", "1",
        "--max-normal-force-n", "50",
        "--max-force-norm-n", "60",
        "--max-torque-norm-nm", "3.0",
        "--step4e-mode", "line",
        "--step4e-version", BRIDGE_PROFILE,
        "--step5b-trial-profile", "none",
        "--step4e-path-shape", "cycloid",
        "--step4e-line-speed-m-s", "0.003",
        "--step4e-line-settle-s", "0.0",
        "--step4e-path-p-gain", "1.5",
        "--step4e-motion-limit-m-s", "0.004",
        "--step4e-total-linear-limit-m-s", "0.004",
        "--step4e-normal-velocity-limit-m-s", "0.01",
        "--step4e-force-p-gain", f"{candidate.force_p_gain:.10g}",
        "--step4e-force-i-gain", f"{candidate.force_i_gain:.10g}",
        "--step4e-force-damping", f"{candidate.force_damping:.10g}",
        "--step4e-normal-command-sign", "1",
        "--step4e-integral-limit-n-s", "1.0",
        "--step4e-min-force-for-control-n", "1.0",
        "--step4e-acquire-grace-s", "0.25",
        "--step4e-reacquire-velocity-m-s", "0.0004",
        "--step4e-orientation-gain", "0.20",
        "--step4e-orientation-wx-sign", "1",
        "--step4e-orientation-wy-sign", "1",
        "--step4e-angular-limit-rad-s", "0.150",
        "--step4e-contact-offset-min-fz-n", "1.0",
        "--step4e-normal-follow-mode", "filtered_live",
        "--step4e-normal-filter-tau-s", "0.35",
        "--step4e-normal-filter-alpha", f"{candidate.normal_filter_alpha:.10g}",
        "--step4e-normal-max-rate-rad-s", "0.010",
        "--step4e-normal-min-force-n", "2.0",
        "--step4e-normal-max-angle-from-latch-deg", "20",
        "--step4e-normal-friction-projection", "on",
        "--output-dir", str(output_dir),
    ]


def is_known_bad_history_path(path: Path) -> bool:
    return path.name.endswith("20260702_112156")
