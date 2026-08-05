"""Offline grey-box identification of the Step5d contact plant.

The 18 sealed r006 observations recorded the *filtered* normal force at 500 Hz
but never the TCP pose, so the plant cannot be read off directly.  It can be
reconstructed: the normal channel of the production outer loop is an exact
scalar recursion driven only by the recorded force and the trial's own
(P, D, kf), so the commanded normal displacement of every sealed trial is
recoverable without any additional measurement.

What remains unknown is the environment: one shared contact stiffness ``k``,
one shared surface profile ``g(t)`` along the time-locked XY path, and one
contact-establishment offset ``delta_i`` per trial.  Those are identifiable
because the surface is the same in all trials while (P, D, tau) are not.

Heavy identification imports (scipy) stay lazy so live host admission can
import ``greybox.receipt`` without pulling the offline fit stack.
"""

from .reconstruct import (
    BundleTrace,
    GreyboxError,
    load_bundle_trace,
    load_run_traces,
    normal_velocity,
    verify_scalar_kernel_against_production,
)
from .plant import (
    FORMAL_BINS,
    FORMAL_WINDOW_S,
    PlantParameters,
    filter_alpha,
    objective_mae_n,
    simulate_trial,
)
from .receipt import SCHEMA, GreyboxReceipt, build_receipt, load_receipt

__all__ = [
    "FORMAL_BINS",
    "FORMAL_WINDOW_S",
    "SCHEMA",
    "BundleTrace",
    "GreyboxError",
    "GreyboxReceipt",
    "IdentificationResult",
    "PlantParameters",
    "build_receipt",
    "filter_alpha",
    "identify_plant",
    "identify_run",
    "load_bundle_trace",
    "load_receipt",
    "load_run_traces",
    "normal_velocity",
    "objective_mae_n",
    "simulate_trial",
    "verify_scalar_kernel_against_production",
]


def __getattr__(name: str):
    if name in {"IdentificationResult", "identify_plant", "identify_run"}:
        from .identify import IdentificationResult, identify_plant, identify_run

        values = {
            "IdentificationResult": IdentificationResult,
            "identify_plant": identify_plant,
            "identify_run": identify_run,
        }
        return values[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
