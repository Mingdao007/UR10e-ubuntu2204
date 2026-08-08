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
- Script2 binds the EOAT-specific fixed Cartesian Home and each typed ARM
  verifies position `<=1 mm`, orientation `<=0.01 rad`, then locks the actual
  q for that verified IK branch (`<=0.02 rad` on later ARM). It runs one
  integrated gentle negative-Z acquisition and force attempt, retracts,
  transfers XY at the clear height, returns to fixed Home, and enters
  `READY_HOME_NEXT`. Abnormal stop or return failure is STOPPED without
  auto-Home; recovery needs Script1 and a new epoch.
- The bounded plan is `3 qualification + Batch A 5 + Batch B 5 + retest
  3 = 16`. PATH requires the same durable ledger to contain qualification
  ordinals 1..3; promotion remains a separate disabled release gate. There is
  no standalone search/canary.
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
  max travel `0.025 m`, and timeout `90 s`. All-phase hard guards match V3/r034:
  absolute normal `60 N`, force norm `100 N`, torque norm `3 Nm`; the selected
  V3 profile has qdot `2.5 rad/s`, slew `2.5 rad/s2`, TP speedj acceleration
  `20 rad/s2`, XY path speed `0.004 m/s`, total/normal linear caps `1.0 m/s`,
  and angular cap `0.25 rad/s`. Return uses V3 rise/descent `a=.060,v=.040`
  and XY transfer `a=.135,v=.090`, ending at fixed Home.
- At TP and writer nominal 500 Hz, a newer packet updates the
  immutable cache; an equal sequence is reusable only when all fields are
  identical. Changed equal payload, sequence regression, or held age `>=80 ms`
  fails closed with reason 43. The pre-latch and post-latch force timers are
  separate `<=20 s` budgets; malformed latch/counter or latch regression stops.
- Controller readback is max age 300 s at Script2 Play and extends only within
  the same uninterrupted runtime identity/session epoch. Script1 start receipt
  is max age 120 s and binds exact Script1 SHA, final pose/q, stationary state,
  Safety NORMAL, and V4 EOAT identity. Stop/load/restart invalidates receipts.
- The qualification live boundary is a thin r004 adapter over the canonical
  V4 baseline state machine, calibrated runtime, Jacobian/qdot gate,
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
- Qualification/PATH timing acceptance independently requires writer publish,
  distinct RTDE, distinct Kunwei, and distinct TP-consumed rates `>=460 Hz`,
  feedback age p99 `<=10 ms`, and maximum fresh gap `<20 ms`; runtime stale
  `>=80 ms` still stops. PATH additionally requires real desired/actual XY,
  phase/time, velocity, qdot/actual_qd, 550 force bins, and guarded return.

### V4 r004 offline blockers

- No controller upload/read-back, Dashboard, Load, Play, bridge, ARM, contact,
  motion, sensor write, network, or promotion was performed in this closure.
- Live EOAT/readback freshness, owner route gates, and the first real
  qualification remain external blockers. Full candidate/path/retest and any
  lineage transition remain blocked. V3/r034 remains the active old-EOAT route.

## Staged independent V4 r005 offline implementation

- r005 is a new content-addressed child of the dirty r004 closure. Its release
  identity is `step5d_strict_rnn_autotune_v4_r005`; the parent map in
  `config/step5d/autotune_v4_r005.json` binds exact r004 source/config/package
  bytes. r004 identity, observations, qualifications, and controller target
  are not overwritten or imported into the r005 campaign.
- A fresh r005 epoch starts with three fresh passing qualifications, followed
  by the existing ten-row P/D bootstrap. The immutable target force is 5 N;
  `D=28` is only the bootstrap/incumbent anchor. After bootstrap, the complete
  named-7D domain is `P,D,tau,I_on_log2,I_off,Ko,Kp`, with bounded 0.25-octave
  lattice coordinates. A dispatched transition changes at most one physical
  coordinate and at most 0.25 octave; target force is never a search field.
- The only trainable force value is the typed, versioned
  `step5d.force-objective/v2` primitive. It consumes fresh immutable PATH
  samples at stage 25 and computes `force_mae_v2` over the half-open window
  `[5.0,60.0)` using 550 equal-width 0.1 s bins: average filtered normal force
  within each bin first, then average the 550 absolute bin errors to 5.0 N.
  The `[0.0,55.0)` `r004_legacy_shadow` uses the same bin-mean formula for
  audit only; it cannot train, trigger retest, or satisfy COMPLETE. A missing
  bin, stale/nonfinite sample, time regression, or conflicting source identity
  produces no trainable objective.
