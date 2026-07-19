# Step5d V3 convergence contract

Status: frozen for the `2026-07-20` convergence lane.

This contract is the authoritative resume surface for the Step5d V3 task. It
exists to prevent selector drift, evidence-recursion, repeated repository
rediscovery, and completion claims that stop before an attended V3 bridge is
actually usable.

## Bound baseline

- Formal runtime base: `c6e9bdbffa97e3ea7103428138a8a07e2438e8fb`.
- V3 source baseline: `dcbe55c7bd99d00570127df512b5cd4663a69e71`.
- Release stage: `step5d_strict_rnn_autotune_v3`.
- Frozen control-profile provenance: `step5d_strict_rnn_autotune_v1`.
- TP program: `step5d_strict_rnn_autotune_v3`.
- Old trial bundles are immutable diagnostic evidence and are not a new
  optimizer population.

## Terminal condition

The task remains active until all of the following are true on the attended
bench:

1. The formal runtime checkout is at the published convergence OID.
2. V3 is the only selected/current Step5d autotune release and no V1 launcher
   or fallback remains runnable.
3. Fresh V3 TP read-back, campaign authorization, plant epoch, stopping-bound
   evidence, and return evidence bind the same release identity.
4. `campaign_ready` is true.
5. The canonical launcher starts that exact V3 bridge and emits
   `V3_BRIDGE_READY_NO_ARM` from the live process.

Offline tests, timing, commits, publication, deployment, reports, and a
simulated readiness signal are checkpoints only. They cannot complete the
task.

## Locked decisions

- `current` means the unique selected route. It does not mean deployed,
  authorized, armed, or campaign-ready.
- V3 is current even while a readiness dimension is false. A V3 failure must
  fail as V3 and must never select V1.
- “Open bridge” means start the V3 process in a no-motion, no-arm state. It
  does not authorize TP Play, mailbox motion, contact, sensor zeroing, or a
  campaign.
- Bridge start and ARM are separate gates. Motion mailbox input is ignored
  until an immutable arming context has passed all motion gates.
- `CertificationMotionAuthorization` and `CampaignAuthorization` are distinct
  typed capabilities and are not interchangeable. Machine-generated bindings
  never grant either capability.
- Timing uses `SCHED_OTHER`, `NI=0`, controlled affinity, and the frozen
  bounded-hold policy below. The older generic concurrency text mentioning
  `SCHED_FIFO/20` is superseded for this V3 acceptance only.
- Only behavior-affecting hot-path or safety changes invalidate timing.
  Selector text, documentation, output paths, read-back publication, evidence
  builders, and verifiers do not become timing subjects.
- Exactly one detached Fable/high advisory is allowed and is non-blocking.
  Exactly one formal pre-live review is allowed and must use Sol/XHigh.

## Frozen blockers

The blocker set is closed and may only move from open to complete:

1. Integration/runtime deployment.
2. V3 selector/current consistency.
3. Fingerprint split with no recursive evidence binding.
4. Bridge, loader, authorization, and campaign wiring.
5. Bounded-hold full-tick timing acceptance.
6. Fresh V3 TP upload/read-back.
7. No-contact stopping/return certification.
8. One Sol/XHigh pre-live audit.
9. Fresh campaign authorization.
10. Actual V3 bridge ready.

A newly observed problem must map to one of these blockers. A reproducible
safety contradiction may create a contract amendment, but it must be recorded
explicitly and reported to the user; a reviewer opinion alone cannot amend the
contract.

## Identity contract

- `tick_semantics_fingerprint`: hot-path control and safety implementation plus
  execution-shape/model/calibration inputs that the tick actually consumes.
- `timing_harness_fingerprint`: pacing, sample counts, measurement code, and
  bounded-hold policy.
- `runtime_environment_fingerprint`: interpreter/runtime, CUDA/CuPy/driver,
  kernel, scheduler, affinity, and thread environment.
- `deployment_fingerprint`: exact V3 TP triplet and controller read-back.
- `orchestration_fingerprint`: batch, resume, lifecycle, authorization,
  launcher, and promotion semantics.
- `release_fingerprint`: a canonical composition of the applicable identities
  and certified stopping/return evidence.
- `evidence_verifier_fingerprint`: provenance only; it never enters the subject
  identity it verifies.

Identity input must use canonical relative names and content, never absolute
worktree paths, timestamps, process IDs, hostnames, output roots, `current` or
`latest` pointers, artifact self-hashes, or verifier/builder self-hashes.

## Bounded-hold timing contract

- Solver: 10,000 samples, P99 <= 1.5 ms, max < 2 ms, zero misses.
- Full tick and safe hold: 30,000 samples, P99 <= 1.8 ms.
- Each paced lane: compute misses <= 300, schedule misses <= 300, maximum
  consecutive compute/schedule misses <= 10, maximum schedule lateness <=
  1.5 ms, and complete non-overflowing miss indices.
- A late candidate is discarded. The bridge holds the last guard-approved
  qdot and heartbeat, restores held qdot to solver history, and always lets an
  exact stop dominate hold.
- TP stale watchdog remains 20 ms.
- One formal attempt is allowed per frozen `(tick semantics, timing harness,
  runtime environment)` identity. Thresholds cannot change after results are
  observed and repeated attempts cannot be used to select a lucky run.

## Resume and change budget

After context compaction, read only this contract, the current convergence
state/event files, `v3_active_surface.json`, and the current intended diff.
Do not reread historical rollouts or rediscover the whole repository.

Each blocker permits at most two patch-to-targeted-test cycles. A third cycle
requires a root-cause checkpoint before any further patch. Write a durable
checkpoint after twenty non-trivial tool calls or approximately twenty-five
minutes. Run the combined impacted suite only at the integration boundary.

## Excluded work

No Step6/7/8, skill-sync, kernel change, Docker-group change, system package or
Pinocchio change, realtime-limit change, remote-desktop workflow, broad legacy
deletion, robot motion, contact, TP Play, sensor zero/tare, or live writer is
authorized by this offline convergence contract.
