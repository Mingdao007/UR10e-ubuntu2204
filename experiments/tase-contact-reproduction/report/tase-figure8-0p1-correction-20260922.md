# Correction: 0.1 N s Figure-eight trial

The run directory and raw receipts are preserved. The parameter file declared
`force_integral_limit_n_s=0.1`, but the live runtime constructor retained its
default `1.0 N s` and reapplied that value on each control tick. The sealed
runtime state reached `0.996890205607247 N s`.

Therefore the run did complete a 60 s `TASE_RNN_MATURE` Figure-eight with
`Md=12`, `Bd=550`, but it is **not evidence for the 0.1 N s condition**. Its
`normal_force_mae_n=1.8391669426132957` remains a valid result for the actual
runtime condition and is excluded from any 0.1 comparison. The parameter
propagation repair is covered by the focused runtime tests; a new live 0.1 run
is required before making a 0.1 performance claim.
