"""Interactive-origin answers use accepted bindings and the existing comment courier."""
import json
import sqlite3
import subprocess

import pytest

from hermes_cli import kanban_origin_comment as comments
from hermes_cli import kanban_placement as placement
from tests.hermes_cli.test_kanban_origin_ledger import world as _world, publish, accept
from tests.hermes_cli.test_kanban_decision_ask import kanban_home as _kanban_home

world = _world
kanban_home = _kanban_home


@pytest.fixture
def bound(world):
    publish(world[1], world[2])
    accept(world)
    return world


def lookup(bound, **changes):
    args = dict(task_id='t_one', board='build', gateway='origin', profile='default',
                session='s_current', resolve_session=lambda _: 's_current')
    args.update(changes)
    return comments.bound_owner(bound[0], **args)


def test_accepted_binding_resolves_compression_continuation(bound):
    route = lookup(bound)
    assert route['gateway'] == 'owner' and route['profile'] == 'work'
    assert route['board'] == 'build' and route['task_id'] == 't_one'


@pytest.mark.parametrize('change', [dict(task_id='t_other'), dict(board='other'),
    dict(gateway='other'), dict(profile='other'), dict(session='unrelated')])
def test_other_origin_or_target_not_authorized(bound, change):
    assert lookup(bound, **change) is None


@pytest.mark.parametrize('sql', [
    'UPDATE origin_obligations SET released=1',
    "UPDATE origin_obligations SET binding_hash='forged'",
    "UPDATE origin_obligations SET binding_id='forged'",
])
def test_released_or_corrupt_binding_cannot_route(bound, sql):
    with sqlite3.connect(bound[0]) as db:
        db.execute(sql)
    if 'released' in sql:
        assert lookup(bound) is None
    else:
        with pytest.raises(ValueError, match='origin_binding_invalid'):
            lookup(bound)


def test_missing_ledger_does_not_create_one(tmp_path):
    path = tmp_path / 'absent.db'
    assert comments.bound_owner(path, task_id='t_one', board='build', gateway='origin',
        profile='default', session='s_current', resolve_session=lambda x: x) is None
    assert not path.exists()


@pytest.fixture
def courier(monkeypatch):
    rows, calls = {}, []
    monkeypatch.setattr(comments, 'send', comments.send)
    monkeypatch.setattr(placement, '_sent_row', lambda rid: rows.get(rid))
    monkeypatch.setattr('hermes_cli.kanban_capability_registry.hub_cli_path', lambda: '/courier')
    def run(argv, **kw):
        calls.append((argv, kw))
        rows[argv[-1]] = ('queued', None)
        return subprocess.CompletedProcess(argv, 0, 'queued', '')
    monkeypatch.setattr(comments.subprocess, 'run', run)
    return rows, calls


def test_submission_is_pending_until_owner_receipt_and_retries_same_id(bound, courier):
    rows, calls = courier
    route = lookup(bound)
    pending = comments.send(route, session='s_current', body='Choose option B')
    assert pending['status'] == 'comment_pending'
    argv, kw = calls[0]
    assert argv[1:6] == ['request', 'owner', 'tasks', 'work/build/t_one', 'comment']
    assert 'text=Choose option B' in argv
    assert kw['env']['HERMES_AGENT'] == '1'
    assert kw['env']['HERMES_SESSION_ID'] == 's_current'
    assert comments.send(route, session='s_current', body='Choose option B')['request_id'] == pending['request_id']
    rows[pending['request_id']] = ('done', json.dumps({'output': 'Comment added'}))
    assert comments.send(route, session='s_current', body='Choose option B')['status'] == 'comment_applied'
    assert len(calls) == 2  # no re-submission once done


@pytest.mark.parametrize('state', ['rejected', 'failed', 'expired'])
def test_uncertain_terminal_outcome_never_reissues(bound, courier, state):
    rows, calls = courier
    route = lookup(bound)
    first = comments.send(route, session='s_current', body='B')
    rows[first['request_id']] = (state, '{}')
    assert comments.send(route, session='s_current', body='B')['status'] == 'comment_unconfirmed'
    assert len(calls) == 1


