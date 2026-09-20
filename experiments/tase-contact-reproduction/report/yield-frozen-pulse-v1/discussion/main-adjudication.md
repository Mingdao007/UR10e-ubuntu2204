# Main adjudication of round 5

Development evidence only. Accept the recommendation to stop unstructured
metric-parameter iteration for the current MSFC candidate and move toward the
already authorized bounded fair comparison with shared adaptive perception.
No formal tuning or holdout budget was consumed by this discussion.

## Accepted evidence and limits

- Native Fable 5.1 session and end_turn verified; xhigh is launch attestation,
  not independently observed server effort. All 39 raw receipt hashes, 58 report
  inputs, seven tool inputs, four native sources and six delivered outputs
  matched. Rerunning its analysis against retained extracted arrays produced
  exactly the delivered JSON. See main-verification.json.
- The identity-metric ablation preserves evolving memory states but removes
  their mechanical effect. It is a DSFC-form mechanical law using MSFC's
  coefficients. Coefficient-matched DSFC must therefore be available within the
  fair search; current seed-to-seed advantages cannot be attributed to memory.
- Frozen-observer results isolate mechanisms; they do not satisfy the unknown
  surface task. Retain one versioned adaptive observer for every primary arm.
- Pulse recovery is sensitive to the fixed force band. Keep the preregistered
  metric unchanged and add physically defined supplementary load/envelope
  descriptors. Do not optimize a favourable band or redefine past results.
- Current memory-on pulse peaks exceed identity by about 0.15 N at both sampled
  steps. Existing results justify stopping this candidate's ad hoc metric
  tuning, not a theorem that all memory designs or all gains are ineffective.

## Corrections to advisory wording and analysis

1. Finite equal-budget tuning can support 'no resolved benefit in the tested
   parameter range and scenarios'. It cannot establish 'no measurable role at
   any gain', global equivalence, or that MSFC is merely DSFC in all conditions.
   The identity ablation is structurally DSFC-form; active MSFC remains a
   different dynamical system even if its measured benefit is absent.
2. The original envelope uses 250 samples: 0.5 s at 2 ms but 0.25 s at 1 ms.
   Preserve that original output and use scripts/main_envelope_check.py for a
   fixed 0.5 s window. In the 1 ms pair, all stay-below times increase by 0.25 s;
   active/identity fitted tau changes from 1.254/1.235 s to 1.277/1.236 s.
   Original force peaks, recovery, integrated-load metrics and signs are
   unchanged. These fitted taus remain descriptive, not system identification.
3. Refining plant substeps at fixed controller dt is not interchangeable with
   refining controller dt. Declare these two checks separately. A numerical
   acceptance threshold must be fixed before candidate evaluation and cannot
   be chosen merely to pass the observed 0.12--0.13 N difference.
4. Wrist/contact force separation remains limited during intervention. The
   simulated force sum explains this particular response; it is not proof that
   these peaks are unavoidable under every shared controller. Friction-bound
   residuals can be diagnostics with a declared mu bound, not human-force
   estimates or universal safety evidence. An observer's conservative friction
   bound need not equal the plant's nominal coefficient.
5. 'Recovery force band' means the absolute load difference from the matched
   nominal trajectory, not absolute error from the 5 N target. This is already
   the implemented metric. No metric definition is changed by this clarification.

## Next bounded work

Finish actual full-cycle writer boundary repair and native route model binding
independently of this scientific decision. Freeze a fair-search protocol with
DSFC's ranges covering the existing MSFC coefficient point, identical shared
modules, explicit pair/scenario accounting, separate numerical checks and
holdout. The old six-controller 256-tick campaign is not the full-task runner
and must not be counted as satisfying this comparison. No expansion to a new
controller or robot platform is required. Retain failures and unresolved common
perception limitations in the final contribution judgement.
