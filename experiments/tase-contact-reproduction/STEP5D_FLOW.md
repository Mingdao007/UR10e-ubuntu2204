# Step5d current flow

## Current stage

- Stage: `step5d_strict_rnn_autotune_v3`.
- Controller target: `step5d_strict_rnn_autotune_v3_r012.urp`.
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

- TP r012 bytes, safety envelope, register contract, force limits, and motion
  limits are unchanged.
- The 1.0 s identity value is a maximum deadline, not a fixed wait.
- State verification is event-driven; no formal 60 s qualification is used.
- Each default bridge attempt receives a unique campaign root.

## Success condition

- The current release and controller read-back remain exact.
- Play establishes the exact runtime identity within 1.0 s without closing the
  bridge.
- The bridge reaches the governed waiting-for-ARM state with zero command.
- No live acceptance is claimed until an explicitly authorized bench run.
