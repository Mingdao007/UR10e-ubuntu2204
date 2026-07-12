# UR10e Variable Impedance Offline Preparation

This experiment is an **offline-only** VIC and diffusion-based impedance
learning scaffold. It is deliberately separate from
`experiments/tase-contact-reproduction/` and does not change the v29/v30
package pointer, controller, bridge, network, FT zero, or robot state.

Current status: `offline_scaffold`; `controller_verified=false`;
`live_motion_authorized=false`; `dbil_active_enabled=false`;
`tacdiffusion_active_enabled=false`.

## Control surfaces

| Policy | ZFT source | Output | Allowed claim |
|---|---|---|---|
| fixed | configured nominal ZFT | diagonal K/D | offline baseline |
| scripted VIC | nominal ZFT + non-increasing phase schedule | diagonal K/D | offline ablation |
| deterministic VIC | nominal ZFT + shared directional adaptor | diagonal K/D | offline/shadow evidence |
| DBIL shadow | predicted sZFT + the same adaptor | diagonal K/D | public-data pipeline/shadow evidence only |

The phase-1 directional adaptor is a bounded, slew-limited, non-increasing
stiffness heuristic. It is deliberately not described as passive: the
`safe_low` stiffness floor and a moving equilibrium can inject energy. A future
active stiffness-increase phase requires an energy tank or passivity
observer/controller and a separate authorization gate.

All policies emit the same validated `ImpedanceProposal`. The supervisor holds
a last-fresh proposal for at most two model periods, then ramps stiffness toward
`safe_low`, enters damping-only behavior, and requests stop. Within one run,
stiffness can stay constant or decrease; recovery/increase requires a new run
after a later contact-release gate.

Shadow capability is separated from command capability twice: every backend
rejects `shadow_only=true`, and `CapabilitySeparatedShadowMux` routes the
established output command without consulting the proposal. The v27/v29
evidence compares the same actual command stream through shadow-off and
computed-shadow-on mux calls; it does not hash a copied “after” array.

`VICSimulatorAdapter` adds an offline-only capability boundary for joint
simulation. Fixed, scripted, and deterministic VIC may be made
`simulation_active` only by an explicit constructor capability; policies stay
shadow-safe and the adapter grants command capability after supervision. Exact
policy-type matching prevents `DBILShadowPolicy` from inheriting this ability.
DBIL active is rejected independently by proposal, run-manifest, DBIL config,
and simulator-adapter contracts. Phase 1 changes translational K only;
rotational K and the orientation command remain fixed. Full-adapter tests pack
the validity bit and six actual-command `float64` values and compare shadow off
versus shadow on bit-for-bit.

## Backend claim boundary

- `velocity_admittance_surrogate` cannot invent a Cartesian controller from K/D.
  It accepts only a fresh, sequence-matched command from the hash-bound pure
  Step5b contact core, preserves tangent/orientation, and applies only a
  dimensionless `sqrt(K_eff/K_baseline)<=1` reaction-normal modulation before
  DLS. The current Step5b ROS2 live-runner acceptance artifact is false, so the
  checked-in binding keeps this route command-disabled. It is always labelled
  `backend_fidelity=surrogate`; it is not true torque impedance.
- `direct_torque_vic_offline_template.script` is now a hash-bound PolyScope
  5.25.2 Direct Torque V2 design target. It contains the local 500 Hz formula
  `tau = J^T(F_ff + K e - D xdot) + coriolis - joint_damping*qd`; gravity is
  excluded because `direct_torque()` compensates gravity internally. The
  template has no invocation, defaults to disabled, and has no upload/network
  path.
- The 5.25.2 template is not executable on the last recorded 5.11.9 controller.
  It remains blocked until the conditional upgrade, fresh readback, and matching
  URSim validation cover syntax, one-tick runtime, heartbeat exit, and timing.
  `controller_verified` remains false in every checked-in artifact.

