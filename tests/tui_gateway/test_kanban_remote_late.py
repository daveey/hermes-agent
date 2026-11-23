"""Late arrivals are one explicit catch-up notice, with custody for every member."""
import contextlib
import json
import os
import sqlite3
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_state import SessionDB
from tests.tui_gateway.test_kanban_remote_arrival import _hub
from tests.tui_gateway.test_kanban_remote_notices import CHAT, _card
from tui_gateway import kanban_remote_notices as krn


@pytest.fixture
def arrival(tmp_path, monkeypatch):
    krn._reset_for_tests()
    replica = _hub(tmp_path, [])
    ledger = tmp_path / "ledger.db"
    monkeypatch.setattr(krn, "ledger_path", lambda: ledger)
    monkeypatch.setattr(krn, "hub_db_path", lambda: replica)
    now = 1_800_000_000.0
    assert krn.collect(CHAT, "default", now=now - 60) == []

    def insert(count, status, age):
        cards = [_card(f"t_late_{i:02}", CHAT, status=status, at=now - age - i * 60)
                 for i in range(count)]
        with sqlite3.connect(replica) as conn:
            for i, card in enumerate(cards, 1):
                conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)",
                             (card["key"], json.dumps(card["task"]), i))
        return cards

    with SessionDB(tmp_path / "state.db") as db:
        db.create_session(CHAT, "desktop")

        @contextlib.contextmanager
        def opener(profile, sid):
            yield db, sid

        yield replica, ledger, now, insert, db, opener


def _rows(ledger):
    with sqlite3.connect(ledger) as conn:
        return conn.execute("SELECT notice_key, delivered FROM notices ORDER BY ref").fetchall()


@pytest.mark.parametrize("door", ["collect", "sweep", "passive"])
@pytest.mark.parametrize("status", ["done", "blocked"])
@pytest.mark.parametrize("count", [1, 5, 23])
def test_late_arrivals_are_one_durable_group(arrival, door, status, count):
    replica, ledger, now, insert, db, opener = arrival
    cards = insert(count, status, 8 * 3600)
    wakes = []

    class Waker:
        grace_s = 0

        def deliver(self, tip, text, metadata, task, at, *, persist, started):
            wakes.append(text)
            assert all(delivered == 0 for _, delivered in _rows(ledger))
            assert persist()
            return "persisted"

    if door == "collect":
        got = krn.collect(CHAT, "default", now=now)
        assert len(got) == 1
        group = got[0]
        text = group["text"]
        keys = group.get("event_keys", [group["event_key"]])
        assert len(keys) == count
        assert len(_rows(ledger)) == count
        assert all(delivered == 0 for _, delivered in _rows(ledger))
        db.append_kanban_notice(CHAT, text, {"notice_id": keys[0], "kanban_event_keys": keys})
        assert krn.ack(group)
    else:
        got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                       ledger=ledger, now=now, waker=Waker() if door == "sweep" else None)
        assert len(got) == 1
        assert len(wakes) == (1 if door == "sweep" else 0)
        text = db.get_messages(CHAT)[0]["content"]
    assert text.startswith("Late notices")
    for card in cards[:20]:
        owner_at = krn._fresh_at(card["task"])
        assert owner_at is not None
        stamp = datetime.fromtimestamp(owner_at, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
        assert stamp in text
        assert card["task"]["id"] in text
    assert "8h" in text and "not delivered then" in text
    assert text.count("not delivered then") == min(count, 20)
    if count > 20:
        assert "+3 more" in text and cards[20]["task"]["id"] not in text
    assert len(_rows(ledger)) == count
    assert all(delivered == 1 for _, delivered in _rows(ledger))
    keys = [key for key, _ in _rows(ledger)]
    assert db.kanban_notice_keys_delivered(CHAT, keys) == set(keys)
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", now=now) == []
    assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=ledger, now=now) == []
    assert len(db.get_messages(CHAT)) == 1


