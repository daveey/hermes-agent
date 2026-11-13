"""The notice index keeps only cards routed to a chat, and a cold read never holds every payload as one string.

On the mini (17k replica rows, 79 MB of JSON, 21 routed chats) ``remote_index_projection._rows`` kept every
decoded task: 169 MB and 1.5M GC-tracked objects retained per gateway, behind a 448 MB cold-read peak. [daveey]
"""
import gc
import json
import sqlite3
import tracemalloc

from tests.tui_gateway.test_kanban_remote_notices import CHAT, _card, _hub
from tui_gateway import kanban_remote_notices as remote
from tui_gateway import remote_index_projection

CARDS = 2000
LINKED = 20
BODY = 10_000


def _fleet(tmp_path):
    cards = []
    for i in range(CARDS):
        card = _card(f"t_{i}", CHAT if i < LINKED else f"other_{i}", status="done", at=1_800_000_000.0)
        if i >= LINKED:
            card["task"]["sessions"][0]["gw"] = "elsewhere"  # not routed to this gateway's chats
        card["task"]["body"] = f"{i:06d} " + "ü€ body text 🙂 " * (BODY // 16)  # non-ASCII: UCS-4 strings
        cards.append(card)
    path = _hub(tmp_path, cards)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE INDEX replica_poll_metadata ON replica(coll, gw, seq)")
        text = conn.execute("SELECT total(octet_length(data)) FROM replica").fetchone()[0]
    return path, text


def _index(path):
    with remote._lock:
        return remote._index(path)


def test_cold_index_retains_only_routed_cards_and_bounded_peak(tmp_path):
    path, text = _fleet(tmp_path)
    remote._reset_for_tests()
    remote_index_projection.reset()
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        me, index = _index(path)
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert me == "macbook"
    assert sorted(card["key"] for card in index[("default", CHAT)]) == sorted(
        f"default/default/t_{i}" for i in range(LINKED))
    rows = remote_index_projection._rows
    assert len(rows) == CARDS
    assert sum(hit[2] is not None for hit in rows.values()) == LINKED
    # main: retained ~2.6x and peak ~6x of the row text; here ~0.01x and ~1.6x.
    assert retained - before < 0.1 * text, f"retained {(retained - before) / text:.2f}x of {text:.0f} bytes"
    assert peak - before < 3.0 * text, f"peak {(peak - before) / text:.2f}x of {text:.0f} bytes"


def test_warm_index_keeps_routed_cards_and_routes_a_newly_linked_one(tmp_path):
    path, _ = _fleet(tmp_path)
    remote._reset_for_tests()
    remote_index_projection.reset()
    assert len(_index(path)[1][("default", CHAT)]) == LINKED
    with sqlite3.connect(path) as conn:
        key = f"default/default/t_{CARDS - 1}"
        task = json.loads(conn.execute("SELECT data FROM replica WHERE key=?", (key,)).fetchone()[0])
        task["sessions"][0].update(gw="macbook", session=CHAT)
        conn.execute("UPDATE replica SET data=?, seq=? WHERE key=?", (json.dumps(task), CARDS + 10, key))
    cards = _index(path)[1][("default", CHAT)]
    assert len(cards) == LINKED + 1
    assert any(card["key"] == key and card["task"]["sessions"][0]["session"] == CHAT for card in cards)
    assert sum(hit[2] is not None for hit in remote_index_projection._rows.values()) == LINKED + 1


def test_task_rows_pages_payloads_in_one_snapshot_and_keeps_the_planner_order(tmp_path, monkeypatch):
    path, text = _fleet(tmp_path)
    monkeypatch.setattr(remote, "_ROW_PAGE_BYTES", 4 << 20)
    with sqlite3.connect(path) as conn:
        sql = "SELECT gw, key, seq, data FROM replica WHERE coll = 'tasks' AND gw != ?"
        expected = conn.execute(sql, ("macbook",)).fetchall()
        traced = []
        conn.set_trace_callback(traced.append)
        actual = remote._task_rows(conn, "macbook")
        assert not conn.in_transaction  # the snapshot it opened is closed
    assert actual == expected
    pages = [q for q in traced if "max(rowid) FROM (SELECT rowid, data" in q]
    per_page = (4 << 20) // (text / CARDS)  # ~180 rows of ~23 KB: about 4 MB of text per page
    assert len(pages) == -(-CARDS // per_page)  # paged by size (no trailing empty page), not one string of all
    assert traced[0].startswith("PRAGMA") and traced[1] == "BEGIN"
