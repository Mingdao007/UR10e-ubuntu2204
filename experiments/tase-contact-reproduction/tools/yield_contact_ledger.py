"""Three-method paired training ledger; no experiment or hardware entrypoint.

Reuse the mature SQLite attempt transactions. This ledger seals declarations
and their artifact digests; the runner remains responsible for producing and
verifying full-task evidence. It never turns holdout rows into training data.
"""
from __future__ import annotations
import hashlib
import json
from collections.abc import Mapping
from types import MappingProxyType
from contact_benchmark_ledger import ContactLedger, canonical
from yield_contact_tuner import METHODS, YieldContactTuner, YieldTrainingObservation


class YieldContactLedger(ContactLedger):
    def __init__(self, path, *, tuner, campaign_protocol_sha256):
        if not isinstance(tuner, YieldContactTuner):
            raise ValueError('validated yield tuner required')
        if (not isinstance(campaign_protocol_sha256, str) or len(campaign_protocol_sha256)!=64
                or any(c not in '0123456789abcdef' for c in campaign_protocol_sha256)):
            raise ValueError('campaign protocol digest required')
        self.tuner=tuner
        self.bindings=MappingProxyType({
            'schema':'yield-contact-training-ledger-v1',
            'split':'training',
            'campaign_protocol_sha256':campaign_protocol_sha256,
            'tuner_config_sha256':tuner.config_sha256,
            'training_cell_id':tuner.training_cell_id,
            'selection_contract_id':tuner.selection_contract_id,
        })
        bound=hashlib.sha256(canonical(dict(self.bindings)).encode()).hexdigest()
        super().__init__(path,protocol_sha256=bound,controllers=METHODS)

    def _verify_tuner_binding(self):
        for field,key in [('config_sha256','tuner_config_sha256'),
                          ('training_cell_id','training_cell_id'),
                          ('selection_contract_id','selection_contract_id')]:
            if getattr(self.tuner,field)!=self.bindings[key]:
                raise ValueError('tuner binding changed after ledger creation')

    def begin(self, *, attempt_id, controller, candidate, condition, unit=None):
        self._verify_tuner_binding()
        if controller not in self.controllers:
            raise ValueError('unknown yield method')
        parsed=self.tuner._parse_candidate(controller,candidate)
        return super().begin(attempt_id=attempt_id,controller=controller,
                             candidate=parsed.as_dict(),condition=condition,unit=unit)

    def seal(self, attempt_id, *, status, evidence):
        self._verify_tuner_binding()
        if not isinstance(evidence,Mapping):
            raise ValueError('bound training evidence required')
        for key,expected in self.bindings.items():
            if evidence.get(key)!=expected:
                raise ValueError(f'training evidence {key} differs')
        return super().seal(attempt_id,status=status,evidence=dict(evidence))

    def inflight(self):
        self._verify_tuner_binding()
        row=self.db.execute(
            "SELECT id,controller,unit,condition FROM attempts WHERE status='running'").fetchone()
        if row is None:
            return None
        return {'attempt_id':row[0],'controller':row[1],'unit':int(row[2]),'condition':row[3]}

    def training_observations(self, controller):
        self._verify_tuner_binding()
        base=super().training_observations(controller)
        rows=[]
        for index,row in enumerate(base):
            sealed=list(self.db.execute(
                'SELECT condition,status,evidence FROM attempts WHERE controller=? AND unit=?',
                (controller,index)))
            pair={condition:status for condition,status,_ in sealed}
            adapted=row.nominal_feasible
            if row.status=='completed':
                evidence={condition:(json.loads(payload) if payload else None)
                          for condition,_,payload in sealed}
                disturbed=evidence.get('disturbed') or {}
                guards=disturbed.get('disturbed_guards_ok')
                pair_feasible=disturbed.get('pair_feasible')
                if 'disturbed_guards_ok' in disturbed or 'pair_feasible' in disturbed:
                    if type(row.nominal_feasible) is not bool:
                        raise ValueError('paired feasibility requires bool nominal_feasible')
                    if type(guards) is not bool:
                        raise ValueError('disturbed_guards_ok must be bool')
                    if type(pair_feasible) is not bool:
                        raise ValueError('pair_feasible must be bool')
                    computed=row.nominal_feasible and guards
                    if pair_feasible is not computed:
                        raise ValueError('pair_feasible contradicts member feasibility')
                    adapted=computed
            rows.append(YieldTrainingObservation(
                method=controller,candidate=row.candidate,status=row.status,
                nominal_feasible=adapted,objective=row.objective,
                unit_index=index,pair_complete=True,pair=pair,split='training',
                training_cell_id=self.tuner.training_cell_id,
                selection_contract_id=self.tuner.selection_contract_id))
        return rows

    def freeze(self, selected_candidates):
        if set(selected_candidates)!=set(METHODS):
            raise ValueError('freeze must cover SFC, DSFC and MSFC')
        selected={}
        for method in METHODS:
            history=self.training_observations(method)
            if len(history)!=24:
                raise ValueError('equal paired tuning budgets not complete')
            for index,row in enumerate(history):
                expected=self.tuner.propose(method,history[:index],index).candidate
                actual=self.tuner._parse_candidate(method,row.candidate)
                if actual.key!=expected.key:
                    raise ValueError('recorded candidate differs from deterministic proposal schedule')
            incumbent=self.tuner.propose(method,history[:20],20).candidate
            actual=self.tuner._parse_candidate(method,selected_candidates[method])
            if actual.key!=incumbent.key:
                raise ValueError('selection differs from frozen discovery incumbent')
            selected[method]=actual.as_dict()
        return super().freeze(selected)
