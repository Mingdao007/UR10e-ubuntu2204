# Step5d Autotune v3 refactoring contract

This contract governs the v3 orchestration refactor. It does not authorize a
bridge, controller connection, TP load/Play, ARM, sensor zero, contact, or
motion. The v1 control path remains the default rollback until a separately
authorized promotion gate passes.

## Frozen baseline

- Commit: `6f9ef0912842ac003545eb1906b38d13c7552218`
- Tag: `archive/step5d-autotune-v1-20260715` (must resolve to the commit above)
- Contract: protected v1 sources, campaign config, and TP triplet remain
  byte-for-byte identical to the frozen commit.
- v3 may call the frozen implementation through an adapter. It must not copy,
  fork, or silently rewrite the control law or TP package.

The repository validator embeds and checks the SHA-256 of every protected path:

- `experiments/tase-contact-reproduction/tools/kunwei_rtde_bridge.py`
- `experiments/tase-contact-reproduction/tools/step5d_p0_v9_control_core.py`
- `experiments/tase-contact-reproduction/tools/step5d_p0_v9_bridge.py`
- `experiments/tase-contact-reproduction/tools/step5d_autotune_contract.py`
- `experiments/tase-contact-reproduction/tools/step5d_autotune_coordinator.py`
- `experiments/tase-contact-reproduction/tools/step5d_autotune_journal.py`
- `experiments/tase-contact-reproduction/tools/step5d_autotune_store.py`
- `experiments/tase-contact-reproduction/tools/step5d_autotune_live_driver.py`
- `experiments/tase-contact-reproduction/tools/step5d_runtime_interface.py`
- `experiments/tase-contact-reproduction/tools/step5d_paper_outer_loop.py`
- `experiments/tase-contact-reproduction/tools/step5d_control_contract.py`
- `experiments/tase-contact-reproduction/tools/contact_semantics.py`
- `experiments/tase-contact-reproduction/tools/build_step5d_autotune_tp.py`
- `experiments/tase-contact-reproduction/scripts/bridge-line-operator.sh`
- `experiments/tase-contact-reproduction/scripts/step5d-autotune-live.sh`
- `experiments/tase-contact-reproduction/config/step5_safe_frame.json`
- `experiments/tase-contact-reproduction/config/step5d_autotune_campaign_v1.json`
- `experiments/tase-contact-reproduction/config/step5d_autotune_i_scale_sanity_v1.json`
- `experiments/tase-contact-reproduction/config/current_stage.json`
- `experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.script`
- `experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.txt`
- `experiments/tase-contact-reproduction/programs/step5/step5d/step5d_strict_rnn_autotune_v1.urp`

`tools/run_step5d_autotune_campaign.py` is orchestration, not the control law.
Its v1 blob remains the declared baseline, while the validator permits exactly
one whole-file `behavior_changing` variant SHA for two default-off v3 hooks:
the durable HOME latch and ACK-after derived postprocess queue. Any other runner
bytes fail closed. With both v3 flags absent, the v1 path remains unchanged.

`config/step5_stage_table.json` is deliberately outside byte-freeze because the
inactive v3 selector is an allowed orchestration addition. Its active/default
v1 binding remains covered by behavior tests and the separate promotion gate.

## Change declaration

Every v3 PR must use `.github/pull_request_template.md` and declare exactly one
change class:

- `behavior_preserving`: production-observable control and safety behavior is
  unchanged. Orchestration-only differences still belong in allowed deltas.
- `behavior_changing`: any lifecycle, persistence, ACK, postprocess, or operator
  behavior deliberately changes. The delta and its rollback must be explicit.

The declaration must bind the full frozen commit, list allowed deltas, and
provide executable rollback and test commands. The validator consumes the PR
body from the GitHub event payload; it does not call the GitHub API.

Keep each PR conceptually single-purpose. The working targets are at most 400
lines of manually authored logic per PR. More than 1,000 changed lines or 20
manually edited files must be split before review. Tests and generated immutable
evidence are reported separately from manually authored logic.

## Runtime budget

All v3 Python runtime modules live directly under
`tools/step5d_autotune_v3/`. The fail-closed budget is:

- no more than seven Python modules, including `__init__.py`;
- no more than 2,500 physical source lines after blank and comment-only lines
  are excluded;
- tests, schemas, service units, and documentation do not count as runtime.

Adding an eighth module or placing v3 runtime outside the governed directory
requires a new approved contract; renaming files to evade the budget is not an
allowed delta.

## Test lanes and promotion boundary

`config/step5d_autotune_v3_test_matrix.json` is the machine-readable lane
contract:

- Small and Medium are hermetic, offline, no-network CI lanes.
- Hosted Small CI may stub only the matrix-listed numeric/live modules that the
  parser never executes. It must import the SHA-protected bridge and assert the
  real `parse_args` code filename and source digest; the parser and its helpers
  may never be doubled.
- Large is manual, serial, digest-pinned URSim, HOLD-only, no ARM, and no
  motion.
- HIL is manual, serial, bound to a fresh controller identity, HOLD-only, no
  ARM, and no motion. It remains blocked until the UR owner authorizes that
  separate phase.

The path-filtered CI workflow runs only Small and Medium. A green PR never
implies that URSim or HIL ran. Large and HIL evidence must name the exact pin
and remain `not_run` until actually executed.

Repository-only validation is useful during editing but is not a PR
declaration acceptance:

```bash
python3 tools/validate_step5d_autotune_v3_refactor.py --repository-only
```

## Review, rollback, and stopping rules

- Add a failing characterization/contract test before a behavior-preserving
  repair. A test must demonstrate that the escaped defect turns red.
- Keep refactor, behavior change, state migration, and selector/promotion in
  separate commits or PRs so each has an independent rollback.
- The first verifiable checkpoint should appear within ten minutes. If no
  independently green milestone exists after thirty minutes, stop expanding
  scope and split the work.
- The local affected fast gate targets 60 seconds. The complete offline release
  gate targets two minutes, excluding formal replay, URSim, and HIL.
- A protected v1 byte change, unclassified flag, missing impacted-test mapping,
  or missing rollback fails closed.

This structure follows the public guidance on small, independently testable
changes and production-configuration testing:

- https://google.github.io/eng-practices/review/developer/small-cls.html
- https://google.github.io/eng-practices/review/reviewer/looking-for.html
- https://sre.google/sre-book/testing-reliability/
- https://abseil.io/resources/swe-book/html/ch14.html
- https://abseil.io/resources/swe-book/html/ch22.html
