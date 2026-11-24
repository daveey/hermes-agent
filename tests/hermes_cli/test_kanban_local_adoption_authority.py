"""Explicit local adoption of an ownerless card confers real renewal-reader authority (resolve_actor)."""
import argparse
import json
import sqlite3

import pytest

from hermes_cli import kanban_chat_links as chat_links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_local_adoption as local
from hermes_cli import kanban_origin_owner as owner
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_scope_renewal import RenewalRefused
from hermes_cli.kanban_scope_renewal_cli import resolve_actor
from tests.hermes_cli import kanban_sync_support as support

SID = '20261123_101010_abcdef'
GW = 'gw-mac'
WRITER = ('agent', SID, '4242:HERMES_SESSION_ID')


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_KANBAN_DB',
                'HERMES_KANBAN_BOARD', 'HERMES_DELEGATED_CHILD_CONTEXT'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_SESSION_ID', SID)
    monkeypatch.setattr('hermes_cli.kanban_origin.gateway_names', lambda: (GW, {GW}))
    with sqlite3.connect(home / 'state.db') as db:
        db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, created_source TEXT)')
        db.execute("INSERT INTO sessions VALUES (?,'desktop','desktop')", (SID,))
    conn = kbc.connect(home / 'kanban.db')
    client.ensure_identity(conn, 'default', GW)
    from hermes_cli import kanban_db_sync as sync
    sync.set_mode(conn, 'primary')
    # Legacy ownerless card: created_by=claude-code-chat, no session, owner or origin.
    tid = kb.create_task(conn, title='legacy', created_by='claude-code-chat')
    with kb.write_txn(conn):
        conn.execute('UPDATE tasks SET session_id=NULL WHERE id=?', (tid,))
    try:
        yield conn, tid, home
    finally:
        conn.close()


def adopt(tid, capsys):
    owner.command(argparse.Namespace(kanban_action='origin-adopt', local=True, task_id=tid,
                                     request_file=None, board='default'))
    return json.loads(capsys.readouterr().out)


def actor(conn, tid, session=SID):
    return resolve_actor(conn, tid, writer=('agent', session, '4242:HERMES_SESSION_ID'),
                         environs=[{'HERMES_SESSION_ID': session}], gateway=GW, profile='default')


def links(conn, tid):
    return [dict(r) for r in conn.execute('SELECT * FROM task_chat_links WHERE task_id=?', (tid,))]


def test_before_adoption_the_ownerless_card_has_no_authority(world):
    conn, tid, _ = world
    with pytest.raises(RenewalRefused) as err:
        actor(conn, tid)
    assert err.value.code == 'not_scope_authority'


def test_observer_subscription_alone_never_confers(world):
    from hermes_cli.kanban_db_notify import add_notify_sub
    conn, tid, _ = world
    add_notify_sub(conn, task_id=tid, platform='tui', chat_id=SID, delivery_metadata={'reason': 'shepherd'})
    with pytest.raises(RenewalRefused) as err:
        actor(conn, tid)
    assert err.value.code == 'not_scope_authority'


def test_adoption_confers_real_authority_without_changing_the_task_row(world, capsys):
    conn, tid, _ = world
    before = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert adopt(tid, capsys)['adopted'] is True
    got = actor(conn, tid)
    assert (got.kind, got.authority, got.session) == ('agent', 'chat', SID)
    created = [l for l in links(conn, tid) if l['kind'] == 'created' and not l['released']]
    assert [(l['chat_gateway'], l['chat_session']) for l in created] == [(GW, SID)]
    after = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
    assert {k: v for k, v in after.items() if k not in ('sync_version', 'updated_at', 'hlc')} == \
        {k: v for k, v in before.items() if k not in ('sync_version', 'updated_at', 'hlc')}


def test_authority_survives_sync_reconcile(world, capsys):
    conn, tid, _ = world
    assert adopt(tid, capsys)['adopted'] is True
    for _ in range(2):
        chat_links.reconcile(conn, 'default', GW, ledger=False)
    assert actor(conn, tid).authority == 'chat'
    assert [l['released'] for l in links(conn, tid) if l['kind'] == 'created'] == [0]


