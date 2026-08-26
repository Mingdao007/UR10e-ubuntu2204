# Kunwei payload-plane differential validation

## Objective

Validate the mass seen through the Kunwei sensing plane without comparing the
Kunwei fit directly with the whole-assembly scale total.

## Fixed baseline

- Unloaded analysis:
  `custom_payload_cog_analysis.json`
- Current RG2 state: wired, open, no contact.
- Measurement plane assumption:
  `UR flange -> Kunwei sensor -> QC/RG2 downstream stack`.
- Old Autotuner white adapter (`IMG_1688`) is excluded.
- Existing UR payload/CoG/TCP are not changed.

## Loaded run

1. Select a rigid reference mass whose scale reading and repeatability are
   recorded in the run metadata.
2. Attach it below the Kunwei sensing plane, with no contact or cable tension.
3. Run the same eight static poses with:
   `capture_kunwei_payload_cog_calibration.py --condition-label loaded_reference --known-added-mass-kg <mass_kg>`.
4. Keep the original unloaded run unchanged.

## Differential analysis

```bash
python3 tools/analyze_custom_payload_cog.py <loaded_run_dir> \
  --builtin-payload-kg 1.55 --builtin-cog-mm 1 13 51

python3 tools/analyze_payload_delta.py \
  <unloaded_run>/custom_payload_cog_analysis.json \
  <loaded_run>/custom_payload_cog_analysis.json \
  --known-added-mass-kg <mass_kg> \
  --mass-tolerance-kg 0.02
```

The differential result is `agree` only when the fitted added mass is within
the declared tolerance, pose pairing is valid, mapping is consistent, and the
design matrices remain full rank. A missing reference mass remains
`pending_reference_mass`; neither result authorizes a controller write.
