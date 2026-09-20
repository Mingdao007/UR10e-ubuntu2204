# Native warm-history audit

This is a development prescribed-input check of the unchanged active MSFC g50 law, not a closed-loop task or a memory-benefit experiment. No training or holdout units were used and no endpoint was accessed.

Twelve histories combine cold, 2 s of 3 N along base x, and 2 s along base y with zero-input gaps of 0, 0.2, 0.6, and 2 s. Each is followed by the same 0.2 s oblique probe and 0.8 s release. Every complete 22-slot initial snapshot is restored in a fresh native instance. All 500 subsequent commands and snapshots match exactly in every case. Raw snapshots, commands, preparation, probe, parameters and native identity are retained in the hashed gzip artifact named in result.json.

The warm-x structure norm drops from 0.62967 to 0.06122 after 0.6 s: 9.72 percent remains, confirming the round6 coupled-recurrence correction. After 2 s the structure is small (6.33e-5), but warm versus cold command differences still reach 0.002447 m/s during the probe. The retained velocity differs too; this comparison does NOT isolate memory causality. Direction-conditioned differences likewise include velocity history. Stationary warm preparation is not a substitute for complete-state replay or a controlled memory ablation.

Actual pre-probe velocity-state slots for warm-x (not scaled output command):

- gap 0 s: [0.20121384824285457, 0.0, 0.0]
- gap 0.2 s: [0.11912198949370112, 0.0, 0.0]
- gap 0.6 s: [0.06302583280112373, 0.0, 0.0]
- gap 2 s: [0.028530758989367334, 0.0, 0.0]

Reproduce from the experiment root with `.venv-contact-six/bin/python report/yield-warm-memory-v1/check.py > new-result.json`; script builds/uses the existing native law only. The raw artifact is local evidence, not tracked large data. The result records its absolute path and SHA-256.
