"""Cold-readable checkpoint receipts for the offline R013 floor campaign.

This module only interprets an already reconstructed ``FloorDiscoveryCoordinator``.
It never creates observations, advances the coordinator, or performs physical I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Mapping, Sequence, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for static type checkers
    from .floor_coordinator import FloorDiscoveryCoordinator


CHECKPOINT_SCHEMA = "step5d.autotune-v4/r013-checkpoint-receipt-v1"
CHECKPOINT_VERSION = 1
COORDINATOR_SCHEMA = "step5d.autotune-v4/r013-floor-coordinator-v1"
COORDINATOR_VERSION = 1
CHECKPOINT_IDS = (100, 160, 200, "final")


class R013CheckpointError(ValueError):
    """The requested report checkpoint cannot be truthfully materialized."""


def _finite(value: Any, field: str, *, nonnegative: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise R013CheckpointError(f"R013 checkpoint {field} is not numeric") from exc
    if not math.isfinite(parsed) or (nonnegative and parsed < 0.0):
        raise R013CheckpointError(f"R013 checkpoint {field} is invalid")
    return parsed


def _fingerprint_key(value: Mapping[str, Any] | str | None) -> str:
    if isinstance(value, str):
        if not value:
            raise R013CheckpointError("R013 checkpoint fingerprint is empty")
        return value
    if isinstance(value, Mapping):
        try:
            return json.dumps(
                dict(value), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise R013CheckpointError("R013 checkpoint fingerprint is not strict JSON") from exc
    raise R013CheckpointError("R013 checkpoint fingerprint is invalid")


@dataclass(frozen=True)
class R013CheckpointPlanV1:
    """Fixed report checkpoints; the live campaign remains owned by the coordinator."""

    novel_target: int = 200
    checkpoints: tuple[int, ...] = (100, 160, 200)
    final_top_k: int = 3
    final_repeat_target_n: int = 5
    schema: str = "step5d.autotune-v4/r013-checkpoint-plan-v1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != "step5d.autotune-v4/r013-checkpoint-plan-v1" or self.version != 1:
            raise R013CheckpointError("R013 checkpoint plan schema/version differs")
        if self.novel_target != 200 or self.checkpoints != (100, 160, 200):
            raise R013CheckpointError("R013 checkpoint plan counts differ")
        if self.final_top_k != 3 or self.final_repeat_target_n != 5:
            raise R013CheckpointError("R013 checkpoint plan repeat targets differ")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "novel_target": self.novel_target,
            "checkpoints": list(self.checkpoints),
            "final_top_k": self.final_top_k,
            "final_repeat_target_n": self.final_repeat_target_n,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R013CheckpointPlanV1":
        required = {"schema", "version", "novel_target", "checkpoints", "final_top_k", "final_repeat_target_n"}
        if not isinstance(value, Mapping) or set(value) != required:
            raise R013CheckpointError("R013 checkpoint plan fields differ")
        checkpoints = value["checkpoints"]
        if not isinstance(checkpoints, Sequence) or isinstance(checkpoints, (str, bytes)):
            raise R013CheckpointError("R013 checkpoint plan checkpoints are invalid")
        return cls(
            schema=value["schema"], version=value["version"], novel_target=value["novel_target"],
            checkpoints=tuple(checkpoints), final_top_k=value["final_top_k"],
            final_repeat_target_n=value["final_repeat_target_n"],
        )


@dataclass(frozen=True)
class R013RepeatGroupReceiptV1:
    canonical_token: str
    n: int
    mean_n: float
    sample_variance_n2: float
    schema: str = "step5d.autotune-v4/r013-repeat-group-receipt-v1"
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != "step5d.autotune-v4/r013-repeat-group-receipt-v1" or self.version != 1:
            raise R013CheckpointError("R013 repeat-group schema/version differs")
        if not isinstance(self.canonical_token, str) or not self.canonical_token:
            raise R013CheckpointError("R013 repeat-group token is invalid")
        if type(self.n) is not int or self.n < 1:
            raise R013CheckpointError("R013 repeat-group n is invalid")
        mean = _finite(self.mean_n, "repeat-group mean", nonnegative=True)
        variance = _finite(self.sample_variance_n2, "repeat-group variance", nonnegative=True)
        object.__setattr__(self, "mean_n", mean)
        object.__setattr__(self, "sample_variance_n2", variance)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "canonical_token": self.canonical_token,
            "n": self.n,
            "mean_n": self.mean_n,
            "sample_variance_n2": self.sample_variance_n2,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R013RepeatGroupReceiptV1":
        required = {"schema", "version", "canonical_token", "n", "mean_n", "sample_variance_n2"}
        if not isinstance(value, Mapping) or set(value) != required:
            raise R013CheckpointError("R013 repeat-group fields differ")
        return cls(**dict(value))


@dataclass(frozen=True)
class R013CheckpointReceiptV1:
    checkpoint: int | str
    fingerprint: Mapping[str, Any] | str
    observed_novel_count: int
    target_novel_count: int
    state: str
    ready: bool
    complete: bool
    target_checkpoint: bool
    checkpoint_only: bool
    empirical_floor_claimed: bool
    novel_role_counts: Mapping[str, int]
    single_trial_min_n: float | None
    final_top3: tuple[R013RepeatGroupReceiptV1, ...] = ()
    schema: str = CHECKPOINT_SCHEMA
    version: int = CHECKPOINT_VERSION

    def __post_init__(self) -> None:
        if self.schema != CHECKPOINT_SCHEMA or self.version != CHECKPOINT_VERSION:
            raise R013CheckpointError("R013 checkpoint receipt schema/version differs")
        if self.checkpoint not in CHECKPOINT_IDS:
            raise R013CheckpointError("R013 checkpoint id is invalid")
        _fingerprint_key(self.fingerprint)
        for name in ("observed_novel_count", "target_novel_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise R013CheckpointError(f"R013 checkpoint {name} is invalid")
        if self.target_novel_count != 200 or not isinstance(self.state, str) or not self.state:
            raise R013CheckpointError("R013 checkpoint state/count is invalid")
        for name in ("ready", "complete", "target_checkpoint", "checkpoint_only", "empirical_floor_claimed"):
            if type(getattr(self, name)) is not bool:
                raise R013CheckpointError(f"R013 checkpoint {name} is not bool")
        if self.empirical_floor_claimed and not (self.checkpoint == "final" and self.ready and self.complete):
            raise R013CheckpointError("R013 empirical-floor claim is not final/complete")
        if self.checkpoint != "final" and self.empirical_floor_claimed:
            raise R013CheckpointError("R013 numeric checkpoint cannot claim empirical floor")
        if self.checkpoint != "final":
            if self.observed_novel_count != self.checkpoint:
                raise R013CheckpointError("R013 numeric checkpoint observed count differs")
            if self.ready is not True or self.complete is not False:
                raise R013CheckpointError("R013 numeric checkpoint readiness differs")
            if self.checkpoint_only is not True:
                raise R013CheckpointError("R013 numeric checkpoint must be checkpoint-only")
        elif (
            self.observed_novel_count != self.target_novel_count
            or self.ready is not True
            or self.complete is not True
            or self.checkpoint_only is not False
            or self.empirical_floor_claimed is not True
        ):
            raise R013CheckpointError("R013 final checkpoint readiness differs")
        if self.single_trial_min_n is not None:
            object.__setattr__(self, "single_trial_min_n", _finite(self.single_trial_min_n, "single-trial minimum", nonnegative=True))
        if not isinstance(self.novel_role_counts, Mapping):
            raise R013CheckpointError("R013 checkpoint role counts are invalid")
        counts: dict[str, int] = {}
        for role, count in self.novel_role_counts.items():
            if not isinstance(role, str) or not role or type(count) is not int or count < 0:
                raise R013CheckpointError("R013 checkpoint role counts are invalid")
            counts[role] = count
        object.__setattr__(self, "novel_role_counts", counts)
        groups = tuple(self.final_top3)
        if any(not isinstance(group, R013RepeatGroupReceiptV1) for group in groups):
            raise R013CheckpointError("R013 checkpoint final groups are not typed")
        if self.checkpoint == "final" and self.ready and len(groups) != 3:
            raise R013CheckpointError("R013 final checkpoint requires three top groups")
        if self.checkpoint == "final" and self.ready and any(group.n < 5 for group in groups):
            raise R013CheckpointError("R013 final checkpoint requires n>=5 top groups")
        if self.checkpoint != "final" and groups:
            raise R013CheckpointError("R013 numeric checkpoint cannot expose final groups")
        object.__setattr__(self, "final_top3", groups)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "checkpoint": self.checkpoint,
            "fingerprint": self.fingerprint,
            "observed_novel_count": self.observed_novel_count,
            "target_novel_count": self.target_novel_count,
            "state": self.state,
            "ready": self.ready,
            "complete": self.complete,
            "target_checkpoint": self.target_checkpoint,
            "checkpoint_only": self.checkpoint_only,
            "empirical_floor_claimed": self.empirical_floor_claimed,
            "novel_role_counts": dict(self.novel_role_counts),
            "single_trial_min_n": self.single_trial_min_n,
            "final_top3": [group.as_dict() for group in self.final_top3],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R013CheckpointReceiptV1":
        required = {
            "schema", "version", "checkpoint", "fingerprint", "observed_novel_count",
            "target_novel_count", "state", "ready", "complete", "target_checkpoint",
            "checkpoint_only", "empirical_floor_claimed", "novel_role_counts",
            "single_trial_min_n", "final_top3",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise R013CheckpointError("R013 checkpoint receipt fields differ")
        groups = value["final_top3"]
        if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
            raise R013CheckpointError("R013 checkpoint final groups are invalid")
        return cls(
            **{key: value[key] for key in required if key != "final_top3"},
            final_top3=tuple(R013RepeatGroupReceiptV1.from_mapping(group) for group in groups),
        )


def _coordinator_rows(coordinator: Any) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, Any]]:
    if not hasattr(coordinator, "snapshot") or not hasattr(coordinator, "novel_rows") or not hasattr(coordinator, "stats"):
        raise R013CheckpointError("R013 checkpoint requires a floor coordinator")
    rows = getattr(coordinator, "novel_rows")
    stats = getattr(coordinator, "stats")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not isinstance(stats, Mapping):
        raise R013CheckpointError("R013 checkpoint coordinator evidence is malformed")
    if any(not isinstance(key, str) or not isinstance(value, Mapping) for key, value in stats.items()):
        raise R013CheckpointError("R013 checkpoint coordinator stats are malformed")
    parsed_rows: list[dict[str, Any]] = []
    tokens: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"canonical_token", "role", "sealed_mae_n", "trial"}:
            raise R013CheckpointError("R013 checkpoint novel row is incomplete or unsealed")
        token = row["canonical_token"]
        if not isinstance(token, str) or not token or token in tokens:
            raise R013CheckpointError("R013 checkpoint novel tokens are not unique")
        if not isinstance(row["role"], str) or not row["role"] or not isinstance(row["trial"], Mapping):
            raise R013CheckpointError("R013 checkpoint novel row is malformed")
        objective = _finite(row["sealed_mae_n"], "sealed MAE", nonnegative=True)
        tokens.add(token)
        parsed_rows.append({"canonical_token": token, "role": row["role"], "sealed_mae_n": objective, "trial": dict(row["trial"])})
        stat = stats.get(token)
        if not isinstance(stat, Mapping) or not isinstance(stat.get("values"), Sequence) or isinstance(stat.get("values"), (str, bytes)) or not stat["values"]:
            raise R013CheckpointError("R013 checkpoint group is incomplete or unsealed")
        values = tuple(_finite(value, "repeat MAE", nonnegative=True) for value in stat["values"])
        evidence = stat.get("guardrail_evidence")
        if evidence is not None and (not isinstance(evidence, Sequence) or isinstance(evidence, (str, bytes)) or len(evidence) != len(values)):
            raise R013CheckpointError("R013 checkpoint guardrail evidence is incomplete")
    try:
        snapshot = coordinator.snapshot()
    except (KeyError, TypeError, ValueError) as exc:
        raise R013CheckpointError("R013 checkpoint coordinator snapshot is malformed") from exc
    if not isinstance(snapshot, Mapping) or snapshot.get("schema") != COORDINATOR_SCHEMA or snapshot.get("version") != COORDINATOR_VERSION:
        raise R013CheckpointError("R013 checkpoint coordinator snapshot schema differs")
    if snapshot.get("novel_count") != len(parsed_rows) or set(stats).intersection(tokens) != tokens:
        raise R013CheckpointError("R013 checkpoint coordinator novel count differs")
    return snapshot, {str(key): dict(value) for key, value in stats.items()}, {"rows": parsed_rows, "tokens": tokens}


def build_checkpoint_receipt(
    coordinator: "FloorDiscoveryCoordinator",
    checkpoint: int | str,
    *,
    plan: R013CheckpointPlanV1 | None = None,
    fingerprint: Mapping[str, Any] | str | None = None,
) -> R013CheckpointReceiptV1:
    """Build a receipt from reconstructed coordinator state without advancing it."""
    parsed_plan = plan or R013CheckpointPlanV1()
    if checkpoint not in CHECKPOINT_IDS:
        raise R013CheckpointError("R013 checkpoint id is invalid")
    snapshot, stats, evidence = _coordinator_rows(coordinator)
    expected_fingerprint = snapshot.get("fingerprint")
    if fingerprint is not None and _fingerprint_key(fingerprint) != _fingerprint_key(expected_fingerprint):
        raise R013CheckpointError("R013 checkpoint fingerprint mismatch")
    observed = int(snapshot["novel_count"])
    if observed > parsed_plan.novel_target:
        raise R013CheckpointError("R013 checkpoint novel count exceeds target")
    role_counts: dict[str, int] = {}
    for row in evidence["rows"]:
        role_counts[row["role"]] = role_counts.get(row["role"], 0) + 1
    minimum = min((float(row["sealed_mae_n"]) for row in evidence["rows"]), default=None)
    final_groups: tuple[R013RepeatGroupReceiptV1, ...] = ()
    if checkpoint == "final":
        if observed != parsed_plan.novel_target:
            raise R013CheckpointError("R013 final checkpoint requires 200 novel rows")
        if snapshot.get("state") != "running" or snapshot.get("complete") is not True:
            raise R013CheckpointError("R013 final checkpoint is not complete")
        if snapshot.get("sentinel_due") is not False or snapshot.get("repeat_queue_count") != 0 or snapshot.get("final_queue_count") != 0 or snapshot.get("final_repeats_complete") is not True:
            raise R013CheckpointError("R013 final checkpoint queues are not drained")
        ranked: list[tuple[float, str, int, float]] = []
        for token, stat in stats.items():
            if not stat.get("is_novel"):
                continue
            values = tuple(_finite(value, "repeat MAE", nonnegative=True) for value in stat.get("values", ()))
            if len(values) < parsed_plan.final_repeat_target_n:
                continue
            evidence_rows = stat.get("guardrail_evidence", ())
            if (
                not isinstance(evidence_rows, Sequence)
                or isinstance(evidence_rows, (str, bytes))
                or len(evidence_rows) != len(values)
                or any(not isinstance(row, Mapping) or row.get("passed") is not True for row in evidence_rows)
            ):
                continue
            mean = math.fsum(values) / len(values)
            variance = math.fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
            ranked.append((mean, str(token), len(values), variance))
        ranked.sort(key=lambda value: (value[0], value[1]))
        if len(ranked) < parsed_plan.final_top_k:
            raise R013CheckpointError("R013 final checkpoint lacks three valid n=5 groups")
        final_groups = tuple(
            R013RepeatGroupReceiptV1(token, n, mean, variance)
            for mean, token, n, variance in ranked[: parsed_plan.final_top_k]
        )
    else:
        if type(checkpoint) is not int or checkpoint not in parsed_plan.checkpoints:
            raise R013CheckpointError("R013 numeric checkpoint id is invalid")
        if observed != checkpoint:
            raise R013CheckpointError("R013 numeric checkpoint requires exact observed count")
    ready = True
    complete = bool(snapshot.get("complete")) if checkpoint == "final" else False
    return R013CheckpointReceiptV1(
        checkpoint=checkpoint,
        fingerprint=expected_fingerprint,
        observed_novel_count=observed,
        target_novel_count=parsed_plan.novel_target,
        state=str(snapshot["state"]),
        ready=ready,
        complete=complete,
        target_checkpoint=bool(snapshot.get("target_checkpoint")),
        checkpoint_only=checkpoint != "final",
        empirical_floor_claimed=checkpoint == "final",
        novel_role_counts=role_counts,
        single_trial_min_n=minimum,
        final_top3=final_groups,
    )


__all__ = [
    "CHECKPOINT_IDS", "CHECKPOINT_SCHEMA", "CHECKPOINT_VERSION",
    "R013CheckpointError", "R013CheckpointPlanV1", "R013CheckpointReceiptV1",
    "R013RepeatGroupReceiptV1", "build_checkpoint_receipt",
]
