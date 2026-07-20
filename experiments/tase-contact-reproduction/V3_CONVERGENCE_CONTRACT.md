# Step5d V3 convergence contract

Status: frozen for the `2026-07-20` convergence lane, with the user-directed
timing amendment recorded below.

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
- The current V3 convergence does not require a formal 10k/30k timing pressure
  test or a temporary CPU/GPU timing service. The existing equivalence
  attestation and short diagnostic remain advisory evidence only.
- CPU/GPU scheduling setup is not an experiment, bridge-start, ARM, or campaign
  prerequisite. This lane must not start or modify the timing service.
- Exactly one detached Fable/high advisory is allowed and is non-blocking.
  Exactly one formal pre-live review is allowed and must use Sol/XHigh.

## Frozen blockers

The blocker set is closed and may only move from open to complete:

1. Integration/runtime deployment.
2. V3 selector/current consistency.
3. Fingerprint split with no recursive evidence binding.
4. Bridge, loader, authorization, and campaign wiring.
5. Timing disposition: `not_required_by_user`; no pressure-test or timing-service
   action remains on the mainline.
6. Fresh V3 TP upload/read-back.
7. No-contact stopping/return certification.
8. One Sol/XHigh pre-live audit.
9. Fresh campaign authorization.
10. Actual V3 bridge ready.

A newly observed problem must map to one of these blockers. A reproducible
safety contradiction may create a contract amendment, but it must be recorded
explicitly and reported to the user; a reviewer opinion alone cannot amend the
contract.

## Blocker 7 certification seam

Blocker 7 is complete only when one production lane consumes an unexpired
`CertificationProcedureTicket` before each bounded procedure and the existing
production bridge remains the sole owner of Kunwei input, RTDE input/output,
exact-stop transport, and retained source-exact telemetry. The TP package may
execute only the ticketed no-contact primitive and guarded return; it must not
own authorization, evidence identity, optimizer state, or campaign readiness.

The lane must cover three retained samples for both `direct_exact_stop` and
`stale_watchdog_exact_stop`, followed by one source-exact three-segment
`return_route` capture. Every capture binds the exact bridge-start context,
deployment read-back, plant epoch, and certification-authorization digest.
Contact, a non-NORMAL safety mode, missing/nonfinite input, stale identity,
expired authorization, procedure mismatch, or incomplete safe closure fails
closed and cannot be promoted.

Analyzer-only code, hand-authored measurement JSON, historical campaign CSV,
safe-Z inference without live force/RTDE observation, a campaign ARM, or a
machine-generated authorization cannot satisfy blocker 7. Certification never
creates an optimizer observation and cannot start or authorize a campaign.

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

## Timing disposition amendment

On `2026-07-20`, the user explicitly removed the formal timing pressure test
from this convergence lane. No 10k/30k run will be performed, and
`ur10e-step5d-timing-env.service` will not be started or modified. Existing
timing artifacts are retained only as diagnostic provenance; they are not a
deployment, bridge-start, ARM, or campaign gate. A future behavior-affecting
hot-path or safety change may require a separately authorized timing
assessment, but it cannot silently reopen this Goal's blocker set.

## Resume and change budget

After context compaction, read only this contract, the current convergence
state/event files, `v3_active_surface.json`, and the current intended diff.
Do not reread historical rollouts or rediscover the whole repository.

Each blocker permits at most two patch-to-targeted-test cycles. A third cycle
requires a root-cause checkpoint before any further patch. Write a durable
checkpoint after twenty non-trivial tool calls or approximately twenty-five
minutes. Run the combined impacted suite only at the integration boundary.

The campaign runtime retains its frozen ceiling of 12 modules and 5200
non-comment source lines. The attended no-contact certification owner is
accounted separately as exactly one named module with a 320-line ceiling; this
exception cannot be reused by campaign, optimizer, or general orchestration
code and does not raise the core runtime ceiling.

## Excluded work

No Step6/7/8, skill-sync, kernel change, Docker-group change, system package or
Pinocchio change, realtime-limit change, remote-desktop workflow, broad legacy
deletion, robot motion, contact, TP Play, sensor zero/tare, or live writer is
authorized by this offline convergence contract.
