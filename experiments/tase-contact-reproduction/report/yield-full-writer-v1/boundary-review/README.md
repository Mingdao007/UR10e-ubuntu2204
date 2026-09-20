# Main review of in-progress boundary repair

These are read-only checks of the isolated writer worktree at the bound file
hashes, before integration. They are not accepted production changes or proof
that the writer's final delivery has the same defects.

1. Protocol identity: old and draft periodic-continuation versions currently
   emit the same protocol SHA-256. The runtime identity also lacks the new
   continuation policy. Explicit behavior version binding is required before
   acceptance; old snapshots must not silently acquire a new endpoint policy.
2. Reference span: an adversarial collector stream starts its reference at
   0.002 s, advances monotonically, but loses a total 0.002 s of reference
   progress. Physical coverage is 62.832 s; reference span including the final
   coverage interval is only 62.830 s. The draft collector accepts it. Full
   physical duration alone is not full reference traversal. This stream is not
   emitted by the actual runtime (which separately checks dt); the collector's
   endpoint proof should nevertheless require last-first+interval >= period.

Existing zero-clock tests and the original strict rejection remain retained.
Main will integrate only after these narrow findings and the complete writer
cycle tests are addressed. No duration threshold, reference timestamp or input
freshness rule should be relaxed to make the checks pass.
