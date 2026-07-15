"""Step5d native autotune control plane v2.

The package owns orchestration state only.  The frozen Step5d control law,
force/frame semantics, rates, limits, and TP motion path remain outside this
package and are bound by the deployment manifest.
"""

from .model import BatchSpec, CandidateSpec, DeploymentSpec
from .reducer import LifecycleEvent, LifecycleSnapshot, LifecycleState, reduce_lifecycle

__all__ = [
    "BatchSpec",
    "CandidateSpec",
    "DeploymentSpec",
    "LifecycleEvent",
    "LifecycleSnapshot",
    "LifecycleState",
    "reduce_lifecycle",
]
