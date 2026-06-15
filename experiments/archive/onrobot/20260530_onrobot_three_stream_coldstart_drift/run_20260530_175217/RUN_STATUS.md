# UR10e / OnRobot 24h Three-Stream Capture Status

Started: 2026-05-30T17:52:17+08:00

Run directory:

`/home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217`

tmux session:

`ur10e_24h_three_stream_20260530`

Launcher command:

```bash
cd /home/andy/ur10e_ros2_ws
CAP_SECONDS=86400 CHECKPOINT_INTERVAL_S=300 PREFIX=three_stream_24h_20260530 \
  bash /home/andy/ur10e_ros2_ws/scripts/run_onrobot_three_stream_coldstart_drift.sh
```

Live capture process observed after start:

```text
python3 /home/andy/ur10e_ros2_ws/scripts/capture_onrobot_ur_three_stream_interruptible.py \
  --seconds 86400 \
  --checkpoint-interval-s 300 \
  --prefix three_stream_24h_20260530 \
  --output-dir /home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217 \
  --confirm INTERRUPTIBLE_THREE_STREAM_SPEED2
```

Streams:

- UR `actual_TCP_force` RTDE at requested 500 Hz.
- OnRobot URCap variables via `output_double_register_24..29`, tuple updates around 125 Hz.
- OnRobot Compute Box UDP high-speed raw stream with `SPEED=2`, around 500 Hz.

Safety boundary:

- No UR motion command.
- No UR `zero_ftsensor()`.
- No OnRobot `BIAS` or `FILTER`.
- No TCP or payload write.
- UDP commands are `SPEED=2`, `START`, and final `STOP` on clean interruption/finish.

Stop cleanly:

```bash
tmux send-keys -t ur10e_24h_three_stream_20260530 C-c
```

Then wait for the script to print/write its summary. The Python logger catches Ctrl+C and sends the final UDP `STOP`.

Status checks:

```bash
tmux capture-pane -t ur10e_24h_three_stream_20260530 -p -S - | tail -80
wc -l /home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217/*.csv
ls -lh /home/andy/ur10e_ros2_ws/experiments/archive/onrobot/20260530_onrobot_three_stream_coldstart_drift/run_20260530_175217
```

## Checkpoints

- `2026-05-30T17:57:35+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:16`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`.
- First checkpoint file:
  `three_stream_24h_20260530_20260530_175220_checkpoint.json`.
  It reports `ok_so_far: true`, `errors: []`, `elapsed_s: 300.499933404`,
  `rtde_rows: 150247`, `udp_rows: 150128`, requested duration `86400.0 s`.
- Files at that巡检: UDP CSV `156325` lines, RTDE/URCap CSV `156475` lines,
  run directory about `67M`.
- `2026-05-30T18:03:59+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `11:40`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`.
- Second checkpoint update at `2026-05-30T18:02:23+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 600.98354292`,
  `rtde_rows: 300490`, `udp_rows: 300252`, requested duration `86400.0 s`.
- Files at the `18:03:59`巡检: UDP CSV `348163` lines, RTDE/URCap CSV
  `348457` lines, run directory about `150M`.
- `2026-05-30T18:08:34+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `16:15`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`.
- Third checkpoint update at `2026-05-30T18:07:23+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 901.479631704`,
  `rtde_rows: 450737`, `udp_rows: 450380`, requested duration `86400.0 s`.
- Files at the `18:08:34`巡检: UDP CSV `485919` lines, RTDE/URCap CSV
  `486310` lines, run directory about `209M`.
- `2026-05-30T18:13:32+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `21:13`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`.
- Fourth checkpoint update at `2026-05-30T18:12:24+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 1201.98214018`,
  `rtde_rows: 600987`, `udp_rows: 600512`, requested duration `86400.0 s`.
- Files at the `18:13:32`巡检: UDP CSV `634649` lines, RTDE/URCap CSV
  `635168` lines, run directory about `272M`.
- `2026-05-30T18:18:33+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `26:14`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`.
- Fifth checkpoint update at `2026-05-30T18:17:24+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 1502.48105683`,
  `rtde_rows: 751236`, `udp_rows: 750642`, requested duration `86400.0 s`.
- Files at the `18:18:33`巡检: UDP CSV `784811` lines, RTDE/URCap CSV
  `785455` lines, run directory about `337M`. Disk free at `18:14:00` was
  about `533G` on `/home/andy/ur10e_ros2_ws`.
- `2026-05-30T18:19:01+08:00` Dashboard changed to `Program running: false`,
  `programState: STOPPED 3.urp`, while the capture PID and CSV writes were
  still active. This means the logger process was still alive, but the
  PolyScope no-motion export program was no longer running.
- `2026-05-30T18:20:02+08:00` URCap register freeze check: direct RTDE read of
  `output_double_register_24..29` returned `157` samples with only `1`
  distinct tuple:
  `(0.112793, 0.126211, 0.44092, -0.003582, -0.003361, -0.001023)`.
  The RTDE/URCap CSV tail had `2000` rows over `t_s 1629.415..1633.413` with
  only `1` distinct URCap tuple. The CSV-level last URCap tuple change was at
  sample `791305`, `t_s 1582.621273352`; at sample `830999`, `t_s
  1662.009622041`, the tuple had been frozen for about `79.388 s`.
- At the same `18:20:02` check, tmux session and PID `7754` were still alive,
  and the CSVs still grew over 10 s: UDP lines `829325 -> 834325`, RTDE/URCap
  lines `830001 -> 835024`. Interpretation: UR RTDE and OnRobot UDP capture
  continued, but the URCap register stream was stale/frozen after the program
  stopped.
- Recovery evidence: by `2026-05-30T18:21:53+08:00`, Dashboard was back to
  `Program running: true`, `programState: PLAYING 3.urp`; direct RTDE read of
  `output_double_register_24..29` returned `157` samples with `157` distinct
  tuples. A full CSV scan found the stale URCap tuple run from sample `791305`,
  `t_s 1582.621273352`, through sample `859752`, `t_s 1719.516048058`,
  duration about `136.894774706 s`. After that, the URCap tuple resumed
  changing.
- Sixth checkpoint update at `2026-05-30T18:22:25+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 1802.929991454`,
  `rtde_rows: 901459`, `udp_rows: 900747`, requested duration `86400.0 s`.
  Note: the built-in checkpoint did not flag the URCap stale-tuple interval;
  rely on the manual stale-run evidence above when analyzing this run.
- `2026-05-30T18:24:04+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `31:45`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV `950109`
  lines, RTDE/URCap CSV `950902` lines, run directory about `406M`.