def test_readoption_is_idempotent(world, capsys):
    conn, tid, _ = world
    assert adopt(tid, capsys)['adopted'] is True
    snap = (links(conn, tid), conn.execute('SELECT COUNT(*) FROM task_events WHERE task_id=?', (tid,)).fetchone()[0])
    assert adopt(tid, capsys)['adopted'] is True
    chat_links.reconcile(conn, 'default', GW, ledger=False)
    assert adopt(tid, capsys)['adopted'] is True
    created = [l for l in links(conn, tid) if l['kind'] == 'created']
    assert len(created) == 1 and created[0]['uid'] == snap[0][0]['uid']
    assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='creator_chats_resolved'",
                        (tid,)).fetchone()[0] == 1


def test_released_prior_owner_is_not_resurrected(world, capsys):
    conn, tid, _ = world
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at)"
                     " VALUES (?,?,?,NULL,?,'created',1,1)", (f'{tid}:{GW}:-:{SID}:created', tid, GW, SID))
    result = adopt(tid, capsys)
    assert result['code'] == 'origin_adoption_conflict'
    assert [l['released'] for l in links(conn, tid)] == [1]
    with pytest.raises(RenewalRefused):
        actor(conn, tid)


def test_another_unreleased_owner_conflicts_and_nothing_is_written(world, capsys):
    conn, tid, _ = world
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at)"
                     " VALUES (?,?,?,NULL,'other','created',0,1)", (f'{tid}:{GW}:-:other:created', tid, GW))
    n = conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
    assert adopt(tid, capsys)['code'] == 'origin_adoption_conflict'
    assert [l['chat_session'] for l in links(conn, tid)] == ['other']
    assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == n
    with pytest.raises(RenewalRefused):
        actor(conn, tid)


@pytest.mark.parametrize('how', ['worker', 'machine'])
def test_worker_and_noninteractive_write_nothing(world, capsys, monkeypatch, how):
    conn, tid, home = world
    if how == 'worker':
        monkeypatch.setenv('HERMES_KANBAN_TASK', 't_worker')
    else:
        with sqlite3.connect(home / 'state.db') as db:
            db.execute("UPDATE sessions SET source='cron',created_source='cron'")
    n = conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0]
    assert adopt(tid, capsys)['code'] == 'origin_interactive_required'
    assert links(conn, tid) == []
    assert conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0] == n


def test_foreign_card_writes_nothing(world, capsys, monkeypatch):
    from hermes_cli.kanban_foreign_card import ForeignCardError
    conn, tid, _ = world
    def refuse(conn, task_id):
        raise ForeignCardError(task_id, 'other-gw')
    monkeypatch.setattr('hermes_cli.kanban_foreign_card.check', refuse)
    assert adopt(tid, capsys)['code'] == 'foreign_card'
    assert links(conn, tid) == []


def test_adopted_owner_can_submit_fresh_uid_mandate_without_releasing_task(world, capsys, monkeypatch):
    from hermes_cli import kanban_scope_renewal as renewal
    from hermes_cli import kanban_scope_renewal_cli as cli
    from hermes_cli import kanban_spend_decision as decision
    from tests.hermes_cli import test_kanban_scope_renewal_synced as remote
    conn, tid, _ = world
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': 'scope ended'})
    assert adopt(tid, capsys)['adopted'] is True
    comment = kb.add_comment(conn, tid, 'default', 'Renew only one bounded correction.',
                             writer_kind='agent', writer_session=SID)
    uid = conn.execute('SELECT uid FROM task_comments WHERE id=?', (comment,)).fetchone()[0]
    hub = remote.Hub({'decision_retain': 1, 'scope_renewal': 1})
    monkeypatch.setattr(client, 'Hub', lambda identity: hub)
    monkeypatch.setattr(client, 'load_identity', lambda: None)
    monkeypatch.setattr(client, 'push', lambda *a, **kw: None)
    episode = renewal.key(decision.pending_episode(conn, tid))
    payload = cli.renew(conn, tid, episode=episode, comment_id=uid, reason='fresh explicit owner mandate',
                        writer=WRITER, env={'HERMES_SESSION_ID': SID}, ancestry=[{'HERMES_SESSION_ID': SID}])
    assert payload['mandate_comment_uid'] == uid
    assert hub.sent[0]['payload']['authority_kind'] == 'chat'
    assert kb.get_task(conn, tid).status == 'triage'
    assert decision.pending(conn, tid)  # renewal is not promotion or new spend


