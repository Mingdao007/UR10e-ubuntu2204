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

## Staged independent V4 r004

- V4 r004 is a separate `step5d_strict_rnn_autotune_v4` source closure. It
  leaves V3/r034, its observations/GP/incumbent, `config/current.json`, and
  every V4/r003 byte untouched. The V4-only pointer is
  `config/step5d/lineage_selector_v4_r004.json`; it is not the canonical
  current selector.
- Script1 is the frozen byte-identical
  `step5d_autotune_start_hover_r001` package, bound to exact pose
  `p[0.487834547,0.129337053,0.033,3.120752062,0,0.068626833]`. It runs only
  initially and after STOP/problem/restart, never before each uninterrupted
  success. Script2 is the new resident
  `step5d_strict_rnn_autotune_v4_r004` package.
- One Script2 Play captures campaign Home pose/q once. Each typed ARM runs one
  integrated gentle negative-Z acquisition and force attempt, retracts, reaches
  transfer-floor Z `>=0.062863519 m`, returns to captured Home, and enters
  `READY_HOME_NEXT`. Abnormal stop or return failure is STOPPED without
  auto-Home; recovery needs Script1 and a new epoch.
- The ordered campaign is exactly `3 qualification + Batch A 5 + Batch B 5 +
  retest 3 = 16` logical attempts. There is no standalone search/canary.
  The capability DAG and runtime feedback loop are separate contracts:
  `typed wire -> bounded contact/baseline/return -> durable campaign policy`
  versus `fresh Kunwei packet -> immutable TP cache -> guarded motion ->
  output/ledger evidence`.
- Physical invariants are target `5.0 N`, `D=28`, Kunwei-only wrench, no UR
  built-in force, and no sensor zero/tare/config command. The layout-606 wire
  retains doubles 24..47 and maps integer inputs 24..32 to baseline count,
  CommandMode, sticky 1 N latch, SessionCommand, command sequence, epoch,
  ordinal, kind, and candidate token. SessionCommand is HOLD=0, ARM=1,
  COMPLETE=2, STOP=3. Outputs 24..34 echo epoch, ordinal, state, token,
  reason, consumed sequence, kind, return guard, runtime protocol, and the
  two-limb runtime identity digest. The digest binds
  `program+contract_sha256+campaign_fingerprint`; session epoch is echoed as
  its separate output field.
- Contact acquisition is negative-Z base motion at `0.0002 m/s`, acceleration
  `0.005 m/s2`, positive normal-load gate `0.5 N`, force-norm gate `0.7 N`,
  max travel `0.025 m`, and timeout `90 s`. Baseline/path guards are absolute
  normal `15 N`, force norm `20 N`, torque norm `1 Nm`, qdot `0.15 rad/s`;
  entry/return speed is bounded by `0.01 m/s`. Return success additionally
  requires retract `>=5 mm`, transfer floor, captured Home position `<=1 mm`,
  orientation `<=0.01 rad`, and joint error `<=0.02 rad`.
- At TP 500 Hz versus nominal writer 125 Hz, a newer packet updates the
  immutable cache; an equal sequence is reusable only when all fields are
  identical. Changed equal payload, sequence regression, or held age `>=80 ms`
  fails closed with reason 43. The pre-latch and post-latch force timers are
  separate `<=20 s` budgets; malformed latch/counter or latch regression stops.
- Controller readback is max age 300 s at Script2 Play and extends only within
  the same uninterrupted runtime identity/session epoch. Script1 start receipt
  is max age 120 s and binds exact Script1 SHA, final pose/q, stationary state,
  Safety NORMAL, and V4 EOAT identity. Stop/load/restart invalidates receipts.
- The live boundary is a thin r004 adapter over the canonical
  `tools/step5d_bridge_authority.py` lease and the existing V4
  `WritableRTDEClient` plus `LiveKunweiTransport` mechanics. r004 uses the same
  canonical `step5d-bridge-writer` resource as V3/V4, while its route and
  attempt IDs are explicitly r004-bound. The adapter owns only layout-606
  recipe/echo checks; it has no Dashboard Load/Play path and no duplicate
  socket/parser/packing implementation. Cleanup is a safe STOP packet, Kunwei
  STOP_STREAM, RTDE close, then canonical lease revoke.
- Every durable campaign row records epoch, immutable attempt execution ID,
  controller receipt SHA, Script1 receipt SHA, input baseline ledger SHA, and
  output ledger SHA. Next ARM requires fsync, cold-read, and live
  `verify_ledger_hash_chain`; interrupted IDs are ineligible for GP. Resume
  verifies the last completed output seal and retries the same ordinal with a
  new execution ID, Script1, and epoch.
- Offline acceptance requires 550 bins, effective rate `>=75 Hz`, p99 packet
  interval `<=20 ms`, max interval `<80 ms`, and passing safety/contact/return
  gates. Promotion requires at least 2/3 retests with MAE `<=0.30 N` and
  candidate median objective `<=0.95 *` frozen anchor median. Otherwise the
  campaign completes with no promotion and the anchor remains immutable.

### V4 r004 offline blockers

- No controller upload/read-back, Dashboard, Load, Play, bridge, ARM, contact,
  motion, sensor write, network, or promotion was performed in this closure.
- Live EOAT installation/readback, live success, owner route gates, and any
  canonical lineage transition remain external blockers. V3/r034 remains the
  active old-EOAT route.
