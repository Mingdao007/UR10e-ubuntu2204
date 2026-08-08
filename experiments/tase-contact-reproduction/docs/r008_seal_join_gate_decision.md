# R008 seal join-gate decision (2026-08-07)

Plan item: decide whether to open **motion no-join** after Phase-5 evidence.
**Default this step: do not change joins.**

## Evidence used (pre-Phase-5 live)

Run: `runs/step5d_autotune_v4_r008/live_20260806_195949_b3_wave4_limit150_pathring_bo_fix_v2`
Analyzer: `tools/r008_phase5_cutover_measure.py` → `/tmp/r008_phase5_baseline_195949.json`

| Metric | Value |
|--------|-------|
| FP | `4ea41044…` (pre-Phase-5; fat JSON only) |
| `seal_total` p50 | **5.22 s** (n=77) |
| `r006_append` p50 | **4.02 s** |
| `ledger_append` p50 | **1.07 s** |
| `R008RAW2` | **0** |
| host.log `reason=43` hits | **3** |
| `R008_PREHOME_SEAL_JOIN` parsed | 0 in that log shape (join may use other tags) |

Offline Phase-5 bench (`/tmp/r008_seal_phase5_bench.json`, ~28k): daemon-like ~**1.6 s** — expected join wait shrink after cutover, not elimination of the race.

## Decision (this step)

| Question | Answer |
|----------|--------|
| Remove preHOME `join_executing` now? | **No** |
| Remove search-critical join now? | **No** |
| Open a “motion no-join” implementation plan now? | **No** — wait for Phase-5 live metrics |

Rationale: standing safe placement remains preHOME drain/join + search-critical gate (`docs/r008_async_seal_contract.md`, `async_seal.py` header). Historical 43s occurred under ~5 s seal; Phase-5 cutover must land and be measured first.

## Re-open criteria (next plan only if met)

After a Phase-5 formal run (`campaign_fingerprint` `b461ed52…`, RAW2 present):

1. Re-run `tools/r008_phase5_cutover_measure.py "$RUN" --json`
2. Open **motion no-join** planning **only if**:
   - `reason_43_hits ≥ 1` in search/ARM gap after cutover, **and**
   - preHOME / search-critical join waits are already short (seal_total p50 ≲ 2 s) so speed alone did not clear 43
3. Otherwise: **keep joins**; treat remaining risk as acceptable under async contract (BO lag ≥1 already).

## Explicit non-goals

- Do not widen `PACKET_STALE_S`
- Do not place seal inside PATH60 / contact search
- Do not use fantasy scores at stop-gate
