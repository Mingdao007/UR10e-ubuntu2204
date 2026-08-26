"""Typed R011 exact/censored observations and the causal early-end protocol."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping, Sequence

from .common import R012ValueError, finite, freeze_tree, json_tree


CENSOR_SCHEMA = "step5d.autotune-v4/r012-censored-observation-v1"
EXACT_SCHEMA = "step5d.autotune-v4/r012-exact-observation-v1"
PROTOCOL_SCHEMA = "step5d.autotune-v4/r012-censored-observation-protocol-v1"
DENOMINATOR_BINS = 550
R009_CLOSED_BINS = DENOMINATOR_BINS
BIN_WIDTH_S = 0.1
KAPPA = 2.0
# Censor policy has one value; none of these aliases define a sigmoid.
KAPPA_START = KAPPA
KAPPA_END = KAPPA
KAPPA_MIDPOINT = KAPPA
KAPPA_STEEPNESS = 0.0
GUARD_BINS = 55
GUARD_FRACTION = GUARD_BINS / DENOMINATOR_BINS

# Censoring is deliberately limited to novel BO trials.  Retests and all
# qualification/confirmation runs must complete the full formal window.
ACTIVE_RUN_KINDS = frozenset({"BO_TRIAL", "NOVEL_BO"})
NON_ABORT_RUN_KINDS = frozenset({
    "QUALIFICATION",
    "ANCHOR",
    "REFERENCE",
    "CHALLENGER_CONFIRM",
    "INCUMBENT_RETEST",
    "NON_BO",
    "CANARY_I_OFF",
    "CANARY_I_ON",
    "I_CANARY",
})


class CensoringError(R012ValueError):
    """An observation or handshake violates the R011 censor contract."""


def _errors(values: Sequence[float]) -> tuple[float, ...]:
    result = tuple(finite(value, "closed bin absolute error") for value in values)
    if any(value < 0.0 for value in result):
        raise CensoringError("closed bin absolute errors must be non-negative")
    return result


def prefix_mean(bin_absolute_errors: Sequence[float], closed_bin_count: int | None = None) -> float:
    """Causal trigger statistic: the arithmetic mean of the closed prefix."""

    values = _errors(bin_absolute_errors)
    count = len(values) if closed_bin_count is None else closed_bin_count
    if isinstance(count, bool) or count <= 0 or count > len(values):
        raise CensoringError("prefix must contain at least one supplied closed bin")
    return sum(values[:count]) / float(count)


def causal_lower_bound(bin_absolute_errors: Sequence[float], closed_bin_count: int, *, denominator_bins: int = DENOMINATOR_BINS) -> float:
    """Stored censor lower bound: closed-prefix sum divided by all 550 bins."""

    if isinstance(closed_bin_count, bool) or not 0 <= closed_bin_count <= DENOMINATOR_BINS:
        raise CensoringError("closed_bin_count must be within 0..550")
    if denominator_bins != DENOMINATOR_BINS or len(bin_absolute_errors) < closed_bin_count:
        raise CensoringError("R011 lower-bound denominator/count differs")
    return sum(_errors(bin_absolute_errors)[:closed_bin_count]) / float(DENOMINATOR_BINS)


def kappa_for_progress(progress_fraction: float) -> float:
    """Compatibility entrypoint with constant-kappa, never a sigmoid."""

    progress = finite(progress_fraction, "progress_fraction")
    if not 0.0 <= progress <= 1.0:
        raise CensoringError("progress_fraction must be within [0,1]")
    return KAPPA


@dataclass(frozen=True)
class CensoredMAEAccumulator:
    closed_absolute_errors: tuple[float, ...] = ()
    denominator_bins: int = DENOMINATOR_BINS

    def __post_init__(self) -> None:
        if self.denominator_bins != DENOMINATOR_BINS or len(self.closed_absolute_errors) > DENOMINATOR_BINS:
            raise CensoringError("censored accumulator denominator/count differs")
        object.__setattr__(self, "closed_absolute_errors", _errors(self.closed_absolute_errors))

    @property
    def closed_bin_count(self) -> int:
        return len(self.closed_absolute_errors)

    @property
    def prefix_mean_n(self) -> float:
        return prefix_mean(self.closed_absolute_errors)

    @property
    def lower_bound_n(self) -> float:
        return causal_lower_bound(self.closed_absolute_errors, self.closed_bin_count)

    def close_bin(self, absolute_error: float) -> "CensoredMAEAccumulator":
        if self.closed_bin_count >= DENOMINATOR_BINS:
            raise CensoringError("all 550 bins are already closed")
        return CensoredMAEAccumulator(self.closed_absolute_errors + (finite(absolute_error, "absolute_error"),), DENOMINATOR_BINS)


@dataclass(frozen=True)
class CensorProtocol:
    denominator_bins: int = DENOMINATOR_BINS
    kappa: float = KAPPA
    guard_bins: int = GUARD_BINS
    mode: str = "active_per_trial"
    active_early_abort_allowed: bool = True
    schema: str = PROTOCOL_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != PROTOCOL_SCHEMA or self.denominator_bins != DENOMINATOR_BINS or self.guard_bins != GUARD_BINS:
            raise CensoringError("R011 censor protocol denominator/guard differs")
        if not math.isclose(finite(self.kappa, "kappa"), KAPPA, rel_tol=0.0, abs_tol=0.0) or self.mode != "active_per_trial" or self.active_early_abort_allowed is not True:
            raise CensoringError("R011 censor protocol is not constant-kappa active policy")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": "r012-constant-kappa-v1",
            "denominator_bins": self.denominator_bins,
            "bin_width_s": BIN_WIDTH_S,
            "guard_bins": self.guard_bins,
            "guard_fraction": GUARD_FRACTION,
            "kappa": self.kappa,
            "trigger_statistic": "causal_prefix_mean_n=sum(closed_bin_abs_errors)/closed_bin_count",
            "stored_lower_bound": "sum(closed_bin_abs_errors)/550",
            "mode": self.mode,
            "active_early_abort_allowed": self.active_early_abort_allowed,
            "eligible_run_kinds": sorted(ACTIVE_RUN_KINDS),
            "excluded_run_kinds": sorted(NON_ABORT_RUN_KINDS),
        }


@dataclass(frozen=True)
class CensorDecision:
    active: bool
    triggered: bool
    run_kind: str
    closed_bin_count: int
    prefix_mean_n: float | None
    lower_bound_n: float | None
    confirmed_incumbent_mean_n: float | None
    kappa: float
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "step5d.autotune-v4/r012-censor-decision-v1",
            "active": self.active,
            "triggered": self.triggered,
            "run_kind": self.run_kind,
            "closed_bin_count": self.closed_bin_count,
            "prefix_mean_n": self.prefix_mean_n,
            "lower_bound_n": self.lower_bound_n,
            "confirmed_incumbent_mean_n": self.confirmed_incumbent_mean_n,
            "kappa": self.kappa,
            "reason": self.reason,
        }


def evaluate_censor_prefix(
    bin_absolute_errors: Sequence[float],
    *,
    run_kind: str,
    confirmed_incumbent_mean_n: float | None,
    protocol: CensorProtocol = CensorProtocol(),
    novel_bo: bool = True,
) -> CensorDecision:
    values = _errors(bin_absolute_errors)
    count = len(values)
    active = run_kind in ACTIVE_RUN_KINDS and novel_bo and confirmed_incumbent_mean_n is not None
    if count < protocol.guard_bins:
        return CensorDecision(active, False, run_kind, count, None, None, confirmed_incumbent_mean_n, protocol.kappa, "guard_not_reached")
    prefix = prefix_mean(values)
    lower = causal_lower_bound(values, count)
    if not active:
        reason = "run_kind_excluded" if run_kind not in ACTIVE_RUN_KINDS else "no_confirmed_incumbent"
        return CensorDecision(False, False, run_kind, count, prefix, lower, confirmed_incumbent_mean_n, protocol.kappa, reason)
    triggered = prefix > protocol.kappa * float(confirmed_incumbent_mean_n)
    return CensorDecision(True, triggered, run_kind, count, prefix, lower, confirmed_incumbent_mean_n, protocol.kappa, "prefix_mean_exceeded" if triggered else "prefix_mean_within_bound")


@dataclass(frozen=True)
class ExactObservation:
    dispatch_id: str
    candidate: Mapping[str, Any]
    objective_n: float
    campaign_id: str
    run_id: str
    attempt_id: str
    completed: bool = True
    sealed: bool = True
    schema: str = EXACT_SCHEMA
    full_observation: bool = True
    eligible: bool = True
    observation_variance_n2: float = 0.01
    kind: str = "BO_TRIAL"
    campaign_epoch: str = "r012"

    def __post_init__(self) -> None:
        if self.schema != EXACT_SCHEMA or not all(isinstance(value, str) and value for value in (self.dispatch_id, self.campaign_id, self.run_id, self.attempt_id)) or self.completed is not True or self.sealed is not True:
            raise CensoringError("exact observation is not a completed sealed R011 row")
        if not self.full_observation or not self.eligible:
            raise CensoringError("exact observation must be full and eligible")
        finite(self.objective_n, "objective_n")
        variance = finite(self.observation_variance_n2, "observation_variance_n2")
        if self.objective_n < 0.0 or variance <= 0.0:
            raise CensoringError("exact objective/noise is invalid")
        object.__setattr__(self, "candidate", freeze_tree(json_tree(self.candidate)))

    @property
    def censored(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "dispatch_id": self.dispatch_id, "candidate": json_tree(self.candidate),
            "objective_n": self.objective_n, "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id,
            "completed": True, "sealed": True, "full_observation": True, "eligible": True,
            "observation_variance_n2": self.observation_variance_n2, "kind": self.kind, "campaign_epoch": self.campaign_epoch,
            "censored": False,
        }


@dataclass(frozen=True)
class CensoredObservation:
    dispatch_id: str
    candidate: Mapping[str, Any]
    lower_bound_n: float
    watermark_s: float
    closed_bin_count: int
    denominator_bins: int
    kappa: float
    incumbent_threshold_n: float
    campaign_id: str
    run_id: str
    attempt_id: str
    completed: bool = True
    sealed: bool = True
    nontrainable: bool = True
    noncontrol: bool = True
    noncompletion: bool = True
    schema: str = CENSOR_SCHEMA
    prefix_mean_n: float | None = None
    kind: str = "BO_TRIAL"
    ack_sequence: int | None = None
    return_guard_closed: bool = True
    home_closed: bool = True
    safe_return_closed: bool = True

    def __post_init__(self) -> None:
        if self.schema != CENSOR_SCHEMA or not all(isinstance(value, str) and value for value in (self.dispatch_id, self.campaign_id, self.run_id, self.attempt_id)) or self.completed is not True or self.sealed is not True:
            raise CensoringError("censored observation is not a completed sealed R011 row")
        lower = finite(self.lower_bound_n, "lower_bound_n")
        watermark = finite(self.watermark_s, "watermark_s")
        threshold = finite(self.incumbent_threshold_n, "incumbent_threshold_n")
        if lower < 0.0 or watermark < 0.0 or threshold < 0.0 or self.denominator_bins != DENOMINATOR_BINS or isinstance(self.closed_bin_count, bool) or not GUARD_BINS <= self.closed_bin_count <= DENOMINATOR_BINS:
            raise CensoringError("censored bound/watermark/bin semantics are invalid")
        if not math.isclose(finite(self.kappa, "kappa"), KAPPA, rel_tol=0.0, abs_tol=0.0) or not all(value is True for value in (self.nontrainable, self.noncontrol, self.noncompletion, self.return_guard_closed, self.home_closed, self.safe_return_closed)):
            raise CensoringError("censored row authority/closure flags differ")
        if self.prefix_mean_n is None or finite(self.prefix_mean_n, "prefix_mean_n") < 0.0:
            raise CensoringError("censored observation must carry the causal prefix mean")
        if not isinstance(self.ack_sequence, int) or isinstance(self.ack_sequence, bool) or self.ack_sequence <= 0:
            raise CensoringError("censored observation must carry a positive ack sequence")
        if self.kind not in ACTIVE_RUN_KINDS:
            raise CensoringError("only novel BO runs may produce censored observations")
        expected_lower = float(self.prefix_mean_n) * self.closed_bin_count / DENOMINATOR_BINS
        if not math.isclose(self.lower_bound_n, expected_lower, rel_tol=0.0, abs_tol=1e-12):
            raise CensoringError("censored prefix mean and stored lower bound are conflated")
        object.__setattr__(self, "candidate", freeze_tree(json_tree(self.candidate)))

    @property
    def censored(self) -> bool:
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "dispatch_id": self.dispatch_id, "candidate": json_tree(self.candidate),
            "lower_bound_n": self.lower_bound_n, "prefix_mean_n": self.prefix_mean_n, "watermark_s": self.watermark_s,
            "closed_bin_count": self.closed_bin_count, "denominator_bins": self.denominator_bins, "kappa": self.kappa,
            "incumbent_threshold_n": self.incumbent_threshold_n, "campaign_id": self.campaign_id, "run_id": self.run_id, "attempt_id": self.attempt_id,
            "completed": True, "sealed": True,
            "nontrainable": True, "noncontrol": True, "noncompletion": True, "kind": self.kind,
            "ack_sequence": self.ack_sequence, "return_guard_closed": True, "home_closed": True,
            "safe_return_closed": True, "censored": True,
        }


Observation = ExactObservation | CensoredObservation


def validate_observation(value: Observation | Mapping[str, Any]) -> Observation:
    if isinstance(value, (ExactObservation, CensoredObservation)):
        return value
    if not isinstance(value, Mapping):
        raise CensoringError("observation must be typed")
    schema = value.get("schema")
    if schema == EXACT_SCHEMA:
        required = set(ExactObservation("d", {}, 0.0, "c", "r", "a").as_dict())
        if set(value) != required or value.get("censored") is not False:
            raise CensoringError("exact observation fields differ")
        return ExactObservation(value["dispatch_id"], value["candidate"], value["objective_n"], value["campaign_id"], value["run_id"], value["attempt_id"], value["completed"], value["sealed"], value["schema"], value["full_observation"], value["eligible"], value["observation_variance_n2"], value["kind"], value["campaign_epoch"])
    if schema == CENSOR_SCHEMA:
        required = set(CensoredObservation("d", {}, 0.0, 0.0, GUARD_BINS, DENOMINATOR_BINS, KAPPA, 0.0, "c", "r", "a", prefix_mean_n=0.0, ack_sequence=1).as_dict())
        if set(value) != required or value.get("censored") is not True:
            raise CensoringError("censored observation fields differ")
        return CensoredObservation(value["dispatch_id"], value["candidate"], value["lower_bound_n"], value["watermark_s"], value["closed_bin_count"], value["denominator_bins"], value["kappa"], value["incumbent_threshold_n"], value["campaign_id"], value["run_id"], value["attempt_id"], value["completed"], value["sealed"], value["nontrainable"], value["noncontrol"], value["noncompletion"], value["schema"], value["prefix_mean_n"], value["kind"], value["ack_sequence"], value["return_guard_closed"], value["home_closed"], value["safe_return_closed"])
    raise CensoringError("observation schema is neither exact nor censored")


def legacy_gp_training_rows(observations: Sequence[Observation | Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Return only exact rows; censored rows are evaluated but never GP rows."""

    return tuple(row.as_dict() for row in (validate_observation(value) for value in observations) if isinstance(row, ExactObservation))


