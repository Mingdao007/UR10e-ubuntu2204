# Step5d lightweight manual-hold route

This is an isolated derivative of frozen r009 (`bf6eb59d9530cf7f170f29c68c8812f00613b381`).
It does not modify the r009 source files, release manifest, package, or
`config/step5d/current.json`.

## Frozen execution envelope

- TP program: `step5d_strict_rnn_manual_tune_v3`
- protocol: `v3_full_home_manual_hold_v1`
- default request: P `0.001`, I `0.0001`, damping `7.0`
- target force: `12 N`; orientation `ko=0.4`
- execution profile: `nf100-slew050-a050` / integer ID `633`
- Stage25 success target: `60 s`
- each logical batch contains exactly one request and returns to campaign Home
- Home may wait without an elapsed-time limit while float-register 26 heartbeat
  remains fresh; heartbeat stale for more than `1.0 s` halts stationary with
  terminal reason `20`
- an inexact ARM identity halts stationary with terminal reason `21`
- TP output register `30` is written last as the identity commit word; the
  manual bridge tolerates only READY/ARMED partial publication for at most
  `250 ms`, and fails closed on RUN-before-commit, reconnect, timeout, sequence
  regression/overshoot, or a wrong committed identity

## Lightweight parameter loop

Parameter enqueue performs schema, range, normalization, immutable identity,
and atomic-file validation only.  It does not rebuild the TP package and does
not run the regression suite for each new P/I/damping value.

```bash
python3 tools/step5d_manual_queue.py enqueue \
  --queue <campaign>/control/manual_queue.json \
  --campaign-id <campaign-id> \
  --release-manifest-sha256 <manual-release-sha256> \
  --launch-profile config/step5/step5d_autotune_v3_launch_profile.json \
  --force-p 0.001 --force-i 0.0001 --force-damping 7

python3 tools/step5d_manual_queue.py status \
  --queue <campaign>/control/manual_queue.json

python3 tools/step5d_manual_queue.py close \
  --queue <campaign>/control/manual_queue.json \
  --reason operator_complete
```

Exact parameter repeats are allowed.  Each enqueue receives a distinct
occurrence and transport identity, while retaining the same control-candidate
identity.  The runtime persists one exact ARM intent before any transport call
and refuses to select another request until the observed `READY_HOME_NEXT`
identity closes the inflight request.

## Activation boundary

`run_step5d_manual_tp_transaction.py` is limited to package upload, fresh GET,
SHA closure, and switching `config/step5d/manual/current.json`.  Those actions
do not authorize Load, Play, a bridge, ARM registers, contact, or robot motion.
The offline `run_step5d_manual_hold_campaign.py` prepares durable identities.

The isolated live adapter is split into fresh context, read-only preflight,
and one process-lifetime NO_ARM bridge owner:

- `build_step5d_manual_bridge_start_context.py` binds the exact manual release,
  frozen launch profile, package hashes, source surface, and plant epoch;
- `preflight_step5d_manual_bridge.py` requires the exact manual TP program,
  normal safety, a stationary robot, start clearance, no writer, no mailbox,
  runtime dependencies, and read-only controller/sensor connectivity;
- `run_step5d_manual_bridge_live.py` holds both live-writer and throughput locks
  until shutdown, then starts only `run_step5d_manual_bridge.py`;
- the bridge declares manual protocol `v3_full_home_manual_hold_v1` while
  explicitly reusing the frozen r009 register wire protocol.  This mapping does
  not authorize ARM or motion and cannot silently select the r009 TP program.

Bridge start, TP Play, ARM, and live/contact remain separate explicit gates.
After an explicit motion authorization, `run_step5d_manual_live_campaign.py`
binds the already-fsynced one-row intent to the exact running bridge, requires
a newer command/trial identity than the observed READY_HOME snapshot, and
atomically stages the ARM mailbox before the operator presses Play.
