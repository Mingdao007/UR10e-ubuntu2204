"""Full formal-period coverage, separate from entry and task-performance claims.

Uses the mature joined RTDE/TP clock and its strict endpoint proof. The old
550-bin campaign field is retained for compatibility; acceptance additionally
requires all 629 bins of the complete 62.83 s formal task. This class does not
supply or infer a physical PATH boundary from a generated command.
"""
import math
from dataclasses import replace
from step5d_autotune_v4_r004.evidence import PathEvidenceCollector, EvidenceError
from contact_yield_protocol import PATH_SEAM_CONTINUATION_S, PERIOD_S
from tase_figure8_protocol import (
    DURATION_S as R013_COMPAT60_DURATION_S,
    PROTOCOL_ID as R013_COMPAT60_PROTOCOL_ID,
    RATE400_PROTOCOL_ID as R013_RATE400_PROTOCOL_ID,
)


# RTDE/TP reference clocks are joined from independently sampled 500 Hz
# streams. This small explicit tolerance covers only sub-frame floating-point
# quantization at the endpoint; formal metric duration and bins remain exact.
REFERENCE_CLOCK_ROUNDING_TOLERANCE_S = 1e-5


class YieldPathEvidenceCollector(PathEvidenceCollector):
    REQUIRED_DURATION_S = PERIOD_S

    def __init__(self, *args, published_reference_lookup, **kwargs):
        super().__init__(*args, **kwargs)
        if not callable(published_reference_lookup):
            raise EvidenceError("yield evidence requires published packet reference lookup")
        self._published_reference_lookup = published_reference_lookup
        self._first_reference_time_s = None
        self._last_reference_time_s = None
        self._endpoint_closure_applied = False
        self._endpoint_closure_deficit_s = 0.0

    def _reference(self, sequence):
        reference=self._published_reference_lookup(sequence)
        if reference.reference_phase != "path" or reference.reference_time_s is None:
            raise EvidenceError("formal coverage requires a consumed published PATH command")
        clock=float(reference.reference_time_s)
        # A consumed command may be a bounded seam continuation after the
        # exact formal period. It is still excluded from the formal metric
        # window; this limit only prevents the end-handshake from being
        # mistaken for an unbounded extra task.
        limit = PERIOD_S + PATH_SEAM_CONTINUATION_S
        if not math.isfinite(clock) or clock < 0.0 or clock > limit + 1e-9:
            raise EvidenceError("consumed formal reference clock outside full task")
        return clock

    def mark_path_start(self, *, observed_at_s, rtde_timestamp_s, tp_sequence):
        clock=self._reference(tp_sequence)
        if clock > PATH_SEAM_CONTINUATION_S:
            raise EvidenceError("formal boundary skipped initial PATH commands")
        self._first_reference_time_s = clock
        super().mark_path_start(observed_at_s=observed_at_s,
            rtde_timestamp_s=rtde_timestamp_s,tp_sequence=tp_sequence)

    def observe_terminal_reference(self, *, sequence):
        """Record one consumed seam reference without adding a metric sample.

        The RTDE clock can be one 2 ms frame behind the final formal
        reference.  A bounded continuation packet proves that the requested
        PATH was consumed through its endpoint, while its force/motion sample
        remains outside the formal MAE window.
        """
        clock = self._reference(sequence)
        if self._last_reference_time_s is not None and clock < self._last_reference_time_s:
            raise EvidenceError("consumed formal reference clock regressed")
        self._last_reference_time_s = clock
        if clock >= PERIOD_S:
            self._endpoint_closure_applied = True
        return clock

    def _validated_path_coverage_interval_s(self):
        interval = super()._validated_path_coverage_interval_s()
        if not self._endpoint_closure_applied:
            return interval
        if self._path_start_rtde_timestamp_s is None or self._last_common_clock is None:
            return interval
        physical_span = self._last_common_clock[0] - self._path_start_rtde_timestamp_s
        deficit = PERIOD_S - (physical_span + interval)
        if 0.0 < deficit <= PATH_SEAM_CONTINUATION_S:
            self._endpoint_closure_deficit_s = deficit
            return interval + deficit
        return interval

    def observe(self, sample):
        reference_time=None
        if sample.state == 25:
            common=self._common_clock(sample)
            if common is None:
                raise EvidenceError("yield coverage requires RTDE/TP join")
            reference_time=self._reference(common[1])
            if (self._last_reference_time_s is not None
                and reference_time < self._last_reference_time_s):
                raise EvidenceError("consumed formal reference clock regressed")
            # Duplicate joins are handled by the mature collector. A new
            # physical join with an unchanged command reference is not progress.
            if (self._last_common_clock is not None and common[0] > self._last_common_clock[0]
                and common[1] > self._last_common_clock[1]
                and reference_time == self._last_reference_time_s):
                raise EvidenceError("consumed formal reference clock was frozen")
        accepted=super().observe(sample)
        if accepted:
            self._last_reference_time_s=reference_time
        return accepted

    def _motion_metrics(self):
        metrics=super()._motion_metrics()
        reference_span = None
        if self._first_reference_time_s is not None and self._last_reference_time_s is not None:
            reference_span = (
                self._last_reference_time_s
                - self._first_reference_time_s
                + metrics['path_coverage_interval_s']
            )
        if reference_span is None or reference_span + REFERENCE_CLOCK_ROUNDING_TOLERANCE_S < PERIOD_S:
            raise EvidenceError(
                "consumed reference did not complete the formal task; "
                f"first={self._first_reference_time_s!r}; "
                f"last={self._last_reference_time_s!r}; "
                f"coverage={metrics['path_coverage_interval_s']!r}; "
                f"span={reference_span!r}; required={PERIOD_S!r}"
            )
        expected=math.ceil(self.REQUIRED_DURATION_S/self.BIN_WIDTH_S)
        bins={int(sample.path_time_s/self.BIN_WIDTH_S) for sample in self.path_samples
              if sample.path_time_s is not None}
        missing=sorted(set(range(expected))-bins)
        if missing:
            raise EvidenceError(f"full yield PATH coverage has missing bins: {missing}")
        return {**metrics,
                'reference_span_s': reference_span,
                'reference_span_required_s': PERIOD_S,
                'reference_clock_rounding_tolerance_s': REFERENCE_CLOCK_ROUNDING_TOLERANCE_S,
                'endpoint_closure_applied': self._endpoint_closure_applied,
                'endpoint_closure_deficit_s': self._endpoint_closure_deficit_s,
                'full_path_bin_count':len(bins),
                'required_full_path_bin_count':expected,
                'entry_in_formal_coverage':False,
                'path_seam_first_reference_s':self._first_reference_time_s,
                'path_seam_continuation_s':PATH_SEAM_CONTINUATION_S}

    def finalize(self, **kwargs):
        evidence=super().finalize(**kwargs)
        expected=math.ceil(self.REQUIRED_DURATION_S/self.BIN_WIDTH_S)
        samples=self.path_samples
        times=[sample.path_time_s for sample in samples]
        ends=[*times[1:], self.REQUIRED_DURATION_S]
        weights=[max(0.,min(end,self.REQUIRED_DURATION_S)-start)
                 for start,end in zip(times,ends)]
        total=sum(weights)
        if total <= 0:
            raise EvidenceError("formal force metrics have no observed duration")
        full_mae=sum(w*abs(sample.filtered_normal_n-5.) for w,sample in zip(weights,samples))/total
        full_rmse=math.sqrt(sum(w*(sample.filtered_normal_n-5.)**2
                               for w,sample in zip(weights,samples))/total)
        return replace(evidence,
            mae_n=full_mae if kwargs.get("mae_n") is None else evidence.mae_n,
            objective=full_mae if kwargs.get("objective") is None else evidence.objective,
            metrics={**evidence.metrics,
                'full_path_bin_count':expected,
                'required_full_path_bin_count':expected,
                'entry_in_formal_coverage':False,
                'path_seam_first_reference_s':self._first_reference_time_s,
                'path_seam_continuation_s':PATH_SEAM_CONTINUATION_S,
                'legacy_first550_bin_mae_n':evidence.mae_n,
                'full_force_mae_n':full_mae,
                'full_force_rmse_n':full_rmse,
                'full_force_metric_duration_s':total,
                'force_metric_basis':'measured filtered normal load, time weighted over formal samples'})


