# R008 Phase 5 — B3 fingerprint prepare (raw_codec)

## Why a new formal ledger

Phase 5 adds `raw_codec: r008raw_v2` into B3 fingerprint material
(`compute_b3_campaign_fingerprint` / `build_b3_contract_document` in
`tools/step5d_autotune_v4_r008/b3_identity.py`). That changes the campaign
identity hash. Published contract + program triplet must be regenerated; live
work continues only on a **new formal ledger** bound to the new fingerprint.

## Identity rules

- B3 FP material now includes `raw_codec = r008raw_v2` (binary-native seal codec).
- Old campaigns: **dual-read only** (read/verify historical artifacts; do not
  treat as the active write identity).
- **No cross-codec resume** — do not continue an old-FP campaign under the new
  codec or vice versa.
- **Live cutover only on the new ledger** after contract + triplet republish.

## Explicit non-goals (closed)

- Does **not** widen `PACKET_STALE_S` (remains 80 ms).
- Does **not** place seal/tell inside contact search or PATH60
  (no seal∩PATH60). Pre-HOME drain/join remains the safe placement.

## Republish checklist

1. `load_b3_contract()` → rewrites `config/step5d/autotune_v4_r008_b3_two_stage.json`
2. `build_b3_triplet(...)` → updates `programs/step5/step5d/` B3 artifacts
3. Confirm wave1–4 fingerprints differ; update test prefixes if needed
4. Start live only against the new `campaign_fingerprint` ledger
