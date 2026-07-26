# Handoff: OnRobot HEX-E Ready For Drift Bring-Up

From: Mac Codex / Mingdao onsite
To: Ubuntu agent on andy7
Status: ready-for-preflight, not yet approved for contact motion
Topic: OnRobot HEX-E mounted on UR10e; prepare ft sensor drift test

Changed:
- OnRobot HEX-E sensor has been mechanically mounted on the UR10e side.
- Sensor cable has been connected and routed along the robot using strap-style cable guidance.
- Onsite visual check suggests enough slack has been left near the wrist and along the arm.
- Current routing follows the UR cable-guidance idea: cable should be guided, not clamped rigidly.

Action:
1. Use this experiment root:
   `/home/andy/ur10e_ros2_ws/experiments/sensor-integration/archive/onrobot-hex-e-v2-3010007655/hex_e_v2_3010007655/`
2. Use existing prompt:
   `GOAL_RUN_ONROBOT_6H.md`
3. Before any long drift logging, perform a no-contact cable sweep:
   - do not touch the table or environment;
   - move only low speed / low range first;
   - check J4/J5/J6 wrist motion in both directions;
   - verify cable does not become taut;
   - verify HEX-E side connector is not pulled;
   - verify cable does not rub on blue covers, black joint seams, adapter edges, or sharp structure.
4. If cable sweep passes, proceed with read-only OnRobot drift bring-up.
5. Keep drift test read-only:
   - no zero / bias;
   - no autocalib;
   - no firmware update;
   - no DIP switch changes;
   - no contact-control test;
   - no TCP/gravity compensation changes during this drift test.

Acceptance:
- `ping 192.168.1.1` succeeds from Ubuntu wired interface.
- `/version` is readable.
- 2-minute dry run logs continuous force/torque data.
- `status=0`, `authenticated=true`, `bias=false`.
- Cable remains slack through the no-contact sweep.
