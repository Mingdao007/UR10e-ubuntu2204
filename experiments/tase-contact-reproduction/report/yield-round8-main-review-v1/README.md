# Round-8 numerical verification

Main independently reran the advisory extractor on all 25 fixed-snapshot raw artifacts. Every artifact hash matched its snapshot, all extracted arrays were exactly equal (including NaNs), and all metadata agreed except elapsed extraction time. Both analysis scripts were independently executed; their JSON outputs exactly matched the advisory outputs.

This verifies reproducibility of the supplied post-processing, not independent mathematical correctness of every diagnostic or acceptance of the unfinished advisory interpretation. The source hashes bind this check to the inspected versions. The Fable discussion remained active at this checkpoint; its prose is not yet adjudicated. No simulation, campaign decision, validation cell, or hardware action was executed by these checks.

Reproduction: from the experiment root, run verify_cache.py with the experiment Python environment; it uses the fixed round-8 snapshot and extractor, writes a separate /tmp/yfp8-main cache, and compares against /tmp/yfp8. If that advisory cache is absent, regenerate it with the bound extractor first. Execute both bound analysis scripts and compare their JSON to the advisory outputs.

## Final delivery

The advisory process subsequently exited with code 0. Main verified all 62 raw/input/output manifest entries, 26 bound files and the canonical campaign-protocol digest; see final-delivery-verification.json. The previously reproduced numerical scripts and results were unchanged. main-adjudication.md records accepted findings and rejected interpretations. No second iteration was needed.

Provenance caveat: prose that names 4cd78c5c as HEAD at receipt time is stale; the machine receipt records a812e074. The fixed 25-attempt snapshot is the scientific input scope. Hashes generated at receipt time prove file identity at that time, not when an advisor first read each file. This matters for the concurrently updated progress.json; main does not use it to expand the fixed-snapshot analysis.