def test_poller_persists_all_group_keys_before_ack(arrival, monkeypatch):
    from tui_gateway import server

    _, ledger, now, insert, db, _ = arrival
    insert(5, "done", 8 * 3600)
    collect = krn.collect
    monkeypatch.setattr(krn, "collect", lambda *a, **k: collect(*a, **{**k, "now": now}))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    claims = []
    texts = server._collect_remote_kanban_notifications(CHAT, claims)
    assert len(texts) == 1 and len(texts[0]._kb_keys) == 5
    session = {"session_key": CHAT, "history": [], "_kanban_claims": claims}
    server._kb_ack_durable(session, set())
    assert {delivered for _, delivered in _rows(ledger)} == {0}
    verdict, _, _ = server._kb_persist_batch(session, texts, False)
    assert verdict == "written"
    server._kb_ack_durable(session, {id(texts[0])})
    keys = [key for key, _ in _rows(ledger)]
    assert len(keys) == 5 and db.kanban_notice_keys_delivered(CHAT, keys) == set(keys)
    assert {delivered for _, delivered in _rows(ledger)} == {1}


@pytest.mark.parametrize("written", [False, True])
@pytest.mark.parametrize("door", ["collect", "sweep"])
def test_group_crash_recovery(arrival, monkeypatch, written, door):
    replica, ledger, now, insert, db, opener = arrival
    insert(5, "done", 8 * 3600)
    metadata = []

    class Waker:
        grace_s = 0

        def deliver(self, tip, text, meta, task, at, *, persist, started):
            metadata.append(meta)
            assert not started()  # admission is not delivery
            if written:
                db.append_kanban_notice(tip, text, meta)
            return "started"

    if door == "collect":
        first = krn.collect(CHAT, "default", now=now)[0]
        if written:
            db.append_kanban_notice(CHAT, first["text"],
                                   {"notice_id": first["event_key"], "kanban_event_keys": first["event_keys"]})
    else:
        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                 ledger=ledger, now=now, waker=Waker())
        assert len(metadata) == 1 and len(metadata[0]["kanban_event_keys"]) == 5
    assert len(_rows(ledger)) == 5 and {d for _, d in _rows(ledger)} == {0}
    with sqlite3.connect(ledger) as conn:
        conn.execute("UPDATE notices SET holder_pid=-1, holder_start=NULL")
    krn._reset_for_tests()
    monkeypatch.setattr(krn, "_holder_alive", lambda *a: False)
    monkeypatch.setattr(krn, "_holder_is_me", lambda pid: pid == os.getpid())
    if door == "collect":
        again = krn.collect(CHAT, "default", now=now,
                            transcript_has=lambda k, t: krn.notice_in_transcript(db, CHAT, k, t))
        assert len(again) == (0 if written else 1)
        if not written:
            assert len(again[0]["children"]) == 5
    else:
        got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                       ledger=ledger, now=now)
        assert len(got) == (0 if written else 1)
        assert len(db.get_messages(CHAT)) == 1
    if written or door == "sweep":
        assert {d for _, d in _rows(ledger)} == {1}


@pytest.mark.parametrize("door", ["collect", "sweep"])
def test_refusal_releases_every_group_member(arrival, monkeypatch, door):
    from tui_gateway import server

    replica, ledger, now, insert, db, opener = arrival
    insert(5, "done", 8 * 3600)
    if door == "collect":
        claim = krn.collect(CHAT, "default", now=now)[0]
        text = object()
        session = {"_kanban_claims": [{"remote": claim, "texts": [text]}], "history_lock": threading.RLock()}
        server._kb_rewind_refused(session, [text], not_owned=True)
    else:
        class Waker:
            grace_s = 0

            def deliver(self, tip, text, metadata, task, at, *, persist, started):
                def busy(*a, **kw):
                    raise RuntimeError("refused before transcript")
                with monkeypatch.context() as m:
                    m.setattr(db, "append_kanban_notice", busy)
                    assert not persist()
                return "deferred"

        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                 ledger=ledger, now=now, waker=Waker())
    assert _rows(ledger) == [] and db.get_messages(CHAT) == []
    again = krn.collect(CHAT, "default", now=now)
    assert len(again) == 1 and len(again[0]["children"]) == 5


