"""Round-8 input/output receipt: hashes of every allowed input actually read, the 25 raw
receipts re-hashed against the fixed snapshot, the outputs, and what was not done."""
import hashlib, json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[4]
D = ROOT / 'report/yield-fair-training-v1/discussion-round8'
def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''): h.update(c)
    return h.hexdigest()
def git(*a):
    return subprocess.run(['git', *a], cwd=ROOT, capture_output=True, text=True).stdout.strip()
INPUTS = [
    'report/yield-fair-training-v1/round8-input-snapshot.json',
    'report/yield-fair-training-v1/README.md', 'report/yield-fair-training-v1/launch-binding.json',
    'report/yield-fair-training-v1/initial-descriptors.json', 'report/yield-fair-training-v1/initial_descriptors.py',
    'report/yield-fair-training-v1/progress.json',
    'config/yield_fair_campaign_v1.json', 'config/yield_fair_tuning_v1.json', 'config/yield_normal_observer_v3.json',
    'tools/yield_fair_selection.py', 'tools/yield_fair_campaign.py', 'tools/yield_contact_ledger.py', 'tools/yield_contact_tuner.py',
    'tools/contact_yield_metrics.py', 'tools/contact_yield_protocol.py',
    'report/yield-fair-tuning-v1/README.md', 'report/yield-fair-tuning-v1/writer-receipt.json',
    'report/yield-fair-tuning-v1/discussion-round7/fable-round7.md', 'report/yield-fair-tuning-v1/discussion-round7/fable-final.txt',
    'report/yield-fair-tuning-v1/discussion-round7/main-adjudication.md', 'report/yield-fair-tuning-v1/discussion-round7/main-verification.json',
    'report/yield-fair-tuning-v1/discussion-round7/round7-input-receipt.json', 'report/yield-fair-tuning-v1/discussion-round7/data/round7-analysis.json',
    'report/yield-validation-reservation-v2/README.md', 'report/yield-validation-reservation-v2/protocol.json', 'report/yield-validation-reservation-v2/receipt.json',
    'report/yield-validation-runner-review-v1/missing-ledger-counterexample.json', 'report/yield-validation-runner-review-v1/original-delivery.json',
    'report/yield-validation-runner-review-v1/main-original-tests.txt',
]
snap = json.loads((ROOT / 'report/yield-fair-training-v1/round8-input-snapshot.json').read_text())
binding = json.loads((ROOT / 'report/yield-fair-training-v1/launch-binding.json').read_text())
cp = binding['campaign_protocol']
raw = []
for a in snap['attempts']:
    p = Path(a['artifact_path']); h = sha(p)
    raw.append({'id': a['id'], 'path': str(p.relative_to(ROOT)), 'sha256_now': h, 'sha256_in_snapshot': a['evidence']['artifact_sha256'], 'match': h == a['evidence']['artifact_sha256'], 'bytes': p.stat().st_size})
bound = []
for grp in ('native_files', 'scientific_files'):
    for p, e in cp['execution_identities'][grp].items(): bound.append({'path': p, 'match': sha(ROOT / p) == e})
for p, e in cp['execution_identities']['native_laws_sha256'].items(): bound.append({'path': p, 'match': sha(ROOT / p) == e})
for p, e in cp['source_hashes'].items(): bound.append({'path': 'tools/' + p, 'match': sha(ROOT / 'tools' / p) == e})
bound.append({'path': binding['qp_library'], 'match': sha(binding['qp_library']) == cp['execution_identities']['qp_library_sha256']})
bound.append({'path': 'config/yield_fair_campaign_v1.json', 'match': sha(ROOT / 'config/yield_fair_campaign_v1.json') == cp['campaign_config_sha256']})
bound.append({'path': 'config/yield_fair_tuning_v1.json', 'match': sha(ROOT / 'config/yield_fair_tuning_v1.json') == cp['tuner_config_sha256']})
bound.append({'path': 'config/yield_normal_observer_v3.json', 'match': sha(ROOT / 'config/yield_normal_observer_v3.json') == cp['observer_config_sha256']})
canon = json.dumps(cp, sort_keys=True, separators=(',', ':')).encode()
bound.append({'path': 'launch-binding.json:campaign_protocol (canonical sha)', 'match': hashlib.sha256(canon).hexdigest() == binding['campaign_protocol_sha256']})
outputs = []
for p in sorted(D.rglob('*')):
    if p.is_file() and p.name != 'round8-input-receipt.json':
        outputs.append({'path': str(p.relative_to(ROOT)), 'sha256': sha(p), 'bytes': p.stat().st_size})
attempt_dirs = sorted(x.name for x in (ROOT / 'runs/yield-fair-training-v1/attempts').iterdir())
listed = {a['id'] for a in snap['attempts']}
receipt = {
    'schema': 'fable-advisory-round-receipt-v1', 'round': 8, 'study': 'yield-fair-training-v1',
    'directory': 'report/yield-fair-training-v1/discussion-round8/',
    'role': 'advisory, non-blocking; no live authority; not formal acceptance; first of at most two iterations',
    'model': 'claude-fable-5-1', 'requested_effort': 'xhigh', 'effort_evidence': 'launch parameter only, not independent server attestation',
    'head_in_session_opening_git_snapshot': 'b0059632', 'head_at_first_read_only_check': '4cd78c5c04eac9789a8a47d6b50e81720a275da4',
    'head_at_receipt': git('rev-parse', 'HEAD'), 'head_log_since_b0059632': git('log', '--oneline', 'b0059632..HEAD').splitlines(),
    'worktree_status_at_receipt': git('status', '--short').splitlines(),
    'written_at_utc': datetime.now(timezone.utc).isoformat(),
    'write_scope': 'report/yield-fair-training-v1/discussion-round8/ only; scratch /tmp/yfp8 not retained',
    'not_done': ['no runtime/test/config/other-report edits', 'no git write operations (read-only rev-parse/log/status only)',
                 'no hardware, transport or network access', 'no closed-loop runs, campaign, holdout or validation launches',
                 'no native law stepped', 'no subagents', 'no SQLite/ledger opened', 'no running or future artifacts read',
                 'active Grok validation-writer worktree not opened; validation draft not audited', 'no formal tuning, validation or holdout unit consumed',
                 'training not mutated; no reweight/reband proposed mid-campaign'],
    'snapshot': {'observed_utc': snap['observed_utc'], 'attempts': len(snap['attempts']), 'sha256': sha(ROOT / 'report/yield-fair-training-v1/round8-input-snapshot.json')},
    'raw_receipts': {'count': len(raw), 'all_match': all(r['match'] for r in raw), 'manifest': raw},
    'attempt_directories_on_disk_not_listed_in_snapshot_and_not_read': sorted(set(attempt_dirs) - listed),
    'binding_checks': {'count': len(bound), 'all_match': all(b['match'] for b in bound), 'checks': bound},
    'inputs_read': [{'path': p, 'sha256': sha(ROOT / p)} for p in INPUTS],
    'cpu_threads': 1, 'python': str(ROOT / '.venv-contact-six/bin/python'),
    'numbers_recomputed_vs_reported': ('all 25 nominal/disturbed check sets recomputed from rows equal the snapshot flags; all 12 objectives recomputed with the frozen '
                                       '_objective_components equal the snapshot to 0.0; every artifact metrics block equals summarize_trial on its rows; see data/round8-analysis.json'),
    'outputs': outputs,
}
print(json.dumps(receipt, indent=1))
