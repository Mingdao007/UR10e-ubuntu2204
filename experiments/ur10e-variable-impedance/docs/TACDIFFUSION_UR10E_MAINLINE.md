# TacDiffusion on UR10e: offline mainline contract

This document fixes the mainline boundary before any controller mutation or robot run.

The target claim is exactly **UR10e 500 Hz force-domain diffusion adaptation**.

It is not a Panda 1 kHz exact reproduction and must not be described as one.

## Controller/runtime boundary

Offline reproduction preparation targets PolyScope 5.26.0 LTS, Direct Torque Control V2, and a 500 Hz controller-local torque loop.

The historical 5.25.2 upgrade, RTDE layout, URScript template, and exact-version URSim protocol remain immutable compatibility evidence.

The physical controller remains `controller_verified=false`. The existing 5.26.0.140462 version string is version-only evidence, not a runtime acceptance.

The 5.26 runtime evaluator requires independently hash-bound controller and robot identity, External Control URCap compatibility, installation/safety/TCP-payload configuration, robot and sensor calibration, sensor-to-TCP transform, stopped-program Dashboard/RTDE/network readback, and Direct Torque V2/Jacobian/dynamics API readback.

A complete system backup, Support File, separate `/programs`/installation/safety/calibration/URCap exports, and SHA-256 manifest must exist before installation.

PolyScope 5.26.0 is an LTS release with a 64-bit controller platform and a four-byte Primary/Secondary configuration-package change: [5.26 release notes](https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-526x/) ✅ confirmed.

PolyScope 5.25.0 changed PROFIsafe outgoing signals to active-low, so detected or unresolved PROFIsafe use is a hard preflight stop: [5.25 release notes](https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-525x/) ✅ confirmed.

URCap incompatibility is also a hard stop.

There is no automatic fallback to 5.23.

The firmware-update motor-power step does not authorize Play, a bridge, a torque session, or motion.

## Task and data boundary

The task remains Step5 curved-surface contact without vision.

Formal training data must be newly collected on the UR10e with an explicit deterministic expert producing and logging the 6D feed-forward wrench label \(F_{ff}\).

Existing v27 and v29 traces may validate parsing, frame transforms, causal synchronization, and command-invariant shadow behavior.

They do not contain formal expert \(F_{ff}\) labels and cannot become training or reproduction evidence.

Data collection advances through independently hash-bound 50-episode pilot and 200-episode formal batches.

An optional 1500-episode batch is allowed only if the frozen validation learning curve is still materially improving after the formal batch.

Splits are grouped by episode, frozen before training, and recorded in each batch manifest to prevent frame-level leakage.

Each schema-v2 episode manifest binds the exact 5.26 Direct Torque V2 500 Hz profile and sibling controller, sensor-calibration, sensor-to-TCP, bias, time-indexed task-ZFT, deterministic expert-definition, software, package, trace, split, and sample-count artifacts. The manifest alone is never training-eligible; the loader must rehash and re-evaluate the full graph.

## Force-domain contract

Each observation contains current and previous 18D samples.

Each sample is 6D external wrench, 6D internal wrench, and 6D end-effector twist, for a 36D condition.

The external wrench is the Kunwei 1 kHz raw stream after SI conversion, explicit zero/bias evidence, and sensor-to-TCP transform, causally synchronized to the UR 500 Hz tick.

The original 1 kHz stream remains available to the independent safety guard.

The internal wrench is reconstructed from the preceding tick's applied no-gravity joint torque, Jacobian, and dynamics terms.

It must never be a copy of the Kunwei wrench.

`actual_current_as_torque` is a shadow cross-check only.

The deterministic expert label is the pre-filter TCP-frame SI wrench `clamp(task_ZFT - K*pose_error + D*twist)`. The model proposes a raw 6D \(F_{df}\).

URScript runs the dynamic filter locally and applies the filtered \(F_{ff}\) in

\[
\tau = J^\top(F_{ff} + K e - D\dot{x}) + C(q,\dot{q}) - D_q\dot{q},
\]

with gravity supplied by `direct_torque()`.

Model staleness beyond two model periods, sequence gaps, nonfinite values, bounds violations, frame/calibration mismatches, or heartbeat loss must monotonically ramp \(F_{ff}\) to zero, enter damping, and then perform a controlled stop.

## Pinned upstream and adaptation boundary

The paper is [arXiv:2409.11047v2](https://arxiv.org/abs/2409.11047v2) (updated 2025-03-06; accepted to ICRA 2025) ✅ confirmed.

The official repository is [popnut123/TacDiffusion](https://github.com/popnut123/TacDiffusion) at commit `6a5567c829c54b7d03164cf40779d2451de4099e` ✅ confirmed.

The repository has no standard license file; its README says “No commercial use!”.

This mainline therefore records a noncommercial notice, not an SPDX license grant.

The UR10e port may independently implement the published model dimensions, force-domain schema, filter equations, and offline training concepts; it does not copy upstream source code under the repository's noncommercial notice.

It must not reuse the Franka MIOS, Docker, or UDP controller as the UR runtime.

The upstream pickle/random-split data path is replaced with an independent, episode-grouped, SHA-256-bound UR10e manifest.

The TacDiffusion-only checkpoint-v2 model keeps DDPM `T=50`, width `N=512`, separate current/previous 18D embeddings, a noisy-action embedding, 128D TimeSiren embedding, BatchNorm/GELU residual blocks, and filter parameters `alpha=0.9`, `beta=0.3`. Training defaults are 1500 epochs, batch size 4096, Adam `1e-3`, and cosine decay. Normalization is train-split-only, and resume restores optimizer, scheduler, Torch RNG, and permutation RNG state. Schema-v1 checkpoints are nonfaithful.

Inference is measured at 50, 100, 200, and 500 Hz.

The selected rate is the highest rate that completes a 60-second run with zero deadline misses, zero nonfinite outputs, and p99 latency no greater than 80% of its period.

If all rates fail, the model remains shadow-only.

## Independent acceptance and reviews

v29, v30, P0, VIC, DBIL, Gazebo, URSim, and TacDiffusion retain separate acceptance records.

Evidence from one domain cannot advance another.

Every new component advances only through:

`implemented -> deterministic_tested -> simulation_run -> hardware_run`.

Ordinary offline coding uses deterministic validation (`0+0`).

Before the first no-contact torque canary, the frozen fingerprint needs `1+1` review.

Before the first contact-torque or model-active TacDiffusion run, the frozen fingerprint needs `2+1` review and fresh live/contact authorization.

Hardware progression is fixed: no-contact fixed-impedance shadow, 2/10/60-second fixed-torque canary, low-risk deterministic-expert contact, expert-data collection, TacDiffusion shadow, and only then separately authorized model-active contact.

## Current status

This round is offline preparation only.

It does not upgrade or upload to the controller, start a bridge, press TP Play, zero the force-torque sensor, change networking, or produce robot motion.

The versioned 5.26 `URSimTransport` seam has no production I/O implementation. Fake captures cannot promote `simulation_run`; no URSim runtime, image pull, container, GPU training, formal dataset, or hardware timing run occurred.
