"""A creator must not park on a product key whose creation its own run saw definitively refused.

The courier rejects an origin forward with ``origin_forward_binding_missing`` asynchronously, after
``kanban_create`` already returned a queued ``wait-check key`` handle.  The pending-admission gate
(``origin_forward_run_end``) intentionally lets a definitive refusal end the run, so without a
second gate the creator parked for 24 h on a card that can never appear.  These drive the real
lifecycle entry points against a real courier journal; nothing here mints origin authority.
"""
import json
import shlex
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_continuation as continuation
from hermes_cli.kanban_origin_forward_wait import wait_keys
from tests.hermes_cli.test_origin_forward_run_end import pending as pending  # noqa: PLC0414 -- pytest fixture reexport
from tests.hermes_cli.test_kanban_continuation import home as home  # noqa: PLC0414 -- pytest fixture reexport

KEY = 'bb-land:817:0123456789abcdef'
REJECTED = {'status': 'rejected', 'code': 'origin_forward_binding_missing', 'handoff_complete': False}
ORIGIN = {'gateway': 'hub', 'profile': 'default', 'session': 'chat'}
UNTIL = 'SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the child card'
AWAITING = {'kind': 'external', 'ref': 'bb-land child', 'why': 'the child card is created by the hub intake'}


def wake(key=KEY, gateway='hub'):
    return shlex.join(['hermes', 'kanban', 'wait-check', 'key', key, '--gateway', gateway])


def journal(path, rid, *, state='rejected', result=REJECTED, key=KEY, run='run-current', task=None,
            creator=None, action='origin-forward', coll='sessions'):
    """One courier row for a creator's keyed child creation."""
    creator = creator or {'gateway': 'hub', 'profile': 'default', 'board': 'default', 'task_id': task}
    body = {'id': rid, 'from': 'gw:worker', 'coll': coll, 'action': action,
            'args': {'origin': ORIGIN, 'creator': creator, 'run_uid': run,
                     'create': {'idempotency_key': key, 'title': 'child'}}}
    with sqlite3.connect(path) as db:
        db.execute('INSERT OR REPLACE INTO sent VALUES(?,?,?,?)', (rid, state, json.dumps(result), json.dumps(body)))


@pytest.fixture
def refused(pending):
    task, run, path, _ = pending
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sent')
    journal(path, 'placement:worker:refused', task=task)
    return task, run, path


def park(conn, how, task, run, *, wake_check=None, awaiting=AWAITING):
    wake_check = wake_check or wake()
    if how == 'schedule':
        return kb.schedule_task(conn, task, reason=UNTIL, expected_run_id=run, wake_check=wake_check,
                                check_every=300, awaiting=awaiting)
    if how == 'block':
        return kb.block_task(conn, task, reason=UNTIL, kind='time', expected_run_id=run, wake_check=wake_check,
                             check_every=300, awaiting=awaiting)
    return continuation.continue_task(conn, task, checkpoint='waiting for the child card', expected_run_id=run,
                                      resume_after_seconds=86400, wake_check=wake_check, check_every=300,
                                      awaiting=awaiting)


def parked(result):
    return bool(result['ok'] if isinstance(result, dict) else result)


def refusal(how, conn, task, run, **kw):
    """Run the park; return its refusal text, or None when it parked."""
    try:
        out = park(conn, how, task, run, **kw)
    except ValueError as exc:
        return str(exc)
    if isinstance(out, dict) and not out.get('ok'):
        return json.dumps(out)
    return None if parked(out) else 'not parked'


def assert_still_running(conn, task, run):
    assert kb.get_task(conn, task).status == 'running'
    assert kb._current_run_id(conn, task) == run
    assert kb.latest_run(conn, task).ended_at is None


@pytest.mark.parametrize('how', ['schedule', 'block', 'continue'])
def test_park_on_the_key_of_a_definitively_rejected_create_is_refused(refused, how):
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        why = refusal(how, conn, task, run)
        assert why and 'origin_forward_rejected_wait' in why, why
        assert KEY in why and 'placement:worker:refused' in why and 'origin_forward_binding_missing' in why
        # The creator stays alive to report the loss; nothing was parked or written.
        assert_still_running(conn, task, run)
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind IN ('scheduled','continued')").fetchone()[0] == 0


@pytest.mark.parametrize('how', ['schedule', 'block', 'continue'])
def test_wait_on_the_key_of_an_unrelated_creation_still_parks(refused, how):
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        assert refusal(how, conn, task, run, wake_check=wake('bb-land:817:other')) is None
        assert kb.get_task(conn, task).status == 'scheduled'


