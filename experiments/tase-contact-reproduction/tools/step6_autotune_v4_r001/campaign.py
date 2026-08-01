"""Offline-only typed campaign contracts for Step6 Figure-Eight r001.

This module deliberately stops at immutable campaign preparation and state
transitions.  It contains no optimizer, GP, runtime, package, controller, or
hardware integration.  The only force evidence accepted here is the already
validated Step6 ``ForceMetrics`` result bound to the locked semantic spec.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Final

from .contracts import (
    FORCE_MAE_V2_SPEC_FINGERPRINT,
    LINEAGE,
    PROFILE,
    REVISION,
    STEP6_R001_PROFILE,
    AutotuneTaskProfile,
)
from .errors import (
    CampaignContractError,
    CampaignIdentityError,
    CampaignPausedError,
    CampaignResumeError,
    CampaignStateError,
    DomainViolationError,
    SeedReceiptError,
)
from .force import ForceMetrics, ForceSample, MetricRole, TimeWindow, compute_force_metrics


SCHEMA_VERSION: Final[int] = 1
COORDINATE_NAMES: Final[tuple[str, ...]] = (
    "P",
    "D",
    "tau",
    "I_on_log2",
    "I_off",
    "Ko",
    "Kp",
)
SEED_RECEIPT_SCHEMA: Final[str] = "step6.autotune_v4.r001.incumbent_seed_receipt"
CANDIDATE_SCHEMA: Final[str] = "step6.autotune_v4.r001.named_7d_candidate"
DOMAIN_SCHEMA: Final[str] = "step6.autotune_v4.r001.named_7d_domain"
BOOTSTRAP_PLAN_SCHEMA: Final[str] = "step6.autotune_v4.r001.bootstrap_pd_plan"
GENESIS_SCHEMA: Final[str] = "step6.autotune_v4.r001.campaign_genesis"
ATTEMPT_SCHEMA: Final[str] = "step6.autotune_v4.r001.attempt_assessment"
PAUSE_CONTEXT_SCHEMA: Final[str] = "step6.autotune_v4.r001.anchor_audit_pause_context"
AUDIT_RECEIPT_SCHEMA: Final[str] = "step6.autotune_v4.r001.anchor_audit_receipt"
STATE_SCHEMA: Final[str] = "step6.autotune_v4.r001.campaign_state"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    """Encode a mapping with explicit, locale-independent JSON rules.

    ``allow_nan=False`` is intentional: a non-finite value is never allowed
    to become a content-addressed campaign binding.
    """

    if not isinstance(payload, Mapping):
        raise CampaignContractError("canonical JSON payload must be a mapping")
    try:
        return json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CampaignContractError("payload cannot be encoded as canonical JSON") from exc


def sha256_canonical(payload: Mapping[str, object]) -> str:
    """Return a lowercase SHA-256 digest of canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _require_schema(mapping: Mapping[str, object], expected: str, error_type: type[Exception]) -> None:
    if mapping.get("schema") != expected:
        raise error_type(f"schema must be {expected!r}")
    version = mapping.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        raise error_type(f"schema_version must be {SCHEMA_VERSION}")


def _require_exact_keys(
    mapping: Mapping[str, object],
    expected: set[str],
    error_type: type[Exception],
) -> None:
    actual = set(mapping.keys())
    if actual != expected or any(not isinstance(key, str) for key in mapping.keys()):
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise error_type(f"mapping keys are not exact; missing={missing!r}, extra={extra!r}")