- User explanation at this stage: the brief stop/stale interval was caused by
  renaming `3.urp`; the user restarted it from the pendant and asked Codex to
  continue monitoring. Codex did not start the UR program from Ubuntu.
- `2026-05-30T18:24:42+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `32:23`; run directory about `414M`; disk free still about `533G`.
  Dashboard check immediately after showed `Safetymode: NORMAL`, `Program
  running: true`, `programState: PLAYING 3.urp`; direct RTDE URCap register
  check returned `157` samples with `157` distinct tuples.
- Seventh checkpoint update at `2026-05-30T18:27:25+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 2103.388018603`,
  `rtde_rows: 1051687`, `udp_rows: 1050856`, requested duration `86400.0 s`.
  Post-checkpoint CSV growth was confirmed at `2026-05-30T18:28:09+08:00`
  with UDP CSV `1072955` lines and RTDE/URCap CSV `1073803` lines.
- `2026-05-30T18:28:54+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `36:35`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `1095054` lines, RTDE/URCap CSV `1095949` lines, run directory about `465M`.
- `2026-05-30T18:29:30+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `37:11`; run directory about `472M`; disk free still about `533G`.
  Dashboard check immediately after showed `Safetymode: NORMAL`, `Program
  running: true`, `programState: PLAYING 3.urp`; direct RTDE URCap register
  check returned `157` samples with `157` distinct tuples.
- Eighth checkpoint update at `2026-05-30T18:32:26+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 2403.88130777`,
  `rtde_rows: 1201933`, `udp_rows: 1200983`, requested duration `86400.0 s`.
  Post-checkpoint CSV growth was confirmed at `2026-05-30T18:33:37+08:00`
  with UDP CSV `1236478 -> 1241531` lines and RTDE/URCap CSV
  `1237487 -> 1242507` lines over 10 s.
- `2026-05-30T18:33:37+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `41:18`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples.
- `2026-05-30T18:34:20+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `42:01`; run directory about `531M`; disk free still about `533G`.
  Dashboard check immediately after showed `Safetymode: NORMAL`, `Program
  running: true`, `programState: PLAYING 3.urp`; direct RTDE URCap register
  check returned `157` samples with `157` distinct tuples. CSV growth over
  10 s was confirmed: UDP CSV `1263743 -> 1268743` lines and RTDE/URCap CSV
  `1264749 -> 1269767` lines.
- Ninth checkpoint update at `2026-05-30T18:37:26+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 2704.30560704`,
  `rtde_rows: 1352144`, `udp_rows: 1351075`, requested duration `86400.0 s`.
- `2026-05-30T18:38:34+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `46:15`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `1385107` lines, RTDE/URCap CSV `1386239` lines, run directory about `582M`.
- `2026-05-30T18:39:23+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `47:04`; run directory about `592M`; disk free still about `533G`.
  Dashboard check immediately after showed `Safetymode: NORMAL`, `Program
  running: true`, `programState: PLAYING 3.urp`; direct RTDE URCap register
  check returned `157` samples with `157` distinct tuples. CSV growth over
  10 s was confirmed: UDP CSV `1416054 -> 1421054` lines and RTDE/URCap CSV
  `1417189 -> 1422217` lines.
- Tenth checkpoint update at `2026-05-30T18:42:27+08:00`: same checkpoint file
  reports `ok_so_far: true`, `errors: []`, `elapsed_s: 3004.729758275`,
  `rtde_rows: 1502355`, `udp_rows: 1501167`, requested duration `86400.0 s`.
- `2026-05-30T18:43:36+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `51:17`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `1535849` lines, RTDE/URCap CSV `1537094` lines, run directory about `644M`.
- Eleventh checkpoint update at `2026-05-30T18:47:27+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 3305.165929583`,
  `rtde_rows: 1652571`, `udp_rows: 1651265`, requested duration `86400.0 s`.