def test_refused_child_does_not_block_unrelated_waits(refused):
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        assert kb.schedule_task(conn, task, reason='pure calendar window', expected_run_id=run, clock_only=True,
                                awaiting={'kind': 'clock', 'why': 'calendar window window'})
        assert kb.get_task(conn, task).status == 'scheduled'


OTHER_CREATOR = {'gateway': 'hub', 'profile': 'default', 'board': 'default', 'task_id': 't_other'}
# change -> (journal kwargs, REJECTED overrides, whether the unsettled-request pending gate holds the run)
CHANGES = {
    'sent': ({'state': 'sent'}, {}, True),
    'queued': ({'state': 'queued'}, {}, True),
    'local': ({'state': 'local'}, {}, True),
    'failed': ({'state': 'failed'}, {}, True),
    'expired': ({'state': 'expired'}, {}, True),
    'done': ({'state': 'done'}, {}, False),
    'other-code': ({}, {'code': 'origin_forward_lease_invalid'}, False),
    'wrong-status': ({}, {'status': 'prepared'}, False),
    'handoff-complete': ({}, {'handoff_complete': True}, False),
    'no-result': ({'result': {}}, {}, False),
    'other-run': ({'run': 'an-earlier-run'}, {}, False),
    'other-task': ({'creator': OTHER_CREATOR}, {}, False),
    'other-action': ({'action': 'create'}, {}, False),
    'other-coll': ({'coll': 'tasks'}, {}, False),
    'keyless': ({'key': None}, {}, False),
}


@pytest.mark.parametrize('name', list(CHANGES))
def test_only_this_runs_definitive_binding_refusal_forbids_the_wait(refused, name):
    task, run, path = refused
    kwargs, overrides, pending_holds = CHANGES[name]
    journal(path, 'placement:worker:refused', task=task, **{'result': {**REJECTED, **overrides}, **kwargs})
    with kbc.connect_closing() as conn:
        why = refusal('schedule', conn, task, run) or ''
        assert 'origin_forward_rejected_wait' not in why
        if pending_holds:
            # An unsettled request still holds the run (pending gate), never the new gate.
            assert 'origin_forward_pending' in why
        else:
            assert why == '' and kb.get_task(conn, task).status == 'scheduled'


def test_malformed_courier_body_is_not_evidence(refused):
    task, run, path = refused
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sent SET body='not json' WHERE id='placement:worker:refused'")
    with kbc.connect_closing() as conn:
        assert refusal('schedule', conn, task, run) is None


def test_same_key_recovery_from_a_later_run_may_be_waited_on(refused):
    """After the supported later-run retry creates a new transport request for the same key."""
    task, run, path = refused
    journal(path, 'placement:worker:retry', state='sent', result={}, task=task)
    with kbc.connect_closing() as conn:
        # The retry is still unsettled: the pending gate keeps the run, it is not a rejected wait.
        assert 'origin_forward_pending' in (refusal('schedule', conn, task, run) or '')
    journal(path, 'placement:worker:retry', state='done', result={}, task=task)
    with kbc.connect_closing() as conn:
        assert refusal('schedule', conn, task, run) is None
        assert kb.get_task(conn, task).status == 'scheduled'


def test_a_later_recovery_row_of_the_same_run_supersedes_the_refusal(refused):
    task, run, path = refused
    journal(path, 'placement:worker:recovery', state='done', result={}, task=task)
    with kbc.connect_closing() as conn:
        assert refusal('continue', conn, task, run) is None


@pytest.mark.parametrize('probe', [
    'hermes kanban wait-check task t_0123456789abcdef --gateway hub',
    'hermes kanban wait-check key --gateway hub',
    "hermes kanban wait-check key 'unterminated",
    'hermes kanban wait-check board-empty default --gateway hub',
    f'hermes kanban wait-check task {KEY} --gateway hub',
])
def test_other_probes_are_not_key_waits(refused, probe):
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        why = refusal('schedule', conn, task, run, wake_check=probe)
        assert 'origin_forward_rejected_wait' not in (why or '')


def test_key_named_in_a_longer_probe_is_still_refused(refused):
    task, run, _ = refused
    longer = wake() + ' --until done,review --board default'
    with kbc.connect_closing() as conn:
        assert 'origin_forward_rejected_wait' in (refusal('schedule', conn, task, run, wake_check=longer) or '')


