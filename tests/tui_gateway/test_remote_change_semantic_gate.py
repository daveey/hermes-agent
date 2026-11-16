"""Real per-task cursor mutation controls for the remote outcome gate. [daveey]"""
import sqlite3
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_session_activity as activity
from tests.tui_gateway import test_session_outcome_notices as helpers
from tui_gateway import notice_fanout as nf


db = helpers.db


@pytest.fixture
def shadow(tmp_path, monkeypatch):
    path = tmp_path / 'shadow.db'
    conn = sqlite3.connect(path, isolation_level=None)
    conn.executescript('''
        CREATE TABLE tasks(id TEXT PRIMARY KEY, status TEXT, created_on TEXT, last_heartbeat_at REAL);
        CREATE TABLE task_chat_links(task_id TEXT, chat_gateway TEXT, chat_profile TEXT,
                                     chat_session TEXT, kind TEXT, released INTEGER);
        INSERT INTO tasks VALUES ('t_remote', 'running', 'worker', 1);
    ''')
    conn.execute('INSERT INTO task_chat_links VALUES (?,?,?,?,?,?)',
                 ('t_remote', 'gw', 'default', helpers.CHAT, 'created', 0))
    rev.ensure_revision(conn, rev.SHADOW_TABLES)
    conn.commit()
    base = ('gw', 'default', (('default', str(path)),))
    ledger = []
    monkeypatch.setattr(nf, '_remote_inputs_key', lambda: (base, list(ledger)))
    monkeypatch.setattr(nf, '_remote_memo', None)
    reads = []

    def project(*args, **kwargs):
        reads.append(1)
        return [{'key': f'{owner}/default/{tid}', 'id': tid, 'board': 'default',
                 'gateway': owner, 'session': session, 'status': status}
                for tid, status, owner, session in conn.execute(
                    'SELECT t.id, t.status, t.created_on, l.chat_session FROM tasks t '
                    'JOIN task_chat_links l ON t.id=l.task_id WHERE l.released=0 AND t.created_on != ? ', ('gw',))]

    monkeypatch.setattr(activity, '_remote_cards', project)
    yield conn, base, reads, ledger, project
    conn.close()


def run(db):
    return nf.card_change_pass(session_db=helpers._opener(db), slugs=[])


def test_real_cursor_heartbeat_and_rebuilt_ledger_skip(db, shadow):
    conn, _, reads, ledger, _ = shadow
    ledger.append({'key': 'owner/default/t_other', 'status': 'running', 'synced_at': 1})
    run(db)  # initial durable state seed
    run(db)  # acknowledge and certify the projection
    before = len(reads)
    for clock in range(2, 5):
        conn.execute('UPDATE tasks SET last_heartbeat_at=?', (clock,))
        conn.commit()
        ledger[0] = {**ledger[0], 'synced_at': clock}
        assert run(db) == []
    assert len(reads) == before


@pytest.mark.parametrize('sql', [
    "UPDATE tasks SET status='blocked'",
    "UPDATE tasks SET created_on='gw'",
    "UPDATE task_chat_links SET released=1",
    "DELETE FROM tasks",
    "DELETE FROM task_chat_links",
])
def test_real_cursor_status_owner_release_and_delete_rearm(db, shadow, sql):
    conn, _, reads, _, _ = shadow
    run(db)
    run(db)
    before = len(reads)
    conn.execute(sql)
    conn.commit()
    assert len(run(db)) == 1
    assert len(reads) > before


def test_link_to_previously_unprojected_task_rearms(db, shadow):
    conn, _, reads, _, _ = shadow
    conn.execute("INSERT INTO tasks VALUES ('t_new', 'running', 'worker', 1)")
    conn.commit()
    run(db)
    run(db)
    before = len(reads)
    conn.execute('INSERT INTO task_chat_links VALUES (?,?,?,?,?,?)',
                 ('t_new', 'gw', 'default', helpers.CHAT, 'shepherd', 0))
    conn.commit()
    assert len(run(db)) == 1
    assert len(reads) > before


def test_write_after_projection_cannot_certify_newer_prints(db, shadow, monkeypatch):
    conn, _, _, _, project = shadow
    run(db)  # seed before racing the pass that can publish a memo
    def racing(*args, **kwargs):
        cards = project(*args, **kwargs)
        conn.execute("UPDATE tasks SET status='blocked'")
        conn.commit()
        return cards
    monkeypatch.setattr(activity, '_remote_cards', racing)
    run(db)  # seeds running, but a blocked write races fingerprint collection
    monkeypatch.setattr(activity, '_remote_cards', project)
    assert len(run(db)) == 1  # must not acknowledge the newer write without projecting it


def test_missing_known_task_keeps_absence_fingerprint(shadow):
    conn, base, _, _, _ = shadow
    prints = nf._known_prints(base, {'default': {'missing'}})
    assert prints == {'default': {'missing': None}}
    polled = nf._poll_boards(base, None)
    cursor = {'default': polled['default'][1]}
    conn.execute("INSERT INTO tasks VALUES ('missing', 'running', 'worker', 1)")
    conn.commit()
    assert not nf._remote_boards_quiet(base, nf._poll_boards(base, cursor), prints)


def test_unreadable_replica_retries_instead_of_publishing(db, shadow, monkeypatch):
    _, _, reads, _, _ = shadow
    @contextmanager
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError('unavailable')
        yield
    monkeypatch.setattr(activity, 'cached_ro', unavailable)
    run(db)
    run(db)
    before = len(reads)
    run(db)
    assert len(reads) > before
