"""Single-inflight evidence ledger for paired, equal-budget contact evaluations.

This has no device/optimizer entrypoint. Physical attempts are registered BEFORE
execution, so a failure or process restart cannot refund an evaluation unit.
Sealing the same evidence is idempotent; conflicting rewrites are rejected.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib,json
from pathlib import Path
import sqlite3
from contact_benchmark_protocol import CONTROLLERS,evaluation_budget


def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)


class ContactLedger:
    def __init__(self,path: Path, *, protocol_sha256: str):
        if len(protocol_sha256)!=64 or any(c not in '0123456789abcdef' for c in protocol_sha256):
            raise ValueError('protocol digest required')
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=5,isolation_level=None)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS units(controller TEXT NOT NULL,number INTEGER NOT NULL,candidate TEXT NOT NULL,
        PRIMARY KEY(controller,number));
        CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,controller TEXT NOT NULL,unit INTEGER NOT NULL,
        condition TEXT NOT NULL,status TEXT NOT NULL,evidence TEXT,
        UNIQUE(controller,unit,condition),FOREIGN KEY(controller,unit) REFERENCES units(controller,number));''')
        with self.transaction():
            prior=self.db.execute("SELECT value FROM metadata WHERE key='protocol'").fetchone()
            if prior and prior[0]!=protocol_sha256:raise ValueError('ledger protocol differs')
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('protocol',?)",(protocol_sha256,))

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:yield
        except Exception:self.db.execute('ROLLBACK');raise
        else:self.db.execute('COMMIT')

    def begin(self, *, attempt_id,controller,candidate,condition,unit=None):
        if controller not in CONTROLLERS or condition not in ('nominal','disturbed'):
            raise ValueError('unknown controller/condition')
        if not isinstance(attempt_id,str) or not attempt_id:raise ValueError('physical attempt ID required')
        identity=canonical(candidate)
        with self.transaction():
            prior=self.db.execute('SELECT controller,unit,condition FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if prior:
                prior_candidate=self.db.execute('SELECT candidate FROM units WHERE controller=? AND number=?',prior[:2]).fetchone()[0]
                if prior[0]!=controller or prior[2]!=condition or (unit is not None and prior[1]!=unit) or prior_candidate!=identity:
                    raise ValueError('attempt identity conflict')
                return prior[1]
            if self.db.execute("SELECT id FROM attempts WHERE status='running'").fetchone():
                raise ValueError('one physical attempt is already inflight')
            if self.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone():
                raise ValueError('tuning ledger is frozen')
            if unit is None:
                if condition!='nominal':raise ValueError('new unit starts with nominal trial')
                unit=self.db.execute('SELECT COUNT(*) FROM units WHERE controller=?',(controller,)).fetchone()[0]
                if unit>=evaluation_budget()['per_controller_units']:raise ValueError('controller budget exhausted')
                pending=self.db.execute('''SELECT u.number FROM units u LEFT JOIN attempts a ON a.controller=u.controller AND a.unit=u.number
                    WHERE u.controller=? GROUP BY u.number HAVING COUNT(a.id)<2''',(controller,)).fetchone()
                if pending:raise ValueError('previous paired unit is incomplete')
                self.db.execute('INSERT INTO units VALUES(?,?,?)',(controller,unit,identity))
            else:
                found=self.db.execute('SELECT candidate FROM units WHERE controller=? AND number=?',(controller,unit)).fetchone()
                if not found or found[0]!=identity:raise ValueError('paired trial candidate differs')
            self.db.execute('INSERT INTO attempts VALUES(?,?,?,?,?,NULL)',(attempt_id,controller,unit,condition,'running'))
            return unit

    def seal(self,attempt_id, *, status,evidence):
        if status not in ('complete','censored','failed','interrupted'):raise ValueError('terminal status required')
        digest=evidence.get('artifact_sha256') if isinstance(evidence,dict) else None
        if not isinstance(digest,str) or len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('immutable evidence digest required')
        if status=='complete' and evidence.get('objective_eligible') is not True:
            raise ValueError('complete result requires uncensored metrics')
        if status!='complete' and evidence.get('objective_eligible') is not False:
            raise ValueError('failed/censored result cannot train optimizer')
        value=canonical(evidence)
        with self.transaction():
            row=self.db.execute('SELECT status,evidence FROM attempts WHERE id=?',(attempt_id,)).fetchone()
            if row is None:raise ValueError('attempt was not registered')
            if row[0]!='running':
                if row!=(status,value):raise ValueError('terminal evidence conflict')
                return
            self.db.execute('UPDATE attempts SET status=?,evidence=? WHERE id=?',(status,value,attempt_id))

    def progress(self):
        return {c:{'used_units':self.db.execute('SELECT COUNT(*) FROM units WHERE controller=?',(c,)).fetchone()[0],
                   'completed_trials':self.db.execute("SELECT COUNT(*) FROM attempts WHERE controller=? AND status='complete'",(c,)).fetchone()[0],
                   'failed_trials':self.db.execute("SELECT COUNT(*) FROM attempts WHERE controller=? AND status IN ('failed','interrupted','censored')",(c,)).fetchone()[0]} for c in CONTROLLERS}

    def freeze(self,selected_candidates):
        if set(selected_candidates)!=set(CONTROLLERS):raise ValueError('freeze must cover all six controllers')
        with self.transaction():
            if self.db.execute("SELECT id FROM attempts WHERE status='running'").fetchone():raise ValueError('attempt inflight')
            for c in CONTROLLERS:
                count=self.db.execute('SELECT COUNT(*) FROM units WHERE controller=?',(c,)).fetchone()[0]
                trials=self.db.execute('SELECT COUNT(*) FROM attempts WHERE controller=?',(c,)).fetchone()[0]
                if count!=24 or trials!=48:raise ValueError('equal paired tuning budgets not complete')
                found=self.db.execute('SELECT number FROM units WHERE controller=? AND candidate=?',(c,canonical(selected_candidates[c]))).fetchone()
                if not found:raise ValueError('selected candidate was not evaluated')
                admitted=self.db.execute("SELECT COUNT(*) FROM attempts WHERE controller=? AND unit=? AND status='complete'",(c,found[0])).fetchone()[0]
                if admitted!=2:raise ValueError('selected candidate lacks a complete nominal/disturbed pair')
            value=canonical(selected_candidates)
            prior=self.db.execute("SELECT value FROM metadata WHERE key='frozen'").fetchone()
            if prior and prior[0]!=value:raise ValueError('frozen parameters cannot change')
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('frozen',?)",(value,))
            return hashlib.sha256(value.encode()).hexdigest()

    def close(self):self.db.close()
