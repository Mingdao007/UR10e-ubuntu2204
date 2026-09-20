# Yield training-result report

training-only development snapshot; not independent validation or a physical result; no superiority claim.

prospective DEVELOPMENT-model comparison; no real precise-contact, safety, or physical qualification claim

Main adjudication in `report/yield-round8-main-review-v1/main-adjudication.md` governs interpretation. A finite objective is not a feasible incumbent. Failed or interrupted units consume budget. SFC stopping before EI is a registered campaign outcome, not an equal-budget freeze or a proposal-superiority result.

## Snapshot identity

| field | value |
| --- | --- |
| observed_utc | 2026-09-20T05:55:54.339092+00:00 |
| source | transactionally consistent read-only SQLite snapshot |
| campaign_root | /home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction/runs/yield-fair-training-v1 |
| campaign_protocol_sha256 | ef310e1e96c098de506c02014f11d4269bce79c8c109a5f37b09a7bdb57a4ff3 |
| config_sha256 | ce36c7a3ba824d4bf4e7c748f3ca77a075e15a381d34b54a0d2b2db81febd27d |
| training_cell_id | stiff_low_mu |
| selection_contract_id | yield-fair-selection-contract-v1 |
| inflight |  |
| frozen | false |
| equal_budget_freeze | false |
| validation_executed | false |
| physical_executed | false |

## Budget and terminal states

Each method has 24 ordinal pair slots. Unspent SFC ordinals after an initial-feasibility stop are `stopped_before_ei`, not a carried best-so-far line.

| method | budget spent | completed | feasible | failed | incomplete | stopped-before-EI slots | not run | stopped before EI | full 24 spent |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SFC | 8 | 8 | 2 | 0 | 0 | 16 | 0 | true | false |
| DSFC | 24 | 24 | 9 | 0 | 0 | 0 | 0 | false | true |
| MSFC | 24 | 24 | 11 | 0 | 0 | 0 | 0 | false | true |

## All 72 ordinal slots

| method | unit | phase | state | pair feasible | nominal feasible | disturbed guards | J (ledger) | literal repeat | shared initial triple |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SFC | 0 | initial | completed_feasible | true | true | true | 77.4531943 | false | true |
| SFC | 1 | initial | completed_feasible | true | true | true | 65.6475347 | false | true |
| SFC | 2 | initial | completed_nominal_infeasible | false | false | true | 68.0511605 | false | true |
| SFC | 3 | initial | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 96.7492045 | false | true |
| SFC | 4 | initial | completed_nominal_infeasible | false | false | true | 102.252015 | false | true |
| SFC | 5 | initial | completed_nominal_infeasible | false | false | true | 66.8878011 | false | true |
| SFC | 6 | initial | completed_nominal_infeasible | false | false | true | 152.541288 | false | true |
| SFC | 7 | initial | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 180.910205 | false | true |
| SFC | 8 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 9 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 10 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 11 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 12 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 13 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 14 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 15 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 16 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 17 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 18 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 19 | bayesian_ei | stopped_before_ei |  |  |  |  | false | false |
| SFC | 20 | repeat_incumbent | stopped_before_ei |  |  |  |  | true | false |
| SFC | 21 | repeat_incumbent | stopped_before_ei |  |  |  |  | true | false |
| SFC | 22 | repeat_incumbent | stopped_before_ei |  |  |  |  | true | false |
| SFC | 23 | repeat_incumbent | stopped_before_ei |  |  |  |  | true | false |
| DSFC | 0 | initial | completed_feasible | true | true | true | 85.820681 | false | true |
| DSFC | 1 | initial | completed_feasible | true | true | true | 84.900443 | false | true |
| DSFC | 2 | initial | completed_feasible | true | true | true | 81.254663 | false | true |
| DSFC | 3 | initial | completed_nominal_infeasible | false | false | true | 86.1717742 | false | true |
| DSFC | 4 | initial | completed_nominal_infeasible | false | false | true | 93.0239462 | false | true |
| DSFC | 5 | initial | completed_nominal_infeasible | false | false | true | 85.5420657 | false | true |
| DSFC | 6 | initial | completed_nominal_infeasible | false | false | true | 97.6639027 | false | true |
| DSFC | 7 | initial | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 171.216932 | false | true |
| DSFC | 8 | bayesian_ei | completed_feasible | true | true | true | 80.5710109 | false | false |
| DSFC | 9 | bayesian_ei | completed_nominal_infeasible | false | false | true | 492.620956 | false | false |
| DSFC | 10 | bayesian_ei | completed_nominal_infeasible | false | false | true | 80.120369 | false | false |
| DSFC | 11 | bayesian_ei | completed_nominal_infeasible | false | false | true | 80.2493707 | false | false |
| DSFC | 12 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 564.862475 | false | false |
| DSFC | 13 | bayesian_ei | completed_feasible | true | true | true | 82.0992566 | false | false |
| DSFC | 14 | bayesian_ei | completed_nominal_infeasible | false | false | true | 81.6781767 | false | false |
| DSFC | 15 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 393.107444 | false | false |
| DSFC | 16 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 471.040047 | false | false |
| DSFC | 17 | bayesian_ei | completed_disturbed_guard_infeasible | false | true | false | 80.0317078 | false | false |
| DSFC | 18 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 57.6091379 | false | false |
| DSFC | 19 | bayesian_ei | completed_nominal_infeasible | false | false | true | 98.0148419 | false | false |
| DSFC | 20 | repeat_incumbent | completed_feasible | true | true | true | 80.5710109 | true | false |
| DSFC | 21 | repeat_incumbent | completed_feasible | true | true | true | 80.5710109 | true | false |
| DSFC | 22 | repeat_incumbent | completed_feasible | true | true | true | 80.5710109 | true | false |
| DSFC | 23 | repeat_incumbent | completed_feasible | true | true | true | 80.5710109 | true | false |
| MSFC | 0 | initial | completed_feasible | true | true | true | 85.2722653 | false | true |
| MSFC | 1 | initial | completed_feasible | true | true | true | 84.8865623 | false | true |
| MSFC | 2 | initial | completed_feasible | true | true | true | 81.407248 | false | true |
| MSFC | 3 | initial | completed_nominal_infeasible | false | false | true | 86.9629579 | false | true |
| MSFC | 4 | initial | completed_nominal_infeasible | false | false | true | 93.9356873 | false | true |
| MSFC | 5 | initial | completed_nominal_infeasible | false | false | true | 85.3312538 | false | true |
| MSFC | 6 | initial | completed_nominal_infeasible | false | false | true | 94.8174163 | false | true |
| MSFC | 7 | initial | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 173.528308 | false | true |
| MSFC | 8 | bayesian_ei | completed_feasible | true | true | true | 80.6368541 | false | false |
| MSFC | 9 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 491.980217 | false | false |
| MSFC | 10 | bayesian_ei | completed_feasible | true | true | true | 85.0691653 | false | false |
| MSFC | 11 | bayesian_ei | completed_feasible | true | true | true | 86.7634464 | false | false |
| MSFC | 12 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 475.530813 | false | false |
| MSFC | 13 | bayesian_ei | completed_nominal_infeasible | false | false | true | 81.7840832 | false | false |
| MSFC | 14 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 371.429358 | false | false |
| MSFC | 15 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 343.019912 | false | false |
| MSFC | 16 | bayesian_ei | completed_nominal_infeasible | false | false | true | 79.3785471 | false | false |
| MSFC | 17 | bayesian_ei | completed_feasible | true | true | true | 80.1716677 | false | false |
| MSFC | 18 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 507.215573 | false | false |
| MSFC | 19 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | false | false | false | 540.257346 | false | false |
| MSFC | 20 | repeat_incumbent | completed_feasible | true | true | true | 80.1716677 | true | false |
| MSFC | 21 | repeat_incumbent | completed_feasible | true | true | true | 80.1716677 | true | false |
| MSFC | 22 | repeat_incumbent | completed_feasible | true | true | true | 80.1716677 | true | false |
| MSFC | 23 | repeat_incumbent | completed_feasible | true | true | true | 80.1716677 | true | false |

