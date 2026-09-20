"""Execute one prepared TASE/V5 figure-eight, without a campaign loop.

Preparation, current Home, controller package and runtime admission remain
owned by the existing V5 preparation path. This module does not provision,
Load/Play, choose gains, or synthesize admission evidence.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .v5_campaign import ENTRY_MODE_HOME_ONLY_V1, V5TrialPlan
from .v5_lifecycle_ledger import LedgerRole
from .v5_live_owner import (
    V5LiveChainRequestV1,
    V5SingleWriterOwnerV1,
    build_v5_live_context,
)


def execute_single_trial(
    *,
    plan: V5TrialPlan,
    run_dir: Path,
    result_path: Path,
    campaign_fingerprint: str,
    release_identity_sha256: str,
    role: LedgerRole,
    launch_profile: Path,
    controller_host: str,
    kunwei_host: str,
    kunwei_port: int = 5152,
    timeout_s: float = 180.0,
    context_factory: Callable[..., Any] = build_v5_live_context,
    owner_factory: Callable[..., Any] = V5SingleWriterOwnerV1,
):
    """Dispatch exactly one Home-bound plan and always close its sole owner.

    The existing owner includes the analytic 60..62.831853 s closure tail.
    A failed attempt is returned/raised as-is: no second plan and no retry.
    All request checks precede opening the prepared live context.
    """
    if not isinstance(plan, V5TrialPlan):
        raise TypeError("single trial requires a typed V5 plan")
    if not plan.requires_home or plan.packable or plan.entry_mode != ENTRY_MODE_HOME_ONLY_V1:
        raise ValueError("single trial requires an unpackable HOME_ONLY plan")
    if plan.prefetched:
        raise ValueError("single trial cannot consume a prefetched campaign plan")
    destination = Path(result_path).absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("single-trial result already exists; do not redispatch")
    request = V5LiveChainRequestV1(
        chain_id=plan.trial_id,
        plans=(plan,),
        campaign_fingerprint=campaign_fingerprint,
        release_identity_sha256=release_identity_sha256,
        role=role,
        durable_result_path=str(destination),
        timeout_s=timeout_s,
    )
    context = context_factory(
        run_dir=Path(run_dir),
        controller_host=controller_host,
        kunwei_host=kunwei_host,
        kunwei_port=kunwei_port,
        launch_profile=Path(launch_profile),
        expected_campaign_fingerprint=campaign_fingerprint,
        expected_release_identity_sha256=release_identity_sha256,
        expected_role=role,
    )
    try:
        return owner_factory(context).execute_chain(request)
    finally:
        context.close()