- `2026-05-30T18:48:02+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `55:42`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `1668535` lines, RTDE/URCap CSV `1669864` lines, run directory about `698M`;
  disk free still about `533G` on `/home/andy/ur10e_ros2_ws`.
- Twelfth checkpoint update at `2026-05-30T18:52:27+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 3605.631409759`,
  `rtde_rows: 1802803`, `udp_rows: 1801378`, requested duration `86400.0 s`.
- `2026-05-30T18:52:52+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:00:33`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `1813849` lines, RTDE/URCap CSV `1815339` lines, run directory about `757M`;
  disk free still about `533G` on `/home/andy/ur10e_ros2_ws`.
- Thirteenth checkpoint update at `2026-05-30T18:57:28+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 3906.098951698`,
  `rtde_rows: 1953035`, `udp_rows: 1951491`, requested duration `86400.0 s`.
- `2026-05-30T18:58:13+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:05:54`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `1974001` lines, RTDE/URCap CSV `1975599` lines, run directory about `822M`;
  disk free still about `533G` on `/home/andy/ur10e_ros2_ws`.
- Fourteenth checkpoint update at `2026-05-30T19:02:28+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 4206.548394714`,
  `rtde_rows: 2103259`, `udp_rows: 2101596`, requested duration `86400.0 s`.
- `2026-05-30T19:02:52+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:10:33`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `2113319` lines, RTDE/URCap CSV `2115001` lines, run directory about `879M`;
  disk free still about `533G` on `/home/andy/ur10e_ros2_ws`.
- Fifteenth checkpoint update at `2026-05-30T19:07:29+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 4506.977365745`,
  `rtde_rows: 2253472`, `udp_rows: 2251690`, requested duration `86400.0 s`.
- `2026-05-30T19:08:07+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:15:48`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `2270743` lines, RTDE/URCap CSV `2272598` lines, run directory about `943M`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Sixteenth checkpoint update at `2026-05-30T19:12:29+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 4807.41378446`,
  `rtde_rows: 2403689`, `udp_rows: 2401789`, requested duration `86400.0 s`.
- `2026-05-30T19:12:54+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:20:35`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `2414160` lines, RTDE/URCap CSV `2416094` lines, run directory about
  `1002M`; disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Seventeenth checkpoint update at `2026-05-30T19:17:30+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  5107.881877753`, `rtde_rows: 2553922`, `udp_rows: 2551903`, requested
  duration `86400.0 s`.
- `2026-05-30T19:18:13+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:25:54`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `2573531` lines, RTDE/URCap CSV `2575606` lines, run directory about `1.1G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Eighteenth checkpoint update at `2026-05-30T19:22:30+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  5408.336443427`, `rtde_rows: 2704149`, `udp_rows: 2702011`, requested
  duration `86400.0 s`.
- `2026-05-30T19:23:43+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:31:24`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `2738478` lines, RTDE/URCap CSV `2740680` lines, run directory about `1.2G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Nineteenth checkpoint update at `2026-05-30T19:27:31+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  5708.801062553`, `rtde_rows: 2854385`, `udp_rows: 2852128`, requested
  duration `86400.0 s`.
- `2026-05-30T19:27:53+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:35:34`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `2863213` lines, RTDE/URCap CSV `2865530` lines, run directory about `1.2G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twentieth checkpoint update at `2026-05-30T19:32:31+08:00`: same checkpoint
  file reports `ok_so_far: true`, `errors: []`, `elapsed_s: 6009.269275794`,
  `rtde_rows: 3004621`, `udp_rows: 3002245`, requested duration `86400.0 s`.
- `2026-05-30T19:33:12+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:40:53`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `3022902` lines, RTDE/URCap CSV `3025337` lines, run directory about `1.3G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-first checkpoint update at `2026-05-30T19:37:32+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  6309.745475518`, `rtde_rows: 3154860`, `udp_rows: 3152365`, requested
  duration `86400.0 s`.
- `2026-05-30T19:37:55+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:45:36`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `3164107` lines, RTDE/URCap CSV `3166688` lines, run directory about `1.3G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-second checkpoint update at `2026-05-30T19:42:32+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  6610.206500795`, `rtde_rows: 3305090`, `udp_rows: 3302477`, requested
  duration `86400.0 s`.
- `2026-05-30T19:42:48+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:50:29`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `3310584` lines, RTDE/URCap CSV `3313278` lines, run directory about `1.4G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-third checkpoint update at `2026-05-30T19:47:33+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  6910.675693957`, `rtde_rows: 3455325`, `udp_rows: 3452592`, requested
  duration `86400.0 s`.
- `2026-05-30T19:47:57+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `01:55:38`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `3464743` lines, RTDE/URCap CSV `3467530` lines, run directory about `1.5G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-fourth checkpoint update at `2026-05-30T19:52:33+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  7211.138810813`, `rtde_rows: 3605556`, `udp_rows: 3602705`, requested
  duration `86400.0 s`.
- `2026-05-30T19:53:05+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:00:46`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `3618531` lines, RTDE/URCap CSV `3621458` lines, run directory about `1.5G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-fifth checkpoint update at `2026-05-30T19:57:33+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  7511.604547278`, `rtde_rows: 3755789`, `udp_rows: 3752818`, requested
  duration `86400.0 s`.
- `2026-05-30T19:58:18+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:05:59`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `3775160` lines, RTDE/URCap CSV `3778218` lines, run directory about `1.6G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-sixth checkpoint update at `2026-05-30T20:02:34+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  7812.068106017`, `rtde_rows: 3906020`, `udp_rows: 3902931`, requested
  duration `86400.0 s`.
- `2026-05-30T20:03:14+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:10:55`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `3922849` lines, RTDE/URCap CSV `3926051` lines, run directory about `1.6G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-seventh checkpoint update at `2026-05-30T20:07:34+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  8112.55150753`, `rtde_rows: 4056262`, `udp_rows: 4053053`, requested
  duration `86400.0 s`.
- `2026-05-30T20:08:07+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:15:48`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `4069213` lines, RTDE/URCap CSV `4072476` lines, run directory about `1.7G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-eighth checkpoint update at `2026-05-30T20:12:35+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  8413.031637154`, `rtde_rows: 4206501`, `udp_rows: 4203174`, requested
  duration `86400.0 s`.
- `2026-05-30T20:12:53+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:20:34`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `4212425` lines, RTDE/URCap CSV `4215830` lines, run directory about `1.8G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Twenty-ninth checkpoint update at `2026-05-30T20:17:35+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  8713.503152069`, `rtde_rows: 4356737`, `udp_rows: 4353291`, requested
  duration `86400.0 s`.
