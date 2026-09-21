"""Fixed-input offline comparison of the two retained RNN solver profiles.

The runner is an adapter around an existing pure solver call.  It deliberately
does not install a profile, open a device, or promote a result to live use.
Its job is to prove that Profile A (legacy r=1) and Profile B (historical
r=.8/512) consumed the same observations and to retain the requested
state/output/residual/timing/safety diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import statistics
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from step5d_autotune_v4_r014.solver_profile import FINITE_TIME_R08, LEGACY_R1, SolverProfile


class ProfileReplayError(ValueError):
    """The offline comparison cannot establish common-input evidence."""


@dataclass(frozen=True)
class ReplayRecord:
    output: tuple[float, ...]
    state: tuple[float, ...]
    residual: float
    converged: bool
    compute_ms: float
    control_interval_s: float
    qdot: tuple[float, ...]
    qp_feasible: bool
    slew_ok: bool
    safety_ok: bool


@dataclass(frozen=True)
class ProfileReplayResult:
    profile: SolverProfile
    input_digest: str
    records: tuple[ReplayRecord, ...]

    @property
    def p99_compute_ms(self) -> float:
        values = sorted(record.compute_ms for record in self.records)
        if not values:
            return float("nan")
        return values[max(0, math.ceil(0.99 * len(values)) - 1)]

    @property
    def safety_ok(self) -> bool:
        return all(
            record.qp_feasible and record.slew_ok and record.safety_ok
            and all(math.isfinite(value) for value in record.qdot)
            for record in self.records
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.as_dict(),
            "profile_sha256": self.profile.sha256,
            "input_digest": self.input_digest,
            "samples": len(self.records),
            "p99_compute_ms": self.p99_compute_ms,
            "safety_ok": self.safety_ok,
            "records": [record.__dict__ for record in self.records],
        }


def _digest_inputs(samples: Sequence[Mapping[str, Any]]) -> str:
    material = json.dumps(samples, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _record_from_mapping(value: Mapping[str, Any]) -> ReplayRecord:
    def finite(name: str) -> float:
        result = float(value[name])
        if not math.isfinite(result):
            raise ProfileReplayError(f"replay field {name} is not finite")
        return result

    def vector(name: str) -> tuple[float, ...]:
        result = tuple(float(item) for item in value[name])
        if not result or not all(math.isfinite(item) for item in result):
            raise ProfileReplayError(f"replay field {name} is not finite")
        return result

    return ReplayRecord(
        output=vector("output"),
        state=vector("state"),
        residual=finite("residual"),
        converged=bool(value["converged"]),
        compute_ms=finite("compute_ms"),
        control_interval_s=finite("control_interval_s"),
        qdot=vector("qdot"),
        qp_feasible=value.get("qp_feasible") is True,
        slew_ok=value.get("slew_ok") is True,
        safety_ok=value.get("safety_ok") is True,
    )


def run_profile_replay(
    samples: Iterable[Mapping[str, Any]],
    profile: SolverProfile,
    step: Callable[[SolverProfile, Mapping[str, Any], Any], Mapping[str, Any]],
    *,
    initial_state: Any = None,
) -> ProfileReplayResult:
    if not isinstance(profile, SolverProfile):
        raise ProfileReplayError("profile is not typed")
    inputs = [dict(sample) for sample in samples]
    digest = _digest_inputs(inputs)
    state = initial_state
    records: list[ReplayRecord] = []
    for sample in inputs:
        started = time.perf_counter()
        result = step(profile, sample, state)
        if not isinstance(result, Mapping):
            raise ProfileReplayError("solver replay result is not a mapping")
        if result.get("compute_ms") is None:
            result = {**result, "compute_ms": (time.perf_counter() - started) * 1000.0}
        state = result.get("next_state")
        records.append(_record_from_mapping(result))
    return ProfileReplayResult(profile=profile, input_digest=digest, records=tuple(records))


def compare_profile_replays(
    left: ProfileReplayResult,
    right: ProfileReplayResult,
) -> dict[str, Any]:
    if left.input_digest != right.input_digest:
        raise ProfileReplayError("profile replays consumed different fixed inputs")
    if len(left.records) != len(right.records):
        raise ProfileReplayError("profile replays have different sample counts")
    output_delta = [
        max(abs(a - b) for a, b in zip(l.output, r.output, strict=True))
        for l, r in zip(left.records, right.records, strict=True)
    ]
    state_delta = [
        max(abs(a - b) for a, b in zip(l.state, r.state, strict=True))
        for l, r in zip(left.records, right.records, strict=True)
    ]
    residual_delta = [abs(l.residual - r.residual) for l, r in zip(left.records, right.records, strict=True)]
    convergence_delta = sum(l.converged != r.converged for l, r in zip(left.records, right.records, strict=True))
    control_intervals = [record.control_interval_s for record in (*left.records, *right.records)]
    return {
        "schema": "tase.rnn-profile-replay-comparison-v1",
        "input_digest": left.input_digest,
        "profiles": [left.profile.as_dict(), right.profile.as_dict()],
        "samples": len(left.records),
        "max_output_delta": max(output_delta, default=0.0),
        "max_state_delta": max(state_delta, default=0.0),
        "max_residual_delta": max(residual_delta, default=0.0),
        "convergence_difference_count": convergence_delta,
        "p99_compute_ms": {
            left.profile.profile_id: left.p99_compute_ms,
            right.profile.profile_id: right.p99_compute_ms,
        },
        "control_interval_s": {
            "min": min(control_intervals, default=float("nan")),
            "max": max(control_intervals, default=float("nan")),
            "mean": statistics.fmean(control_intervals) if control_intervals else float("nan"),
        },
        "qdot_bounds_ok": left.safety_ok and right.safety_ok,
        "promotion": "offline_comparison_only",
    }


__all__ = [
    "FINITE_TIME_R08",
    "LEGACY_R1",
    "ProfileReplayError",
    "ProfileReplayResult",
    "compare_profile_replays",
    "run_profile_replay",
]
