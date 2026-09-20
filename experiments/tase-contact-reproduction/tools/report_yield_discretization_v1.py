"""DC-v1 two-factor numerical sensitivity and contact-metric semantics."""
import argparse,hashlib,shutil
from pathlib import Path
from run_contact_yield import read,write

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 rows=read(a.input/'summary.json')['runs'];assert len(rows)==4
 for r in rows:assert hashlib.sha256(Path(r['path']).read_bytes()).hexdigest()==r['sha256']
 index={(r['dt_s'],r['plant_dt_s']):r for r in rows};edges=[]
 for field in ('contact_loss_duration_s','force_mae_n','force_peak_n','path_rmse_m','progress_ratio'):
  at=lambda d,h:index[d,h]['metrics'][field]
  edges.append({'metric':field,'plant_step_halved_at_2ms':at(.002,.000125)-at(.002,.00025),'plant_step_halved_at_1ms':at(.001,.000125)-at(.001,.00025),
   'controller_dt_halved_at_025ms_plant':at(.001,.00025)-at(.002,.00025),'controller_dt_halved_at_0125ms_plant':at(.001,.000125)-at(.002,.000125)})
 write(a.output/'results.json',{'runs':rows,'finite_differences':edges,'claim':'two-factor numerical sensitivity, not convergence order or physical accuracy'})
 for name in ('protocol.json','manifest.json','start.json'):shutil.copy2(a.input/name,a.output/name)
 lines=['# DC-v1: separate control and plant discretization','','All four cells use the same SFC parameters, NO-v3 observer and normal intervention. Two cells are retained and two are new.','',
 '| Control dt ms | Plant step ms | Min load N | Load <1 N s | Zero load s | Geometric separation s | Force MAE N | Peak N | Path RMS mm | Progress | Saturation s | QP intervention s |',
 '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
 for r in sorted(rows,key=lambda r:(-r['dt_s'],-r['plant_dt_s'])):
  m=r['metrics'];vals=[r['dt_s']*1000,r['plant_dt_s']*1000,r['min_contact_load_n'],m['contact_loss_duration_s'],r['zero_load_duration_s'],r['geometric_separation_duration_s'],m['force_mae_n'],m['force_peak_n'],m['path_rmse_m']*1000,m['progress_ratio'],m['saturation_ticks']*r['dt_s'],m['qp_intervention_ticks']*r['dt_s']]
  lines.append('| '+' | '.join('NA' if v is None else f'{v:.4f}' for v in vals)+' |')
 lines+=['','`contact_loss_duration_s` is the unchanged historical <1 N threshold metric. It is not physical separation. Zero force and nonnegative surface gap are separately evaluated. Saturation and QP exposure are converted from ticks to seconds to compare grids. All failures and original metrics remain in results.json. Two levels do not establish convergence order; no controller ranking or physical acceptance follows.']
 (a.output/'results.md').write_text('\n'.join(lines)+'\n')
if __name__=='__main__':main()
