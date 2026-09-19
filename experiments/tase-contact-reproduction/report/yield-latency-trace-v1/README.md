# Latency attribution and runtime deadline scope v1

Development fixtures only. No robot IO, physical pilot, transport qualification, holdout or controller-performance ranking. The existing robot route remains inactive.

## Trace attribution and correction of scope

The standalone native qualification fixture (three full 1 s entry + 62.83 s PATH cycles) now records wall time, thread CPU time and `gc.callbacks` events. Its default mode keeps GC enabled. Raw arrays and GC events are retained alongside exact tick attribution.

- SFC tick 1412: 22.455 ms wall, 22.439 ms CPU; generation-2 GC takes 21.540 ms and collects 10606 objects.
- DSFC tick 14613: 25.991 ms wall, 25.971 ms CPU; generation-2 GC takes 25.058 ms and collects 15096 objects.
- MSFC has no generation-2 collection or >2 ms sample in this fixture run. This does not mean its law is faster or safer.
- SFC first-tick 2.231 ms includes only 0.049 ms of GC; its remaining startup cost is not attributed here.

Crucially, these traces bypass the mature writer's existing GC boundary. Source inspection confirms `LiveR004Writer.execute_attempt` already disables cyclic GC within its attempt and restores it in `finally`; pre-ARM collection already exists. Therefore the standalone GC spikes are NOT evidence that the actual writer suffers these same spikes, and are NOT by themselves grounds for migrating all controller mathematics to C++. No production GC policy was changed.

The `mature-policy/` diagnostic reproduces this existing bounded policy around the standalone fixture (explicit collection beforehand, restore original enabled state afterward). It still does not execute the transport/writer chain or qualify realtime performance. Setup/model creation is included in that bounded GC scope, whereas actual writer control construction precedes its hot-loop boundary. Max RSS is process high-water usage, not a memory bound. No collection event or latency sample is deleted from either report.

## Deadline defect and implementation

The old `YieldContactRuntime.step` checked its deadline before output dictionary conversion and freshness reporting, then committed clocks. `pause` had the same gap. Result materialization errors could also escape after committing state.

Both now construct the complete runtime response within the transactional boundary. Elapsed time is measured after response materialization, the existing deadline is checked, and any exception restores controller state, memory, phase and runtime clocks. Successfully received observation diagnostics remain retained. `runtime_wall_s` covers the runtime through response materialization; it still excludes subsequent provider packaging, qualification gates, writer IO and logging. Runtime identity now includes `deadline_scope=through_result_materialization_v2`, preventing old-scope snapshots from silently matching.

Four injected-error tests cover command/pause and delayed reporting/report-construction exceptions. They verify full provider/runtime state equality with the pre-call snapshot while the received observation count still increases. Production deadlines and freshness thresholds are unchanged.

The enabled-GC trace process imported the 629f646d runtime before this deadline-scope edit. The separate mature-policy process uses the patched runtime. Do not treat this as a perfectly isolated GC-only timing intervention; the deadline patch is a small but explicit second difference. The directly matched callback intervals establish GC attribution independently of that comparison.

## Reproduction and pending work

Run `measure.py --skip-profile --output NEW_DIRECTORY`; add `--mature-gc-policy` for the existing-policy diagnostic. The ordinary host is unpinned and clocks are synthetic sample clocks. Per-tick timers measure real compute time; there is no pacing, real receive delay or hardware execution. Baseline state is stubbed successful and pose remains stationary. Native law, QP, snapshots and qualification gates are real.

Next engineering evidence must come from the full writer with the actual provider, logging and transport qualification, under its actual scheduler/GC policy. Keep CPU time, wall time, received timestamps and consumed packet evidence distinct. Shared observer identifiability and the force/friction tradeoff remain unresolved research work; none of these timing repairs establishes a proposal advantage.

## Existing-policy diagnostic results

| Method | PATH p99 ms | PATH max ms | PATH >2 ms / 31416 | All-phase >2 ms |
|---|---:|---:|---:|---:|
| SFC | 0.720 | 0.980 | 0 | 1 |
| DSFC | 0.744 | 0.989 | 0 | 0 |
| MSFC | 0.741 | 2.638 | 1 | 1 |

All measured native ticks observed GC disabled and no callback events; the original enabled state was restored after each bounded trial. SFC still has one first-entry >2 ms sample, so startup is not qualified. This single development run supplies no hard worst-case bound.

The remaining MSFC mature-policy outlier is tick 11935: 2.638 ms wall versus 2.603 ms thread CPU, with no GC callback. It cannot be explained primarily as off-CPU scheduling delay from these clocks; its computation/page-fault/frequency cause remains unassigned. Keep it as a deadline miss, not an excluded outlier.

`bindings.json` records exact seed parameters and source hashes. The fixture uses the protocol's original MSFC gain (0.17166683818219516), not the GM-v1 g50 candidate. These timing results do not qualify the selected scientific candidate or replace its separate full-route identity.

## Validation

`deadline-tests.txt`: 4 injected deadline/materialization cases pass. `regression-tests.txt`: 193 contact, evidence-ledger and motion-profile tests pass. `analyze.py` independently regenerates the enabled-GC attribution byte-for-byte. All measurement processes exited successfully; no robot process or production GC policy was modified.