class TaseR013Compat60PathEvidenceCollector(PathEvidenceCollector):
    """550-bin collector for the explicitly separate 60 s R013 window."""

    REQUIRED_DURATION_S = R013_COMPAT60_DURATION_S
    REQUIRED_BINS = 550
    PROTOCOL_ID = R013_COMPAT60_PROTOCOL_ID

    def __init__(self, *args, **kwargs):
        published_reference_lookup = kwargs.pop("published_reference_lookup", None)
        if not callable(published_reference_lookup):
            raise EvidenceError("R013-compatible evidence requires published packet lookup")
        super().__init__(*args, **kwargs)
        self.protocol_id = self.PROTOCOL_ID
        self._published_reference_lookup = published_reference_lookup
        self._first_reference_time_s = None
        self._last_reference_time_s = None
        self._endpoint_closure_applied = False
        self._endpoint_closure_deficit_s = 0.0

    def _reference(self, sequence):
        reference = self._published_reference_lookup(sequence)
        if reference.reference_phase != "path" or reference.reference_time_s is None:
            raise EvidenceError("R013 coverage requires a consumed published PATH command")
        clock = float(reference.reference_time_s)
        if not math.isfinite(clock) or clock < 0.0 or clock > self.REQUIRED_DURATION_S + PATH_SEAM_CONTINUATION_S + 1e-9:
            raise EvidenceError("R013 consumed reference clock is outside the bounded task seam")
        return clock

    def mark_path_start(self, *, observed_at_s, rtde_timestamp_s, tp_sequence):
        clock = self._reference(tp_sequence)
        if clock > PATH_SEAM_CONTINUATION_S:
            raise EvidenceError("R013 PATH boundary skipped initial commands")
        self._first_reference_time_s = clock
        super().mark_path_start(
            observed_at_s=observed_at_s,
            rtde_timestamp_s=rtde_timestamp_s,
            tp_sequence=tp_sequence,
        )

    def observe_terminal_reference(self, *, sequence):
        """Record a consumed bounded seam packet without adding a metric sample."""

        clock = self._reference(sequence)
        if self._last_reference_time_s is not None and clock < self._last_reference_time_s:
            raise EvidenceError("R013 consumed reference clock regressed")
        self._last_reference_time_s = clock
        if clock >= self.REQUIRED_DURATION_S:
            self._endpoint_closure_applied = True
        return clock

    def _validated_path_coverage_interval_s(self):
        interval = super()._validated_path_coverage_interval_s()
        if not self._endpoint_closure_applied:
            return interval
        if self._path_start_rtde_timestamp_s is None or self._last_common_clock is None:
            return interval
        physical_span = self._last_common_clock[0] - self._path_start_rtde_timestamp_s
        deficit = self.REQUIRED_DURATION_S - (physical_span + interval)
        if 0.0 < deficit <= PATH_SEAM_CONTINUATION_S:
            self._endpoint_closure_deficit_s = deficit
            return interval + deficit
        return interval

    def observe(self, sample):
        """Join the mature clocks while binning only the formal ``[5,60)`` window."""
        # The parent collector bins relative to the first PATH sample.  For
        # this protocol PATH starts at t=0, while the metric deliberately
        # starts at t=5.  Suppress its bin write and retain all other timing,
        # safety, and motion checks unchanged.
        original_bins = self._bins
        self._bins = {}
        try:
            accepted = super().observe(sample)
        finally:
            parent_bins = self._bins
            self._bins = original_bins
        if not accepted:
            return False
        common_clock = self._common_clock(sample)
        if common_clock is not None:
            reference_time = self._reference(common_clock[1])
            if self._last_reference_time_s is not None and reference_time < self._last_reference_time_s:
                raise EvidenceError("R013 consumed reference clock regressed")
            self._last_reference_time_s = reference_time
        time_s = sample.path_time_s
        if time_s is not None and 5.0 <= time_s < self.REQUIRED_DURATION_S:
            index = int((time_s - 5.0) / self.BIN_WIDTH_S + 1e-9)
            if 0 <= index < self.REQUIRED_BINS:
                self._bins.setdefault(index, []).append(sample.filtered_normal_n)
        # ``parent_bins`` is intentionally discarded; it is relative to the
        # wrong origin and must never enter the formal denominator.
        del parent_bins
        return True

    def _motion_metrics(self):
        return {
            **super()._motion_metrics(),
            "protocol_id": self.PROTOCOL_ID,
            "formal_window_s": [5.0, 60.0],
            "full_period_protocol": False,
            "historical_compatibility": "R013",
            "endpoint_closure_applied": self._endpoint_closure_applied,
            "endpoint_closure_deficit_s": self._endpoint_closure_deficit_s,
            "path_seam_first_reference_s": self._first_reference_time_s,
            "path_seam_continuation_s": PATH_SEAM_CONTINUATION_S,
        }

    def finalize(self, **kwargs):
        evidence = super().finalize(**kwargs)
        metrics = {
            **evidence.metrics,
            "protocol_id": self.PROTOCOL_ID,
            "formal_window_s": [5.0, 60.0],
            "full_period_protocol": False,
            "historical_compatibility": "R013",
            "complete": True,
            "objective_eligible": True,
            "coverage_complete": True,
            "interrupted": False,
            "complete_bins": self.REQUIRED_BINS,
            "required_bins": self.REQUIRED_BINS,
            "formal_metric_duration_s": 55.0,
            "normal_force_mae_n": float(evidence.mae_n),
            "mae_n": float(evidence.mae_n),
            "first_consumed_formal_reference_s": self._first_reference_time_s,
        }
        return replace(
            evidence,
            metrics=metrics,
        )


class TaseR013Rate400PathEvidenceCollector(TaseR013Compat60PathEvidenceCollector):
    """Separate 400 Hz data-admission identity with unchanged PATH motion."""

    PROTOCOL_ID = R013_RATE400_PROTOCOL_ID

    def __init__(self, *args, **kwargs):
        from step5d_autotune_v4_r013.live_runtime import (
            R013LightweightTimingEvidenceCollector,
        )
        if kwargs.get("timing") is not None:
            raise EvidenceError("rate400 collector owns its declared timing policy")
        kwargs["timing"] = R013LightweightTimingEvidenceCollector(
            minimum_rate_hz=400.0,
            acceptance_protocol_id=R013_RATE400_PROTOCOL_ID,
        )
        super().__init__(*args, **kwargs)
