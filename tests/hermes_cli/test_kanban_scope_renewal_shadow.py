"""Foreign-home renewal reads shadow evidence; only the canonical Board may write it."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import nullcontext

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_scope_renewal_cli as cli
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_scope_renewal_synced as support

live = support.live


@pytest.fixture(params=['shadow', 'primary'])
def foreign(live, monkeypatch, tmp_path, request):
    hub = support.Hub({'scope_renewal': 1})
    tid, episode, comment = support.stopped(live, monkeypatch, hub)
    # The worker's home has a created link to the hub chat, not to a worker-local chat.
    with kb.write_txn(live):
        live.execute("UPDATE tasks SET created_on='worker-home' WHERE id=?", (tid,))
        live.execute("UPDATE task_chat_links SET chat_gateway='hub' WHERE task_id=?", (tid,))

    from hermes_cli import kanban_origin
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('hub',))
    primary = kbc.connect(tmp_path / 'hub.db')
    client.ensure_identity(primary, 'default', 'hub')
    sync.set_mode(primary, request.param)
    path = tmp_path / 'replica.db'

    def snapshot():
        with sqlite3.connect(path) as dest:  # noqa: kanban-lockguard -- disposable replica fixture
            live.backup(dest)

    snapshot()
    from hermes_cli import kanban_foreign_card
    monkeypatch.setattr(kanban_foreign_card, 'replica_path', lambda db: path)
    try:
        yield primary, live, hub, tid, episode, comment, path, snapshot
    finally:
        primary.close()


def invoke(monkeypatch, primary, tid, episode, comment):
    monkeypatch.setattr(kbc, 'connect_closing', lambda: nullcontext(primary))
    monkeypatch.setattr(cli, 'os', support.SimpleNamespace(environ={}))
    from hermes_cli import kanban_comment_writer as writer
    monkeypatch.setattr(writer, 'cli_comment_writer', lambda **kw: support.AGENT)
    monkeypatch.setattr(writer, 'ancestry_environs', lambda: [{'HERMES_SESSION_ID': support.CHAT}])
    return cli.command(argparse.Namespace(task_id=tid, episode=episode, mandate_comment=comment,
                                          reason='authorized', json=True))


def test_owner_chat_can_submit_foreign_home_renewal_without_writing_shadow(foreign, monkeypatch, capsys):
    primary, home, hub, tid, episode, comment, path, _ = foreign
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert not kb.get_task(primary, tid)
    changes = primary.total_changes
    assert invoke(monkeypatch, primary, tid, episode, comment) == 0
    assert '"ok": true' in capsys.readouterr().out
    wire, = hub.sent
    assert wire['payload']['authority_ref'] == f'@session:default@hub/{support.CHAT}'
    assert wire['op'] == 'renew_scope' and wire['episode'] == episode
    assert wire['mandate_comment_uid'] == home.execute('SELECT uid FROM task_comments WHERE id=?', (comment,)).fetchone()[0]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    # The normal sync consumer, not a CLI write to the shadow/home, projects the Board event on home.
    assert primary.total_changes == changes
    assert not kb.get_task(primary, tid)
    proof = hub.row_evidence('default', 'task_events', wire['event_uid'])
    sync.apply_remote_changes(home, [client._row_change('task_events', proof['row'])],
                              new_cursor=sync.sync_state(home)['cursor'], board='default')
    assert home.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='scope_renewed'", (tid,)).fetchone()[0] == wire['event_uid']
    assert kb.get_task(home, tid).status == 'triage'


@pytest.mark.parametrize('damage,code', [('shepherd', 'not_scope_authority'),
                                         ('writer', 'mandate_authority'),
                                         ('safety', 'safety_restricted'),
                                         ('released', 'not_scope_authority'),
                                         ('gateway', 'not_scope_authority')])
def test_foreign_home_refusals_before_board_submission(foreign, monkeypatch, capsys, damage, code):
    primary, home, hub, tid, episode, comment, path, snapshot = foreign
    def change_writer():
        row = home.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='commented'", (tid,)).fetchone()
        payload = json.loads(row['payload'])
        payload['writer_session'] = 'different-principal'
        home.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), row['id']))

    changes = {
        'shepherd': lambda: home.execute("UPDATE task_chat_links SET kind='shepherd' WHERE task_id=?", (tid,)),
        'released': lambda: home.execute('UPDATE task_chat_links SET released=1 WHERE task_id=?', (tid,)),
        'gateway': lambda: home.execute("UPDATE task_chat_links SET chat_gateway='worker-home' WHERE task_id=?", (tid,)),
        'writer': change_writer,
        'safety': lambda: kb._append_event(home, tid, 'decision_hold_restricted',
                                          {'episode': episode, 'cause': 'safety_refusal'}),
    }
    with kb.write_txn(home):
        changes[damage]()
    snapshot()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert invoke(monkeypatch, primary, tid, episode, comment) == 1
    assert code in capsys.readouterr().out
    assert not hub.sent
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize('ancestry,code', [
    ([{'HERMES_SESSION_ID': 'another-chat'}], 'identity_unavailable'),
    ([{'HERMES_SESSION_ID': support.CHAT, 'HERMES_KANBAN_WORKER_TASK': 't_unrelated'}], 'not_scope_authority'),
])
def test_shadow_evidence_does_not_authorize_spoofed_process_identity(foreign, ancestry, code):
    primary, _, hub, tid, episode, comment, _, _ = foreign
    with pytest.raises(cli.RenewalRefused) as caught:
        cli.renew(primary, tid, episode=episode, comment_id=comment, reason='authorized',
                  writer=support.AGENT, env={'HERMES_SESSION_ID': support.CHAT}, ancestry=ancestry)
    assert caught.value.code == code
    assert not hub.sent


@pytest.mark.parametrize('damage', [None, 'shepherd', 'session', 'released', 'gateway', 'profile', 'deleted', 'task'])
def test_missing_replica_owner_uses_exact_canonical_created_link(foreign, monkeypatch, capsys, damage):
    primary, home, hub, tid, episode, comment, path, snapshot = foreign
    # Exact incident shape: home has legacy NULL-profile ownership; hub has only an observer.
    owner = dict(home.execute('SELECT * FROM task_chat_links WHERE task_id=?', (tid,)).fetchone())
    assert owner['chat_profile'] is None
    with kb.write_txn(home):
        home.execute("UPDATE task_chat_links SET kind='shepherd',chat_profile='default' WHERE task_id=?", (tid,))
    snapshot()
    with kb.write_txn(home):
        home.execute("UPDATE task_chat_links SET kind='created',chat_profile=NULL WHERE task_id=?", (tid,))
    from hermes_cli.kanban_chat_links import link_uid
    uid = link_uid(tid, 'hub', None, support.CHAT, 'created')
    owner.update(uid=uid, deleted_at=None)
    mutations = {'session': ('chat_session', 'wrong'), 'gateway': ('chat_gateway', 'wrong'),
                 'profile': ('chat_profile', 'wrong'), 'task': ('task_id', 'wrong'),
                 'shepherd': ('kind', 'shepherd'), 'released': ('released', 1), 'deleted': ('deleted_at', 1)}
    if damage is not None:
        field, value = mutations[damage]
        owner[field] = value
    real = hub.row_evidence
    reads = []

    def evidence(board, table, key):
        if table != 'task_chat_links':
            return real(board, table, key)
        reads.append(key)
        return {'state': 'live', 'row': owner} if key == uid else {'state': 'absent'}

    hub.row_evidence = evidence
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert invoke(monkeypatch, primary, tid, episode, comment) == (0 if damage is None else 1)
    assert ('"ok": true' if damage is None else 'not_scope_authority') in capsys.readouterr().out
    assert uid in reads
    assert len(hub.sent) == (1 if damage is None else 0)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not kb.get_task(primary, tid)


@pytest.mark.parametrize('profile', [None, 'default', 'other'])
@pytest.mark.parametrize('failure', [None, 'absent', 'offline', 'wrong-session', 'own-run', 'ambiguous'])
def test_canonical_owner_lookup_preserves_process_and_transport_gates(foreign, monkeypatch, profile, failure):
    primary, home, hub, tid, episode, comment, _, snapshot = foreign
    with kb.write_txn(home):
        home.execute("UPDATE task_chat_links SET kind='shepherd' WHERE task_id=?", (tid,))
    snapshot()
    from hermes_cli import profiles
    from hermes_cli.kanban_chat_links import link_uid
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: profile or 'default')
    session = 'wrong-chat' if failure == 'wrong-session' else support.CHAT
    reads = []
    closed = []
    real = hub.row_evidence
    uid = link_uid(tid, 'hub', profile, support.CHAT, 'created')

    def evidence(board, table, key):
        if table != 'task_chat_links':
            return real(board, table, key)
        reads.append(key)
        if failure == 'offline':
            raise client.SyncUnavailable('offline')
        if failure == 'absent' or key != uid:
            return {'state': 'absent'}
        return {'state': 'live', 'row': dict(uid=uid, task_id=tid, chat_gateway='hub', chat_profile=profile,
                                           chat_session=support.CHAT, kind='created', released=0, deleted_at=None)}

    hub.row_evidence = evidence
    hub.close = lambda: closed.append(True)
    env = {'HERMES_SESSION_ID': session}
    ancestry = [env]
    if failure == 'own-run':
        env['HERMES_KANBAN_WORKER_TASK'] = tid
    elif failure == 'ambiguous':
        ancestry = [{'HERMES_SESSION_ID': 'another-session'}]
    kwargs = dict(episode=episode, comment_id=comment, reason='authorized',
                  writer=('agent', session, '4242:HERMES_SESSION_ID'), env=env, ancestry=ancestry)
    if failure is None:
        assert cli.renew(primary, tid, **kwargs)['authority_kind'] == 'chat'
        assert uid in reads and len(hub.sent) == 1
    else:
        with pytest.raises(cli.RenewalRefused) as caught:
            cli.renew(primary, tid, **kwargs)
        assert caught.value.code == {'offline': 'board_unavailable', 'own-run': 'own_run',
                                     'ambiguous': 'identity_unavailable'}.get(failure, 'not_scope_authority')
        assert not hub.sent
        if failure in {'own-run', 'ambiguous'}:
            assert not reads
        else:
            assert reads and closed


def test_canonical_revalidation_can_refuse_stale_shadow_authority(foreign, monkeypatch, capsys):
    primary, _, hub, tid, episode, comment, path, _ = foreign
    hub.reply = {'status': 'rejected', 'reason': 'not_scope_authority'}
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert invoke(monkeypatch, primary, tid, episode, comment) == 1
    assert 'not_scope_authority' in capsys.readouterr().out
    assert len(hub.sent) == 1
    assert not primary.execute("SELECT 1 FROM task_events WHERE kind='scope_renewed'").fetchone()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
