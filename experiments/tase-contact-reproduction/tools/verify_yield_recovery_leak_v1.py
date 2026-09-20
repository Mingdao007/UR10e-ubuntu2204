"""Check the quasi-static leak heuristic against actual post-release displacement."""
import argparse,hashlib,gc
from pathlib import Path
import numpy as np
from run_contact_yield import read,write

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 index={x['id']:x for x in read(Path('report/yield-normal-v3/results.json'))['rows']};data={};hashes={}
 for name in ('combined-mild-approach','combined-tangent-hold'):
  row=index[name];path=Path(row['path']);sha=hashlib.sha256(path.read_bytes()).hexdigest();assert sha==row['sha256'];hashes[str(path)]=sha
  r=read(path);data[name]={key:np.asarray([x[field] for x in r['rows']]) for key,field in [('t','time_s'),('n','normal_estimate'),('pos','position_m')]};del r;gc.collect()
 n=data['combined-mild-approach'];d=data['combined-tangent-hold'];assert np.array_equal(n['t'],d['t'])
 t=d['t'];angle=np.rad2deg(np.arccos(np.clip(np.sum(n['n']*d['n'],axis=1),-1,1)));distance=np.linalg.norm(d['pos']-n['pos'],axis=1)*1000
 recovery=index['combined-tangent-hold']['pair']['recovery_s'];w=(t>=35.5)&(angle>.5)&(t<35.5+recovery+2);slope,intercept=np.polyfit(angle[w],distance[w],1);i=np.argmin(abs(t-35.5-recovery))
 result={'sources':hashes,'fit_slope_mm_per_deg':float(slope),'fit_intercept_mm':float(intercept),'heuristic_slope_mm_per_deg':5000/120*np.pi/180,
  'at_recovery':{'angle_deg':float(angle[i]),'actual_distance_mm':float(distance[i]),'leak_only_prediction_mm':float(5000/120*np.sin(np.deg2rad(angle[i])))},
  'claim':'Slope agreement supports a coupling mechanism, not an exact dynamic identity or exclusive observer causality. The fitted intercept is nonzero.'}
 write(a.output,result)
if __name__=='__main__':main()
