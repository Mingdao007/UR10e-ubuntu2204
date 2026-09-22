# Rate400 PATH publish diagnostic (2026-09-23)

The `r013_60_rate400` protocol leaves the nominal 500 Hz sender and all runtime safety limits unchanged. Its 400 Hz threshold is a separate mean-rate data-admission criterion. A completed 60 s PATH and verified joint Home do not convert a below-threshold attempt into a successful observation.

| Attempt | Host PATH publishes / 60 s | TP distinct echoes / 60 s | One-second host windows below 400 | First such window | Min one-second host count | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Tuner `bo-08` | 25,146 / 419.10 Hz | 22,523 / 375.38 Hz | 26 | PATH second 6 | 329 | Timing failure; Home verified |
| Tuner `repeat-02` | 26,287 / 438.12 Hz | 23,802 / 396.70 Hz | 16 | PATH second 0 | 328 | Timing failure; Home verified |
| Confirmation first B | 25,908 / 431.80 Hz | 23,455 / 390.92 Hz | 19 | PATH second 0 | 336 | Timing failure; full pair rerun |
| Confirmation valid B, block 0 | 29,394 / 489.90 Hz | See its receipt | 2 | PATH second 20 | 350 | Complete, eligible |

The host counts come from `attempt-result.json.host_path_publishes`, which counts **successful PATH sends**. The TP counts come from `evidence.metrics.timing_evidence.distinct_tp_consumed_packet_echoes`. The older `writer_publishes` timing field has the scope of writer sequences represented in accepted RTDE/TP samples; it is not the total successful host PATH send count. The one-second bins above use the last `host_path_publishes` packets with `command_mode=2` in each sealed `published_packets.jsonl`, anchored at the first of those packets. The healthy comparison row is a different candidate and is not a matched controlled intervention.

Sources: tuner `session-01/attempts/0016` and `session-02/attempts/0006`; confirmation `confirmation-rate400-b-v1/session-01/attempts/0002` and `session-02/attempts/0002`, all under `runs/tase-resident-tune-rate400-b-20260923-01/`. The original files, seal hashes, and failure classifications remain unchanged.

The first bottleneck is at least partly on the host side: it sent materially fewer than 30,000 PATH commands in each failed attempt. A mean host rate above 400 does not make the TP mean pass. The slowdown begins at different PATH times, so these traces do not identify a single stage transition. Packet-to-packet final joint-velocity changes did not establish a causal link to the slow windows. Existing data have no per-cycle call stack or complete scheduler trace for these failed cycles. The specific cause remains unknown; neither network, integral saturation, SHA-256 work, nor force-preempt should be labeled as proved cause.

Before another same-cause live repair attempt, capture low-overhead per-cycle elapsed time around sensor receive, outer loop, solver, packet construction, send, and observation recording, plus a timestamped scheduler/runqueue trace. Align the first delayed send with RTDE/TP consumed sequence and force-preempt/limit events. Preserve the 400 Hz protocol identity and all existing stop, velocity, slew, wrench, and Home protections.
