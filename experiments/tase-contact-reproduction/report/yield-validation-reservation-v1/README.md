# Prospective simulator validation reservation

This reserves six finite condition combinations before any formal tuning or validation run. It is a scientific specification, not an executable or qualified campaign. The original task remains 5 N force regulation, unknown-surface following and compliant attitude on the existing UR10e model.

The six cells span both existing material models, two previously unused curvature pairs, positive/negative 7-degree normal priors, all six disturbance types, and cold/warm startup. All five arms (three tuned methods and two matched ablations) run each cell. This intentionally bounded stress set does not identify independent factorial effects. All cells and failures must appear in the report.

Each cell/arm has its own nominal/disturbed pair at 2 ms/0.25 ms plant, 1 ms/0.25 ms plant, and 2 ms/0.125 ms plant: 60 base trials and 120 separate numerical checks, maximum 180. These are validation costs, not hidden additions to the 24 training pairs per method. No validation trial has run. Identical deterministic repeats do not yield confidence intervals; the physical five-repeat requirement remains outstanding.

The audited retained protocol files contain explicit curvatures (0.8,0.4) and (6,8); this reservation uses (1.2,0.7) and (3,4). The exact audit scope and input hashes are in protocol.json. Novel factors are not independent validation of the simulator's physics. Already viewed seed conditions remain development transfer. Any reserved result used for a design change becomes development for that design.

Before launch, integrate and verify the runner/evaluator, freeze the final code and selected parameters, and bind their identities. Prospective changes must supersede this version before results are viewed. This file does not request new physical workpieces or change the laboratory task; physical scene selection remains tied to the retained platform.