- `2026-05-30T20:18:08+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:25:49`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `4369690` lines, RTDE/URCap CSV `4373232` lines, run directory about `1.8G`;
  disk free about `532G` on `/home/andy/ur10e_ros2_ws`.
- Thirtieth checkpoint update at `2026-05-30T20:22:36+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  9013.968554699`, `rtde_rows: 4506969`, `udp_rows: 4503404`, requested
  duration `86400.0 s`.
- `2026-05-30T20:22:54+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:30:35`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `4512637` lines, RTDE/URCap CSV `4516270` lines, run directory about `1.9G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-first checkpoint update at `2026-05-30T20:27:36+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  9314.45314035`, `rtde_rows: 4657211`, `udp_rows: 4653527`, requested
  duration `86400.0 s`.
- `2026-05-30T20:28:10+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:35:51`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `4670584` lines, RTDE/URCap CSV `4674331` lines, run directory about `1.9G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-second checkpoint update at `2026-05-30T20:32:37+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  9614.937393254`, `rtde_rows: 4807452`, `udp_rows: 4803650`, requested
  duration `86400.0 s`.
- `2026-05-30T20:33:02+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:40:43`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `4816160` lines, RTDE/URCap CSV `4820032` lines, run directory about `2.0G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-third checkpoint update at `2026-05-30T20:37:37+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  9915.37955211`, `rtde_rows: 4957673`, `udp_rows: 4953751`, requested
  duration `86400.0 s`.
- `2026-05-30T20:38:00+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:45:41`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `4965107` lines, RTDE/URCap CSV `4969095` lines, run directory about `2.1G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-fourth checkpoint update at `2026-05-30T20:42:38+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  10215.813487614`, `rtde_rows: 5107889`, `udp_rows: 5103848`, requested
  duration `86400.0 s`.
- `2026-05-30T20:43:24+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:51:05`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `5127266` lines, RTDE/URCap CSV `5131434` lines, run directory about `2.1G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-fifth checkpoint update at `2026-05-30T20:47:38+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  10516.251684508`, `rtde_rows: 5258108`, `udp_rows: 5253949`, requested
  duration `86400.0 s`.
- `2026-05-30T20:47:58+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `02:55:39`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `5264001` lines, RTDE/URCap CSV `5268241` lines, run directory about `2.2G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-sixth checkpoint update at `2026-05-30T20:52:38+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  10816.380192524`, `rtde_rows: 5408172`, `udp_rows: 5403894`, requested
  duration `86400.0 s`.
- `2026-05-30T20:53:08+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:00:49`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `5418743` lines, RTDE/URCap CSV `5423093` lines, run directory about `2.2G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-seventh checkpoint update at `2026-05-30T20:57:38+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  11116.500915791`, `rtde_rows: 5558232`, `udp_rows: 5553835`, requested
  duration `86400.0 s`.
- `2026-05-30T20:58:21+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:06:02`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `5575319` lines, RTDE/URCap CSV `5579811` lines, run directory about `2.3G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-eighth checkpoint update at `2026-05-30T21:02:38+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  11416.615911727`, `rtde_rows: 5708289`, `udp_rows: 5703773`, requested
  duration `86400.0 s`.
- `2026-05-30T21:03:01+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:10:42`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `5715266` lines, RTDE/URCap CSV `5719855` lines, run directory about `2.4G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Thirty-ninth checkpoint update at `2026-05-30T21:07:39+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  11716.713293112`, `rtde_rows: 5858337`, `udp_rows: 5853703`, requested
  duration `86400.0 s`.
- `2026-05-30T21:08:15+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:15:56`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `5872955` lines, RTDE/URCap CSV `5877668` lines, run directory about `2.4G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Fortieth checkpoint update at `2026-05-30T21:12:39+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  12016.795435709`, `rtde_rows: 6008378`, `udp_rows: 6003624`, requested
  duration `86400.0 s`.
- `2026-05-30T21:13:07+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:20:48`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `6017955` lines, RTDE/URCap CSV `6022799` lines, run directory about `2.5G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Forty-first checkpoint update at `2026-05-30T21:17:39+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  12317.282595605`, `rtde_rows: 6158621`, `udp_rows: 6153749`, requested
  duration `86400.0 s`.
- `2026-05-30T21:18:15+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:25:55`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `6171478` lines, RTDE/URCap CSV `6176468` lines, run directory about `2.5G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Forty-second checkpoint update at `2026-05-30T21:22:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  12617.702847561`, `rtde_rows: 6308830`, `udp_rows: 6303839`, requested
  duration `86400.0 s`.
- `2026-05-30T21:22:59+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:30:40`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `6313425` lines, RTDE/URCap CSV `6318520` lines, run directory about `2.6G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Forty-third checkpoint update at `2026-05-30T21:27:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  12917.772276896`, `rtde_rows: 6458865`, `udp_rows: 6453755`, requested
  duration `86400.0 s`.
- `2026-05-30T21:28:12+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:35:53`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `6469849` lines, RTDE/URCap CSV `6475063` lines, run directory about `2.7G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Forty-fourth checkpoint update at `2026-05-30T21:32:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  13217.850616933`, `rtde_rows: 6608903`, `udp_rows: 6603675`, requested
  duration `86400.0 s`.
- `2026-05-30T21:33:23+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:41:04`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `6625584` lines, RTDE/URCap CSV `6630920` lines, run directory about `2.7G`;
  disk free about `531G` on `/home/andy/ur10e_ros2_ws`.
- Forty-fifth checkpoint update at `2026-05-30T21:37:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  13517.944140786`, `rtde_rows: 6758949`, `udp_rows: 6753602`, requested
  duration `86400.0 s`.
- `2026-05-30T21:38:15+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:45:56`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `6771478` lines, RTDE/URCap CSV `6776956` lines, run directory about `2.8G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Forty-sixth checkpoint update at `2026-05-30T21:42:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  13818.049616628`, `rtde_rows: 6909002`, `udp_rows: 6903536`, requested
  duration `86400.0 s`.
