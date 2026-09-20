# Yield cycle boundary v1

Development software seam only. No robot IO, transport qualification, or
physical task evidence.

## Defect

Full-writer coverage uses the first *consumed* PATH echo as its RTDE origin.
The generation fence previously backdated `_path_command_started_mono_s` by
the *current* formal clock (`now - formal_time_s`). That reconstructs a
nominal PATH t=0 at the exact 1 s seam.

When the 2 ms sample grid does not land on 1.0 — including a binary64 grid
starting at 100 s — entry occupies one extra sample. First PATH is commanded
at formal ≈ 0.002. That late start is already required by
`test_irregular_entry_boundary_does_not_freeze_or_invent_zero` (1.002 s →
0.002 formal). Inventing a zero PATH sample, freezing the clock, or rounding
timestamps is forbidden.

The backdated fence then expired one to two samples before a full 62.831853 s
of *consumed* PATH existed. Strict evidence reported
`path duration is 62.830000 s, below 62.8319 s` (observed span 62.828 + 0.002
coverage). Clock origin 0 happened to land on 1.0 and passed; origin 100 s
did not. That is not a clock-origin special case. It is a fence/coverage
origin mismatch.

## Repair (`unwrapped_periodic_v1`)

1. Entry-aware generation fence now starts at the same instant as RTDE
   coverage: the first consumed PATH echo. It is not backdated to a nominal
   t=0 that was never commanded.
2. `YieldContactProvider.execution_phase` accepts at most
   `PATH_SEAM_CONTINUATION_S` (0.004 s) past `1 + PERIOD_S`, matching the
   existing skipped-initial PATH bound.
3. Yield `Task.reference` uses unwrapped periodic time on that same bounded
   overshoot. It does not clamp to the nominal endpoint (that would freeze
   the last sample). Benchmark `Task` used by other routes is unchanged.
4. `YieldPathEvidenceCollector` allows consumed formal clocks in
   `[0, PERIOD_S + first_consumed_PATH_ref)`. Clock-0 first ref is 0, so the
   previous `[0, PERIOD_S)` bound is unchanged. Duration, 629 bins, and
   freshness/hash gates are not relaxed. A last sample at 62.828 still fails.

Old 60 s collectors and non-entry-aware writer routes keep their fence
(`_path_command_started_mono_s = now` without yield backdating).

Stationary full-writer observations remain task-ineligible. Canonical
admission still cannot construct `CanonicalQualificationControl()` against
the frozen RNN xacro digest; that gap is unchanged.