OTHER_KEY = 'bb-land:817:other'


@pytest.mark.parametrize('order', ['lost-last', 'lost-first'])
@pytest.mark.parametrize('how', ['schedule', 'block', 'continue'])
def test_lost_key_in_a_conjunction_is_refused_wherever_it_sits(refused, how, order):
    """``wait-check key OTHER && wait-check key LOST`` can never fire: the lost key is not just the first one."""
    task, run, _ = refused
    first, second = (wake(OTHER_KEY), wake()) if order == 'lost-last' else (wake(), wake(OTHER_KEY))
    with kbc.connect_closing() as conn:
        why = refusal(how, conn, task, run, wake_check=f'{first} && {second}')
        assert 'origin_forward_rejected_wait' in (why or ''), why
        assert KEY in why
        assert_still_running(conn, task, run)


SPACING = [('{a} {op} {b}'), ('{a}{op} {b}'), ('{a} {op}{b}'), ('{a}{op}{b}')]


@pytest.mark.parametrize('order', ['lost-first', 'lost-last'])
@pytest.mark.parametrize('spacing', SPACING, ids=['both', 'left', 'right', 'none'])
@pytest.mark.parametrize('joiner', ['||', '|', '&'])
def test_a_probe_with_an_alternative_can_fire_without_the_lost_key_so_it_still_parks(refused, joiner, spacing, order):
    """``||``, ``|`` and ``&`` can fire without the lost key, whichever side it is on and however spaced."""
    task, run, _ = refused
    first, second = (KEY, OTHER_KEY) if order == 'lost-first' else (OTHER_KEY, KEY)
    probe = spacing.format(a=wake(first), op=joiner, b=wake(second))
    assert wait_keys(probe) == [], probe
    with kbc.connect_closing() as conn:
        assert refusal('schedule', conn, task, run, wake_check=probe) is None, probe
        assert kb.get_task(conn, task).status == 'scheduled'


@pytest.mark.parametrize('spacing', SPACING, ids=['both', 'left', 'right', 'none'])
def test_a_conjunction_is_judged_on_every_key_however_it_is_spaced(spacing):
    probe = spacing.format(a=wake(OTHER_KEY), op='&&', b=wake())
    assert wait_keys(probe) == [OTHER_KEY, KEY], probe
    assert wait_keys(spacing.format(a=wake(), op='&&', b=wake(OTHER_KEY))) == [KEY, OTHER_KEY]


@pytest.mark.parametrize('probe, keys', [
    (f'{wake(OTHER_KEY)}; {wake()}', [KEY]),                      # the exit status is the last command's
    (f'{wake(OTHER_KEY)};{wake()}', [KEY]),
    (f'{wake(OTHER_KEY)}\n{wake()}', [KEY]),
    (f'{wake()}; {wake(OTHER_KEY)}', [OTHER_KEY]),
    (f'{wake()};{wake(OTHER_KEY)}', [OTHER_KEY]),
    (f'{wake()}\n{wake(OTHER_KEY)}', [OTHER_KEY]),
    (f'{wake()}; true', []),
    (f'{wake()};', [KEY]),                                        # a trailing separator decides nothing
    (f'{wake()}\n', [KEY]),
    (f'{wake()} && {wake(OTHER_KEY)}; {wake(OTHER_KEY)} && {wake()}', [OTHER_KEY, KEY]),
    (f'{wake()} > /dev/null 2>&1', [KEY]),                        # redirects change no exit status
    (f'{wake()} &> /dev/null', [KEY]),
    (f'{wake()}||true', []),
    (f'({wake()})', []),
    (f"{wake()} || true", []),
    (f'{wake()} && true', [KEY]),
])
def test_wait_keys_reads_the_keys_the_exit_status_depends_on(probe, keys):
    assert wait_keys(probe) == keys, probe


def test_a_quoted_operator_in_a_key_is_one_word():
    assert wait_keys("hermes kanban wait-check key 'a;b' --gateway hub") == ['a;b']
    assert wait_keys('hermes kanban wait-check key a#b --gateway hub') == ['a#b']


@pytest.mark.parametrize('joiner', ['; ', '\n', ';'])
def test_the_lost_key_before_a_separator_does_not_refuse_but_after_it_does(refused, joiner):
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        assert refusal('schedule', conn, task, run, wake_check=f'{wake()}{joiner}{wake(OTHER_KEY)}') is None
        assert kb.get_task(conn, task).status == 'scheduled'


