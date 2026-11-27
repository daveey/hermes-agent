"""A worker whose only owner chat is its own gateway's standing owner cannot file a canonical-home child.

The standing owner (``kanban_owner_*``) is a producer fallback, not a conversation: a same-gateway link never
supplies the child's origin session (that would mint custody nothing attested). The create must therefore say
what to do, once, before any courier request exists, instead of three opaque ``canonical_home_origin_required``
refusals from the hub. Foreign-owner forwarding and the headless-session branch are unchanged.
"""
import json

import pytest

from hermes_cli import kanban_creation_home as creation_home
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_owner_chat as owner
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_provenance as provenance
from tests.tools import test_kanban_cross_board_creator_chats as helpers
from tools import kanban_tools as tools

home = helpers.home

STANDING = owner.PREFIX + 'terminal'
REQUEST = {'title': 'Independent exact-head review PR #123 @' + 'a' * 10,
           'body': 'Review exact head ' + 'a' * 40 + '; require independent verdict and required CI.',
           'assignee': 'default', 'skills': ['sdlc-review'], 'runs_on': ['pool:worker'], 'requires': [],
           'project': '', 'idempotency_key': 'core-wt-review:123:' + 'a' * 40}


@pytest.fixture
def courier(home, monkeypatch):
    """Real kanban_create planning and argv; the hub's installed canonical-home admission is the seam."""
    monkeypatch.delenv('HERMES_ORIGIN_SESSION', raising=False)
    monkeypatch.setenv('HERMES_SESSION_ID', helpers.WORKER)
    monkeypatch.setenv('HERMES_SESSION_KEY', helpers.WORKER)
    monkeypatch.setattr(kp, 'config', lambda: {'worker_queue': {'here': 'pool:worker'}})
    monkeypatch.setattr(kp, 'linux_pool_gateway', lambda *a: True)
    monkeypatch.setattr(kp, 'remote_state', lambda rid: None)
    monkeypatch.setattr(kp, 'await_task_id', lambda *a, **k: ('t_review', 'ready'))
    sent = []

    def run(argv):
        if argv[-1] == '--help':
            return 0, '--option --canonical-home canonical-home-v2', ''
        sent.append(argv)
        if not argv[argv.index('--origin-session') + 1]:
            return 2, '', 'canonical_home_origin_required: complete payload and tracked origin required'
        return 0, 'queued', ''

    kp._help_cache.clear()
    monkeypatch.setattr(kp, 'hub_runner', run)
    return sent


def _creator(gateway, session, monkeypatch):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='Shepherd', assignee='default', session_id=helpers.WORKER,
                                 settle_owner_chat=False)
        helpers._link(conn, creator, gateway, 'default', session)
        assert kb.claim_task(conn, creator) is not None
        conn.execute('UPDATE task_runs SET uid=? WHERE task_id=?', ('authenticated-run', creator))
        conn.commit()
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    return creator


def _tasks():
    with kbc.connect_closing() as conn:
        return [t.id for t in kb.list_tasks(conn)]


def test_same_gateway_standing_owner_refuses_once_with_the_remedy(courier, monkeypatch):
    creator = _creator('here', STANDING, monkeypatch)
    for _ in range(2):  # identical retries stay one pre-courier refusal, never a request
        out = json.loads(tools._handle_create(dict(REQUEST)))
        assert out.get('created') is False, out
        assert 'canonical_home_origin_required' not in out['error']
        assert STANDING in out['error'] and 'here' in out['error']
        assert 'different gateway' in out['error'] and 'move' in out['error'].lower()
        assert out.get('code') == 'origin_same_gateway_standing_owner'
    assert courier == []  # nothing was queued: the frozen idempotency key stays free for the foreign worker
    assert _tasks() == [creator]


def test_same_gateway_standing_owner_replica_only_still_refuses(courier, monkeypatch):
    """The remedy comes from the same owner resolution as the origin lookup, so it cannot disagree with it."""
    from hermes_cli import kanban_creator_chats as chats
    creator = _creator('here', STANDING, monkeypatch)
    monkeypatch.setattr(chats, '_replica_chats', lambda *a: [
        {'chat_gateway': 'here', 'chat_profile': 'default', 'chat_session': STANDING}])
    out = json.loads(tools._handle_create(dict(REQUEST)))
    assert out.get('code') == 'origin_same_gateway_standing_owner', out
    assert courier == [] and _tasks() == [creator]


def test_foreign_standing_owner_still_forwards_with_authority(courier, monkeypatch):
    creator = _creator('away', STANDING, monkeypatch)
    monkeypatch.setattr(provenance, 'forwarding_authority', lambda *a, **k: {
        'creator': {'gateway': 'away', 'profile': 'default', 'board': 'default', 'task_id': creator},
        'run_uid': 'authenticated-run'})
    out = json.loads(tools._handle_create(dict(REQUEST)))
    assert len(courier) == 1, out
    argv = courier[0]
    assert argv[argv.index('--origin-session') + 1] == STANDING
    assert argv[argv.index('--origin-gateway') + 1] == 'away'
    assert argv[argv.index('--creator-run') + 1] == 'authenticated-run'


def test_headless_worker_without_owner_keeps_the_hub_refusal(courier, monkeypatch):
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='Unowned', assignee='default', session_id=helpers.WORKER,
                                 settle_owner_chat=False)
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    out = json.loads(tools._handle_create(dict(REQUEST)))
    assert len(courier) == 1 and out.get('created') is False, out
    assert 'canonical_home_origin_required' in out['error']
    assert out.get('code') != 'origin_same_gateway_standing_owner'


def test_creator_with_a_persisted_chat_session_still_sends_that_origin(courier, monkeypatch):
    """Control for the ``session_id`` guard: the origin exists (the creator's own chat), so the create is valid."""
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='Shepherd', assignee='default', session_id=helpers.CHAT,
                                 settle_owner_chat=False)
        helpers._link(conn, creator, 'here', 'default', STANDING)
        assert kb.claim_task(conn, creator) is not None
        conn.commit()
    monkeypatch.setenv('HERMES_KANBAN_TASK', creator)
    out = json.loads(tools._handle_create(dict(REQUEST)))
    assert out.get('code') != 'origin_same_gateway_standing_owner', out
    assert len(courier) == 1, out
    assert courier[0][courier[0].index('--origin-session') + 1] == helpers.CHAT


def test_non_canonical_remote_create_is_never_pre_refused(courier, monkeypatch):
    """Control for the ``home is None`` guard: the hub accepts an empty origin for a non-canonical create."""
    real = creation_home.prepare

    def prepare(view, **kw):
        _, v, _ = real(view, **kw)
        return None, v, {'where': 'otherbox'}

    monkeypatch.setattr(creation_home, 'prepare', prepare)
    _creator('here', STANDING, monkeypatch)
    out = json.loads(tools._handle_create(dict(REQUEST)))
    assert out.get('code') != 'origin_same_gateway_standing_owner', out
    assert len(courier) == 1, out
    assert '--canonical-home' not in courier[0]


def test_origin_lookup_contract_is_unchanged(home):
    from tools.kanban_tools_placement import _placement_origin
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title='Shepherd', assignee='default', session_id=helpers.WORKER,
                                 settle_owner_chat=False)
        helpers._link(conn, creator, 'here', 'default', STANDING)
        assert _placement_origin(conn, creator, helpers.WORKER) == {'session_id': None, 'origin_profile': None}
