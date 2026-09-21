# TASE offline campaign receipt, 2026-09-21

This directory is the sealed offline record for schema
`ur10e.tase-research-campaign-v1`. It is a deterministic software proxy, not
UR10e, bridge, physical-contact, or human-push evidence. The controller saw
the measured-wrench normal proxy only; no true surface height or pointwise
curvature was supplied.

The fixed budget was 8 initial + 12 BO + 4 repeat units and five blocked
holdout rounds for each of eight named identities. The run contains 192 tuning
and 160 holdout attempts. Failed attempts remain in the denominator: 72 tuning
and 60 holdout failures, including all three unavailable TASE+LAC/NAC/SFC
composition identities.

The primary endpoint is normal-force MAE. Paired differences are method minus
`TASE_RNN_MATURE` over 20 holdout pairs where executable. The 95% CI results
are recorded in `summary.json`; no method supports the preregistered 0.10-N
improvement threshold. `attempts.jsonl` and `holdout.jsonl` are the complete
row ledgers. Their SHA-256 values are:

```text
attempts.jsonl  7a6e093d83d54135fc35d045baf082e7592a29b259641a281eb4da2ee22fcfd4
holdout.jsonl   873dd1dc8d01ab1fee47ce174fb19e66095a627c5318ac9e9ea4da3c0616da3c
```

Re-run from `tools/tase_research_campaign.py` with the recorded seed and
`build_contact_qp.py` output. The campaign deliberately does not register a
composition as executable merely to fill a denominator.
