# UR10e Variable Impedance Offline Preparation

This experiment is an **offline-only** VIC and diffusion-based impedance
learning scaffold. It is deliberately separate from
`experiments/tase-contact-reproduction/` and does not change the v29/v30
package pointer, controller, bridge, network, FT zero, or robot state.

Current status: `offline_scaffold`; `live_motion_authorized=false`;
`dbil_active_enabled=false`.

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

## Backend claim boundary

- `velocity_admittance_surrogate` cannot invent a Cartesian controller from K/D.
  It accepts only a fresh, sequence-matched command from the hash-bound pure
  Step5b contact core, preserves tangent/orientation, and applies only a
  dimensionless `sqrt(K_eff/K_baseline)<=1` reaction-normal modulation before
  DLS. The current Step5b ROS2 live-runner acceptance artifact is false, so the
  checked-in binding keeps this route command-disabled. It is always labelled
  `backend_fidelity=surrogate`; it is not true torque impedance.
- `direct_torque_vic_offline_template.script` is a hash-bound PolyScope 5.23+
  template. It contains the local 500 Hz formula
  `tau = J^T(K e - D xdot) + coriolis - joint_damping*qd`; gravity is excluded
  because `direct_torque()` compensates gravity internally. The template has no
  invocation, defaults to disabled, and has no upload/network path.
- The 5.23 template is not executable on the last recorded 5.11.9 controller.
  It remains blocked until a matching URSim validates syntax, one-tick runtime,
  heartbeat exit, and timing. An actual controller upgrade is outside this repo.

The RTDE manifest binds exactly 18 input doubles (equilibrium pose, K, D), plus
integer mode/sequence/heartbeat/exclusive lease. Sequence is the packet commit:
the controller reads it before and after the payload, requires both reads and
heartbeat to match, and requires exact `+1` advancement. Controller-side gates
also enforce K bounds/down-slew, `D=2*zeta*sqrt(K*M)`, translation-only VIC,
fixed orientation, equilibrium slew, new-run release/baseline reset, zero-wrench
startup ticks, force/torque/TCP-cage/joint guards, and finite bounded damping
exit torque. A frozen, mixed, gapped, or lease-switched packet fails closed.

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
  --frame-transform-sha256 FRAME_CONTRACT_SHA256
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
  --output /external/evidence/paced-200-100-50.json
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

The independently paced 200/100/50 Hz trials each ran for at least 60 seconds.
All three failed: p99 latency was 58.010/64.619/76.803 ms with
11,997/6,000/2,999 deadline misses respectively; all outputs remained finite.
No model rate is selected and DBIL remains shadow-only.
The older free-running 60-second benchmark is retained only as an unpaced
throughput diagnostic and is never selection-eligible. `select-rate` accepts
only the complete paced bundle schema with all three independent trials; a
generic list of rate/duration/p99/miss records is rejected.

The v27/v29 adapter resamples bridge evidence to 200 Hz, uses shortest-arc
quaternion SLERP, rotates both force and moment from TCP to base, and preserves
actual `step4e_cmd_* + cmd_valid` with zero-order hold. Missing commands fail
conversion rather than becoming zero. Both retained traces lack an independent
sensor-calibration hash, calibrated Jacobian, and explicit task ZFT. Therefore
their 64-window four-policy runs are diagnostic only: each showed the actual
command bytes/validity bit-for-bit unchanged, while `claim_valid_proposals=0`.

Portable tracked hashes and scopes live in
`evidence/offline_evidence_index.json`; external files are resolved relative to
`UR10E_VIC_EVIDENCE_ROOT`. The compact index validates without the cache or raw
traces present, but that default validation proves only locator schema and
current validator-source bindings. It does not claim an external artifact
rehash or scientific/live claim validation. Supplying the external root enables
an explicit rehash; matching hashes still do not cross the separate claim-state
gates.

## Validation

Run the dependency-light checks with:

```bash
bash check.sh
PYTHON_BIN=/path/to/torch-enabled-python bash check.sh
```

They cover quaternion sign/SLERP invariance, contracts and claim states, K/D
PSD/bounds/slew, two-period stale failover, capability-separated muxing, all
four policies, stable Step5b binding, 5.23 controller packet-state oracle and
template guards, dataset/compact-evidence portability, upstream core, and the
independently paced rate harness. They do not replace URSim or physical-bench
validation.

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
