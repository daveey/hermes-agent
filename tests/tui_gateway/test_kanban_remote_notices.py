"""A remote owner publishes completion evidence; the destination persists one passive notice."""

import contextlib
import json
import os
import sqlite3
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_origin_ledger import observe_owner_task
from hermes_state import SessionDB
from tui_gateway.kanban_remote_notices import sweep_remote_notices

CHAT = "20261026_235722_bef6c6"
OTHER = "20261022_204322_4d8ce1"


def _replicate(tmp_path, summary, *, legacy=False):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="remote worker", assignee="default")
        for sid in (CHAT, OTHER):
            kbn.add_notify_sub(conn, task_id=tid, platform="hub", chat_id=f"macbook/default/{sid}")
        kb.complete_task(conn, tid, summary=summary)
        if legacy:
            conn.execute("UPDATE task_events SET payload=json_remove(payload,'$.notice_version') WHERE task_id=?", (tid,))
            conn.commit()
        task = {"id": tid, "status": "done", "title": "remote worker", "board": "default",
                "gateway": "hub", "profile": "default", "assignee": "default", "origin_bindings": [],
                "sessions": [{"gw": "macbook", "profile": "default", "session": sid, "rel": "shepherd"}
                             for sid in (CHAT, OTHER)]}
        conn.execute("BEGIN")
        published = observe_owner_task(conn, task, now=0)
        conn.rollback()
    replica = tmp_path / "hub.db"
    with sqlite3.connect(replica) as conn:
        conn.execute("CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,data TEXT)")
        conn.execute("INSERT INTO replica VALUES (?,?,?,?)", ("hub", "tasks", f"default/default/{tid}", json.dumps(published)))
    return tid, replica


def test_remote_completion_reaches_only_addressed_chat_and_is_idempotent(tmp_path):
    tid, replica = _replicate(tmp_path, f"FINAL SEND for @session:default@macbook/{CHAT}: remote proof")
    with SessionDB(tmp_path / "state.db") as db:
        for sid in (CHAT, OTHER):
            db.create_session(sid, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            assert profile == "default"
            yield db, db.get_compression_tip(sid) or sid

        assert len(sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)) == 2
        # Already persisted: later passes skip it without opening the store (no writer txn per pass).
        assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener) == []
        messages = db.get_messages(CHAT)
        assert len(messages) == 1
        assert messages[0]["display_kind"] == "kanban_notice"
        assert "remote proof" in messages[0]["content"] and tid in messages[0]["content"]
        assert "remote proof" not in db.get_messages(OTHER)[0]["content"]
        db.end_session(CHAT, "compression")
        db.create_session("continuation", "desktop", parent_session_id=CHAT)
        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)
        assert not db.get_messages("continuation")
        assert len(db.get_messages(CHAT)) == 1
        assert not sweep_remote_notices(replica_path=replica, gateway="stranger", session_db=opener)


def test_remote_generic_notice_and_busy_retry(tmp_path):
    _tid, replica = _replicate(tmp_path, "generic completion proof")
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        busy = True

        @contextlib.contextmanager
        def opener(profile, sid):
            if busy:
                raise RuntimeError("busy")
            yield (db, sid) if sid == CHAT else (None, None)

        assert not sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)
        busy = False
        assert len(sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)) == 1
        assert "generic completion proof" in db.get_messages(CHAT)[0]["content"]


def test_legacy_done_cards_are_not_replayed_on_rollout(tmp_path):
    _tid, replica = _replicate(tmp_path, "historical completion", legacy=True)
    assert not sweep_remote_notices(replica_path=replica, gateway="macbook")
    with sqlite3.connect(replica) as conn:
        task = json.loads(conn.execute("SELECT data FROM replica").fetchone()[0])
        assert "completion_notice" not in task


def test_poller_collect_delivers_final_send_and_sweeper_dedupes(tmp_path):
    import time
    from tui_gateway import kanban_remote_notices as krn

    tid, replica = _replicate(tmp_path, f"proof prose\nFINAL SEND for @session:default@macbook/{CHAT}: poller proof")
    with sqlite3.connect(replica) as conn:
        data = json.loads(conn.execute("SELECT data FROM replica").fetchone()[0])
        data["completed_at"] = time.time()
        conn.execute("DROP TABLE replica")
        conn.execute("CREATE TABLE replica(gw TEXT,coll TEXT,key TEXT,data TEXT,seq INTEGER)")
        conn.execute("CREATE TABLE meta(k TEXT,v TEXT)")
        conn.execute("INSERT INTO meta VALUES ('gateway','macbook')")
        conn.execute("INSERT INTO replica VALUES (?,?,?,?,1)", ("hub", "tasks", f"default/default/{tid}", json.dumps(data)))
    krn._reset_for_tests()
    ledger = tmp_path / "ledger.db"
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger)
    assert len(got) == 1
    assert "poller proof" in got[0]["text"] and "ran on hub" not in got[0]["text"]
    assert "FINAL SEND" not in got[0]["text"]
    other = krn.collect(OTHER, "default", hub_db=replica, ledger=ledger)
    assert "poller proof" not in other[0]["text"]
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        db.record_kanban_notice(CHAT, got[0]["text"], {"notice_id": "live:x", "kanban_event_keys": [got[0]["event_key"]]})

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)
        assert len(db.get_messages(CHAT)) == 1