Ledger J components are reported with absolute descriptors. They are not asserted to be directly optimized precision or safety.

## Initial units 0–7

Shared-triple comparison only where the registered mechanical `(m, mu, g)` values actually match. Infeasible rows are retained.

| unit | method | state | nominal | disturbed guards | J | nom force MAE (N) | nom path RMS (m) | nom progress | dist force MAE (N) | dist path RMS (m) | dist progress | dist low-load <1N (s) | shared triple |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | SFC | completed_feasible | feasible | feasible | 77.4531943 | 0.111888107 | 0.00543327869 | 0.854637043 | 0.476741197 | 0.00707853073 | 0.876478634 | 0 | true |
| 0 | DSFC | completed_feasible | feasible | feasible | 85.820681 | 0.0752700387 | 0.00522040337 | 0.876881818 | 0.446775173 | 0.00642773049 | 0.894044372 | 0 | true |
| 0 | MSFC | completed_feasible | feasible | feasible | 85.2722653 | 0.0767926994 | 0.00522050417 | 0.876563649 | 0.447963509 | 0.0064234164 | 0.893972044 | 0 | true |
| 1 | SFC | completed_feasible | feasible | feasible | 65.6475347 | 0.0971850691 | 0.00547334859 | 0.852458223 | 0.462576072 | 0.00703748536 | 0.868937255 | 0 | true |
| 1 | DSFC | completed_feasible | feasible | feasible | 84.900443 | 0.0714854421 | 0.00521886885 | 0.875053109 | 0.444495656 | 0.0064869684 | 0.886158807 | 0 | true |
| 1 | MSFC | completed_feasible | feasible | feasible | 84.8865623 | 0.0720101995 | 0.00521885534 | 0.874971727 | 0.444833253 | 0.00649637431 | 0.886454503 | 0 | true |
| 2 | SFC | completed_nominal_infeasible | infeasible | feasible | 68.0511605 | 0.113136193 | 0.00553513038 | 0.846943968 | 0.471888108 | 0.00725758049 | 0.862735251 | 0 | true |
| 2 | DSFC | completed_feasible | feasible | feasible | 81.254663 | 0.0601474318 | 0.00521576191 | 0.873507359 | 0.431222729 | 0.00635607842 | 0.882778951 | 0 | true |
| 2 | MSFC | completed_feasible | feasible | feasible | 81.407248 | 0.0608973258 | 0.00521650037 | 0.873291811 | 0.431952024 | 0.00636016695 | 0.882684022 | 0 | true |
| 3 | SFC | completed_nominal_and_disturbed_guard_infeasible | infeasible | infeasible | 96.7492045 | 0.135338412 | 0.00549673105 | 0.851210806 | 0.525391423 | 0.00777141326 | 0.86132139 | 0.61 | true |
| 3 | DSFC | completed_nominal_infeasible | infeasible | feasible | 86.1717742 | 0.12566957 | 0.00546687466 | 0.852195152 | 0.502921882 | 0.0076995786 | 0.865239525 | 0 | true |
| 3 | MSFC | completed_nominal_infeasible | infeasible | feasible | 86.9629579 | 0.125704814 | 0.00546654373 | 0.852196159 | 0.503068518 | 0.00771263582 | 0.865061494 | 0 | true |
| 4 | SFC | completed_nominal_infeasible | infeasible | feasible | 102.252015 | 0.251087463 | 0.00540609089 | 0.871169435 | 0.58983142 | 0.00662734625 | 0.888971504 | 0 | true |
| 4 | DSFC | completed_nominal_infeasible | infeasible | feasible | 93.0239462 | 0.126760096 | 0.00523701923 | 0.871349399 | 0.493665706 | 0.00656723673 | 0.889429593 | 0 | true |
| 4 | MSFC | completed_nominal_infeasible | infeasible | feasible | 93.9356873 | 0.137062222 | 0.00525500472 | 0.870213922 | 0.503263919 | 0.00646667535 | 0.889078353 | 0 | true |
| 5 | SFC | completed_nominal_infeasible | infeasible | feasible | 66.8878011 | 0.17278003 | 0.00549682878 | 0.856654032 | 0.551787782 | 0.00708349967 | 0.876610427 | 0 | true |
| 5 | DSFC | completed_nominal_infeasible | infeasible | feasible | 85.5420657 | 0.146865793 | 0.00544092626 | 0.85536722 | 0.517149645 | 0.00721594482 | 0.877228028 | 0 | true |
| 5 | MSFC | completed_nominal_infeasible | infeasible | feasible | 85.3312538 | 0.147124415 | 0.00544201726 | 0.855297938 | 0.517184597 | 0.00728401394 | 0.87715024 | 0 | true |
| 6 | SFC | completed_nominal_infeasible | infeasible | feasible | 152.541288 | 0.567434078 | 0.00490564126 | 0.825473875 | 0.819122942 | 0.00498748572 | 0.8600356 | 0 | true |
| 6 | DSFC | completed_nominal_infeasible | infeasible | feasible | 97.6639027 | 0.135458384 | 0.00450621287 | 0.833482815 | 0.496478501 | 0.00482238783 | 0.875619177 | 0 | true |
| 6 | MSFC | completed_nominal_infeasible | infeasible | feasible | 94.8174163 | 0.166376555 | 0.00457552275 | 0.831691414 | 0.526474412 | 0.00492934964 | 0.870612441 | 0 | true |
| 7 | SFC | completed_nominal_and_disturbed_guard_infeasible | infeasible | infeasible | 180.910205 | 1.32987623 | 0.00623558634 | 0.853001077 | 1.2324838 | 0.00693198723 | 0.828133656 | 0 | true |
| 7 | DSFC | completed_nominal_and_disturbed_guard_infeasible | infeasible | infeasible | 171.216932 | 0.78523959 | 0.00587888396 | 0.855667865 | 0.769387643 | 0.00698923907 | 0.817908247 | 0 | true |
| 7 | MSFC | completed_nominal_and_disturbed_guard_infeasible | infeasible | infeasible | 173.528308 | 0.871590883 | 0.00594499642 | 0.852714818 | 0.826410325 | 0.0070238818 | 0.812044782 | 0 | true |

