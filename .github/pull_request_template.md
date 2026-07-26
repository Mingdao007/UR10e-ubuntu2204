## Change class

Select exactly one:

- [ ] `behavior_preserving`
- [ ] `behavior_changing`
- Frozen baseline: `6f9ef0912842ac003545eb1906b38d13c7552218`

## Allowed deltas

<!-- List every production-observable delta. Use an explicit reason when the
change is behavior-preserving but alters orchestration or diagnostics. -->

## Protected v1 closure

- [ ] The complete frozen v1 source/config/TP closure is zero-diff.
- [ ] No controller upload, load/Play, ARM, zero/tare, contact, or motion occurred.

## Rollback

```bash
# Add the exact non-interactive rollback command. Do not leave this comment only.
```

## Validation commands

```bash
# Add the exact commands run and keep URSim/HIL out of the hermetic PR gate.
```

## Evidence and remaining gates

<!-- State which Small/Medium checks passed. Mark Large URSim and HIL as not_run
unless their digest/identity-bound evidence actually exists. -->