def test_poller_acks_the_ledger_only_after_the_transcript_write(tmp_path, monkeypatch):
    """The open-chat poller does not ack the in-flight row until the notice text is in the transcript."""
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway.session_notifications import _kb_ack_durable

    now = time.time()
    card = _card("t_poll_ack", CHAT, status="done", at=now - 4, final="after-write")
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    monkeypatch.setattr(krn, "ledger_path", lambda: ledger)
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    text = object()
    session = {"_kanban_claims": [{"remote": got[0], "texts": [text]}]}
    _kb_ack_durable(session, set())
    assert _ledger_row(ledger, CHAT, got[0]["ref"])["delivered"] == 0
    _kb_ack_durable(session, {id(text)})
    assert _ledger_row(ledger, CHAT, got[0]["ref"])["delivered"] == 1
    assert session["_kanban_claims"][0]["acked"] is True


def test_absent_replica_is_read_only_and_not_created(tmp_path):
    path = tmp_path / "absent.db"
    assert sweep_remote_notices(replica_path=path, gateway="macbook") == []
    assert not path.exists()


# Live ledger rows the 2026-10-28 scan found claimed delivered=1 and missing from the transcript.
# 33 of 37 landed; these four did not. The two 5ab65c rows were still in memory (chat mid-turn).
PENDING_SESSION = "20261027_134353_5ab65c"
PENDING_TASKS = ("t_01a183148bf778c8", "t_01a1834ff6b03914")
LOST_SESSION = "20261026_235722_bef6c6"
LOST_TASKS = ("t_01a18481058a3df0", "t_01a1836a150478cb")
CLAIMED_PDT = 1793227349.0  # 2026-10-28 15:42:29 PDT, the 5ab65c claim time


def _dead_pid() -> int:
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _hub(tmp_path, cards, me="macbook"):
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta(k TEXT, v TEXT)")
        conn.execute("INSERT INTO meta VALUES ('gateway', ?)", (me,))
        conn.execute("CREATE TABLE replica(gw TEXT, coll TEXT, key TEXT, data TEXT, seq INTEGER)")
        for i, card in enumerate(cards, 1):
            conn.execute("INSERT INTO replica VALUES (?,?,?,?,?)",
                         ("hub", "tasks", card["key"], json.dumps(card["task"]), i))
    return path


def _card(tid, session, *, status, at, summary="handoff", final=None):
    task = {"id": tid, "status": status, "title": tid, "board": "default", "assignee": "default",
            "sessions": [{"gw": "macbook", "profile": "default", "session": session, "rel": "shepherd"}]}
    if status == "done":
        task["completed_at"] = at
        task["completion_notice"] = {"event_id": 9000 + (hash(tid) % 1000), "payload": {"summary": summary}}
        if final:
            task["completion_notice"]["payload"]["final_sends"] = {session: final}
            task["completion_notice"]["payload"]["summary"] = f"prose\nFINAL SEND for @session:default@macbook/{session}: {final}"
    else:
        task["run"] = {"ended_at": at, "summary": summary}
        task["block_kind"] = "needs_input"
    return {"key": f"default/default/{tid}", "task": task}


def _ledger_row(ledger, session, ref):
    with sqlite3.connect(ledger) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM notices WHERE session=? AND ref=?", (session, ref)).fetchone()
        return dict(row) if row else None


def _orphan_holder(ledger, session, ref):
    with sqlite3.connect(ledger) as conn:
        conn.execute("UPDATE notices SET holder_pid=?, holder_start=NULL WHERE session=? AND ref=?",
                     (_dead_pid(), session, ref))


def test_done_claim_survives_the_consumer_dying_before_the_turn(tmp_path):
    """A. Claimed while the chat is busy; the consumer dies before the turn; the next one delivers once and acks."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    card = _card("t_done", CHAT, status="done", at=now - 10, final="once")
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    first = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    assert len(first) == 1 and first[0]["event_key"]
    row = _ledger_row(ledger, CHAT, first[0]["ref"])
    assert row["delivered"] == 0 and row["holder_pid"] == os.getpid()
    _orphan_holder(ledger, CHAT, first[0]["ref"])

    again = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now,
                        transcript_has=lambda key, tid: False)
    assert [item["ref"] for item in again] == [first[0]["ref"]]
    assert krn.ack(again[0], ledger=ledger)
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []
    assert _ledger_row(ledger, CHAT, first[0]["ref"])["delivered"] == 1


def test_transcript_write_then_death_before_ack_is_not_delivered_twice(tmp_path):
    """B. Written, then the consumer dies before the ack: no second notice, and the row is acked."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    card = _card("t_written", CHAT, status="done", at=now - 5, final="kept")
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        db.record_kanban_notice(CHAT, got[0]["text"],
                                {"notice_id": "live:b", "kanban_event_keys": [got[0]["event_key"]]})
        _orphan_holder(ledger, CHAT, got[0]["ref"])

        def has(key, tid):
            return krn.notice_in_transcript(db, CHAT, key, tid)

        assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now, transcript_has=has) == []
        assert len(db.get_messages(CHAT)) == 1
    assert _ledger_row(ledger, CHAT, got[0]["ref"])["delivered"] == 1


def test_poller_and_sweep_share_one_key(tmp_path):
    """C (dedup half). The formatter is PR 177's. Poller + sweep still land one row."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    card = _card("t_shared", CHAT, status="done", at=now - 3, final="shared-key")
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        db.record_kanban_notice(CHAT, got[0]["text"],
                                {"notice_id": got[0]["event_key"], "kanban_event_keys": [got[0]["event_key"]]})

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger)
        assert len(db.get_messages(CHAT)) == 1
        assert "shared-key" in db.get_messages(CHAT)[0]["content"]


def test_blocked_claim_survives_the_consumer_dying(tmp_path):
    """D. A blocked remote notice has the same in-flight guarantee, and the sweep uses the same key."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    card = _card("t_blocked", CHAT, status="blocked", at=now - 8, summary="need a decision")
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    first = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    assert len(first) == 1 and first[0]["event_key"] and "blocked" in first[0]["text"]
    assert _ledger_row(ledger, CHAT, first[0]["ref"])["delivered"] == 0
    _orphan_holder(ledger, CHAT, first[0]["ref"])
    again = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    assert len(again) == 1 and again[0]["event_key"] == first[0]["event_key"]
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        db.record_kanban_notice(CHAT, again[0]["text"],
                                {"notice_id": again[0]["event_key"], "kanban_event_keys": [again[0]["event_key"]]})

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger)
        assert len(db.get_messages(CHAT)) == 1
    assert krn.ack(again[0], ledger=ledger)
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now) == []


