"""Reproduce the TR-v1 development comparison without declaring a winner."""
import argparse
import json
from pathlib import Path
import shutil


def fmt(value,scale=1):
    return '-' if value is None else f'{scale*value:.4f}'


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runs',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    x=json.loads((a.runs/'summary.json').read_text())
    for name in ('protocol.json','summary.json','manifest.json'):shutil.copy2(a.runs/name,a.output/name)
    lines=['# TR-v1 development results','','Frozen parameters; common outer and constraints; no retuning or final holdout.',
        'All scenarios are retained, including failures. Progress is measured projection, not a completion certificate.','',
        '| Variant | Material | Scenario | Force MAE N | Peak N | Path RMS mm | Orientation RMS deg | Progress | Contact loss s | Recovery s |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in x['runs']:
        m=r['metrics'];q=r.get('pair',{})
        recovery='censored' if q.get('right_censored') else fmt(q.get('recovery_s'))
        lines.append('| '+' | '.join([r['variant'],r['material'],r['scenario'],fmt(m['force_mae_n']),fmt(m['force_peak_n']),
            fmt(m['path_rmse_m'],1000),fmt(m['orientation_rmse_rad'],180/3.141592653589793),fmt(m['progress_ratio']),
            fmt(m['contact_loss_duration_s']),recovery])+' |')
    on='MSFC-GM-v1-g50-on';off='MSFC-GM-v1-g50-identity_metric'
    index={(r['variant'],r['material'],r['scenario']):r for r in x['runs']}
    lines+=['','## Mechanically matched memory ablation','','All deltas are ON minus identity metric. Negative is lower, not automatically better yielding.',
            '', '| Material | Scenario | Delta peak N | Delta path RMS mm | Delta recovery s | Delta yielding mm |',
            '|---|---|---:|---:|---:|---:|']
    changes=[]
    for material in ('stiff_low_mu','compliant_high_mu'):
        for scenario in ('nominal','sustained_release_normal','sustained_release_tangent','short_pulse_oblique'):
            b=index[on,material,scenario];c=index[off,material,scenario]
            row={'material':material,'scenario':scenario,'force_peak_delta_n':b['metrics']['force_peak_n']-c['metrics']['force_peak_n'],
                 'path_rms_delta_m':b['metrics']['path_rmse_m']-c['metrics']['path_rmse_m']}
            for key in ('recovery_s','yield_peak_m'):
                bv=b.get('pair',{}).get(key);cv=c.get('pair',{}).get(key)
                row[key+'_delta']=None if bv is None or cv is None else bv-cv
            changes.append(row)
            lines.append(f"| {material} | {scenario} | {fmt(row['force_peak_delta_n'])} | {fmt(row['path_rms_delta_m'],1000)} | {fmt(row['recovery_s_delta'])} | {fmt(row['yield_peak_m_delta'],1000)} |")
    (a.output/'results.md').write_text('\n'.join(lines)+'\n')
    (a.output/'memory-deltas.json').write_text(json.dumps(changes,indent=2)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    labels=['SFC','DSFC','MSFC g50','MSFC g50 identity']
    variants=['SFC','DSFC',on,off]
    scenarios=['sustained_release_normal','sustained_release_tangent','short_pulse_oblique']
    fig,axes=plt.subplots(2,2,figsize=(11,8),constrained_layout=True)
    for i,material in enumerate(('stiff_low_mu','compliant_high_mu')):
        for j,(field,title) in enumerate((('force_peak_n','Contact peak N'),('recovery_s','Recovery s'))):
            ax=axes[i,j]
            for k,variant in enumerate(variants):
                rs=[index[variant,material,s] for s in scenarios]
                vals=[r['metrics'][field] if j==0 else r['pair'].get(field) for r in rs]
                ax.bar(np.arange(3)+(k-1.5)*.2,[np.nan if v is None else v for v in vals],.19,label=labels[k])
            ax.set_xticks(np.arange(3),['Normal hold','Tangent hold','Oblique pulse'],fontsize=9)
            ax.set_title(material+' / '+title,fontsize=10);ax.grid(axis='y',alpha=.2)
    axes[0,0].legend(fontsize=8,ncol=2)
    fig.suptitle('TR-v1: development transfer, frozen parameters\nNo formal tuning, uncertainty interval or winner claim',fontsize=13)
    fig.savefig(a.output/'transfer.png',dpi=160);fig.savefig(a.output/'transfer.pdf')


if __name__=='__main__':main()