@pytest.mark.parametrize("door", ["collect", "sweep"])
@pytest.mark.parametrize("age", [600, 2 * 3600, 6 * 3600])
def test_fresh_owner_times_keep_original_text(arrival, door, age):
    replica, ledger, now, insert, db, opener = arrival
    cards = insert(1, "done", age)
    card = cards[0]
    expected = krn._completion_text("hub", card["key"], card["task"], "macbook", "default", CHAT)
    if door == "collect":
        got = krn.collect(CHAT, "default", now=now)
        assert [g["text"] for g in got] == [expected]
        assert "children" not in got[0]
    else:
        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                 ledger=ledger, now=now)
        assert [m["content"] for m in db.get_messages(CHAT)] == [expected]


def test_late_sweep_retains_live_poller_grace(arrival):
    replica, ledger, now, insert, db, opener = arrival
    insert(5, "done", 8 * 3600)

    class Waker:
        grace_s = 30

        def deliver(self, *a, **kw):
            pytest.fail("grace must defer the whole late group")

    assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=ledger, now=now, waker=Waker()) == []
    assert _rows(ledger) == [] and db.get_messages(CHAT) == []
    assert len(krn.collect(CHAT, "default", now=now)) == 1


@pytest.mark.parametrize("door", ["collect", "sweep"])
@pytest.mark.parametrize("month,zone", [(7, "PDT"), (12, "PST")])
def test_late_owner_time_uses_pacific_dst_and_notice_fallback(arrival, door, month, zone):
    replica, ledger, now, insert, db, opener = arrival
    owner = datetime.fromisoformat(f"2026-{month:02}-01T12:00:00+00:00").timestamp()
    card = insert(1, "done", now - owner)[0]
    del card["task"]["completed_at"]
    card["task"]["completion_notice"]["created_at"] = owner
    with sqlite3.connect(replica) as conn:
        conn.execute("UPDATE replica SET data=?, seq=2", (json.dumps(card["task"]),))
    if door == "collect":
        text = krn.collect(CHAT, "default", now=now)[0]["text"]
    else:
        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                 ledger=ledger, now=now)
        text = db.get_messages(CHAT)[0]["content"]
    stamp = datetime.fromtimestamp(owner, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
    assert stamp.endswith(zone) and stamp in text
    assert text.startswith("Late notices")


@pytest.mark.parametrize("guard", ["archived", "moved", "worker", "live"])
def test_late_sweep_keeps_destination_guards(arrival, guard):
    replica, ledger, now, insert, db, opener = arrival
    insert(5, "done", 8 * 3600)
    if guard == "archived":
        db._write_rowcount("UPDATE sessions SET archived=1 WHERE id=?", (CHAT,))
    elif guard == "moved":
        db.end_session(CHAT, "moved_to_gateway:other")
    elif guard == "worker":
        db._write_rowcount("UPDATE sessions SET source='kanban' WHERE id=?", (CHAT,))
    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=ledger, now=now, live_ids={CHAT} if guard == "live" else set())
    assert got == [] and db.get_messages(CHAT) == []
    assert {d for _, d in _rows(ledger)} == ({krn.SETTLED} if guard == "worker" else set())


