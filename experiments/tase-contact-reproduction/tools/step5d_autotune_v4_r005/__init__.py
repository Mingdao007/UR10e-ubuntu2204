"""Offline-only Step5d Autotune V4 r005 host-loop primitives.

The package is deliberately independent from the r004 live identity.  It can
reuse r004 mechanics through typed adapters, but it owns a new campaign
contract, observation ledger, attempt sequence, and source closure.
"""

from .contracts import (
    D_ANCHOR,
    I_ON_ANCHOR,
    NAMED_DIMENSIONS,
    PROGRAM,
    TARGET_FORCE_N,
    Candidate,
    R005Contract,
    R005ContractError,
    bootstrap_pd_candidates,
    full_domain,
    load_contract,
    validate_transition,
)
from step5d_force_objective import ForceObjective, ForceObjectiveBuilder, ForceObjectiveError, ForcePathSample

__all__ = [
    "Candidate",
    "D_ANCHOR",
    "I_ON_ANCHOR",
    "NAMED_DIMENSIONS",
    "PROGRAM",
    "R005Contract",
    "R005ContractError",
    "TARGET_FORCE_N",
    "bootstrap_pd_candidates",
    "full_domain",
    "load_contract",
    "validate_transition",
    "ForceObjective",
    "ForceObjectiveBuilder",
    "ForceObjectiveError",
    "ForcePathSample",
]