- The host is long-lived and owns the sequence
  `HOME -> dispatch -> ARM -> 60 s -> safe return -> durable seal/tell ->
  refill -> next ARM`. The TP stays resident at `READY_HOME_NEXT` and accepts
  any positive monotonically increasing attempt sequence; there is no 1..16
  ordinal gate. The asynchronous bound is exactly two pending rows plus one
  physical inflight dispatch.
- The V4 adapter reuses the V3 durable queue, one-inflight dispatch semantics,
  sealed observations, CUDA qLogNEI, and ask/tell types. qLogNEI receives
  observed and pending candidates through `X_pending`; observed/pending
  duplicates are excluded and a missing CUDA backend is a hard optimizer stop,
  never a local/degraded fallback. The control host never imports Torch: the
  lineage-neutral `tools/step5d_optimizer_runtime.py` resolver selects the
  canonical V3 `optimizer` profile, and the child worker self-attests its
  environment id/hash, exact Torch/BoTorch/GPyTorch versions, CUDA availability,
  and expected GPU name/UUID. Missing, stale, tampered, or mismatched runtime
  identity fails closed while still at Home.
- The production CLI composes the r005 ledger, V3 durable queue adapter,
  V4BoAdapter, one r005 RuntimePort, and one `R005MatureWriter` over the
  verified r004 RTDE/Kunwei/controller writer. The extension changes only the
  r005 identity and positive unbounded ARM policy; the mature transport,
  controller, Home, path, timing, and safety primitives remain the execution
  owner. Admission validates all fresh receipts and the exact r005 triplet
  before the writer can open; the CLI performs no Dashboard Load or Play.
- A safe returned but nontrainable row is sealed and durably excluded from the
  optimizer while the host continues. Code/evidence identity errors,
  qdot/actual_qd packet/RTDE/time binding errors, safety failures, and safe
  return failures revoke authority and stop incomplete. Safety failure revokes
  before any safe-return call. Outcomes are exactly `OBJECTIVE`,
  `SAFE_NONTRAINABLE`, `CODE_OR_EVIDENCE_BUG`, and
  `SAFETY_OR_RETURN_FAILURE`; qdot and actual_qd must share packet sequence,
  RTDE sequence, and bounded timestamp.
- Absolute-deadline pacing remains nominal 500 Hz. Writer, RTDE, Kunwei, and TP
  rates are distinct acceptance gates, each at least 460 Hz. A production live
  CLI has no stop-after-ordinal option; the diagnostic stop exists only under
  FakeRTDE/offline mode.
- When an eligible BO result has MAE `<=0.20 N`, all queued pending rows are
  durably cancelled and three incumbent retests begin. Completion requires at
  least two of three retests at `<=0.20 N`, all three motion/timing/contact/
  return/identity gates, and a retest median at least 5 percent below the
  bootstrap anchor median. A failed retest returns to BO; there is no trial
  count limit. Domain exhaustion, operator stop, or hard fault is incomplete.
- Resume performs a real fresh-process cold-read hash-chain verification of
  the r005 ledger. An incomplete physical attempt is reconciled only after
  Home, retaining its logical request identity while allocating a new
  execution id. r004 qualifications remain audit-only. The r005 triplet,
  numeric sanity, stage row, and offline closure are local preparation only;
  no controller upload/read-back, Dashboard, Load, Play, ARM, bridge, motion,
  contact, zero, tare, sensor write, network action, or live evidence exists
  in this closure.

## Staged independent V4 r006 offline implementation

- r006 is a new content-addressed child of the current dirty r005 closure.
  `config/step5d/autotune_v4_r006.json` binds exact r004/r005 parent bytes,
  while `lineage_selector_v4_r006.json` is a candidate pointer only.  r004 and
  r005 published identities, qualifications, ledgers, and package files are
  read-only audit parents; the canonical V3 selector is unchanged.
