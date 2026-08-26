# R012 I-off vs I-on canary (not overnight BO)

## Scope

Manual / alternate-order canary only. **Not** part of the R012 overnight qLogNEI campaign.
Integral gain and integral limit are **never** overnight BO axes.

## Fixed production default (overnight BO)

- `force_i_gain = 0`
- `i_off = true`
- I is **not** a GP axis in R012 production.

## Canary contrast (n=3 each arm, alternating order)

| Arm | force_i_gain | i_off | Integrator |
|-----|--------------|-------|------------|
| I-off (production) | 0 | true | n/a |
| I-on (canary) | **2.55e-3** | false | Ki=`2.55e-3`, integral limit **fixed 15 N·s** (not searched) |

## Protocol

1. Alternate I-off and I-on blocks (e.g. off×1, on×1, …) until each arm has **n=3** exact sealed W9c entry repeats at the same P/D/D/τ/Ko/Kp anchor.
2. **PATH entry:** clear/reset integrator at PATH start for I-on.
3. **Conditional freeze:** freeze integral on loss of contact **or** `|force error| > 2 N`.
4. Early-abort remains κ=2 **novel-BO-only**; canary / reference / confirm / retest use full 550-bin completion.
5. Compare sealed MAE means; do **not** feed canary rows into production 5D GP until a separate I-on night identity is opened.
6. Record arm label in ledger kind (`CANARY_I_OFF` / `CANARY_I_ON`).

## Promotion gate

Open a **new** I-on overnight campaign identity only if:

- I-on confirmed mean sealed MAE is lower than I-off, and
- no windup / hard-stop / reason43 rate increase vs I-off on the n=3 block.

## Explicit non-goals

- No I dimension in 5D GP.
- No overnight BO with I active or integral-limit search.
- No controller upload/readback from this canary spec alone.
