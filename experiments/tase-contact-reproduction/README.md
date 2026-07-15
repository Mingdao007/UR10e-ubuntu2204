# TASE contact reproduction

This repository contains UR10e Step5/Step6 contact-control experiments and
their retained evidence. Current Step5d autotune maintenance is centered on
the v2 control plane; historical implementations remain available as archive
evidence, not as competing sources of current state.

## Current Step5d route

Step5d native autotune v1 is frozen. The v2 control plane replaces its
directory/Markdown/process-local state with:

- a pure physical lifecycle reducer;
- one SQLite campaign database;
- atomic mailbox transport;
- a framed Dashboard client;
- a systemd user service with `Restart=no` and one writer;
- one-way legacy evidence import;
- fixed two-table trial reports.

The offline v2 implementation does not authorize bridge start, TP Play,
motion, contact, zero/tare, or controller writes. Live cutover remains blocked
until the current controller triplet is fetched, the user waypoint is
preserved, the TP watchdog-only diff passes, and fresh readback and golden
replay evidence exist. The frozen G10 golden replay is now attested by
`config/step5/golden_replay_g10_v2_result.json`; controller-bound gates remain
closed.

## Operator interface

The canonical user command is:

```bash
step5d-autotune-live.sh status --json
```

Supported control-plane commands are:

```bash
step5d-autotune-live.sh start
step5d-autotune-live.sh enqueue --file batch.json
step5d-autotune-live.sh report --batch latest --format markdown
step5d-autotune-live.sh replay --group G10 --reason video
step5d-autotune-live.sh stop-after-current
```

Calling `step5d-autotune-live.sh` without arguments maps to `start`. `start`
must fail closed with one primary blocker until all live gates are current.

Parameter-only `enqueue` accepts exactly five new tuples and performs only
schema, envelope, Decimal identity, and global dedup checks. It does not run
tests, alter the deployment fingerprint, commit code, or restart the service.

## Sources of truth

- `config/step5/current.json`: compact static deployment and safety envelope.
- `runs/step5d_autotune_v2/control_plane.sqlite3`: ignored dynamic campaign state.
- `config/step5/stages/index.json`: generated per-stage manifest index.
- `STEP5_FLOW.md`: generated compact operator view.
- `docs/archive/step5/INDEX.md`: SHA-backed long-form document archive.
- `config/step5/autotune_legacy_map_v1.json`: immutable G1–G15 import map.

Legacy `config/current_stage.json`, `config/step5_stage_table.json`, and
`config/step5d/current.json` remain frozen compatibility inputs until v2 live
acceptance. They are not read by the v2 runtime.

## Safety invariants

- One live writer owns RTDE inputs and the bridge process tree.
- RNN, trajectory, force/frame math, units, rates, command order, and guards
  are frozen during the orchestration refactor.
- `reaction_normal` measures load; `approach_normal` defines press/posture.
- A tuple becomes non-repeatable as soon as TP consumption is proven or
  ambiguous.
- Home verification and immutable raw sealing precede ACK.
- Metrics, PNG generation, and transfers begin only after physical closure.
- Replay candidates never enter optimizer/incumbent history by default.
- Static authorization and fresh runtime readiness are separate facts.

## Development

Run the focused v2 suite without unrelated ROS pytest plugins:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_step5d_autotune_v2_*.py
```

Run generated-document and archive checks:

```bash
python3 tools/generate_step5_docs.py --check
python3 tools/split_step5_stage_table.py --check
python3 tools/index_step5_archives.py --check
python3 tools/validate_step5_docs.py
```

The complete legacy suite is not a per-parameter gate. Code changes use the
focused deterministic suite plus applicable UR force/frame, textbook,
package, and readback gates.

## Historical material

Pre-v2 README and Step5 flow content is preserved under
`docs/archive/step5/`. Runtime recordings remain under ignored `runs/` trees
and are imported read-only with source checksums. Existing handoffs keep their
original paths and are indexed separately; they are never rewritten to make a
new campaign appear clean.
