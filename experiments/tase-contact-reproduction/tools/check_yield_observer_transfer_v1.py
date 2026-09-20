"""Full-state replay and 1 ms refinement of OT-v1 SFC normal-contact-loss case."""
import argparse,hashlib
from pathlib import Path
from run_contact_yield import read,write
from contact_yield_runner import run_closed_loop
from contact_yield_controller import YieldSettings
from contact_yield_replay import replay_artifact,refinement_error

def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=False)
 original=read(a.input);ident=original['identity_payload'];assert original['method']=='SFC' and original['scenario']=='sustained_release_normal'
 write(a.output/'protocol.json',{'input':str(a.input.resolve()),'sha256':hashlib.sha256(a.input.read_bytes()).hexdigest(),
  'dt_s':.001,'parameters_changed':False,'live_executed':False,'reason':'verify newly observed contact loss, not retune'})
 replay=replay_artifact(original);write(a.output/'replay.json',replay);assert replay['passed']
 fine=run_closed_loop(method=original['method'],scenario=original['scenario'],material=original['material'],
  duration_s=original['duration_s'],dt_s=.001,timeline=original['timeline'],preparation=original['preparation'],
  law_parameters=ident['parameters'],settings=YieldSettings(**ident['settings']),qp_library=ident['qp_library'],
  estimator_parameters=ident['estimator_parameters'],surface_parameters=original.get('surface_parameters'),
  plant_substeps=original['plant_identity_payload'].get('integration_substeps'),record_fullstate=True)
 path=a.output/'fine.json.gz';write(path,fine)
 diagnostic=None if fine['metrics']['failed'] else refinement_error(original['rows'],fine['rows'],coarse_dt_s=.002,fine_dt_s=.001)
 write(a.output/'results.json',{'replay':replay,'fine_path':str(path.resolve()),'fine_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
  'full_cycle':fine['full_cycle'],'fine_metrics':fine['metrics'],'coarse_metrics':original['metrics'],'refinement':diagnostic,
  'claim':'same-implementation replay and step sensitivity; not physical validation'})
if __name__=='__main__':main()