Low-load duration: duration of true_normal_load_n < 1 N on the declared sample grid; not geometric separation or physical contact loss.

## Best-so-far J (feasible completed pairs only)

Infeasible complete objectives are listed as samples and do not update the incumbent curve. Literal repeats are labeled and are not independent confidence samples. No CI, winner, or p-value.

| method | unit | phase | state | observed J | feasible | infeasible sample | best-so-far J | literal repeat |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SFC | 0 | initial | completed_feasible | 77.4531943 | true | false | 77.4531943 | false |
| SFC | 1 | initial | completed_feasible | 65.6475347 | true | false | 65.6475347 | false |
| SFC | 2 | initial | completed_nominal_infeasible | 68.0511605 | false | true | 65.6475347 | false |
| SFC | 3 | initial | completed_nominal_and_disturbed_guard_infeasible | 96.7492045 | false | true | 65.6475347 | false |
| SFC | 4 | initial | completed_nominal_infeasible | 102.252015 | false | true | 65.6475347 | false |
| SFC | 5 | initial | completed_nominal_infeasible | 66.8878011 | false | true | 65.6475347 | false |
| SFC | 6 | initial | completed_nominal_infeasible | 152.541288 | false | true | 65.6475347 | false |
| SFC | 7 | initial | completed_nominal_and_disturbed_guard_infeasible | 180.910205 | false | true | 65.6475347 | false |
| DSFC | 0 | initial | completed_feasible | 85.820681 | true | false | 85.820681 | false |
| DSFC | 1 | initial | completed_feasible | 84.900443 | true | false | 84.900443 | false |
| DSFC | 2 | initial | completed_feasible | 81.254663 | true | false | 81.254663 | false |
| DSFC | 3 | initial | completed_nominal_infeasible | 86.1717742 | false | true | 81.254663 | false |
| DSFC | 4 | initial | completed_nominal_infeasible | 93.0239462 | false | true | 81.254663 | false |
| DSFC | 5 | initial | completed_nominal_infeasible | 85.5420657 | false | true | 81.254663 | false |
| DSFC | 6 | initial | completed_nominal_infeasible | 97.6639027 | false | true | 81.254663 | false |
| DSFC | 7 | initial | completed_nominal_and_disturbed_guard_infeasible | 171.216932 | false | true | 81.254663 | false |
| DSFC | 8 | bayesian_ei | completed_feasible | 80.5710109 | true | false | 80.5710109 | false |
| DSFC | 9 | bayesian_ei | completed_nominal_infeasible | 492.620956 | false | true | 80.5710109 | false |
| DSFC | 10 | bayesian_ei | completed_nominal_infeasible | 80.120369 | false | true | 80.5710109 | false |
| DSFC | 11 | bayesian_ei | completed_nominal_infeasible | 80.2493707 | false | true | 80.5710109 | false |
| DSFC | 12 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 564.862475 | false | true | 80.5710109 | false |
| DSFC | 13 | bayesian_ei | completed_feasible | 82.0992566 | true | false | 80.5710109 | false |
| DSFC | 14 | bayesian_ei | completed_nominal_infeasible | 81.6781767 | false | true | 80.5710109 | false |
| DSFC | 15 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 393.107444 | false | true | 80.5710109 | false |
| DSFC | 16 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 471.040047 | false | true | 80.5710109 | false |
| DSFC | 17 | bayesian_ei | completed_disturbed_guard_infeasible | 80.0317078 | false | true | 80.5710109 | false |
| DSFC | 18 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 57.6091379 | false | true | 80.5710109 | false |
| DSFC | 19 | bayesian_ei | completed_nominal_infeasible | 98.0148419 | false | true | 80.5710109 | false |
| DSFC | 20 | repeat_incumbent | completed_feasible | 80.5710109 | true | false | 80.5710109 | true |
| DSFC | 21 | repeat_incumbent | completed_feasible | 80.5710109 | true | false | 80.5710109 | true |
| DSFC | 22 | repeat_incumbent | completed_feasible | 80.5710109 | true | false | 80.5710109 | true |
| DSFC | 23 | repeat_incumbent | completed_feasible | 80.5710109 | true | false | 80.5710109 | true |
| MSFC | 0 | initial | completed_feasible | 85.2722653 | true | false | 85.2722653 | false |
| MSFC | 1 | initial | completed_feasible | 84.8865623 | true | false | 84.8865623 | false |
| MSFC | 2 | initial | completed_feasible | 81.407248 | true | false | 81.407248 | false |
| MSFC | 3 | initial | completed_nominal_infeasible | 86.9629579 | false | true | 81.407248 | false |
| MSFC | 4 | initial | completed_nominal_infeasible | 93.9356873 | false | true | 81.407248 | false |
| MSFC | 5 | initial | completed_nominal_infeasible | 85.3312538 | false | true | 81.407248 | false |
| MSFC | 6 | initial | completed_nominal_infeasible | 94.8174163 | false | true | 81.407248 | false |
| MSFC | 7 | initial | completed_nominal_and_disturbed_guard_infeasible | 173.528308 | false | true | 81.407248 | false |
| MSFC | 8 | bayesian_ei | completed_feasible | 80.6368541 | true | false | 80.6368541 | false |
| MSFC | 9 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 491.980217 | false | true | 80.6368541 | false |
| MSFC | 10 | bayesian_ei | completed_feasible | 85.0691653 | true | false | 80.6368541 | false |
| MSFC | 11 | bayesian_ei | completed_feasible | 86.7634464 | true | false | 80.6368541 | false |
| MSFC | 12 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 475.530813 | false | true | 80.6368541 | false |
| MSFC | 13 | bayesian_ei | completed_nominal_infeasible | 81.7840832 | false | true | 80.6368541 | false |
| MSFC | 14 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 371.429358 | false | true | 80.6368541 | false |
| MSFC | 15 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 343.019912 | false | true | 80.6368541 | false |
| MSFC | 16 | bayesian_ei | completed_nominal_infeasible | 79.3785471 | false | true | 80.6368541 | false |
| MSFC | 17 | bayesian_ei | completed_feasible | 80.1716677 | true | false | 80.1716677 | false |
| MSFC | 18 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 507.215573 | false | true | 80.1716677 | false |
| MSFC | 19 | bayesian_ei | completed_nominal_and_disturbed_guard_infeasible | 540.257346 | false | true | 80.1716677 | false |
| MSFC | 20 | repeat_incumbent | completed_feasible | 80.1716677 | true | false | 80.1716677 | true |
| MSFC | 21 | repeat_incumbent | completed_feasible | 80.1716677 | true | false | 80.1716677 | true |
| MSFC | 22 | repeat_incumbent | completed_feasible | 80.1716677 | true | false | 80.1716677 | true |
| MSFC | 23 | repeat_incumbent | completed_feasible | 80.1716677 | true | false | 80.1716677 | true |

