# UR Force/Frame Contract

This contract is the canonical sign and frame source for UR contact-control
work in this experiment. It is a controller-design gate, not a report note.

## Current Bench Convention

- The force/torque signal is the wrench applied by the environment on the tool.
- In the current tabletop-contact posture, TCP `+Z` points approximately into
  the surface. Pressing downward makes the environment reaction point upward,
  so the TCP-frame normal force component is negative.
- Positive `normal_load_n` is a derived contact load, not a raw sensor-axis
  value.

## Required Terms

- `reaction_normal`: unit direction of the environment reaction force.
- `approach_normal`: unit direction used to press into the surface.
- `control_reaction_normal`: latched/filtered reaction normal used by the
  controller.
- `normal_load_n`: `dot(force_base, control_reaction_normal)`.
- `force_error_n`: `target_load_n - normal_load_n`.
- `orientation_target_axis`: the axis the tool TCP `+Z` should align with.

`approach_normal = -reaction_normal`. New contact-control code must not use a
bare `normal` name when the distinction matters.

## Controller Contract

- Use `reaction_normal` to compute positive load.
- Use `approach_normal` when a low-load controller must press into the surface.
- Use `reaction_normal` when a high-load controller must unload.
- Use `orientation_target_axis = approach_normal` for the current tool posture
  unless a stage-specific contract explicitly says otherwise.
- Do not build `orientation_target_axis` or `R_d.z` by normalizing raw live
  force. Use the latched/filtered normal from the contact-search path.
- If a controller differs from Step5b/Step6b semantics, it must name the
  difference and pass a replay artifact before live contact.

## Mandatory Gates

Before any new or changed live contact controller can be uploaded or run:

- Static check: no raw-force-normalized orientation target.
- Unit check: low force presses along `approach_normal`, high force unloads
  along `reaction_normal`.
- Replay check: historical bridge CSV rows do not show a 25.15 to 25.0
  orientation-sign jump.
- Runtime check: if contact-search orientation error and outer-loop orientation
  error disagree by more than the configured tolerance, command validity must
  hard-fail before a speed command is emitted.

Package read-back proves controller files were delivered. This contract proves
the delivered controller has not inverted force/frame semantics.