def _require_sha(value: object, label: str, error_type: type[Exception] = CampaignContractError) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise error_type(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def _require_nonempty_text(
    value: object,
    label: str,
    error_type: type[Exception] = CampaignContractError,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{label} must be non-empty text")
    return value


def _finite(value: object, label: str, error_type: type[Exception] = CampaignContractError) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error_type(f"{label} must be a finite real number")
    result = float(value)
    if not math.isfinite(result):
        raise error_type(f"{label} must be finite")
    return result


def _mapping_with_digest(content: Mapping[str, object], digest_name: str, digest: str) -> dict[str, object]:
    result = dict(content)
    result[digest_name] = digest
    return result


@dataclass(frozen=True, slots=True)
class Named7dCandidate:
    """The only candidate shape accepted by the offline Step6 campaign."""

    P: float
    D: float
    tau: float
    I_on_log2: float
    I_off: int
    Ko: float
    Kp: float
    schema: str = CANDIDATE_SCHEMA
    schema_version: int = SCHEMA_VERSION
    candidate_uid: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("P", "D", "tau", "I_on_log2", "Ko", "Kp"):
            object.__setattr__(self, name, _finite(getattr(self, name), f"candidate.{name}"))
        if isinstance(self.I_off, bool) or not isinstance(self.I_off, int) or self.I_off not in (0, 1):
            raise CampaignContractError("candidate.I_off must be categorical integer 0 or 1")
        if self.I_off == 1 and (
            self.I_on_log2 != 0.0 or math.copysign(1.0, self.I_on_log2) < 0.0
        ):
            raise CampaignContractError("candidate.I_on_log2 must be canonical positive zero when I_off=1")
        _require_schema(self.to_content_mapping(), CANDIDATE_SCHEMA, CampaignContractError)
        object.__setattr__(self, "candidate_uid", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "P": self.P,
            "D": self.D,
            "tau": self.tau,
            "I_on_log2": self.I_on_log2,
            "I_off": self.I_off,
            "Ko": self.Ko,
            "Kp": self.Kp,
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "candidate_uid", self.candidate_uid)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> Named7dCandidate:
        error_type = CampaignContractError
        if not isinstance(mapping, Mapping):
            raise error_type("named candidate must be decoded from a mapping")
        _require_exact_keys(
            mapping,
            {"schema", "schema_version", *COORDINATE_NAMES, "candidate_uid"},
            error_type,
        )
        _require_schema(mapping, CANDIDATE_SCHEMA, error_type)
        candidate = cls(
            P=mapping["P"],
            D=mapping["D"],
            tau=mapping["tau"],
            I_on_log2=mapping["I_on_log2"],
            I_off=mapping["I_off"],
            Ko=mapping["Ko"],
            Kp=mapping["Kp"],
        )
        if mapping["candidate_uid"] != candidate.candidate_uid:
            raise error_type("candidate_uid does not match the canonical candidate content")
        return candidate

    @property
    def coordinates(self) -> tuple[float, float, float, float, int, float, float]:
        return (self.P, self.D, self.tau, self.I_on_log2, self.I_off, self.Ko, self.Kp)

    @property
    def named_coordinates(self) -> dict[str, float | int]:
        return dict(zip(COORDINATE_NAMES, self.coordinates, strict=True))

    @property
    def uid(self) -> str:
        return self.candidate_uid


@dataclass(frozen=True, slots=True)
class IncumbentSeedReceipt:
    """A minimal Step5d handoff receipt with no optimizer state."""

    candidate: Named7dCandidate
    source_candidate_uid: str
    source_identity: str
    source_closure_sha256: str
    handoff_sha256: str
    schema: str = SEED_RECEIPT_SCHEMA
    schema_version: int = SCHEMA_VERSION
    receipt_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Named7dCandidate):
            raise SeedReceiptError("seed receipt candidate must be a Named7dCandidate")
        _require_sha(self.source_candidate_uid, "source_candidate_uid", SeedReceiptError)
        _require_nonempty_text(self.source_identity, "source_identity", SeedReceiptError)
        _require_sha(self.source_closure_sha256, "source_closure_sha256", SeedReceiptError)
        _require_sha(self.handoff_sha256, "handoff_sha256", SeedReceiptError)
        _require_schema(self.to_content_mapping(), SEED_RECEIPT_SCHEMA, SeedReceiptError)
        object.__setattr__(self, "receipt_digest", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "candidate": self.candidate.to_mapping(),
            "source_candidate_uid": self.source_candidate_uid,
            "source_identity": self.source_identity,
            "source_closure_sha256": self.source_closure_sha256,
            "handoff_sha256": self.handoff_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "receipt_digest", self.receipt_digest)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> IncumbentSeedReceipt:
        if not isinstance(mapping, Mapping):
            raise SeedReceiptError("seed receipt must be decoded from a mapping")
        _require_exact_keys(
            mapping,
            {
                "schema",
                "schema_version",
                "candidate",
                "source_candidate_uid",
                "source_identity",
                "source_closure_sha256",
                "handoff_sha256",
                "receipt_digest",
            },
            SeedReceiptError,
        )
        _require_schema(mapping, SEED_RECEIPT_SCHEMA, SeedReceiptError)
        try:
            candidate = Named7dCandidate.from_mapping(mapping["candidate"])
        except CampaignContractError as exc:
            raise SeedReceiptError(str(exc)) from exc
        receipt = cls(
            candidate=candidate,
            source_candidate_uid=mapping["source_candidate_uid"],
            source_identity=mapping["source_identity"],
            source_closure_sha256=mapping["source_closure_sha256"],
            handoff_sha256=mapping["handoff_sha256"],
        )
        if mapping["receipt_digest"] != receipt.receipt_digest:
            raise SeedReceiptError("receipt_digest does not match the canonical handoff content")
        return receipt

    @property
    def candidate_uid(self) -> str:
        return self.candidate.candidate_uid

    @property
    def digest(self) -> str:
        return self.receipt_digest

    @property
    def source_sha256(self) -> str:
        return self.source_closure_sha256

    @property
    def handoff_digest(self) -> str:
        return self.handoff_sha256

    @property
    def coordinates(self) -> tuple[float, float, float, float, int, float, float]:
        return self.candidate.coordinates

    @property
    def P(self) -> float:
        return self.candidate.P

    @property
    def D(self) -> float:
        return self.candidate.D

    @property
    def tau(self) -> float:
        return self.candidate.tau

    @property
    def I_on_log2(self) -> float:
        return self.candidate.I_on_log2

    @property
    def I_off(self) -> int:
        return self.candidate.I_off

    @property
    def Ko(self) -> float:
        return self.candidate.Ko

    @property
    def Kp(self) -> float:
        return self.candidate.Kp


@dataclass(frozen=True, slots=True)
class Named7dDomain:
    """Injected named-coordinate domain; no parent Step5d code is imported."""

    P: tuple[float, ...]
    D: tuple[float, ...]
    tau: tuple[float, ...]
    I_on_log2: tuple[float, ...]
    I_off: tuple[int, ...]
    Ko: tuple[float, ...]
    Kp: tuple[float, ...]
    source_identity: str = "injected_named_7d_domain"
    source_domain_sha256: str | None = None
    schema: str = DOMAIN_SCHEMA
    schema_version: int = SCHEMA_VERSION
    semantic_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_text(self.source_identity, "domain.source_identity")
        if self.source_domain_sha256 is not None:
            _require_sha(self.source_domain_sha256, "source_domain_sha256")
        normalized: dict[str, tuple[float, ...] | tuple[int, ...]] = {}
        for name in COORDINATE_NAMES:
            raw_values = getattr(self, name)
            if not isinstance(raw_values, (tuple, list)):
                raise CampaignContractError(f"domain.{name} must be an immutable tuple of allowed values")
            if not raw_values:
                raise CampaignContractError(f"domain.{name} must contain at least one allowed value")
            if name == "I_off":
                values: tuple[int, ...] = tuple(raw_values)  # type: ignore[assignment]
                if any(isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1) for value in values):
                    raise CampaignContractError("domain.I_off values must be categorical integers 0 or 1")
            else:
                values = tuple(_finite(value, f"domain.{name}[{index}]") for index, value in enumerate(raw_values))
            if len(set(values)) != len(values):
                raise CampaignContractError(f"domain.{name} contains duplicate values")
            if tuple(sorted(values)) != values:
                raise CampaignContractError(f"domain.{name} must be strictly ascending")
            normalized[name] = values
        for name, values in normalized.items():
            object.__setattr__(self, name, values)
        _require_schema(self.to_content_mapping(), DOMAIN_SCHEMA, CampaignContractError)
        object.__setattr__(self, "semantic_fingerprint", sha256_canonical(self.to_content_mapping()))

    @classmethod
    def from_allowed_values(
        cls,
        *,
        P: tuple[float, ...] | list[float],
        D: tuple[float, ...] | list[float],
        tau: tuple[float, ...] | list[float],
        I_on_log2: tuple[float, ...] | list[float],
        I_off: tuple[int, ...] | list[int],
        Ko: tuple[float, ...] | list[float],
        Kp: tuple[float, ...] | list[float],
        source_identity: str = "injected_named_7d_domain",
        source_domain_sha256: str | None = None,
    ) -> Named7dDomain:
        return cls(
            P=P,
            D=D,
            tau=tau,
            I_on_log2=I_on_log2,
            I_off=I_off,
            Ko=Ko,
            Kp=Kp,
            source_identity=source_identity,
            source_domain_sha256=source_domain_sha256,
        )

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "coordinate_names": COORDINATE_NAMES,
            "P": self.P,
            "D": self.D,
            "tau": self.tau,
            "I_on_log2": self.I_on_log2,
            "I_off": self.I_off,
            "Ko": self.Ko,
            "Kp": self.Kp,
        }

    def to_mapping(self) -> dict[str, object]:
        result = self.to_content_mapping()
        result["source_identity"] = self.source_identity
        result["source_domain_sha256"] = self.source_domain_sha256
        result["semantic_fingerprint"] = self.semantic_fingerprint
        return result

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> Named7dDomain:
        if not isinstance(mapping, Mapping):
            raise CampaignContractError("named domain must be decoded from a mapping")
        expected = {
            "schema",
            "schema_version",
            "coordinate_names",
            *COORDINATE_NAMES,
            "source_identity",
            "source_domain_sha256",
            "semantic_fingerprint",
        }
        _require_exact_keys(mapping, expected, CampaignContractError)
        _require_schema(mapping, DOMAIN_SCHEMA, CampaignContractError)
        if tuple(mapping["coordinate_names"]) != COORDINATE_NAMES:
            raise CampaignContractError("domain coordinate names are not the locked named-7D order")
        domain = cls(
            P=tuple(mapping["P"]),  # type: ignore[arg-type]
            D=tuple(mapping["D"]),  # type: ignore[arg-type]
            tau=tuple(mapping["tau"]),  # type: ignore[arg-type]
            I_on_log2=tuple(mapping["I_on_log2"]),  # type: ignore[arg-type]
            I_off=tuple(mapping["I_off"]),  # type: ignore[arg-type]
            Ko=tuple(mapping["Ko"]),  # type: ignore[arg-type]
            Kp=tuple(mapping["Kp"]),  # type: ignore[arg-type]
            source_identity=mapping["source_identity"],  # type: ignore[arg-type]
            source_domain_sha256=mapping["source_domain_sha256"],  # type: ignore[arg-type]
        )
        if mapping["semantic_fingerprint"] != domain.semantic_fingerprint:
            raise CampaignContractError("domain semantic_fingerprint does not match allowed values")
        return domain

    @property
    def domain_fingerprint(self) -> str:
        return self.semantic_fingerprint

    @property
    def allowed_values(self) -> dict[str, tuple[float, ...] | tuple[int, ...]]:
        return {name: getattr(self, name) for name in COORDINATE_NAMES}

    def validate_candidate(self, candidate: Named7dCandidate) -> None:
        if not isinstance(candidate, Named7dCandidate):
            raise DomainViolationError("domain validation requires a Named7dCandidate")
        for name in COORDINATE_NAMES:
            value = getattr(candidate, name)
            if value not in getattr(self, name):
                raise DomainViolationError(
                    f"candidate coordinate {name}={value!r} is outside the injected named domain"
                )

    def contains(self, candidate: Named7dCandidate) -> bool:
        try:
            self.validate_candidate(candidate)
        except DomainViolationError:
            return False
        return True


class BootstrapSlot(str, Enum):
    ANCHOR = "anchor"
    P_MINUS_QUARTER_OCTAVE = "P_minus_0.25_octave"
    P_PLUS_QUARTER_OCTAVE = "P_plus_0.25_octave"
    D_MINUS_QUARTER_OCTAVE = "D_minus_0.25_octave"
    D_PLUS_QUARTER_OCTAVE = "D_plus_0.25_octave"


BOOTSTRAP_SLOT_ORDER: Final[tuple[BootstrapSlot, ...]] = (
    BootstrapSlot.ANCHOR,
    BootstrapSlot.ANCHOR,
    BootstrapSlot.ANCHOR,
    BootstrapSlot.P_MINUS_QUARTER_OCTAVE,
    BootstrapSlot.ANCHOR,
    BootstrapSlot.P_PLUS_QUARTER_OCTAVE,
    BootstrapSlot.ANCHOR,
    BootstrapSlot.D_MINUS_QUARTER_OCTAVE,
    BootstrapSlot.ANCHOR,
    BootstrapSlot.D_PLUS_QUARTER_OCTAVE,
)


