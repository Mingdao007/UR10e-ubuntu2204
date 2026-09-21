# Canonical Figure-eight Home — 2026-09-21

The August R013 Step6 Figure-eight contact-derived Home is now the canonical Home for the Figure-eight path, its autotuner, and future Figure-eight recovery:

`p[0.4620551816, 0.1778825964, 0.03408876139925415, 3.120752062, 0, 0.068626833]`

The robot was physically moved from the verified intermediate calibration Home to this pose with the historical orientation preserved. The final-descent receipt reports 45.8 µm position error and 0.053 mrad orientation error. A fresh RTDE stationarity sample after package delivery reports Safety `NORMAL`, Dashboard `STOPPED`, remote control `true`, 121 samples, maximum TCP speed `2.3e-8 m/s`, and maximum joint speed `2.1e-8 rad/s`.

The formal `step5d_contact_home_v1` package was regenerated with this target, uploaded to `/programs/andyl/kunwei/step5`, and fetched back with byte-equal script/TXT/URP hashes. It was then loaded and left stopped. Kunwei remained intentionally disconnected, so this is a Home and package identity result only; it is not force/contact or Figure-eight acceptance evidence.

The earlier new migration package was preserved as a failed attempt because Dashboard Play stopped immediately before motion. A known-live historical package was used for the intermediate move, followed by a verified final descent. The canonical formal package binds its final descent to `target_pose[2]`; it does not retain the old literal `0.033 m` descent floor.

See `receipt.json` for the complete evidence paths and hashes.
