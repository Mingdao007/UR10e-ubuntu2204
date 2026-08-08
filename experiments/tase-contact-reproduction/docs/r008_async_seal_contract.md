# R008 async seal contract

Authoritative contract for async seal/tell vs BO ask, formal ledger, and motion
freshness gates. Code pointers: `async_seal.py`, `live_adapter.py`
(`R008_ASK_SEAL_NO_JOIN`).

## Sealed MAE authority

**Sealed MAE** (cold-verify `objective_mae_n` on a durable sidecar row) is the
only score that may drive:

- formal ledger append / eligibility
- hard stop-gate (`hard_stop_penalty`, I-on unlock, campaign stop)
- RETEST / tell decisions

In-flight or predicted values are not sealed until cold verify completes and the
row is durable on disk.

## BO ask: no join (`R008_ASK_SEAL_NO_JOIN`)

Optimizer ask binds against **already-sealed sidecar rows only** — it does
**not** `join_all()` before each ask. The BO/GP posterior may therefore lag the
latest physical attempt by **≥1 sealed attempt**. This is **intended**: ask must
not block on seal wall, and must not treat in-flight work as sealed.

## Forbidden: fantasy / placeholder scores

**Fantasy, placeholder, or predicted scores** (simulation, GP mean, builder
estimates, UI placeholders) **MUST NOT** enter:

- stop-gate or campaign-stop logic
- formal tell / ledger append as if sealed

They may inform exploration scheduling only when explicitly non-authoritative.

## Remaining joins (unchanged until live evidence)

These motion-path joins stay in place; do not remove without live proof:

1. **preHOME** — `join_executing()` drains in-flight seal before next HOME
2. **search-critical** — `begin_search_critical()` joins executing seal before
   contact search / PATH60 span (ARM → SAFE_RETURN)

Rationale: reason=43 / TP freshness; see `async_seal.py` header.

## Closed non-goals

- **`PACKET_STALE_S` stays 80 ms** — do not widen to absorb seal cost (~15–35 s
  would be required; kills realtime gate).
- **No seal ∩ PATH60** — seal/tell must not overlap contact search or PATH60.
  PATH-kind seals use `hold_until_path60`; safe drain is preHOME only.

## Related

- Phase 5 fingerprint / new ledger: `docs/r008_seal_phase5_fp_prepare.md`
- Phase 5 live cutover: `docs/r008_seal_phase5_live_cutover.md`
- Join-gate decision (keep joins until P5 live 43): `docs/r008_seal_join_gate_decision.md`
- Cursor rule (search-critical): `~/.cursor/rules/r008-seal-search-critical.mdc`
