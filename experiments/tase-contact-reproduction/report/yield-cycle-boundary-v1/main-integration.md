# Main integration and review

The original Grok delivery and provenance are bound by writer-receipt.json.
Native session 01a0bc56-8b0d-7d42-bb33-19502fa0cc04 completed without fallback,
reported grok-4.6/end_turn, and used the Homepool route. High effort is launch
attestation, not independently verified server effort.

Main retained two pre-integration counterexamples under
report/yield-full-writer-v1/boundary-review and made narrow corrections:

- Bind unwrapped_periodic_v1 and its 4 ms maximum continuation in the protocol
  digest and measured runtime identity. A snapshot from a different endpoint
  policy is rejected before mutation. This changes runtime identity; older
  snapshots remain historical evidence, not admissible current runtime state.
- Require last consumed reference minus first consumed reference plus actual
  final coverage interval to cover the full period. Monotonically advancing
  references that omit 2 ms are rejected even with sufficient physical duration.

The implementation retains continuous, unwrapped reference time. It executes
bounded additional periodic geometry to cover a full period measured from the
first consumed PATH command. No entry/HOLD padding, zero-time invention,
clock rounding, cached timestamp refresh, endpoint clamp, or shortened duration
is used. The strict 629-bin and full-period checks remain. Beyond the bounded
4 ms continuation the adapter still rejects rather than claiming completion.

Validation: main-tests.txt covers native MSFC at clock origins 0 and 100,
reference coverage (including the new adversarial shortfall), provider identity,
phase/state/fault behavior, writer-loop and two legacy boundary regressions.
main-additional-methods.txt adds actual native SFC and DSFC full writer loops
at both clock origins. These use explicit stationary simulated observations;
task-performance eligibility remains false. Production constructor/model
admission and real transport qualification are separate, still incomplete.
