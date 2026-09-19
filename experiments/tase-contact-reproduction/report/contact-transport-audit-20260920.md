# Contact transport preparation audit

Scope: offline source inspection and deterministic tests. No Dashboard, RTDE,
sensor, program load/play, bridge, motion or contact operation was performed.

The new research branch starts at `6753fa48`. The original
`contact-six-qp-20260917` checkout retains its sensor-capture edit, provider
edit, and untracked `run_contact_six_live.py`. A binary patch and the untracked
script were preserved under `runs/preserved-input-20260920/` with SHA-256
manifest before any new implementation. The original files were not edited.

## Integrated fixes

- `R004OutputSnapshot` retains wall-clock receipt time and separately carries
  `received_monotonic_s`, captured when RTDE returns a newly decoded sample.
  Repeated controller timestamps and empty polls do not refresh this field.
  The contact provider rejects absent receive evidence and lets the existing
  freshness contract reject stale frames. This timestamp measures host decode
  receipt, not wire arrival or robot execution; these remain distinct evidence.
- Home orientation is compared by the shortest relative quaternion angle,
  rather than Euclidean rotation-vector difference. Equivalent representations
  differing by 2*pi pass, while a true 0.02 rad change is still measured.
  The contact-six TP generator already uses `pose_trans(pose_inv(expected),
  actual)` for this check; this host fix aligns with that existing package.
  Historical R004 template bytes were not regenerated or uploaded.
- Synthetic timing/entry fixtures now declare their synthetic receive clock.
  They do not supply physical timing evidence.

The focused receive/Home/provider tests passed (8); runtime/freshness/Home
regressions passed (20). The larger legacy contact run initially reported
129 passed / 4 failed: two required the absent worktree-local QP build, one
campaign depended on that build, and one fixture omitted the new receive
field. After building the existing native QP and updating synthetic fixtures,
the affected campaign/timing/entry group passed all 9 tests.

## Preserved runner is not an admitted route

The untracked historical runner records the intended startup order: stop,
confirm STOPPED, negotiate input recipe, load/play, and hand the same open
connection to the sole mature writer. Its source has three unresolved identity
defects: Home SHA mismatch is accepted if nonempty; prerequisite validation
discards `now_s`; and modified contract content retains historical hashes.
Copying these receipts would weaken admission. This runner is therefore
preserved as audit input, not installed as the new task entrypoint.

The existing R013 resident binding validator already checks physical output
identity and rejects unregistered programs or disguised historical identity.
Any new live integration must use those actual bindings, fresh read-back,
and uninterrupted session evidence. The new research task has no admitted
resident target yet. Full transport qualification and a correctly bound
single-writer adapter remain necessary after offline mechanism acceptance.

Historical PLAYING or Home observations from the source task are not current
hardware observations. No current robot-state claim is made here.