The RTDE manifest binds exactly 24 input doubles (equilibrium pose, K, D, and raw
six-axis `F_df`), plus integer control/model mode, sequence, period, frame token,
heartbeat, and exclusive lease. Sequence is the packet commit:
the controller reads it before and after the payload, requires both reads and
heartbeat to match, and requires exact `+1` advancement. Controller-side gates
also enforce K bounds/down-slew, `D=2*zeta*sqrt(K*M)`, translation-only VIC,
fixed orientation, equilibrium slew, new-run release/baseline reset, zero-wrench
startup ticks, force/torque/TCP-cage/joint guards, and finite bounded damping
exit torque. A frozen, mixed, gapped, or lease-switched packet fails closed.

Raw model output is filtered locally with the paper's second-order equation at
500 Hz. A proposal may be held for at most two declared model periods. Stale,
gapped, nonfinite, out-of-bounds, or wrong-frame input monotonically removes only
feed-forward force that was actually applied before entering damping and a
controlled stop. Shadow mode computes and reports the filter state but routes
exact zero feed-forward force to the torque path, including during fault exit.
The checked-in template has a hash-bound compile-time
`model_active_allowed=false`; host registers alone cannot enable model-active
torque.

## TacDiffusion force-domain lane

The new `ur10e_vic.tacdiffusion` module is part of this experiment rather than a
second control stack. Its force-only observation is 36D: current and previous
external wrench, internal wrench, and end-effector twist, all in one explicitly
bound TCP frame. Kunwei input is converted to SI, transformed with a calibrated
sensor-to-TCP lineage, and causally sampled from the retained 1 kHz stream onto
the 500 Hz control grid. The raw 1 kHz stream remains independent safety input.

Internal wrench is reconstructed from the preceding tick's applied no-gravity
joint torque, Jacobian, Coriolis term, and joint damping. It has no external
wrench argument, so the Kunwei signal cannot be copied into the internal channel.
`actual_current_as_torque` is accepted only as a separate shadow comparison.

Formal datasets use episode-grouped, non-pickled NPZ files with 36D condition,
6D expert `F_ff`, episode identity, frozen split, frame/calibration lineage, and
artifact hashes. Every episode manifest must bind the exact
`polyscope-5.25.2-direct-torque-v2-500hz` profile and its controller readback
hash, and its declared sample count must match the dataset. v27/v29 traces stay
pipeline-only and cannot be promoted into expert labels. Data collection is
staged at 50 episodes, 200 episodes, and an optional 1500 episodes only when the
frozen validation curve still improves.

The first portable model is a clean-room DDPM MLP design pinned to 50 denoising
steps, hidden width 512, current-plus-previous conditioning, and seed 42. The
runtime stays inactive and shadow-only until real expert data, checkpoint, and
independent timing evidence exist. Rates 50/100/200/500 Hz must each be tested
for 60 seconds; the selector accepts only the highest rate with zero deadline
misses, zero nonfinite output, and p99 no greater than 80% of its period.

The only allowed eventual claim is **UR10e 500 Hz force-domain diffusion
adaptation**. It is not a Panda 1 kHz exact reproduction or Panda-to-UR10e
zero-shot result.

The portable CLI has no controller transport. Typical offline commands are:

```bash
python3 -m ur10e_vic.tacdiffusion.cli validate-dataset \
  --dataset /external/ur10e-expert.npz \
  --manifest /external/ur10e-expert-manifest.json \
  --trace-manifest /external/episode-001.json
python3 -m ur10e_vic.tacdiffusion.cli train \
  --dataset /external/ur10e-expert.npz \
  --dataset-manifest /external/ur10e-expert-manifest.json \
  --trace-manifest /external/episode-001.json \
  --checkpoint /external/tacdiffusion.pt \
  --checkpoint-manifest /external/tacdiffusion-checkpoint.json
python3 -m ur10e_vic.tacdiffusion.cli benchmark \
  --checkpoint /external/tacdiffusion.pt \
  --checkpoint-manifest /external/tacdiffusion-checkpoint.json \
  --condition-json /external/condition.json \
  --duration-per-rate-s 60 \
  --output /external/tacdiffusion-timing-raw.json
```

