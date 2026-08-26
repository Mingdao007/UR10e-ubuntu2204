"""Durable extension-epoch contracts for the Home-only V5 campaign.

An extension epoch is a new optimizer namespace.  Physical rows remain in the
parent ledger for audit, while the next GP fit receives only the deterministic
active set described by this module.  No live transport or campaign decision
is opened here; owners call these pure, cold-verifiable helpers at an epoch
boundary after Home and writer release.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Mapping, Sequence

try:  # tests may put ``tools`` directly on sys.path
    from step6_figure8_autotune_v1.core import CompleteCandidateV1
except ModuleNotFoundError:  # pragma: no cover - repository-root imports
    from tools.step6_figure8_autotune_v1.core import CompleteCandidateV1


EXTENSION_POLICY_SCHEMA = "step6.autotune/figure8-v5-extension-policy-v1"
EXTENSION_POLICY_VERSION = 1
ACTIVE_SET_SCHEMA = "step6.autotune/figure8-v5-active-set-v1"
ACTIVE_SET_VERSION = 1
EPOCH_RECEIPT_SCHEMA = "step6.autotune/figure8-v5-extension-epoch-receipt-v1"
EPOCH_RECEIPT_VERSION = 1
EXTENSION_EXACT_TARGET = 200
ACTIVE_SET_MAX = 512
HISTORICAL_CHAMPION_MAX = 32
LOWEST_MAE_MAX = 128
RECENT_OBSERVATION_MAX = 160


class V5ExtensionError(RuntimeError):
    """An extension identity or active-set contract is invalid."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise V5ExtensionError("extension value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(value: Any, name: str) -> str:
    parsed = str(value)
    if len(parsed) != 64 or any(c not in "0123456789abcdef" for c in parsed):
        raise V5ExtensionError(f"{name} must be a lowercase SHA-256")
    return parsed


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise V5ExtensionError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise V5ExtensionError(f"{name} must be finite") from exc
    if not math.isfinite(parsed):
        raise V5ExtensionError(f"{name} must be finite")
    return parsed


def _features(candidate: CompleteCandidateV1, row: Mapping[str, Any]) -> tuple[float, ...]:
    supplied = row.get("features")
    if supplied is not None:
        if isinstance(supplied, (str, bytes)) or not isinstance(supplied, Sequence) or len(supplied) != 6:
            raise V5ExtensionError("active-set observation features must have six values")
        values = tuple(_finite(value, "active-set feature") for value in supplied)
    else:
        block = candidate.controller_path
        p_gain = _finite(block["force_p_gain"], "force_p_gain")
        damping = _finite(block["force_damping"], "force_damping")
        if p_gain <= 0.0 or damping <= 0.0:
            raise V5ExtensionError("controller features must be positive")
        values = (
            math.log2(p_gain / damping),
            math.log2(damping),
            math.log2(_finite(block["normal_filter_tau_s"], "normal_filter_tau_s")),
            math.log2(_finite(block["orientation_ko"], "orientation_ko")),
            math.log2(_finite(block["motion_kp"], "motion_kp")),
            math.log2(_finite(block["force_i_gain"], "force_i_gain") / p_gain),
        )
    if any(not math.isfinite(value) for value in values):
        raise V5ExtensionError("active-set feature is non-finite")
    return values