def test_legacy_fresh_rows_reoffered_once_and_stale_rows_left(tmp_path, caplog):
    """E. The four missing 10-28 rows (inside FRESH_S, no transcript) are re-offered once. An older row is not."""
    import logging
    from tui_gateway import kanban_remote_notices as krn

    now = CLAIMED_PDT + 120
    pending = [_card(tid, PENDING_SESSION, status="done", at=CLAIMED_PDT - 30) for tid in PENDING_TASKS]
    lost = [_card(tid, LOST_SESSION, status="done", at=CLAIMED_PDT - 2500) for tid in LOST_TASKS]
    stale_card = _card("t_old", CHAT, status="done", at=now - krn.FRESH_S - 100)
    replica = _hub(tmp_path, pending + lost + [stale_card])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE notices (session TEXT, ref TEXT, state TEXT, at REAL, delivered INTEGER, "
                     "PRIMARY KEY (session, ref, state))")
        for card, session, at in (
            *[(c, PENDING_SESSION, CLAIMED_PDT) for c in pending],
            *[(c, LOST_SESSION, CLAIMED_PDT - 2400) for c in lost],
            (stale_card, CHAT, now - krn.FRESH_S - 50),
        ):
            task = card["task"]
            term = int(task.get("completed_at") or 0)
            conn.execute("INSERT INTO notices VALUES (?,?,?,?,1)",
                         (session, f"hub/{card['key']}", f"done@{term}", at))
    caplog.set_level(logging.INFO, logger="tui_gateway.kanban_remote_notices")
    got = krn.collect(PENDING_SESSION, "default", hub_db=replica, ledger=ledger, now=now,
                      transcript_has=lambda key, tid: False)
    assert {item["ref"].rsplit("/", 1)[-1] for item in got} == set(PENDING_TASKS)
    lost_got = krn.collect(LOST_SESSION, "default", hub_db=replica, ledger=ledger, now=now,
                           transcript_has=lambda key, tid: False)
    assert {item["ref"].rsplit("/", 1)[-1] for item in lost_got} == set(LOST_TASKS)
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now,
                       transcript_has=lambda key, tid: False) == []
    assert "left 1 delivered notice" in caplog.text
    assert krn.collect(PENDING_SESSION, "default", hub_db=replica, ledger=ledger, now=now,
                       transcript_has=lambda key, tid: False) == []


