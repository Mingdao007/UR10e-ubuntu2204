"""Typed immutable runtime strategies for fresh R013 lineages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


DISABLED_STRATEGY_SCHEMA = "step5d.autotune-v4/r013-runtime-strategy-disabled-v1"
PHASE_TARGET_STRATEGY_SCHEMA = (
    "step5d.autotune-v4/r013-path-phase-force-target-correction-v1"
)
RUNTIME_STRATEGY_VERSION = 1

DISABLED_RUNTIME_STRATEGY: dict[str, Any] = {
    "schema": DISABLED_STRATEGY_SCHEMA,
    "version": RUNTIME_STRATEGY_VERSION,
    "enabled": False,
}


class R013RuntimeStrategyError(ValueError):
    """A runtime strategy is malformed, unbounded, or semantically ambiguous."""


def _validate_runtime_receipt(
    value: Any,
    *,
    expected_sha256: str,
    expected_enabled: bool,
) -> dict[str, Any]:
    fields = {
        "schema", "runtime_strategy_sha256", "enabled", "path_clock",
        "path_sample_count", "formal_sample_count", "minimum_effective_target_n",
        "maximum_applied_correction_n", "first_path_time_s", "last_path_time_s",
        "maximum_path_clock_gap_s", "violation_count",
        "exit_restored_to_unmodified_target", "safe_return_transition_observed",
        "exit_mode",
    }
    if type(value) is not dict or set(value) != fields:
        raise R013RuntimeStrategyError("R013 strategy sidecar runtime receipt fields differ")
    if (
        value.get("schema") != "step5d.autotune-v4/r013-runtime-strategy-receipt-v1"
        or value.get("runtime_strategy_sha256") != expected_sha256
        or value.get("enabled") is not expected_enabled
        or value.get("path_clock") != "runtime_desired_twist_path_time_s"
        or type(value.get("path_sample_count")) is not int
        or value["path_sample_count"] <= 0
        or type(value.get("formal_sample_count")) is not int
        or not 0 < value["formal_sample_count"] <= value["path_sample_count"]
        or type(value.get("violation_count")) is not int
        or value["violation_count"] != 0
        or value.get("exit_restored_to_unmodified_target") is not True
        or value.get("safe_return_transition_observed") is not True
        or value.get("exit_mode") != "safe_return"
    ):
        raise R013RuntimeStrategyError("R013 strategy sidecar runtime receipt differs")
    numeric = {
        key: _exact_number(value.get(key), key)
        for key in (
            "minimum_effective_target_n", "maximum_applied_correction_n",
            "first_path_time_s", "last_path_time_s", "maximum_path_clock_gap_s",
        )
    }
    if (
        not 3.75 <= numeric["minimum_effective_target_n"] <= 5.0
        or not 0.0 <= numeric["maximum_applied_correction_n"] <= 1.25
        or not 0.0 <= numeric["first_path_time_s"] <= 0.1
        or numeric["last_path_time_s"] < 59.9
        or not 0.0 <= numeric["maximum_path_clock_gap_s"] <= 0.08
    ):
        raise R013RuntimeStrategyError("R013 strategy sidecar runtime receipt bounds differ")
    return dict(value)


def _validate_physical_admission(value: Any, *, dispatch_id: str) -> dict[str, Any]:
    fields = {
        "schema", "dispatch_id", "candidate_token", "candidate_key",
        "attempt_sequence", "execution_id", "sealed_mae_n", "physical_eligible",
        "timing_gate", "motion_gate", "qualification_passed", "observation_uid",
        "sealed", "epoch_qualification_passed", "trial_admission_passed",
        "campaign_fingerprint",
    }
    if type(value) is not dict or set(value) != fields:
        raise R013RuntimeStrategyError("R013 strategy sidecar physical admission fields differ")
    if (
        value.get("schema") != "step5d.autotune-v4/r013-physical-admission-v1"
        or value.get("dispatch_id") != dispatch_id
        or type(value.get("candidate_token")) is not str
        or not value["candidate_token"]
        or type(value.get("candidate_key")) is not list
        or len(value["candidate_key"]) != 8
        or type(value["candidate_key"][3]) is not bool
        or any(
            type(value["candidate_key"][index]) not in (int, float)
            or not math.isfinite(float(value["candidate_key"][index]))
            for index in (0, 1, 2, 4, 5, 6, 7)
        )
        or type(value.get("attempt_sequence")) is not int
        or value["attempt_sequence"] <= 0
        or type(value.get("execution_id")) is not str
        or not value["execution_id"]
        or type(value.get("observation_uid")) is not str
        or not value["observation_uid"]
        or value.get("sealed") is not True
        or any(
            type(value.get(name)) is not bool
            for name in (
                "physical_eligible", "timing_gate", "motion_gate",
                "qualification_passed",
            )
        )
        or any(
            value.get(name) is not None and type(value.get(name)) is not bool
            for name in ("epoch_qualification_passed", "trial_admission_passed")
        )
        or (
            value.get("campaign_fingerprint") is not None
            and type(value.get("campaign_fingerprint")) is not dict
        )
    ):
        raise R013RuntimeStrategyError("R013 strategy sidecar physical admission differs")
    objective = _exact_number(value.get("sealed_mae_n"), "sealed_mae_n")
    if objective < 0.0:
        raise R013RuntimeStrategyError("R013 strategy sidecar physical admission MAE differs")
    return dict(value)


def _exact_number(value: Any, name: str) -> float:
    if type(value) not in (int, float):
        raise R013RuntimeStrategyError(f"{name} must be an exact JSON number")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise R013RuntimeStrategyError(f"{name} must be finite")
    return parsed


def validate_runtime_strategy(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return dict(DISABLED_RUNTIME_STRATEGY)
    if type(value) is not dict:
        raise R013RuntimeStrategyError("R013 runtime strategy must be a JSON object")
    if value.get("schema") == DISABLED_STRATEGY_SCHEMA:
        if value != DISABLED_RUNTIME_STRATEGY or any(
            type(value.get(key)) is not type(expected)
            for key, expected in DISABLED_RUNTIME_STRATEGY.items()
        ):
            raise R013RuntimeStrategyError("R013 disabled runtime strategy differs")
        return dict(DISABLED_RUNTIME_STRATEGY)
    required = {
        "schema", "version", "enabled", "interpolation", "base_target_n",
        "formal_window_s", "knot_times_s", "target_correction_n",
        "minimum_effective_target_n", "maximum_effective_target_n",
        "maximum_correction_n", "source",
    }
    if set(value) != required:
        raise R013RuntimeStrategyError("R013 phase-target strategy fields differ")
    if (
        value.get("schema") != PHASE_TARGET_STRATEGY_SCHEMA
        or type(value.get("version")) is not int
        or value.get("version") != RUNTIME_STRATEGY_VERSION
        or value.get("enabled") is not True
        or value.get("interpolation") != "linear_clamped"
    ):
        raise R013RuntimeStrategyError("R013 phase-target strategy contract differs")
    base = _exact_number(value.get("base_target_n"), "base_target_n")
    minimum = _exact_number(
        value.get("minimum_effective_target_n"), "minimum_effective_target_n"
    )
    maximum = _exact_number(
        value.get("maximum_effective_target_n"), "maximum_effective_target_n"
    )
    max_correction = _exact_number(value.get("maximum_correction_n"), "maximum_correction_n")
    if not (3.0 <= minimum < maximum == base == 5.0 and 0.0 < max_correction <= 1.25):
        raise R013RuntimeStrategyError("R013 phase-target safety bounds differ")
    window = value.get("formal_window_s")
    knots = value.get("knot_times_s")
    corrections = value.get("target_correction_n")
    if type(window) is not list or len(window) != 2:
        raise R013RuntimeStrategyError("R013 phase-target formal window differs")
    parsed_window = [_exact_number(item, "formal_window_s") for item in window]
    if parsed_window != [5.0, 60.0]:
        raise R013RuntimeStrategyError("R013 phase-target formal window differs")
    if type(knots) is not list or type(corrections) is not list or len(knots) != len(corrections):
        raise R013RuntimeStrategyError("R013 phase-target knots differ")
    parsed_knots = [_exact_number(item, "knot_times_s") for item in knots]
    parsed_corrections = [
        _exact_number(item, "target_correction_n") for item in corrections
    ]
    if len(parsed_knots) < 2 or any(
        right <= left for left, right in zip(parsed_knots, parsed_knots[1:])
    ):
        raise R013RuntimeStrategyError("R013 phase-target knot time order differs")
    if parsed_knots[0] < parsed_window[0] or parsed_knots[-1] > parsed_window[1]:
        raise R013RuntimeStrategyError("R013 phase-target knots exceed the formal window")
    if any(correction < 0.0 or correction > max_correction for correction in parsed_corrections):
        raise R013RuntimeStrategyError("R013 phase-target correction exceeds its bound")
    if any(base - correction < minimum for correction in parsed_corrections):
        raise R013RuntimeStrategyError("R013 phase-target effective target is unsafe")
    source = value.get("source")
    source_fields = {
        "estimator", "sealed_metric_semantics", "physical_ledger_sha256",
        "training_attempt_sequences", "training_row_sha256", "holdout_attempt_sequence",
        "holdout_observed_mae_n", "holdout_counterfactual_mae_n", "holdout_role",
        "direction_selection_used_holdout",
    }
    if type(source) is not dict or set(source) != source_fields:
        raise R013RuntimeStrategyError("R013 phase-target source fields differ")
    if source.get("estimator") != "positive_median_signed_error_5s_segments_v1":
        raise R013RuntimeStrategyError("R013 phase-target estimator differs")
    if source.get("sealed_metric_semantics") != (
        "force-mae-v2-sealed|formal=[5,60)|bin=0.1s|target=5N"
    ):
        raise R013RuntimeStrategyError("R013 phase-target metric semantics differ")
    if (
        source.get("holdout_role")
        != "numeric_profile_validation_only_not_pristine_confirmatory"
        or source.get("direction_selection_used_holdout") is not True
    ):
        raise R013RuntimeStrategyError("R013 phase-target holdout role differs")
    digest = source.get("physical_ledger_sha256")
    if type(digest) is not str or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise R013RuntimeStrategyError("R013 phase-target source digest differs")
    sequences = source.get("training_attempt_sequences")
    row_digests = source.get("training_row_sha256")
    if (
        type(sequences) is not list
        or type(row_digests) is not list
        or len(sequences) != len(row_digests)
        or len(sequences) < 2
        or any(type(item) is not int or item <= 0 for item in sequences)
        or sequences != sorted(set(sequences))
        or any(
            type(item) is not str
            or len(item) != 64
            or any(c not in "0123456789abcdef" for c in item)
            for item in row_digests
        )
    ):
        raise R013RuntimeStrategyError("R013 phase-target training identities differ")
    holdout = source.get("holdout_attempt_sequence")
    if type(holdout) is not int or holdout <= 0 or holdout in sequences:
        raise R013RuntimeStrategyError("R013 phase-target holdout identity differs")
    observed = _exact_number(source.get("holdout_observed_mae_n"), "holdout_observed_mae_n")
    counterfactual = _exact_number(
        source.get("holdout_counterfactual_mae_n"), "holdout_counterfactual_mae_n"
    )
    if not (0.0 <= counterfactual < observed):
        raise R013RuntimeStrategyError("R013 phase-target holdout evidence differs")
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False))


def runtime_strategy_sha256(value: Mapping[str, Any] | None) -> str:
    strategy = validate_runtime_strategy(value)
    encoded = json.dumps(
        strategy,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CompiledRuntimeStrategy:
    """Validated once, allocation-free inputs for the 500 Hz control path."""

    enabled: bool
    base_target_n: float
    minimum_effective_target_n: float
    formal_window_s: tuple[float, float]
    knot_times_s: tuple[float, ...]
    target_correction_n: tuple[float, ...]


def compile_runtime_strategy(
    value: Mapping[str, Any] | None,
) -> CompiledRuntimeStrategy:
    """Validate the external JSON contract once before motion starts."""

    parsed = validate_runtime_strategy(value)
    if parsed["enabled"] is False:
        return CompiledRuntimeStrategy(
            enabled=False,
            base_target_n=5.0,
            minimum_effective_target_n=3.75,
            formal_window_s=(5.0, 60.0),
            knot_times_s=(),
            target_correction_n=(),
        )
    return CompiledRuntimeStrategy(
        enabled=True,
        base_target_n=float(parsed["base_target_n"]),
        minimum_effective_target_n=float(parsed["minimum_effective_target_n"]),
        formal_window_s=tuple(float(item) for item in parsed["formal_window_s"]),
        knot_times_s=tuple(float(item) for item in parsed["knot_times_s"]),
        target_correction_n=tuple(
            float(item) for item in parsed["target_correction_n"]
        ),
    )


def target_correction_n_compiled(
    strategy: CompiledRuntimeStrategy,
    *,
    path_time_s: float,
    mode: str,
) -> float:
    """Compute a correction from an already validated immutable strategy."""

    if not strategy.enabled or mode != "path":
        return 0.0
    time_s = float(path_time_s)
    if not math.isfinite(time_s):
        raise R013RuntimeStrategyError("path_time_s must be finite")
    start, end = strategy.formal_window_s
    if time_s < start or time_s >= end:
        return 0.0
    knots = strategy.knot_times_s
    values = strategy.target_correction_n
    if time_s <= knots[0]:
        return values[0]
    if time_s >= knots[-1]:
        return values[-1]
    for left_index, (left, right) in enumerate(zip(knots, knots[1:])):
        if left <= time_s <= right:
            alpha = (time_s - left) / (right - left)
            return values[left_index] + alpha * (values[left_index + 1] - values[left_index])
    raise R013RuntimeStrategyError("R013 phase-target interpolation failed")


def target_correction_n(strategy: Mapping[str, Any], *, path_time_s: float, mode: str) -> float:
    compiled = compile_runtime_strategy(strategy)
    if not compiled.enabled or mode != "path":
        return 0.0
    return target_correction_n_compiled(
        compiled,
        path_time_s=_exact_number(path_time_s, "path_time_s"),
        mode=mode,
    )


class RuntimeStrategyReceiptLedger:
    """Small hash-chained sidecar binding strategy execution to physical truth."""

    schema = "step5d.autotune-v4/r013-runtime-strategy-sidecar-v1"

    def __init__(
        self,
        path: Path,
        *,
        campaign_id: str,
        run_id: str,
        attempt_id: str,
        strategy: Mapping[str, Any],
        must_exist: bool = False,
    ) -> None:
        if type(must_exist) is not bool:
            raise R013RuntimeStrategyError("R013 strategy sidecar must_exist differs")
        self.path = Path(path)
        parsed_strategy = validate_runtime_strategy(strategy)
        self._strategy_enabled = bool(parsed_strategy["enabled"])
        self.identity = {
            "campaign_id": str(campaign_id),
            "run_id": str(run_id),
            "attempt_id": str(attempt_id),
            "runtime_strategy_sha256": runtime_strategy_sha256(parsed_strategy),
        }
        if not all(self.identity.values()):
            raise R013RuntimeStrategyError("R013 strategy sidecar identity is incomplete")
        if self.path.is_symlink() or (self.path.exists() and not self.path.is_file()):
            raise R013RuntimeStrategyError("R013 strategy sidecar path is not a regular file")
        if self.path.exists():
            self._rows = self._load()
        elif must_exist:
            raise R013RuntimeStrategyError("R013 strategy sidecar is missing on resume")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            header = {
                "schema": self.schema,
                "record_type": "header",
                "sequence": 0,
                **self.identity,
            }
            with self.path.open("xb") as stream:
                stream.write(self._canonical(header) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._rows = (header,)

    @staticmethod
    def _canonical(value: Mapping[str, Any]) -> bytes:
        return json.dumps(
            dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")

    @classmethod
    def _row_hash(cls, value: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            cls._canonical({key: item for key, item in value.items() if key != "row_sha256"})
        ).hexdigest()

    def _load(self) -> tuple[dict[str, Any], ...]:
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if any(not line for line in lines):
            raise R013RuntimeStrategyError("R013 strategy sidecar contains a blank record")
        rows = tuple(json.loads(line) for line in lines)
        if not rows:
            raise R013RuntimeStrategyError("R013 strategy sidecar is empty")
        expected_header = {
            "schema": self.schema,
            "record_type": "header",
            "sequence": 0,
            **self.identity,
        }
        if rows[0] != expected_header:
            raise R013RuntimeStrategyError("R013 strategy sidecar header differs")
        previous = "0" * 64
        seen_attempts: set[int] = set()
        for sequence, row in enumerate(rows[1:], 1):
            required = {
                "schema", "record_type", "sequence", *self.identity,
                "attempt_sequence", "execution_id", "observation_uid", "dispatch_id",
                "physical_admission", "runtime_strategy_receipt", "previous_sha256",
                "row_sha256",
            }
            if set(row) != required or any(row.get(key) != value for key, value in self.identity.items()):
                raise R013RuntimeStrategyError("R013 strategy sidecar row fields differ")
            attempt = row.get("attempt_sequence")
            if (
                row.get("schema") != self.schema
                or row.get("record_type") != "receipt"
                or row.get("sequence") != sequence
                or type(attempt) is not int
                or attempt <= 0
                or attempt in seen_attempts
                or row.get("previous_sha256") != previous
                or row.get("row_sha256") != self._row_hash(row)
            ):
                raise R013RuntimeStrategyError("R013 strategy sidecar chain differs")
            admission = _validate_physical_admission(
                row.get("physical_admission"),
                dispatch_id=str(row.get("dispatch_id", "")),
            )
            receipt = _validate_runtime_receipt(
                row.get("runtime_strategy_receipt"),
                expected_sha256=self.identity["runtime_strategy_sha256"],
                expected_enabled=self._strategy_enabled,
            )
            if (
                admission.get("attempt_sequence") != attempt
                or admission.get("execution_id") != row.get("execution_id")
                or admission.get("observation_uid") != row.get("observation_uid")
                or admission.get("dispatch_id") != row.get("dispatch_id")
            ):
                raise R013RuntimeStrategyError("R013 strategy sidecar receipt binding differs")
            seen_attempts.add(attempt)
            previous = row["row_sha256"]
        return rows

    @property
    def rows(self) -> tuple[Mapping[str, Any], ...]:
        return self._rows

    def validate_campaign_records(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> None:
        """Cold-join every completed campaign attempt to exactly one sidecar row."""

        completed: list[Mapping[str, Any]] = []
        for record in records:
            if record.get("record_type") in {"observation", "rejected"}:
                payload = record.get("payload")
                if type(payload) is not dict:
                    raise R013RuntimeStrategyError(
                        "R013 strategy campaign completion payload differs"
                    )
                completed.append(payload)
        sidecar_rows = self._rows[1:]
        if len(completed) != len(sidecar_rows):
            raise R013RuntimeStrategyError(
                "R013 strategy sidecar/campaign completion count differs"
            )
        for payload, sidecar_row in zip(completed, sidecar_rows, strict=True):
            if (
                payload.get("dispatch_id") != sidecar_row.get("dispatch_id")
                or payload.get("runtime_strategy_sidecar_sha256")
                != sidecar_row.get("row_sha256")
                or payload.get("physical_admission")
                != sidecar_row.get("physical_admission")
                or payload.get("runtime_strategy_receipt")
                != sidecar_row.get("runtime_strategy_receipt")
            ):
                raise R013RuntimeStrategyError(
                    "R013 strategy sidecar/campaign receipt binding differs"
                )

    def append(
        self,
        *,
        dispatch_id: str,
        physical_admission: Mapping[str, Any],
        runtime_strategy_receipt: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        attempt = physical_admission.get("attempt_sequence")
        if type(attempt) is not int or attempt <= 0:
            raise R013RuntimeStrategyError("R013 strategy sidecar attempt differs")
        if any(row.get("attempt_sequence") == attempt for row in self._rows[1:]):
            raise R013RuntimeStrategyError("R013 strategy sidecar attempt is duplicated")
        parsed_receipt = _validate_runtime_receipt(
            runtime_strategy_receipt,
            expected_sha256=self.identity["runtime_strategy_sha256"],
            expected_enabled=self._strategy_enabled,
        )
        parsed_admission = _validate_physical_admission(
            physical_admission,
            dispatch_id=dispatch_id,
        )
        row = {
            "schema": self.schema,
            "record_type": "receipt",
            "sequence": len(self._rows),
            **self.identity,
            "attempt_sequence": attempt,
            "execution_id": str(physical_admission.get("execution_id", "")),
            "observation_uid": str(physical_admission.get("observation_uid", "")),
            "dispatch_id": str(dispatch_id),
            "physical_admission": parsed_admission,
            "runtime_strategy_receipt": parsed_receipt,
            "previous_sha256": (
                "0" * 64 if len(self._rows) == 1 else str(self._rows[-1]["row_sha256"])
            ),
        }
        row["row_sha256"] = self._row_hash(row)
        with self.path.open("ab") as stream:
            stream.write(self._canonical(row) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        self._rows = (*self._rows, row)
        return row


__all__ = [
    "DISABLED_RUNTIME_STRATEGY", "DISABLED_STRATEGY_SCHEMA",
    "PHASE_TARGET_STRATEGY_SCHEMA", "R013RuntimeStrategyError",
    "RuntimeStrategyReceiptLedger",
    "CompiledRuntimeStrategy", "compile_runtime_strategy", "target_correction_n",
    "target_correction_n_compiled", "runtime_strategy_sha256",
    "validate_runtime_strategy",
]
