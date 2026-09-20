"""Build a vertical-only relief program and reusable clearance-only Home."""
import argparse,copy,datetime,json
from pathlib import Path
from build_contact_home import build,HOME_SOURCE
from build_contact_benchmark_triplet import CONTROLLER_DIR
from build_step4e_p0p1_programs import build_urp
from step5d_autotune_v4_r012.controller_triplet import validate_urscript_block_balance

RELIEF_PROGRAM='step5d_contact_relief_v1'

def build_recovery(receipt,output):
    home=json.loads(Path(receipt).read_text())
    home.pop('bounded_recovery',None);home.pop('bounded_withdrawal',None)
    home['clearance_entry']=True
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    bound=output/'recovery-home-source.json';bound.write_text(json.dumps(home,indent=2)+'\n')
    result=build(bound,output)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H%MZ_CONTACT_RELIEF_V1')
    prefix=HOME_SOURCE.read_text().split('def step5d_autotune_start_hover_r001():',1)[0]
    prefix=prefix[prefix.index('def codex_start_hover_finite_value'):]
    text=f'''# VERSION: {stamp}
# ROLE: monitored Base +Z relief only; no XY or attitude transfer
# No force-control restart; host owns fresh Kunwei unloading guard and Stop.
{prefix}
def {RELIEF_PROGRAM}():
  local current_pose = get_actual_tcp_pose()
  local target_pose = p[{', '.join(str(float(x)) for x in home['home_pose'])}]
  if not codex_start_hover_finite_pose(current_pose):
    halt
  end
  local dx = current_pose[0]-target_pose[0]
  local dy = current_pose[1]-target_pose[1]
  local dz = current_pose[2]-target_pose[2]
  local distance = sqrt(dx*dx + dy*dy + dz*dz)
  local turn = pose_trans(pose_inv(target_pose), current_pose)
  local angle = sqrt(turn[3]*turn[3] + turn[4]*turn[4] + turn[5]*turn[5])
  if distance > 0.080 or angle > 0.010 or current_pose[2] < 0.018 or current_pose[2] > 0.034:
    textmsg("contact_relief: initial geometry rejected")
    halt
  end
  if current_pose[2] < 0.033:
    local rise_pose = p[current_pose[0], current_pose[1], 0.033, current_pose[3], current_pose[4], current_pose[5]]
    movel(rise_pose, a=0.005, v=0.0005, r=0.0)
    stopl(0.1)
  end
  sleep(0.20)
  textmsg("contact_relief: vertical stage complete; awaiting host release check")
  halt
end

{RELIEF_PROGRAM}()
'''
    validate_urscript_block_balance(text)
    for ext,data in [('script',text.encode()),('txt',f'{RELIEF_PROGRAM}\n{stamp}\n'.encode()),('urp',build_urp(text,RELIEF_PROGRAM,CONTROLLER_DIR))]:
        (output/f'{RELIEF_PROGRAM}.{ext}').write_bytes(data)
    relief={'basename':RELIEF_PROGRAM,'stamp':stamp,'controller_directory':CONTROLLER_DIR,'vertical_speed_m_s':.0005,'vertical_acceleration_m_s2':.005,'max_rise_m':.015,'xy_or_attitude_motion':False}
    (output/f'{RELIEF_PROGRAM}.binding.json').write_text(json.dumps(relief,indent=2)+'\n')
    return {'home':result,'relief':relief}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--home-receipt',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();print(json.dumps(build_recovery(a.home_receipt,a.output),indent=2))
