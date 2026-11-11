"""A competing writer taking one member must not consume the rest of a late batch."""
import pytest

from hermes_state import SessionDB
from hermes_state_errors import KanbanNoticeOverlapError

from tests.tui_gateway.test_kanban_remote_late import arrival, _rows
from tests.tui_gateway.test_kanban_remote_notices import CHAT
from tui_gateway import kanban_remote_notices as krn


def _seed_member(db, group):
    child = group["children"][0]
    db.append_kanban_notice(CHAT, child["text"], {
        "notice_id": child["event_key"], "kanban_event_keys": [child["event_key"]]})
    return child


def _assert_once(db, keys):
    rows = db.get_messages(CHAT)
    stored = [k for row in rows for k in row["display_metadata"]["kanban_event_keys"]]
    assert sorted(stored) == sorted(keys)
    assert len(rows) == 2


@pytest.mark.parametrize("same_id", [False, True])
def test_partial_overlap_is_not_a_successful_record(arrival, same_id):
    _, _, now, insert, db, _ = arrival
    insert(3, "done", 8 * 3600)
    group = krn.collect(CHAT, "default", now=now)[0]
    child = _seed_member(db, group)
    with pytest.raises(ValueError, match="partial.*overlap"):
        db.record_kanban_notice(CHAT, group["text"], {
            "notice_id": child["event_key"] if same_id else "overlapping-batch",
            "kanban_event_keys": group["event_keys"]})
    assert db.kanban_notice_keys_delivered(CHAT, group["event_keys"]) == {child["event_key"]}


def test_poller_retries_partial_overlap_without_losing_or_repeating_members(arrival, monkeypatch):
    from tui_gateway import server

    _, ledger, now, insert, db, _ = arrival
    insert(3, "done", 8 * 3600)
    collect = krn.collect
    monkeypatch.setattr(krn, "collect", lambda *a, **k: collect(*a, **{**k, "now": now}))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    claims = []
    texts = server._collect_remote_kanban_notifications(CHAT, claims)
    group = claims[0]["remote"]
    # Another consumer commits AFTER the poller has claimed/rendered the group.
    child = _seed_member(db, group)
    session = {"session_key": CHAT, "history": [], "_kanban_claims": claims}
    verdict, fresh, _ = server._kb_persist_batch(session, texts, False)
    assert verdict == "written"
    assert child["text"] not in str(fresh[0])
    server._kb_ack_durable(session, {id(t) for t in texts})
    assert {d for _, d in _rows(ledger)} == {1}
    _assert_once(db, group["event_keys"])


@pytest.mark.parametrize("wake", [False, True])
def test_sweep_partial_overlap_keeps_all_members_in_custody(arrival, wake):
    replica, ledger, now, insert, db, opener = arrival
    insert(3, "done", 8 * 3600)
    original = db.append_kanban_notice
    injected, keys = [], []

    def competing_write(tip, text, metadata):
        if len(metadata.get("kanban_event_keys", [])) > 1 and not injected:
            keys.extend(metadata["kanban_event_keys"])
            key = metadata["kanban_event_keys"][0]
            injected.append(key)
            original(tip, "Competing delivery", {"notice_id": key, "kanban_event_keys": [key]})
        return original(tip, text, metadata)

    db.append_kanban_notice = competing_write

    class Waker:
        grace_s = 0

        def deliver(self, tip, text, metadata, task, at, *, persist, started):
            return "persisted" if persist() else "deferred"

    krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                             ledger=ledger, now=now, waker=Waker() if wake else None)
    # A lost race can defer, but it must not mark absent members delivered.
    assert len(keys) == 3
    durable = db.kanban_notice_keys_delivered(CHAT, keys)
    assert all(not delivered or key in durable for key, delivered in _rows(ledger))
    krn._reset_for_tests()
    krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                             ledger=ledger, now=now)
    assert {d for _, d in _rows(ledger)} == {1}
    _assert_once(db, keys)


def test_record_rechecks_partial_overlap_inside_writer_transaction(arrival, monkeypatch):
    _, _, now, insert, db, _ = arrival
    insert(3, "done", 8 * 3600)
    group = krn.collect(CHAT, "default", now=now)[0]
    execute = db._execute_write
    with SessionDB(db.db_path) as competitor:
        def race(callback, **kwargs):
            _seed_member(competitor, group)
            return execute(callback, **kwargs)
        monkeypatch.setattr(db, "_execute_write", race)
        with pytest.raises(KanbanNoticeOverlapError, match="partial.*overlap"):
            db.record_kanban_notice(CHAT, group["text"], {
                "notice_id": "transaction-race", "kanban_event_keys": group["event_keys"]})
    assert len(db.get_messages(CHAT)) == 1