def test_sweep_skips_persisted_notices_without_opening_store_even_after_restart(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    krn._reset_for_tests()
    _tid, replica = _replicate(tmp_path, f"FINAL SEND for @session:default@macbook/{CHAT}: memo proof")
    ledger = tmp_path / "remote-notices.db"
    opens = []
    with SessionDB(tmp_path / "state.db") as db:
        for sid in (CHAT, OTHER):
            db.create_session(sid, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            opens.append(sid)
            yield db, db.get_compression_tip(sid) or sid

        def sweep():
            return sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger)

        assert len(sweep()) == 2
        assert len(opens) == 2
        for _ in range(3):
            assert sweep() == []
        assert len(opens) == 2
        krn._reset_for_tests()  # a restarted backend loads the ledger: no store opened, no notice re-written
        assert sweep() == []
        assert len(opens) == 2
        ledger.unlink()  # a lost ledger: re-checks once, finds the rows (still one notice per chat), records again
        for suffix in ("-wal", "-shm"):
            ledger.with_name(ledger.name + suffix).unlink(missing_ok=True)
        krn._reset_for_tests()
        assert sweep() == []  # transcript recovery restores receipts without another write
        with sqlite3.connect(ledger) as conn:
            assert conn.execute('SELECT delivered FROM notices').fetchall() == [(krn.DELIVERED,)] * 2
        assert len(opens) == 4 and len(db.get_messages(CHAT)) == 1
        krn._reset_for_tests()
        assert sweep() == [] and len(opens) == 4


def test_sweep_keeps_working_in_memory_when_the_ledger_is_unusable(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    krn._reset_for_tests()
    _tid, replica = _replicate(tmp_path, "unusable ledger proof")
    ledger = tmp_path / "remote-notices.db"
    ledger.mkdir()  # sqlite cannot open a directory
    opens = []
    with SessionDB(tmp_path / "state.db") as db:
        for sid in (CHAT, OTHER):
            db.create_session(sid, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            opens.append(sid)
            yield db, sid

        def sweep():
            return sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger)

        assert len(sweep()) == 2
        assert sweep() == [] and len(opens) == 2
        assert len(db.get_messages(CHAT)) == 1


def test_swept_rows_older_than_the_keep_window_are_pruned_on_load(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    krn._reset_for_tests()
    ledger = tmp_path / "remote-notices.db"
    conn = krn._ledger(ledger)
    with conn:
        krn._swept(conn)
        conn.executemany("INSERT INTO swept (replica, profile, session, notice_id, at) VALUES (?,?,?,?,?)", [
            ("hub.db", "default", CHAT, "old", time.time() - krn.SWEPT_KEEP_S - 60),
            ("hub.db", "default", CHAT, "new", time.time())])
    conn.close()
    krn._load_swept(ledger)
    assert ("hub.db", "default", CHAT, "new") in krn._delivered
    assert ("hub.db", "default", CHAT, "old") not in krn._delivered
    conn = krn._ledger(ledger)
    assert [r[0] for r in conn.execute("SELECT notice_id FROM swept")] == ["new"]
    conn.close()


def test_sweep_does_not_remember_a_notice_it_could_not_write(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    krn._reset_for_tests()
    _tid, replica = _replicate(tmp_path, "retry proof")
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        missing = True

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (None, None) if missing or sid != CHAT else (db, sid)

        ledger = tmp_path / "remote-notices.db"
        assert not sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger)
        krn._reset_for_tests()  # nothing was recorded for the unwritten notice, so a restart still retries it
        missing = False
        assert len(sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                        ledger=ledger)) == 1


# [daveey] 2026-10-29 00:22Z on the MacBook the unloaded-chat sweep appended 293 stale "blocked" notices into idle
# chats: frozen legacy-key replica rows (``<board>/<id>``, published before the owner switched to
# ``<profile>/<board>/<id>``) of cards done days earlier. Examples below are three of them.
REPLAYED = ("t_cc5aeb02", "t_414dafc6", "t_4bf9a311")
LEGACY_BLOCKED_AT = 1792843956.0  # 2026-10-24, "Held in ready by the respawn guard (active_pr) for 360 min"
SWEPT_AT = 1793233342.0  # 2026-10-29 00:22:22Z, first stale append


def _legacy(card: dict) -> dict:
    """The same card under the pre-board-qualified replica key."""
    return {"key": card["key"].split("/", 1)[1], "task": card["task"]}


def _sweep_db(tmp_path, sessions):
    db = SessionDB(tmp_path / "state.db")
    for sid in sessions:
        db.create_session(sid, "desktop")

    @contextlib.contextmanager
    def opener(profile, sid):
        yield (db, db.get_compression_tip(sid) or sid) if sid in sessions else (None, None)

    return db, opener


def test_sweep_does_not_replay_stale_blocked_legacy_rows_of_done_cards(tmp_path):
    """The 00:22Z incident: days-old legacy blocked rows beside current done rows append nothing."""
    from tui_gateway import kanban_remote_notices as krn

    cards = []
    for tid in REPLAYED:
        old = _card(tid, CHAT, status="blocked", at=LEGACY_BLOCKED_AT,
                    summary="Held in ready by the respawn guard (active_pr) for 360 min")
        cards.append(_legacy(old))
        cards.append(_card(tid, CHAT, status="done", at=LEGACY_BLOCKED_AT + 2 * 86400))
    replica = _hub(tmp_path, cards)
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    db, opener = _sweep_db(tmp_path, (CHAT,))
    with db:
        assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=ledger, now=SWEPT_AT) == []
        assert db.get_messages(CHAT) == []
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=SWEPT_AT,
                       transcript_has=lambda key, tid: False) == []


def test_sweep_skips_a_four_day_old_blocked_card_even_with_no_newer_row(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_card("t_old_blocked", CHAT, status="blocked", at=now - 4 * 86400)])
    krn._reset_for_tests()
    db, opener = _sweep_db(tmp_path, (CHAT,))
    with db:
        assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=tmp_path / "ledger.db", now=now) == []
        assert db.get_messages(CHAT) == []


def test_superseded_legacy_row_is_ignored_even_when_recent(tmp_path):
    """Canonical row wins regardless of age: a fresh legacy blocked row beside the current done row yields
    exactly the done notice, from the sweep and from the poller."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    stale_view = _legacy(_card("t_moved", CHAT, status="blocked", at=now - 600, summary="old block"))
    current = _card("t_moved", CHAT, status="done", at=now - 60, summary="real handoff")
    for order in ([stale_view, current], [current, stale_view]):
        sub = tmp_path / str(len(list(tmp_path.iterdir())))
        sub.mkdir()
        replica = _hub(sub, order)
        krn._reset_for_tests()
        got = krn.collect(OTHER, "default", hub_db=replica, ledger=sub / "ledger.db", now=now)
        assert got == []  # not linked to OTHER
        db, opener = _sweep_db(sub, (CHAT,))
        with db:
            out = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                       ledger=sub / "ledger.db", now=now)
            assert len(out) == 1
            texts = [m["content"] for m in db.get_messages(CHAT)]
            assert len(texts) == 1 and "done" in texts[0] and "old block" not in texts[0]
        krn._reset_for_tests()
        polled = krn.collect(CHAT, "default", hub_db=replica, ledger=sub / "poll-ledger.db", now=now)
        assert [p["state"].split("@")[0] for p in polled] == ["done"]


def test_sweep_skips_a_notice_the_ledger_settled_silently(tmp_path):
    """A notice collect() recorded without announcing (a local placeholder covers it) is not swept later."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_card("t_placeholder", CHAT, status="done", at=now - 30)])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now, skip=lambda tid: True) == []
    db, opener = _sweep_db(tmp_path, (CHAT,))
    with db:
        assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=ledger, now=now) == []
        assert db.get_messages(CHAT) == []


