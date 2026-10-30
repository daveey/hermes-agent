"""A remote notice's ledger ack means a transcript row. [daveey]

2026-10-29, David-MBP ledger row 1245: hub card t_b49c4888 blocked again (blocked@1793336479). A Desktop backend
claimed the notice for chat 20261026_000514_23ef49 at 22:02:26 PDT and was replaced by the 22:06 core update
before the chat took a turn. Its successor found the dead holder and asked the transcript; the transcript check
also matched any ``Kanban t_b49c4888 `` line, and the chat had one from the card's EARLIER block (16:03 PDT,
blocked@1793312960). So the new block was acked delivered=1 with no transcript row and no ``swept`` row.
"""

import contextlib
import os
import sqlite3
import time

from hermes_state import SessionDB
from tui_gateway import kanban_remote_notices as krn
from tui_gateway.kanban_remote_notices import sweep_remote_notices

from tests.tui_gateway.test_kanban_remote_notices import _card, _dead_pid, _hub, _ledger_row

CHAT = "20261026_000514_23ef49"
TID = "t_b49c4888"
REF = f"hub/default/default/{TID}"


def _earlier_block_notice(db, now):
    """The 16:03 notice of the card's previous block: its own key, and a ``Kanban <id> blocked`` line."""
    old = _card(TID, CHAT, status="blocked", at=now - 6 * 3600 + 600, summary="needs you in Slack")
    key = krn._shared_key("hub", f"default/default/{TID}", old["task"], "default", CHAT)
    text = krn.format_notice("hub", f"default/default/{TID}", old["task"])
    assert f"Kanban {TID} " in text
    db.record_kanban_notice(CHAT, text, {"notice_id": key, "kanban_event_keys": [key]})
    return key


def _orphan(ledger):
    with sqlite3.connect(ledger) as conn:
        conn.execute("UPDATE notices SET holder_pid=?, holder_start=NULL WHERE session=? AND ref=?",
                     (_dead_pid(), CHAT, REF))


def _swept_reasons(ledger):
    with contextlib.closing(sqlite3.connect(ledger)) as conn:
        return {row[0]: row[1] for row in conn.execute("SELECT notice_id, reason FROM swept WHERE session=?", (CHAT,))}


def test_dead_holder_claim_is_reoffered_when_only_an_earlier_state_is_in_the_transcript(tmp_path):
    """Row 1245: the poller path must re-offer the new block, not ack it against the old block's notice."""
    now = time.time()
    replica = _hub(tmp_path, [_card(TID, CHAT, status="blocked", at=now - 60, summary="resume to fix ingress")])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        old_key = _earlier_block_notice(db, now)

        def has(key, tid):
            return krn.notice_in_transcript(db, CHAT, key, tid)

        first = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now, transcript_has=has)
        assert [item["ref"] for item in first] == [REF] and first[0]["event_key"] != old_key
        _orphan(ledger)  # the claiming backend was replaced before the chat's turn

        again = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now, transcript_has=has)

        assert [item["event_key"] for item in again] == [first[0]["event_key"]]
        row = _ledger_row(ledger, CHAT, REF)
        assert row["delivered"] == 0 and row["holder_pid"] == os.getpid()
        assert first[0]["event_key"] not in db.kanban_notice_keys_delivered(CHAT, [first[0]["event_key"]])


def test_sweeper_does_not_ack_a_dead_claim_against_an_earlier_state(tmp_path):
    """The unloaded sweeper's claim path uses the same check: it delivers the new block instead of acking it."""
    now = time.time()
    replica = _hub(tmp_path, [_card(TID, CHAT, status="blocked", at=now - 120, summary="resume to fix ingress")])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    wakes = []

    class Waker:
        grace_s = 30.0

        def deliver(self, tip, text, metadata, task, at, *, persist, started):
            wakes.append(metadata["notice_id"])
            return "persisted" if persist() else "deferred"

    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        _earlier_block_notice(db, now)
        assert len(krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)) == 1
        _orphan(ledger)

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        out = sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                   now=now, waker=Waker())

        assert [item["task_id"] for item in out] == [TID] and len(wakes) == 1
        rows = [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]
        assert len(rows) == 2 and "resume to fix ingress" in rows[-1]["content"]
    assert _ledger_row(ledger, CHAT, REF)["delivered"] == 1
    assert _swept_reasons(ledger)[wakes[0]] == "delivered"


def test_dead_holder_claim_whose_key_is_in_the_transcript_still_acks(tmp_path):
    """Control: a real delivery (the claim's own key is in the transcript) acks with no second notice, and the
    settle is recorded in ``swept`` with its reason."""
    now = time.time()
    replica = _hub(tmp_path, [_card(TID, CHAT, status="blocked", at=now - 60, summary="resume to fix ingress")])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")
        _earlier_block_notice(db, now)

        def has(key, tid):
            return krn.notice_in_transcript(db, CHAT, key, tid)

        first = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now, transcript_has=has)
        db.record_kanban_notice(CHAT, first[0]["text"],
                                {"notice_id": "live:x", "kanban_event_keys": [first[0]["event_key"]]})
        _orphan(ledger)

        assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now, transcript_has=has) == []
        assert len(db.get_messages(CHAT)) == 2
    assert _ledger_row(ledger, CHAT, REF)["delivered"] == 1
    assert _swept_reasons(ledger)[first[0]["event_key"]] == "transcript"


def test_live_ack_after_the_transcript_write_marks_delivered(tmp_path):
    """Control: the normal path (claim, write, ack) still ends delivered=1."""
    now = time.time()
    replica = _hub(tmp_path, [_card(TID, CHAT, status="blocked", at=now - 60)])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
    assert _ledger_row(ledger, CHAT, REF)["delivered"] == 0
    assert krn.ack(got[0], ledger=ledger)
    assert _ledger_row(ledger, CHAT, REF)["delivered"] == krn.DELIVERED == 1


def test_silent_settles_are_not_recorded_as_delivered(tmp_path):
    """A stale state or one a local placeholder reports is SETTLED with a ``swept`` reason, never delivered=1,
    and the sweeper still leaves it alone."""
    now = time.time()
    stale = _card("t_stale", CHAT, status="done", at=now - krn.FRESH_S - 600)
    covered = _card("t_covered", CHAT, status="done", at=now - 30)
    replica = _hub(tmp_path, [stale, covered])
    ledger = tmp_path / "ledger.db"
    with sqlite3.connect(replica) as conn:  # a synchronized replica: its first snapshot is the baseline
        conn.executemany("INSERT INTO meta VALUES (?,?)", [("cursor", "5"), ("head", "5")])
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now,
                       skip=lambda tid: tid == "t_covered") == []
    for tid in ("t_stale", "t_covered"):
        assert _ledger_row(ledger, CHAT, f"hub/default/default/{tid}")["delivered"] == krn.SETTLED
    reasons = _swept_reasons(ledger)
    assert sorted(reasons.values()) == ["placeholder", "stale"]
    krn._reset_for_tests()
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield (db, sid) if sid == CHAT else (None, None)

        assert sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger,
                                    now=now) == []
        assert db.get_messages(CHAT) == []
