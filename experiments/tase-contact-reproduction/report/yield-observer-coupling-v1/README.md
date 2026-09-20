# OC-v1: common-observer coupling diagnostic

This is an exact decomposition of existing DSFC NO-v3 and frozen-observer
matched nominal/intervention data, not a new control design or a new campaign.
All six source receipts are hash-verified; see results.json. The reconstruction
checks every sample and separates measured-force, target-normal,
path-displacement and path-projection differences. RMS terms can cancel and
must not be added as independent effects.

After tangent release, NO-v3 has 0.350 N RMS target-normal difference and
0.427 N path-displacement difference, while total law-input difference is
0.104 N. The observed normal estimate changes both force reference direction
and the tangential task projection. This prevents interpreting aggregate
velocity/input alone as evidence that the observer is unimportant.

A physical limitation remains: the implemented wrist force feedback contains
contact plus modeled external force. The simulator's normal perturbation is
outward; at quasi-static balance, maintaining total sensed normal force does
not imply maintaining the same true workpiece contact load. True contact load
is evaluator-only. No decomposition here identifies human force online or
claims exact 5 N contact during arbitrary external intervention.

Reproduce with tools/analyze_yield_observer_coupling_v1.py. No robot endpoints,
new gates, task clock changes or state resets are used.