- `2026-05-30T21:43:14+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:50:55`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `6920637` lines, RTDE/URCap CSV `6926227` lines, run directory about `2.8G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Forty-seventh checkpoint update at `2026-05-30T21:47:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  14118.141433121`, `rtde_rows: 7059048`, `udp_rows: 7053464`, requested
  duration `86400.0 s`.
- `2026-05-30T21:48:04+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `03:55:45`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `7065478` lines, RTDE/URCap CSV `7071196` lines, run directory about `2.9G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Forty-eighth checkpoint update at `2026-05-30T21:52:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  14418.24114264`, `rtde_rows: 7209098`, `udp_rows: 7203394`, requested
  duration `86400.0 s`.
- `2026-05-30T21:53:22+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:01:03`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `7224531` lines, RTDE/URCap CSV `7230364` lines, run directory about `3.0G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Forty-ninth checkpoint update at `2026-05-30T21:57:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  14718.335792013`, `rtde_rows: 7359145`, `udp_rows: 7353323`, requested
  duration `86400.0 s`.
- `2026-05-30T21:58:13+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:05:55`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `7370054` lines, RTDE/URCap CSV `7376031` lines, run directory about `3.0G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fiftieth checkpoint update at `2026-05-30T22:02:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  15018.423853862`, `rtde_rows: 7509189`, `udp_rows: 7503248`, requested
  duration `86400.0 s`.
- `2026-05-30T22:02:57+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:10:38`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `7511478` lines, RTDE/URCap CSV `7517556` lines, run directory about `3.1G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-first checkpoint update at `2026-05-30T22:07:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  15318.520027548`, `rtde_rows: 7659237`, `udp_rows: 7653177`, requested
  duration `86400.0 s`.
- `2026-05-30T22:08:22+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:16:03`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `7673902` lines, RTDE/URCap CSV `7680063` lines, run directory about `3.1G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-second checkpoint update at `2026-05-30T22:12:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  15618.585554106`, `rtde_rows: 7809269`, `udp_rows: 7803090`, requested
  duration `86400.0 s`.
- `2026-05-30T22:13:04+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:20:44`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `7814690` lines, RTDE/URCap CSV `7821001` lines, run directory about `3.2G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-third checkpoint update at `2026-05-30T22:17:40+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  15918.662737768`, `rtde_rows: 7959305`, `udp_rows: 7953007`, requested
  duration `86400.0 s`.
- `2026-05-30T22:18:18+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:26:00`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `7972107` lines, RTDE/URCap CSV `7978507` lines, run directory about `3.3G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-fourth checkpoint update at `2026-05-30T22:22:41+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  16218.711637495`, `rtde_rows: 8109328`, `udp_rows: 8102911`, requested
  duration `86400.0 s`.
- `2026-05-30T22:23:06+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:30:47`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `8115584` lines, RTDE/URCap CSV `8122107` lines, run directory about `3.3G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-fifth checkpoint update at `2026-05-30T22:27:41+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  16518.795493526`, `rtde_rows: 8259368`, `udp_rows: 8252833`, requested
  duration `86400.0 s`.
- `2026-05-30T22:28:22+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:36:03`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `8273637` lines, RTDE/URCap CSV `8280311` lines, run directory about `3.4G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-sixth checkpoint update at `2026-05-30T22:32:41+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  16819.276099355`, `rtde_rows: 8409607`, `udp_rows: 8402953`, requested
  duration `86400.0 s`.
- `2026-05-30T22:33:16+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:40:57`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `8420425` lines, RTDE/URCap CSV `8427218` lines, run directory about `3.4G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-seventh checkpoint update at `2026-05-30T22:37:42+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  17119.699017378`, `rtde_rows: 8559818`, `udp_rows: 8553045`, requested
  duration `86400.0 s`.
- `2026-05-30T22:38:06+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:45:47`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `8565531` lines, RTDE/URCap CSV `8572449` lines, run directory about `3.5G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-eighth checkpoint update at `2026-05-30T22:42:42+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  17420.113685834`, `rtde_rows: 8710024`, `udp_rows: 8703133`, requested
  duration `86400.0 s`.
- `2026-05-30T22:43:11+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:50:52`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `8717796` lines, RTDE/URCap CSV `8724826` lines, run directory about `3.6G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Fifty-ninth checkpoint update at `2026-05-30T22:47:42+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  17720.536523695`, `rtde_rows: 8860236`, `udp_rows: 8853225`, requested
  duration `86400.0 s`.
- `2026-05-30T22:48:24+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `04:56:05`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `8873902` lines, RTDE/URCap CSV `8881081` lines, run directory about `3.6G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Sixtieth checkpoint update at `2026-05-30T22:52:43+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  18020.983644276`, `rtde_rows: 9010461`, `udp_rows: 9003332`, requested
  duration `86400.0 s`.
