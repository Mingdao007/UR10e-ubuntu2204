# TASE offline campaign receipt, 2026-09-21

This directory is the sealed offline record for schema
`ur10e.tase-research-campaign-v1`. It is a deterministic software proxy, not
UR10e, bridge, physical-contact, or human-push evidence. The controller saw
the measured-wrench normal proxy only. The pose channel uses one fixed proxy
rotation and does not encode the case normal; no true surface height or
pointwise curvature was supplied.

The fixed budget was 8 initial + 12 BO + 4 repeat units and five blocked
holdout rounds for each of eight named identities. Each tuning candidate was
evaluated on the same four cases, producing 768 tuning and 160 holdout
attempts. The 12 BO slots use a history-dependent Gaussian-kernel
lower-confidence-bound acquisition on case-balanced mean MAE. The matched QP
has one admissible solver identity, so its BO slots are recorded repeated
acquisitions of that fixed solver. Failed attempts remain in the denominator:
336 tuning and 70 holdout failures, including all three unavailable
TASE+LAC/NAC/SFC composition identities and infeasible QP cases.

The primary endpoint is normal-force MAE. Paired differences are method minus
`TASE_RNN_MATURE` over the 20 independently seeded block/case holdout units
where both methods completed. The same trial seed is shared across methods for
each pair. The 95% CI results
are recorded in `summary.json`; no method supports the preregistered 0.10-N
improvement threshold. `attempts.jsonl` and `holdout.jsonl` are the complete
row ledgers. Their SHA-256 values are:

```text
attempts.jsonl  32793868d0475a0e42bdc7a170b43c5e449876a0dc58db91ae271b7c79546a9c
holdout.jsonl   a7783ffa93f259c8ec5b5276469e50108cfa710f67b553401035ae8b9eaa2b69
```

Re-run from `tools/tase_research_campaign.py` with the recorded seed and
`build_contact_qp.py` output. The campaign deliberately does not register a
composition as executable merely to fill a denominator.
