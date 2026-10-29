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


def test_sweep_skips_persisted_notices_without_opening_store_until_restart(tmp_path):
    from tui_gateway import kanban_remote_notices as krn

    krn._reset_for_tests()
    _tid, replica = _replicate(tmp_path, f"FINAL SEND for @session:default@macbook/{CHAT}: memo proof")
    opens = []
    with SessionDB(tmp_path / "state.db") as db:
        for sid in (CHAT, OTHER):
            db.create_session(sid, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            opens.append(sid)
            yield db, db.get_compression_tip(sid) or sid

        assert len(sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)) == 2
        assert len(opens) == 2
        for _ in range(3):
            assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener) == []
        assert len(opens) == 2
        krn._reset_for_tests()  # a restarted backend re-checks once and finds the rows: still one notice per chat
        assert len(sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)) == 2
        assert len(opens) == 4 and len(db.get_messages(CHAT)) == 1


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

        assert not sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)
        missing = False
        assert len(sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener)) == 1
