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

## Release-contract boundary

`release-contract-check` is a pure, no-network, no-subprocess, no-trial state
transition. Its immutable certificate binds the exact candidate, source,
launcher, runtime, and safety scope. Any binding change requires a new
certificate; it never simulates Play, ARM, a trial, or elapsed robot time.

Delivery separately requires exact TP bytes and a fresh controller GET. Neither
the certificate nor delivery proves a live bridge or grants physical authority.

## Capability boundary

- Starting a bridge initially grants only `bridge=true`.
- Manual Play/ARM/motion requires a current external capability document bound
  to the same launch attempt, campaign, and release. The Manual campaign runner
  cannot create that document.
- V3 orchestration authority is the current fingerprint-bound campaign lease.
- Zero and tare remain false unless a separate UR owner action explicitly
  authorizes them; neither is implied by readiness.
- A release contract grants no bridge, Play, ARM, motion, zero, or tare authority.

## Machine states and claims

| State | Required evidence | Permitted claim |
|---|---|---|
| `RELEASE_CONTRACT_PROVEN` | Exact candidate certificate; no live process required | Offline release evidence only |
| `WAITING_FOR_IDENTITY_PLAY` | Exact delivered release, same-attempt owner, fresh live predicates, current capability/lease; TP has not yet written current runtime identity | Bridge is ready for separately authorized physical Play; ARM remains closed |
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
binding drift, runtime/environment drift, release-contract drift, capability or
lease expiry, heartbeat loss, controller/load/TP identity mismatch, single
writer loss, mailbox contamination, or external observation expiry.

After compaction or resume, the first observation is canonical `status --json`.
An internal reproducible failure requires retained red evidence, the earliest
regression, a fix, and deterministic revalidation. External or physical blockers
require positive device/network evidence and never authorize speculative code
changes.

## Offline acceptance

The offline governance change is acceptable only when all of the following are
true:

1. Repository validator passes with an explicit behavior-changing declaration.
2. Hermetic Small and Medium matrices pass in a clean environment.
3. Release-contract purity, certificate binding, and delivery/readback tests pass.
4. Host bindings are regenerated after the last governed source change.
5. The change is committed and reviewed through a PR; the runtime checkout is
   updated only by post-merge fast-forward deployment.

Offline acceptance never means the controller, TP, Kunwei sensor, network, or
physical bench is ready. Those predicates can only be observed after the bench
is powered on.
