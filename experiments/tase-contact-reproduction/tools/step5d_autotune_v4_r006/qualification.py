"""Fresh qualification branch with execution-id binding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .lattice import ANCHOR_POINT, ParameterPoint
from .runtime import AttemptIdentity, AttemptResult, Disposition, R006RuntimeError


@dataclass(frozen=True)
class QualificationTrial:
    ordinal: int
    execution_id: str
    point: ParameterPoint = ANCHOR_POINT

    def __post_init__(self) -> None:
        if self.ordinal not in (1, 2, 3) or not self.execution_id:
            raise R006RuntimeError("qualification trial requires ordinal 1..3 and execution_id")


class QualificationBranch:
    """Exactly three fresh passing rows; parent qualifications are audit-only."""

    def __init__(self) -> None:
        self.trials: list[QualificationTrial] = []

    def bind(self, *, execution_id: str) -> QualificationTrial:
        ordinal = len(self.trials) + 1
        trial = QualificationTrial(ordinal, execution_id)
        self.trials.append(trial)
        return trial

    def accept(self, result: AttemptResult) -> None:
        if result.identity.kind != "QUALIFICATION":
            raise R006RuntimeError("qualification branch received a non-qualification result")
        if result.identity.execution_id not in {trial.execution_id for trial in self.trials}:
            raise R006RuntimeError("qualification result execution_id is not bound")
        if result.disposition is not Disposition.OBJECTIVE or not all(
            (result.safe_return, result.safety_gate, result.motion_gate, result.timing_gate, result.identity_gate)
        ):
            raise R006RuntimeError("qualification result did not pass all gates")

    @property
    def complete(self) -> bool:
        return len(self.trials) == 3 and len({trial.execution_id for trial in self.trials}) == 3


__all__ = ["QualificationBranch", "QualificationTrial"]