`evidence/tacdiffusion_synthetic_canary_60s.json` is a 30,000-tick logical
packet/filter oracle run. It proves shadow command invariance and selected
fail-closed transitions in Python; it is explicitly not URSim, wall-clock
500 Hz, or hardware evidence.

## Conditional controller upgrade

`config/controller_5_25_2_upgrade_preflight.json` records the 2026-07-14
checkpoint. `tools/validate_controller_5_25_2_upgrade.py` evaluates captured
preflight and post-upgrade JSON without containing any controller transport.
Preflight requires complete backup/Support File/exports, official URUP hash,
FAT32 media, URCap compatibility, and explicit PROFIsafe breaking-change review.
Post-upgrade readback must preserve installation/safety/calibration/URCap hashes,
report exactly 5.25.2, and expose Dashboard, RTDE, network, Direct Torque V2,
Jacobian, and dynamics APIs without Play, bridge, torque, or motion.

Mainline reconciliation is independently recorded in
`config/mainline_reconciliation_20260713.json`: the dirty Ubuntu checkout remains
untouched, while its binary patch, untracked archive, per-path hashes, and
migration decisions live in the external handoff bundle.

## Portable DBIL workflow

The first configuration is pinned to a 16-sample window, hidden size 512, four
attention heads, six Transformer layers, 20 diffusion steps, and seed 42. The
upstream source is pinned in `config/dbil_upstream_lock.json`; datasets,
statistics, and checkpoints stay outside Git and are SHA-256 bound.

The retained trained model is explicitly `portable_component_ddpm_v0`; its
checkpoint is not upstream-faithful. A separate
`upstream_slerp_cross_attention_v1` core now implements the pinned source's
pose-query/wrench cross-attention, quaternion SLERP noise, relative-quaternion
target/loss, iterative subtract/inverse-multiply reconstruction, and per-window
stiffness estimate. That core is unit-tested but has not been trained, so no
faithful checkpoint or reproduction claim exists.

The portable dataset is a non-pickled NPZ with:

```text
pose_history    float [N, 16, 7]   # xyz + scalar-first quaternion
wrench_history  float [N, 16, 6]
target_s_zft    float [N, 16, 7]
split           int8  [N]          # file-level train/validation/test/application
```

Example offline commands:

