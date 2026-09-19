"""Full formal-period coverage, separate from entry and task-performance claims.

Uses the mature joined RTDE/TP clock and its strict endpoint proof. The old
550-bin campaign field is retained for compatibility; acceptance additionally
requires all 629 bins of the complete 62.83 s formal task. This class does not
supply or infer a physical PATH boundary from a generated command.
"""
import math
from dataclasses import replace
from step5d_autotune_v4_r004.evidence import PathEvidenceCollector, EvidenceError
from contact_yield_protocol import PERIOD_S


class YieldPathEvidenceCollector(PathEvidenceCollector):
    REQUIRED_DURATION_S = PERIOD_S

    def __init__(self, *args, published_reference_lookup, **kwargs):
        super().__init__(*args, **kwargs)
        if not callable(published_reference_lookup):
            raise EvidenceError("yield evidence requires published packet reference lookup")
        self._published_reference_lookup = published_reference_lookup
        self._last_reference_time_s = None

    def _reference(self, sequence):
        reference=self._published_reference_lookup(sequence)
        if reference.reference_phase != "path" or reference.reference_time_s is None:
            raise EvidenceError("formal coverage requires a consumed published PATH command")
        clock=float(reference.reference_time_s)
        if not math.isfinite(clock) or not 0 <= clock < PERIOD_S:
            raise EvidenceError("consumed formal reference clock outside full task")
        return clock

    def mark_path_start(self, *, observed_at_s, rtde_timestamp_s, tp_sequence):
        clock=self._reference(tp_sequence)
        if clock > .004:
            raise EvidenceError("formal boundary skipped initial PATH commands")
        super().mark_path_start(observed_at_s=observed_at_s,
            rtde_timestamp_s=rtde_timestamp_s,tp_sequence=tp_sequence)

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
        if (self._last_reference_time_s is None
            or self._last_reference_time_s + metrics['path_coverage_interval_s'] < PERIOD_S):
            raise EvidenceError("consumed reference did not complete the formal task")
        expected=math.ceil(self.REQUIRED_DURATION_S/self.BIN_WIDTH_S)
        bins={int(sample.path_time_s/self.BIN_WIDTH_S) for sample in self.path_samples
              if sample.path_time_s is not None}
        missing=sorted(set(range(expected))-bins)
        if missing:
            raise EvidenceError(f"full yield PATH coverage has missing bins: {missing}")
        return {**metrics, 'full_path_bin_count':len(bins),
                'required_full_path_bin_count':expected,
                'entry_in_formal_coverage':False}

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
                'legacy_first550_bin_mae_n':evidence.mae_n,
                'full_force_mae_n':full_mae,
                'full_force_rmse_n':full_rmse,
                'full_force_metric_duration_s':total,
                'force_metric_basis':'measured filtered normal load, time weighted over formal samples'})