@dataclass(frozen=True, slots=True)
class BootstrapRow:
    index: int
    slot: BootstrapSlot
    candidate: Named7dCandidate
    replaced_by_anchor: bool = False
    replacement_reason: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int) or self.index < 0:
            raise CampaignContractError("bootstrap row index must be a non-negative integer")
        if not isinstance(self.slot, BootstrapSlot):
            raise CampaignContractError("bootstrap row slot must be a BootstrapSlot")
        if not isinstance(self.candidate, Named7dCandidate):
            raise CampaignContractError("bootstrap row candidate must be a Named7dCandidate")
        if not isinstance(self.replaced_by_anchor, bool):
            raise CampaignContractError("replaced_by_anchor must be boolean")
        if self.replaced_by_anchor and self.replacement_reason != "outside_injected_domain":
            raise CampaignContractError("anchor replacement must record the deterministic reason")
        if not self.replaced_by_anchor and self.replacement_reason is not None:
            raise CampaignContractError("non-replaced bootstrap row cannot carry a replacement reason")

    @property
    def attempt_number(self) -> int:
        return self.index + 1


@dataclass(frozen=True, slots=True)
class BootstrapPlan:
    anchor: Named7dCandidate
    rows: tuple[BootstrapRow, ...]
    domain_fingerprint: str
    seed_receipt_digest: str
    schema: str = BOOTSTRAP_PLAN_SCHEMA
    schema_version: int = SCHEMA_VERSION
    plan_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, Named7dCandidate):
            raise CampaignContractError("bootstrap anchor must be a Named7dCandidate")
        _require_sha(self.domain_fingerprint, "domain_fingerprint")
        _require_sha(self.seed_receipt_digest, "seed_receipt_digest")
        rows = tuple(self.rows)
        if len(rows) != len(BOOTSTRAP_SLOT_ORDER):
            raise CampaignContractError("Step6 bootstrap plan must contain exactly ten rows")
        for index, (row, expected_slot) in enumerate(zip(rows, BOOTSTRAP_SLOT_ORDER, strict=True)):
            if not isinstance(row, BootstrapRow) or row.index != index or row.slot is not expected_slot:
                raise CampaignContractError("bootstrap rows do not match the locked ten-row order")
            if row.slot is BootstrapSlot.ANCHOR:
                if row.candidate != self.anchor:
                    raise CampaignContractError("anchor row must contain the exact anchor replicate")
                if row.replaced_by_anchor:
                    raise CampaignContractError("anchor row cannot be marked as a probe replacement")
            elif row.replaced_by_anchor and row.candidate != self.anchor:
                raise CampaignContractError("replaced probe row must contain the exact anchor")
        object.__setattr__(self, "rows", rows)
        _require_schema(self.to_content_mapping(), BOOTSTRAP_PLAN_SCHEMA, CampaignContractError)
        object.__setattr__(self, "plan_fingerprint", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "anchor_uid": self.anchor.candidate_uid,
            "domain_fingerprint": self.domain_fingerprint,
            "seed_receipt_digest": self.seed_receipt_digest,
            "rows": tuple(
                {
                    "index": row.index,
                    "slot": row.slot.value,
                    "candidate_uid": row.candidate.candidate_uid,
                    "replaced_by_anchor": row.replaced_by_anchor,
                    "replacement_reason": row.replacement_reason,
                }
                for row in self.rows
            ),
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "plan_fingerprint", self.plan_fingerprint)


def _probe_candidate(anchor: Named7dCandidate, slot: BootstrapSlot) -> Named7dCandidate:
    if slot is BootstrapSlot.P_MINUS_QUARTER_OCTAVE:
        return replace(anchor, P=anchor.P - 0.25)
    if slot is BootstrapSlot.P_PLUS_QUARTER_OCTAVE:
        return replace(anchor, P=anchor.P + 0.25)
    if slot is BootstrapSlot.D_MINUS_QUARTER_OCTAVE:
        return replace(anchor, D=anchor.D - 0.25)
    if slot is BootstrapSlot.D_PLUS_QUARTER_OCTAVE:
        return replace(anchor, D=anchor.D + 0.25)
    return anchor


def plan_bootstrap_pd(seed_receipt: IncumbentSeedReceipt, domain: Named7dDomain) -> BootstrapPlan:
    """Create exactly the locked ten-row named-coordinate bootstrap plan."""

    if not isinstance(seed_receipt, IncumbentSeedReceipt):
        raise CampaignContractError("bootstrap planning requires an IncumbentSeedReceipt")
    if not isinstance(domain, Named7dDomain):
        raise CampaignContractError("bootstrap planning requires an injected Named7dDomain")
    domain.validate_candidate(seed_receipt.candidate)
    rows: list[BootstrapRow] = []
    for index, slot in enumerate(BOOTSTRAP_SLOT_ORDER):
        if slot is BootstrapSlot.ANCHOR:
            rows.append(BootstrapRow(index=index, slot=slot, candidate=seed_receipt.candidate))
            continue
        probe = _probe_candidate(seed_receipt.candidate, slot)
        if domain.contains(probe):
            rows.append(BootstrapRow(index=index, slot=slot, candidate=probe))
        else:
            rows.append(
                BootstrapRow(
                    index=index,
                    slot=slot,
                    candidate=seed_receipt.candidate,
                    replaced_by_anchor=True,
                    replacement_reason="outside_injected_domain",
                )
            )
    return BootstrapPlan(
        anchor=seed_receipt.candidate,
        rows=tuple(rows),
        domain_fingerprint=domain.semantic_fingerprint,
        seed_receipt_digest=seed_receipt.receipt_digest,
    )


@dataclass(frozen=True, slots=True)
class CampaignGenesis:
    """Fresh Step6 campaign identity; old optimizer state is not importable."""

    campaign_id: str
    epoch: int
    source_closure_sha256: str
    seed_receipt_digest: str
    domain_fingerprint: str
    force_mae_spec_fingerprint: str
    lineage: str = LINEAGE
    profile: str = PROFILE
    revision: str = REVISION
    schema: str = GENESIS_SCHEMA
    schema_version: int = SCHEMA_VERSION
    campaign_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_nonempty_text(self.campaign_id, "campaign_id", CampaignIdentityError)
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch <= 0:
            raise CampaignIdentityError("campaign epoch must be a positive integer")
        for name in (
            "source_closure_sha256",
            "seed_receipt_digest",
            "domain_fingerprint",
            "force_mae_spec_fingerprint",
        ):
            _require_sha(getattr(self, name), name, CampaignIdentityError)
        if self.lineage != LINEAGE or self.profile != PROFILE or self.revision != REVISION:
            raise CampaignIdentityError("campaign genesis identity is not the locked Step6 r001 identity")
        if self.force_mae_spec_fingerprint != FORCE_MAE_V2_SPEC_FINGERPRINT:
            raise CampaignIdentityError("campaign genesis is not bound to the locked Step6 force semantic fingerprint")
        _require_schema(self.to_content_mapping(), GENESIS_SCHEMA, CampaignIdentityError)
        object.__setattr__(self, "campaign_fingerprint", sha256_canonical(self.to_content_mapping()))

    @classmethod
    def create(
        cls,
        *,
        campaign_id: str,
        epoch: int,
        source_closure_sha256: str,
        seed_receipt: IncumbentSeedReceipt,
        domain: Named7dDomain,
        profile: AutotuneTaskProfile = STEP6_R001_PROFILE,
    ) -> CampaignGenesis:
        if profile is not STEP6_R001_PROFILE:
            raise CampaignIdentityError("campaign genesis accepts only the locked Step6 r001 profile")
        if not isinstance(seed_receipt, IncumbentSeedReceipt) or not isinstance(domain, Named7dDomain):
            raise CampaignIdentityError("campaign genesis requires typed seed receipt and named domain")
        domain.validate_candidate(seed_receipt.candidate)
        return cls(
            campaign_id=campaign_id,
            epoch=epoch,
            source_closure_sha256=source_closure_sha256,
            seed_receipt_digest=seed_receipt.receipt_digest,
            domain_fingerprint=domain.semantic_fingerprint,
            force_mae_spec_fingerprint=profile.force_mae_spec.semantic_fingerprint,
        )

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "epoch": self.epoch,
            "lineage": self.lineage,
            "profile": self.profile,
            "revision": self.revision,
            "source_closure_sha256": self.source_closure_sha256,
            "seed_receipt_digest": self.seed_receipt_digest,
            "domain_fingerprint": self.domain_fingerprint,
            "force_mae_spec_fingerprint": self.force_mae_spec_fingerprint,
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "campaign_fingerprint", self.campaign_fingerprint)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> CampaignGenesis:
        if not isinstance(mapping, Mapping):
            raise CampaignIdentityError("campaign genesis must be decoded from a mapping")
        _require_exact_keys(
            mapping,
            {
                "schema",
                "schema_version",
                "campaign_id",
                "epoch",
                "lineage",
                "profile",
                "revision",
                "source_closure_sha256",
                "seed_receipt_digest",
                "domain_fingerprint",
                "force_mae_spec_fingerprint",
                "campaign_fingerprint",
            },
            CampaignIdentityError,
        )
        _require_schema(mapping, GENESIS_SCHEMA, CampaignIdentityError)
        genesis = cls(
            campaign_id=mapping["campaign_id"],  # type: ignore[arg-type]
            epoch=mapping["epoch"],  # type: ignore[arg-type]
            source_closure_sha256=mapping["source_closure_sha256"],  # type: ignore[arg-type]
            seed_receipt_digest=mapping["seed_receipt_digest"],  # type: ignore[arg-type]
            domain_fingerprint=mapping["domain_fingerprint"],  # type: ignore[arg-type]
            force_mae_spec_fingerprint=mapping["force_mae_spec_fingerprint"],  # type: ignore[arg-type]
            lineage=mapping["lineage"],  # type: ignore[arg-type]
            profile=mapping["profile"],  # type: ignore[arg-type]
            revision=mapping["revision"],  # type: ignore[arg-type]
        )
        if mapping["campaign_fingerprint"] != genesis.campaign_fingerprint:
            raise CampaignIdentityError("campaign_fingerprint does not match campaign genesis content")
        return genesis

    @property
    def fingerprint(self) -> str:
        return self.campaign_fingerprint

    @property
    def genesis_digest(self) -> str:
        return self.campaign_fingerprint


