"""Single-dispatch and cleanup checks; no hardware endpoints."""
from dataclasses import replace
from types import SimpleNamespace

import pytest
from step6_figure8_autotune_v1.single_trial import execute_single_trial
from step6_figure8_autotune_v1.v5_campaign import ENTRY_MODE_HOME_ONLY_V1
from step6_figure8_autotune_v1.v5_lifecycle_ledger import LedgerRole
from test_step6_autotuner_v5_live_owner import _plan


def run(tmp_path, *, fail=False, invalid=False, existing=False):
    calls = []
    plan = _plan(1, 2.0)
    if not invalid:
        plan = replace(plan, requires_home=True, packable=False,
                       entry_mode=ENTRY_MODE_HOME_ONLY_V1)
    output = tmp_path / 'result.json'
    if existing:
        output.write_text('{}')
    def context_factory(**kwargs):
        calls.append(('open', kwargs))
        return SimpleNamespace(close=lambda: calls.append(('close',)))
    class Owner:
        def __init__(self, context):
            self.context = context
        def execute_chain(self, request):
            calls.append(('dispatch', request))
            if fail:
                raise RuntimeError('measured failure')
            return 'sealed'
    args = dict(plan=plan, run_dir=tmp_path, result_path=output,
                campaign_fingerprint='a'*64, release_identity_sha256='b'*64,
                role=LedgerRole.PRIMARY, launch_profile=tmp_path/'launch.json',
                controller_host='not-connected', kunwei_host='not-connected',
                context_factory=context_factory, owner_factory=Owner)
    return calls, args


def test_single_dispatch_closes_and_preserves_exact_plan(tmp_path):
    calls, args = run(tmp_path)
    assert execute_single_trial(**args) == 'sealed'
    assert [row[0] for row in calls] == ['open', 'dispatch', 'close']
    assert calls[1][1].plans == (args['plan'],)
    assert calls[0][1]['expected_release_identity_sha256'] == 'b'*64


def test_failure_closes_without_retry(tmp_path):
    calls, args = run(tmp_path, fail=True)
    with pytest.raises(RuntimeError, match='measured failure'):
        execute_single_trial(**args)
    assert [row[0] for row in calls] == ['open', 'dispatch', 'close']


@pytest.mark.parametrize('flag', ['invalid', 'existing'])
def test_rejects_before_open(tmp_path, flag):
    calls, args = run(tmp_path, **{flag: True})
    with pytest.raises((ValueError, FileExistsError)):
        execute_single_trial(**args)
    assert calls == []


def test_existing_owner_completes_one_full_period_and_home(tmp_path):
    from test_step6_autotuner_v5_live_owner import (
        _Transport, _Writer, _Backend, LifecycleTrace,
        V5LifecycleTraceAdapter, V5LiveContextV1,
        LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH,
    )
    transport = _Transport()
    writer = _Writer(transport)
    closed = []
    context = V5LiveContextV1(
        writer=writer, transport=transport,
        trace=V5LifecycleTraceAdapter(LifecycleTrace(
            tmp_path/'life', path_min_duration_s=59.5,
            coverage_profile=LIFECYCLE_COVERAGE_PROFILE_V5_IMMEDIATE_PATH)),
        backend_factory=lambda spec: _Backend(), anchor_pose=(0.,)*6,
        close_callback=lambda: closed.append(True),
    )
    _, args = run(tmp_path)
    args['context_factory'] = lambda **kwargs: context
    args.pop('owner_factory')
    result = execute_single_trial(**args)
    assert len(result.records) == len(result.chain.attempts) == 1
    assert result.chain.attempts[0].boundary.mode.value == 'HOME'
    assert result.lifecycle_receipt['coverage_complete'] is True
    assert writer.session.finished is True
    assert writer.failed_closed is None
    assert closed == [True]
