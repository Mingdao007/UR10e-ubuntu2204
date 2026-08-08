# R008 Phase-5 B3 live cutover (raw_codec `r008raw_v2`)

Cutover from pre-Phase-5 B3 campaigns (e.g. `4ea41044…`, `d1b34669…`) to the
**new formal fingerprint** that includes binary-native seal identity.

See also: `docs/r008_seal_phase5_fp_prepare.md` (offline republish rationale).

## Published identity (verified 2026-08-07)

| Field | Value |
|-------|-------|
| **campaign_fingerprint** | `b461ed52f69d2ab11c41a252898e88122b5c1dcacc21ba102222e1a9e7250f8b` |
| **raw_codec** | `r008raw_v2` |
| **contract_sha256** | `89d75ea389018015feb71ec66258e66d8a2abbbc003f4765e2fba3fe315cb4f7` |
| **program** | `step5d_strict_rnn_autotune_v4_r008_b3_two_stage` |
| **parent (mainline, never resume)** | `1db4f9bf587826e7562dccef7040f627e660e540f847a2e8b92f63879870cc4a` |

Config: `config/step5d/autotune_v4_r008_b3_two_stage.json` → `b3_canary`.

Triplet digests (deploy-manifest):

| artifact | sha256 |
|----------|--------|
| script | `17be0b240132e7dbcf93649684f897f42848e489737322d8feb7fdeeb77dbaf8` |
| txt | `d979097d188fd8e192e10800c96c0ffc3865f382f793bfc5e2810f7d33a29618` |
| urp | `61659550bc507599ada28a97f62a21158ba290305128091ed548d8b81d41eefb` |

Fingerprint is embedded in `.script` (`V4_CAMPAIGN_FINGERPRINT`), `.deploy-manifest.json`, and `.numeric-sanity.json`.

**No cross-codec resume.** Old ledgers remain dual-read only.

## Host wiring — Phase-5 scopes (confirmed)

Both B3 and mainline r008 host entrypoints enter Phase-5 overlays:

```python
# tools/run_step5d_autotune_v4_r008_b3_two_stage.py (live)
with (
    r008_timing_scope(),
    r008_fresh_verify_scope(),
    r008_bounded_sidecar_scope(),      # → nests r008_binary_seal_scope()
    r008_optimizer_timeout_scope(),
    r008_optimizer_keepalive_scope(),
):
    ...
```

- `r008_bounded_sidecar_scope()` patches sidecar cold-verify and **nests** `r008_binary_seal_scope()` (`bounded_sidecar_verify.py:440`).
- Seal daemon also opens `r008_bounded_sidecar_scope(tail_rows=1)` at init (`seal_daemon.py:92`).
- Phase-5 writes slim JSON `samples: []` + sibling `.r008raw` (`R008RAW2` magic) via `ledger_raw_artifact.py`.

Same pattern in `tools/run_step5d_autotune_v4_r008.py` (mainline r008 host).

### Closed constraints (do not change at cutover)

- `PACKET_STALE_S` stays **80 ms** — no seal inside contact search or PATH60.
- Pre-HOME drain/join only (`R008_PREHOME_SEAL_JOIN`).

## Live readiness gate (read-only)

Before cutover, confirm:

```bash
# No formal host / seal daemon
ps aux | rg 'run_step5d_autotune|supervise_step5d|seal_daemon' | rg -v rg

# Observers are diagnose-only (OK to leave running on *old* run dirs)
ps aux | rg step5d_r008_body_observer | rg -v rg

# Dashboard stopped (prepare will stop/load/play)
python3 - <<'PY'
import socket, time
s = socket.create_connection(("192.168.1.18", 29999), timeout=3)
s.recv(4096)
s.sendall(b"running\n")
print(s.recv(4096).decode(errors="replace").strip())
s.close()
PY
```

**2026-08-07 check:** no formal host; two body_observer watches on pre-Phase-5 runs (`live_20260806_*`). Prepare was **not** run here — it requires Script1 home motion (not motion-free).

**Blocker (same day):** Dashboard reports `PLAYING step5d_strict_rnn_autotune_v4_r008_b3_two_stage.urp` with **no** formal host process — orphan TP. **Do not prepare/launch until Dashboard stop** and `running=false`. Status snapshot: `/tmp/r008_phase5_cutover_status.json`. Pre-P5 baseline metrics: `/tmp/r008_phase5_baseline_195949.json`.

## Cutover procedure

Worktree:

```bash
export WT=/home/andy/.codex-worktrees/step5d-v4-r004-20260801/experiments/tase-contact-reproduction
export CONTROL_PY=/home/andy/.local/share/step5d-autotune-v3/runtimes/8f980abdd3d9a4ebef460be259f6cd7c0c802015ee553201d3a888be83252475/control/bin/python
cd "$WT"
```

### 0. Offline recipe (no robot)