class AttemptDisposition(str, Enum):
    BASELINE_ONLY = "baseline_only"
    OBJECTIVE = "objective"
    SAFE_NONTRAINABLE = "safe_nontrainable"
    HARD_STOP_SAFETY = "hard_stop_safety"
    HARD_STOP_RETURN = "hard_stop_return"
    HARD_STOP_CODE_EVIDENCE = "hard_stop_code_evidence"
    HARD_STOP_OPTIMIZER = "hard_stop_optimizer"


HARD_STOP_DISPOSITIONS: Final[frozenset[AttemptDisposition]] = frozenset(
    {
        AttemptDisposition.HARD_STOP_SAFETY,
        AttemptDisposition.HARD_STOP_RETURN,
        AttemptDisposition.HARD_STOP_CODE_EVIDENCE,
        AttemptDisposition.HARD_STOP_OPTIMIZER,
    }
)


class CampaignPhase(str, Enum):
    BOOTSTRAP_PD = "BOOTSTRAP_PD"
    PAUSED_FOR_ANCHOR_AUDIT = "PAUSED_FOR_ANCHOR_AUDIT"
    HARD_STOPPED = "HARD_STOPPED"
    RESTART_REQUIRED = "RESTART_REQUIRED"
    READY_FOR_BO = "READY_FOR_BO"


def _window_mapping(window: TimeWindow) -> dict[str, float]:
    return {"start_s": window.start_s, "end_s": window.end_s}


def _metrics_binding_content(metrics: ForceMetrics) -> dict[str, object]:
    coverage = metrics.coverage
    diagnostics = metrics.diagnostics
    return {
        "schema": "step6.autotune_v4.r001.force_metrics_binding",
        "schema_version": SCHEMA_VERSION,
        "semantic_fingerprint": metrics.semantic_fingerprint,
        "force_mae_v2_n": metrics.force_mae_v2_n,
        "force_mae_v1_compat_shadow_n": metrics.force_mae_v1_compat_shadow_n,
        "v2_role": metrics.v2_role.value,
        "v1_role": metrics.v1_role.value,
        "coverage": {
            "formal_window": _window_mapping(coverage.formal_window),
            "bin_width_s": coverage.bin_width_s,
            "bin_count": coverage.bin_count,
            "total_input_sample_count": coverage.total_input_sample_count,
            "distinct_sample_count": coverage.distinct_sample_count,
            "exact_replay_count": coverage.exact_replay_count,
            "in_window_distinct_sample_count": coverage.in_window_distinct_sample_count,
            "pre_window_distinct_sample_count": coverage.pre_window_distinct_sample_count,
            "end_boundary_distinct_sample_count": coverage.end_boundary_distinct_sample_count,
            "per_bin_distinct_sample_counts": coverage.per_bin_distinct_sample_counts,
            "source_ids": coverage.source_ids,
            "observed_path_time_min_s": coverage.observed_path_time_min_s,
            "observed_path_time_max_s": coverage.observed_path_time_max_s,
            "complete": coverage.complete,
        },
        "bins": tuple(
            {
                "index": item.index,
                "window": _window_mapping(item.window),
                "distinct_sample_count": item.distinct_sample_count,
                "v2_abs_error_mean_n": item.v2_abs_error_mean_n,
                "v1_signed_force_mean_n": item.v1_signed_force_mean_n,
                "v1_compat_abs_error_n": item.v1_compat_abs_error_n,
            }
            for item in metrics.bins
        ),
        "diagnostics": {
            "right_lobe_mae_n": diagnostics.right_lobe_mae_n,
            "left_lobe_mae_n": diagnostics.left_lobe_mae_n,
            "center_crossing_mae_n": diagnostics.center_crossing_mae_n,
            "segment_maes_n": diagnostics.segment_maes_n,
            "worst_segment_index": diagnostics.worst_segment_index,
            "worst_segment_mae_n": diagnostics.worst_segment_mae_n,
            "lobe_imbalance_n": diagnostics.lobe_imbalance_n,
            "right_lobe_bin_indices": diagnostics.right_lobe_bin_indices,
            "left_lobe_bin_indices": diagnostics.left_lobe_bin_indices,
            "ambiguous_center_bin_indices": diagnostics.ambiguous_center_bin_indices,
        },
    }


def force_metrics_evidence_binding(metrics: ForceMetrics) -> str:
    """Return the deterministic content binding used by pause/audit receipts."""

    if type(metrics) is not ForceMetrics:
        raise CampaignContractError("evidence binding requires a typed ForceMetrics result")
    return sha256_canonical(_metrics_binding_content(metrics))


def _validate_complete_force_metrics(metrics: object) -> ForceMetrics:
    if type(metrics) is not ForceMetrics:
        raise CampaignContractError("force objective evidence must be the builder's exact ForceMetrics type")
    if metrics.semantic_fingerprint != STEP6_R001_PROFILE.force_mae_spec.semantic_fingerprint:
        raise CampaignIdentityError("ForceMetrics semantic fingerprint is not the locked Step6 fingerprint")
    if metrics.coverage.semantic_fingerprint != STEP6_R001_PROFILE.force_mae_spec.semantic_fingerprint:
        raise CampaignIdentityError("ForceMetrics coverage semantic fingerprint is not the locked Step6 fingerprint")
    if metrics.v2_role is not MetricRole.OPTIMIZER_OBJECTIVE or metrics.v1_role is not MetricRole.AUDIT_ONLY:
        raise CampaignContractError("ForceMetrics roles are not the locked objective/audit roles")
    if not metrics.coverage.complete:
        raise CampaignContractError("OBJECTIVE requires complete force coverage")
    if len(metrics.bins) != STEP6_R001_PROFILE.force_mae_spec.bin_count:
        raise CampaignContractError("OBJECTIVE requires all 550 formal bins")
    if any(count < 1 for count in metrics.coverage.per_bin_distinct_sample_counts):
        raise CampaignContractError("OBJECTIVE requires one distinct sample in every formal bin")
    return metrics


def _sample_content_mapping(sample: ForceSample) -> dict[str, object]:
    return {
        "source_id": sample.sample_identity.source_id,
        "sequence": sample.sample_identity.sequence,
        "path_time_s": sample.path_time_s,
        "filtered_normal_n": sample.filtered_normal_n,
    }


def _sample_sort_key(sample: ForceSample) -> tuple[str, int, float, float]:
    return (
        sample.sample_identity.source_id,
        sample.sample_identity.sequence,
        sample.path_time_s,
        sample.filtered_normal_n,
    )


def _distinct_samples(samples: tuple[ForceSample, ...]) -> tuple[ForceSample, ...]:
    by_identity: dict[object, ForceSample] = {}
    for sample in samples:
        prior = by_identity.get(sample.sample_identity)
        if prior is None:
            by_identity[sample.sample_identity] = sample
    return tuple(sorted(by_identity.values(), key=_sample_sort_key))


