"""Changed replica commits must not re-decode unchanged task projections."""
import json
import sqlite3

from tests.tui_gateway.test_kanban_remote_arrival import CHAT, _card
from tests.tui_gateway.test_kanban_remote_idle_read import NOW, _replica
from tui_gateway import kanban_remote_notices as remote


def test_unrelated_commit_skips_task_projection(tmp_path, monkeypatch):
    path = _replica(tmp_path, [_card('t_one', CHAT, status='done', at=NOW)], 'wal')
    remote._reset_for_tests()
    expected = remote._sweep_snapshot(path, 'macbook')
    remote._sweep_snapshot(path, 'macbook')
    calls = []
    original = remote._sweep_cards

    def track(rows, me):
        calls.extend(row[1] for row in rows)
        return original(rows, me)

    monkeypatch.setattr(remote, '_sweep_cards', track)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE meta SET v='999' WHERE k='head'")
    assert remote._sweep_snapshot(path, 'macbook') == expected
    assert calls == []


def test_only_changed_task_is_projected(tmp_path, monkeypatch):
    cards = [_card(t, CHAT, status='done', at=NOW) for t in ('t_one', 't_two')]
    path = _replica(tmp_path, cards, 'wal')
    remote._reset_for_tests()
    remote._sweep_snapshot(path, 'macbook')
    remote._sweep_snapshot(path, 'macbook')
    calls = []
    original = remote._sweep_cards

    def track(rows, me):
        calls.extend(row[1] for row in rows)
        return original(rows, me)

    monkeypatch.setattr(remote, '_sweep_cards', track)
    changed = _card('t_one', CHAT, status='blocked', at=NOW + 1)
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE replica SET data=?,seq=seq+10 WHERE key=?',
                     (json.dumps(changed['task']), changed['key']))
    got = remote._sweep_snapshot(path, 'macbook')
    assert got[1]['hub/' + changed['key']][0] == f'blocked@{int(NOW)+1}'
    assert calls == [changed['key']]


def test_replacement_gateway_and_deletion_invalidate(tmp_path):
    import os

    card = _card('t_one', CHAT, status='done', at=NOW)
    path = _replica(tmp_path / 'live', [card])
    remote._reset_for_tests()
    assert remote._sweep_snapshot(path, 'macbook')[0]
    assert remote._sweep_snapshot(path, 'hub') == ([], {})
    assert remote._sweep_snapshot(path, 'macbook')[0]
    changed = _card('t_one', CHAT, status='blocked', at=NOW + 1)
    staged = _replica(tmp_path / 'stage', [changed])
    os.replace(staged, path)
    got = remote._sweep_snapshot(path, 'macbook')
    assert got[1]['hub/' + card['key']][0] == f'blocked@{int(NOW)+1}'
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM replica')
    assert remote._sweep_snapshot(path, 'macbook') == ([], {})
    from tui_gateway import remote_sweep_projection
    assert remote_sweep_projection._rows == {}


def test_unknown_fingerprint_never_reuses_row_projection(tmp_path, monkeypatch):
    from tui_gateway import kanban_replica_observer as observer

    card = _card('t_one', CHAT, status='done', at=NOW)
    path = _replica(tmp_path, [card])
    remote._reset_for_tests()
    remote._sweep_snapshot(path, 'macbook')
    monkeypatch.setattr(observer, 'fingerprint', lambda path: None)
    changed = _card('t_one', CHAT, status='blocked', at=NOW + 1)
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE replica SET data=?', (json.dumps(changed['task']),))
    got = remote._sweep_snapshot(path, 'macbook')
    assert got[1]['hub/' + card['key']][0] == f'blocked@{int(NOW)+1}'
    assert remote._sweep_memo is None
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE replica SET data=?', (json.dumps(card['task']),))
    assert remote._sweep_snapshot(path, 'macbook')[1]['hub/' + card['key']][0] == f'done@{int(NOW)}'


def test_canonical_row_add_and_remove(tmp_path):
    card = _card('t_one', CHAT, status='done', at=NOW)
    legacy = {**card, 'key': 'default/t_one'}
    path = _replica(tmp_path, [legacy], 'wal')
    remote._reset_for_tests()
    assert list(remote._sweep_snapshot(path, 'macbook')[1]) == ['hub/' + legacy['key']]
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,2)",
                     (card['key'], json.dumps(card['task'])))
    assert list(remote._sweep_snapshot(path, 'macbook')[1]) == ['hub/' + card['key']]
    with sqlite3.connect(path) as conn:
        conn.execute('DELETE FROM replica WHERE key=?', (card['key'],))
    assert list(remote._sweep_snapshot(path, 'macbook')[1]) == ['hub/' + legacy['key']]


def test_warm_payload_read_is_one_snapshot(tmp_path, monkeypatch):
    card = _card('t_one', CHAT, status='done', at=NOW)
    path = _replica(tmp_path, [card], 'wal')
    remote._reset_for_tests()
    remote._sweep_snapshot(path, 'macbook')
    changed = _card('t_one', CHAT, status='blocked', at=NOW + 1)
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE replica SET data=?,seq=2', (json.dumps(changed['task']),))
    newer = _card('t_one', CHAT, status='done', at=NOW + 2)
    original = remote._ro
    fired = []

    class Reader:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            if sql.startswith('SELECT data FROM replica') and not fired:
                fired.append(True)
                with sqlite3.connect(path) as writer:
                    writer.execute('UPDATE replica SET data=?,seq=3', (json.dumps(newer['task']),))
            return self.conn.execute(sql, *args)

        def close(self):
            self.conn.close()

    monkeypatch.setattr(remote, '_ro', lambda path: Reader(original(path)))
    got = remote._sweep_snapshot(path, 'macbook')
    assert fired
    assert got[1]['hub/' + card['key']][0] == f'blocked@{int(NOW)+1}'
    assert remote._sweep_memo is None
    monkeypatch.setattr(remote, '_ro', original)
    assert remote._sweep_snapshot(path, 'macbook')[1]['hub/' + card['key']][0] == f'done@{int(NOW)+2}'


def test_legacy_no_seq_rereads_payload(tmp_path):
    card = _card('t_one', CHAT, status='done', at=NOW)
    path = _replica(tmp_path, [card])
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE replica DROP COLUMN seq')
    remote._reset_for_tests()
    assert remote._sweep_snapshot(path, 'macbook')[0]
    changed = _card('t_one', CHAT, status='blocked', at=NOW + 1)
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE replica SET data=?', (json.dumps(changed['task']),))
    assert remote._sweep_snapshot(path, 'macbook')[1]['hub/' + card['key']][0] == f'blocked@{int(NOW)+1}'
