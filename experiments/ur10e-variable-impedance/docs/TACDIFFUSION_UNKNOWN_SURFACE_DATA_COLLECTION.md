# TacDiffusion unknown-surface data-collection contract

This additive contract supersedes the live-routing and review-policy parts of
the retained v2 offline plan without rewriting historical evidence.

## Unknown surface

The physical curved surface is unknown to the controller. The v11 CAD
registration is offline/simulation evidence only. A live episode reference
provides a bounded nominal in-plane path, timing, target load, and a named
nominal task loading axis. It must not inject CAD height or CAD local surface
normals. Measured-load feedback follows the unknown height online.

The required bridge input is therefore an unknown-surface nominal reference,
not a curved CAD trajectory backend. Any field named `reaction_normal_base`
on the retained bridge must be treated as the nominal task loading axis until
the bridge contract is renamed; it is not a claim that the local surface
normal is known.

## What starts data collection

Starting the retained fixture-shadow bridge does not start TacDiffusion expert
data collection. Its TP package requires zero feed-forward wrench, and its
bridge does not persist the hash-bound 84D observation plus 12D expert-action
episode record.

Data collection starts only when all of the following are true:

1. The unknown-surface nominal reference is bound without CAD Z/normal
   feed-forward.
2. The bridge durably records every 500 Hz observation/action row and the
   causal 1 kHz Kunwei source lineage.
3. The applied deterministic-expert action, filter state, package identity,
   controller identity, calibration, bias, and episode split are hash-bound.
4. The exact package and controller runtime have passed no-motion validation.
5. The final frozen composite fingerprint passes one deferred `2+1` review
   and receives fresh contact authorization.

## Current implementation boundary

The constant-Z unknown-surface reference builder now consumes the taught XY
footprint, binds Z and orientation to a fresh episode-start TCP pose, and
checks both desired and actual TCP poses against a hard tube. The dedicated
expert-episode artifact writer durably validates full 84D observation and 12D
expert/applied-action rows with causal external-source lineage.

These components are offline/tooling accepted only. The current live bridge
still lacks the controller-telemetry and 1 kHz recorder integration needed to
produce training-eligible rows. A no-contact shadow artifact must set
`training_eligible=false`; equality of expert and applied action is mandatory
for a training-eligible artifact.

## PolyScope Simulation Mode

The physical controller's PolyScope Simulation button can run the exact TP
program without moving the robot. A retained trace may verify on the observed
5.26 runtime:

- URScript parsing and availability of `direct_torque()`, `get_jacobian()`,
  and `get_coriolis_and_centrifugal_torques()`;
- RTDE input/output register roundtrip and Stage25 entry;
- 500 Hz loop timing, heartbeat/sequence fault handling, and controlled stop;
- exact package hashes and the no-motion Simulation Mode state.

This is controller-runtime no-motion evidence, not physical torque/contact
acceptance. Complete `controller_verified` still separately requires the
controller and robot identity plus installation, safety, TCP/payload, URCap,
calibration, and API evidence.

## Review policy

The current effective policy is
`config/tacdiffusion_review_policy_v3.json`. No-contact canaries use
deterministic `0+0` validation and fresh no-contact authorization. One final
`2+1` review is deferred until immediately before expert-data collection
contact or model-active contact, after the composite fingerprint is frozen.
