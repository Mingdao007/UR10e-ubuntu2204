# OC-v1: observed coupling into the common law input

For disturbed d and matched nominal 0, P = I - nn^T and e = x - x_ref. With the recorded zero integral/compliance stiffness, the exact difference is:

    du = (fd-f0) + F*(nd-n0) - K*P0*(ed-e0) - K*(Pd-P0)*ed

Every sample is reconstructed within 1e-11 N. These terms are coupled observations, not independently intervened causal effects. RMS magnitudes do not add; cancellation can occur. The counterfactual replacement of a normal estimate is NOT executed.

| Observer | Direction | Window | Total input RMS N | Measured force term N | Target-normal term N | Path-displacement term N | Path-projection term N | Feedforward delta mm/s |
|---|---|---|---:|---:|---:|---:|---:|---:|
| combined | normal | intervention | 0.8134 | 0.8580 | 0.1523 | 0.4103 | 0.0038 | 0.0833 |
| combined | normal | post_release | 0.2085 | 0.2101 | 0.0878 | 0.0906 | 0.0054 | 0.0272 |
| combined | tangent | intervention | 0.5272 | 2.3139 | 0.8385 | 1.8593 | 0.1083 | 0.4342 |
| combined | tangent | post_release | 0.1044 | 0.1368 | 0.3495 | 0.4273 | 0.0920 | 0.1726 |
| frozen | normal | intervention | 0.5931 | 0.6537 | 0.0000 | 0.2870 | 0.0000 | 0.0000 |
| frozen | normal | post_release | 0.0958 | 0.0959 | 0.0000 | 0.0055 | 0.0000 | 0.0000 |
| frozen | tangent | intervention | 0.5171 | 2.2106 | 0.0000 | 2.1516 | 0.0000 | 0.0000 |
| frozen | tangent | post_release | 0.0191 | 0.0916 | 0.0000 | 0.0821 | 0.0000 | 0.0000 |
