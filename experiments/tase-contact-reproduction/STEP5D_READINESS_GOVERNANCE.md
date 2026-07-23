# Step5d readiness governance

This document defines the machine boundary behind any Step5d readiness claim. It
does not grant Play, ARM, motion, zero, tare, controller access, or network
authority.

## Canonical authority

- The only public live launcher is `scripts/step5d-autotune-v3.sh bridge-live`.
- The only public status reducer is
  `scripts/step5d-autotune-v3.sh status --json`.
- A caller that needs permission to use readiness language must additionally
  obtain `status --json --assert-state WAITING_FOR_IDENTITY_PLAY` or
  `WAITING_FOR_PLAY`; the returned claim is not a motion command.
- Every launch owns a monotonically fenced owner epoch. A second live owner is
  rejected while the first owner PID and `/proc` start time remain current.
- Every phase is journaled as `STARTED` and then `PASSED`, `FAILED`, or
  `CANCELLED`. A terminal attempt always wins over retained route evidence.
- Route selection is closed-world and uses one immutable Dashboard snapshot:
  exact Manual V2, exact current V3, or `BLOCKED`. An unknown loaded program is
  never interpreted as V3.
- A status without a current canonical launch attempt is blocked. It must not
  reuse an old Manual or V3 readiness state.

## Identity layers

The Manual TP payload and host runtime are independent identities.

- The immutable Manual release manifest binds only TP artifacts and their
  generated/read-back payload.
- The content-addressed Manual host binding separately binds repository source,
  `uv.lock`, and the runtime contract.
- Rebinding host source must not change the Manual TP pointer, the current V3
  pointer, or any controller file.
- V3 launch identity is updated from the freshly verified delivery observation
  after atomic promotion; the initial route snapshot remains only the routing
  decision evidence.

## Qualification boundary

Manual and V3 production qualification share one exclusive lease for the fixed
production-shaped localhost endpoints. Qualification runs the canonical shell,
real owner process, real bridge worker, production encoder/parser, and real
mailbox order; only Dashboard, secondary, RTDE, Kunwei, and TP endpoints are
substituted.

Manual qualification must demonstrate a persistent multi-heartbeat NO_ARM
canary, an exact process tree, zero ARM acknowledgements, and a clean process
group stop. Passing evidence is bound to the Manual payload, host binding,
runtime bundle, exact interpreter, endpoint content, and source surface. Red
endpoint and process evidence is retained on failure.

## Capability boundary

- Starting a bridge initially grants only `bridge=true`.
- Manual Play/ARM/motion requires a current external capability document bound
  to the same launch attempt, campaign, and release. The Manual campaign runner
  cannot create that document.
- V3 orchestration authority is the current fingerprint-bound campaign lease.
- Zero and tare remain false unless a separate UR owner action explicitly
  authorizes them; neither is implied by readiness.
- Qualification is always bridge-only and cannot publish a Play prompt.

## Machine states and claims

| State | Required evidence | Permitted claim |
|---|---|---|
| `BRIDGE_ALIVE_NO_ARM` | Current fenced owner, production-qualified path, live bridge heartbeat | Bridge exists; no Play/ARM/motion authority |
| `WAITING_FOR_IDENTITY_PLAY` | Same-attempt owner, qualification, fresh live predicates, current capability/lease; TP has not yet written current runtime identity | User may perform the first physical Play; not `BENCH_READY` |
| `WAITING_FOR_PLAY` | All prior predicates plus current TP runtime identity | User may perform physical Play |
| `PLAY_OBSERVED_IDENTITY_RECHECKED` | Play observed; fresh exact loaded-program and authorization rechecked | No Play prompt; ARM has not yet been acknowledged |
| `ARM_PENDING` | Exact ARM command has been published; TP has not acknowledged its full identity | Campaign is not yet `RUNNING` |
| `RUNNING` | Play observed, post-Play identity rechecked, exact TP ARM identity acknowledged | Campaign is running |
| `LIVE_PROVEN` | One trial complete, next ARM acknowledged, bridge heartbeat covers the acknowledgement | Outcome evidence only; never admission for a future run |

A readiness claim can only be minted for `WAITING_FOR_IDENTITY_PLAY` or
`WAITING_FOR_PLAY`. It is bound to the exact status digest and attempt and
expires after five seconds. Both Manual and V3 live workers refresh it while the
same pre-Play predicates remain true; any failed refresh stops the worker
fail-closed. Human prose, log text, elapsed time, and test counts are not
readiness evidence.

For Manual, every refresh performs a fresh Dashboard loaded-program observation.
Authorization and loaded identity are checked again after Play and before every
ARM. Publishing an ARM changes state only to `ARM_PENDING`; `RUNNING` requires
the exact campaign, trial, candidate, profile, sequence, and row identity to be
acknowledged by TP.

Public status also recomputes Manual heartbeat from both the bound PID and a
fresh production CSV write; a living but stalled process cannot mint a claim.

## Invalidation and recovery

The following events immediately suppress `play_prompt_ready`: owner death or
epoch change, route snapshot drift, terminal launch phase, release or host
binding drift, runtime/environment drift, qualification drift, capability or
lease expiry, heartbeat loss, controller/load/TP identity mismatch, single
writer loss, mailbox contamination, or external observation expiry.

After compaction or resume, the first observation is canonical `status --json`.
An internal reproducible failure requires retained red evidence, the earliest
regression, a fix, and full requalification. External or physical blockers
require positive device/network evidence and never authorize speculative code
changes.

## Offline acceptance

The offline governance change is acceptable only when all of the following are
true:

1. Repository validator passes with an explicit behavior-changing declaration.
2. Hermetic Small and Medium matrices pass in a clean environment.
3. Manual production qualification and V3 production qualification use the
   fixed endpoint lease and retain failure evidence.
4. Host bindings are regenerated after the last governed source change.
5. The change is committed and reviewed through a PR; the runtime checkout is
   updated only by post-merge fast-forward deployment.

Offline acceptance never means the controller, TP, Kunwei sensor, network, or
physical bench is ready. Those predicates can only be observed after the bench
is powered on.
