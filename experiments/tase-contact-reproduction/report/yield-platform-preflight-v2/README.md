# Platform preflight v2: correct route scope, then fresh Dashboard observation

This supersedes the interpretation of isolation in v1; v1 raw observations remain intact.

The realsetup owner reference has separate headings. Its general “Formal 500 Hz realtime lane” selects a currently allowed CPU and verifies FIFO/20, without requiring the kernel-isolated set. The intersection-with-isolated-set requirement occurs under “Five-law scripted-force campaign route”, specifically sfc_scripted_force_campaign_v1. Applying that narrower condition to this TASE task was an overbroad interpretation. No instruction or runtime gate was weakened to repair this mistake.

A bounded child process selected CPU 0 from its fresh allowed affinity, successfully entered SCHED_FIFO/20, then restored both its previous scheduler and full affinity. CPU 0 is an observed selection for this permission diagnostic, not a future hard-coded CPU, low-load measurement or formal 2 ms timing qualification. The legacy text-template failure does not establish lack of effective FIFO permission.

Fresh robot-side observations (2026-09-20, exact receipt timestamps in manifest):

- Robot direct Ethernet route and ping pass. No existing TCP connection to robot or sensor was listed immediately before Dashboard acquisition; this local observation cannot exclude robot-local programs or another host.
- Seven read-only Dashboard queries succeed: Remote true; Safety NORMAL; Robotmode RUNNING; Program running true; PLAYING step5d_contact_six_qp_v1.urp; loaded path /programs/andyl/kunwei/step5/step5d_contact_six_qp_v1.urp; PolyScope 5.26.0.140462.
- The task did not start this resident program. PLAYING does not prove motion, a safe static/no-contact state, current command-writer ownership or active yield-provider identity.

A concise user question requests the resident program's ownership and current static/contact state. Pending that information, no Load/Play, Stop, bridge, sensor zero, command write, RTDE input or motion is performed. Existing live intent is retained; the missing item is current physical/ownership evidence. Offline native-writer composition proceeds independently.

Remaining: route/profile and package binding for the actual selected candidate, single writer/observer ownership, fresh calibrated robot/sensor state, physical clearance/contact conditions, full-chain timing and transport qualification. No new pilot acceptance follows from this snapshot.
