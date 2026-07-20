# Step5d TacDiffusion Direct Torque fixture-shadow flow

This route keeps the frozen Step5d v3 approach, contact search, preload, 12 N
outer loop, and 60 s cycloid. Stage25 uses the PolyScope 5.26 Direct Torque
backend with fixed K/D/Dq. TacDiffusion is a deterministic 50 Hz fixture
shadow only; it has no command or fallback authority and `model_active=false`.

## Current state

- Local `.script/.txt/.urp` package generated and exact cachedContents checked.
- The exact triplet is uploaded to the controller and a fresh controller
  readback matches the local hashes.
- An isolated official 5.25.2 URSim compatibility check parsed the PolyScope
  Script-node contents, entered Stage20 waiting for the bridge, and observed no
  joint or TCP motion. This is compatibility evidence, not 5.26 live acceptance.
- `step5d-tacdiffusion-shadow.sh status` is safe and read-only.
- Bridge `start` is fail-closed until package readback, controller 5.26 proof,
  a verified Kunwei sensor-to-TCP wrench transform, the applicable formal
  review gate, and a fresh tranche-specific authorization are all hash-bound.
- No bridge, load, Play, sensor stream command, URScript send to the physical
  controller, or robot motion has been performed by this implementation tranche.
  The only controller mutation was the scoped TP triplet upload plus fresh
  readback.

## Operator sequence after all gates pass

1. Open `/programs/andyl/kunwei/step5/step5d_tacdiffusion_direct_torque_fixture_shadow_v1.urp` on the TP. Do not press Play yet.
2. Run `step5d-tacdiffusion-shadow.sh start --live --allow-kunwei-stream-command --write-rtde-inputs --authorization <fresh-artifact.json>`.
3. The bridge collects exactly 1000 software baseline samples and waits. It does not use hardware tare or `zero_ftsensor`.
4. The user presses Play once. The bridge sends legacy precontact fields, then arms Direct Torque only after TP Stage25 reports WAITING while the program is PLAYING.
5. Only normal 60 s completion may use the frozen guarded retract/home route. Packet, sensor, runtime, or safety faults exit torque and latch without auto-home.

No-contact acceptance is 2 s, then 10 s, then 60 s with a fresh authorization
for each tranche. Contact uses the same progression only after its separate
2+1 review and fresh authorization.
