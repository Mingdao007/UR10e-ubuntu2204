"""r008 observation kinds and durable queue extensions."""

from __future__ import annotations

from pathlib import Path

from step5d_autotune_v4_r006.live_adapter import R006NativeV3DurableQueueAdapter


R008_REPEATABLE_KINDS = frozenset(
    {
        "QUALIFICATION",
        "BOOTSTRAP_PD",
        "WARM_START_1",
        "WARM_START_2",
        "ROUTE",
        "RETEST",
        "P0_NOCONTACT",
        "STAIRCASE",
        "SPACEFILL",
        "BO",
        "BO_TRIAL",
        "ANCHOR",
    }
)


class R008DurableQueueAdapter(R006NativeV3DurableQueueAdapter):
    """Native r006 queue with the r008 phase-plan kinds admitted as repeatable."""

    repeatable_kinds = R008_REPEATABLE_KINDS

    def __init__(
        self,
        root: Path,
        *,
        campaign_id: str,
        launch_profile_path: Path,
        release_manifest_sha256: str | None = None,
    ) -> None:
        super().__init__(
            root,
            campaign_id=campaign_id,
            launch_profile_path=launch_profile_path,
            release_manifest_sha256=release_manifest_sha256,
        )


__all__ = ["R008_REPEATABLE_KINDS", "R008DurableQueueAdapter"]
