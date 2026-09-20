import json,hashlib
from pathlib import Path
import numpy as np
from contact_yield_laws import YieldLaw
root=Path.cwd();manifest=json.loads((root/'report/yield-mechanism-development-v1/frozen-manifest.json').read_text())
results=[]
for u in (0,4):
 slot=next(s for s in manifest['slots'] if s['unit_index']==u and s['arm']=='SFC' and s['resolution_id']=='base')
 aid=f'SFC-{u:02d}-disturbed';cache=Path('/tmp/yfp10-main')/f'{aid}.npz'
 z=np.load(cache);forces=z['res_law_force_base_n']
 for dt in (.002,.001):
  for axis in range(3):
   maximum=0.
   with YieldLaw('SFC',slot['law_parameters'],dt_s=dt,build_root=root/'build/contact-six-laws') as a,YieldLaw('SFC_RADIAL',slot['law_parameters'],dt_s=dt,build_root=root/'build/contact-six-laws') as b:
    for row in forces:
     f=np.zeros(3);f[axis]=row[axis]
     for repeat in range(round(.002/dt)):
      maximum=max(maximum,float(np.max(np.abs(np.array(a.step(f,dt))-np.array(b.step(f,dt))))))
   assert maximum<1e-14,(u,dt,axis,maximum)
   results.append({'unit':u,'dt_s':dt,'axis':axis,'ticks':len(forces)*round(.002/dt),'max_command_difference_m_s':maximum,'parameters':slot['law_parameters'],'cache_sha256':hashlib.sha256(cache.read_bytes()).hexdigest()})
print(json.dumps({'scope':'Axis restriction of full recorded PATH law-force input, zero initial state, each force component replayed separately; 1ms uses zero-order-held 2ms input. Not a closed-loop trial or physical evidence.','results':results},indent=2))
