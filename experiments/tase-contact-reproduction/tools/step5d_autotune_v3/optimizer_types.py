"""Pure optimizer/control boundary types with no runtime or GPU side effects."""

from __future__ import annotations

from dataclasses import dataclass
import math

from step5d_autotune_contract import Evaluation, ForceCandidate, require_sha256
from ur10e_experiment_runtime.candidate_identity import ControlCandidateUid


PRODUCTION_OPTIMIZER_SEED = 9009


@dataclass(frozen=True)
class OutcomeRecord:
    candidate: ForceCandidate
    evaluation: Evaluation
    profile_id: str
    plant_epoch: int
    latest_trace_sha256: str | None = None
    control_candidate_uid: ControlCandidateUid | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ForceCandidate):
            raise ValueError("candidate must be a ForceCandidate")
        if not isinstance(self.evaluation, Evaluation):
            raise ValueError("evaluation must be an Evaluation")
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ValueError("profile_id must be a non-empty string")
        if (
            isinstance(self.plant_epoch, bool)
            or not isinstance(self.plant_epoch, int)
            or self.plant_epoch < 1
        ):
            raise ValueError("plant_epoch must be a positive integer")
        if self.latest_trace_sha256 is not None:
            require_sha256("latest_trace_sha256", self.latest_trace_sha256)
        if self.control_candidate_uid is not None:
            try:
                object.__setattr__(
                    self,
                    "control_candidate_uid",
                    ControlCandidateUid.parse(self.control_candidate_uid),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("observation control UID is invalid") from exc

    @property
    def optimizer_group_uid(self) -> str:
        if self.control_candidate_uid is not None:
            return str(self.control_candidate_uid)
        return f"historical-parameter:v1:{self.candidate.candidate_uid}"

    @property
    def eligible(self) -> bool:
        objective = self.evaluation.objective_mae_n
        return (
            self.evaluation.eligible
            and objective is not None
            and math.isfinite(float(objective))
        )

    @property
    def objective(self) -> float:
        if not self.eligible:
            raise ValueError("ineligible observation has no optimizer objective")
        return float(self.evaluation.objective_mae_n)


Observation = OutcomeRecord


__all__ = [
    "Observation",
    "OutcomeRecord",
    "PRODUCTION_OPTIMIZER_SEED",
]
