"""Report all frozen-prior cells; never interpret fixed-normal success as identification."""
import argparse,json,hashlib
from pathlib import Path
from run_contact_yield import read,write
ROOT=Path(__file__).resolve().parents[1]
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    summary=read(a.input/'summary.json');rows=summary['runs']
    assert len(rows)==6
    for row in rows:assert hashlib.sha256(Path(row['path']).read_bytes()).hexdigest()==row['sha256']
    a.output.mkdir(parents=True,exist_ok=True)
    table=['| Material | Prior | Full cycle | Normal RMS deg | Attitude RMS deg | Force MAE N | Peak N | Path RMS mm | Progress ratio | Contact loss s |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in rows:
        m=r['metrics'];table.append(f"| {r['material']} | {r['prior']} | {r['full_cycle']} | {r['normal_rms_deg']:.3f} | {r['orientation_rms_deg']:.3f} | {m['force_mae_n']:.3f} | {m['force_peak_n']:.3f} | {1000*m['path_rmse_m']:.3f} | {m['progress_ratio']:.3f} | {m['contact_loss_duration_s']:.3f} |")
    # All quantitative metrics are preserved, not merely selected table fields.
    write(a.output/'prior-results.json',summary)
    text='''# P0-v1: independent observer prior uncertainty

Six development-only nominal full-cycle trials, fixed DSFC candidate and shared plant/controller settings. The physical approach, robot initial configuration, surface, reference path, force target and orientation-compliance law are unchanged between priors. Only estimator initialization differs; motion_gain=0 and force_correction_gain=0 explicitly freeze its state.

The two 10-degree errors point respectively along the projected initial feed direction and across it, in the physical tangent plane. Their input is defined from the known approach and task reference, not simulator surface truth. Full initial and evolving state and raw observations are retained for every cell. No reinitialization during contact and no retuning.

'''+ '\n'.join(table)+'''

This is a prior-sensitivity baseline, not a proposed solution for unknown surfaces. Small error from the approach prior cannot demonstrate online identification. Conversely a worse tilted-prior result does not prove that a particular adaptive estimator can correct it. All cells are development; none is an independent holdout. The nominal-only matrix cannot establish intervention robustness or large varying-normal tracking.

Full metrics (including saturation, QP intervention, force-limit duration and failure reasons) and raw receipt hashes are in prior-results.json. Force diagnostic limits are simulator reporting thresholds, not fragile-object damage limits. The model's joint-rate clip, friction and servo assumptions remain unchanged and unvalidated physically.
'''
    with (a.output/'prior-results.md').open('x') as f:f.write(text)
if __name__=='__main__':main()
