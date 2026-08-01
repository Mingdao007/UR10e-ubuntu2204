"""Typed fail-closed errors for the offline Step6 r001 primitives."""

from __future__ import annotations


class Step6PrimitiveError(ValueError):
    """Base class for invalid Step6 r001 contracts or evidence."""


class ContractInvariantError(Step6PrimitiveError):
    """A locked Step6 r001 invariant was not satisfied."""


class SafeFrameValidationError(ContractInvariantError):
    """The inherited safe-frame source or its XY geometry is invalid."""


class InvalidPathTimeError(ContractInvariantError):
    """A path time is not a finite value in the closed program interval."""


class InvalidForceSampleError(ContractInvariantError):
    """A force sample is not a finite, typed, aligned sample."""


class ForceEvidenceError(ContractInvariantError):
    """Force evidence cannot produce a valid objective or audit result."""


class DuplicateIdentityConflictError(ForceEvidenceError):
    """One stable sample identity was reused with a conflicting payload."""

    def __init__(self, *, sample_identity: object, prior: object, conflicting: object) -> None:
        self.sample_identity = sample_identity
        self.prior = prior
        self.conflicting = conflicting
        super().__init__(
            "sample identity was reused with conflicting path time or force: "
            f"identity={sample_identity!r}, prior={prior!r}, conflicting={conflicting!r}"
        )


class IncompleteCoverageError(ForceEvidenceError):
    """The formal [5, 60) window does not contain all 550 complete bins."""

    def __init__(
        self,
        *,
        missing_bin_indices: tuple[int, ...],
        observed_bin_sample_counts: tuple[int, ...],
        distinct_sample_count: int,
        duplicate_replay_count: int,
    ) -> None:
        self.missing_bin_indices = missing_bin_indices
        self.observed_bin_sample_counts = observed_bin_sample_counts
        self.distinct_sample_count = distinct_sample_count
        self.duplicate_replay_count = duplicate_replay_count
        super().__init__(
            "Step6 force evidence has incomplete formal coverage: "
            f"missing_bins={missing_bin_indices!r}, "
            f"distinct_samples={distinct_sample_count}, "
            f"exact_replays={duplicate_replay_count}"
        )


class CampaignContractError(Step6PrimitiveError):
    """An offline campaign contract, mapping, or binding is invalid."""


class SeedReceiptError(CampaignContractError):
    """An incumbent seed receipt is malformed or contains forbidden state."""


class DomainViolationError(CampaignContractError):
    """A named 7D candidate is outside the injected immutable domain."""


class CampaignIdentityError(CampaignContractError):
    """Campaign identity, epoch, profile, or content binding is inconsistent."""


class CampaignStateError(CampaignContractError):
    """An immutable campaign state transition is not permitted."""


class CampaignPausedError(CampaignStateError):
    """No dispatch is allowed while the campaign is paused for anchor audit."""


class CampaignResumeError(CampaignStateError):
    """An anchor-audit receipt is missing, failed, or not exactly bound."""


class FakeRTDEError(CampaignContractError):
    """An offline-only FakeRTDE route, payload, freshness, or evidence error."""


class FakeRTDEIdentityError(FakeRTDEError):
    """A FakeRTDE packet is not bound to the exact route identity."""


class FakeRTDEFreshnessError(FakeRTDEError):
    """A FakeRTDE packet violates transport or command freshness."""


class FakeRTDEEvidenceError(FakeRTDEError):
    """A FakeRTDE observation cannot produce complete diagnostic evidence."""
