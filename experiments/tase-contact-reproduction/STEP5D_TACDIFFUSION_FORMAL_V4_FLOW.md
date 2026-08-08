# Step5d TacDiffusion formal V4 flow

This is an isolated Remote Secondary Client Direct Torque route. It does not
load or execute the TP program currently selected for Autotune, and TP upload
or controller read-back is therefore inapplicable rather than missing. The
physical controller identity is instead rebound on every attempt by the
Secondary Client receiver source hash, protocol token, live lease, episode
identity, Remote state, Safety NORMAL, and the fresh RTDE echo.

Kunwei KWR75 raw TCP is the only force/contact/guard/training authority. UR
internal F/T fields and APIs are forbidden. The software baseline is exactly
1000 native frames; the sensor is never zeroed, tared, filtered, or configured.

The ordered stages are:

1. seven independent no-contact trajectory qualifications, each 8 s at 500 Hz
   and guarded at 6 N / 0.5 Nm;
2. fixed-K expert campaign, 200 eligible episodes, first 50 pilot episodes
   included, Kxyz 600 N/m and Krot 30 Nm/rad;
3. sealed 84D/12D dataset, fixed 6D model targets, checkpoint, exact 100/50 Hz
   50-step benchmark, then two 45 s model-inactive shadows;
4. variable-K campaign only after fixed-K completion and K/load qualification,
   again 200 eligible episodes with the first 50 included;
5. sealed 84D/12D dataset, variable 7D model targets, checkpoint, exact 100/50
   Hz benchmark, and two 45 s model-inactive shadows;
6. acceptance reports, cold-read closure, and a Git checkpoint.

Every contact episode uses the same deterministic acquisition primitive:
1000-frame software baseline; base -Z search at 0.5 mm/s up to 25 mm; 1 N load
latched for 50 consecutive native samples; bounded settle; 8 s trajectory;
then 10 mm retract along the reaction normal and a fresh TaskReadyHome. Normal
load is the environment-on-tool reaction projected on the named reaction
normal. On sensor, force/torque guard, protective-stop, Safety, joint, or route
identity faults, the runner exits Direct Torque and latches without automatic
motion. Only normal, ineligible, or explicitly recoverable terminal states may
perform the guarded retract/return.

Each physical attempt has an immutable directory and exactly one terminal
hash-chained ledger record. Failed attempts never consume an eligible ordinal.
The formal dataset builder accepts only 200 cold-readable V4 artifacts with
their recorder manifests and sole formal eligibility receipts.