def _observation(row: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise V5ExtensionError("active-set observation is not a mapping")
    candidate = CompleteCandidateV1.from_mapping(row.get("candidate"))
    candidate_key = str(row.get("candidate_key", candidate.candidate_key))
    if candidate_key != candidate.candidate_key:
        raise V5ExtensionError("active-set candidate key differs from candidate")
    mae = row.get("mean_n", row.get("mae_n"))
    mae_n = _finite(mae, "active-set MAE")
    if mae_n < 0.0:
        raise V5ExtensionError("active-set MAE is negative")
    ordinal_raw = row.get("ordinal", row.get("budget_ordinal", index + 1))
    if type(ordinal_raw) is not int or ordinal_raw <= 0:
        raise V5ExtensionError("active-set ordinal must be a positive integer")
    observation_id = str(row.get("observation_id", f"{candidate_key}:{ordinal_raw}"))
    if not observation_id:
        raise V5ExtensionError("active-set observation id is empty")
    features = _features(candidate, row)
    return {
        "observation_id": observation_id,
        "candidate_key": candidate_key,
        "candidate": candidate.as_dict(),
        "mean_n": mae_n,
        "ordinal": ordinal_raw,
        "features": list(features),
    }


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    return math.sqrt(math.fsum((float(a) - float(b)) ** 2 for a, b in zip(left, right, strict=True)))


def build_active_set(
    observations: Sequence[Mapping[str, Any]],
    *,
    parent_fingerprint_sha256: str,
    parent_ledger_head_sha256: str,
    max_size: int = ACTIVE_SET_MAX,
) -> dict[str, Any]:
    """Select the versioned active set with deterministic tie breaking."""

    parent_fp = _sha(parent_fingerprint_sha256, "parent fingerprint")
    parent_head = _sha(parent_ledger_head_sha256, "parent ledger head")
    if type(max_size) is not int or not 1 <= max_size <= ACTIVE_SET_MAX:
        raise V5ExtensionError("active-set size must be within the 512-row bound")
    rows = tuple(_observation(row, index) for index, row in enumerate(observations))
    if not rows:
        raise V5ExtensionError("active-set cannot be built from empty observations")
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        prior = by_id.get(row["observation_id"])
        if prior is not None and prior != row:
            raise V5ExtensionError("active-set observation id is not immutable")
        by_id[row["observation_id"]] = row
    rows = tuple(by_id.values())
    mae_order = tuple(sorted(rows, key=lambda row: (row["mean_n"], row["observation_id"])))
    recent_order = tuple(sorted(rows, key=lambda row: (-row["ordinal"], row["observation_id"])))
    selected: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = {}

    def add(items: Sequence[Mapping[str, Any]], label: str) -> None:
        for item in items:
            key = str(item["observation_id"])
            if key not in selected and len(selected) < max_size:
                selected[key] = dict(item)
            sources.setdefault(key, set()).add(label)

    add(mae_order[:HISTORICAL_CHAMPION_MAX], "historical_champion")
    add(mae_order[:LOWEST_MAE_MAX], "lowest_mae")
    add(recent_order[:RECENT_OBSERVATION_MAX], "recent")

    # Fill the remaining slots by farthest-first coverage.  The first point
    # is fixed by (MAE, id), and every later tie is resolved by observation id.
    remaining = {str(row["observation_id"]): row for row in rows if str(row["observation_id"]) not in selected}
    while remaining and len(selected) < max_size:
        if not selected:
            chosen = min(remaining.values(), key=lambda row: (row["mean_n"], row["observation_id"]))
        else:
            selected_features = tuple(row["features"] for row in selected.values())
            ranked = []
            for row in remaining.values():
                min_distance = min(_distance(row["features"], other) for other in selected_features)
                ranked.append((min_distance, -row["ordinal"], -row["mean_n"], row["observation_id"], row))
            chosen = max(ranked, key=lambda item: item[:4])[-1]
        key = str(chosen["observation_id"])
        selected[key] = dict(chosen)
        sources.setdefault(key, set()).add("maximin_coverage")
        remaining.pop(key, None)

    selected_rows = [
        {**selected[key], "selection_sources": sorted(sources.get(key, {"maximin_coverage"}))}
        for key in sorted(selected)
    ]
    body = {
        "schema": ACTIVE_SET_SCHEMA,
        "version": ACTIVE_SET_VERSION,
        "policy_schema": EXTENSION_POLICY_SCHEMA,
        "policy_version": EXTENSION_POLICY_VERSION,
        "parent_fingerprint_sha256": parent_fp,
        "parent_ledger_head_sha256": parent_head,
        "max_size": max_size,
        "selection_order": [
            "historical_champions_by_mean_mae",
            "lowest_mae_observations",
            "recent_observations",
            "deterministic_farthest_first_maximin",
        ],
        "selection_caps": {
            "historical_champions": HISTORICAL_CHAMPION_MAX,
            "lowest_mae": LOWEST_MAE_MAX,
            "recent": RECENT_OBSERVATION_MAX,
        },
        "source_observation_count": len(rows),
        "selected_observation_count": len(selected_rows),
        "selected_observation_ids": [row["observation_id"] for row in selected_rows],
        "observations": selected_rows,
    }
    return {**body, "active_set_sha256": canonical_sha256(body)}


def verify_active_set(receipt: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise V5ExtensionError("active-set receipt is not a mapping")
    required = {
        "schema", "version", "policy_schema", "policy_version", "parent_fingerprint_sha256",
        "parent_ledger_head_sha256", "max_size", "selection_order", "selection_caps",
        "source_observation_count", "selected_observation_count", "selected_observation_ids",
        "observations", "active_set_sha256",
    }
    if set(receipt) != required or receipt.get("schema") != ACTIVE_SET_SCHEMA or receipt.get("version") != ACTIVE_SET_VERSION:
        raise V5ExtensionError("active-set receipt schema differs")
    expected = canonical_sha256({key: receipt[key] for key in required if key != "active_set_sha256"})
    if receipt["active_set_sha256"] != expected:
        raise V5ExtensionError("active-set receipt hash differs")
    _sha(receipt["parent_fingerprint_sha256"], "active-set parent fingerprint")
    _sha(receipt["parent_ledger_head_sha256"], "active-set parent ledger head")
    observations = receipt["observations"]
    ids = receipt["selected_observation_ids"]
    if not isinstance(observations, Sequence) or isinstance(observations, (str, bytes)) or not isinstance(ids, Sequence) or tuple(ids) != tuple(row.get("observation_id") for row in observations):
        raise V5ExtensionError("active-set observation ordering differs")
    if receipt["selected_observation_count"] != len(observations) or len(observations) > int(receipt["max_size"]) or len(observations) > ACTIVE_SET_MAX:
        raise V5ExtensionError("active-set observation count differs")
    for index, row in enumerate(observations):
        parsed = _observation(row, index)
        if parsed["observation_id"] != row.get("observation_id"):
            raise V5ExtensionError("active-set observation is not canonical")
    return dict(receipt)


@dataclass(frozen=True)
class V5ExtensionEpochReceiptV1:
    epoch: int
    parent_epoch: int
    parent_campaign_fingerprint_sha256: str
    parent_ledger_head_sha256: str
    campaign_fingerprint_sha256: str
    physical_ledger_head_sha256: str
    active_set_sha256: str
    exact_novel_count: int
    top3_total_n: int
    status: str = "CLOSED"
    schema: str = EPOCH_RECEIPT_SCHEMA
    version: int = EPOCH_RECEIPT_VERSION

    def __post_init__(self) -> None:
        if self.schema != EPOCH_RECEIPT_SCHEMA or self.version != EPOCH_RECEIPT_VERSION:
            raise V5ExtensionError("extension epoch receipt schema differs")
        for value, name in ((self.epoch, "epoch"), (self.parent_epoch, "parent epoch")):
            if type(value) is not int or value < 0:
                raise V5ExtensionError(f"{name} is invalid")
        if self.epoch != self.parent_epoch + 1:
            raise V5ExtensionError("extension epoch is not the immediate successor")
        for value, name in (
            (self.parent_campaign_fingerprint_sha256, "parent campaign fingerprint"),
            (self.parent_ledger_head_sha256, "parent ledger head"),
            (self.campaign_fingerprint_sha256, "campaign fingerprint"),
            (self.physical_ledger_head_sha256, "physical ledger head"),
            (self.active_set_sha256, "active-set hash"),
        ):
            _sha(value, name)
        if self.exact_novel_count != EXTENSION_EXACT_TARGET or self.top3_total_n != 5:
            raise V5ExtensionError("extension closeout counts differ")
        if self.status not in {"CLOSED", "PAUSED_STORAGE", "PAUSED_USER", "PAUSED_FAULT"}:
            raise V5ExtensionError("extension status is unknown")

    def as_dict(self) -> dict[str, Any]:
        body = {
            "schema": self.schema,
            "version": self.version,
            "epoch": self.epoch,
            "parent_epoch": self.parent_epoch,
            "parent_campaign_fingerprint_sha256": self.parent_campaign_fingerprint_sha256,
            "parent_ledger_head_sha256": self.parent_ledger_head_sha256,
            "campaign_fingerprint_sha256": self.campaign_fingerprint_sha256,
            "physical_ledger_head_sha256": self.physical_ledger_head_sha256,
            "active_set_sha256": self.active_set_sha256,
            "exact_novel_count": self.exact_novel_count,
            "top3_total_n": self.top3_total_n,
            "status": self.status,
        }
        return {**body, "receipt_sha256": canonical_sha256(body)}


def verify_epoch_receipt(receipt: Mapping[str, Any]) -> V5ExtensionEpochReceiptV1:
    if not isinstance(receipt, Mapping):
        raise V5ExtensionError("epoch receipt is not a mapping")
    value = dict(receipt)
    receipt_sha = value.pop("receipt_sha256", None)
    if receipt_sha != canonical_sha256(value):
        raise V5ExtensionError("epoch receipt hash differs")
    try:
        return V5ExtensionEpochReceiptV1(**value)
    except TypeError as exc:
        raise V5ExtensionError("epoch receipt fields differ") from exc


def derive_extension_fingerprint(
    *,
    release_identity_sha256: str,
    parent_campaign_fingerprint_sha256: str,
    parent_ledger_head_sha256: str,
    epoch: int,
    entry_mode: str = "HOME_ONLY_V1",
) -> str:
    """Derive an epoch-isolated Home-only campaign fingerprint."""

    _sha(release_identity_sha256, "release identity")
    _sha(parent_campaign_fingerprint_sha256, "parent campaign fingerprint")
    _sha(parent_ledger_head_sha256, "parent ledger head")
    if type(epoch) is not int or epoch < 1:
        raise V5ExtensionError("extension epoch must be positive")
    if entry_mode != "HOME_ONLY_V1":
        raise V5ExtensionError("extensions require Home-only entry")
    return canonical_sha256(
        {
            "schema": EPOCH_RECEIPT_SCHEMA,
            "version": EPOCH_RECEIPT_VERSION,
            "release_identity_sha256": release_identity_sha256,
            "parent_campaign_fingerprint_sha256": parent_campaign_fingerprint_sha256,
            "parent_ledger_head_sha256": parent_ledger_head_sha256,
            "epoch": epoch,
            "entry_mode": entry_mode,
            "correction_weights": [0.0] * 6,
        }
    )


__all__ = [
    "ACTIVE_SET_MAX",
    "ACTIVE_SET_SCHEMA",
    "EXTENSION_EXACT_TARGET",
    "V5ExtensionEpochReceiptV1",
    "V5ExtensionError",
    "build_active_set",
    "derive_extension_fingerprint",
    "verify_active_set",
    "verify_epoch_receipt",
]
