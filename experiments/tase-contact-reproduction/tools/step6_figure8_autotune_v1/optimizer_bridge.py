"""Control-safe bridge to the isolated Figure-eight CUDA optimizer."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from .core import CompleteCandidateV1, ProductionProposalUnavailable, SOBOL_POOL_SIZE


class SubprocessFigureEightProposalProviderV1:
    """Invoke one bounded optimizer subprocess per serial qLogNEI ask."""

    def __init__(
        self,
        *,
        optimizer_python: Path,
        state_dir: Path,
        timeout_s: float = 180.0,
        seed: int = 6016,
    ) -> None:
        # Preserve the venv launcher path.  Resolving a normal venv symlink to
        # /usr/bin/python discards pyvenv.cfg discovery and silently removes
        # the production BoTorch/CUDA environment.
        self.optimizer_python = Path(
            os.path.abspath(os.fspath(optimizer_python))
        )
        self.optimizer_python_target = self.optimizer_python.resolve(strict=True)
        self.state_dir = Path(state_dir).resolve()
        self.timeout_s = float(timeout_s)
        self.seed = int(seed)
        self.invocation_count = 0
        self.last_scored_pool: tuple[dict[str, Any], ...] = ()
        self.last_observed_posterior_probability: dict[str, float] = {}
        self.last_observed_posterior_mean_n: dict[str, float] = {}
        self.last_observed_posterior_variance_n2: dict[str, float] = {}
        if (
            not self.optimizer_python.is_file()
            or not self.optimizer_python_target.is_file()
            or not os.access(self.optimizer_python, os.X_OK)
        ):
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer Python is not an executable regular file"
            )
        if self.timeout_s <= 0.0:
            raise ProductionProposalUnavailable("Figure-eight optimizer timeout is invalid")
        if self.state_dir.exists():
            serials: list[int] = []
            for path in self.state_dir.glob("qlognei-*-request.json"):
                try:
                    serial = int(path.name.split("-")[1])
                except (IndexError, ValueError):
                    raise ProductionProposalUnavailable(
                        "Figure-eight optimizer state contains an unknown request"
                    )
                serials.append(serial)
            response_serials: set[int] = set()
            for path in self.state_dir.glob("qlognei-*-response.json"):
                try:
                    response_serials.add(int(path.name.split("-")[1]))
                except (IndexError, ValueError) as exc:
                    raise ProductionProposalUnavailable(
                        "Figure-eight optimizer state contains an unknown response"
                    ) from exc
            if not response_serials.issubset(set(serials)):
                raise ProductionProposalUnavailable(
                    "Figure-eight optimizer response lacks its request"
                )
            if serials and set(serials) != set(range(1, max(serials) + 1)):
                raise ProductionProposalUnavailable(
                    "Figure-eight optimizer invocation history has a gap"
                )
            self.invocation_count = max(serials, default=0)

    def __call__(
        self,
        pool: Sequence[Mapping[str, Any]],
        *,
        observations: Sequence[Mapping[str, Any]],
        block: str,
        evaluated_keys: Iterable[str] = (),
        pending_keys: Iterable[str] = (),
        pending_candidates: Sequence[Mapping[str, Any]] = (),
    ) -> Mapping[str, Any]:
        if len(pool) != SOBOL_POOL_SIZE:
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer bridge requires exactly 128 candidates"
            )
        parsed = tuple(CompleteCandidateV1.from_mapping(item) for item in pool)
        evaluated_key_set = {str(item) for item in evaluated_keys}
        pending_key_set = {str(item) for item in pending_keys}
        excluded = evaluated_key_set | pending_key_set
        if len({item.candidate_key for item in parsed}) != SOBOL_POOL_SIZE or any(
            item.candidate_key in excluded for item in parsed
        ):
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer bridge pool is not fresh"
            )
        parsed_pending = tuple(
            CompleteCandidateV1.from_mapping(item) for item in pending_candidates
        )
        if {item.candidate_key for item in parsed_pending} != pending_key_set:
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer pending candidates do not match pending keys"
            )
        self.invocation_count += 1
        self.state_dir.mkdir(parents=True, exist_ok=True)
        prefix = f"qlognei-{self.invocation_count:04d}"
        request_path = self.state_dir / f"{prefix}-request.json"
        response_path = self.state_dir / f"{prefix}-response.json"
        if request_path.exists() or response_path.exists():
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer invocation path already exists"
            )
        request = {
            "schema": "step6.autotune/figure8-qlognei-request-v1",
            "version": 1,
            "block": str(block),
            "pool": [item.as_dict() for item in parsed],
            "observations": [dict(item) for item in observations],
            "evaluated_keys": sorted(evaluated_key_set),
            "pending_keys": sorted(pending_key_set),
            "pending_candidates": [item.as_dict() for item in parsed_pending],
            "seed": self.seed + self.invocation_count,
        }
        request_path.write_text(
            json.dumps(request, sort_keys=True, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        worker = Path(__file__).with_name("optimizer_worker.py")
        completed = subprocess.run(
            [
                str(self.optimizer_python),
                str(worker),
                "--request",
                str(request_path),
                "--response",
                str(response_path),
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=self.timeout_s,
            check=False,
        )
        if not response_path.is_file():
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer subprocess produced no response"
            )
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer response is unreadable"
            ) from exc
        if completed.returncode != 0 or response.get("schema") != "step6.autotune/figure8-qlognei-proposal-v1":
            detail = str(response.get("error") or completed.stderr.strip())
            raise ProductionProposalUnavailable(
                f"Figure-eight optimizer subprocess failed: {detail}"
            )
        selected = CompleteCandidateV1.from_mapping(response.get("candidate"))
        if selected.candidate_key not in {item.candidate_key for item in parsed}:
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer selected outside the fresh pool"
            )
        scored = response.get("scored_pool")
        if not isinstance(scored, list) or len(scored) != SOBOL_POOL_SIZE:
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer response lacks 128 scored candidates"
            )
        self.last_scored_pool = tuple(dict(item) for item in scored)
        observed_probability = response.get("observed_posterior_probability")
        if not isinstance(observed_probability, Mapping):
            raise ProductionProposalUnavailable(
                "Figure-eight optimizer response lacks observed posterior probabilities"
            )
        expected_observed = {
            CompleteCandidateV1.from_mapping(item["candidate"]).candidate_key
            for item in observations
        }
        if set(observed_probability) != expected_observed:
            raise ProductionProposalUnavailable(
                "Figure-eight observed posterior probability keys differ"
            )
        parsed_probability = {
            str(key): float(value) for key, value in observed_probability.items()
        }
        if any(not 0.0 <= value <= 1.0 for value in parsed_probability.values()):
            raise ProductionProposalUnavailable(
                "Figure-eight observed posterior probability is outside [0,1]"
            )
        self.last_observed_posterior_probability = parsed_probability
        observed_mean = response.get("observed_posterior_mean_n")
        if not isinstance(observed_mean, Mapping) or set(observed_mean) != expected_observed:
            raise ProductionProposalUnavailable(
                "Figure-eight observed posterior mean keys differ"
            )
        parsed_mean = {str(key): float(value) for key, value in observed_mean.items()}
        if any(not math.isfinite(value) for value in parsed_mean.values()):
            raise ProductionProposalUnavailable(
                "Figure-eight observed posterior mean is nonfinite"
            )
        self.last_observed_posterior_mean_n = parsed_mean
        observed_variance = response.get("observed_posterior_variance_n2")
        if not isinstance(observed_variance, Mapping):
            raise ProductionProposalUnavailable(
                "Figure-eight qLogNEI response lacks observed posterior variance"
            )
        parsed_variance: dict[str, float] = {}
        for key, value in observed_variance.items():
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ProductionProposalUnavailable(
                    "Figure-eight observed posterior variance is invalid"
                ) from exc
            if not math.isfinite(number) or number <= 0.0:
                raise ProductionProposalUnavailable(
                    "Figure-eight observed posterior variance is non-positive"
                )
            parsed_variance[str(key)] = number
        if set(parsed_variance) != set(parsed_mean):
            raise ProductionProposalUnavailable(
                "Figure-eight observed posterior mean/variance identities differ"
            )
        self.last_observed_posterior_variance_n2 = parsed_variance
        return response


__all__ = ["SubprocessFigureEightProposalProviderV1"]
