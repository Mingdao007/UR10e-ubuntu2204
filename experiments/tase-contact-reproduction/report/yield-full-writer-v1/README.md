# Native yield laws inside the mature writer: offline diagnostic

SFC, DSFC and MSFC g50 completed entry plus the full logical PATH at clock
origin 0 using the real native runtime, qualification step, packet history,
evidence collector and LiveR004Writer.execute_attempt. Input transport and
baseline admission are explicit fixtures, with stationary calibrated-home
observations. Every method remains task-ineligible. This is neither physical
completion, production constructor admission nor formal timing qualification.

The same execution at clock origin 100 s fails the unchanged duration gate:
62.830 s < 62.831853 s. Its negative test and initial failure logs are retained.
It is a genuine phase/coverage boundary defect, currently assigned to the same
Grok session for repair. Origin 0 is not accepted as resolving that defect.

Actual fail_closed emits zero-qdot STOP packets. Stale receive clocks and cached
polls preserve the last committed native state. The initial hash-fault snapshot
was taken before a legitimate baseline pause: test-v2 failed; moving only the
fixture observation to state 25 passes (hash-v3). No production guard was relaxed.

Unpaced diagnostic loop wall p99: SFC 0.914 ms, DSFC 0.922 ms, MSFC 0.887 ms.
Each method has one active iteration over 2 ms (max 3.543/3.581/2.078 ms).
The terminal iteration additionally takes about 2.1 s for evidence finalization
and snapshot capture, and is classified separately rather than discarded.
Runtime deadline is disabled only in this explicit diagnostic; production
1.5 ms runtime and 1 ms QP deadlines remain unchanged. Synthetic sleeps do not
measure actual 500 Hz transport. This run overlapped focused offline tests and
Fable analysis; no exclusive-core or formal low-load claim is made.

The initial Grok delivery used a no-op fail_closed fixture; main replaced it
with actual STOP observation and complete native-state comparisons. Original
writer receipt and three initial file hashes are retained. Same native session
01a0bc56-8b0d-7d42-bb33-19502fa0cc04, Homepool grok-4.6; high effort is
config/launch attestation only. No fallback or hardware operation occurred.

Raw timing arrays and metadata: runs/yield-full-writer-v1, bound by
raw-manifest.json. results.json documents measurement boundaries, exact law
parameters, expanded model identity and native solver binding. Production
construction remains separately blocked by the old RNN xacro contract; the
explicit object.__new__ fixture does not bypass that real admission requirement.
