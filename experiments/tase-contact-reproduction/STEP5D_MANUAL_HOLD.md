# Step5d lightweight manual-hold route

This is an isolated derivative of frozen r009 (`bf6eb59d9530cf7f170f29c68c8812f00613b381`).
It does not modify the r009 source files, release manifest, package, or
`config/step5d/current.json`.

## Frozen execution envelope

- TP program: `step5d_strict_rnn_manual_tune_v1`
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
The offline `run_step5d_manual_hold_campaign.py` prepares durable identities
only and deliberately contains no controller transport.  Any later live
adapter and live execution require a separate explicit operator trigger and
the normal UR10e live/contact gates.