_FORCE_OBJECTIVE_RECEIPT_BUILDER_TOKEN = object()


@dataclass(frozen=True, slots=True)
class ForceObjectiveReceipt:
    """Builder-produced immutable binding of raw samples and complete metrics.

    The private construction token makes the public constructor fail closed;
    callers must use :func:`build_force_objective_receipt`.  The stored metrics
    are recomputed from the stored raw samples on every construction, including
    ``dataclasses.replace`` reconstruction.
    """

    raw_samples: tuple[ForceSample, ...]
    metrics: ForceMetrics
    _builder_token: object = field(default=None, repr=False, compare=False)
    distinct_samples: tuple[ForceSample, ...] = field(init=False)
    evidence_sha256: str = field(init=False)
    metrics_binding_sha256: str = field(init=False)
    receipt_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self._builder_token is not _FORCE_OBJECTIVE_RECEIPT_BUILDER_TOKEN:
            raise CampaignContractError(
                "ForceObjectiveReceipt must be produced by build_force_objective_receipt"
            )
        if not isinstance(self.raw_samples, tuple):
            raise CampaignContractError("ForceObjectiveReceipt raw_samples must be an immutable tuple")
        if any(type(sample) is not ForceSample for sample in self.raw_samples):
            raise CampaignContractError("ForceObjectiveReceipt raw evidence must contain exact ForceSample values")
        metrics = _validate_complete_force_metrics(self.metrics)
        try:
            recomputed = compute_force_metrics(self.raw_samples)
        except (CampaignContractError, ValueError) as exc:
            raise CampaignContractError("ForceObjectiveReceipt raw evidence cannot be recomputed") from exc
        if recomputed != metrics:
            raise CampaignContractError("stored ForceMetrics do not match the bound raw ForceSample evidence")
        distinct = _distinct_samples(self.raw_samples)
        evidence_sha256 = sha256_canonical(
            {
                "schema": "step6.autotune_v4.r001.distinct_force_samples",
                "schema_version": SCHEMA_VERSION,
                "samples": tuple(_sample_content_mapping(sample) for sample in distinct),
            }
        )
        metrics_binding_sha256 = force_metrics_evidence_binding(metrics)
        object.__setattr__(self, "distinct_samples", distinct)
        object.__setattr__(self, "evidence_sha256", evidence_sha256)
        object.__setattr__(self, "metrics_binding_sha256", metrics_binding_sha256)
        object.__setattr__(self, "receipt_digest", sha256_canonical(self.to_content_mapping()))

    @property
    def force_metrics(self) -> ForceMetrics:
        return self.metrics

    @property
    def semantic_fingerprint(self) -> str:
        return self.metrics.semantic_fingerprint

    @property
    def spec_fingerprint(self) -> str:
        return self.semantic_fingerprint

    @property
    def objective_value_n(self) -> float:
        return self.metrics.force_mae_v2_n

    @property
    def force_mae_v1_compat_shadow_n(self) -> float:
        return self.metrics.force_mae_v1_compat_shadow_n

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": "step6.autotune_v4.r001.force_objective_receipt",
            "schema_version": SCHEMA_VERSION,
            "raw_samples": tuple(_sample_content_mapping(sample) for sample in self.raw_samples),
            "distinct_samples": tuple(_sample_content_mapping(sample) for sample in self.distinct_samples),
            "evidence_sha256": self.evidence_sha256,
            "metrics_binding_sha256": self.metrics_binding_sha256,
            "metrics": _metrics_binding_content(self.metrics),
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "receipt_digest", self.receipt_digest)


def build_force_objective_receipt(samples: Iterable[ForceSample]) -> ForceObjectiveReceipt:
    """Build objective evidence only from raw typed samples.

    The builder owns the call to ``compute_force_metrics``.  Input order is
    canonicalized after validation, while exact replay rows remain bound in
    ``raw_samples`` and distinct identities receive their own evidence digest.
    """

    try:
        raw_samples = tuple(samples)
    except TypeError as exc:
        raise CampaignContractError("objective evidence must be an iterable of ForceSample values") from exc
    if any(type(sample) is not ForceSample for sample in raw_samples):
        raise CampaignContractError("objective evidence must contain exact ForceSample values")
    canonical_samples = tuple(sorted(raw_samples, key=_sample_sort_key))
    metrics = compute_force_metrics(canonical_samples)
    return ForceObjectiveReceipt(
        raw_samples=canonical_samples,
        metrics=metrics,
        _builder_token=_FORCE_OBJECTIVE_RECEIPT_BUILDER_TOKEN,
    )


@dataclass(frozen=True, slots=True)
class AttemptAssessment:
    """Separate qualification from optimizer eligibility and objective presence."""

    candidate: Named7dCandidate
    disposition: AttemptDisposition
    qualification_passed: bool
    objective_receipt: ForceObjectiveReceipt | None = None
    force_metrics: ForceMetrics | None = field(init=False)
    optimizer_eligible: bool = field(init=False)
    objective_value_n: float | None = field(init=False)
    evidence_binding_sha256: str | None = field(init=False)
    assessment_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, Named7dCandidate):
            raise CampaignContractError("attempt assessment candidate must be a Named7dCandidate")
        if not isinstance(self.disposition, AttemptDisposition):
            raise CampaignContractError("attempt disposition must be an AttemptDisposition")
        if not isinstance(self.qualification_passed, bool):
            raise CampaignContractError("qualification_passed must be boolean")
        receipt = self.objective_receipt
        if receipt is not None and type(receipt) is not ForceObjectiveReceipt:
            raise CampaignContractError("objective evidence must be an exact ForceObjectiveReceipt")
        if self.disposition is AttemptDisposition.OBJECTIVE:
            if receipt is None:
                raise CampaignContractError("OBJECTIVE requires a ForceObjectiveReceipt")
            _validate_complete_force_metrics(receipt.metrics)
        elif receipt is not None:
            raise CampaignContractError("ForceObjectiveReceipt is only valid for OBJECTIVE assessment")
        metrics = None if receipt is None else receipt.metrics
        eligible = (
            self.disposition is AttemptDisposition.OBJECTIVE
            and self.qualification_passed
            and receipt is not None
        )
        evidence_binding = None if receipt is None else receipt.evidence_sha256
        objective = None if not eligible else receipt.objective_value_n  # type: ignore[union-attr]
        object.__setattr__(self, "force_metrics", metrics)
        object.__setattr__(self, "optimizer_eligible", eligible)
        object.__setattr__(self, "objective_value_n", objective)
        object.__setattr__(self, "evidence_binding_sha256", evidence_binding)
        object.__setattr__(self, "assessment_fingerprint", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": ATTEMPT_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "candidate_uid": self.candidate.candidate_uid,
            "disposition": self.disposition.value,
            "qualification_passed": self.qualification_passed,
            "optimizer_eligible": self.optimizer_eligible,
            "objective_value_n": self.objective_value_n,
            "objective_receipt_digest": None if self.objective_receipt is None else self.objective_receipt.receipt_digest,
            "force_metrics_semantic_fingerprint": (
                None if self.force_metrics is None else self.force_metrics.semantic_fingerprint
            ),
            "evidence_binding_sha256": self.evidence_binding_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "assessment_fingerprint", self.assessment_fingerprint)


def assess_attempt(
    candidate: Named7dCandidate,
    disposition: AttemptDisposition,
    qualification_passed: bool,
    objective_receipt: ForceObjectiveReceipt | None = None,
    *,
    force_metrics: object | None = None,
) -> AttemptAssessment:
    """Construct one assessment; direct ForceMetrics are never an objective seam."""

    if force_metrics is not None:
        raise CampaignContractError(
            "direct ForceMetrics or scalar objective input is forbidden; use build_force_objective_receipt"
        )
    return AttemptAssessment(
        candidate=candidate,
        disposition=disposition,
        qualification_passed=qualification_passed,
        objective_receipt=objective_receipt,
    )