def require_exact_for_legacy_gp(value: Observation | Mapping[str, Any]) -> ExactObservation:
    row = validate_observation(value)
    if isinstance(row, CensoredObservation):
        raise CensoringError("censored observation cannot become an exact GP row")
    return row


def hypothetical_ts_value(value: Observation | Mapping[str, Any]) -> float:
    row = validate_observation(value)
    return row.lower_bound_n if isinstance(row, CensoredObservation) else row.objective_n


class HandshakeState(str, Enum):
    DISARMED = "DISARMED"
    PATH = "PATH"
    PATH_COMPLETE = "PATH_COMPLETE"
    RETURN_HOME = "RETURN_HOME"
    CLOSED = "CLOSED"


@dataclass
class FakeRTDE:
    """In-memory RTDE register surface used only by offline protocol tests."""

    input_registers: dict[int, int] = None  # type: ignore[assignment]
    output_registers: dict[int, int] = None  # type: ignore[assignment]
    state: HandshakeState = HandshakeState.DISARMED

    def __post_init__(self) -> None:
        self.input_registers = {} if self.input_registers is None else dict(self.input_registers)
        self.output_registers = {} if self.output_registers is None else dict(self.output_registers)


@dataclass
class PathEarlyEndHandshake:
    """Sequence-matched request/ack state machine; booleans are not accepted."""

    runtime_protocol: int = 612012
    request_register: int = 35
    ack_register: int = 36
    terminal_reason_register: int = 28
    reason_register: int = 35
    rtde: FakeRTDE | None = None
    attempt_sequence: int | None = None

    def arm(self, attempt_sequence: int) -> None:
        if isinstance(attempt_sequence, bool) or attempt_sequence <= 0:
            raise CensoringError("attempt/session sequence must be a positive integer")
        self.rtde = self.rtde or FakeRTDE()
        self.attempt_sequence = attempt_sequence
        self.rtde.input_registers[self.request_register] = 0
        self.rtde.output_registers[self.ack_register] = 0
        self.rtde.output_registers[self.terminal_reason_register] = 0
        self.rtde.output_registers[self.reason_register] = 0
        self.rtde.state = HandshakeState.PATH

    def request(self, sequence: int) -> bool:
        if self.rtde is None or self.attempt_sequence is None or sequence != self.attempt_sequence:
            return False
        if isinstance(sequence, bool) or sequence <= 0:
            return False
        self.rtde.input_registers[self.request_register] = sequence
        return True

    def tp_step(self) -> bool:
        if self.rtde is None or self.attempt_sequence is None or self.rtde.state != HandshakeState.PATH:
            return False
        if self.rtde.input_registers.get(self.request_register) != self.attempt_sequence:
            return False
        self.rtde.output_registers[self.ack_register] = self.attempt_sequence
        self.rtde.output_registers[self.terminal_reason_register] = 0
        # output 35 is the existing reason43 subtype.  Graceful completion is
        # a normal PATH completion: reason=0 and subtype=0.  Register 36 is
        # exclusively the sequence-matched acknowledgement.
        self.rtde.output_registers[self.reason_register] = 0
        self.rtde.state = HandshakeState.PATH_COMPLETE
        return True

    def begin_return_home(self) -> None:
        if self.rtde is None or self.rtde.state != HandshakeState.PATH_COMPLETE:
            raise CensoringError("return-home requires sequence-matched PATH completion")
        self.rtde.state = HandshakeState.RETURN_HOME

    def finalize_censor(self, *, return_guard: bool, home: bool, safe_return: bool) -> bool:
        if self.rtde is None or self.attempt_sequence is None:
            return False
        matched = self.rtde.output_registers.get(self.ack_register) == self.attempt_sequence
        if not (matched and self.rtde.output_registers.get(self.terminal_reason_register) == 0 and self.rtde.output_registers.get(self.reason_register) == 0 and self.rtde.state == HandshakeState.RETURN_HOME and return_guard and home and safe_return):
            return False
        self.rtde.state = HandshakeState.CLOSED
        return True

    def seal_censored_observation(
        self,
        *,
        dispatch_id: str,
        candidate: Mapping[str, Any],
        closed_absolute_errors: Sequence[float],
        watermark_s: float,
        confirmed_incumbent_mean_n: float,
        campaign_id: str,
        run_id: str,
        attempt_id: str,
        return_guard: bool,
        home: bool,
        safe_return: bool,
    ) -> CensoredObservation | None:
        """Seal only after the matching ack and complete safe-return closure."""

        values = _errors(closed_absolute_errors)
        if not self.finalize_censor(return_guard=return_guard, home=home, safe_return=safe_return):
            return None
        if len(values) < GUARD_BINS:
            raise CensoringError("a censor row requires the 55-bin guard")
        return CensoredObservation(
            dispatch_id,
            candidate,
            sum(values) / DENOMINATOR_BINS,
            watermark_s,
            len(values),
            DENOMINATOR_BINS,
            KAPPA,
            confirmed_incumbent_mean_n,
            campaign_id,
            run_id,
            attempt_id,
            prefix_mean_n=sum(values) / len(values),
            ack_sequence=self.attempt_sequence,
        )

    def reset_for_next_arm(self) -> None:
        self.attempt_sequence = None
        if self.rtde is not None:
            self.rtde.state = HandshakeState.DISARMED


__all__ = [
    "ACTIVE_RUN_KINDS", "BIN_WIDTH_S", "CENSOR_SCHEMA", "CensorDecision", "CensorProtocol", "CensoredMAEAccumulator",
    "CensoredObservation", "CensoringError", "DENOMINATOR_BINS", "ExactObservation", "FakeRTDE", "GUARD_BINS",
    "HandshakeState", "KAPPA", "NON_ABORT_RUN_KINDS", "Observation", "PROTOCOL_SCHEMA", "PathEarlyEndHandshake",
    "causal_lower_bound", "evaluate_censor_prefix", "hypothetical_ts_value", "kappa_for_progress", "legacy_gp_training_rows",
    "prefix_mean", "require_exact_for_legacy_gp", "validate_observation",
]
