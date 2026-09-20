"""FT-v1 three-observer comparison without final controller ranking."""
import argparse,json
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--report',type=Path,required=True);a=p.parse_args();rows=json.loads((a.report/'results.json').read_text())['rows']
 index={(r['method'],r['observer'],r['scenario']):r for r in rows};methods=('SFC','DSFC','MSFC');observers=('legacy','NO-v3','frozen')
 panels=[('nominal','force_mae_n','Nominal force MAE (N)',1),('nominal','path_rmse_m','Nominal path RMS (mm)',1000),('nominal','progress_ratio','Nominal measured progress',1),
 ('sustained_release_tangent','yield_peak_m','Tangent yielding (mm)',1000),('sustained_release_tangent','recovery_s','Tangent recovery (s)',1),('sustained_release_tangent','force_peak_n','Tangent contact peak (N)',1)]
 fig,axes=plt.subplots(2,3,figsize=(12,7),constrained_layout=True)
 for ax,(scenario,field,title,scale) in zip(axes.flat,panels):
  for k,observer in enumerate(observers):
   vals=[]
   for method in methods:
    r=index[method,observer,scenario];v=(r.get('pair',{}).get(field) if field in ('yield_peak_m','recovery_s') else r['metrics'][field]);vals.append(np.nan if v is None else v*scale)
   ax.bar(np.arange(3)+(k-1)*.25,vals,.25,label=observer,color=('#90959a','#2878a0','#b66c32')[k])
  ax.set_xticks(np.arange(3),['SFC','DSFC','MSFC g50']);ax.set_title(title,fontsize=11);ax.grid(axis='y',alpha=.2)
 axes[0,0].legend(fontsize=9)
 fig.suptitle('FT-v1: frozen observer isolates adaptation as an experimental factor\nDevelopment only; fixed candidate parameters, no equal-budget optimization. Frozen is an ablation.',fontsize=12)
 for ext in ('png','pdf','svg'):fig.savefig(a.report/f'comparison.{ext}',dpi=160)
 svg=a.report/'comparison.svg';svg.write_text('\n'.join(x.rstrip() for x in svg.read_text().splitlines())+'\n')
if __name__=='__main__':main()
