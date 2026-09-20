# Full-task three-method parameter proposer v1

Implemented and verified software proposer only. No simulator or physical
trial was launched; zero formal tuning or holdout units consumed. The scalar
selection contract, experiment ledger/runner, numerical qualification and
independent holdout remain to be completed before campaign execution.

The config binds complete FT-v1 SFC/DSFC/MSFC parameters. Only m, mu and g
vary: common absolute ranges [4,16], [40,2500], [.005,.2], respectively;
m is sampled linearly, mu and g logarithmically. DSFC/MSFC fixed a=.05,
p=.5 and n=3 match the actual study. MSFC retains its active metric and fixed
memory parameters. These are offline search ranges, not hardware limits.

All methods receive the same eight initial mechanical triples: the three
FT-v1 operating points followed by five shared scrambled Sobol points. The
coefficient-matched DSFC g50 point is therefore evaluated explicitly. Twelve
subsequent slots require actual finite Bayesian EI; unavailable EI raises
rather than masquerading as space filling. Four final slots repeat the best
feasible candidate from the first twenty, without changing the incumbent in
response to repeat outcomes. No feasible incumbent is an explicit failure.

Every row identifies one completed nominal/disturbed pair, an explicit training
split, one training cell and one selection contract. Failed units count and
carry no invented objective. Missing/reordered ordinals, holdout rows, mixed
cells/contracts and contradictory optional pair-member statuses are rejected.
The proposer is stateless: dry proposal generation does not debit a budget.
Pair completion remains a caller declaration; this module is not a trial ledger.

Homepool Grok 4.6 session 01a0bc83-3105-7482-9c99-60394c9ef37c delivered the
implementation without fallback. Native model/session/end_turn and launch
endpoint are verified; high effort is launch/config attestation only. Main
reproduced its 18 tests, demonstrated three input/config acceptance gaps, then
corrected them and added drift rejection for the frozen seed/task bindings.
main-review-before.json preserves the negative findings. main-tests-v2.txt
records 25 passing tests, including the existing six-law tuner regressions
for the shared EI-kernel extraction. All objectives in tests are synthetic.

Use tools/yield_contact_tuner.py::YieldContactTuner with explicit
training_cell_id and selection_contract_id. The config explicitly does not
freeze the whole campaign. Fable discussion in the separate discussion/
directory is advisory work, not this software's acceptance authority.