def test_poller_retries_a_race_after_its_prefilter(arrival, monkeypatch):
    from tui_gateway import server

    _, ledger, now, insert, db, _ = arrival
    insert(3, "done", 8 * 3600)
    collect = krn.collect
    monkeypatch.setattr(krn, "collect", lambda *a, **k: collect(*a, **{**k, "now": now}))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    claims = []
    texts = server._collect_remote_kanban_notifications(CHAT, claims)
    group = claims[0]["remote"]
    session = {"session_key": CHAT, "history": [], "_kanban_claims": claims}
    execute, raced = db._execute_write, []
    with SessionDB(db.db_path) as competitor:
        def race(callback, **kwargs):
            if not raced:
                raced.append(_seed_member(competitor, group))
            return execute(callback, **kwargs)
        monkeypatch.setattr(db, "_execute_write", race)
        verdict, _, _ = server._kb_persist_batch(session, texts, False)
        assert verdict == "busy" and "_submit_user_row" not in session
        assert {d for _, d in _rows(ledger)} == {0}
        verdict, fresh, _ = server._kb_persist_batch(session, texts, False)
    assert verdict == "written"
    assert raced[0]["text"] not in str(fresh[0])
    server._kb_ack_durable(session, {id(t) for t in texts})
    assert {d for _, d in _rows(ledger)} == {1}
    _assert_once(db, group["event_keys"])


def test_record_full_coverage_can_span_rows_without_a_write_lock(arrival, monkeypatch):
    _, _, now, insert, db, _ = arrival
    insert(2, "done", 8 * 3600)
    group = krn.collect(CHAT, "default", now=now)[0]
    for child in group["children"]:
        db.append_kanban_notice(CHAT, child["text"], {
            "notice_id": child["event_key"], "kanban_event_keys": [child["event_key"]]})
    def no_write(*args, **kwargs):
        pytest.fail("fully covered batch must not take the writer lock")
    monkeypatch.setattr(db, "_execute_write", no_write)
    _, inserted = db.record_kanban_notice(CHAT, group["text"], {
        "notice_id": "different-batch", "kanban_event_keys": group["event_keys"]})
    assert not inserted
    _assert_once(db, group["event_keys"])


def test_poller_fully_delivered_late_group_needs_no_second_row(arrival, monkeypatch):
    from tui_gateway import server

    _, ledger, now, insert, db, _ = arrival
    insert(3, "done", 8 * 3600)
    collect = krn.collect
    monkeypatch.setattr(krn, "collect", lambda *a, **k: collect(*a, **{**k, "now": now}))
    monkeypatch.setattr(server, "_get_db", lambda: db)
    claims = []
    texts = server._collect_remote_kanban_notifications(CHAT, claims)
    group = claims[0]["remote"]
    db.append_kanban_notice(CHAT, group["text"], {
        "notice_id": "competing-group", "kanban_event_keys": group["event_keys"]})
    session = {"session_key": CHAT, "history": [], "_kanban_claims": claims}
    verdict, fresh, _ = server._kb_persist_batch(session, texts, False)
    assert verdict == "delivered" and fresh == []
    server._kb_ack_durable(session, {id(t) for t in texts})
    assert len(db.get_messages(CHAT)) == 1 and {d for _, d in _rows(ledger)} == {1}


def test_sweep_full_coverage_during_scan_does_not_wake(arrival, monkeypatch):
    from tui_gateway import kanban_remote_late as late

    replica, ledger, now, insert, db, opener = arrival
    insert(3, "done", 8 * 3600)
    flush = late.SweepBatches._flush_one

    def race(self, profile, tip, entries):
        for item, _, metadata, _ in entries:
            db.append_kanban_notice(tip, item["text"], metadata)
        return flush(self, profile, tip, entries)

    monkeypatch.setattr(late.SweepBatches, "_flush_one", race)

    class Waker:
        grace_s = 0
        def deliver(self, *args, **kwargs):
            pytest.fail("fully delivered group must not wake")

    got = krn.sweep_remote_notices(replica_path=replica, gateway="macbook", session_db=opener,
                                 ledger=ledger, now=now, waker=Waker())
    assert [entry["wake"] for entry in got] == ["delivered"]
    assert len(db.get_messages(CHAT)) == 3 and {d for _, d in _rows(ledger)} == {1}


@pytest.mark.parametrize("count", [19, 20, 21])
def test_group_cap_boundary_keeps_all_keys(arrival, count):
    _, _, now, insert, _db, _ = arrival
    insert(count, "done", 8 * 3600)
    group = krn.collect(CHAT, "default", now=now)[0]
    assert len(group["event_keys"]) == count
    assert group["text"].count("not delivered then") == min(20, count)
    assert (f"+{count - 20} more" in group["text"]) == (count > 20)
