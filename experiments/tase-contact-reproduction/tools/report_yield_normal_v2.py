"""Retain all four observer-development cells and explicit coupled costs."""
import argparse,json,hashlib
from pathlib import Path
import numpy as np
from run_contact_yield import read
ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--forceoff',type=Path);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    baseline=read(ROOT/'runs/yield-normal-v2/protocol.json')['retained_baseline']
    variants={'legacy':baseline,'NO-v2':read(ROOT/'runs/yield-normal-v2/summary.json')['runs']}
    if a.forceoff:variants['NO-forceoff-v1']=read(a.forceoff/'summary.json')['runs']
    rows=[]
    for name,cells in variants.items():
        for cell in cells:
            path=Path(cell['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==cell['sha256']
            r=read(path)
            rows.append({'variant':name,'material':cell['material'],'scenario':cell['scenario'],
                'path':str(path),'sha256':cell['sha256'],'full_cycle':r['full_cycle'],
                'normal_rms_deg':float(np.rad2deg(np.sqrt(np.mean([x['normal_estimation_error_rad']**2 for x in r['rows']])))),
                'orientation_rms_deg':float(np.rad2deg(np.sqrt(np.mean([x['orientation_error_rad']**2 for x in r['rows']])))),
                'metrics':r['metrics']})
            del r
    (a.output/'comparison.json').write_text(json.dumps(rows,indent=2)+'\n')
    lines=['# Common observer development: all cells retained','','All data are development, with fixed DSFC mechanics and common constraints. No physical or winner claim.','',
        '| Observer | Material | Scenario | Normal RMS deg | Attitude RMS deg | Force MAE N | Peak N | Path RMS mm | Contact loss s | Failed |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---|']
    for r in rows:
        m=r['metrics'];lines.append(f"| {r['variant']} | {r['material']} | {r['scenario']} | {r['normal_rms_deg']:.3f} | {r['orientation_rms_deg']:.3f} | {m['force_mae_n']:.3f} | {m['force_peak_n']:.3f} | {1000*m['path_rmse_m']:.3f} | {m['contact_loss_duration_s']:.3f} | {m['failed']} |")
    (a.output/'results.md').write_text('\n'.join(lines)+'\n')
if __name__=='__main__':main()