- `2026-05-30T22:52:59+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:00:40`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `9011425` lines, RTDE/URCap CSV `9018692` lines, run directory about `3.7G`;
  disk free about `530G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-first checkpoint update at `2026-05-30T22:57:43+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  18321.405090556`, `rtde_rows: 9160673`, `udp_rows: 9153425`, requested
  duration `86400.0 s`.
- `2026-05-30T22:58:11+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:05:52`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `9167425` lines, RTDE/URCap CSV `9174791` lines, run directory about `3.7G`;
  disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-second checkpoint update at `2026-05-30T23:02:44+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  18621.850632438`, `rtde_rows: 9310896`, `udp_rows: 9303529`, requested
  duration `86400.0 s`.
- `2026-05-30T23:03:11+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:10:52`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `9317213` lines, RTDE/URCap CSV `9324738` lines, run directory about `3.8G`;
  disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-third checkpoint update at `2026-05-30T23:07:44+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  18922.2692629`, `rtde_rows: 9461106`, `udp_rows: 9453619`, requested
  duration `86400.0 s`.
- `2026-05-30T23:08:15+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:15:56`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `9468955` lines, RTDE/URCap CSV `9476599` lines, run directory about `3.9G`;
  disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-fourth checkpoint update at `2026-05-30T23:12:44+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  19222.332906998`, `rtde_rows: 9611137`, `udp_rows: 9603532`, requested
  duration `86400.0 s`.
- `2026-05-30T23:13:29+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:21:10`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `9626001` lines, RTDE/URCap CSV `9634001` lines, run directory about `3.9G`;
  disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-fifth checkpoint update at `2026-05-30T23:17:44+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  19522.399764653`, `rtde_rows: 9761171`, `udp_rows: 9753447`, requested
  duration `86400.0 s`.
- `2026-05-30T23:18:13+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:25:54`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `9768160` lines, RTDE/URCap CSV `9776064` lines, run directory about `4.0G`;
  disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-sixth checkpoint update at `2026-05-30T23:22:44+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  19822.494227428`, `rtde_rows: 9911217`, `udp_rows: 9903375`, requested
  duration `86400.0 s`.
- `2026-05-30T23:23:13+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:30:54`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `9917849` lines, RTDE/URCap CSV `9925838` lines, run directory about `4.0G`;
  disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-seventh checkpoint update at `2026-05-30T23:27:44+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  20122.607437898`, `rtde_rows: 10061273`, `udp_rows: 10053311`, requested
  duration `86400.0 s`.
- `2026-05-30T23:28:21+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:36:02`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `10071729` lines, RTDE/URCap CSV `10079837` lines, run directory about
  `4.1G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-eighth checkpoint update at `2026-05-30T23:32:44+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  20422.671690101`, `rtde_rows: 10211304`, `udp_rows: 10203224`, requested
  duration `86400.0 s`.
- `2026-05-30T23:33:25+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:41:06`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `10223625` lines, RTDE/URCap CSV `10231822` lines, run directory about
  `4.2G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Sixty-ninth checkpoint update at `2026-05-30T23:37:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  20722.76416381`, `rtde_rows: 10361349`, `udp_rows: 10353150`, requested
  duration `86400.0 s`.
- `2026-05-30T23:38:26+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:46:07`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `10373937` lines, RTDE/URCap CSV `10382293` lines, run directory about
  `4.2G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Seventieth checkpoint update at `2026-05-30T23:42:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  21022.857579165`, `rtde_rows: 10511395`, `udp_rows: 10503078`, requested
  duration `86400.0 s`.
- `2026-05-30T23:43:00+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:50:41`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `10510729` lines, RTDE/URCap CSV `10519223` lines, run directory about
  `4.3G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-first checkpoint update at `2026-05-30T23:47:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  21322.913896236`, `rtde_rows: 10661423`, `udp_rows: 10652986`, requested
  duration `86400.0 s`.
- `2026-05-30T23:48:29+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `05:56:10`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `10675157` lines, RTDE/URCap CSV `10683763` lines, run directory about
  `4.3G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-second checkpoint update at `2026-05-30T23:52:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  21622.945865748`, `rtde_rows: 10811438`, `udp_rows: 10802883`, requested
  duration `86400.0 s`.
- `2026-05-30T23:53:27+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:01:08`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `10824209` lines, RTDE/URCap CSV `10832940` lines, run directory about
  `4.4G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-third checkpoint update at `2026-05-30T23:57:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  21922.991928493`, `rtde_rows: 10961461`, `udp_rows: 10952787`, requested
  duration `86400.0 s`.
- `2026-05-30T23:58:25+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:06:06`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `10973053` lines, RTDE/URCap CSV `10981874` lines, run directory about
  `4.5G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-fourth checkpoint update at `2026-05-31T00:02:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  22223.096924315`, `rtde_rows: 11111513`, `udp_rows: 11102720`, requested
  duration `86400.0 s`.
- `2026-05-31T00:03:05+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:10:46`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `11112937` lines, RTDE/URCap CSV `11121871` lines, run directory about
  `4.5G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-fifth checkpoint update at `2026-05-31T00:07:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  22523.146628326`, `rtde_rows: 11261537`, `udp_rows: 11252626`, requested
  duration `86400.0 s`.
- `2026-05-31T00:08:20+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:16:01`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `11270313` lines, RTDE/URCap CSV `11279395` lines, run directory about
  `4.6G`; disk free about `529G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-sixth checkpoint update at `2026-05-31T00:12:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  22823.19164854`, `rtde_rows: 11411560`, `udp_rows: 11402529`, requested
  duration `86400.0 s`.
- `2026-05-31T00:13:31+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:21:12`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `11425625` lines, RTDE/URCap CSV `11434802` lines, run directory about
  `4.6G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-seventh checkpoint update at `2026-05-31T00:17:45+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  23123.671948394`, `rtde_rows: 11561800`, `udp_rows: 11552650`, requested
  duration `86400.0 s`.
- `2026-05-31T00:18:08+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:25:49`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `11563885` lines, RTDE/URCap CSV `11573221` lines, run directory about
  `4.7G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-eighth checkpoint update at `2026-05-31T00:22:46+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  23424.134943269`, `rtde_rows: 11712031`, `udp_rows: 11702763`, requested
  duration `86400.0 s`.
- `2026-05-31T00:23:08+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:30:49`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `11713833` lines, RTDE/URCap CSV `11723230` lines, run directory about
  `4.7G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Seventy-ninth checkpoint update at `2026-05-31T00:27:46+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  23724.593298422`, `rtde_rows: 11862260`, `udp_rows: 11852872`, requested
  duration `86400.0 s`.