def test_real_waker_groups_each_chat_with_deferred_callbacks(arrival, monkeypatch):
    from tui_gateway import kanban_unloaded as ku
    from tests.tui_gateway.test_kanban_remote_notices import OTHER

    replica, ledger, now, insert, db, opener = arrival
    cards = insert(5, "done", 8 * 3600)
    db.create_session(OTHER, "desktop")
    with sqlite3.connect(replica) as conn:
        for i, card in enumerate(cards):
            card["task"]["sessions"].append({"gw": "macbook", "profile": "default", "session": OTHER,
                                                "rel": "shepherd"})
            conn.execute("UPDATE replica SET data=?, seq=? WHERE key=?",
                         (json.dumps(card["task"]), 10 + i, card["key"]))
    ku._reset_wake_budget_for_tests()
    jobs, turns = [], []

    def wake(tip, text, metadata):
        turns.append(tip)
        assert text.startswith("Late notices") and len(metadata["kanban_event_keys"]) == 5
        db.append_kanban_notice(tip, text, metadata)
        return "started"

    waker = ku._RemoteWaker(wake, lambda job: (jobs.append(job), "dispatched")[1], 0)
    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=ledger, now=now, waker=waker)
    assert len(got) == 2 and len(jobs) == 2 and turns == []
    assert len(_rows(ledger)) == 10 and {d for _, d in _rows(ledger)} == {0}
    for job in jobs:
        assert job() == "started"
    assert set(turns) == {CHAT, OTHER} and len(turns) == 2
    assert {d for _, d in _rows(ledger)} == {1}
    assert len(db.get_messages(CHAT)) == len(db.get_messages(OTHER)) == 1
    assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=ledger, now=now, waker=waker) == []
    ku._reset_wake_budget_for_tests()


# --- custody under failure, wake representative, six-day acceptance -----------------------------------------

HUMAN = {"action": "Reply A or B. Unblock = A; archive = drop.", "why_human": "owner pick",
         "interaction": "reply", "scope": "deploy window", "evidence": "no window chosen"}


def _put(replica, cards, start=100):
    with sqlite3.connect(replica) as conn:
        for i, card in enumerate(cards, start):
            conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)", (card["key"], json.dumps(card["task"]), i))


def _mixed(now, order, age=8 * 3600):
    done = _card("t_mix_done", CHAT, status="done", at=now - age)
    human = _card("t_mix_human", CHAT, status="blocked", at=now - age - 60)
    human["task"]["human_request"] = HUMAN
    return [human, done] if order == "human_first" else [done, human]


def _two_chats(arrival):
    from tests.tui_gateway.test_kanban_remote_notices import OTHER

    replica, _ledger, _now, insert, db, _opener = arrival
    cards = insert(2, "done", 8 * 3600)
    db.create_session(OTHER, "desktop")
    with sqlite3.connect(replica) as conn:
        for i, card in enumerate(cards):
            card["task"]["sessions"].append({"gw": "macbook", "profile": "default", "session": OTHER,
                                            "rel": "shepherd"})
            conn.execute("UPDATE replica SET data=?, seq=? WHERE key=?", (json.dumps(card["task"]), 10 + i, card["key"]))
    return OTHER


def _delivered_by_session(ledger):
    with sqlite3.connect(ledger) as conn:
        return {(s, d) for s, d in conn.execute("SELECT session, delivered FROM notices")}


def test_render_failure_releases_claims_and_isolates_chats(arrival, monkeypatch):
    from tui_gateway import kanban_remote_late as late

    replica, ledger, now, _insert, db, opener = arrival
    other = _two_chats(arrival)
    real = late.group

    def flaky(items, at):
        if items[0]["session"] == other:
            raise ValueError("render boom")
        return real(items, at)

    monkeypatch.setattr(late, "group", flaky)
    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=ledger, now=now)
    assert [g["session_id"] for g in got] == [CHAT]
    assert _delivered_by_session(ledger) == {(CHAT, 1)}  # OTHER's claims were released, not left in flight
    assert len(db.get_messages(CHAT)) == 1 and db.get_messages(other) == []
    monkeypatch.setattr(late, "group", real)
    krn._reset_for_tests()
    retry = krn.collect(other, "default", now=now)
    assert len(retry) == 1 and len(retry[0]["children"]) == 2