@pytest.mark.parametrize('joiner', ['; ', '\n', ';', ' &&', '&& ', '&&'])
def test_the_lost_key_after_a_separator_or_conjunction_refuses(refused, joiner):
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        why = refusal('schedule', conn, task, run, wake_check=f'{wake(OTHER_KEY)}{joiner}{wake()}')
        assert 'origin_forward_rejected_wait' in (why or ''), why


def test_another_creators_completed_request_for_the_key_lifts_the_refusal(refused):
    """The key may exist (another creator made it): never claim ``wait-check key`` can not see it."""
    task, run, path = refused
    journal(path, 'placement:other:done', state='done', result={}, creator=OTHER_CREATOR)
    with kbc.connect_closing() as conn:
        assert refusal('schedule', conn, task, run) is None
        assert kb.get_task(conn, task).status == 'scheduled'


def test_another_creators_inflight_request_for_the_key_lifts_the_refusal_without_a_pending_hold(refused):
    task, run, path = refused
    journal(path, 'placement:other:sent', state='sent', result={}, creator=OTHER_CREATOR)
    with kbc.connect_closing() as conn:
        assert 'origin_forward_rejected_wait' not in (refusal('schedule', conn, task, run) or '')


def test_another_creators_own_binding_refusal_for_the_key_does_not_lift_it(refused):
    task, run, path = refused
    journal(path, 'placement:other:refused', creator=OTHER_CREATOR)
    with kbc.connect_closing() as conn:
        assert 'origin_forward_rejected_wait' in (refusal('schedule', conn, task, run) or '')


@pytest.mark.parametrize('handoff', ['absent', 'null'])
def test_a_refusal_without_an_explicit_incomplete_handoff_is_not_definitive(refused, handoff):
    task, run, path = refused
    result = {k: v for k, v in REJECTED.items() if k != 'handoff_complete'}
    if handoff == 'null':
        result['handoff_complete'] = None
    journal(path, 'placement:worker:refused', task=task, result=result)
    with kbc.connect_closing() as conn:
        assert refusal('schedule', conn, task, run) is None
        assert kb.get_task(conn, task).status == 'scheduled'


def test_a_non_forward_courier_row_mentioning_the_forward_does_not_lift_the_refusal(refused):
    """Only ``origin-forward`` requests are evidence: an unrelated action naming it is neither a recovery nor a refusal."""
    task, run, path = refused
    journal(path, 'placement:worker:other-action', state='done', result={}, task=task, action='create')
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sent SET body=replace(body, '\"child\"', '\"origin-forward\"') WHERE id=?",
                   ('placement:worker:other-action',))
        assert db.execute("SELECT COUNT(*) FROM sent WHERE body LIKE '%origin-forward%'").fetchone()[0] == 2
    with kbc.connect_closing() as conn:
        assert 'origin_forward_rejected_wait' in (refusal('schedule', conn, task, run) or '')


@pytest.mark.parametrize('damage', ['missing', 'unreadable', 'no-table'])
def test_missing_or_unreadable_evidence_is_not_a_refusal(refused, damage):
    task, run, path = refused
    if damage == 'missing':
        path.unlink()
    elif damage == 'unreadable':
        path.write_bytes(b'not a database')
    else:
        path.unlink()
        sqlite3.connect(path).close()
    with kbc.connect_closing() as conn:
        why = refusal('schedule', conn, task, run)
        assert 'origin_forward_rejected_wait' not in (why or '')


def test_board_without_portable_run_identity_is_not_judged(refused):
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        conn.execute('ALTER TABLE task_runs RENAME COLUMN uid TO legacy_uid')
        conn.commit()
        assert refusal('schedule', conn, task, run) is None


def test_ended_run_has_no_run_identity_to_judge(refused):
    from hermes_cli.kanban_origin_forward_wait import rejected_wait
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        assert rejected_wait(conn, task, wake()) is not None
        conn.execute('UPDATE task_runs SET ended_at=1 WHERE id=?', (run,))
        conn.commit()
        assert rejected_wait(conn, task, wake()) is None


def test_completion_is_never_blocked_by_the_new_gate(refused):
    """A creator may still finish and report; the owning chat already holds the rejection notice."""
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        assert kb.complete_task(conn, task, summary='child not created: origin_forward_binding_missing',
                                expected_run_id=run)