## Artifact digest manifest

Complete members are hashed before parse. Running members are never read.

| attempt | status | sha256 | bytes | parsed |
| --- | --- | --- | --- | --- |
| SFC-00-nominal | complete | e51055b6e96aec95671647c044e7100567a5dafa59343afd7afa0c3adea3e3a9 | 340008836 | true |
| SFC-00-disturbed | complete | 5d835eceb7996ad91e253029522b459c2a5b861d4cfb768c16fce8d6151392aa | 340116904 | true |
| SFC-01-nominal | complete | 7943dd2ae88242eede493ea94bc24ca64a81da0dd4cd7906a89416ba6cafc714 | 339933918 | true |
| SFC-01-disturbed | complete | b2751ef469193cf86ddad09700ee581644b7f61c431b6c08489c8333ad37a5d9 | 340304899 | true |
| SFC-02-nominal | complete | e8c852ef4b5db1f396f4ca95be8504d2e7a4922937824edb7f21a91d9bf3bb68 | 339307580 | true |
| SFC-02-disturbed | complete | 00844745601772d8fbdbc785caf2977e353a6d0145db506c3051b9be43fee798 | 339975090 | true |
| SFC-03-nominal | complete | ec804e9fd8c215a886eaa8bac9a1bbf151ffd3d68a05b2c692d58aa5556c43d6 | 339948759 | true |
| SFC-03-disturbed | complete | 9b10e74288ab5fab023239f544679dc47c58e25a9d1653a678be6eba839efd5e | 340545068 | true |
| SFC-04-nominal | complete | 863c6bf56ee4219396ca9e763cb8129d2f929ac656227c39260262d381856c0d | 339660060 | true |
| SFC-04-disturbed | complete | 7bbeeadc62fbb0c3d520258aaa4ff9b30075fc7a68170080d45099a20ffc1d4f | 339787935 | true |
| SFC-05-nominal | complete | 965fb7133bded642fe699013ba5f256d1f0fb6f5fa59234f9305f28613c7e4ed | 339804102 | true |
| SFC-05-disturbed | complete | 71f68ecbed1a5df337143f34e7c2ede3533573cb51ca136f79602dfe4068e813 | 340290281 | true |
| SFC-06-nominal | complete | 6a870d9d70b3f9f81892af537852266755223312952b3dc724b41e1c5d0ef8df | 339339788 | true |
| SFC-06-disturbed | complete | 2099693f44cba134503858677b3d71f2b96f7a62679ccb2f14419352efd178cb | 340244325 | true |
| SFC-07-nominal | complete | 2bd0dafc6c3332b4ef9ce90b697a9319af53d2fa99b05821e28263ef5bd12b94 | 338392161 | true |
| SFC-07-disturbed | complete | 7e0b7469b10135cbc106facd00931c1f0c8b132b06128e7ce509d54998c82cae | 339177931 | true |
| DSFC-00-nominal | complete | e2c99ad28124832160b936b4334786440a81da031668e129fef0d883206ddd30 | 339324784 | true |
| DSFC-00-disturbed | complete | 80390b0948e4e4a5ec6e7005ad07de9dced9db78223e8e07b6509edb9ea6084e | 339404496 | true |
| DSFC-01-nominal | complete | 31145a4dc39339177fc6981d19c5b0f62fb5d3daa2b1fbfe14e8820ce66a340b | 339288510 | true |
| DSFC-01-disturbed | complete | ddae9fdc48084c2ee3c74a91099134f610a04aef739023398cf2023c05038ac3 | 339579986 | true |
| DSFC-02-nominal | complete | 681213de0e2ae22ba75ed97ee953d4a2915aab0ddfea796a7e71101d40ebdde2 | 338763637 | true |
| DSFC-02-disturbed | complete | f9565bc3c7e790a727e25bef62da49350b8c1796990e6467039b7a7d457d5eb5 | 339311993 | true |
| DSFC-03-nominal | complete | 6caf014f2f5b54bc9ccbd02a9c762b0c3c7b34f178f5d5d6df05d1ef412275fc | 339207073 | true |
| DSFC-03-disturbed | complete | 547db5e177ad9ea9a2ca82184f506c2190bf54a4d2a0b953710a9101af847823 | 339913775 | true |
| DSFC-04-nominal | complete | 8d130644c2c0b9c1d579593a03faa7152f59c7bec3f8efa59b2af093523f38ef | 339120682 | true |
| DSFC-04-disturbed | complete | 4a00a855d3f6754c5fc6ba46148bc6930dcca2043b974df069c3128af315c7a2 | 339250705 | true |
| DSFC-05-nominal | complete | b2bc36f4560f70277bb1d5a94b2cbf1a9baca2aaa86ec8d75a31325a499fd201 | 339149408 | true |
| DSFC-05-disturbed | complete | 9a97697d39d8acb0ab778f16a18feea8df17669b3433f4a4ddbeacf7fd0f38e0 | 339258405 | true |
| DSFC-06-nominal | complete | 32e6bafa68135c0aa0a713216703532ac0d547451389560f34cfc6a314fc7f16 | 338427271 | true |
| DSFC-06-disturbed | complete | da4abffe206386336d09baddcb3410e90879b0d3d2f144e9bdec8c9cd2944e0e | 339284786 | true |
| DSFC-07-nominal | complete | 1bdffc9dfc6a9218c3f6f7c6e4a733e83acd57243d36aab27f8a8a41868777a8 | 338082822 | true |
| DSFC-07-disturbed | complete | a7ff573fb23c3ea6c4fb954d64b301f64057916009488e28ca6b440f3449fe0f | 338793584 | true |
| DSFC-08-nominal | complete | 1104d5c90c3f46cd0d4e290c33f1494d5316db3a65982e3b8a6291015f37d168 | 338656509 | true |
| DSFC-08-disturbed | complete | 762ea4d7363df69fb8fd010c8990e4f0b542608ad62c26aba864da42a4b8edd5 | 339323902 | true |
| DSFC-09-nominal | complete | fcda63440634174b815cdb7059356904251c00a2dc22ef3daf0f95a446014f97 | 336721492 | true |
| DSFC-09-disturbed | complete | 26ea727798c9acb15f7c93e7b0cacaa7ac22e52feae22f9bfe8d549318e19c42 | 339209512 | true |
| DSFC-10-nominal | complete | b7062ce9376a5c9f20d36c47342c738d1606d3e64ba56c2cecd6891a5f064006 | 338464117 | true |
| DSFC-10-disturbed | complete | 93f074ab66d99decae5d681dcdff887a14c8878eacb75cdd0fac6699de84c741 | 339368381 | true |
| DSFC-11-nominal | complete | 5c3a7ae296ccd930f1a6abedddd48deda0113e127f0c449eda87462330c908fd | 338188259 | true |
| DSFC-11-disturbed | complete | 1920bcc364418b703f808fcb0d8caaa9de257b5534dbe5db57e0e41cdb713f5d | 338861022 | true |
| DSFC-12-nominal | complete | 273bdc6fad3380b6bb547f346559c115aaf0fa2c7d75c7a02d9c556fc9cffa6b | 336502696 | true |
| DSFC-12-disturbed | complete | 7dc89d0e86bdcae90a443724812c639759c79d01a180af8e08bbf93e0ba5e940 | 338935638 | true |
| DSFC-13-nominal | complete | 88232d4da28090db8242c367ca20a7e0b39461453ac17f002acea8025a1a33cc | 339273766 | true |
| DSFC-13-disturbed | complete | 345273ae63b049edc7b9b44119c774305c25294e60581d321b3cb63067bb7d04 | 339401470 | true |
| DSFC-14-nominal | complete | f843b7ab0ad36f7f67a67257d4e80ed5c1a36d0b6101026c8bf441c663252a54 | 338489224 | true |
| DSFC-14-disturbed | complete | 973ca930ea539f2bcbcf7e5fc99ab5d735f36f751a4a240d3fc9c891a9bcfab1 | 339148758 | true |
| DSFC-15-nominal | complete | c80c931ea7baf87a5b181485706135577d2b02284a7832c5e144ab027537c3e9 | 335705196 | true |
| DSFC-15-disturbed | complete | 6109d6ca3d7bdc3ffe4a5a885ad5b2737cbf5f9da1fbefc93674c66dc37a663e | 336929594 | true |
| DSFC-16-nominal | complete | a7242e0bd3df462964f3b5f82ceb1e5316fd0e3dd566bdab6f299f53d58b5959 | 336256840 | true |
| DSFC-16-disturbed | complete | 8df7093402495f3712a23be40c0a833f6e0230335fe0cb4b2918a458b6b6ac1d | 339187817 | true |
| DSFC-17-nominal | complete | dc1b33d0e382277491e029b67ed7571580e3f8c03607e35d120fe387e55ebec0 | 339272225 | true |
| DSFC-17-disturbed | complete | 0fa077f855e1293153af8162adef199bdf82a8dec241e4c737715f8e4b708c22 | 340023611 | true |
| DSFC-18-nominal | complete | fc45a0b77c0a7ca17b6be2ce227576015b952ad4b1183037fa6a2150c1b9b1c5 | 338582472 | true |
| DSFC-18-disturbed | complete | f3c7ffea131093e8338894d819b716e9246394677d3e6cd24ee1a3a94cf2d911 | 339471812 | true |
| DSFC-19-nominal | complete | eefc114c4105b37699b19075eae3585efbe7e3e2958998db352bb07480e62107 | 339226475 | true |
| DSFC-19-disturbed | complete | c0b25e409b343f822a10567b9eda9abc4ac739d61ccfcc6f692e358f9094bfd5 | 339694040 | true |
| DSFC-20-nominal | complete | 36d7f522cd19caf9f03d6b32df673538da8dc5a5ac9fd851d29cb8fa891f0744 | 338656508 | true |
| DSFC-20-disturbed | complete | f417a6c4e6f3aad2869a315a2453c1c95a6e3834fc20e4e6bd79478c455fadeb | 339323901 | true |
| DSFC-21-nominal | complete | 9db9d52b8d620bbe8fde744141cf79d63c03a942ac263718d9fb059fca85b35f | 338656508 | true |
| DSFC-21-disturbed | complete | 4b508e53232b416ab862398158e4200318a523997be6253917f75dd8c999b8e1 | 339323902 | true |
| DSFC-22-nominal | complete | b4df5f46e198b41691bf4cf61312ff926fad05bbc9cade73414cdc5cc29e933a | 338656507 | true |
| DSFC-22-disturbed | complete | abab897675776b8b1c01bbce4e3551b2d81867c32afd7d9c8283a5834c0c09b3 | 339323901 | true |
| DSFC-23-nominal | complete | ee32f66befd6bec326d68a93a3d9d82dc87c4f229302b3601d3ce0f950785dd9 | 338656508 | true |
| DSFC-23-disturbed | complete | bf68497d20d942bf58a23a39648a48330fc62a3324162e6513cfaea24420877e | 339323901 | true |
| MSFC-00-nominal | complete | 74d541d3674b10b4006b4e82606cef07516a13525e853455f8fb626101f7587c | 347920549 | true |
| MSFC-00-disturbed | complete | b323f738dd3bf67f28a0829bead3c846d971f64b6ac5688e0e89679a22c5191a | 347834315 | true |
| MSFC-01-nominal | complete | 03bbe1d2123d095ed4def2d0778f065890bbac063b811b86bfb0253ac3f5925b | 347913556 | true |
| MSFC-01-disturbed | complete | 103386ceb9fa72414c4b3e724be06b24b7131ba4a7af1b03f31621aba4f741a7 | 348035643 | true |
| MSFC-02-nominal | complete | a16f00120e4956ad1c883e897174c5a88d81fe228b32ce2eed27c83f75203ef2 | 347380988 | true |
| MSFC-02-disturbed | complete | 62a4e263f0205ee206d4dc9776a50ebb8e218e759ac063c578db3c4482bf9495 | 347946670 | true |
| MSFC-03-nominal | complete | 0d459958dc4d48deab07971b61e22f2db23a1f7f6f8f6ab525a719abaa331e25 | 347774089 | true |
| MSFC-03-disturbed | complete | 224cd7bd897ffa657a80de1f5007b41c0cb0314f3307eb190f9f295d1680705b | 348363569 | true |
| MSFC-04-nominal | complete | 6a3e93a848cbb46636673b57f183908e6e2be53f3ee5103948bf0672ab0fdc5b | 347481836 | true |
| MSFC-04-disturbed | complete | 380f5e4d62747038f37cc36b72773c53b9d055b3ef5f320bbac88ce11a1d992b | 347554686 | true |
| MSFC-05-nominal | complete | ef4e6ccb400aec22217ab68ba15a1f63f473d0091e4af7a985a4a120af2cc94d | 347734780 | true |
| MSFC-05-disturbed | complete | 09782ba2dea47a957e7017b675bf70e3fd10d1e9a6319290065907388339b691 | 347639639 | true |
| MSFC-06-nominal | complete | a6d68bc64536496e83073a482a794ed7dc2fb3106f79252c92be4485bd4bf25c | 347282364 | true |
| MSFC-06-disturbed | complete | 2ea59fe9d72a0cc1e8665c9c6da1f8e7c891509802b4a5a62109fbd7111b5d2a | 348071220 | true |
| MSFC-07-nominal | complete | 7287ac24c47b52329deae08d1aa1e7e71994e5be4065aeaab92ea8f7ab8e9e90 | 345993265 | true |
| MSFC-07-disturbed | complete | ad5c07a2705474d680506eb312b8a3c06a81cfd21d203859d02cf5d3872acdf8 | 346766480 | true |
| MSFC-08-nominal | complete | d14fa0c3a49102f17d8229eb0bd718061475994271edd8eda820b0f5f20ff358 | 347271504 | true |
| MSFC-08-disturbed | complete | 550fee4a62630ac3a2b4508789042d40b696879ff097446f90252585be7ed62d | 347840181 | true |
| MSFC-09-nominal | complete | 31e4e7d5455df2958e3e5a6c7ae6ae6f890e2e646b6de275531655a387b609e4 | 344904769 | true |
| MSFC-09-disturbed | complete | 40ef5dea311a28cbdd7909e35c463f4a165d26d5c204df4c38b8fd02f650d63c | 347578052 | true |
| MSFC-10-nominal | complete | 663ddbfab1ebfe587622cc3697895688486645e8d7966724f65057ba9df12b68 | 347911245 | true |
| MSFC-10-disturbed | complete | 7f7b9e7f472af3766003aef2153766e2ffefd035cc5e1e7c7e82498d057f5310 | 348060523 | true |
| MSFC-11-nominal | complete | 4dc5063433384a585bd09a1d7af563f70a025d1fdde871086c4d7fa2b1a0b2f8 | 347682016 | true |
| MSFC-11-disturbed | complete | 1f86dbb83dc1a53239ce2592f6827fa96f359fd4887e1ffb44ed5855e2fc6e9d | 347721439 | true |
| MSFC-12-nominal | complete | 3ab4914d6d2b60db8dfb8577519d52fcc89f340809f6c904a6ef4a6e45453f55 | 344519655 | true |
| MSFC-12-disturbed | complete | e50ff14b97db709b942c4fea89600e5d6270b47a1734f10539ff52429425ce27 | 347636654 | true |
| MSFC-13-nominal | complete | 9e9ed1bb1a075b0956682305306097ac8f8335ab1aa04b390dbd447a8ded575e | 347062655 | true |
| MSFC-13-disturbed | complete | ed2140c7fcb4c8f400def10a596314291af4101a078118b32cc2329875b5e1d9 | 347640437 | true |
| MSFC-14-nominal | complete | 18efe3a1133a949be5097aaeaa4b03a7e3e4a3d9c012f9522a5852c1aebff409 | 344738610 | true |
| MSFC-14-disturbed | complete | b497fc21344683a3e4cce01b8c32f129df11787a5111b8145ece8a1214167a34 | 345933965 | true |
| MSFC-15-nominal | complete | 89abd48b55ab045792d477ecc4ff6de67412898a298707a7d608804ff25c5ef6 | 344657550 | true |
| MSFC-15-disturbed | complete | e8d1af20719f955fdc22c762e2cc163fd67124eba8360e113d96ef64f9659112 | 345730151 | true |
| MSFC-16-nominal | complete | 9731abd13270e530d4854dc5376932e334572da080602ab0d82356066b389941 | 347021882 | true |
| MSFC-16-disturbed | complete | 6e2133dab8e0c2cca205ae62a95156c7987c27517a91c5c0885ca65f72c52b6a | 347602738 | true |
| MSFC-17-nominal | complete | fc61cea69e41337517567ecb12a349d86d63bebc934a55bdcb12edfe2f15918b | 347201590 | true |
| MSFC-17-disturbed | complete | 6481b3700f051ac5f8c1deb398f620d2eec6c0fbdf3263be935cc0dc13f6cb11 | 347688195 | true |
| MSFC-18-nominal | complete | 1d7b2693c7dcd7c6e1f7e9596f10a47e3ccb6f1956703951a6abf2dec08de39e | 344519612 | true |
| MSFC-18-disturbed | complete | 3e42d1e518add0d7594018d51bfb807152687c512a135b4daa5477166de7ec6a | 347609582 | true |
| MSFC-19-nominal | complete | fc1b55c085cbfc42efe6439ecee48e0c6c775aa2567b6ae41b1d0c79df42c494 | 344891964 | true |
| MSFC-19-disturbed | complete | 97816bc66cd95aa0ea8781ca777a775ddf948fbd5f19aafae58f6b67840002f5 | 348003698 | true |
| MSFC-20-nominal | complete | ad8954d5b6a4551a68627859c86f14a5357624408e7a1d7b1bbc7c1483f27650 | 347201592 | true |
| MSFC-20-disturbed | complete | 6a148808819d12e9f98652ca2156b837256c4cb035b1ade0d19665e5fa5adeb6 | 347688195 | true |
| MSFC-21-nominal | complete | 1d0fc68c7969f42bcccfdacbab3caa573fb95aee7dc315682d28583b4c6bb23d | 347201591 | true |
| MSFC-21-disturbed | complete | a82f1a8d45230d670c4d2779357b72f3143f48e8005b51cd560ee023754add11 | 347688195 | true |
| MSFC-22-nominal | complete | 019ffc8f22a61b8f301428a905feed2f44bf442549309aa3b280612edb43afb4 | 347201591 | true |
| MSFC-22-disturbed | complete | 4d4d5ee4f7faa2680bacfb8bd451370d02f51637ae9002401cdd786c49a5f253 | 347688194 | true |
| MSFC-23-nominal | complete | 560e4b856c96f0d830d227000de7b8c573e7f566690ae72b2974ce0387caabee | 347201591 | true |
| MSFC-23-disturbed | complete | 5da191f02a78075efad28d0b12629a0422c0aaf9b8ef6273edf6c67e4657fb1d | 347688193 | true |

## Limits

- No cross-campaign pooling, reserved validation, or physical trial is included.
- EI rows are method-specific training trajectories, not coefficient-matched pairs.
- Plots and tables in this directory are training-only descriptors.

