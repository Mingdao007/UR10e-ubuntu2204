# Step5d current flow

## Current stage

- Stage: `step5d_strict_rnn_autotune_v3`.
- Controller target: `step5d_strict_rnn_autotune_v3_r013.urp`.
- Canonical launcher: `scripts/step5d-autotune-v3.sh bridge-live`.
- Current immutable release: `config/step5d/current.json`.

## Bridge and contact contract

- Bridge startup, TP Play, ARM, contact, and motion remain separate gates.
- Bridge startup never sends Load, Play, ARM, sensor zero, or motion.
- TP runtime identity uses output integer registers 35-37.
- After Play, identity may establish for at most 1.0 controller-time second.
- While identity is pending, the bridge publishes no command and consumes no
  mailbox entry.
- Safety outside NORMAL, timestamp regression, timeout, or post-verification
  identity drift fails closed.

## Guards and timing

- TP r013 bytes, safety envelope, register contract, force limits, and motion
  limits are unchanged.
- The 1.0 s identity value is a maximum deadline, not a fixed wait.
- State verification is event-driven; no formal 60 s qualification is used.
- TP and host wait indefinitely for Play or the next parameter while stationary
  at Home; an empty queue is not a fault.

## Parameter receiver

- The approved initial ten parameters are seeded once and run before any
  optional optimizer output.
- The receiver is an unbounded file queue with one inflight parameter.
- `next` requests may jump ahead of normal FIFO requests.
- A physically attempted parameter is never dispatched again. Data failures are
  terminal records for that attempt and the receiver continues.
- The optimizer is only an optional sender; it is not a bridge, runtime,
  release, or experiment dependency.
- Manual V2 has no active launcher or status route and is historical-only.

## Success condition

- The current release and controller read-back remain exact.
- Play establishes the exact runtime identity within 1.0 s without closing the
  bridge.
- The bridge reaches the governed waiting-for-ARM state with zero command.
- No live acceptance is claimed until an explicitly authorized bench run.