def test_sweep_still_delivers_genuine_fresh_notices_once(tmp_path):
    """Control: fresh done and blocked cards on an idle chat are each swept exactly once."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_card("t_fresh_done", CHAT, status="done", at=now - 120, summary="shipped"),
                              _card("t_fresh_blocked", CHAT, status="blocked", at=now - 90, summary="need a yes")])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    db, opener = _sweep_db(tmp_path, (CHAT,))
    with db:
        out = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)
        assert sorted(item["task_id"] for item in out) == ["t_fresh_blocked", "t_fresh_done"]
        krn._reset_for_tests()  # a restarted backend: the ledger and transcript keys still dedupe
        assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=ledger, now=now) == []
        texts = [m["content"] for m in db.get_messages(CHAT)]
        assert len(texts) == 2 and any("need a yes" in t for t in texts)
    for item in ("t_fresh_done", "t_fresh_blocked"):
        assert _ledger_row(ledger, CHAT, f"hub/default/default/{item}")["delivered"] == 1


def test_canonical_drops_only_superseded_legacy_rows():
    from tui_gateway import kanban_remote_notices as krn

    rows = [("hub", "default/t_a", 1, "legacy-a"), ("hub", "default/default/t_a", 9, "current-a"),
            ("hub", "other/default/t_a", 5, "other-profile-a"),  # same board + id, another profile: kept
            ("hub", "bb/t_orphan", 2, "legacy-only"),  # no current row: kept (freshness decides)
            ("mini", "default/t_a", 3, "other-gateway-legacy")]  # another owner gateway: kept
    assert sorted(r[3] for r in krn._canonical(rows)) == [
        "current-a", "legacy-only", "other-gateway-legacy", "other-profile-a"]


def test_both_remote_writers_record_blocked_on_human(tmp_path, monkeypatch):
    """The notice guarantee cannot look a remote card up locally, so both writers store its human-block state."""
    from tui_gateway import kanban_remote_notices as krn
    from tui_gateway import server

    human = {"action": "Reply ship or hold here. Unblock = deploy; archive = drop it.",
             "why_human": "Only David approves prod.", "interaction": "approval", "scope": "prod deploy",
             "evidence": "CI green"}
    now = time.time()
    asks = _card("t_asks", CHAT, status="blocked", at=now - 60, summary="need a yes")
    asks["task"]["human_request"] = human
    plain = _card("t_plain", CHAT, status="blocked", at=now - 50, summary="tool broke")
    plain["task"]["block_kind"] = "transient"
    done = _card("t_done2", CHAT, status="done", at=now - 40, summary="shipped")
    replica = _hub(tmp_path, [asks, plain, done])

    krn._reset_for_tests()
    db, opener = _sweep_db(tmp_path, (CHAT,))
    with db:
        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                             ledger=tmp_path / "sweep.db", now=now)
        flags = {m["display_metadata"]["task_id"]: m["display_metadata"].get("blocked_on_human")
                 for m in db.get_messages(CHAT)}
    assert flags == {"t_asks": True, "t_plain": False, "t_done2": False}

    krn._reset_for_tests()
    got = {item["ref"].rsplit("/", 1)[1]: item.get("blocked_on_human")
           for item in krn.collect(CHAT, "default", hub_db=replica, ledger=tmp_path / "poll.db", now=now)}
    assert got == {"t_asks": True, "t_plain": False, "t_done2": False}

    # The open-chat poller's persisted row carries it too (any remote item in the batch blocked on a human).
    monkeypatch.setattr(krn, "hub_db_path", lambda: replica)
    monkeypatch.setattr(krn, "collect", lambda *a, **k: [
        {"text": "⏸ t_asks blocked", "event_key": "remote:hub:default/default/t_asks:x", "blocked_on_human": True,
         "ref": "hub/default/default/t_asks", "state": "blocked@1", "session": CHAT}])
    texts = server._collect_remote_kanban_notifications(CHAT, None)
    with SessionDB(tmp_path / "live.db") as live:
        live.create_session(CHAT, "desktop")
        monkeypatch.setattr(server, "_get_db", lambda: live)
        verdict, _batch, _kwargs = server._kb_persist_batch({"session_key": CHAT, "history": []}, texts, False)
        assert verdict == "written"
        rows = [m for m in live.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]
    assert rows[-1]["display_metadata"].get("blocked_on_human") is True


# [daveey] MacBook remote-notices.db row 842 (2026-10-28 22:51 PDT): t_01a18622e706e70a's blocked notice was claimed
# by pid 35287, which died before the transcript write. The card was unblocked at 23:20, so neither collect nor the
# sweep (both walk terminal cards only) looked at the row again: delivered=0 with a dead holder for over 24 h.
ROW_842_TASK = "t_01a18622e706e70a"
ROW_842_BLOCKED_AT = 1793253046


def test_sweep_settles_a_dead_holder_claim_whose_card_left_that_state(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    now = ROW_842_BLOCKED_AT + 30 * 3600.0
    moved = _card(ROW_842_TASK, CHAT, status="ready", at=ROW_842_BLOCKED_AT)  # unblocked: no longer terminal
    held = _card("t_still_blocked", CHAT, status="blocked", at=ROW_842_BLOCKED_AT)  # still in the claimed state
    alive = _card("t_live_holder", CHAT, status="ready", at=ROW_842_BLOCKED_AT)  # moved on, but holder is alive
    replica = _hub(tmp_path, [moved, held, alive])
    ledger = tmp_path / "remote-notices.db"
    state = f"blocked@{ROW_842_BLOCKED_AT}"
    dead = _dead_pid()
    me, start = kbn._my_holder()
    krn._reset_for_tests()
    conn = krn._ledger(ledger)
    with conn:
        for card, pid, pstart in ((moved, dead, None), (held, dead, None), (alive, me, start)):
            ref = f"hub/{card['key']}"
            conn.execute("INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, "
                         "notice_key, legacy_seen) VALUES (?,?,?,?,0,?,?,?,1)",
                         (CHAT, ref, state, ROW_842_BLOCKED_AT + 5, pid, pstart,
                          f"remote:hub:{card['key']}:{state}:default:{CHAT}"))
    conn.close()

    db, opener = _sweep_db(tmp_path, (CHAT,))
    with db:
        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)
        assert not any(ROW_842_TASK in (m.get("content") or "") for m in db.get_messages(CHAT))  # no stale text

    row = _ledger_row(ledger, CHAT, f"hub/{moved['key']}")
    # [daveey] SETTLED, not DELIVERED: no transcript row was written (MacBook row 842 was re-settled as delivered=1
    # with swept reason=delivered at 23:34 PDT 2026-10-29, after 9eb41135c6 made delivered=1 mean "in the transcript").
    assert (row["delivered"], row["holder_pid"], row["holder_start"], row.get("settled")) == (
        krn.SETTLED, None, None, "stale")
    with sqlite3.connect(ledger) as c:
        swept = c.execute("SELECT replica, profile, session, notice_id, reason FROM swept").fetchall()
    assert swept == [(str(replica), "default", CHAT, f"remote:hub:{moved['key']}:{state}:default:{CHAT}", "stale")]
    for card, pid in ((held, dead), (alive, me)):
        other = _ledger_row(ledger, CHAT, f"hub/{card['key']}")
        assert (other["delivered"], other["holder_pid"], other.get("settled")) == (0, pid, None)

    # Idempotent; the still-terminal row stays with collect's dead-holder re-offer path.
    sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)
    assert _ledger_row(ledger, CHAT, f"hub/{held['key']}")["delivered"] == 0


def test_every_delivered_ack_has_a_transcript_row_after_a_stale_settle(tmp_path):
    """[daveey] The ledger invariant the readback checks: a ``delivered=1`` row is a notice in the chat. A stale
    settle writes no row, so it must leave no DELIVERED row and no ``swept`` row claiming a delivery."""
    from tui_gateway import kanban_remote_notices as krn

    now = ROW_842_BLOCKED_AT + 30 * 3600.0
    moved = _card(ROW_842_TASK, CHAT, status="ready", at=ROW_842_BLOCKED_AT)
    replica = _hub(tmp_path, [moved])
    ledger = tmp_path / "remote-notices.db"
    state = f"blocked@{ROW_842_BLOCKED_AT}"
    krn._reset_for_tests()
    conn = krn._ledger(ledger)
    with conn:
        conn.execute("INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, notice_key, "
                     "legacy_seen) VALUES (?,?,?,?,0,?,NULL,?,1)",
                     (CHAT, f"hub/{moved['key']}", state, ROW_842_BLOCKED_AT + 5, _dead_pid(),
                      f"remote:hub:{moved['key']}:{state}:default:{CHAT}"))
    conn.close()

    db, opener = _sweep_db(tmp_path, (CHAT,))
    with db:
        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)
        transcript = [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]
    with sqlite3.connect(ledger) as c:
        acks = c.execute("SELECT count(*) FROM notices WHERE delivered=?", (krn.DELIVERED,)).fetchone()[0]
        reasons = [r for (r,) in c.execute("SELECT reason FROM swept")]
    assert transcript == []
    assert acks == len(transcript) == 0
    assert reasons == ["stale"]
    # Settled for every reader: the ledger skip used by the sweep and collect treats it as done.
    with contextlib.closing(krn._ledger(ledger)) as c:
        assert krn._ledger_settled(c, (CHAT,), f"hub/{moved['key']}", state)


def _adopted(card, origin_session, *, gateway="macbook", shepherd=False):
    """``card`` re-shaped as an adopted card: its chat is named only by an ``origin_bindings`` marker."""
    task = card["task"]
    task["origin_bindings"] = [{"binding_id": "binding-x", "version": 1, "task_record_key": card["key"],
                                "origin": {"gateway": gateway, "profile": "default", "session": origin_session},
                                "owner": {"board": "default", "gateway": "hub", "profile": "default",
                                          "task_id": task["id"]}}]
    if not shepherd:
        task["sessions"] = [{"gw": "macbook", "profile": "default", "session": OTHER, "rel": "shepherd"}]
    return card


def test_sweep_notifies_the_origin_chat_of_an_adopted_card(tmp_path):
    """[daveey] An adopted card's origin chat gets the done notice although no shepherd link names it
    (hub t_a989e5e0 reached five unrelated shepherds and never chat 20261026_110445_20914b)."""
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_adopted(_card("t_adopted", CHAT, status="done", at=now - 4), CHAT)])
    krn._reset_for_tests()
    with SessionDB(tmp_path / "state.db") as db:
        for sid in (CHAT, OTHER):
            db.create_session(sid, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield db, sid

        got = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=tmp_path / "ledger.db", now=now)
        assert sorted(d["session_id"] for d in got) == sorted([CHAT, OTHER])
        assert [m["display_kind"] for m in db.get_messages(CHAT)] == ["kanban_notice"]
        assert "t_adopted" in db.get_messages(CHAT)[0]["content"]


def test_poller_collect_returns_the_adopted_cards_notice_to_its_origin_chat(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_adopted(_card("t_adopted_poll", CHAT, status="done", at=now - 4), CHAT)])
    krn._reset_for_tests()
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=tmp_path / "ledger.db", now=now)
    assert [g["ref"].rsplit("/", 1)[-1] for g in got] == ["t_adopted_poll"]


def test_origin_that_is_also_a_shepherd_gets_one_notice_and_foreign_origins_none(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    both = _adopted(_card("t_both", CHAT, status="done", at=now - 4), CHAT, shepherd=True)
    foreign = _adopted(_card("t_foreign", OTHER, status="done", at=now - 4), CHAT, gateway="mini")
    foreign["task"]["sessions"] = []
    replica = _hub(tmp_path, [both, foreign])
    krn._reset_for_tests()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield db, sid

        got = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=tmp_path / "ledger.db", now=now)
        assert [(d["session_id"], d["task_id"]) for d in got] == [(CHAT, "t_both")]
        assert len(db.get_messages(CHAT)) == 1
    assert [g["ref"].rsplit("/", 1)[-1] for g in krn.collect(CHAT, "default", hub_db=replica,
                                                               ledger=tmp_path / "ledger2.db", now=now)] == ["t_both"]


def test_every_silent_sweep_skip_names_its_reason_once(tmp_path, caplog):
    """t_01a1d46be379dfd3: t_01a1d405586c5ba1's notice was held in flight by another Desktop backend for 30 min
    and the sweep skipped it with no log line, so the delay was misread as 46 min of discovery lag. Each skip
    (claim in flight elsewhere, chat open here) and each live-poller claim logs once per (chat, notice, reason)."""
    import logging
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    card = _card("t_waits", CHAT, status="done", at=now - 40)
    replica = _hub(tmp_path, [card])
    ledger = tmp_path / "ledger.db"
    ref, state = "hub/default/default/t_waits", krn._notice_state(card["task"])
    krn._reset_for_tests()
    krn._ledger(ledger).close()
    other = os.getppid()  # a live backend that is not this process
    with sqlite3.connect(ledger) as conn:
        conn.execute("INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, notice_key, "
                     "legacy_seen) VALUES (?,?,?,?,0,?,NULL,?,0)", (CHAT, ref, state, now - 30, other, "k"))
    woken = []

    class Waker:
        grace_s = 0.0

        def deliver(self, tip, text, metadata, task, now, *, persist, started):
            woken.append(task["id"])
            return "started"

    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        caplog.set_level(logging.INFO, logger=krn.__name__)
        for _ in range(3):
            sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                 now=now, waker=Waker())
        for _ in range(2):
            sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                 now=now, waker=Waker(), live_ids={CHAT})
        assert woken == []
        with sqlite3.connect(ledger) as conn:
            conn.execute("DELETE FROM notices WHERE session=? AND ref=?", (CHAT, ref))  # the holder released it
        for _ in range(2):
            krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
            with sqlite3.connect(ledger) as conn:  # a non-owner hands it straight back (release) and re-claims
                conn.execute("DELETE FROM notices WHERE session=? AND ref=?", (CHAT, ref))

    waits = [r.getMessage() for r in caplog.records if " waits: " in r.getMessage()]
    assert waits == [
        f"remote Kanban notice {ref} {state} for chat {CHAT} waits: in flight under backend pid {other} until that "
        "claim is acked or released",
        f"remote Kanban notice {ref} {state} for chat {CHAT} waits: chat is open in this backend; its live poller "
        "claims it",
        f"remote Kanban notice {ref} {state} for chat {CHAT} waits: claimed by the live poller of backend pid "
        f"{os.getpid()}",
    ]


class _GraceWaker:
    def __init__(self, grace_s=0.0):
        self.grace_s, self.woken = grace_s, []

    def deliver(self, tip, text, metadata, task, now, *, persist, started):
        self.woken.append(task["id"])
        return "started"


@contextlib.contextmanager
def _chat_db(tmp_path):
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        yield opener


def _wait_lines(caplog, level=None):
    return [r.getMessage() for r in caplog.records
            if " waits: " in r.getMessage() and (level is None or r.levelno == level)]


def test_collect_still_offers_its_claim_when_the_wait_log_raises(tmp_path, monkeypatch, caplog):
    """PR #2116 review: the claim log ran between _claim_notice's offer and out.append, so an exception there
    (eviction racing a concurrent sweep) fell into collect's outer except and stranded the claim in flight
    under this pid. The log is after the append now and never raises."""
    import logging
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_card("t_logboom", CHAT, status="done", at=now - 40)])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()

    def boom(*a, **k):
        raise RuntimeError("dictionary changed size during iteration")

    caplog.set_level(logging.INFO, logger=krn.__name__)
    monkeypatch.setattr(krn.logger, "info", boom)
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    assert [g["ref"].rsplit("/", 1)[-1] for g in got] == ["t_logboom"]
    row = _ledger_row(ledger, CHAT, "hub/default/default/t_logboom")
    assert row is not None and row["delivered"] == 0 and row["holder_pid"] == os.getpid()  # in flight AND offered


def test_open_chat_skip_logs_info_only_when_unclaimed_past_the_grace_window(tmp_path, caplog):
    """PR #2116 review: the healthy path logged two INFO lines per notice (the sweep's open-chat skip and the
    poller's claim). Within the grace window, or once this backend's poller claimed it, the skip is DEBUG."""
    import logging
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_card("t_open", CHAT, status="done", at=now - 40)])
    ref = "hub/default/default/t_open"
    caplog.set_level(logging.DEBUG, logger=krn.__name__)
    with _chat_db(tmp_path) as opener:
        krn._reset_for_tests()
        ledger = tmp_path / "healthy.db"
        sweep = dict(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                     waker=_GraceWaker(60.0), live_ids={CHAT})
        sweep_remote_notices(now=now, **sweep)  # inside the grace window
        assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + 1)
        sweep_remote_notices(now=now + 120, **sweep)  # past it, but our poller holds the claim
        info = _wait_lines(caplog, logging.INFO)
        assert len(info) == 1 and "claimed by the live poller" in info[0], info

        caplog.clear()
        krn._reset_for_tests()
        sweep["ledger"] = tmp_path / "stuck.db"
        sweep_remote_notices(now=now, **sweep)
        assert _wait_lines(caplog, logging.INFO) == []
        sweep_remote_notices(now=now + 120, **sweep)  # still unclaimed past the window: worth an INFO line
        assert [line.split(" waits: ")[1] for line in _wait_lines(caplog, logging.INFO)] == [
            "chat is open in this backend; its live poller claims it"]
        assert ref in _wait_lines(caplog, logging.INFO)[0]


def test_sweep_names_its_own_in_flight_claim_and_the_gateway_fifo_once(tmp_path, monkeypatch, caplog):
    """PR #2116 review: _wait_reason's own-holder branch and the FIFO skip had no test."""
    import logging
    from tui_gateway import kanban_remote_fifo
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    card = _card("t_mine", CHAT, status="done", at=now - 40)
    replica = _hub(tmp_path, [card])
    ref, state = "hub/default/default/t_mine", krn._notice_state(card["task"])
    caplog.set_level(logging.INFO, logger=krn.__name__)
    with _chat_db(tmp_path) as opener:
        krn._reset_for_tests()
        ledger = tmp_path / "mine.db"
        krn._ledger(ledger).close()
        with sqlite3.connect(ledger) as conn:  # this backend claimed it; its turn has not written the row yet
            conn.execute("INSERT INTO notices (session, ref, state, at, delivered, holder_pid, holder_start, "
                         "notice_key, legacy_seen) VALUES (?,?,?,?,0,?,NULL,?,0)",
                         (CHAT, ref, state, now - 30, os.getpid(), "k"))
        waker = _GraceWaker()
        for _ in range(2):
            sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                 now=now, waker=waker)
        assert waker.woken == []
        assert _wait_lines(caplog) == [
            f"remote Kanban notice {ref} {state} for chat {CHAT} waits: in flight in this backend (pid "
            f"{os.getpid()}) until its turn's row is written"]

        caplog.clear()
        krn._reset_for_tests()
        held = []
        monkeypatch.setattr(kanban_remote_fifo, "gateway_holds", lambda db, ids, key: held.append(key) or True)
        waker = _GraceWaker()
        for _ in range(2):
            sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                 ledger=tmp_path / "fifo.db", now=now, waker=waker)
        assert held and waker.woken == []
        assert _wait_lines(caplog) == [
            f"remote Kanban notice {ref} {state} for chat {CHAT} waits: left to the gateway's FIFO (the gateway "
            "runs this chat's turns)"]


