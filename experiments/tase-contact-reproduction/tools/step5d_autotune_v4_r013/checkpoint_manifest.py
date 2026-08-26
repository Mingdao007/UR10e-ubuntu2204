"""Offline materialization of report checkpoints from a coordinator event log."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence, TYPE_CHECKING

from .checkpoint import (
    CHECKPOINT_IDS,
    R013CheckpointError,
    R013CheckpointPlanV1,
    R013CheckpointReceiptV1,
    build_checkpoint_receipt,
)
from .floor_coordinator import FloorDiscoveryCoordinator, NOVEL_ROLES

if TYPE_CHECKING:  # pragma: no cover
    from .floor_coordinator import FloorTrialRequest


MANIFEST_SCHEMA = "step5d.autotune-v4/r013-checkpoint-manifest-v1"
MANIFEST_VERSION = 1


class R013CheckpointManifestError(ValueError):
    """The coordinator event log cannot produce a truthful checkpoint manifest."""


def _fingerprint_key(value: Mapping[str, Any] | str) -> str:
    if isinstance(value, str):
        if not value:
            raise R013CheckpointManifestError("R013 checkpoint manifest fingerprint is empty")
        return value
    if isinstance(value, Mapping):
        try:
            return json.dumps(
                dict(value), sort_keys=True, separators=(",", ":"),
                ensure_ascii=False, allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise R013CheckpointManifestError(
                "R013 checkpoint manifest fingerprint is not strict JSON"
            ) from exc
    raise R013CheckpointManifestError("R013 checkpoint manifest fingerprint is invalid")


@dataclass(frozen=True)
class R013CheckpointManifestV1:
    fingerprint: Mapping[str, Any] | str
    current_novel_count: int
    current_state: str
    current_complete: bool
    receipts: tuple[R013CheckpointReceiptV1, ...]
    event_count: int
    schema: str = MANIFEST_SCHEMA
    version: int = MANIFEST_VERSION

    def __post_init__(self) -> None:
        if self.schema != MANIFEST_SCHEMA or self.version != MANIFEST_VERSION:
            raise R013CheckpointManifestError("R013 checkpoint manifest schema/version differs")
        _fingerprint_key(self.fingerprint)
        if type(self.current_novel_count) is not int or self.current_novel_count < 0:
            raise R013CheckpointManifestError("R013 checkpoint manifest novel count is invalid")
        if not isinstance(self.current_state, str) or not self.current_state:
            raise R013CheckpointManifestError("R013 checkpoint manifest state is invalid")
        if type(self.current_complete) is not bool:
            raise R013CheckpointManifestError("R013 checkpoint manifest complete flag is invalid")
        if type(self.event_count) is not int or self.event_count < 0:
            raise R013CheckpointManifestError("R013 checkpoint manifest event count is invalid")
        receipts = tuple(self.receipts)
        if any(not isinstance(receipt, R013CheckpointReceiptV1) for receipt in receipts):
            raise R013CheckpointManifestError("R013 checkpoint manifest receipts are not typed")
        order = {checkpoint: index for index, checkpoint in enumerate(CHECKPOINT_IDS)}
        seen: set[int | str] = set()
        previous = -1
        for receipt in receipts:
            if receipt.checkpoint in seen or _fingerprint_key(receipt.fingerprint) != _fingerprint_key(self.fingerprint):
                raise R013CheckpointManifestError("R013 checkpoint manifest receipt identity differs")
            if receipt.observed_novel_count > self.current_novel_count:
                raise R013CheckpointManifestError("R013 checkpoint manifest contains a future receipt")
            if receipt.checkpoint == "final" and not self.current_complete:
                raise R013CheckpointManifestError("R013 final receipt requires a complete manifest")
            seen.add(receipt.checkpoint)
            current = order[receipt.checkpoint]
            if current <= previous:
                raise R013CheckpointManifestError("R013 checkpoint manifest receipt order differs")
            previous = current
        object.__setattr__(self, "receipts", receipts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "fingerprint": self.fingerprint,
            "current_novel_count": self.current_novel_count,
            "current_state": self.current_state,
            "current_complete": self.current_complete,
            "receipts": [receipt.as_dict() for receipt in self.receipts],
            "event_count": self.event_count,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "R013CheckpointManifestV1":
        required = {
            "schema", "version", "fingerprint", "current_novel_count", "current_state",
            "current_complete", "receipts", "event_count",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            raise R013CheckpointManifestError("R013 checkpoint manifest fields differ")
        receipts = value["receipts"]
        if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
            raise R013CheckpointManifestError("R013 checkpoint manifest receipts are invalid")
        return cls(
            schema=value["schema"],
            version=value["version"],
            fingerprint=value["fingerprint"],
            current_novel_count=value["current_novel_count"],
            current_state=value["current_state"],
            current_complete=value["current_complete"],
            receipts=tuple(R013CheckpointReceiptV1.from_mapping(item) for item in receipts),
            event_count=value["event_count"],
        )


def _active_event_prefixes(
    coordinator: FloorDiscoveryCoordinator,
    plan: R013CheckpointPlanV1,
) -> tuple[dict[int, tuple[Mapping[str, Any], ...]], tuple[Mapping[str, Any], ...]]:
    events = tuple(coordinator.event_log)
    if any(not isinstance(event, Mapping) for event in events):
        raise R013CheckpointManifestError("R013 checkpoint manifest event log is malformed")
    restart_indices = [index for index, event in enumerate(events) if event.get("event") == "restart"]
    start = restart_indices[-1] if restart_indices else 0
    active = events[start:]
    request_roles: dict[str, str] = {}
    novel_count = 0
    prefixes: dict[int, tuple[Mapping[str, Any], ...]] = {}
    for index, event in enumerate(active):
        kind = event.get("event")
        if kind == "request":
            request = event.get("request")
            if not isinstance(request, Mapping) or not isinstance(request.get("request_id"), str):
                raise R013CheckpointManifestError("R013 checkpoint manifest request is malformed")
            role = request.get("role")
            if not isinstance(role, str):
                raise R013CheckpointManifestError("R013 checkpoint manifest request role is malformed")
            request_roles[request["request_id"]] = role
            continue
        if kind != "result":
            continue
        request_id = event.get("request_id")
        role = request_roles.get(request_id)
        if role is None:
            raise R013CheckpointManifestError("R013 checkpoint manifest result lacks its request")
        if (
            role in NOVEL_ROLES
            and event.get("admitted") is True
            and event.get("complete") is True
            and event.get("sealed") is True
            and event.get("sealed_mae_n") is not None
        ):
            novel_count += 1
            if novel_count in plan.checkpoints:
                prefixes[novel_count] = active[: index + 1]
    return prefixes, active


def _replay_prefix(
    coordinator: FloorDiscoveryCoordinator,
    records: Sequence[Mapping[str, Any]],
) -> FloorDiscoveryCoordinator:
    try:
        return FloorDiscoveryCoordinator.from_records(
            coordinator.policy,
            records,
            fingerprint=coordinator.fingerprint,
            seed_candidate=coordinator.seed_candidate,
            frozen_incumbent_n=coordinator.frozen_incumbent_n,
            core_proposal_provider=coordinator.core_proposal_provider,
            correction_proposal_provider=coordinator.correction_proposal_provider,
        )
    except Exception as exc:  # normalize replay failures at the manifest boundary
        raise R013CheckpointManifestError(
            "R013 checkpoint manifest event-log replay failed"
        ) from exc


def materialize_checkpoint_manifest(
    coordinator: FloorDiscoveryCoordinator,
    *,
    plan: R013CheckpointPlanV1 | None = None,
) -> R013CheckpointManifestV1:
    """Build all truthful checkpoints present in the current fingerprint generation."""
    if not isinstance(coordinator, FloorDiscoveryCoordinator):
        raise R013CheckpointManifestError("R013 checkpoint manifest requires a floor coordinator")
    parsed_plan = plan or R013CheckpointPlanV1()
    prefixes, active_events = _active_event_prefixes(coordinator, parsed_plan)
    receipts: list[R013CheckpointReceiptV1] = []
    for checkpoint in parsed_plan.checkpoints:
        prefix = prefixes.get(checkpoint)
        if prefix is None:
            continue
        replayed = _replay_prefix(coordinator, prefix)
        try:
            receipts.append(build_checkpoint_receipt(replayed, checkpoint, plan=parsed_plan))
        except R013CheckpointError as exc:
            raise R013CheckpointManifestError(
                f"R013 checkpoint manifest cannot materialize {checkpoint}"
            ) from exc
    if coordinator.complete:
        try:
            receipts.append(build_checkpoint_receipt(coordinator, "final", plan=parsed_plan))
        except R013CheckpointError as exc:
            raise R013CheckpointManifestError(
                "R013 checkpoint manifest final receipt is not truthful"
            ) from exc
    snapshot = coordinator.snapshot()
    fingerprint = snapshot.get("fingerprint")
    try:
        return R013CheckpointManifestV1(
            fingerprint=fingerprint,
            current_novel_count=int(snapshot["novel_count"]),
            current_state=str(snapshot["state"]),
            current_complete=bool(snapshot["complete"]),
            receipts=tuple(receipts),
            event_count=len(active_events),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise R013CheckpointManifestError(
            "R013 checkpoint manifest coordinator snapshot is malformed"
        ) from exc


__all__ = [
    "MANIFEST_SCHEMA", "MANIFEST_VERSION", "R013CheckpointManifestError",
    "R013CheckpointManifestV1", "materialize_checkpoint_manifest",
]
