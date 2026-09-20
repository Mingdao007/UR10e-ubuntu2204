"""Prescribed-history native MSFC audit; no closed-loop or hardware claim."""
from pathlib import Path
import hashlib,json,sys
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from contact_yield_laws import YieldLaw
params=json.loads((ROOT/'report/yield-frozen-transfer-v1/protocol.json').read_text())['parameters']['MSFC']
dt=.002
rows=[]
for history in ('cold','held_x','held_y'):
 for gap in (0.,.2,.6,2.):
  with YieldLaw('MSFC',params,dt_s=dt,build_root=ROOT/'build/contact-six-laws') as law:
   forcing=[]
   if history!='cold':
    force=[3.,0.,0.] if history=='held_x' else [0.,3.,0.]
    for _ in range(1000):forcing.append(force)
   forcing.extend([[0.,0.,0.]]*round(gap/dt))
   for f in forcing:law.step(f,dt)
   initial=law.snapshot();outputs=[];states=[]
   probes=[[1.5,1.,.5]]*100+[[0.,0.,0.]]*400
   for f in probes:
    outputs.append(law.step(f,dt));states.append(law.snapshot().values)
   with YieldLaw('MSFC',params,dt_s=dt,build_root=ROOT/'build/contact-six-laws') as fresh:
    fresh.restore(initial);replay=[];rs=[]
    for f in probes:
     replay.append(fresh.step(f,dt));rs.append(fresh.snapshot().values)
   row={'history':history,'zero_input_gap_s':gap,'preparation_force_n':None if history=='cold' else force,
    'preparation_duration_s':0. if history=='cold' else 2.,'initial_snapshot':list(initial.values),
    'initial_history_norm':float(np.linalg.norm(initial.values[7:10])),
    'initial_structure_norm':float(np.linalg.norm(initial.values[10:19])),
    'command_replay_max_abs_m_s':float(np.max(np.abs(np.asarray(outputs)-replay))),
    'state_replay_max_abs':float(np.max(np.abs(np.asarray(states)-rs))),
    'commands_m_s':outputs,'snapshots':states,'identity':law.identity,'build_fingerprint':law.build_fingerprint}
   assert row['command_replay_max_abs_m_s']==0 and row['state_replay_max_abs']==0
   rows.append(row)
summary=[]
for gap in (0.,.2,.6,2.):
 a={r['history']:r for r in rows if r['zero_input_gap_s']==gap}
 summary.append({'gap_s':gap,'warm_x_vs_cold_max_command_difference_m_s':float(np.max(np.abs(np.asarray(a['held_x']['commands_m_s'])-a['cold']['commands_m_s']))),
 'warm_x_vs_y_max_command_difference_m_s':float(np.max(np.abs(np.asarray(a['held_x']['commands_m_s'])-a['held_y']['commands_m_s']))),
 'warm_x_structure_norm':a['held_x']['initial_structure_norm']})
print(json.dumps({'schema':'yield-warm-memory-prescribed-v1','scope':'development native prescribed input/state audit; history changes velocity and memory; no isolated memory causality, closed-loop advantage, physical or final-holdout claim',
 'parameters':params,'dt_s':dt,'probe':{'force_n':[1.5,1.,.5],'hold_ticks':100,'release_ticks':400},
 'script_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'summary':summary,'rows':rows},separators=(',',':')))
