# R012 host launch (brain over R008 B3 motion)

R012 live host **reuses** R008 B3 motion, TP, seal, queue, and ledger from:

`/home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/tase-contact-reproduction`

R012 owns:

- `ConfirmedIncumbentScheduler` (3× REFERENCE → incumbent → BO)
- 5D production GP + qLogNEI (`tools/step5d_autotune_v4_r012/qlognei.py`)
- Protocol **612012** censor handshake (registers 35/36)

Motion still comes from `step5d_strict_rnn_autotune_v4_r008_b3_two_stage`.

## Offline smoke

```bash
cd /home/andy/.codex-worktrees/step5d-r011-offline-autotune-20260809/experiments/tase-contact-reproduction
python3 tools/run_step5d_autotune_v4_r012_live.py --dry-run
pytest tests/test_step5d_autotune_v4_r012_live_host.py -q
```

## Prepare (no upload by default)

```bash
python3 tools/prepare_step5d_autotune_v4_r012_live.py
```

Writes `runs/step5d_autotune_v4_r012/live_*/r012_host_launch_context.json`.

## Upload B3 triplet (reuse R008 prepare)

Use the sibling worktree B3 prepare to upload/readback the published B3 program, then copy receipts into the R012 run dir:

```bash
python3 /home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/tase-contact-reproduction/tools/prepare_step5d_autotune_v4_r008_b3_two_stage.py \
  --root "$PWD" \
  --run-dir runs/step5d_autotune_v4_r012/live_YYYYMMDD_HHMMSS_r012_host \
  --controller-readback-dir /tmp/r008_b3_readback \
  --controller-readback-result /tmp/r008_b3_readback_result.json \
  --robot-host 192.168.1.18 \
  --kunwei-host 192.168.50.25 \
  --kunwei-port 5152
```

## Live mode

Routine live uses `--mode bounded_live` (the default) and a direct owner
factory. It does not require a magic authorization token or repeated chat
confirmation. The owner factory must return `(writer, runner)` or an
`R012TrialExecutor`; the executor installs and verifies the composed PATH guard
stack before dispatch.

`--mode formal_release` is explicit opt-in and additionally requires
`R012_ALLOW_FORMAL_LIVE=r012_b3_scheduler_qlognei`. `R008_ALLOW_FORMAL_LIVE` is
always rejected.

**Forbidden:** `R008_ALLOW_FORMAL_LIVE` (R008 Wave5 quarantine must not bypass R012).

## Supervise

```bash
CONTROL_PY=~/.local/share/step5d-autotune-v3/runtimes/.../control/bin/python
$CONTROL_PY -u tools/supervise_step5d_autotune_v4_r012_host.py \
  --run-dir runs/step5d_autotune_v4_r012/live_YYYYMMDD_HHMMSS_r012_host \
  --ids-json /tmp/r012_host_ids.json \
  --control-python $CONTROL_PY
```

Add `--owner-factory module:callable` for the direct live owner. Add
`--mode formal_release` and the formal environment variable only for that
explicit mode.

## Remaining glue (honest)

1. **Bench motion**: upload/play B3 triplet (or future R012 wave program) and confirm protocol **612012** reg36 ack on live TP.
2. **`bounded_tuning_ready` / `live_ready`**: stay false until `motion_proven` evidence is supplied to the readiness report.

Adapter glue (scheduler-first `_refill`, serial q=1, direct result ingest,
guard-bound writer, and one-attempt Home stop) is implemented in
`tools/step5d_autotune_v4_r012/live_adapter.py`.

Do **not** widen `PACKET_STALE_S` or place seal inside search/PATH60.