- `2026-05-31T00:28:03+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:35:44`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `11861261` lines, RTDE/URCap CSV `11870807` lines, run directory about
  `4.8G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eightieth checkpoint update at `2026-05-31T00:32:53+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  24025.042597268`, `rtde_rows: 12012484`, `udp_rows: 12002978`, requested
  duration `86400.0 s`.
- `2026-05-31T00:33:07+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:40:48`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `12013001` lines, RTDE/URCap CSV `12022650` lines, run directory about
  `4.9G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-first checkpoint update at `2026-05-31T00:38:03+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  24325.481313125`, `rtde_rows: 12162703`, `udp_rows: 12153078`, requested
  duration `86400.0 s`.
- `2026-05-31T00:38:20+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:46:01`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `12169625` lines, RTDE/URCap CSV `12179448` lines, run directory about
  `4.9G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-second checkpoint update at `2026-05-31T00:43:11+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  24625.947209606`, `rtde_rows: 12312935`, `udp_rows: 12303191`, requested
  duration `86400.0 s`.
- `2026-05-31T00:43:28+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:51:09`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `12323521` lines, RTDE/URCap CSV `12333429` lines, run directory about
  `5.0G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-third checkpoint update at `2026-05-31T00:47:56+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  24926.38344736`, `rtde_rows: 12463153`, `udp_rows: 12453290`, requested
  duration `86400.0 s`.
- `2026-05-31T00:48:10+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `06:55:51`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `12464053` lines, RTDE/URCap CSV `12474105` lines, run directory about
  `5.0G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-fourth checkpoint update at `2026-05-31T00:53:00+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  25226.816817962`, `rtde_rows: 12613369`, `udp_rows: 12603388`, requested
  duration `86400.0 s`.
- `2026-05-31T00:53:14+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:00:55`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `12616157` lines, RTDE/URCap CSV `12626320` lines, run directory about
  `5.1G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-fifth checkpoint update at `2026-05-31T00:58:02+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  25527.243807672`, `rtde_rows: 12763582`, `udp_rows: 12753482`, requested
  duration `86400.0 s`.
- `2026-05-31T00:58:16+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:05:57`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `12766885` lines, RTDE/URCap CSV `12777193` lines, run directory about
  `5.2G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-sixth checkpoint update at `2026-05-31T01:03:13+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  25827.705051532`, `rtde_rows: 12913813`, `udp_rows: 12903593`, requested
  duration `86400.0 s`.
- `2026-05-31T01:03:34+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:11:15`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `12925885` lines, RTDE/URCap CSV `12936297` lines, run directory about
  `5.2G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-seventh checkpoint update at `2026-05-31T01:07:58+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  26128.168332984`, `rtde_rows: 13064044`, `udp_rows: 13053705`, requested
  duration `86400.0 s`.
- `2026-05-31T01:08:14+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:15:55`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `13065781` lines, RTDE/URCap CSV `13076283` lines, run directory about
  `5.3G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-eighth checkpoint update at `2026-05-31T01:13:08+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  26428.205612525`, `rtde_rows: 13214062`, `udp_rows: 13203605`, requested
  duration `86400.0 s`.
- `2026-05-31T01:13:29+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:21:10`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `13223209` lines, RTDE/URCap CSV `13233858` lines, run directory about
  `5.3G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Eighty-ninth checkpoint update at `2026-05-31T01:17:57+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  26728.210745792`, `rtde_rows: 13364064`, `udp_rows: 13353488`, requested
  duration `86400.0 s`.
- `2026-05-31T01:18:13+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:25:54`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `13365001` lines, RTDE/URCap CSV `13375793` lines, run directory about
  `5.4G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Ninetieth checkpoint update at `2026-05-31T01:23:00+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  27028.652165606`, `rtde_rows: 13514285`, `udp_rows: 13503589`, requested
  duration `86400.0 s`.
- `2026-05-31T01:23:19+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:31:00`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `13518001` lines, RTDE/URCap CSV `13528891` lines, run directory about
  `5.5G`; disk free about `528G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-first checkpoint update at `2026-05-31T01:28:14+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  27329.119495679`, `rtde_rows: 13664518`, `udp_rows: 13653704`, requested
  duration `86400.0 s`.
- `2026-05-31T01:28:28+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:36:09`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `13672157` lines, RTDE/URCap CSV `13683129` lines, run directory about
  `5.5G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-second checkpoint update at `2026-05-31T01:33:18+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  27629.577931264`, `rtde_rows: 13814747`, `udp_rows: 13803814`, requested
  duration `86400.0 s`.
- `2026-05-31T01:33:31+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:41:12`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `13823833` lines, RTDE/URCap CSV `13834937` lines, run directory about
  `5.6G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-third checkpoint update at `2026-05-31T01:37:58+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  27930.006258864`, `rtde_rows: 13964961`, `udp_rows: 13953909`, requested
  duration `86400.0 s`.
- `2026-05-31T01:38:12+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:45:53`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `13964105` lines, RTDE/URCap CSV `13975352` lines, run directory about
  `5.6G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-fourth checkpoint update at `2026-05-31T01:43:06+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  28230.466497609`, `rtde_rows: 14115190`, `udp_rows: 14104019`, requested
  duration `86400.0 s`.