@pytest.mark.parametrize('profile', ['default', 'builder'])
@pytest.mark.parametrize('default_collision', [False, True])
def test_adopted_profile_owner_survives_reconcile(tmp_path, monkeypatch, profile, default_collision):
    from pathlib import Path
    from hermes_cli import kanban_origin
    root = tmp_path / '.hermes'
    named = root / 'profiles' / 'builder'
    named.mkdir(parents=True)
    home = root if profile == 'default' else named
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(root))
    for key in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_KANBAN_DB',
                'HERMES_KANBAN_BOARD', 'HERMES_DELEGATED_CHILD_CONTEXT', 'HERMES_PROFILE', 'HERMES_PROFILE_NAME'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HERMES_SESSION_ID', SID)
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: (GW, {GW}))
    for target in [home] + ([root] if profile != 'default' and default_collision else []):
        with sqlite3.connect(target / 'state.db') as db:
            db.execute('CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, created_source TEXT)')
            db.execute("INSERT INTO sessions VALUES (?,'desktop','desktop')", (SID,))
    support.write_sync_config(root, True)
    with kbc.connect(root / 'kanban.db') as conn:
        client.ensure_identity(conn, 'default', GW)
        from hermes_cli import kanban_db_sync as sync
        sync.set_mode(conn, 'primary')
        tid = kb.create_task(conn, title='ownerless profile adoption', created_by='claude-code-chat')
        assert local.adopt(conn, tid)['adopted']
        for _ in range(2):
            chat_links.reconcile(conn, 'default', GW, ledger=False)
            got = resolve_actor(conn, tid, writer=WRITER, environs=[{'HERMES_SESSION_ID': SID}],
                                gateway=GW, profile=profile)
            assert got.authority == 'chat'
            owners = [l for l in links(conn, tid) if l['kind'] == 'created' and not l['released']]
            assert [l['chat_profile'] or 'default' for l in owners] == [profile]
            if profile != 'default':
                with pytest.raises(RenewalRefused):
                    actor(conn, tid)


@pytest.mark.parametrize('old_profile', [None, 'default', 'builder'])
def test_adopting_fresh_chat_preserves_other_released_owner(world, capsys, old_profile):
    conn, tid, home = world
    old = '20261123_121212_aaaaaa'
    with sqlite3.connect(home / 'state.db') as db:
        db.execute("INSERT INTO sessions VALUES (?,'desktop','desktop')", (old,))
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_chat_links "
            "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
            "VALUES (?,?,?,?,?,'created',1,1)",
            (chat_links.link_uid(tid, GW, old_profile, old, 'created'), tid, GW, old_profile, old))
    with pytest.raises(RenewalRefused):
        actor(conn, tid, old)
    assert adopt(tid, capsys)['adopted']
    for _ in range(2):
        chat_links.reconcile(conn, 'default', GW, ledger=False)
        assert actor(conn, tid).authority == 'chat'
        previous = [row for row in links(conn, tid) if row['chat_session'] == old]
        assert [row['released'] for row in previous] == [1]
        with pytest.raises(RenewalRefused):
            actor(conn, tid, old)


def test_adoption_does_not_authorize_another_chat(world, capsys):
    conn, tid, _ = world
    assert adopt(tid, capsys)['adopted'] is True
    with pytest.raises(RenewalRefused) as err:
        actor(conn, tid, session='20261123_999999_ffffff')
    assert err.value.code in {'not_scope_authority', 'identity_unavailable'}
