"""Recompute retained one-pose evidence; no robot endpoint or contract mutation."""
import hashlib,json,sys
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tools'))
from step5c_calibrated_kinematics_audit import build_calibrated_model,DEFAULT_XACRO_PATH,DEFAULT_CALIBRATION_YAML,rotvec_to_matrix
from contact_yield_kinematics import Ur10eKinematics,TCP_OFFSET_TOOL0
from contact_yield_math import so3_log

def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 source=ROOT/'report/contact-six-qp-20260917/readonly-kinematics.json'
 old=json.loads(source.read_text());model=build_calibrated_model();kin=Ur10eKinematics(model)
 actual=old['rtde'];assert np.array_equal(np.asarray(actual['tcp_offset'][:3]),TCP_OFFSET_TOOL0)
 assert np.allclose(actual['tcp_offset'][3:],0,rtol=0,atol=0)
 pose=kin.pose_and_jacobian(actual['actual_q']);rotation=rotvec_to_matrix(np.asarray(actual['actual_TCP_pose'][3:]))
 position_error=float(np.linalg.norm(np.asarray(pose['position_m'])-actual['actual_TCP_pose'][:3]));rotation_error=float(np.linalg.norm(so3_log(pose['rotation']@rotation.T)))
 generated=hashlib.sha256(model.urdf_text.encode()).hexdigest()
 result={'scope':'offline recomputation of 2026-09-17 one-pose measurement; no new physical or workspace qualification',
  'source':str(source),'source_sha256':sha(source),'source_observed_at':old['observed_at'],
  'xacro_path':str(DEFAULT_XACRO_PATH),'xacro_sha256':sha(DEFAULT_XACRO_PATH),
  'calibration_yaml':str(DEFAULT_CALIBRATION_YAML),'calibration_yaml_sha256':sha(DEFAULT_CALIBRATION_YAML),
  'calibration_hash':model.calibration_hash,'generated_urdf_sha256':generated,
  'same_xacro_as_retained':sha(DEFAULT_XACRO_PATH)==old['sources'][str(DEFAULT_XACRO_PATH)],
  'same_expanded_urdf_as_retained':generated==old['generated_urdf_sha256'],
  'same_calibration_as_retained':model.calibration_hash==old['calibration_hash'],
  'position_error_m':position_error,'rotation_error_rad':rotation_error,
  'retained_position_error_m':old['position_error_m'],'retained_rotation_error_rad':old['rotation_error_rad'],
  'source_files':{str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'tools/step5c_calibrated_kinematics_audit.py',ROOT/'tools/contact_yield_kinematics.py',ROOT/'tools/contact_yield_math.py')}}
 out=Path(__file__).with_name('result.json')
 with out.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
 print(json.dumps(result,indent=2))
if __name__=='__main__':main()
