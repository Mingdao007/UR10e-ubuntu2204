from dataclasses import replace
import math,json
from contact_yield_protocol import PERIOD_S
from yield_contact_evidence import YieldPathEvidenceCollector
from step5d_autotune_v4_r004_live_writer import BoundedPacketHistory
from test_step5d_autotune_v4_r004_evidence_ledger import _motion_sample
h=BoundedPacketHistory();h.record(0,published_at_s=100.,qdot=(0.,)*6,reference_phase='path',reference_time_s=.002)
c=YieldPathEvidenceCollector(require_path_boundary=True,published_reference_lookup=h.consumed)
c.mark_path_start(observed_at_s=100.,rtde_timestamp_s=0.,tp_sequence=0)
n=math.floor(PERIOD_S/.002)+1
for i in range(n):
 t=i*.002;r=t+.002-min(.002,t*.1)
 if i:h.record(i,published_at_s=100+t,qdot=(0.,)*6,reference_phase='path',reference_time_s=r)
 seq={'writer':i,'rtde':t,'kunwei':i,'tp':i}
 c.observe(replace(_motion_sample(i,t,final=i==n-1),observed_at_s=100+t,source_sequences=seq,source_sequence=seq))
e=c.finalize(return_gate_passed=True,contact_gate_passed=True,home_proof={'stationary':True})
print(json.dumps({'scope':'adversarial offline evidence collector check, not actual emitted runtime','accepted':True,'physical_duration_s':e.path_duration_s,'first_reference_s':.002,'last_reference_s':r,'reference_span_plus_coverage_s':r-.002+.002,'required_s':PERIOD_S},indent=2))
