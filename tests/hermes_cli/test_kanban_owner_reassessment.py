"""The staged planner grants no scanner exemption or execution permission."""
import pytest

from hermes_cli import kanban_owner_reassessment as r, kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_origin_worker_custody as custody

worker = custody.worker
UID = '018f051a-657d-7001-8001-000000000001'


def optin():
    return {'version': 1, 'reassess_at': 150,
            'target': {'kind': 'queue', 'identity': 'executor:example', 'revision': 'queue-cut:42'},
            'evidence_refs': [{'table': 'task_events', 'uid': UID}]}


def test_request_copies_without_normalizing_exact_identities():
    value = optin()
    parsed = r.request(value)
    parsed['target']['revision'] = 'different'
    assert value == optin()
    assert r.request(value)['target']['identity'] == 'executor:example'


@pytest.mark.parametrize('change', [
    lambda v: v.update(issuer={'session_id': 'forged'}),
    lambda v: v.update(version=True), lambda v: v.update(reassess_at=True),
    lambda v: v.update(reassess_at=float('nan')), lambda v: v.update(reassess_at=float('inf')),
    lambda v: v.update(reassess_at=0), lambda v: v['target'].update(revision=''),
    lambda v: v['target'].update(kind='clock'), lambda v: v['target'].update(identity=' '),
    lambda v: v['target'].update(authority=True), lambda v: v.update(evidence_refs=[]),
    lambda v: v['evidence_refs'].append(v['evidence_refs'][0]),
    lambda v: v['evidence_refs'][0].update(uid='legacy:1'),
    lambda v: v['evidence_refs'][0].update(table='comments'),
    lambda v: v['evidence_refs'][0].update(issuer='forged'),
])
def test_reject_caller_authority_and_malformed_input(change):
    value = optin()
    change(value)
    with pytest.raises(r.ReassessmentRefused):
        r.request(value)


def test_plan_shortens_dispatcher_wake_and_retains_original_backstop():
    payload = {'wake_at': 200, 'reason': 'queue bounded by existing backstop'}
    producer = {'kind': 'external', 'ref': 'executor:example', 'why': 'queue reassessment'}
    result, backstop = r.plan(optin(), now=100, final_backstop=200, awaiting=producer, payload=payload)
    assert result == {**payload, 'wake_at': 150, 'awaiting': producer}
    assert backstop == 200
    assert payload['wake_at'] == 200
    # An expired absolute reassessment never slides after a restart.
    with pytest.raises(r.ReassessmentRefused):
        r.plan(optin(), now=150, final_backstop=200, awaiting=producer, payload=payload)


@pytest.mark.parametrize('now,backstop', [(150, 200), (151, 200), (100, 149), (True, 200), (100, float('inf'))])
def test_plan_refuses_expired_extended_or_noninteger_deadline(now, backstop):
    with pytest.raises(r.ReassessmentRefused):
        r.plan(optin(), now=now, final_backstop=backstop,
               awaiting={'kind': 'ci', 'ref': 'job:42'}, payload={'wake_at': backstop})


@pytest.mark.parametrize('producer', [None, {}, {'kind': 'clock', 'ref': 'later'},
    {'kind': 'human', 'ref': 'owner'}, {'kind': 'ci', 'ref': ''},
    {'kind': 'deploy', 'ref': 'release:42', 'health': 'healthy'}])
def test_exact_producer_required_not_future_wake_or_prose(producer):
    with pytest.raises(r.ReassessmentRefused):
        r.plan(optin(), now=100, final_backstop=200, awaiting=producer, payload={'wake_at': 200})


def test_changed_final_wake_refuses():
    with pytest.raises(r.ReassessmentRefused):
        r.plan(optin(), now=100, final_backstop=200, awaiting={'kind': 'ci', 'ref': 'job:42'},
               payload={'wake_at': 201})


def test_issuer_refuses_environment_or_shape_only_authority(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_KANBAN_TASK', 't_0000000000000001')
    with kbc.connect_closing(tmp_path / 'fixture.db') as conn, kb.write_txn(conn):
        for candidate in (None, {}, {'creator': 't_0000000000000001'}):
            with pytest.raises(r.ReassessmentRefused, match='private admitted'):
                r.issuer(candidate, conn, 't_0000000000000001')


@pytest.mark.parametrize('change', ['none', 'missing_admission', 'wrong_session', 'wrong_task', 'ended_run'])
def test_issuer_uses_real_facade_and_current_claim(worker, monkeypatch, change):
    from agent.origin_worker_custody import capture

    agent, _, _, context, _ = worker
    observed = []
    def loop(*args, **kwargs):
        cap = capture(context['task_id'])
        assert cap is not None
        with kbc.connect_closing() as conn, kb.write_txn(conn):
            if change != 'missing_admission':
                kb._append_event(conn, context['task_id'], 'owner_admitted',
                    {'session_id': 'wrong-session' if change == 'wrong_session' else agent.session_id},
                    run_id=context['run_id'])
            if change == 'ended_run':
                conn.execute("UPDATE task_runs SET status='scheduled',ended_at=1 WHERE id=?", (context['run_id'],))
            tid = 't_0000000000000001' if change == 'wrong_task' else context['task_id']
            if change == 'none':
                observed.append(r.issuer(cap, conn, tid))
            else:
                with pytest.raises(r.ReassessmentRefused):
                    r.issuer(cap, conn, tid)
        return dict(final_response='ok', messages=[], failed=False)
    monkeypatch.setattr('agent.conversation_loop.run_conversation', loop)
    agent.run_conversation('bounded owner reassessment fixture')
    if change == 'none':
        assert len(observed) == 1
        assert observed[0]['session_id'] == agent.session_id
        assert observed[0]['profile'] == 'default'
        assert all(r._uid(observed[0][key]) for key in ('run_uid', 'owner_admitted_uid', 'worker_bound_uid'))
