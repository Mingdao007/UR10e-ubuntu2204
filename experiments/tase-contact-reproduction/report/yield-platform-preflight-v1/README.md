# Original-platform preflight v1

Read-only observations on 2026-09-20 UTC; timestamps and raw receipts are retained in manifest.json. No bridge, Dashboard/RTDE/Kunwei endpoint connection, Load/Play, sensor zero, configuration write or motion was performed. The robot received ICMP ping only.

- Local Ethernet route and both robot/sensor subnet aliases match bench defaults. Robot ping succeeded twice. This does not establish sensor availability or robot safety state.
- No known bridge/controller command names were found in the bounded process scan. This is not a complete writer-lock admission or evidence of a stopped robot.
- The resolver accepts the retained package but still selects `step5d_strict_rnn_autotune_v3`. Its stored readiness does not qualify the new yield provider or give fresh robot identity.
- The existing realtime helper reports missing literal `priority 99` configuration lines. The current session does have the realtime group and RLIMIT_RTPRIO=99. No host configuration was changed to satisfy a template comparison.
- The bounded child-process probe found kernel-isolated CPUs 2 and 18, but neither is in that process's current allowed affinity. It stopped before attempting FIFO scheduling. Consequently FIFO permission was not tested and realtime admission remains incomplete. Root cgroup effective CPUs listing 0-31 is not proof that this process may use an isolated CPU; the session-specific cgroup path was unavailable in this filesystem view.

Per `ur10e/references/concurrency-gates.md`, endpoint preflight was not started because local realtime admission did not pass. No gate was relaxed. An appropriately bound native launcher must establish fresh eligible affinity and FIFO/20 before continuing the staged preflight; the current shell must not claim qualification from RLIMIT alone.

The offline study may continue. This snapshot is not a general physical pilot approval or a new request for user authorization. Current sensor/robot identity, safety state, command-writer ownership, observer barrier, the yield route's package binding and full-chain timing remain to be established.
