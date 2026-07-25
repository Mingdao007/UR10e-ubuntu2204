# Step5 Teach Pendant layout

The controller root is `/programs/andyl/kunwei/step5`.

- `step5a/` contains the visible Step5a package.
- `step5b/` contains the visible v3 package; `step5b/archive/` contains v1 and v2.
- `step5c/archive/` contains the quarantined Step5c packages. Nothing in this
  directory is a runnable/current claim.
- `step5d/` contains the requested visible v34 and v35 packages;
  `step5d/archive/` contains superseded, failed, manual, P0, and shadow packages.
- `step5d/broken/` is local-only recovery storage. The v25 controller triplet
  was removed from the TP layout because its `.urp` cachedContents did not match
  its same-basename `.script`; it is not deployable or runnable evidence.
- `step5d/archive/` contains the archived autotune releases r018, r019, and
  r020. `step5d_strict_rnn_autotune_v3_r021` remains the protected/current
  autotune release at the Step5 root; future releases must not displace it
  without an explicit layout update.

Visibility is not live authorization. Existing package lifecycle, contact,
bridge, Load, Play, and ARM gates remain unchanged.

The machine-readable relocation set is
`config/step5/step5_tp_layout.json`. Relocated triplets preserve their
companion `.script` bytes while rebinding `.txt`, `.urp` directory,
Script-node path, and `installationRelativePath` to the destination.