```bash
cd experiments/ur10e-variable-impedance
export PYTHONPATH="$PWD"
python3 -m ur10e_vic.cli validate-lock \
  --lock config/dbil_upstream_lock.json
python3 -m ur10e_vic.cli dataset-manifest \
  --dataset /external/data/parkour.npz \
  --output /external/evidence/parkour-manifest.json \
  --stats-output /external/evidence/parkour-stats.json
python3 -m ur10e_vic.cli convert-parkour \
  --source-root /external/upstream/Data/Parkour \
  --source-git-root /external/upstream \
  --dataset-output /external/data/parkour.npz \
  --manifest-output /external/evidence/parkour-manifest.json \
  --stats-output /external/evidence/parkour-stats.json
python3 -m ur10e_vic.cli train \
  --dataset /external/data/parkour.npz \
  --stats /external/evidence/parkour-stats.json \
  --checkpoint /external/checkpoints/dbil-seed42.pt
python3 -m ur10e_vic.cli infer-window \
  --checkpoint /external/checkpoints/dbil-seed42.pt \
  --checkpoint-sha256 CHECKPOINT_SHA256 \
  --stats /external/evidence/parkour-stats.json \
  --stats-sha256 STATS_SHA256 \
  --observation-json /external/evidence/ur10e-observation.json
python3 -m ur10e_vic.cli convert-ur-trace \
  --csv /external/v29/bridge_rtde_500hz.csv \
  --dataset-output /external/evidence/v29-observations.npz \
  --manifest-output /external/evidence/v29-observations-manifest.json \
  --command-kind step5b_twist \
  --calibration-artifact /external/evidence/sensor-calibration-lineage.json \
  --frame-transform-artifact /external/evidence/wrench-frame-transform.json \
  --task-zft-artifact /external/evidence/time-indexed-task-zft.json
python3 -m ur10e_vic.cli ablate-ur-trace \
  --dataset /external/evidence/v29-observations.npz \
  --checkpoint /external/checkpoints/dbil-seed42.pt \
  --checkpoint-sha256 CHECKPOINT_SHA256 \
  --stats /external/evidence/parkour-stats.json \
  --stats-sha256 STATS_SHA256 \
  --output /external/evidence/v29-four-policy-shadow.json
python3 -m ur10e_vic.cli benchmark-rates \
  --checkpoint /external/checkpoints/dbil-seed42.pt \
  --checkpoint-sha256 CHECKPOINT_SHA256 \
  --stats /external/evidence/parkour-stats.json \
  --stats-sha256 STATS_SHA256 \
  --observation-json /external/evidence/observation.json \
  --duration-per-rate-s 60 \
  --output /external/evidence/paced-200-100-50-candidate.json
python3 -m ur10e_vic.cli select-rate \
  --evidence /external/evidence/paced-200-100-50-candidate.json \
  --checkpoint /external/checkpoints/dbil-seed42.pt \
  --stats /external/evidence/parkour-stats.json \
  --observation-json /external/evidence/observation.json \
  --output /external/evidence/paced-200-100-50-selection.json
```

Training and inference require a separate PyTorch environment; PyTorch is not
silently added to the robot data environment. The converter assigns whole
source files—not overlapping windows—to deterministic train/validation/test
splits, preventing overlap leakage. The rate selector accepts the
highest of 200/100/50 Hz with at least 60 seconds of evidence, zero deadline
misses, and p99 no greater than 80% of its period. If none passes, DBIL remains
shadow-only.

The full pinned Parkour conversion now covers all 20 usable text logs and
54,292 windows (37,866 train / 6,492 validation / 9,336 test / 598 application),
with every source checked against its pinned Git blob. Training remains the
earlier five-log subset: 10,431 windows, 2,689 training windows, and 20 MPS
epochs. Full-data conversion is not full-data training and neither is a paper
or hardware reproduction.

The timing producer now writes every scheduled release/start/end/nonfinite flag
and always marks its output as an unvalidated candidate. A separate validator
rehashes checkpoint/statistics/observation/harness sources, binds runtime/device,
recomputes p99 and deadline misses from raw ticks, and only then emits a
selection manifest. The current raw-per-tick Mac MPS trials each ran for at
least 60 seconds and the independent validator accepted their provenance.
All three still failed: p99 latency was 59.315/70.100/74.292 ms at
200/100/50 Hz, with 11,995/5,994/3,000 deadline misses respectively; all
outputs remained finite. No model rate is selected and DBIL remains
shadow-only. This is evidence for the portable subset checkpoint on Mac MPS,
not an upstream-faithful checkpoint or Ubuntu RTX result. The older paced
artifact remains immutable historical rejection evidence.
The older free-running 60-second benchmark is retained only as an unpaced
throughput diagnostic and is never selection-eligible. `select-rate` accepts
only the complete paced bundle schema with all three independent trials; a
generic list of rate/duration/p99/miss records is rejected.