def test_timeout_keeps_frozen_id(bound, courier, monkeypatch):
    seen = []
    def timeout(argv, **kw):
        seen.append(argv[-1])
        raise subprocess.TimeoutExpired(argv, 30)
    monkeypatch.setattr(comments.subprocess, 'run', timeout)
    for _ in range(2):
        assert comments.send(lookup(bound), session='s_current', body='B')['status'] == 'comment_pending'
    assert seen[0] == seen[1]


@pytest.mark.parametrize('human', [False, True])
def test_origin_tool_comment_reaches_owner_only_as_agent(kanban_home, world, monkeypatch, tmp_path, human):
    import shutil
    from gateway.session_context import scoped_current_session_id
    from hermes_constants import get_hermes_home
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_origin_adapter as adapter
    from hermes_cli.kanban_comment_origin import find_comment
    from tests.hermes_cli import test_kanban_decision_ask as helpers

    with kbc.connect_closing(tmp_path / 'owner.db') as owner:
        tid = helpers._running(owner)
        binding = world[2]
        binding['owner'].update(task_id=tid, board='default')
        binding['task_record_key'] = f'work/default/{tid}'
        publish(world[1], binding)
        accept(world)
        home = get_hermes_home()
        (home / 'kanban').mkdir(exist_ok=True)
        shutil.copyfile(world[0], home / 'kanban/origin-ledger.db')
        helpers._session('s_old')
        monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
        monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
        rows = {}
        monkeypatch.setattr(placement, '_sent_row', lambda rid: rows.get(rid))
        monkeypatch.setattr('hermes_cli.kanban_capability_registry.hub_cli_path', lambda: '/courier')
        def deliver(argv, **kw):
            # Transport boundary: server stamps the sender; real owner writer and probe read.
            assert kw['env']['HERMES_AGENT'] == '1'
            assert argv[4] == f'work/default/{tid}'
            body = argv[7].removeprefix('text=')
            kb.add_comment(owner, tid, author='hub:gw:origin', body=body,
                writer_kind='agent', writer_session=kw['env']['HERMES_SESSION_ID'], origin_gateway='origin')
            rows[argv[-1]] = ('done', '{}')
            return subprocess.CompletedProcess(argv, 0, '', '')
        monkeypatch.setattr(comments.subprocess, 'run', deliver)
        with scoped_current_session_id('s_old'):
            out = helpers._tool('comment', {'task_id': tid, 'body': 'Choose B', 'relays_user_message': human})
        hit = find_comment(owner, tid, from_gateway='origin', skip_system=True)
        if human:
            assert hit is None
            assert 'origin_comment_human_relay_unsupported' in out.get('error', '')
        else:
            assert out['status'] == 'comment_applied', out
            assert hit['body'] == 'Choose B'
            assert kb.get_task(owner, tid).status == 'running'  # no unblock/status authority


def test_courier_holds_real_drain_lease_and_refuses_closed_latch(bound, courier, monkeypatch, tmp_path):
    from hermes_cli import worker_drain_fence as fence
    from tests.hermes_cli.test_worker_drain_fence import IDENT, req
    fence.install_state(tmp_path, IDENT)
    monkeypatch.setattr(fence, 'admit', lambda purpose: fence.acquire_lease(tmp_path, IDENT, purpose))
    original = comments.subprocess.run
    def observed(*args, **kwargs):
        assert fence.busy_leases(tmp_path) == 1
        return original(*args, **kwargs)
    monkeypatch.setattr(comments.subprocess, 'run', observed)
    comments.send(lookup(bound), session='s_current', body='B')
    assert fence.busy_leases(tmp_path) == 0
    fence.apply_request(tmp_path, IDENT, req('establish', 1))
    with pytest.raises(fence.DrainRefused):
        comments.send(lookup(bound), session='s_current', body='C')
    assert len(courier[1]) == 1


def test_oversize_text_is_refused_not_truncated(bound, courier):
    with pytest.raises(ValueError, match='origin_comment_too_long'):
        comments.send(lookup(bound), session='s_current', body='x' * 8001)
    assert courier[1] == []