def test_absurd_owner_time_in_one_chat_never_holds_another_chats_group(arrival):
    """``datetime.fromtimestamp(-1e30)`` raises OverflowError while rendering one chat's late group; that chat's
    claims are released and the other chat's group is still delivered in the same flush."""
    from tests.tui_gateway.test_kanban_remote_notices import OTHER

    replica, ledger, now, insert, db, opener = arrival
    insert(2, "done", 8 * 3600)
    db.create_session(OTHER, "desktop")
    _put(replica, [_card("t_late_absurd", OTHER, status="done", at=-1e30)])

    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=ledger, now=now)

    assert [g["session_id"] for g in got] == [CHAT]
    assert _delivered_by_session(ledger) == {(CHAT, 1)}  # OTHER's claim released, not left in flight
    assert len(db.get_messages(CHAT)) == 1 and db.get_messages(OTHER) == []


def test_wake_failure_never_unclaims_a_persisted_chat(arrival):
    replica, ledger, now, _insert, db, opener = arrival
    other = _two_chats(arrival)

    class Waker:
        grace_s = 0

        def deliver(self, tip, text, metadata, task, at, *, persist, started):
            if tip == other:
                raise RuntimeError("waker boom")
            assert persist()
            return "persisted"

    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=ledger, now=now, waker=Waker())
    assert [g["session_id"] for g in got] == [CHAT]
    assert _delivered_by_session(ledger) == {(CHAT, 1)}
    assert len(db.get_messages(CHAT)) == 1


def test_error_midscan_still_flushes_pending_batches(arrival, monkeypatch):
    replica, ledger, now, insert, db, opener = arrival
    insert(3, "done", 8 * 3600)
    calls = []
    real = krn._deliver_link

    def boom(*a, **kw):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("scan boom")
        return real(*a, **kw)

    monkeypatch.setattr(krn, "_deliver_link", boom)
    with contextlib.suppress(RuntimeError):
        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)
    rows = _rows(ledger)
    assert len(rows) in (0, 2) and len({d for _, d in rows}) <= 1  # delivered together, or not claimed at all
    assert len(db.get_messages(CHAT)) == (1 if rows else 0)


def test_collect_grouping_failure_retains_claims_for_delivery(arrival, monkeypatch):
    from tui_gateway import kanban_remote_late as late

    _replica, ledger, now, insert, _db, _opener = arrival
    insert(5, "done", 8 * 3600)
    monkeypatch.setattr(late, "group", lambda *a: (_ for _ in ()).throw(KeyError("owner_at")))
    result = krn.collect(CHAT, "default", now=now)
    assert len(result) == 5 and result.incomplete
    assert len(_rows(ledger)) == 5
    for claim in result:
        assert krn.ack(claim, ledger=ledger)
    again = krn.collect(CHAT, "default", now=now)
    assert again == [] and again.quiet


@pytest.mark.parametrize("order", ["human_first", "done_first"])
def test_mixed_group_wakes_once_in_either_order(arrival, order):
    from tui_gateway import kanban_unloaded as ku

    replica, ledger, now, _insert, db, opener = arrival
    _put(replica, _mixed(now, order))
    ku._reset_wake_budget_for_tests()
    jobs, turns = [], []

    def wake(tip, text, metadata):
        turns.append((tip, text, metadata))
        db.append_kanban_notice(tip, text, metadata)
        return "started"

    waker = ku._RemoteWaker(wake, lambda job: (jobs.append(job), "dispatched")[1], 0)
    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=ledger, now=now, waker=waker)
    assert [g["wake"] for g in got] == ["dispatched"] and len(jobs) == 1  # one batch, not skipped:human
    assert jobs[0]() == "started"
    (_tip, text, metadata), = turns
    assert len(metadata["kanban_event_keys"]) == 2 and metadata["blocked_on_human"] is False
    assert "t_mix_done" in text and "t_mix_human" in text
    assert {d for _, d in _rows(ledger)} == {1} and len(db.get_messages(CHAT)) == 1
    ku._reset_wake_budget_for_tests()


