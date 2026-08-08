"""Unbounded quarter-octave graph, finite trust regions, and warm start."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Mapping, Sequence

from .contracts import (
    D_ANCHOR,
    I_ON_ANCHOR,
    KO_ANCHOR,
    KP_ANCHOR,
    P_ANCHOR,
    STEP_OCTAVE,
    TAU_ANCHOR,
)


class LatticeError(ValueError):
    """A point or graph edge violates the r006 parameter contract."""


class IMode(str, Enum):
    OFF = "OFF"
    ON = "ON"


@dataclass(frozen=True, order=True)
class ParameterPoint:
    """A graph coordinate; target force is deliberately absent."""

    p_step: int = 0
    d_step: int = 0
    tau_step: int = 0
    i_mode: IMode = IMode.OFF
    i_step: int | None = None
    ko_step: int = 0
    kp_step: int = 0

    def __post_init__(self) -> None:
        for name in ("p_step", "d_step", "tau_step", "ko_step", "kp_step"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise LatticeError(f"{name} must be an integer lattice coordinate")
        if not isinstance(self.i_mode, IMode):
            raise LatticeError("I mode is not typed")
        if self.i_mode is IMode.OFF:
            if self.i_step is not None:
                raise LatticeError("I-off point cannot carry an I-on exponent")
        else:
            if isinstance(self.i_step, bool) or not isinstance(self.i_step, int):
                raise LatticeError("I-on point requires an integer exponent")

    @property
    def key(self) -> tuple[object, ...]:
        return (self.p_step, self.d_step, self.tau_step, self.i_mode.value, self.i_step, self.ko_step, self.kp_step)

    @property
    def canonical(self) -> dict[str, object]:
        return {
            "P_log2": self.p_step * STEP_OCTAVE,
            "D_log2": self.d_step * STEP_OCTAVE,
            "tau_log2": self.tau_step * STEP_OCTAVE,
            "I_mode": self.i_mode.value,
            "I_on_log2": None if self.i_step is None else self.i_step * STEP_OCTAVE,
            "Ko_log2": self.ko_step * STEP_OCTAVE,
            "Kp_log2": self.kp_step * STEP_OCTAVE,
        }

    @property
    def p_gain(self) -> float:
        return P_ANCHOR * 2.0 ** (self.p_step * STEP_OCTAVE)

    @property
    def d_gain(self) -> float:
        return D_ANCHOR * 2.0 ** (self.d_step * STEP_OCTAVE)

    @property
    def tau_s(self) -> float:
        return TAU_ANCHOR * 2.0 ** (self.tau_step * STEP_OCTAVE)

    @property
    def i_gain(self) -> float:
        if self.i_mode is IMode.OFF:
            return 0.0
        assert self.i_step is not None
        return I_ON_ANCHOR * 2.0 ** (self.i_step * STEP_OCTAVE)

    @property
    def ko(self) -> float:
        return KO_ANCHOR * 2.0 ** (self.ko_step * STEP_OCTAVE)

    @property
    def kp(self) -> float:
        return KP_ANCHOR * 2.0 ** (self.kp_step * STEP_OCTAVE)

    @property
    def uid(self) -> str:
        import hashlib
        import json

        return hashlib.sha256(json.dumps(self.canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @property
    def physical_coordinates(self) -> tuple[float, ...]:
        return (self.p_gain, self.d_gain, self.tau_s, self.i_gain, self.ko, self.kp)

    def with_step(self, axis: str, delta: int) -> "ParameterPoint":
        if delta not in (-1, 1):
            raise LatticeError("one graph edge is at most one quarter octave")
        fields = {
            "P": "p_step",
            "D": "d_step",
            "tau": "tau_step",
            "Ko": "ko_step",
            "Kp": "kp_step",
        }
        if axis == "I":
            if self.i_mode is IMode.OFF:
                raise LatticeError("I-off must use the fixed I-on portal")
            assert self.i_step is not None
            return ParameterPoint(
                self.p_step, self.d_step, self.tau_step, IMode.ON,
                self.i_step + delta, self.ko_step, self.kp_step,
            )
        if axis not in fields:
            raise LatticeError(f"unknown lattice axis {axis}")
        values = self.key
        index = {"P": 0, "D": 1, "tau": 2, "Ko": 5, "Kp": 6}[axis]
        updated = list(values)
        updated[index] = int(updated[index]) + delta
        return ParameterPoint(
            int(updated[0]), int(updated[1]), int(updated[2]), IMode(str(updated[3])),
            updated[4], int(updated[5]), int(updated[6]),
        )


ANCHOR_POINT = ParameterPoint()
I_ON_ENTRY = ParameterPoint(i_mode=IMode.ON, i_step=-4)
I_ON_WARM_MINUS = ParameterPoint(i_mode=IMode.ON, i_step=-4)
I_ON_WARM_PLUS = ParameterPoint(i_mode=IMode.ON, i_step=-1)


def transition_is_legal(previous: ParameterPoint, candidate: ParameterPoint) -> bool:
    """Exactly one shared coordinate moves one quarter octave, or a portal."""

    if not isinstance(previous, ParameterPoint) or not isinstance(candidate, ParameterPoint):
        return False
    if previous == candidate:
        return False
    if previous.i_mode is IMode.OFF and candidate.i_mode is IMode.ON:
        return candidate.i_step == -4 and previous.key[:3] == candidate.key[:3] and previous.key[5:] == candidate.key[5:]
    if previous.i_mode is IMode.ON and candidate.i_mode is IMode.OFF:
        return previous.i_step == -4 and previous.key[:3] == candidate.key[:3] and previous.key[5:] == candidate.key[5:]
    deltas = [
        candidate.p_step - previous.p_step,
        candidate.d_step - previous.d_step,
        candidate.tau_step - previous.tau_step,
        candidate.ko_step - previous.ko_step,
        candidate.kp_step - previous.kp_step,
    ]
    i_delta = 0 if previous.i_mode is IMode.OFF else (candidate.i_step or 0) - (previous.i_step or 0)
    if previous.i_mode is not candidate.i_mode:
        return False
    return sum(abs(delta) for delta in deltas) + abs(i_delta) == 1


def neighbors(point: ParameterPoint) -> tuple[ParameterPoint, ...]:
    values: list[ParameterPoint] = []
    for axis in ("P", "D", "tau", "I", "Ko", "Kp"):
        for delta in (-1, 1):
            if axis == "I":
                if point.i_mode is IMode.OFF and delta == 1:
                    values.append(
                        ParameterPoint(
                            point.p_step,
                            point.d_step,
                            point.tau_step,
                            IMode.ON,
                            -4,
                            point.ko_step,
                            point.kp_step,
                        )
                    )
                elif point.i_mode is IMode.ON:
                    if point.i_step == -4 and delta == -1:
                        values.append(ParameterPoint(point.p_step, point.d_step, point.tau_step, IMode.OFF, None, point.ko_step, point.kp_step))
                    values.append(point.with_step("I", delta))
            else:
                values.append(point.with_step(axis, delta))
    unique = {item.key: item for item in values}
    return tuple(unique[key] for key in sorted(unique))


@dataclass(frozen=True)
class TrustRegion:
    """Finite region: ±0.5 octave on five shared axes, I mode unrestricted."""

    center: ParameterPoint
    radius_steps: int = 2

    def __post_init__(self) -> None:
        if self.radius_steps != 2:
            raise LatticeError("r006 trust region radius is fixed at ±0.5 octave")

    def contains(self, point: ParameterPoint) -> bool:
        return all(
            abs(left - right) <= self.radius_steps
            for left, right in (
                (self.center.p_step, point.p_step),
                (self.center.d_step, point.d_step),
                (self.center.tau_step, point.tau_step),
                (self.center.ko_step, point.ko_step),
                (self.center.kp_step, point.kp_step),
            )
        )

    @property
    def center_modes(self) -> tuple[ParameterPoint, ...]:
        return (
            ParameterPoint(self.center.p_step, self.center.d_step, self.center.tau_step, IMode.OFF, None, self.center.ko_step, self.center.kp_step),
            *tuple(
                ParameterPoint(self.center.p_step, self.center.d_step, self.center.tau_step, IMode.ON, step, self.center.ko_step, self.center.kp_step)
                for step in (-4, -3, -2, -1, 0)
            ),
        )

    def points(self) -> tuple[ParameterPoint, ...]:
        points: list[ParameterPoint] = []
        for p in range(self.center.p_step - 2, self.center.p_step + 3):
            for d in range(self.center.d_step - 2, self.center.d_step + 3):
                for tau in range(self.center.tau_step - 2, self.center.tau_step + 3):
                    for ko in range(self.center.ko_step - 2, self.center.ko_step + 3):
                        for kp in range(self.center.kp_step - 2, self.center.kp_step + 3):
                            points.append(ParameterPoint(p, d, tau, IMode.OFF, None, ko, kp))
                            points.extend(ParameterPoint(p, d, tau, IMode.ON, i, ko, kp) for i in (-4, -3, -2, -1, 0))
        return tuple(sorted(points, key=lambda item: item.key))


@dataclass(frozen=True)
class RoutePlan:
    points: tuple[ParameterPoint, ...]
    route_observations: tuple[ParameterPoint, ...]


def route_bfs(start: ParameterPoint, goal: ParameterPoint, *, region: TrustRegion | None = None) -> RoutePlan:
    if start == goal:
        return RoutePlan((start,), (start,))
    allowed = None if region is None else set(region.points())
    queue: deque[ParameterPoint] = deque([start])
    parents: dict[ParameterPoint, ParameterPoint | None] = {start: None}
    while queue:
        current = queue.popleft()
        for candidate in neighbors(current):
            if allowed is not None and candidate not in allowed:
                continue
            if candidate in parents:
                continue
            parents[candidate] = current
            if candidate == goal:
                queue.clear()
                break
            queue.append(candidate)
    if goal not in parents:
        raise LatticeError("goal has no legal BFS route")
    path: list[ParameterPoint] = []
    current: ParameterPoint | None = goal
    while current is not None:
        path.append(current)
        current = parents[current]
    path.reverse()
    return RoutePlan(tuple(path), tuple(path))


@dataclass
class RegionTraversal:
    region: TrustRegion
    score: Callable[[ParameterPoint], float] = lambda _point: 0.0
    _visited: set[ParameterPoint] = field(default_factory=set)
    _frontier: deque[ParameterPoint] = field(default_factory=deque)
    _boundary_turn: bool = True

    def __post_init__(self) -> None:
        self._frontier.append(self.region.center)

    def _boundary(self) -> list[ParameterPoint]:
        center = self.region.center
        candidates = [
            point
            for point in self.region.points()
            if any(
                abs(getattr(point, field) - getattr(center, field)) == self.region.radius_steps
                for field in ("p_step", "d_step", "tau_step", "ko_step", "kp_step")
            )
            and point not in self._visited
        ]
        return candidates

    def next(self) -> ParameterPoint | None:
        remaining = set(self.region.points()) - self._visited
        if not remaining:
            return None
        selected: ParameterPoint | None = None
        if self._boundary_turn:
            candidates = self._boundary()
            if candidates:
                selected = min(candidates, key=lambda point: (-float(self.score(point)), point.key))
        if selected is None:
            while self._frontier and self._frontier[0] in self._visited:
                self._frontier.popleft()
            if self._frontier:
                selected = self._frontier.popleft()
            else:
                selected = min(remaining, key=lambda point: point.key)
        self._visited.add(selected)
        for candidate in neighbors(selected):
            if candidate in remaining and self.region.contains(candidate) and candidate not in self._visited and candidate not in self._frontier:
                self._frontier.append(candidate)
        self._boundary_turn = not self._boundary_turn
        return selected

    @property
    def visited(self) -> frozenset[ParameterPoint]:
        return frozenset(self._visited)


@dataclass(frozen=True)
class PointObservation:
    point: ParameterPoint
    mae_n: float
    std_n: float = 0.0
    repeat_count: int = 1
    eligible: bool = True

    @property
    def ucb95_n(self) -> float:
        if not self.eligible:
            return float("inf")
        return self.mae_n + 1.96 * self.std_n / math.sqrt(max(1, self.repeat_count))

    def simultaneous_ucb95_n(self, eligible_count: int) -> float:
        if not self.eligible:
            return float("inf")
        if eligible_count <= 0:
            raise LatticeError("simultaneous UCB requires at least one eligible point")
        z = __import__("statistics").NormalDist().inv_cdf(1.0 - 0.05 / (2.0 * eligible_count))
        return self.mae_n + z * self.std_n / math.sqrt(max(1, self.repeat_count))


def select_second_center(observations: Iterable[PointObservation]) -> ParameterPoint:
    eligible = tuple(item for item in observations if item.eligible)
    if not eligible:
        raise LatticeError("no eligible point for second warm-start center")
    return min(eligible, key=lambda item: (item.simultaneous_ucb95_n(len(eligible)), item.point.key)).point


def warm_start_plan(center: ParameterPoint = ANCHOR_POINT) -> tuple[ParameterPoint, ...]:
    """Return the exact 25-trial first group after three qualifications."""

    sequence: list[ParameterPoint] = [center]
    for axis in ("P", "D", "tau", "Ko", "Kp"):
        sequence.extend((center.with_step(axis, -1), center, center.with_step(axis, 1), center))
    if center.i_mode is IMode.OFF:
        sequence.extend(
            (
                ParameterPoint(center.p_step, center.d_step, center.tau_step, IMode.ON, -4, center.ko_step, center.kp_step),
                ParameterPoint(center.p_step, center.d_step, center.tau_step, IMode.ON, -3, center.ko_step, center.kp_step),
                ParameterPoint(center.p_step, center.d_step, center.tau_step, IMode.ON, -4, center.ko_step, center.kp_step),
                center,
            )
        )
    else:
        assert center.i_step is not None
        sequence.extend((center.with_step("I", -1), center, center.with_step("I", 1), center))
    if len(sequence) != 25:
        raise AssertionError("r006 first warm-start group must contain exactly 25 trials")
    for previous, candidate in zip(sequence, sequence[1:]):
        if previous != candidate and not transition_is_legal(previous, candidate):
            raise AssertionError("warm-start plan contains an illegal transition")
    return tuple(sequence)


def second_warm_start_plan(center: ParameterPoint) -> tuple[ParameterPoint, ...]:
    if not isinstance(center, ParameterPoint):
        raise LatticeError("second center is not typed")
    return (center,) * 25


__all__ = [
    "ANCHOR_POINT",
    "I_ON_ENTRY",
    "IMode",
    "LatticeError",
    "ParameterPoint",
    "PointObservation",
    "RegionTraversal",
    "RoutePlan",
    "TrustRegion",
    "neighbors",
    "route_bfs",
    "second_warm_start_plan",
    "select_second_center",
    "transition_is_legal",
    "warm_start_plan",
]