- `2026-05-31T01:43:20+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:51:01`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `14117937` lines, RTDE/URCap CSV `14129315` lines, run directory about
  `5.7G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-fifth checkpoint update at `2026-05-31T01:48:14+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  28530.906062035`, `rtde_rows: 14265410`, `udp_rows: 14254120`, requested
  duration `86400.0 s`.
- `2026-05-31T01:48:30+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `07:56:11`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `14272937` lines, RTDE/URCap CSV `14284415` lines, run directory about
  `5.8G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-sixth checkpoint update at `2026-05-31T01:53:22+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  28831.368440117`, `rtde_rows: 14415641`, `udp_rows: 14404232`, requested
  duration `86400.0 s`.
- `2026-05-31T01:53:36+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:01:17`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `14425729` lines, RTDE/URCap CSV `14437328` lines, run directory about
  `5.8G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-seventh checkpoint update at `2026-05-31T01:57:56+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  29131.829101599`, `rtde_rows: 14565871`, `udp_rows: 14554343`, requested
  duration `86400.0 s`.
- `2026-05-31T01:58:10+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:05:51`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `14562729` lines, RTDE/URCap CSV `14574444` lines, run directory about
  `5.9G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-eighth checkpoint update at `2026-05-31T02:02:56+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  29432.245799902`, `rtde_rows: 14716078`, `udp_rows: 14704432`, requested
  duration `86400.0 s`.
- `2026-05-31T02:03:10+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:10:51`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `14712365` lines, RTDE/URCap CSV `14724211` lines, run directory about
  `5.9G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- Ninety-ninth checkpoint update at `2026-05-31T02:08:08+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  29732.672165365`, `rtde_rows: 14866291`, `udp_rows: 14854526`, requested
  duration `86400.0 s`.
- `2026-05-31T02:08:22+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:16:03`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `14868365` lines, RTDE/URCap CSV `14880349` lines, run directory about
  `6.0G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- One-hundredth checkpoint update at `2026-05-31T02:13:11+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  30033.147506311`, `rtde_rows: 15016529`, `udp_rows: 15004644`, requested
  duration `86400.0 s`.
- `2026-05-31T02:13:24+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:21:05`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `15019469` lines, RTDE/URCap CSV `15031566` lines, run directory about
  `6.1G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- One-hundred-first checkpoint update at `2026-05-31T02:18:14+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  30333.616891291`, `rtde_rows: 15166763`, `udp_rows: 15154760`, requested
  duration `86400.0 s`.
- `2026-05-31T02:18:29+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:26:10`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `15171885` lines, RTDE/URCap CSV `15184058` lines, run directory about
  `6.1G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- One-hundred-second checkpoint update at `2026-05-31T02:23:18+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  30634.08823642`, `rtde_rows: 15316998`, `udp_rows: 15304876`, requested
  duration `86400.0 s`.
- `2026-05-31T02:23:33+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:31:14`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `15323625` lines, RTDE/URCap CSV `15335929` lines, run directory about
  `6.2G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- One-hundred-third checkpoint update at `2026-05-31T02:28:03+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  30934.560972087`, `rtde_rows: 15467234`, `udp_rows: 15454993`, requested
  duration `86400.0 s`.
- `2026-05-31T02:28:16+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:35:57`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `15465157` lines, RTDE/URCap CSV `15477580` lines, run directory about
  `6.2G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- One-hundred-fourth checkpoint update at `2026-05-31T02:33:13+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  31234.997336084`, `rtde_rows: 15617452`, `udp_rows: 15605092`, requested
  duration `86400.0 s`.
- `2026-05-31T02:33:26+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:41:07`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `15620001` lines, RTDE/URCap CSV `15632558` lines, run directory about
  `6.3G`; disk free about `527G` on `/home/andy/ur10e_ros2_ws`.
- One-hundred-fifth checkpoint update at `2026-05-31T02:38:25+08:00`: same
  checkpoint file reports `ok_so_far: true`, `errors: []`, `elapsed_s:
  31535.463911835`, `rtde_rows: 15767685`, `udp_rows: 15755205`, requested
  duration `86400.0 s`.
- `2026-05-31T02:38:40+08:00`巡检：tmux session still alive; capture PID `7754`
  elapsed `08:46:21`; Dashboard `Safetymode: NORMAL`, `Program running: true`,
  `programState: PLAYING 3.urp`; direct RTDE URCap register check returned
  `157` samples with `157` distinct tuples. Files at this巡检: UDP CSV
  `15776469` lines, RTDE/URCap CSV `15789160` lines, run directory about
  `6.3G`; disk free about `526G` on `/home/andy/ur10e_ros2_ws`.
- Final stop requested by user at `2026-05-31T02:39+08:00` because the dataset
  was sufficient. Codex sent `Ctrl-C` to tmux session
  `ur10e_24h_three_stream_20260530`; the capture script handled `SIGINT` and
  wrote checkpoint `stop_reason: signal_2`. The checkpoint records final UDP
  `STOP` command sent at `2026-05-31T02:39:34.446`, `ok_so_far: true`, and
  `errors: []`.
- Final summary generated at about `2026-05-31T02:46+08:00`: summary JSON
  `three_stream_24h_20260530_20260530_175220_summary.json`, plots
  `plots/three_stream_24h_20260530_20260530_175220_first_zero_fz_overlay.png`
  and
  `plots/three_stream_24h_20260530_20260530_175220_first_zero_force_axes.png`.
  Summary reports `ok: true`, `stop_reason: signal_2`, RTDE samples
  `15816006` at `499.998458 Hz`, UDP samples `15803493` at `499.602935 Hz`,
  UDP status all `0`, sequence delta all `+1`, sample-counter delta all `+2`,
  and no errors.
- Markdown report written to
  `three_stream_24h_20260530_report.md`. Note: Dashboard after logger stop
  still reported `Program running: true`, `programState: PLAYING 3.urp`;
  stop the PolyScope program manually on the pendant if no longer needed.
