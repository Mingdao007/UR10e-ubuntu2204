"""Typed review-governance source contract for TacDiffusion formal V4.

The contract is a source-of-truth reader/validator only.  It does not launch
reviewers and it does not rewrite historical review artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .contracts import FORMAL_REVIEW_GOVERNANCE_SCHEMA_V1


REVIEW_GOVERNANCE_SOURCE_SCHEMA_V1 = FORMAL_REVIEW_GOVERNANCE_SCHEMA_V1
FORMAL_V4_LINEAGE = "tacdiffusion_formal_v4"
LEGACY_REVIEW_CONTRACTS = ("1+1", "2+1")


@dataclass(frozen=True)
class ReviewGovernanceSourceContractV1:
    """Formal V4 reviewer cardinality and historical-artifact boundary."""

    lineage: str = FORMAL_V4_LINEAGE
    automatic_reviewer_count: int = 0
    explicit_luna_max_reviewer_max: int = 1
    explicit_luna_max_only: bool = True
    supersedes_legacy_contracts: tuple[str, ...] = LEGACY_REVIEW_CONTRACTS
    historical_artifacts_are_not_rewritten: bool = True
    ordinary_offline_review: str = "0+0 deterministic validation"
    schema: str = REVIEW_GOVERNANCE_SOURCE_SCHEMA_V1

    def __post_init__(self) -> None:
        if self.schema != REVIEW_GOVERNANCE_SOURCE_SCHEMA_V1:
            raise ValueError("unsupported formal V4 review-governance schema")
        if self.lineage != FORMAL_V4_LINEAGE:
            raise ValueError("review-governance contract is for TacDiffusion formal V4")
        if self.automatic_reviewer_count != 0:
            raise ValueError("formal V4 automatic reviewer count must be zero")
        if self.explicit_luna_max_reviewer_max != 1:
            raise ValueError("formal V4 permits at most one explicit Luna Max reviewer")
        if self.explicit_luna_max_only is not True:
            raise ValueError("formal V4 reviewer must be explicitly requested Luna Max")
        if tuple(self.supersedes_legacy_contracts) != LEGACY_REVIEW_CONTRACTS:
            raise ValueError("formal V4 must supersede legacy 1+1 and 2+1 contracts")
        if self.historical_artifacts_are_not_rewritten is not True:
            raise ValueError("historical review artifacts must remain immutable")
        if not self.ordinary_offline_review.strip():
            raise ValueError("ordinary offline review semantics are required")

    def validate_requested_review(self, *, automatic_reviewers: int = 0, explicit_luna_max_reviewers: int = 0) -> None:
        if automatic_reviewers != self.automatic_reviewer_count:
            raise ValueError("formal V4 automatic reviewer count is not zero")
        if explicit_luna_max_reviewers < 0 or explicit_luna_max_reviewers > self.explicit_luna_max_reviewer_max:
            raise ValueError("formal V4 allows at most one explicit Luna Max reviewer")

    @property
    def automatic_reviewers(self) -> int:
        return self.automatic_reviewer_count

    @property
    def optional_luna_max_reviewer_max(self) -> int:
        return self.explicit_luna_max_reviewer_max

    def as_json(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "lineage": self.lineage,
            "automatic_reviewer_count": self.automatic_reviewer_count,
            "explicit_luna_max_reviewer_max": self.explicit_luna_max_reviewer_max,
            "explicit_luna_max_only": self.explicit_luna_max_only,
            "supersedes_legacy_contracts": list(self.supersedes_legacy_contracts),
            "historical_artifacts_are_not_rewritten": self.historical_artifacts_are_not_rewritten,
            "ordinary_offline_review": self.ordinary_offline_review,
        }

    @property
    def fingerprint_sha256(self) -> str:
        payload = json.dumps(self.as_json(), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def build_review_governance_source_contract() -> ReviewGovernanceSourceContractV1:
    """Build the canonical formal-V4 governance source object."""

    return ReviewGovernanceSourceContractV1()


def load_review_governance_source(path: str | Path) -> ReviewGovernanceSourceContractV1:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("review-governance source must be a JSON object")
    contract = ReviewGovernanceSourceContractV1(
        schema=str(payload.get("schema", "")),
        lineage=str(payload.get("lineage", "")),
        automatic_reviewer_count=int(payload.get("automatic_reviewer_count", -1)),
        explicit_luna_max_reviewer_max=int(payload.get("explicit_luna_max_reviewer_max", -1)),
        explicit_luna_max_only=bool(payload.get("explicit_luna_max_only", False)),
        supersedes_legacy_contracts=tuple(str(value) for value in payload.get("supersedes_legacy_contracts", ())),
        historical_artifacts_are_not_rewritten=bool(payload.get("historical_artifacts_are_not_rewritten", False)),
        ordinary_offline_review=str(payload.get("ordinary_offline_review", "")),
    )
    if contract.as_json() != dict(payload):
        raise ValueError("review-governance source contains non-canonical or extra fields")
    return contract


# Descriptive aliases make the source contract easy to find without creating
# multiple writers or multiple semantics.
FormalV4ReviewGovernanceV1 = ReviewGovernanceSourceContractV1
TacDiffusionV4ReviewGovernanceV1 = ReviewGovernanceSourceContractV1


__all__ = [
    "FORMAL_V4_LINEAGE",
    "LEGACY_REVIEW_CONTRACTS",
    "REVIEW_GOVERNANCE_SOURCE_SCHEMA_V1",
    "ReviewGovernanceSourceContractV1",
    "FormalV4ReviewGovernanceV1",
    "TacDiffusionV4ReviewGovernanceV1",
    "build_review_governance_source_contract",
    "load_review_governance_source",
]