def test_kernel_forced_park_naming_the_lost_key_remains_possible(refused):
    from hermes_cli.kanban_workspace_publication import involuntary_park
    task, run, _ = refused
    with kbc.connect_closing() as conn:
        assert involuntary_park(task, kb.schedule_task, conn, task, expected_run_id=run, reason=UNTIL,
                                wake_check=wake(), check_every=300, awaiting=AWAITING)
        assert kb.latest_run(conn, task).ended_at is not None


def _create(placement, conn, task, run_uid):
    creator = {'gateway': 'hub', 'profile': 'default', 'board': 'default', 'task_id': task}
    return placement.create_remote({'title': 'child', 'assignee': 'default', 'idempotency_key': KEY},
                                   target='hub', self_gw='worker', origin_session='chat',
                                   origin_profile='default', origin_gateway='hub', creator=creator,
                                   run_uid=run_uid, cfg={}, conn=conn)


def test_real_create_then_async_binding_refusal_cannot_be_waited_on_but_later_run_recovery_can(pending, monkeypatch):
    """The reproduced incident: queued handle -> courier refusal -> park; then the supported same-key retry."""
    from hermes_cli import kanban_placement as placement
    from hermes_cli.kanban_creation_wait import queued_wait
    task, run, path, _ = pending
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sent WHERE 1=1')
    current = ['run-current']

    def enqueue(argv):
        # Queued, not yet judged by the origin gateway: how the courier journals a fresh forward.
        journal(path, argv[argv.index('--id') + 1], state='sent', result={}, task=task, run=current[0])
        return 0, '', ''
    monkeypatch.setattr(placement, 'hub_carries_options', lambda cli: True)
    monkeypatch.setattr(placement, 'hub_runner', enqueue)
    handle = queued_wait({'idempotency_key': KEY}, 'hub', 'queued')['wake_check']
    assert handle == wake()
    with kbc.connect_closing() as conn:
        first = _create(placement, conn, task, 'run-current')
        assert first['ok'], first
        rid = first['request_id']
        # While the courier has not judged the forward, the pending gate (not this one) holds the run.
        assert 'origin_forward_pending' in (refusal('schedule', conn, task, run, wake_check=handle) or '')
        with sqlite3.connect(path) as db:
            db.execute("UPDATE sent SET state='rejected', result=?", (json.dumps(REJECTED),))
        why = refusal('schedule', conn, task, run, wake_check=handle)
        assert why and 'origin_forward_rejected_wait' in why and rid in why, why
        assert_still_running(conn, task, run)
        # A later authenticated run retries the same product key under its own transport identity.
        conn.execute("UPDATE task_runs SET uid='run-two' WHERE id=?", (run,))
        conn.commit()
        current[0] = 'run-two'
        second = _create(placement, conn, task, 'run-two')
        assert second['ok'] and second['request_id'] != rid, second
        assert 'origin_forward_pending' in (refusal('schedule', conn, task, run, wake_check=handle) or '')
        with sqlite3.connect(path) as db:
            db.execute("UPDATE sent SET state='done', result='{}' WHERE id=?", (second['request_id'],))
        assert refusal('schedule', conn, task, run, wake_check=handle) is None
        assert kb.get_task(conn, task).status == 'scheduled'


@pytest.mark.parametrize('tool', ['kanban_schedule', 'kanban_block', 'kanban_continue'])
def test_worker_tools_refuse_the_lost_key_and_report_why(refused, monkeypatch, tool):
    from tools import kanban_tools as kt
    task, run, _ = refused
    monkeypatch.setenv('HERMES_KANBAN_TASK', task)
    monkeypatch.setenv('HERMES_KANBAN_RUN_ID', str(run))
    monkeypatch.setenv('HERMES_KANBAN_WAKE_DRYRUN', '0')
    args = {'wake_check': wake(), 'check_every': 300, 'awaiting': AWAITING}
    if tool == 'kanban_continue':
        out = json.loads(kt._handle_continue({**args, 'checkpoint': 'waiting for the child card',
                                              'resume_after_seconds': 86400}))
    elif tool == 'kanban_block':
        out = json.loads(kt._handle_block({**args, 'reason': UNTIL, 'kind': 'time'}))
    else:
        out = json.loads(kt._handle_schedule({**args, 'reason': UNTIL}))
    assert not out.get('ok') and out.get('status') != 'scheduled', out
    assert 'origin_forward_rejected_wait' in json.dumps(out), out
    with kbc.connect_closing() as conn:
        assert_still_running(conn, task, run)
