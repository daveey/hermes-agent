"""Index decoding must scale with changed remote rows, not replica churn."""
import json
import sqlite3

import pytest

from tests.tui_gateway.test_kanban_remote_idle_read import CHAT, NOW, _card, _index, _replica
from tui_gateway import kanban_remote_notices as remote


def test_only_changed_remote_rows_are_decoded(tmp_path, monkeypatch):
    cards = [_card(f"t_{i}", CHAT, status="done", at=NOW) for i in range(30)]
    path = _replica(tmp_path, cards, "wal")
    remote._reset_for_tests()
    assert len(_index(path)[1][("default", CHAT)]) == 30
    seen, real = [], remote._linked_chats
    monkeypatch.setattr(remote, "_linked_chats", lambda task, me: seen.append(task["id"]) or real(task, me))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE replica SET seq=100 WHERE key=?", (cards[0]["key"],))
    assert len(_index(path)[1][("default", CHAT)]) == 30
    assert seen == ["t_0"]
    seen.clear()
    with sqlite3.connect(path) as conn:
        me = conn.execute("SELECT v FROM meta WHERE k='gateway'").fetchone()[0]
        conn.execute("INSERT INTO replica VALUES (?, 'tasks', 'default/default/local', '{}', 101)", (me,))
    assert len(_index(path)[1][("default", CHAT)]) == 30
    assert seen == []


@pytest.mark.parametrize("missing", [False, True])
def test_unknown_seq_does_not_hide_updates(tmp_path, missing):
    card = _card("t_legacy", CHAT, status="done", at=NOW)
    path = _replica(tmp_path, [card])
    with sqlite3.connect(path) as conn:
        if missing:
            conn.execute("ALTER TABLE replica DROP COLUMN seq")
        else:
            conn.execute("UPDATE replica SET seq=NULL")
    remote._reset_for_tests()
    assert _index(path)[1]
    card["task"]["status"] = "running"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE replica SET data=?", (json.dumps(card["task"]),))
    assert _index(path)[1] == {}


def test_warm_read_fetches_only_changed_payload_and_keeps_snapshot(tmp_path, monkeypatch):
    cards = [_card(f"t_{i}", CHAT, status="done", at=NOW) for i in range(2)]
    path = _replica(tmp_path, cards, "wal")
    remote._reset_for_tests()
    assert len(_index(path)[1][("default", CHAT)]) == 2
    with sqlite3.connect(path) as writer:
        writer.execute("UPDATE replica SET seq=100 WHERE key=?", (cards[0]["key"],))
    real_ro, traced = remote._ro, []
    changed = False

    def trace(sql):
        nonlocal changed
        traced.append(sql)
        if sql.startswith("SELECT data") and not changed:
            changed = True
            with sqlite3.connect(path) as writer:
                task = dict(cards[0]["task"], status="running")
                writer.execute("UPDATE replica SET data=?, seq=101 WHERE key=?",
                               (json.dumps(task), cards[0]["key"]))

    def opened(path):
        conn = real_ro(path)
        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(remote, "_ro", opened)
    assert len(_index(path)[1][("default", CHAT)]) == 2
    assert changed
    assert len([sql for sql in traced if sql.startswith("SELECT data")]) == 1
    assert not any("seq, data" in sql for sql in traced)
    assert len(_index(path)[1][("default", CHAT)]) == 1


def test_canonical_deletion_and_legacy_done_without_notice(tmp_path):
    card = _card("t_legacy", CHAT, status="done", at=NOW)
    card["task"].pop("completion_notice", None)
    card["key"] = "default/t_legacy"
    path = _replica(tmp_path, [card])
    remote._reset_for_tests()
    assert _index(path)[1][("default", CHAT)][0]["key"] == card["key"]
    with sqlite3.connect(path) as writer:
        writer.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,100)",
                       ("default/default/t_legacy", json.dumps(dict(card["task"], status="running"))))
    assert _index(path)[1] == {}
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM replica WHERE key='default/default/t_legacy'")
    assert _index(path)[1][("default", CHAT)][0]["key"] == card["key"]
    with sqlite3.connect(path) as writer:
        writer.execute("DELETE FROM replica")
    assert _index(path)[1] == {}
