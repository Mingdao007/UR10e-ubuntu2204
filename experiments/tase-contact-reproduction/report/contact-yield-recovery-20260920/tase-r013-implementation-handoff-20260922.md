# TASE-RNN 60 s Figure-eight implementation handoff

## Implemented scope

This change adds the explicit `figure8_window60_r013_compat_v1` protocol for
the TASE-RNN Figure-eight autotuner. It keeps the existing 62.831853 s
full-period route under its original identity and does not silently relabel
one as the other. The new protocol uses the 1 s entry, a 60 s PATH, the formal
metric window `[5,60)`, 0.1 s bins, 550 required bins, 5 N target, and the
40 mm by 10 mm reference amplitudes at 0.1 rad/s.

The sole live writer now selects the typed protocol request and the matching
collector. The TASE provider records the selected identity and rejects a
parameter file whose protocol identity differs. `figure8.sh` accepts an
explicit `--duration r013_60`; the autotuner always passes that token, while
the old full-period default remains available as a separate route.

Offline-only composition support records
`TASE_RNN_MATURE+SFC_TANGENTIAL`, tangent-projects SFC, transports the tangent
basis, keeps TASE normal/orientation authority, and realizes one bounded joint
velocity command. The conditional double-clamp policy is sealed as
`conditional-double-clamp-v1` with a 1.0 N s state cap and 0.5 N authority.

## Changed files

- `tools/tase_figure8_protocol.py`
- `tools/yield_contact_evidence.py`
- `tools/contact_yield_live_path.py`
- `tools/tase_contact_provider.py`
- `tools/step5d_autotune_v4_r004_live_writer.py`
- `tools/contact_yield_live_writer.py`
- `tools/contact_yield_live.py`
- `tools/tase_autotuner.py`
- `tools/tase_r013_timing_ledger.py`
- `tools/tase_rnn_profile_replay.py`
- `tools/tase_sfc_fusion.py`
- `scripts/figure8.sh`
- `config/tase_figure8_window60_r013_compat_v1.json`
- `config/tase_autotuner_v1.json`

## Validation

- 24 focused offline tests passed in `.venv-contact-six`.
- Existing TASE/provider/live mock regression passed: 36 tests, 1 warning.
- `scripts/contact-six.sh status` passed with `device_io=false`,
  `motion_authorized=false`, and `full_composition_imports_ready=true`.
- Textbook alignment and contact semantic gates passed; both report the
  offline-only boundary.
- A fixed-input Profile A/B replay was sealed at
  `report/contact-yield-recovery-20260920/tase-rnn-profile-replay-r013-20260922.json`.
  It compares legacy `r=1` with historical `r=.8/512`; it is an offline
  hypothesis comparison and carries no live promotion.
- `python3 -m py_compile` and `bash -n scripts/figure8.sh` passed.

## Live acceptance status

No bridge, TP play, Kunwei stream, robot motion, or autotuner campaign was
started by this implementation. The current resolver for this experiment
reports package read-back and liveprep identity/readiness as fresh and places
the route in `awaiting_live_authorization`; the next owner is the live-bench
route. A real 60 s unit still needs the existing Home/contact/search/entry/
PATH/stop/relief/Home gates, raw traces, video, timing ledger, and verified
Home before it can count as an autotuner baseline.

The previous 62.831853 s evidence remains separate and is not used as a
60 s observation. No manuscript or controller-comparison claim is changed by
this handoff.

## Implementor completion

Implementation and offline validation are complete for the bounded software
slice described above. The implementor is returning the live-bench gate to
the main task owner; no physical acceptance is claimed.

## Auditor completion

Independent auditor review is pending. The report is intentionally marked
offline/tooling-only until that review and the first clean 60 s live unit are
sealed.

## Implementor risk

The live writer still depends on fresh package/read-back, bridge, sensor, Home,
and recovery gates. None of those physical predicates are inferred from the
offline tests or from the earlier 62 s campaign.

## Auditor evidence inspected

The auditor should inspect the resolver snapshot, contact-six preflight,
focused and mock regression outputs, the fixed-input replay receipt, and the
first fresh 60 s run directory before making a physical acceptance decision.

## Auditor validation

The current offline validation is reproducible from the commands listed above;
live validation is intentionally absent from this handoff.

## Auditor acceptance

Offline/tooling scope only. This handoff does not accept a robot run, promote a
controller, or authorize a manuscript result.

## Auditor risk

The 60 s collector and the existing 62 s collector are separate identities;
mixing their receipts would invalidate the autotuner denominator and must be
rejected.

## Auditor next decision

After a fresh resolver/read-back gate, decide whether to dispatch the first
explicit `r013_60` live unit through the single recovery owner. A failed unit
remains in the denominator even if Home recovery succeeds.