@dataclass(frozen=True, slots=True)
class DispatchTicket:
    """An offline dispatch description; it does not dispatch to a robot."""

    campaign_fingerprint: str
    epoch: int
    bootstrap_index: int
    slot: BootstrapSlot
    candidate: Named7dCandidate
    attempt_identity: str = field(init=False)
    ticket_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        _require_sha(self.campaign_fingerprint, "campaign_fingerprint", CampaignIdentityError)
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch <= 0:
            raise CampaignIdentityError("dispatch ticket epoch must be positive")
        if isinstance(self.bootstrap_index, bool) or not isinstance(self.bootstrap_index, int) or self.bootstrap_index < 0:
            raise CampaignStateError("dispatch ticket bootstrap index must be non-negative")
        if not isinstance(self.slot, BootstrapSlot) or not isinstance(self.candidate, Named7dCandidate):
            raise CampaignContractError("dispatch ticket has invalid typed fields")
        identity_content = {
            "schema": "step6.autotune_v4.r001.attempt_identity",
            "schema_version": SCHEMA_VERSION,
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "bootstrap_index": self.bootstrap_index,
            "candidate_uid": self.candidate.candidate_uid,
        }
        object.__setattr__(self, "attempt_identity", sha256_canonical(identity_content))
        object.__setattr__(self, "ticket_fingerprint", sha256_canonical(self.to_content_mapping()))

    @classmethod
    def create(cls, genesis: CampaignGenesis, row: BootstrapRow) -> DispatchTicket:
        if not isinstance(genesis, CampaignGenesis) or not isinstance(row, BootstrapRow):
            raise CampaignStateError("dispatch ticket requires campaign genesis and bootstrap row")
        return cls(
            campaign_fingerprint=genesis.campaign_fingerprint,
            epoch=genesis.epoch,
            bootstrap_index=row.index,
            slot=row.slot,
            candidate=row.candidate,
        )

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": "step6.autotune_v4.r001.dispatch_ticket",
            "schema_version": SCHEMA_VERSION,
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "bootstrap_index": self.bootstrap_index,
            "slot": self.slot.value,
            "candidate_uid": self.candidate.candidate_uid,
            "attempt_identity": self.attempt_identity,
        }


@dataclass(frozen=True, slots=True)
class CampaignAttemptRecord:
    ticket: DispatchTicket
    assessment: AttemptAssessment
    prior_ledger_head_sha256: str
    return_receipt_sha256: str | None = None
    ledger_head_sha256: str = field(init=False)
    record_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.ticket, DispatchTicket) or not isinstance(self.assessment, AttemptAssessment):
            raise CampaignStateError("attempt record has invalid typed ticket or assessment")
        _require_sha(self.prior_ledger_head_sha256, "prior_ledger_head_sha256", CampaignStateError)
        if self.return_receipt_sha256 is not None:
            _require_sha(self.return_receipt_sha256, "return_receipt_sha256", CampaignStateError)
        if self.assessment.disposition in {
            AttemptDisposition.OBJECTIVE,
            AttemptDisposition.SAFE_NONTRAINABLE,
        } and self.return_receipt_sha256 is None:
            raise CampaignStateError("objective and safe-nontrainable attempts require a post-return receipt")
        if self.ticket.candidate != self.assessment.candidate:
            raise CampaignStateError("attempt ticket and assessment candidate differ")
        content = self.to_content_mapping()
        object.__setattr__(self, "ledger_head_sha256", sha256_canonical(content))
        object.__setattr__(self, "record_fingerprint", sha256_canonical(self.to_content_mapping()))

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": "step6.autotune_v4.r001.campaign_attempt_record",
            "schema_version": SCHEMA_VERSION,
            "prior_ledger_head_sha256": self.prior_ledger_head_sha256,
            "ticket_fingerprint": self.ticket.ticket_fingerprint,
            "attempt_identity": self.ticket.attempt_identity,
            "assessment_fingerprint": self.assessment.assessment_fingerprint,
            "return_receipt_sha256": self.return_receipt_sha256,
        }


@dataclass(frozen=True, slots=True)
class PauseContext:
    """Exact immutable binding required before leaving anchor-audit pause."""

    campaign_fingerprint: str
    epoch: int
    first_attempt_identity: str
    candidate_uid: str
    objective_receipt_digest: str
    force_metrics_fingerprint: str
    metrics_binding_sha256: str
    evidence_binding_sha256: str
    ledger_head_sha256: str
    source_closure_sha256: str
    package_root_sha256: str
    return_receipt_sha256: str
    schema: str = PAUSE_CONTEXT_SCHEMA
    schema_version: int = SCHEMA_VERSION
    context_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "campaign_fingerprint",
            "first_attempt_identity",
            "candidate_uid",
            "objective_receipt_digest",
            "force_metrics_fingerprint",
            "metrics_binding_sha256",
            "evidence_binding_sha256",
            "ledger_head_sha256",
            "source_closure_sha256",
            "package_root_sha256",
            "return_receipt_sha256",
        ):
            _require_sha(getattr(self, name), name, CampaignStateError)
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch <= 0:
            raise CampaignStateError("pause context epoch must be positive")
        _require_schema(self.to_content_mapping(), PAUSE_CONTEXT_SCHEMA, CampaignStateError)
        object.__setattr__(self, "context_fingerprint", sha256_canonical(self.to_content_mapping()))

    @classmethod
    def from_first_attempt(
        cls,
        genesis: CampaignGenesis,
        ticket: DispatchTicket,
        assessment: AttemptAssessment,
        ledger_head_sha256: str,
        package_root_sha256: str,
        return_receipt_sha256: str,
    ) -> PauseContext:
        if (
            ticket.bootstrap_index != 0
            or not assessment.optimizer_eligible
            or assessment.objective_receipt is None
        ):
            raise CampaignStateError("anchor-audit pause requires the eligible first bootstrap objective")
        if ticket.campaign_fingerprint != genesis.campaign_fingerprint or ticket.epoch != genesis.epoch:
            raise CampaignIdentityError("first attempt is not bound to campaign genesis")
        _require_sha(return_receipt_sha256, "return_receipt_sha256", CampaignStateError)
        return cls(
            campaign_fingerprint=genesis.campaign_fingerprint,
            epoch=genesis.epoch,
            first_attempt_identity=ticket.attempt_identity,
            candidate_uid=ticket.candidate.candidate_uid,
            objective_receipt_digest=assessment.objective_receipt.receipt_digest,
            force_metrics_fingerprint=assessment.objective_receipt.semantic_fingerprint,
            metrics_binding_sha256=assessment.objective_receipt.metrics_binding_sha256,
            evidence_binding_sha256=assessment.evidence_binding_sha256,  # type: ignore[arg-type]
            ledger_head_sha256=ledger_head_sha256,
            source_closure_sha256=genesis.source_closure_sha256,
            package_root_sha256=package_root_sha256,
            return_receipt_sha256=return_receipt_sha256,
        )

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "first_attempt_identity": self.first_attempt_identity,
            "candidate_uid": self.candidate_uid,
            "objective_receipt_digest": self.objective_receipt_digest,
            "force_metrics_fingerprint": self.force_metrics_fingerprint,
            "metrics_binding_sha256": self.metrics_binding_sha256,
            "evidence_binding_sha256": self.evidence_binding_sha256,
            "ledger_head_sha256": self.ledger_head_sha256,
            "source_closure_sha256": self.source_closure_sha256,
            "package_root_sha256": self.package_root_sha256,
            "return_receipt_sha256": self.return_receipt_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "context_fingerprint", self.context_fingerprint)


class AnchorAuditStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class AnchorAuditReceipt:
    status: AnchorAuditStatus
    pause_context_fingerprint: str
    campaign_fingerprint: str
    epoch: int
    first_attempt_identity: str
    candidate_uid: str
    objective_receipt_digest: str
    force_metrics_fingerprint: str
    metrics_binding_sha256: str
    evidence_binding_sha256: str
    ledger_head_sha256: str
    source_closure_sha256: str
    package_root_sha256: str
    return_receipt_sha256: str
    schema: str = AUDIT_RECEIPT_SCHEMA
    schema_version: int = SCHEMA_VERSION
    receipt_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.status, AnchorAuditStatus):
            raise CampaignResumeError("anchor audit status must be PASS or FAIL")
        for name in (
            "pause_context_fingerprint",
            "campaign_fingerprint",
            "first_attempt_identity",
            "candidate_uid",
            "objective_receipt_digest",
            "force_metrics_fingerprint",
            "metrics_binding_sha256",
            "evidence_binding_sha256",
            "ledger_head_sha256",
            "source_closure_sha256",
            "package_root_sha256",
            "return_receipt_sha256",
        ):
            _require_sha(getattr(self, name), name, CampaignResumeError)
        if isinstance(self.epoch, bool) or not isinstance(self.epoch, int) or self.epoch <= 0:
            raise CampaignResumeError("anchor audit receipt epoch must be positive")
        _require_schema(self.to_content_mapping(), AUDIT_RECEIPT_SCHEMA, CampaignResumeError)
        object.__setattr__(self, "receipt_fingerprint", sha256_canonical(self.to_content_mapping()))

    @classmethod
    def from_context(cls, context: PauseContext, status: AnchorAuditStatus) -> AnchorAuditReceipt:
        if not isinstance(context, PauseContext):
            raise CampaignResumeError("anchor audit receipt requires an immutable PauseContext")
        return cls(
            status=status,
            pause_context_fingerprint=context.context_fingerprint,
            campaign_fingerprint=context.campaign_fingerprint,
            epoch=context.epoch,
            first_attempt_identity=context.first_attempt_identity,
            candidate_uid=context.candidate_uid,
            objective_receipt_digest=context.objective_receipt_digest,
            force_metrics_fingerprint=context.force_metrics_fingerprint,
            metrics_binding_sha256=context.metrics_binding_sha256,
            evidence_binding_sha256=context.evidence_binding_sha256,
            ledger_head_sha256=context.ledger_head_sha256,
            source_closure_sha256=context.source_closure_sha256,
            package_root_sha256=context.package_root_sha256,
            return_receipt_sha256=context.return_receipt_sha256,
        )

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "pause_context_fingerprint": self.pause_context_fingerprint,
            "campaign_fingerprint": self.campaign_fingerprint,
            "epoch": self.epoch,
            "first_attempt_identity": self.first_attempt_identity,
            "candidate_uid": self.candidate_uid,
            "objective_receipt_digest": self.objective_receipt_digest,
            "force_metrics_fingerprint": self.force_metrics_fingerprint,
            "metrics_binding_sha256": self.metrics_binding_sha256,
            "evidence_binding_sha256": self.evidence_binding_sha256,
            "ledger_head_sha256": self.ledger_head_sha256,
            "source_closure_sha256": self.source_closure_sha256,
            "package_root_sha256": self.package_root_sha256,
            "return_receipt_sha256": self.return_receipt_sha256,
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "receipt_fingerprint", self.receipt_fingerprint)


def _initial_ledger_head(genesis: CampaignGenesis) -> str:
    return sha256_canonical(
        {
            "schema": "step6.autotune_v4.r001.genesis_ledger_head",
            "schema_version": SCHEMA_VERSION,
            "campaign_fingerprint": genesis.campaign_fingerprint,
            "epoch": genesis.epoch,
        }
    )