def test_open_chat_skip_logs_info_once_a_poller_claim_is_released_undelivered(tmp_path, caplog):
    """PR #2158 review: a stored "claimed by the live poller" marker counted as healthy forever, so a notice whose
    claim was later released undelivered (release_undelivered, or a crashed turn's claim) only logged DEBUG past
    the grace window. The sweep now reads the claim's ledger row: no row, or a dead holder, is stuck."""
    import logging
    from tui_gateway import kanban_remote_late
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_card("t_released", CHAT, status="done", at=now - 40)])
    ref = "hub/default/default/t_released"
    caplog.set_level(logging.DEBUG, logger=krn.__name__)
    with _chat_db(tmp_path) as opener:
        for case in ("released", "crashed"):
            caplog.clear()
            krn._reset_for_tests()
            ledger = tmp_path / f"{case}.db"
            sweep = dict(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                         waker=_GraceWaker(60.0), live_ids={CHAT})
            claims = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + 1)
            assert claims
            if case == "released":
                kanban_remote_late.release_undelivered(claims, ledger)
                assert _ledger_row(ledger, CHAT, ref) is None
            else:
                _orphan_holder(ledger, CHAT, ref)
            sweep_remote_notices(now=now + 120, **sweep)
            stuck = [line for line in _wait_lines(caplog, logging.INFO) if "claimed by the live poller" not in line]
            assert [line.split(" waits: ")[1] for line in stuck] == [
                "chat is open in this backend; its live poller claims it"], (case, stuck)


