# Autotuner V4/V5 completion status — 2026-08-18

This is a status/incident report, not a promotion receipt. The original handoff
and its SHA are unchanged. No Stage B result is promoted and no V5 Primary or
Correction campaign was started.

```mermaid
flowchart TD
  A[Stage A I-off 5D search<br/>100 attempts] --> A1[Observed: 1 exact, 99 ineligible]
  A1 --> A2[Search NOT passed]
  A2 --> A3[Independent confirmation roots<br/>n=3, old source identity]
  A3 --> A4[Confirmation root PASS only]

  B[Stage B I-on 6D old root] --> B1[First resident reuse boundary failure<br/>ARM_ENTRY -> CONTACT_SEARCH]
  B1 --> B2[100 search: 1 exact, 99 ineligible]
  B2 --> B3[Post-budget confirmation loop]
  B3 --> B4[Safe Home-boundary SIGINT]
  B4 --> B5[SKIPPED / INVALID; artifacts retained]

  C[Figure-eight fresh package/readback] --> C1[No-motion probe PASS]
  C1 --> C2[No-contact canary PASS]
  C2 --> C3[7 capability chains sealed]
  C3 --> C4{Home/rollover equivalence}
  C4 -->|CI [-0.23516,+0.23436] N| C5[NO-GO measurement-resolution]
  C5 --> C6[Primary/Correction not started]

  D[Managed runtime timeout incident] --> D1[Child entered live owner]
  D1 --> D2[Outer 60 s kill left stale owner/.part]
  D2 --> D3[Controller STOP + Home recovery]
  D3 --> D4[Root forbidden from resume]

  A4 --> E[Current endpoint: discuss/pilot gate]
  B5 --> E
  C6 --> E
  D4 --> E
```

## Evidence tiers

| Layer | Current status | Evidence/identity | Admission consequence |
|---|---|---|---|
| Stage A search | `NOT_PASS` | `v4_stage_ff_ioff_100_20260818.jsonl`; 100 closed, 1 exact, 99 ineligible | no search promotion |
| Stage A confirmation | `PASS_CURRENT_CONFIRMATION_ROOT` only | `reports/v4_stage_a_confirmation_20260818.json`; n=3, mean `0.5212097187 N`; source identity `37529c…` | coordinate/baseline seed only after source rebind |
| Stage B search | `SKIPPED_INVALID` | `reports/v4_stage_b_skipped_invalid_20260818.json`; resident boundary failure starts at ordinal 2 | no Stage B BO evidence |
| Figure-eight package | stale for any future run after later source fixes | fresh readback artifacts exist but current source closure has advanced | rebuild/readback required before another live round |
| Figure-eight no-motion/no-contact | `PASS` for the executed identity | recipe receipts and canary run `094401` | no campaign admission by itself |
| Figure-eight capability | `NO_GO_MEASUREMENT_RESOLUTION` | `reports/v5_capability_measurement_resolution_block_20260818.json`; seven chains, fixed `±0.05 N` margin | Primary/Correction blocked |
| Managed timeout | `INCIDENT / NON-RESUMABLE` | `reports/v5_managed_timeout_incident_20260818.json` | fresh state root required |

## Fixed boundaries

- `0.98` legacy timing ratio was not relaxed.
- No safety/CBF/HardTube gate was weakened.
- Censoring remains separate from GP/tell/ranking; the cross-stage early-stop
  implementation is versioned, 25-bin V4-specific, and physical sequence-bound.
- The Figure-eight equivalence margin remains `±0.05 N`; estimated required
  pairs (`111`) exceed the declared upper bound (`80`), so no automatic extra
  pilot or margin change was made.
- Current robot evidence after recovery: `STOPPED`, `Safety NORMAL`, stationary
  Home verified.

## Current source identity

`config/step6/r013_figure8_direct_campaign_v1.json` is currently bound to
source closure `4bc8ec7813a287fe64d299f6def12d0acbbd97081cf0229a5b4bebd3338c9b05`.
Any future Figure-eight live run must rebuild its package/readback/no-motion
receipts against this identity and use a fresh state root.
