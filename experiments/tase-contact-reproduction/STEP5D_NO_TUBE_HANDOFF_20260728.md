# STEP5d no-tube handoff — 2026-07-28

这份文件是 architecture/evidence projection，不是状态 authority。可执行
authority 是 `config/step5d/no_tube_handoff.json`、
`config/step5_stage_table.json` 中的同一 binding，以及
`HandoffStateStore` 产生的 transition receipts。其他 status 文件只能是
projection。

## 已实现的 primitive

| Primitive | bounded input | observable output | 独立 acceptance |
| --- | --- | --- | --- |
| r026 controller core | accepted r026 trial | terminal trial receipt | reuse existing successful r026 evidence |
| Script 1 positioner | immutable `step5d_autotune_start_hover_r001` | exact TCP pose + stationary evidence | package/read-back + Home observer tests |
| Startup trigger | exact Remote Control Dashboard Load/Play response | program identity and Dashboard response receipts | wrong identity/Safety/allow-list rejection test |
| RTDE Home observer seam | ordered read-only RTDE output samples | `HOME_VERIFIED` receipt | pose, speed, qdot, freshness/order, bounded host/controller sample gaps, Remote/Safety/robot mode, 0.5 s dwell |
| Queue | atomic candidate records | revision, candidate/request IDs, inflight identity | submit/dispatch/finish/recovery tests |
| Candidate feeder | sealed results + authoritative queue view | atomic refill receipt | no controller/bridge/RTDE/network imports; pending-only depth |
| Guard policy | observation + proposed command | `GuardDecision` | each policy independent; empty chain is identity |
| Metric/postprocess | sealed capture | metric/postprocess receipt | `.part` never enters BO; failure does not stop live writer |

## Executable startup contract

The sole public operator entrypoint is:

```text
scripts/step5d-autotune-v3.sh campaign-start
```

The coordinator's transition order is fixed:

```text
RELEASE_READY
  → QUEUE_READY
  → SCRIPT1_LOADED
  → SCRIPT1_PLAYED
  → HOME_VERIFIED
  → R026_LOADED
  → R026_IDENTITY_VERIFIED
  → BRIDGE_READY
  → CAMPAIGN_RUNNING
```

No transition is inferred from process liveness or log text. Each state has a
receipt; invalid order, wrong identity, missing queue watermark, stale Home
sample, unsafe status, or missing contract fails closed. The injected
`CampaignStartCoordinator` is independently testable and is now bound to the
route-bound Remote Control adapter in
`tools/step5d_remote_startup.py`. `--offline` remains an explicit no-network
preflight; the live entrypoint owns the complete Remote Load/Play → Home → r026
identity → canonical bridge → governed Remote Play composition.

Script 1 is the existing `step5d_autotune_start_hover_r001` identity. Its target
TCP pose is:

```text
p[0.487834547, 0.129337053, 0.033000000, 3.120752062, 0.000000000, 0.068626833]
```

The contract deliberately has `target_joint_q = null`. Script 1 has no bridge,
campaign lease, ARM, contact, zero, or tare lifecycle. Script 2 remains the
current immutable `step5d_strict_rnn_autotune_v3_r026` release with execution
profile `nf500-slew250-a250`; its existing controller core was not rewritten.

## Async candidate plane

The feeder is an independent candidate-plane process. The handoff manifest owns
`high=8` and `low=4`; the mechanism accepts these values and does not define
them. `QUEUE_READY` records the actual pending depth, candidate IDs, request
UIDs, queue revision, campaign ID, execution profile, and inflight identity.
Inflight is recorded but excluded from refillable pending depth.

The feeder reads sealed terminal postprocess results only. `.part.json`, symlink,
malformed, and non-`SUCCEEDED` results are rejected. It does not access
Dashboard, controller, bridge, RTDE, ARM, robot commands, or a live writer.
Existing queue buffer continues when feeder work fails. If pending capacity is
exhausted, the receiver's safe Home/hold path is the allowed safety boundary;
the feeder never submits an unvalidated candidate to preserve apparent motion.

## Runtime loops and composition seams

The acceptance/build DAG is:

```text
Script 1 package → startup trigger → HOME_VERIFIED → r026 identity → bridge/Play → campaign
Queue + accepted r026 worker → campaign executor
Sealed capture → metric → optimizer → feeder → Queue
```

The runtime feedback loop is separate:

```text
observation → controller → optional GuardPolicyChain → command → robot → observation
```

`GuardPolicyChain(())` returns the exact original proposed-command object with
`stop=false`. Tube is therefore an optional policy, not a lifecycle feature;
disabling it does not alter queue, release, startup, postprocess, or controller
identity.

## Validation and gate status

Offline focused validation passed:

```text
44 changed-primitive/seam tests passed
30 retained queue tests passed
11 retained feeder tests passed
32 canonical architecture/matrix tests passed
52 combined refactor-gate/matrix-runner/active-surface tests passed
```

The set covers the no-tube handoff, startup coordinator, Remote startup/bridge
fresh-attempt and Play-receipt contracts, bounded Home continuity, guard
identity, existing Script 1 package, queue contracts, and candidate feeder. Package
validation/read-back is bound to the existing two triplets; no new controller
program ID was created. This is offline proof and package/read-back proof only.
It is not live startup, bridge readiness, ARM, motion, capture completion,
postprocess completion, or campaign completion. No live action was run in this
implementation turn because the fresh owner resolver remains
`awaiting_live_authorization` with `live_motion_authorized=false`.

Current run boundary: existing r026 runtime continuity may be used as retained
evidence. The next new campaign must independently qualify the full
Script 1 → HOME_VERIFIED → Script 2/r026 → bridge → Play path.

## Next action — user

Inspect the Remote Control adapter diff and receipts, then run the next new
campaign through `campaign-start` once the existing live-authorization gate is
explicitly active. That qualification must collect fresh receipts for every
state and report offline proof, package/read-back, live startup, and campaign
completion separately.

## Open decisions for user

None — Remote Control is fixed by the active manifest and state table. The
remaining gate is the existing live authorization; queue watermarks remain
manifest-owned at 8/4, and campaign completion still requires separate sealed
capture → metric → optimizer → feeder evidence.