def test_open_chat_skip_finds_the_poller_claim_of_a_card_linked_to_a_lineage_ancestor(tmp_path, caplog):
    """PR #2158 review: collect recorded its claim under the live session key while the sweep looked it up by the
    link's session, so a card linked to a compacted ancestor still logged one INFO line past grace."""
    import logging
    from tui_gateway import kanban_remote_notices as krn

    now = time.time()
    replica = _hub(tmp_path, [_card("t_ancestor", OTHER, status="done", at=now - 40)])
    caplog.set_level(logging.DEBUG, logger=krn.__name__)
    with _chat_db(tmp_path) as opener:
        krn._reset_for_tests()
        ledger = tmp_path / "ancestor.db"
        claims = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now + 1, lineage=(OTHER,))
        assert [c["session"] for c in claims] == [CHAT]  # claimed under the live session key, not the link's
        sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                             waker=_GraceWaker(60.0), live_ids={CHAT, OTHER}, now=now + 120)
        info = _wait_lines(caplog, logging.INFO)
        assert len(info) == 1 and "claimed by the live poller" in info[0], info
        assert all(krn.ack(c, ledger=ledger) for c in claims)  # delivered: final, and still no open-chat INFO
        for later in (180, 240):
            sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                 waker=_GraceWaker(60.0), live_ids={CHAT, OTHER}, now=now + later)
        assert _wait_lines(caplog, logging.INFO) == info
        assert krn._poller_claims[(OTHER, claims[0]["ref"], claims[0]["state"])] == krn._CLAIM_DELIVERED
