# NO-v3: combined motion and force-motion coplanarity observer

Development only. Fixed DSFC candidate, stiff_low_mu, 2 ms controller period,
8 plant substeps, 1 s entry and the complete 62.831853 s reference period.
No default promotion, retuning, physical claim or holdout use.

With pre-update inward unit normal n, P = I - n n^T, measured base-frame
velocity v and force f, define c = f cross v. The proposed update is

    g_motion = 0.3 (n dot v) P v / max(v dot v, 0.002^2)
    g_cp = 0.3 (n dot chat) P chat, chat = c / norm(c)
    g = g_motion + g_cp
    n_next = normalize(n - dt * rate_cap(g, 0.05))

The CP term is zero if norm(c) <= 1e-9 N m/s. This numerical degeneracy
threshold is not a sensor noise model or a robustness guarantee. Both terms
use the same pre-update normal and the existing contact/excitation gates;
the rate cap is applied once to the sum. The legacy force-direction bias is
disabled. No residual cone gate, intervention reset or geometry truth input
is added. The zero CP gain retains the existing estimator identity/arithmetic.

Nine new cells, all reported:

1. Mild surface, approach prior, combined observer, nominal.
2. Mild surface, initial feed-direction 10 degree prior, combined, nominal.
3. Mild surface, across-feed 10 degree prior, combined, nominal.
4. Strong curvature (6, 8) / m, approach prior, combined, nominal.
5. Mild surface, approach prior, combined, normal hold/release.
6. Mild surface, approach prior, combined, tangent hold/release.
7. Mild surface, across-feed prior, motion-only ablation, nominal.
8. Mild surface, approach prior, frozen observer, normal hold/release.
9. Mild surface, approach prior, frozen observer, tangent hold/release.

Retain the three stiff P0-v1 frozen-prior nominal trials and the stiff SE-v1
strong-curvature frozen-prior trial. Verify raw receipt hashes and mechanical,
reference and initial-state invariants before interpreting comparisons.
Initial estimator error and PATH-start error are separate: entry already
allows estimator updates. Record contact/excitation gate fractions and tail
error, alongside contact force, path, attitude, measured progress, saturation,
contact loss and matched nominal recovery. A failed or shortened attempt is
retained, not silently replaced. Missing tail data is null.

The first matrix deliberately does not claim compliant-material transfer or
actual cross-slide intervention robustness. Reference-tangent forcing need
not align with actual sliding. The ideal Coulomb contact model makes the
coplanarity assumption favorable; filtered transients and added external force
can still violate it. The model has no orientation-dependent contact patch.
Any useful result needs subsequent numerical checks and bounded transfer.
