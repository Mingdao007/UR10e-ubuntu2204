#!/usr/bin/env python3
"""Strict Step5c TASE RNN gate.

This module intentionally refuses to produce live commands until the paper
truth contract is verified against the PDF. It prevents a DLS or placeholder
controller from being exposed as a finite-time RNN.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXPERIMENT_ROOT = Path(__file__).resolve().parents[1]
PAPER_TRUTH_PATH = EXPERIMENT_ROOT / "config" / "step5c_tase_paper_truth.json"
STRICT_DRYRUN_STAGE_ID = "step5c_strict_rnn_dryrun_v1"
STATUS_PAPER_TRUTH_PENDING = 91.0


class PaperTruthPendingError(RuntimeError):
    """Raised when strict RNN equations or parameters are not PDF-verified."""


@dataclass(frozen=True)
class StrictRnnConfig:
    paper_truth_path: Path = PAPER_TRUTH_PATH
    qdot_limit_rad_s: float = 0.20


@dataclass(frozen=True)
class StrictRnnCommandResult:
    qdot: tuple[float, float, float, float, float, float]
    solver_status: float
    residual_norm: float


def load_paper_truth(path: Path = PAPER_TRUTH_PATH) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PaperTruthPendingError(f"paper truth file missing: {path}") from exc
    except json.JSONDecodeError as exc:
        raise PaperTruthPendingError(f"paper truth file is not valid JSON: {path}: {exc}") from exc


def pending_paper_truth_fields(payload: dict[str, Any]) -> list[str]:
    pending = list(payload.get("pending_pdf_verify", []))
    for section_name, section in payload.get("sections", {}).items():
        if isinstance(section, dict):
            for field in section.get("pending_pdf_verify", []):
                pending.append(f"{section_name}.{field}")
    return sorted(str(field) for field in pending)


def assert_paper_truth_verified(payload: dict[str, Any]) -> None:
    pending = pending_paper_truth_fields(payload)
    if pending:
        raise PaperTruthPendingError(
            "strict Step5c RNN is blocked by unverified paper-truth fields: "
            + ", ".join(pending)
        )
    if not payload.get("strict_rnn_enabled", False):
        raise PaperTruthPendingError("strict Step5c RNN is disabled in paper truth config")


def _finite_vector(values: Any, length: int, name: str) -> tuple[float, ...]:
    try:
        vector = tuple(float(value) for value in values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a finite length-{length} vector") from exc
    if len(vector) != length or any(not math.isfinite(value) for value in vector):
        raise ValueError(f"{name} must be a finite length-{length} vector")
    return vector


class StrictTaseRnnSolver:
    """Paper-gated strict RNN solver interface.

    The numerical controller body is intentionally not implemented until
    `step5c_tase_paper_truth.json` closes every PDF verification item.
    """

    def __init__(self, config: StrictRnnConfig = StrictRnnConfig()) -> None:
        self.config = config
        self.paper_truth = load_paper_truth(config.paper_truth_path)
        assert_paper_truth_verified(self.paper_truth)

    def solve(self, *, actual_q: Any, actual_qd: Any, target_state: dict[str, Any]) -> StrictRnnCommandResult:
        _finite_vector(actual_q, 6, "actual_q")
        _finite_vector(actual_qd, 6, "actual_qd")
        if not isinstance(target_state, dict):
            raise ValueError("target_state must be a dict")
        raise NotImplementedError("strict Step5c RNN equations are not implemented yet")


def main() -> int:
    payload = load_paper_truth()
    assert_paper_truth_verified(payload)
    raise SystemExit("strict Step5c RNN equations are not implemented yet")


if __name__ == "__main__":
    raise SystemExit(main())
