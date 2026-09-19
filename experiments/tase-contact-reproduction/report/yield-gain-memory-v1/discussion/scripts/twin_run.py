"""Private advisory twin/refinement run. Writes ONLY to /tmp/ygm. No device, no repo write."""
import sys, json, gzip, os, time
ROOT='/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction'
sys.path.insert(0, ROOT+'/tools'); os.chdir(ROOT)
import numpy as np
import contact_yield_runner as runner
scen=sys.argv[1]; sub=int(sys.argv[2]); eps=float(sys.argv[3]); out=sys.argv[4]
orig=runner.make_system
def patched(**kw):
    ctrl,plant,origin=orig(**kw)
    if eps!=0.0:
        plant.q[0]+=eps
        contact=plant._contact_wrench(*plant._pose_and_twist())
        plant.sensed_force=np.array(contact['force_base_n'],dtype=float).copy()
    return ctrl,plant,origin
runner.make_system=patched
params=json.load(open(ROOT+'/config/contact_yield_candidates/msfc.json'))
t0=time.time()
r=runner.run_closed_loop(method='MSFC',scenario=scen,material='stiff_low_mu',duration_s=runner.PERIOD_S,dt_s=0.002,
    timeline='full_cycle',law_parameters=params,record_fullstate=False,plant_substeps=sub)
r['advisory_note']={'perturbation_rad_joint0':eps,'purpose':'twin/refinement diagnostic; not a retained experiment','wall_s':time.time()-t0}
with gzip.open(out,'wt') as f: json.dump(r,f,separators=(',',':'))
print(json.dumps({k:r['metrics'][k] for k in ('failed','failure_message','force_mae_n','force_peak_n','contact_loss_duration_s','saturation_ticks','path_rmse_m')}),'wall',round(time.time()-t0,1))