@dataclass(frozen=True, slots=True)
class CampaignState:
    """Immutable offline campaign state with explicit pause and resume gates."""

    genesis: CampaignGenesis
    bootstrap_plan: BootstrapPlan
    package_root_sha256: str
    ledger_head_sha256: str
    phase: CampaignPhase = CampaignPhase.BOOTSTRAP_PD
    next_bootstrap_index: int = 0
    attempts: tuple[CampaignAttemptRecord, ...] = ()
    pause_context: PauseContext | None = None
    last_audit_receipt: AnchorAuditReceipt | None = None
    state_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.genesis, CampaignGenesis) or not isinstance(self.bootstrap_plan, BootstrapPlan):
            raise CampaignStateError("campaign state requires typed genesis and bootstrap plan")
        if self.bootstrap_plan.domain_fingerprint != self.genesis.domain_fingerprint:
            raise CampaignIdentityError("bootstrap domain fingerprint differs from campaign genesis")
        if self.bootstrap_plan.seed_receipt_digest != self.genesis.seed_receipt_digest:
            raise CampaignIdentityError("bootstrap seed receipt digest differs from campaign genesis")
        for name in ("package_root_sha256", "ledger_head_sha256"):
            _require_sha(getattr(self, name), name, CampaignStateError)
        if not isinstance(self.phase, CampaignPhase):
            raise CampaignStateError("unknown campaign phase")
        if isinstance(self.next_bootstrap_index, bool) or not isinstance(self.next_bootstrap_index, int):
            raise CampaignStateError("next_bootstrap_index must be an integer")
        if not 0 <= self.next_bootstrap_index <= len(self.bootstrap_plan.rows):
            raise CampaignStateError("next_bootstrap_index is outside the bootstrap plan")
        attempts = tuple(self.attempts)
        if len(attempts) > len(self.bootstrap_plan.rows):
            raise CampaignStateError("campaign state has too many attempt records")
        if len(attempts) != self.next_bootstrap_index:
            raise CampaignStateError("next_bootstrap_index must equal the consumed attempt prefix")
        expected_ledger_head = _initial_ledger_head(self.genesis)
        for index, record in enumerate(attempts):
            if not isinstance(record, CampaignAttemptRecord) or record.ticket.bootstrap_index != index:
                raise CampaignStateError("campaign attempts are not a contiguous immutable prefix")
            if record.ticket.campaign_fingerprint != self.genesis.campaign_fingerprint or record.ticket.epoch != self.genesis.epoch:
                raise CampaignIdentityError("campaign attempt is not bound to this genesis")
            expected_ticket = DispatchTicket.create(self.genesis, self.bootstrap_plan.rows[index])
            if record.ticket != expected_ticket:
                raise CampaignIdentityError("campaign attempt does not match the immutable bootstrap plan row")
            if record.prior_ledger_head_sha256 != expected_ledger_head:
                raise CampaignStateError("campaign attempt ledger chain has a broken prior head")
            expected_ledger_head = record.ledger_head_sha256
        if self.ledger_head_sha256 != expected_ledger_head:
            raise CampaignStateError("campaign state ledger head is not the exact attempt-chain head")
        object.__setattr__(self, "attempts", attempts)
        expected_phase, expected_pause, expected_audit = self._derive_expected_transition()
        if self.phase is not expected_phase:
            raise CampaignStateError(
                f"campaign phase {self.phase.value!r} does not match the immutable attempt record transition"
            )
        if self.pause_context != expected_pause:
            raise CampaignStateError("stored pause context is not exactly re-derived from the first attempt")
        if self.last_audit_receipt != expected_audit:
            raise CampaignResumeError("stored anchor-audit PASS receipt is not exactly bound to campaign evidence")
        object.__setattr__(self, "state_fingerprint", sha256_canonical(self.to_content_mapping()))

    @staticmethod
    def _is_hard_stop(assessment: AttemptAssessment) -> bool:
        return assessment.disposition in HARD_STOP_DISPOSITIONS

    @staticmethod
    def _is_consumable_after_anchor_audit(assessment: AttemptAssessment) -> bool:
        return assessment.disposition is AttemptDisposition.SAFE_NONTRAINABLE or assessment.optimizer_eligible

    def _derive_expected_transition(
        self,
    ) -> tuple[CampaignPhase, PauseContext | None, AnchorAuditReceipt | None]:
        if not self.attempts:
            if self.pause_context is not None or self.last_audit_receipt is not None:
                raise CampaignStateError("an empty campaign cannot carry pause or audit state")
            return CampaignPhase.BOOTSTRAP_PD, None, None

        first_record = self.attempts[0]
        first_assessment = first_record.assessment
        if self._is_hard_stop(first_assessment):
            if len(self.attempts) != 1:
                raise CampaignStateError("a hard stop must terminate the immutable same-epoch attempt prefix")
            return CampaignPhase.HARD_STOPPED, None, None

        first_objective = (
            first_assessment.disposition is AttemptDisposition.OBJECTIVE
            and first_assessment.optimizer_eligible
        )
        if not first_objective:
            if len(self.attempts) != 1:
                raise CampaignStateError("a first-attempt noneligible outcome requires a new epoch")
            return CampaignPhase.RESTART_REQUIRED, None, None

        expected_pause = PauseContext.from_first_attempt(
            self.genesis,
            first_record.ticket,
            first_assessment,
            first_record.ledger_head_sha256,
            self.package_root_sha256,
            first_record.return_receipt_sha256,  # type: ignore[arg-type]
        )
        expected_pass = AnchorAuditReceipt.from_context(expected_pause, AnchorAuditStatus.PASS)
        if self.last_audit_receipt is None:
            if len(self.attempts) != 1:
                raise CampaignResumeError("later attempts cannot exist before the exact anchor-audit PASS receipt")
            return CampaignPhase.PAUSED_FOR_ANCHOR_AUDIT, expected_pause, None
        if self.last_audit_receipt != expected_pass:
            raise CampaignResumeError("stored PASS receipt is not exactly bound to the re-derived pause context")
        if self.pause_context is not None:
            raise CampaignStateError("resumed campaign cannot retain a pause context")

        for index, record in enumerate(self.attempts[1:], start=1):
            assessment = record.assessment
            if self._is_hard_stop(assessment):
                if index != len(self.attempts) - 1:
                    raise CampaignStateError("a hard stop must be the final same-epoch record")
                return CampaignPhase.HARD_STOPPED, None, expected_pass
            if not self._is_consumable_after_anchor_audit(assessment):
                if index != len(self.attempts) - 1:
                    raise CampaignStateError("a restart-required outcome must be the final same-epoch record")
                return CampaignPhase.RESTART_REQUIRED, None, expected_pass
        if len(self.attempts) == len(self.bootstrap_plan.rows):
            return CampaignPhase.READY_FOR_BO, None, expected_pass
        return CampaignPhase.BOOTSTRAP_PD, None, expected_pass

    @classmethod
    def create(
        cls,
        genesis: CampaignGenesis,
        bootstrap_plan: BootstrapPlan,
        *,
        package_root_sha256: str,
    ) -> CampaignState:
        if not isinstance(genesis, CampaignGenesis) or not isinstance(bootstrap_plan, BootstrapPlan):
            raise CampaignStateError("campaign state creation requires typed genesis and bootstrap plan")
        if bootstrap_plan.domain_fingerprint != genesis.domain_fingerprint or bootstrap_plan.seed_receipt_digest != genesis.seed_receipt_digest:
            raise CampaignIdentityError("bootstrap plan is not bound to campaign genesis")
        return cls(
            genesis=genesis,
            bootstrap_plan=bootstrap_plan,
            package_root_sha256=package_root_sha256,
            ledger_head_sha256=_initial_ledger_head(genesis),
        )

    def to_content_mapping(self) -> dict[str, object]:
        return {
            "schema": STATE_SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "campaign_fingerprint": self.genesis.campaign_fingerprint,
            "epoch": self.genesis.epoch,
            "phase": self.phase,
            "next_bootstrap_index": self.next_bootstrap_index,
            "package_root_sha256": self.package_root_sha256,
            "ledger_head_sha256": self.ledger_head_sha256,
            "attempt_record_fingerprints": tuple(record.record_fingerprint for record in self.attempts),
            "pause_context_fingerprint": None if self.pause_context is None else self.pause_context.context_fingerprint,
            "last_audit_receipt_fingerprint": (
                None if self.last_audit_receipt is None else self.last_audit_receipt.receipt_fingerprint
            ),
        }

    def to_mapping(self) -> dict[str, object]:
        return _mapping_with_digest(self.to_content_mapping(), "state_fingerprint", self.state_fingerprint)

    def dispatch_next(self) -> DispatchTicket | None:
        if self.phase is CampaignPhase.PAUSED_FOR_ANCHOR_AUDIT:
            raise CampaignPausedError("campaign is paused for anchor audit; no next attempt may be dispatched")
        if self.phase in {CampaignPhase.HARD_STOPPED, CampaignPhase.RESTART_REQUIRED}:
            raise CampaignStateError(f"campaign phase {self.phase.value!r} forbids dispatch in this epoch")
        if self.phase is CampaignPhase.READY_FOR_BO:
            return None
        row = self.bootstrap_plan.rows[self.next_bootstrap_index]
        return DispatchTicket.create(self.genesis, row)

    def record_attempt(
        self,
        ticket: DispatchTicket,
        assessment: AttemptAssessment,
        *,
        return_receipt_sha256: str | None = None,
    ) -> CampaignState:
        if self.phase is not CampaignPhase.BOOTSTRAP_PD:
            raise CampaignStateError("attempts cannot be recorded outside BOOTSTRAP_PD")
        if not isinstance(ticket, DispatchTicket) or not isinstance(assessment, AttemptAssessment):
            raise CampaignStateError("record_attempt requires typed ticket and assessment")
        expected = self.dispatch_next()
        if expected is None or ticket != expected:
            raise CampaignIdentityError("attempt ticket is not the next exact campaign dispatch")
        if assessment.candidate != ticket.candidate:
            raise CampaignStateError("attempt assessment candidate differs from dispatch ticket")
        if return_receipt_sha256 is not None:
            _require_sha(return_receipt_sha256, "return_receipt_sha256", CampaignStateError)
        record = CampaignAttemptRecord(
            ticket=ticket,
            assessment=assessment,
            prior_ledger_head_sha256=self.ledger_head_sha256,
            return_receipt_sha256=return_receipt_sha256,
        )
        new_attempts = self.attempts + (record,)
        if self._is_hard_stop(assessment):
            next_phase = CampaignPhase.HARD_STOPPED
            pause_context = None
        elif ticket.bootstrap_index == 0 and assessment.optimizer_eligible:
            pause_context = PauseContext.from_first_attempt(
                self.genesis,
                ticket,
                assessment,
                record.ledger_head_sha256,
                self.package_root_sha256,
                record.return_receipt_sha256,  # type: ignore[arg-type]
            )
            next_phase = CampaignPhase.PAUSED_FOR_ANCHOR_AUDIT
        elif ticket.bootstrap_index == 0:
            next_phase = CampaignPhase.RESTART_REQUIRED
            pause_context = None
        elif not self._is_consumable_after_anchor_audit(assessment):
            next_phase = CampaignPhase.RESTART_REQUIRED
            pause_context = None
        else:
            next_phase = CampaignPhase.BOOTSTRAP_PD
            pause_context = None
        next_index = self.next_bootstrap_index + 1
        if next_phase is CampaignPhase.BOOTSTRAP_PD and next_index == len(self.bootstrap_plan.rows):
            next_phase = CampaignPhase.READY_FOR_BO
        return CampaignState(
            genesis=self.genesis,
            bootstrap_plan=self.bootstrap_plan,
            package_root_sha256=self.package_root_sha256,
            ledger_head_sha256=record.ledger_head_sha256,
            phase=next_phase,
            next_bootstrap_index=next_index,
            attempts=new_attempts,
            pause_context=pause_context,
            last_audit_receipt=self.last_audit_receipt,
        )

    def resume_from_anchor_audit(self, receipt: AnchorAuditReceipt | None) -> CampaignState:
        if self.phase is not CampaignPhase.PAUSED_FOR_ANCHOR_AUDIT or self.pause_context is None:
            raise CampaignResumeError("campaign is not awaiting an anchor-audit resume")
        if receipt is None:
            raise CampaignResumeError("anchor-audit PASS receipt is required to resume")
        if not isinstance(receipt, AnchorAuditReceipt):
            raise CampaignResumeError("resume requires a typed AnchorAuditReceipt")
        if receipt.status is not AnchorAuditStatus.PASS:
            raise CampaignResumeError("anchor-audit FAIL cannot resume the same epoch; restart in a new epoch")
        expected = AnchorAuditReceipt.from_context(self.pause_context, AnchorAuditStatus.PASS)
        if receipt != expected:
            raise CampaignResumeError("anchor-audit receipt is not exactly bound to the immutable pause context")
        return CampaignState(
            genesis=self.genesis,
            bootstrap_plan=self.bootstrap_plan,
            package_root_sha256=self.package_root_sha256,
            ledger_head_sha256=self.ledger_head_sha256,
            phase=CampaignPhase.BOOTSTRAP_PD,
            next_bootstrap_index=1,
            attempts=self.attempts,
            pause_context=None,
            last_audit_receipt=receipt,
        )

    # Explicit aliases keep the transition vocabulary small but discoverable.
    next_dispatch = dispatch_next
    accept_attempt = record_attempt
    resume = resume_from_anchor_audit


__all__ = [
    "AUDIT_RECEIPT_SCHEMA",
    "ATTEMPT_SCHEMA",
    "AnchorAuditReceipt",
    "AnchorAuditStatus",
    "AttemptAssessment",
    "AttemptDisposition",
    "BOOTSTRAP_PLAN_SCHEMA",
    "BOOTSTRAP_SLOT_ORDER",
    "BootstrapPlan",
    "BootstrapRow",
    "BootstrapSlot",
    "CANDIDATE_SCHEMA",
    "COORDINATE_NAMES",
    "CampaignAttemptRecord",
    "CampaignGenesis",
    "CampaignPhase",
    "CampaignState",
    "DOMAIN_SCHEMA",
    "DomainViolationError",
    "GENESIS_SCHEMA",
    "HARD_STOP_DISPOSITIONS",
    "IncumbentSeedReceipt",
    "Named7dCandidate",
    "Named7dDomain",
    "PAUSE_CONTEXT_SCHEMA",
    "PauseContext",
    "SEED_RECEIPT_SCHEMA",
    "SCHEMA_VERSION",
    "STATE_SCHEMA",
    "DispatchTicket",
    "ForceObjectiveReceipt",
    "assess_attempt",
    "build_force_objective_receipt",
    "canonical_json_bytes",
    "force_metrics_evidence_binding",
    "plan_bootstrap_pd",
    "sha256_canonical",
]
