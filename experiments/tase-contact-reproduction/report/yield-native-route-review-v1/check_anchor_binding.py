from pathlib import Path
import json,hashlib
import numpy as np
from yield_native_route import create_native_yield_runtime
from step5c_calibrated_kinematics_audit import rotvec_to_matrix
primary=Path('/home/andy/.codex-worktrees/contact-yield-recovery-20260920/experiments/tase-contact-reproduction')
source=Path('tools/yield_native_route.py');before=hashlib.sha256(source.read_bytes()).hexdigest()
home=json.loads((primary/'report/contact-six-qp-20260917/preserved-home.json').read_text())
rot=rotvec_to_matrix(np.asarray(home['home_pose'][3:]))
kwargs=dict(method='SFC',qp_library=primary/'build/contact-qp/libcontact_qp.so',task_basis=rot,approach_inward_base=rot[:,2],deadline_s=None,build_root=primary/'build/contact-six-laws')
a,bind_a=create_native_yield_runtime(anchor_m=home['home_pose'][:3],**kwargs)
b,bind_b=create_native_yield_runtime(anchor_m=np.asarray(home['home_pose'][:3])+[.001,0,0],**kwargs)
try:
 accepted=False;error=None
 try:bind_a.require_runtime(b);accepted=True
 except Exception as exc:error=str(exc)
 print(json.dumps({'scope':'offline constructor-only draft review; no command/endpoint access','source_sha256':before,'source_stable':before==hashlib.sha256(source.read_bytes()).hexdigest(),'runtime_identities_differ':a.identity!=b.identity,'controller_identities_equal':a.controller.identity==b.controller.identity,'anchor_changed_m':.001,'binding_for_A_accepts_B':accepted,'error':error},indent=2))
finally:a.close();b.close()
