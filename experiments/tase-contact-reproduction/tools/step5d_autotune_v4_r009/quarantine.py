"""Fail-closed quarantine for the historical executable R008 path."""

from __future__ import annotations

from typing import NoReturn


HISTORICAL_R008_CAMPAIGN_FINGERPRINT = (
    "b461ed52f69d2ab11c41a252898e88122b5c1dcacc21ba102222e1a9e7250f8b"
)
HISTORICAL_R008_CONTRACT_SHA256 = (
    "89d75ea389018015feb71ec66258e66d8a2abbbc003f4765e2fba3fe315cb4f7"
)


class R008HistoricalLineageDoNotResumeError(RuntimeError):
    """The historical R008 formal entry path is not an R009 input."""

    code = "R008_HISTORICAL_LINEAGE_DO_NOT_RESUME"

    def __init__(self, entrypoint: str) -> None:
        self.entrypoint = str(entrypoint)
        super().__init__(
            f"{self.code}: formal R008 entry is quarantined as historical lineage "
            f"and must not resume ({self.entrypoint}); campaign="
            f"{HISTORICAL_R008_CAMPAIGN_FINGERPRINT}, contract="
            f"{HISTORICAL_R008_CONTRACT_SHA256}"
        )


# Short compatibility names keep the typed boundary discoverable to callers
# without creating alternate recovery behavior.
R008ResumeQuarantinedError = R008HistoricalLineageDoNotResumeError
R008HistoricalLineageError = R008HistoricalLineageDoNotResumeError


def reject_r008_formal_resume(entrypoint: str) -> NoReturn:
    """Raise before any historical R008 runtime/authority entry is reached."""

    raise R008HistoricalLineageDoNotResumeError(entrypoint)


assert_r008_formal_resume_quarantined = reject_r008_formal_resume


__all__ = [
    "HISTORICAL_R008_CAMPAIGN_FINGERPRINT",
    "HISTORICAL_R008_CONTRACT_SHA256",
    "R008HistoricalLineageDoNotResumeError",
    "R008HistoricalLineageError",
    "R008ResumeQuarantinedError",
    "assert_r008_formal_resume_quarantined",
    "reject_r008_formal_resume",
]