The v27/v29 adapter resamples bridge evidence to 200 Hz, uses shortest-arc
quaternion SLERP, rotates both force and moment from TCP to base, and preserves
actual `step4e_cmd_* + cmd_valid` with zero-order hold. Missing commands fail
conversion rather than becoming zero. Claim-valid lineage is artifact-path-first:
the converter itself rehashes a sensor-calibration-lineage artifact, a wrench
frame-transform artifact, and a time-indexed task-ZFT artifact covering the full
trace. Bare SHA strings or a constant pose remain diagnostic-only. Artifact
identity proves lineage, not the metrological accuracy of a calibration. Both
retained traces lack an independent sensor-calibration-lineage artifact and an
explicit time-indexed task-ZFT artifact. Therefore
their 64-window four-policy runs are diagnostic only: each showed the actual
command bytes/validity bit-for-bit unchanged, while `claim_valid_proposals=0`.
A calibrated Jacobian is additionally required before using a converted trace
for a velocity-backend control claim, but is not invented as a prerequisite
for proposal-only shadow evidence.

Portable tracked hashes and scopes live in
`evidence/offline_evidence_index.json`; external files are resolved relative to
`UR10E_VIC_EVIDENCE_ROOT`. The compact index validates without the cache or raw
traces present, but that default validation proves only locator schema and
current validator-source bindings. It does not claim an external artifact
rehash or scientific/live claim validation. Supplying the external root enables
an explicit rehash; matching hashes still do not cross the separate claim-state
gates. Evidence schema v3 keeps historical entries immutable and uses separate
`current_selected` timing/trace pointers, so a validated future result can
supersede history without rewriting it or hard-coding a failed outcome.

## Validation

Run the dependency-light checks with:

```bash
bash check.sh
PYTHON_BIN=/path/to/torch-enabled-python bash check.sh
```

They cover quaternion sign/SLERP invariance, contracts and claim states, K/D
PSD/bounds/slew, two-period stale failover, capability-separated muxing, all
four VIC policies, fixed-orientation/translational-only phase-1 VIC, the full
offline simulator adapter and actual-command bit invariance, stable Step5b
binding, the 5.25.2 Direct Torque V2 packet-state oracle and template guards,
TacDiffusion SI/frame/causal/internal-wrench/filter/data/model contracts,
dataset/compact-evidence portability, upstream DBIL core, and independent
paced-rate selectors. They do not replace URSim or physical-bench validation.

## Sources

- Geiger et al., *Diffusion-Based Impedance Learning for Contact-Rich
  Manipulation Tasks*, [arXiv:2509.19696](https://arxiv.org/abs/2509.19696)
  (preprint).
- [Pinned upstream implementation](https://github.com/StrokeAIRobotics/DiffusionBasedImpedanceLearning)
  (MIT).
- Universal Robots [PolyScope 5.23 release notes](https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-523x/)
  and official manuals for [`direct_torque()`](https://www.universal-robots.com/manuals/EN/HTML/SW5_23/Content/prod-scriptmanual/all_scripts/direct_torque.htm),
  [`get_jacobian(q, tcp)`](https://www.universal-robots.com/manuals/EN/HTML/SW5_23/Content/prod-scriptmanual/all_scripts/get_jacobian.htm),
  and [`get_coriolis_and_centrifugal_torques(q, qd)`](https://www.universal-robots.com/manuals/EN/HTML/SW5_23/Content/prod-scriptmanual/all_scripts/get_coriolis_and_centrifugal_torques.htm).
- Wu et al., *TacDiffusion: Force-domain Diffusion Policy for Precise Tactile
  Manipulation*, [arXiv:2409.11047v2](https://arxiv.org/abs/2409.11047v2), and
  the pinned [official implementation](https://github.com/popnut123/TacDiffusion).
- Universal Robots [PolyScope 5.25 release notes](https://www.universal-robots.com/articles/ur/release-notes/release-note-software-version-525x/),
  [software update procedure](https://www.universal-robots.com/manuals/EN/HTML/SW5_25/Content/prod-serv-man/E-series/serv-man-update.htm), and the
  5.25 [`direct_torque()` manual](https://www.universal-robots.com/manuals/EN/HTML/SW5_25/Content/prod-scriptmanual/all_scripts/direct_torque.htm).