```bash
STAMP=$(date +%Y%m%d_%H%M%S)
RUN=$WT/runs/step5d_autotune_v4_r008/live_${STAMP}_b3_phase5_raw2
mkdir -p "$RUN" && find "$RUN" -mindepth 1 -delete

python3 tools/launch_step5d_autotune_v4_r008_b3_two_stage.py \
  --run-dir "$RUN" \
  --rebuild-triplet   # omit if triplet already matches deploy-manifest
```

Writes `$RUN/b3_launch_recipe.json` with FP, triplet digests, prepare/host module names.

### 1. Upload B3 triplet (controller)

```bash
python3 tools/upload_ur_tp_package.py step5d_strict_rnn_autotune_v4_r008_b3_two_stage \
  --local-dir programs/step5/step5d
# Save readback dir + deploy result JSON for prepare.
```

### 2. Prepare — new formal ledger (plays Script1 + loads B3 resident; **no ARM**)

Requires **empty** run dir, fresh controller readback, dashboard NORMAL:

```bash
"$CONTROL_PY" -u tools/prepare_step5d_autotune_v4_r008_b3_two_stage.py \
  --root "$WT" \
  --run-dir "$RUN" \
  --controller-readback-dir "<readback_dir>" \
  --controller-readback-result "<deploy_or_readback_result.json>" \
  --robot-host 192.168.1.18 \
  --kunwei-host 192.168.50.25 \
  --kunwei-port 5152
```

Success JSON status: `r008_b3_two_stage_resident_ready_no_arm`.

Record from `launch_context.json`:

- `route_id`, `attempt_id`, `session_id`, `session_epoch`
- triplet sha256s, `campaign_fingerprint`, `contract_sha256`

```bash
echo "$RUN" > /tmp/r008_phase5_cutover_run_dir.txt
```

### 3. Thresholds receipt

Issue `$RUN/thresholds_receipt.json` with **Phase-5** `campaign_fingerprint` and `contract_sha256` (`schema=step5d.autotune-v4/r006-runtime-thresholds-v1`, typically `application_mae_threshold_n=0.35`, `pac_epsilon_n=0.05`).

### 4. Host launch (supervised — never bare nohup)

Build ids JSON (same shape as `/tmp/r008_b3_ids.json`) from prepare output.

```bash
"$CONTROL_PY" -u tools/supervise_step5d_autotune_v4_r008_b3_host.py \
  --run-dir "$RUN" \
  --ids-json /tmp/r008_b3_phase5_ids.json \
  --control-python "$CONTROL_PY"
```

Host module: `run_step5d_autotune_v4_r008_b3_two_stage live` with mandatory `--resident-session-id`.

Formal autotune: **omit** `--timing-canary`.

## Success checks after first sealed trial

| Check | Expected (Phase-5) |
|-------|-------------------|
| Run FP | `b461ed52…` in ledger header / launch_context |
| Binary sidecar | `raw_force_evidence/*.r008raw` starts with `R008RAW2` |
| Slim JSON artifact | `samples: []` in sibling `.json` |
| Objective receipt version | `r008-sealed-binary-raw-v1` |
| raw_codec in receipt bundle | `r008raw_v2` |
| Seal daemon stages | `r006_append` + `ledger_append` logged; `seal_total` present |
| Pre-HOME join | `R008_PREHOME_SEAL_JOIN:waited_s=…` in host.log (typically ~5–6 s) |
| Freshness | **zero** `reason=43` / `reason_code=43` in host.log |

## Measurement script

```bash
python3 tools/r008_phase5_cutover_measure.py "$RUN"
python3 tools/r008_phase5_cutover_measure.py "$RUN" --json
```

Also installed at `/tmp/r008_phase5_cutover_measure.py` (symlink).

Works on historical runs (reports legacy fat JSON vs Phase-5 slim+binary mix).

Example baseline (pre-Phase-5 `live_20260806_195949_*`):

- `seal_total` p50 ≈ 5.1 s, `r006_append` p50 ≈ 4.0 s
- `R008_PREHOME_SEAL_JOIN` present on older campaigns; wave4 bo_fix run used `R008_ASK_SEAL_JOIN` instead
- `reason=43`: 0 hits in sampled tail (still monitor on cutover)

## Quick FP verify (offline)

```bash
cd "$WT"
python3 - <<'PY'
import json
from pathlib import Path
import sys; sys.path.insert(0, "tools")
from step5d_autotune_v4_r008.b3_identity import load_b3_contract
cfg = json.loads(Path("config/step5d/autotune_v4_r008_b3_two_stage.json").read_text())
c = load_b3_contract()
assert cfg["b3_canary"]["raw_codec"] == "r008raw_v2"
assert cfg["b3_canary"]["campaign_fingerprint"].startswith("b461ed52")
assert c.campaign_fingerprint == cfg["b3_canary"]["campaign_fingerprint"]
print("OK", c.campaign_fingerprint[:16], cfg["b3_canary"]["raw_codec"])
PY
```
