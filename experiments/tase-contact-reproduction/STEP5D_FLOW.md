# Step5d current and staged flow

## Current stage

- Stage: `step5d_strict_rnn_autotune_v3`.
- Controller target: `step5d_strict_rnn_autotune_v3_r034.urp`.
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

- TP r022 bytes, safety envelope, register contract, force limits, and motion
  limits are unchanged.
- The 1.0 s identity value is a maximum deadline, not a fixed wait.
- State verification is event-driven; no formal 60 s qualification is used.
- TP and host wait indefinitely for Play or the next parameter while stationary at
  Home; an empty queue is not a fault.

## Parameter receiver

- The sender owns candidate-pool submission.
- The receiver does not seed an approved initial ten and starts/continues as an
  unbounded file queue with one inflight parameter.
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

## Staged independent V4 r002

- V4 is a separate `step5d_strict_rnn_autotune_v4` lineage and does not mutate
  V3/r034, its observations, GP, incumbent, current pointer, or fixed-2 ms
  contract.
- V4 r001 is historical and `SUPERSEDED_ARCHITECTURE_NOT_LOADABLE`; r002 is the
  only staged architecture candidate.
- Old and new EOATs are hash-bound typed profiles. Both use the same
  `apply_and_verify_eoat(profile, controller)` primitive; profile selection is
  data, while stationary, Safety NORMAL, single writer, and fresh GET match are
  invariants.
- Entry, force search, baseline, timing, qdot gate, and candidate provider are
  replaceable behavior primitives. Provider outputs pass through the fixed
  V4 invariant envelope before a typed wire packet can be published.
- Removing a non-safety provider degrades to zero qdot/no motion. Target 5 N,
  Kunwei-only authority, model hashes, hard guards, physical caps, timing
  stop conditions, schema, and EOAT readback are not removable.
- Baseline qualification has one owner:
  `BaselineQualificationLedger`. Only hash-bound terminal stage-22 receipts
  change the consecutive-success count; the typed TP command mode is
  `HOLD/BASELINE/PATH/RETRACT/STOP`.
- BO is an optional candidate provider. Without BO, the anchor/manual staged
  provider still produces useful bounded behavior.

### V4 capability DAG

`EOAT profile -> shared force search -> injected entry/timing/baseline/qdot
policies -> fixed invariant envelope -> typed wire -> attempt/replay evidence
-> optional candidate provider`

### V4 runtime feedback loop

`fresh Kunwei + robot observation -> local policies -> invariant envelope ->
typed command -> single RTDE writer -> guarded TP execution -> fresh
observation`

### V4 live blockers

- New EOAT physically installed and freshly apply/GET-verified.
- r006 live success.
- Three consecutive ledger-owned 5 N baseline receipts.
- Formal Review v3 and fresh Remote route-owner gates.
- Canonical lineage transition. Exact r002 triplet upload/read-back is closed
  by the controller-readback receipt; it did not Load/Play or select V4.

Until all blockers close, V4 remains no-Load/no-Play/no-motion and V3/r034
remains the active old-EOAT autotune path.