- `ActiveMotionEnvelopeV2` wraps the frozen V3/r034 execution profile instead
  of extending its allowed values.  Only active PATH limits double: XY and
  tangential `0.004 -> 0.008 m/s`, total/normal linear `1 -> 2 m/s`, angular
  `.25 -> .5 rad/s`, qdot `2.5 -> 5 rad/s`, host slew `2.5 -> 5 rad/s2`, TP
  `speedj` acceleration `20 -> 40 rad/s2`, and normal-vector update `100 ->
  200 rad/s`.  PATH omega `.1`, 60 s, amplitude `.015`, contact search,
  fixed Home/return `movel` values, hard force/torque guards, 500/460 Hz, and
  the 80 ms lease remain unchanged.
- The resident loop is `HOME_IDLE -> DISPATCH -> ARM -> ACTIVE 60s -> RETURN
  -> SEAL -> TELL -> REFILL`.  Home and intertrial waits are unbounded safe
  HOLD; the 80 ms deadline belongs only to an ARM-ed ACTIVE lease.  Attempt
  sequence is positive, monotonic, and unbounded.  A distinct controller/RTDE
  frame alone advances packet sequence; equal identity reuses only an exact
  payload.  Queue capacity is two pending plus one physical inflight, with a
  CUDA-required managed optimizer and no degraded fallback.
- The r006 receipt is builder-only and raw-bundle bound.  It seals per-bin
  count/sum, raw bundle digest, source sequence/time identity, attempt and
  campaign binding, semantic fingerprint, and sufficient-statistics digest.
  Cold-read reconstructs the formal objective from raw PATH stage-25 samples in
  `[5,60)` using 550 bins and equal-weight bin-mean absolute errors to 5 N.
  r004 `[0,55)` and r005 `abs(bin mean - 5)` remain explicit shadow metrics;
  scalar/mapping-only callers cannot become trainable.
- The graph is an unbounded quarter-octave lattice over `P,D,tau,I_mode/I_on,
  Ko,Kp`; target force is not a coordinate.  One physical transition changes
  one coordinate by at most `.25` octave, with the fixed OFF -> I-on `-1.0`
  portal.  A finite trust region is `+/- .5` octave over five shared axes and
  includes OFF plus five I-on points.  Traversal alternates optimistic
  boundary selection with deterministic BFS so the frontier cannot starve.
- After three fresh execution-id-bound qualifications, the first warm-start
  group is exactly 25 trials: anchor plus minus/anchor/plus/anchor for each of
  `P,D,tau,Ko,Kp`, followed by the four-trial I tail.  The second center is the
  lowest simultaneous-95%-UCB eligible point; the safe BFS route is included as
  observations, a second exact 25 is run, and GP hyperparameters then freeze.
  Home waits for typed versioned `pac_epsilon_n` and
  `application_mae_threshold_n`; absent values cannot ARM.
- Production GP features are `-log2(P), log2(D/P), conditional log2(I/P),
  log2(tau), log2(Ko), log2(Kp), I_mode` with shared, same-mode, and I-on-only
  Matern-5/2 components and fixed Gaussian `train_Yvar` floor `1e-4 N2`.
  Student-t variational and RBF models are command-invariant shadows only.
  qLogNEI always receives `X_pending`; the scheduler is q=4 until the local
  gap is at most `2 epsilon`, then q=1.
- The certificate is finite-region simultaneous 95% only and passes when
  incumbent UCB minus minimum LCB is at most epsilon.  A threshold result
  durably cancels pending rows and runs three retests; at least two must pass,
  the median must improve the anchor by at least 5%, and all gates must pass.
  COMPLETE requires application acceptance and local epsilon-PAC together;
  certified regions may recenter/expand, but global convergence is never
  claimed.  Terminal dispositions remain `OBJECTIVE`, `SAFE_NONTRAINABLE`,
  `CODE_OR_EVIDENCE_BUG`, and `SAFETY_OR_RETURN_FAILURE`; safety/return failure
  stops immediately without auto-Home.
- The offline FakeRTDE regression uses the existing r005 absolute-deadline and
  fresh-frame primitives at host 500 Hz versus RTDE 495 Hz for 60 s, proving no
  echo backlog and a real source stall stop at 80 ms.  The local TP triplet,
  numeric sanity, managed runtime manifest, and source closure are generated
  only for offline preparation.  No upload, Load, Play, ARM, bridge, motion,
  contact, zero, tare, network action, or live acceptance is claimed.
