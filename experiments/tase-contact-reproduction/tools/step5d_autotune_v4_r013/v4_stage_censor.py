"""Streaming V4 stage censor observer over sealed 0.1 s PATH bins."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping

from step5d_autotune_v4_r012.censor import ACTIVE_RUN_KINDS, DENOMINATOR_BINS, KAPPA
from step5d_autotune_v4_r012.live_host import PathEarlyEndController

from step5d_autotune_v4_r013.v4_two_stage_campaign import (
    CENSOR_KAPPA,
    CENSOR_MIN_CLOSED_BINS,
    V4StageCampaignV1,
    V4StageError,
)


CENSOR_OBSERVER_SCHEMA = "step5d.autotune-v4/v4-stage-censor-observer-v1"
V4_CENSOR_SCHEMA = "step5d.autotune-v4/v4-stage-censored-observation-v1"
V4_CENSOR_VERSION = 1
BIN_WIDTH_S = 0.1
FORMAL_START_S = 5.0
FORMAL_END_S = 60.0
V4_MIN_CLOSED_BINS = CENSOR_MIN_CLOSED_BINS


@dataclass(frozen=True)
class V4CensoredObservationV1:
    """V4-specific sealed censor row with the reviewed 25-bin guard.

    The legacy R012 observation remains unchanged at its 55-bin guard.  V4
    uses this versioned row so its 25-bin early-stop contract cannot silently
    mutate the older R012 protocol or its safety/ledger validators.
    """

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
    prefix_mean_n: float
    ack_sequence: int
    baseline: Mapping[str, Any]
    completed: bool = True
    sealed: bool = True
    nontrainable: bool = True
    noncontrol: bool = True
    noncompletion: bool = True
    kind: str = "BO_TRIAL"
    schema: str = V4_CENSOR_SCHEMA
    version: int = V4_CENSOR_VERSION

    def __post_init__(self) -> None:
        if self.schema != V4_CENSOR_SCHEMA or self.version != V4_CENSOR_VERSION:
            raise V4StageError("V4 censored observation schema/version differs")
        if not all(isinstance(value, str) and value for value in (
            self.dispatch_id, self.campaign_id, self.run_id, self.attempt_id,
        )):
            raise V4StageError("V4 censored observation identity is incomplete")
        if self.denominator_bins != DENOMINATOR_BINS:
            raise V4StageError("V4 censored denominator differs")
        if type(self.closed_bin_count) is not int or not V4_MIN_CLOSED_BINS <= self.closed_bin_count <= DENOMINATOR_BINS:
            raise V4StageError("V4 censored closed-bin guard differs")
        if not all(value is True for value in (
            self.completed, self.sealed, self.nontrainable, self.noncontrol, self.noncompletion,
        )):
            raise V4StageError("V4 censored closure flags differ")
        if self.kind not in ACTIVE_RUN_KINDS:
            raise V4StageError("V4 censored row must be a novel BO kind")
        if type(self.ack_sequence) is not int or self.ack_sequence <= 0:
            raise V4StageError("V4 censored row needs a positive ack sequence")
        for value, role in (
            (self.lower_bound_n, "lower bound"),
            (self.watermark_s, "watermark"),
            (self.kappa, "kappa"),
            (self.incumbent_threshold_n, "incumbent threshold"),
            (self.prefix_mean_n, "prefix mean"),
        ):
            try:
                parsed = float(value)
            except (TypeError, ValueError, OverflowError) as exc:
                raise V4StageError(f"V4 censored {role} is not finite") from exc
            if not math.isfinite(parsed) or parsed < 0.0:
                raise V4StageError(f"V4 censored {role} is invalid")
        if not math.isclose(float(self.kappa), KAPPA, rel_tol=0.0, abs_tol=0.0):
            raise V4StageError("V4 censored kappa differs")
        expected_lower = float(self.prefix_mean_n) * self.closed_bin_count / DENOMINATOR_BINS
        if not math.isclose(float(self.lower_bound_n), expected_lower, rel_tol=0.0, abs_tol=1e-12):
            raise V4StageError("V4 censored lower-bound semantics differ")
        if not isinstance(self.baseline, Mapping):
            raise V4StageError("V4 censored baseline receipt is missing")
        baseline_mean = float(self.baseline.get("mean_n", float("nan")))
        if not math.isfinite(baseline_mean) or not math.isclose(
            baseline_mean, float(self.incumbent_threshold_n), rel_tol=0.0, abs_tol=1e-12
        ):
            raise V4StageError("V4 censored baseline threshold differs")
        object.__setattr__(self, "candidate", dict(self.candidate))
        object.__setattr__(self, "baseline", dict(self.baseline))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "version": self.version,
            "dispatch_id": self.dispatch_id,
            "candidate": dict(self.candidate),
            "lower_bound_n": self.lower_bound_n,
            "prefix_mean_n": self.prefix_mean_n,
            "watermark_s": self.watermark_s,
            "closed_bin_count": self.closed_bin_count,
            "minimum_closed_bins": V4_MIN_CLOSED_BINS,
            "denominator_bins": self.denominator_bins,
            "kappa": self.kappa,
            "incumbent_threshold_n": self.incumbent_threshold_n,
            "baseline": dict(self.baseline),
            "campaign_id": self.campaign_id,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "completed": True,
            "sealed": True,
            "nontrainable": True,
            "noncontrol": True,
            "noncompletion": True,
            "kind": self.kind,
            "ack_sequence": self.ack_sequence,
            "censored": True,
        }


@dataclass
class V4ActiveCensorRuntime:
    """Live V4 censor seam with a 25-bin guard and typed baseline receipt."""

    controller: PathEarlyEndController
    campaign_id: str
    run_id: str
    attempt_id: str
    scheduled: Any | None = None
    attempt_sequence: int | None = None
    incumbent_mean_n: float | None = None
    baseline: Mapping[str, Any] | None = None
    closed_absolute_errors: list[float] = field(default_factory=list)
    _bin_index: int | None = None
    _bin_force_sum: float = 0.0
    _bin_sample_count: int = 0
    requested: bool = False

    def arm(
        self,
        *,
        scheduled: Any,
        attempt_sequence: int | None,
        incumbent_mean_n: float | None,
        baseline: Mapping[str, Any] | None = None,
    ) -> None:
        self.scheduled = scheduled
        if attempt_sequence is not None and (isinstance(attempt_sequence, bool) or int(attempt_sequence) <= 0):
            raise V4StageError("V4 active censor attempt sequence is invalid")
        self.attempt_sequence = None if attempt_sequence is None else int(attempt_sequence)
        self.incumbent_mean_n = None if incumbent_mean_n is None else float(incumbent_mean_n)
        self.baseline = None if baseline is None else dict(baseline)
        self.closed_absolute_errors.clear()
        self._bin_index = None
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        self.requested = False
        if self.attempt_sequence is not None:
            self.controller.arm(self.attempt_sequence)

    def bind_physical_attempt(self, attempt_sequence: int) -> None:
        """Bind the register handshake after R013 allocates its physical sequence."""

        if self.scheduled is None or self.requested:
            raise V4StageError("V4 active censor is not awaiting a physical sequence")
        if isinstance(attempt_sequence, bool) or int(attempt_sequence) <= 0:
            raise V4StageError("V4 physical censor sequence is invalid")
        self.attempt_sequence = int(attempt_sequence)
        self.controller.arm(self.attempt_sequence)

    def _close_current_bin(self) -> None:
        if self._bin_index is None or self._bin_sample_count <= 0:
            return
        mean_force_n = self._bin_force_sum / float(self._bin_sample_count)
        self.closed_absolute_errors.append(abs(mean_force_n - 5.0))
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        if self.scheduled is None or self.requested:
            return
        if (
            self.scheduled.kind not in ACTIVE_RUN_KINDS
            or not bool(getattr(self.scheduled, "abort_allowed", False))
            or self.incumbent_mean_n is None
            or len(self.closed_absolute_errors) < V4_MIN_CLOSED_BINS
        ):
            return
        prefix = math.fsum(self.closed_absolute_errors) / len(self.closed_absolute_errors)
        if prefix > KAPPA * self.incumbent_mean_n:
            sequence = self.attempt_sequence
            if sequence is None or not self.controller.request_early_end(sequence):
                raise V4StageError("V4 active censor could not issue sequence-matched request")
            self.requested = True

    def observe_path_sample(self, sample: Any) -> None:
        if self.scheduled is None or self.requested:
            return
        try:
            state = int(getattr(sample, "state"))
            path_time = float(getattr(sample, "path_time_s"))
            force = float(getattr(sample, "filtered_normal_n"))
        except (TypeError, ValueError, OverflowError):
            return
        if state != 25 or not math.isfinite(path_time) or not math.isfinite(force):
            return
        if not FORMAL_START_S <= path_time < FORMAL_END_S:
            return
        index = int(math.floor((path_time - FORMAL_START_S) / BIN_WIDTH_S))
        if not 0 <= index < DENOMINATOR_BINS:
            return
        if self._bin_index is None:
            self._bin_index = index
        elif index < self._bin_index:
            raise V4StageError("V4 active censor PATH bin regressed")
        elif index != self._bin_index:
            self._close_current_bin()
            if self.requested:
                return
            self._bin_index = index
        self._bin_force_sum += force
        self._bin_sample_count += 1

    def finalize(
        self,
        *,
        dispatch_id: str,
        return_guard: bool,
        home: bool,
        safe_return: bool,
    ) -> V4CensoredObservationV1 | None:
        if not self.requested or self.scheduled is None or self.incumbent_mean_n is None:
            return None
        self._close_current_bin()
        if len(self.closed_absolute_errors) < V4_MIN_CLOSED_BINS:
            raise V4StageError("V4 active censor fired before the 25-bin guard")
        # The live writer has no in-memory FakeRTDE state.  Validate the
        # sequence/terminal registers directly in that case; the old R012
        # helper only mirrors completion when ``handshake.rtde`` is present.
        if getattr(self.controller, "writer", None) is not None:
            ack, terminal_reason, reason43_subtype = self.controller.read_completion_registers()
            if (ack, terminal_reason, reason43_subtype) != (self.attempt_sequence, 0, 0):
                return None
            if not all(bool(value) for value in (return_guard, home, safe_return)):
                return None
        else:
            if not self.controller.observe_live_completion():
                return None
            self.controller.handshake.begin_return_home()
            if not self.controller.handshake.finalize_censor(
                return_guard=bool(return_guard), home=bool(home), safe_return=bool(safe_return)
            ):
                return None
            return None
        prefix = math.fsum(self.closed_absolute_errors) / len(self.closed_absolute_errors)
        return V4CensoredObservationV1(
            dispatch_id=str(dispatch_id),
            candidate=dict(self.scheduled.candidate),
            lower_bound_n=math.fsum(self.closed_absolute_errors) / DENOMINATOR_BINS,
            watermark_s=FORMAL_START_S + len(self.closed_absolute_errors) * BIN_WIDTH_S,
            closed_bin_count=len(self.closed_absolute_errors),
            denominator_bins=DENOMINATOR_BINS,
            kappa=KAPPA,
            incumbent_threshold_n=self.incumbent_mean_n,
            campaign_id=self.campaign_id,
            run_id=self.run_id,
            attempt_id=self.attempt_id,
            prefix_mean_n=prefix,
            ack_sequence=int(self.attempt_sequence),
            baseline=dict(self.baseline or {"baseline_kind": "unspecified", "mean_n": self.incumbent_mean_n}),
            kind=str(self.scheduled.kind),
        )

    def reset(self) -> None:
        self.scheduled = None
        self.attempt_sequence = None
        self.incumbent_mean_n = None
        self.baseline = None
        self.closed_absolute_errors.clear()
        self._bin_index = None
        self._bin_force_sum = 0.0
        self._bin_sample_count = 0
        self.requested = False
        self.controller.handshake.reset_for_next_arm()


@dataclass
class V4StageCensorObserverV1:
    campaign: V4StageCampaignV1
    attempt_ordinal: int
    requested: bool = False
    closed_absolute_errors: list[float] = field(default_factory=list)
    _bin_index: int | None = None
    _force_sum: float = 0.0
    _sample_count: int = 0
    trigger_watermark_s: float | None = None

    schema: str = CENSOR_OBSERVER_SCHEMA
    version: int = 1

    def __post_init__(self) -> None:
        if self.schema != CENSOR_OBSERVER_SCHEMA or self.version != 1:
            raise V4StageError("V4 censor observer schema/version differs")
        if self.campaign.in_flight is None or self.campaign.in_flight.ordinal != self.attempt_ordinal:
            raise V4StageError("V4 censor observer is not bound to the active attempt")

    @property
    def closed_bin_count(self) -> int:
        return len(self.closed_absolute_errors)

    @property
    def prefix_mae_n(self) -> float | None:
        if not self.closed_absolute_errors:
            return None
        return math.fsum(self.closed_absolute_errors) / len(self.closed_absolute_errors)

    def _close_bin(self) -> None:
        if self._bin_index is None or self._sample_count <= 0:
            return
        mean_force = self._force_sum / self._sample_count
        self.closed_absolute_errors.append(abs(mean_force - 5.0))
        self._force_sum = 0.0
        self._sample_count = 0
        if self.requested:
            return
        incumbent = self.campaign.censor_incumbent
        if self.closed_bin_count < CENSOR_MIN_CLOSED_BINS or incumbent is None:
            return
        if self.prefix_mae_n is not None and self.prefix_mae_n > CENSOR_KAPPA * float(incumbent["mean_n"]):
            self.requested = True
            self.trigger_watermark_s = FORMAL_START_S + self.closed_bin_count * BIN_WIDTH_S

    def observe(self, *, state: int, path_time_s: float, filtered_normal_n: float) -> bool:
        """Consume one fresh sample and return whether a graceful end is requested."""

        if self.requested or state != 25:
            return self.requested
        time_s = float(path_time_s)
        force_n = float(filtered_normal_n)
        if not math.isfinite(time_s) or not math.isfinite(force_n):
            return self.requested
        if not FORMAL_START_S <= time_s < FORMAL_END_S:
            return self.requested
        index = int(math.floor((time_s - FORMAL_START_S) / BIN_WIDTH_S))
        if self._bin_index is None:
            self._bin_index = index
        elif index < self._bin_index:
            raise V4StageError("V4 censor path bin regressed")
        elif index != self._bin_index:
            self._close_bin()
            if self.requested:
                return True
            self._bin_index = index
        self._force_sum += force_n
        self._sample_count += 1
        return self.requested

    def close_current(self) -> None:
        self._close_bin()

    def as_dict(self) -> dict[str, Any]:
        incumbent = self.campaign.censor_incumbent
        return {
            "schema": self.schema,
            "version": self.version,
            "attempt_ordinal": self.attempt_ordinal,
            "requested": self.requested,
            "closed_bin_count": self.closed_bin_count,
            "prefix_mae_n": self.prefix_mae_n,
            "trigger_watermark_s": self.trigger_watermark_s,
            "kappa": CENSOR_KAPPA,
            "min_closed_bins": CENSOR_MIN_CLOSED_BINS,
            "nontrainable": True,
            "baseline": None if incumbent is None else dict(incumbent),
        }


__all__ = [
    "BIN_WIDTH_S",
    "CENSOR_OBSERVER_SCHEMA",
    "V4_CENSOR_SCHEMA",
    "V4_MIN_CLOSED_BINS",
    "V4ActiveCensorRuntime",
    "V4CensoredObservationV1",
    "V4StageCensorObserverV1",
]
