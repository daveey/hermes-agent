"""One unreadable local board must not abort loaded discovery for healthy boards.

Real Scheduler, real kanban SQLite boards (``list_boards`` inventory), the canonical
local collector (``_collect_kanban_notifications`` via ``_notif_poll_kanban``) and the
real transcript/ACK delivery boundary; only model-turn admission is replaced.
"""
import contextlib
import threading

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import notification_loaded_f as loaded
from tui_gateway import server

CHAT = 'tui-loaded-local-a'
OTHER = 'tui-loaded-local-b'


def _subscribed_done(board, chat):
    with contextlib.closing(kbc.connect(board=board)) as conn:
        tid = kb.create_task(conn, title=f'notify {board}', assignee='worker')
        kbn.add_notify_sub(conn, task_id=tid, platform='tui', chat_id=chat)
        kb.complete_task(conn, tid, summary='all done')
    return tid


def _corrupt(board):
    path = kb.kanban_db_path(board)
    for suffix in ('-wal', '-shm'):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    path.write_bytes(b'not a sqlite database ' * 256)


def _deliver_harness(tmp_path, monkeypatch, *chats):
    from hermes_state import SessionDB
    state = tmp_path / 'notice-state.db'
    with SessionDB(state) as db:
        for chat in chats:
            db.create_session(chat, 'desktop')
    monkeypatch.setattr(krn, 'hub_db_path', lambda: tmp_path / 'absent-replica.db')
    monkeypatch.setattr(krn, 'ledger_path', lambda: tmp_path / 'absent-ledger.db')
    monkeypatch.setattr(server, '_session_db', lambda *_: SessionDB(state))
    monkeypatch.setattr(server, '_session_profile_runtime_scope', lambda *_: contextlib.nullcontext())
    monkeypatch.setattr(server, '_kb_stray_delegate_keys', lambda *_: ())
    monkeypatch.setattr(server, '_session_lineage', lambda *_: ())
    monkeypatch.setattr(server, '_submit_row_target_key', lambda session, *_: session['session_key'])
    monkeypatch.setattr(server, '_kb_gateway_owns', lambda *_: False)
    monkeypatch.setattr(server, '_kb_settle_held', lambda *_: None)
    monkeypatch.setattr(server, '_kb_owner_refusal', lambda *_: None)
    monkeypatch.setattr(server, '_notif_claim_turn', lambda *_: True)
    monkeypatch.setattr(server, '_notif_release_turn', lambda *_: None)
    monkeypatch.setattr(server, '_emit', lambda *_: None)
    submitted = []
    monkeypatch.setattr(server, '_notif_submit', lambda *args, **kw: submitted.append((args, kw)) or True)
    return state, submitted


def _delivered(state, chat):
    from hermes_state import SessionDB
    with SessionDB(state) as db:
        return '\n'.join(str(m.get('content') or '') for m in db.get_messages(chat))


def _tick(action, sid, clock):
    action.tick(sid, clock[0], clock[0] - 60, None, server._notif_poll_kanban)


def test_bad_board_isolated_and_retried_on_next_reconcile(tmp_path, monkeypatch):
    # Positive control (default board) and a healthy sibling sorted after the bad board.
    kb.create_board('bad-board')
    kb.create_board('zeta-board')
    control = _subscribed_done(kb.DEFAULT_BOARD, CHAT)
    sibling = _subscribed_done('zeta-board', CHAT)
    stranded = _subscribed_done('bad-board', OTHER)
    _corrupt('bad-board')
    assert [m['slug'] for m in kb.list_boards(include_archived=False)] == ['default', 'bad-board', 'zeta-board']
    state, submitted = _deliver_harness(tmp_path, monkeypatch, CHAT, OTHER)

    clock = [100.0]
    scheduler = loaded.Scheduler(clock=lambda: clock[0])
    session = {'session_key': CHAT, 'profile_home': None, 'history_lock': threading.Lock()}
    other = {'session_key': OTHER, 'profile_home': None, 'history_lock': threading.Lock()}
    action = scheduler.add(session, 'default', lambda: None)
    other_action = scheduler.add(other, 'default', lambda: None)

    scheduler.step()
    assert len(action.pending) == len(other_action.pending) == 1
    _tick(action, 'ui-a', clock)  # real executor: due() -> canonical collector -> transcript + ACK
    assert session['_notif_local_boards'] == ('default', 'zeta-board')
    text = _delivered(state, CHAT)
    assert control in text and sibling in text  # healthy boards delivered despite the failing board
    assert len(submitted) == 1 and submitted[0][1]['display_kind'] == 'kanban_notice'
    assert not session.get('_kanban_claims') and not session.get('_kanban_pending')  # ACKed, nothing in custody
    scope = (None, 'default')
    assert set(scheduler.local_incomplete[scope]) == {'bad-board'}  # explicit retry obligation
    assert session['_notif_local_incomplete'] == other['_notif_local_incomplete'] == ('bad-board',)
    _tick(other_action, 'ui-b', clock)
    assert other['_notif_local_boards'] == ()  # failed board never handed to the collector
    assert _delivered(state, OTHER) == '' and len(submitted) == 1

    # Recovery: the obligation is not accelerated; it clears only at the next 30 s reconciliation.
    kb.kanban_db_path('bad-board').unlink()
    kb.init_db(board='bad-board')
    stranded = _subscribed_done('bad-board', OTHER)
    clock[0] += 29
    scheduler.step()
    assert set(scheduler.local_incomplete[scope]) == {'bad-board'}
    assert not other_action.pending and not action.pending
    clock[0] += 1
    scheduler.step()
    assert scope not in scheduler.local_incomplete
    assert '_notif_local_incomplete' not in other and '_notif_local_incomplete' not in session
    _tick(other_action, 'ui-b', clock)
    assert other['_notif_local_boards'] == ('bad-board',)
    assert stranded in _delivered(state, OTHER)
    assert not other.get('_kanban_claims') and not other.get('_kanban_pending')
    # Exactly once: chat A's earlier notices are not re-delivered by the recovered pass.
    _tick(action, 'ui-a', clock)
    assert session['_notif_local_boards'] == ('default', 'zeta-board')
    text = _delivered(state, CHAT)
    assert text.count(control) == 1 and text.count(sibling) == 1
    assert len(submitted) == 2
    action.close()
    other_action.close()
    assert '_notif_local_incomplete' not in session
