# Full-period execution seam v1

Development integration, inactive route; no robot motion, transport qualification or physical acceptance claimed.

The existing CanonicalQualificationControl now consumes the shared provider with a 1 s entry followed by the complete 62.831853 s formal path. Provider state is snapshotted and restored if subsequent qualification gates reject a computed command. A full native MSFC fixture exercises 500 entry and 31416 formal ticks with measured-observation inputs. Its stationary robot fixture validates software execution, not closed-loop performance.

The existing sole-writer packet history now binds published reference phase and time to consumed packet sequence. Formal evidence excludes entry and requires all 629 time bins and the strict physical RTDE/TP endpoint proof. The final received frame may consume the last valid PATH command after host generation ends; it is retained by consumed reference identity rather than discarded by the host generation clock. A real execute_attempt loop with in-memory transport and stub control verifies this timing path. No second command channel was added.

Full-period filtered normal-force MAE/RMSE are time weighted over formal samples. The legacy first-550-bin DTO remains for compatibility and its metric is separately labeled. These measured force projections do not identify human force or true contact normal independently.

A full-cycle test exposed numerical overshoot at the strict Cartesian cap. Shared pre-QP caps now reserve 2*sqrt(3)*1e-6 Cartesian and 2e-7 joint margins against unchanged solver validation tolerances. Requested and guarded settings are recorded in runtime identity. No gate tolerance was relaxed and no post-hoc command scaling was added. Earlier simulation receipts do not automatically validate this runtime configuration.

Validation: contact-regression-tests.txt records 188 passing tests, including full native execution, provider rollback, coverage, real writer-loop fixtures, numerical/controller tests, evidence ledger and motion profiles. Earlier failed attempts and their repairs remain in this directory. final-boundary-tests.txt records the targeted entry/end-fence checks. The baseline ef55d7f9 also fails the two historical live-boundary tests in legacy-baseline-tests.txt (obsolete three-qualification expectation and force-only evidence fixture). The repaired frozen-clock/fake poll fixtures reveal a further stale ordinal-four expectation; these are not evidence of production qualification. No claim that the entire historical live-boundary suite passes.

Remaining: full measured execution latency against the 2 ms budget; active-route identity/profile admission, transport qualification and physical servo/contact identification; complete measured task evidence and original-platform pilot. Shared normal-estimation/friction failures remain unresolved and are documented in yield-normal-v2. No formal holdout or winning proposal has been declared.
