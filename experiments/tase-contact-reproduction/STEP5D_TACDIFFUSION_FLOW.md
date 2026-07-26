# Step5d TacDiffusion Direct Torque fixture-shadow flow

This route keeps the frozen Step5d v3 approach, contact search, preload, 12 N
outer loop, and 60 s cycloid. Stage25 uses the PolyScope 5.26 Direct Torque
backend with fixed K/D/Dq. TacDiffusion is a deterministic 50 Hz fixture
shadow only; it has no command or fallback authority and `model_active=false`.

This fixture-shadow route is not the expert-data collection route. Its TP
package requires zero feed-forward wrench, and the bridge does not persist the
84D observation plus 12D expert-action episode record required by the
TacDiffusion dataset. Starting this bridge alone therefore does not mean data
collection has started.

## Current state

- Local `.script/.txt/.urp` package generated and exact cachedContents checked.
- The exact triplet is uploaded to the controller and a fresh controller
  readback matches the local hashes.
- The unchanged KWR75B mounting, fresh read-only controller TCP offset, vendor
  drawing, and retained gravity-axis sweep now bind the sensor-to-active-TCP
  rotation and `90.6 mm` lever arm in a SHA-verified calibration artifact.
- An isolated official 5.25.2 URSim compatibility check parsed the PolyScope
  Script-node contents, entered Stage20 waiting for the bridge, and observed no
  joint or TCP motion. This is compatibility evidence, not 5.26 live acceptance.
- `step5d-tacdiffusion-shadow.sh status` is safe and read-only.
- Bridge `start` remains fail-closed until controller 5.26 proof, the applicable
  formal review gate, and a fresh tranche-specific authorization are all
  hash-bound. Package readback and sensor-frame calibration are verified.
- No bridge, load, Play, sensor stream command, URScript send to the physical
  controller, or robot motion has been performed by this implementation tranche.
  The only controller mutation was the scoped TP triplet upload plus fresh
  readback.

## Unknown-surface and controller-simulation boundaries

- The physical curved surface remains unknown to the controller. The live
  reference is nominal in-plane path plus target load; it must not inject the
  registered v11 CAD height or CAD local normals.
- The PolyScope Simulation button may run the exact 5.26 TP program without
  moving the robot. A hash-bound Simulation Mode trace can verify parser/API
  availability, RTDE register roundtrip, Stage25 entry, and controlled stop.
  This is controller-runtime no-motion evidence, not physical torque/contact
  acceptance.
- The complete `controller_verified` claim still separately needs controller
  and robot identity plus installation, safety, TCP/payload, URCap,
  calibration, and API evidence.

## Operator sequence after all gates pass

1. Open `/programs/andyl/kunwei/step5/step5d_tacdiffusion_direct_torque_fixture_shadow_v1.urp` on the TP. Do not press Play yet.
2. Run `step5d-tacdiffusion-shadow.sh start --live --allow-kunwei-stream-command --write-rtde-inputs --authorization <fresh-artifact.json>`.
3. The bridge collects exactly 1000 software baseline samples and waits. It does not use hardware tare or `zero_ftsensor`.
4. The user presses Play once. The bridge sends legacy precontact fields, then arms Direct Torque only after TP Stage25 reports WAITING while the program is PLAYING.
5. Only normal 60 s completion may use the frozen guarded retract/home route. Packet, sensor, runtime, or safety faults exit torque and latch without auto-home.

No-contact acceptance is 2 s, then 10 s, then 60 s with a fresh authorization
for each tranche and deterministic `0+0` validation. One final frozen-
fingerprint `2+1` review is deferred until immediately before expert-data
collection contact, which also requires fresh contact authorization.
