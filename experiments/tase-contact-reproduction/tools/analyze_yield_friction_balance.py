"""Posthoc mechanism diagnostic only; does not replace full-window metrics."""
import json
from pathlib import Path
import numpy as np
from run_contact_yield import read
ROOT=Path(__file__).resolve().parents[1]
rows=read(ROOT/'report/yield-normal-v2/comparison.json');out=[]
for row in rows:
    if row['scenario']!='nominal' or row['variant']=='NO-v2':continue
    r=read(Path(row['path']));force=[];restoring=[];balance=[];displacement=[]
    for rec in r['records']:
        v=rec['result']
        if v['phase']!='path' or v['path_time_s']<5.:continue
        n=np.asarray(v['inward_normal_base']);P=np.eye(3)-np.outer(n,n)
        f=P@np.asarray(v['filtered_force_base_n'])
        restoring_force=-120.*P@np.asarray(v['path_error_base_m'])
        force.append(f);restoring.append(restoring_force);balance.append(f+restoring_force)
        displacement.append(np.linalg.norm(P@np.asarray(v['path_error_base_m'])))
    rms=lambda values:float(np.sqrt(np.mean(np.sum(np.asarray(values)**2,axis=1))))
    out.append({'variant':row['variant'],'material':row['material'],'source_sha256':row['sha256'],
        'window':'PATH >=5s, posthoc diagnostic, complete metrics unchanged',
        'tangent_force_rms_n':rms(force),'restoring_rms_n':rms(restoring),
        'net_tangent_law_input_rms_n':rms(balance),
        'estimated_frame_path_rms_m':float(np.sqrt(np.mean(np.asarray(displacement)**2)))})
    del r
(ROOT/'report/yield-normal-v2/friction-balance.json').write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps(out,indent=2))
