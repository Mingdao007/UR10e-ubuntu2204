"""Native mechanical equivalence check, not a robot/contact or tuned comparison."""
from pathlib import Path
import sys,json,hashlib
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from contact_yield_laws import YieldLaw
from contact_yield_protocol import law_seed_parameters

ms=json.loads((ROOT/'report/yield-frozen-transfer-v1/protocol.json').read_text())['parameters']['MSFC']
ms={**ms,'minimum_metric_eigenvalue':1.0}
ds=law_seed_parameters('DSFC')
ds.update({k:ms[k] for k in ('m','g','p','a','n','mu')})
results=[]
for dt in (.001,.002,.003):
    times=np.arange(0.,6.,dt)
    forces=np.column_stack((2*np.sin(3*times),1.3*np.cos(2*times),.7*np.sin(7*times)))
    forces[(times>=1)&(times<2),0]+=3.
    forces[(times>=3)&(times<3.1)]+=np.array([2.,-3.,1.])
    forces[times>=4]=0.
    with YieldLaw('DSFC',ds,dt_s=dt,build_root=ROOT/'build/contact-six-laws') as a, YieldLaw('MSFC',ms,dt_s=dt,build_root=ROOT/'build/contact-six-laws') as b:
        outputs=[];snapshots=None;max_structure=0.
        for i,f in enumerate(forces):
            va=a.step(f,dt);vb=b.step(f,dt)
            outputs.append([va,vb])
            # Slots 10:19 are the evolving MSFC structure, not its metric.
            max_structure=max(max_structure,float(np.linalg.norm(b.snapshot().values[10:19])))
            if i==len(forces)//2:snapshots=(a.snapshot(),b.snapshot(),i)
        arr=np.asarray(outputs)
        a.restore(snapshots[0]);b.restore(snapshots[1]);replay=[]
        for f in forces[snapshots[2]+1:]:replay.append([a.step(f,dt),b.step(f,dt)])
        err=float(np.max(np.abs(arr[:,0]-arr[:,1])))
        replay_err=float(np.max(np.abs(np.asarray(replay)-arr[snapshots[2]+1:])))
        results.append({'dt_s':dt,'ticks':len(forces),'command_max_abs_difference_m_s':err,'within_1e-12_m_s':err<=1e-12,'snapshot_replay_max_abs_difference':replay_err,'max_memory_structure_norm':max_structure,'build_fingerprint':a.build_fingerprint,'dsfc_identity':a.identity,'msfc_identity':b.identity})
        assert err<=1e-12 and replay_err==0.
print(json.dumps({'version':'coefficient-equivalence-v1','scope':'prescribed-input native mechanical check only; no closed-loop superiority or physical claim','DSFC':ds,'MSFC_identity':ms,'results':results,'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2))
