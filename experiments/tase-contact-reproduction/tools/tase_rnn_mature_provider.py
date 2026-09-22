"""Thin binding for the mature online TASE RNN control path.

The provider owns identity and delegation only.  Control equations remain in
the validated outer-loop and strict-RNN modules used by the existing bridge.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from step5c_strict_rnn import StrictTaseRnnSolver
from step5d_paper_outer_loop import (
    Step5dOuterLoopConfig,
    Step5dOuterLoopInputs,
    Step5dOuterLoopOutput,
    Step5dOuterLoopState,
    compute_step5d_outer_loop,
    rnn_target_state_from_outer_loop,
)


PROVIDER_ID = "TASE_RNN_MATURE"
PROVIDER_ENV = "TASE_CONTROL_PROVIDER"
TRAJECTORY_ENV = "TASE_TRAJECTORY"
EXPECTED_TRAJECTORY = "figure8"
SOURCE_MODULES = (
    "tools/step5d_paper_outer_loop.py",
    "tools/step5c_strict_rnn.py",
)


class TaseRnnMatureProvider:
    """Delegate one control tick to the existing mature implementation."""

    provider_id = PROVIDER_ID

    def validate_binding(self, environment: Mapping[str, str] | None = None) -> None:
        env = os.environ if environment is None else environment
        provider = env.get(PROVIDER_ENV, PROVIDER_ID)
        if provider != PROVIDER_ID:
            raise RuntimeError(
                f"unsupported TASE control provider {provider!r}; expected {PROVIDER_ID}"
            )
        trajectory = env.get(TRAJECTORY_ENV)
        if trajectory is not None and trajectory != EXPECTED_TRAJECTORY:
            raise RuntimeError(
                f"TASE_RNN_MATURE is bound to {EXPECTED_TRAJECTORY!r}, got {trajectory!r}"
            )

    def compute_outer_loop(
        self,
        config: Step5dOuterLoopConfig,
        state: Step5dOuterLoopState,
        inputs: Step5dOuterLoopInputs,
        **kwargs: Any,
    ) -> Step5dOuterLoopOutput:
        self.validate_binding()
        return compute_step5d_outer_loop(config, state, inputs, **kwargs)

    def target_state(self, output: Step5dOuterLoopOutput, **kwargs: Any) -> Any:
        self.validate_binding()
        return rnn_target_state_from_outer_loop(output, **kwargs)

    @staticmethod
    def build_solver(config: Any) -> StrictTaseRnnSolver:
        """Construct the same strict solver used by the mature bridge."""

        return StrictTaseRnnSolver(config)

    def binding_receipt(self, experiment_root: Path) -> dict[str, Any]:
        return {
            "provider_id": PROVIDER_ID,
            "trajectory": EXPECTED_TRAJECTORY,
            "source_modules": [str(experiment_root / path) for path in SOURCE_MODULES],
            "delegates_control": True,
            "owns_solver": False,
            "owns_supervisor": False,
            "owns_recovery": False,
        }


def provider_binding_receipt(experiment_root: Path) -> dict[str, Any]:
    return TaseRnnMatureProvider().binding_receipt(experiment_root)
