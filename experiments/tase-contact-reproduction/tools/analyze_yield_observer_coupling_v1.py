"""Exact same-time law-input decomposition; observational, not causal intervention."""
import argparse,hashlib,gc
from pathlib import Path
import numpy as np
from run_contact_yield import read,write

def load(row):
 p=Path(row['path']);assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'];return read(p)
def rms(a):return float(np.sqrt(np.mean(np.sum(a*a,axis=1)))) if len(a) else None
def arrays(r):
 rows=[x for x in r['records'] if x['reference']['phase']=='path']
 return {k:np.asarray([x['result'][field] for x in rows]) for k,field in {
 't':'path_time_s','n':'inward_normal_base','f':'filtered_force_base_n','u':'law_force_base_n',
 'e':'path_error_base_m','vref':'reference_velocity_base_m_s'}.items()}
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 report=read(a.input/'results.json');index={r['id']:r for r in report['rows']};results=[];sources={}
 for variant,base in [('combined','combined-mild-approach'),('frozen','retained-approach')]:
  row=index[base];nom=load(row);sources[row['path']]=row['sha256'];na=arrays(nom)
  settings=nom['identity_payload']['settings'];assert settings['integral_force_gain']==0 and settings['compliance_stiffness_n_per_m']==0
  k=settings['path_stiffness_n_per_m'];target=settings['target_force_n'];del nom;gc.collect()
  pn=np.eye(3)[None,:,:]-na['n'][:,:,None]*na['n'][:,None,:]
  for direction in ('normal','tangent'):
   row=index[f'{variant}-{direction}-hold'];r=load(row);sources[row['path']]=row['sha256'];da=arrays(r)
   assert r['identity_payload']['settings']==settings and np.array_equal(na['t'],da['t']) and np.array_equal(na['vref'],da['vref'])
   pd=np.eye(3)[None,:,:]-da['n'][:,:,None]*da['n'][:,None,:]
   mv=lambda mat,v:np.einsum('nij,nj->ni',mat,v)
   terms={'measured_force':da['f']-na['f'],'target_normal':target*(da['n']-na['n']),
    'path_displacement':-k*mv(pn,da['e']-na['e']),'path_projection':-k*mv(pd-pn,da['e'])}
   delta=da['u']-na['u'];error=np.max(np.abs(sum(terms.values())-delta));assert error<1e-11,error
   feed=mv(pd-pn,na['vref']);windows={}
   for name,lo,hi in [('before',0.,20.),('intervention',20.,35.5),('post_release',35.5,62.832),('tail',52.832,62.832)]:
    mask=(na['t']>=lo)&(na['t']<hi)
    windows[name]={'samples':int(sum(mask)),'total_law_input_delta_rms_n':rms(delta[mask]),
      'term_rms_n':{key:rms(val[mask]) for key,val in terms.items()},'feedforward_delta_rms_m_s':rms(feed[mask]),
      'normal_difference_rms_deg':float(np.sqrt(np.mean(np.rad2deg(np.arccos(np.clip(np.sum(na['n'][mask]*da['n'][mask],axis=1),-1,1)))**2)))}
   results.append({'variant':variant,'direction':direction,'reconstruction_max_abs_n':float(error),'windows':windows,'pair':row['pair']})
   del r;gc.collect()
 write(a.output/'results.json',{'results':results,'sources':sources,'claim':'exact observed input decomposition, not counterfactual closed-loop causality','live_executed':False})
 lines=['# OC-v1: observed coupling into the common law input','','For disturbed d and matched nominal 0, P = I - nn^T and e = x - x_ref. With the recorded zero integral/compliance stiffness, the exact difference is:','','    du = (fd-f0) + F*(nd-n0) - K*P0*(ed-e0) - K*(Pd-P0)*ed','','Every sample is reconstructed within 1e-11 N. These terms are coupled observations, not independently intervened causal effects. RMS magnitudes do not add; cancellation can occur. The counterfactual replacement of a normal estimate is NOT executed.','','| Observer | Direction | Window | Total input RMS N | Measured force term N | Target-normal term N | Path-displacement term N | Path-projection term N | Feedforward delta mm/s |','|---|---|---|---:|---:|---:|---:|---:|---:|']
 for row in results:
  for window in ('intervention','post_release'):
   w=row['windows'][window];values=[w['total_law_input_delta_rms_n']]+list(w['term_rms_n'].values())+[w['feedforward_delta_rms_m_s']*1000]
   lines.append('| '+' | '.join([row['variant'],row['direction'],window]+[f'{v:.4f}' for v in values])+' |')
 (a.output/'results.md').write_text('\n'.join(lines)+'\n')
if __name__=='__main__':main()
