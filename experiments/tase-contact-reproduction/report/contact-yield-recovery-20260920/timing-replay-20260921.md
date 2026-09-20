# Timing replay and live gate status

## Observed live blocker

The current resolver remains `liveprep_blocked`. The retained stopped-NORMAL
qualification evidence records a measured lower bound of `11.450823 ms` between
accepted control samples and `11.224208 ms` from the first accepted control
sample through the send wrapper. The hard readiness limit remains `(0, 4 ms]`.

No timing threshold, deadline, motion limit, or admission rule was relaxed.

## Offline repair

Commits `9e4aacdc` and `63d39acd` add an opt-in monotonic hot-path trace and
make first-sample metadata explicit. The trace separates control/provider,
wire build, transport send, post-send tracing, packet history/evidence, and
scheduler boundaries. Failed ticks restore the control time anchor and
provider state. First samples retain `actual_dt_s=null`; later samples carry
the measured writer monotonic interval.

Offline replay showed active ticks around `1.6 ms`, transport around `0.001 ms`,
and post-send/evidence around `0.017 ms`. It did not reproduce the live
`11.224208 ms` delay, so no non-control work was moved out of the live loop.

Focused validation: `78 passed`, one existing xacro deprecation warning.

## Promotion boundary

The instrumentation is offline-ready only. A fresh live timing receipt is still
required before bridge admission or the first Figure-eight run. The current
package/read-back identity is verified, but `dispatchable=false` and no live
motion is authorized by the resolver.
