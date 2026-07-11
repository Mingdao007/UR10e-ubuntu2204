# UR10e URSim Offline Preparation

This directory defines two immutable, offline-only URSim preparation lanes:

- PolyScope `5.11.9.1010452`: P0 v8 URScript, RTDE float-register contract,
  layout `524`, heartbeat fault, bounded `speedj`, and safe-exit preparation.
- PolyScope `5.23.0`: VIC `direct_torque` template/version parser, RTDE layout,
  coherent sequence/heartbeat oracle, zero-damping startup, and zero-damping
  safe-exit preparation.

The exact software versions are pinned, while both required image digests are
explicitly unresolved because the audited Ubuntu host has an inactive Docker
service and no URSim image. The v1 manifest schema therefore permits only
`status=blocked`, `availability=unavailable`, and false protocol claims. Static
source validation cannot be promoted into URSim execution evidence.

The builder performs local read-only hashing plus existing P0 package/runtime
and VIC backend-oracle validation. It has no container, network, controller, or
live mutation surface. A future executed lane needs a new evidence manifest
bound to a real image digest; this preparation manifest must not be edited into
a pass.

Run:

```bash
bash check.sh
python3 tools/build_ursim_preparation_manifest.py > /tmp/ursim-preparation.json
python3 tools/verify_ursim_preparation_manifest.py \
  --manifest /tmp/ursim-preparation.json
```

Expected result: both lanes verify as correctly prepared but remain
`blocked/unavailable`; `p0_ursim_protocol_pass=false` and
`direct_torque_ursim_software_pass=false`.
