import sqlite3
import threading
import pytest
from tests.tui_gateway import test_loaded_local_board_isolation as f
from tui_gateway import notification_loaded_f as loaded, kanban_remote_notices as krn, server

@pytest.mark.parametrize('mode', ['legacy', 'missing', 'corrupt', 'bad_capture'])
def test_healthy_local_survives_remote_failure(tmp_path, monkeypatch, mode):
    tid = f._subscribed_done(f.kb.DEFAULT_BOARD, f.CHAT)
    state, submitted = f._deliver_harness(tmp_path, monkeypatch, f.CHAT)
    path = tmp_path / 'replica.db'
    if mode in ('legacy', 'corrupt'):
        path.write_bytes(b'not sqlite')
    elif mode == 'bad_capture':
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)')
            conn.executemany('INSERT INTO meta VALUES (?,?)', [('gateway','local'),('cursor','1'),('head','1')])
    monkeypatch.setattr(krn, 'hub_db_path', lambda: path)
    session = {'session_key': f.CHAT, 'profile_home': None, 'history_lock': threading.Lock()}
    if mode == 'legacy':
        server._notif_poll_kanban('ui', session)
    else:
        clock = [100.0]
        scheduler = loaded.Scheduler(clock=lambda: clock[0])
        action = scheduler.add(session, 'default', lambda: None)
        errors = []
        scope = (None, 'default')
        remembered = {('remote', 'default/t_old'): (f.CHAT,)}
        scheduler.known_keys[scope] = remembered.copy()
        try:
            for at in [100.0, 130.0, 160.0]:
                clock[0] = at
                try:
                    scheduler.step()
                except sqlite3.Error as exc:
                    errors.append(str(exc))
                f._tick(action, 'ui', clock)
            assert tid in f._delivered(state, f.CHAT), (errors, len(action.pending), submitted)
            assert not errors
            assert scheduler.next_reconcile == 190.0
            if mode in ('corrupt', 'bad_capture'):
                assert scope in scheduler.remote_incomplete
                assert scheduler.hint_incomplete is (mode == 'corrupt')
                assert scheduler.known_keys[scope] == remembered
                # Failed keyed capture remains unknown until the 30s pass.
                scheduler.invalidate([('remote', 'default/t_old')])
                clock[0] = 161.0
                scheduler.step()
                f._tick(action, 'ui', clock)
                assert not scheduler.dirty
                assert scope in scheduler.remote_incomplete
                assert scheduler.next_reconcile == 190.0
                # Removing the unavailable replica is a real absent-source
                # recovery. The next clock clears unknown state, not before.
                path.unlink()
                clock[0] = 190.0
                scheduler.step()
                f._tick(action, 'ui', clock)
                assert scope not in scheduler.remote_incomplete
                assert not scheduler.hint_incomplete
                assert scheduler.known_keys[scope] == {}
                assert not scheduler.dirty
        finally:
            action.close()
    assert tid in f._delivered(state, f.CHAT)
    assert len(submitted) == 1