@pytest.mark.parametrize("door", ["sweep", "collect"])
def test_human_only_group_stays_passive(arrival, door):
    from tui_gateway import kanban_unloaded as ku

    replica, ledger, now, _insert, db, opener = arrival
    cards = [_card(f"t_h{i}", CHAT, status="blocked", at=now - 8 * 3600 - i * 60) for i in range(3)]
    for card in cards:
        card["task"]["human_request"] = HUMAN
    _put(replica, cards)
    ku._reset_wake_budget_for_tests()
    jobs = []
    waker = ku._RemoteWaker(lambda *a: pytest.fail("human-only groups must not wake"),
                            lambda job: (jobs.append(job), "dispatched")[1], 0)
    if door == "collect":
        group = krn.collect(CHAT, "default", now=now)[0]
        assert group["blocked_on_human"] is True and len(group["children"]) == 3
        return
    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                   ledger=ledger, now=now, waker=waker)
    assert [g["wake"] for g in got] == ["skipped:human"] and jobs == []
    assert {d for _, d in _rows(ledger)} == {1} and len(db.get_messages(CHAT)) == 1
    ku._reset_wake_budget_for_tests()


@pytest.mark.parametrize("door", ["collect", "sweep"])
def test_six_day_old_owner_time_is_explicit(arrival, door):
    replica, ledger, now, insert, db, opener = arrival
    cards = insert(2, "done", 6 * 86400)
    if door == "collect":
        text = krn.collect(CHAT, "default", now=now)[0]["text"]
    else:
        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)
        text = db.get_messages(CHAT)[0]["content"]
    zone = ZoneInfo("America/Los_Angeles")
    assert text.startswith("Late notices")
    for card, age in zip(cards, ("144h 0m", "144h 1m")):
        stamp = datetime.fromtimestamp(krn._fresh_at(card["task"]), zone).strftime("%Y-%m-%d %H:%M %Z")
        assert f"- {stamp} ({age} ago; not delivered then)" in text


@pytest.mark.parametrize("door", ["collect", "sweep"])
def test_late_withdrawal_joins_the_group_under_its_own_ledger_state(arrival, door):
    """A withdrawal receipt's ledger state is ``request_withdrawn@<event>``, not ``archived@0``: the batch claim
    must carry that identity or its ack misses the row and the receipt is re-offered."""
    replica, ledger, now, insert, db, opener = arrival
    insert(2, "done", 6 * 86400)
    card = _card("t_late_withdrawn", CHAT, status="done", at=now - 6 * 86400)
    task = {**card["task"], "status": "archived",
            "withdrawal_notice": {"kind": "request_withdrawn", "event_id": 42, "created_at": now - 6 * 86400}}
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)", (card["key"], json.dumps(task), 99))
    if door == "collect":
        group = krn.collect(CHAT, "default", now=now)[0]
        db.append_kanban_notice(CHAT, group["text"], {"notice_id": group["event_keys"][0],
                                                      "kanban_event_keys": group["event_keys"]})
        assert krn.ack(group)
    else:
        krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener, ledger=ledger, now=now)
    with sqlite3.connect(ledger) as conn:
        states = dict(conn.execute("SELECT ref, state || ':' || delivered FROM notices").fetchall())
    assert states[f"hub/{card['key']}"] == "request_withdrawn@42:1"
    assert len(states) == 3 and all(value.endswith(":1") for value in states.values())
    assert len(db.get_messages(CHAT)) == 1 and "t_late_withdrawn" in db.get_messages(CHAT)[0]["content"]
    krn._reset_for_tests()
    assert krn.collect(CHAT, "default", now=now) == []
    assert krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                    ledger=ledger, now=now) == []
