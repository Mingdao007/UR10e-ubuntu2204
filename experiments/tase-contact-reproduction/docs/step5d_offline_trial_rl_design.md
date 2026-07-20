# Step5d offline trial-level policy design

This package builds an offline-only proposal for the next campaign. It does not
write current V3 optimizer state, start a bridge, load a TP program, arm motion,
or expose an API that a live controller can consume.

## Evidence model

One physical trial produces one dataset row and one action. Dense CSV samples
are inputs to deterministic feature extraction, never independent actions or
episodes. Every record binds its immutable source digest, campaign/plant split
group, parameter-semantics fingerprint, outcome class, metric role, explicit
missingness, and reward/constraint eligibility.

V2 actions remain three-dimensional because `orientation_ko` is not hash-bound;
the field is `null` and `action_complete` is false. V3 trials 13–22 retain their
diagnostic-only or unavailable metric roles and cannot populate reward learning.
Infrastructure, observer, operator, parameter-safety, model-mismatch, and unknown
outcomes remain separate. Unknown evidence fails closed.

## Conservative policy gate

Future eligible data uses a deterministic 32-member bootstrap ridge ensemble
with alpha `1e-3`, seed `20260720`, and log2 action coordinates. Candidate actions
are exact observed certified actions only. Training requires at least two grouped
campaign/plant epochs and at least `max(10, 2 * (design_rank + 1))` complete reward
and constraint records. Evaluation is leave-one-group-out; the 90% reward interval
must cover at least 80% of held-out rewards. The 95% Wilson upper bound for unsafe
constraints must be no greater than 0.20.

If any gate fails, the artifact has `result_status=insufficient_valid_trials`, a
null proposal, and `warm_start_eligible=false`. The fixed configuration
`[0.001, 0.00001, 7.0, 0.4]` is labeled
`configuration_fallback_not_policy`; it is not evidence of an optimized action.

Every artifact remains `promotion_status=offline_only`,
`usable_as=next_campaign_warm_start`, and
`current_v3_optimizer_eligible=false`. A separate owner-gated task is required
for any future VIC-RL residual inference, HIL, or attended live promotion.
