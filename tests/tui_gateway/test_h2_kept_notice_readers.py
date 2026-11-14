"""H2: the notice guarantee and the orphan-reap notice retry read a FOREIGN profile's ``state.db`` through the kept
read-only handle (``hermes_state_kept_reader``): no per-pass writer open or schema reconcile. Only an actual notice
write or outcome refresh borrows the shared writer, through ``notice_writer``. The launch store stays the shared
writable handle. [daveey]"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

import pytest

import hermes_state_kept_reader as kept
import hermes_state_outcome as so
import hermes_state_registry
from hermes_state import SessionDB
from tui_gateway import notice_candidates as nc
from tui_gateway import notice_guarantee as ng
from tui_gateway import reap_notices as rn
from tui_gateway import server
from tui_gateway.kanban_notice_session_store import NoticeSessionStore, notice_writer, reader_of

CHAT = "20261114_090000_h2kept"


@pytest.fixture
def foreign(tmp_path, monkeypatch):
    """A foreign profile's seeded ``state.db`` served as profile ``other``."""
    so._reset_hooks_for_tests()
    ng._reset_refreshed_for_tests()
    home = tmp_path / "other"
    home.mkdir()
    path = home / "state.db"
    seed = SessionDB(path)
    seed.create_session(CHAT, "desktop")
    seed.append_message(CHAT, "user", "run the deploy card")
    seed.append_message(CHAT, "assistant", "Started it; I will report when it lands.")
    seed.close()
    monkeypatch.setattr(server, "_profile_home", lambda profile: home if profile == "other" else None)
    monkeypatch.setattr(server, "_readopt_wake_profiles", lambda: ["other"])
    monkeypatch.setattr(ng, "_this_gateway", lambda: "local-example")
    monkeypatch.setattr(nc, "_cache", nc.OrderedDict())
    yield path
    kept.close_all_under(None)
    hermes_state_registry.close_all()
    so._reset_hooks_for_tests()


@pytest.fixture
def writer_borrows(monkeypatch):
    """Every shared-writer borrow (``hermes_state_registry.acquire``)."""
    calls: list = []
    real = hermes_state_registry.acquire

    def counting(path, *a, **kw):
        calls.append(Path(path))
        return real(path, *a, **kw)

    monkeypatch.setattr(hermes_state_registry, "acquire", counting)
    return calls


def _notice(path, text="Card t_1 blocked on David", age=600.0, **meta):
    db = hermes_state_registry.acquire(path)
    try:
        row = db.append_kanban_notice(CHAT, text, {"notice_id": f"n:{text}:{time.time()}", "display_text": text, **meta})
        db._write_rowcount("UPDATE messages SET timestamp = ? WHERE id = ?", (time.time() - age, row))
    finally:
        hermes_state_registry.release(db)
    return row


def _pass(**kw):
    return ng.guarantee_pass(stores=ng._stores, holders=kw.pop("holders", list), **kw)


def test_foreign_profile_store_is_the_kept_read_only_handle(foreign, writer_borrows):
    with ng._profile_store("other") as store:
        assert isinstance(store, NoticeSessionStore)
        assert reader_of(store).read_only
        with pytest.raises(Exception):
            store.append_kanban_notice(CHAT, "x", {"notice_id": "direct"})  # a direct write fails, never elsewhere
    assert writer_borrows == []


def test_missing_foreign_store_is_skipped_not_created(foreign):
    foreign.unlink()
    for side in foreign.parent.glob("state.db*"):
        side.unlink()
    with ng._profile_store("other") as store:
        assert store is None
    assert not foreign.exists()


def test_candidate_scan_and_held_skip_borrow_no_writer(foreign, writer_borrows, monkeypatch):
    _notice(foreign)
    writer_borrows.clear()
    monkeypatch.setattr(hermes_state_registry, "acquire", lambda *a, **k: pytest.fail("writer borrowed"))
    holders = lambda: [{"pid": 1, "session_id": CHAT, "surface": "tui", "host": "x"}]
    monkeypatch.setattr("tui_gateway.kanban_unloaded._other_holder", lambda h, ids: "pid 1")
    out = _pass(holders=holders)
    assert [r["result"] for r in out] == ["skipped:held"]
    assert kept.stats["opens"] >= 1


def test_candidates_cache_identity_is_stable_across_passes(foreign):
    _notice(foreign)
    keys = []
    for _ in range(3):
        with ng._profile_store("other") as store:
            assert len(ng.candidates(store, time.time())) == 1
        keys.append(set(nc._cache))
    assert len(keys[0]) == 1 and keys[0] == keys[1] == keys[2]  # same kept reader: one entry, never a fresh key
    [(key_db, _conn, _sql)] = keys[0]
    assert not isinstance(key_db, NoticeSessionStore)


def test_refresh_branch_writes_through_the_writer_once_per_row(foreign, writer_borrows):
    _notice(foreign, blocked_on_human=True, gateway="remote-example")
    writer_borrows.clear()
    assert [r["result"] for r in _pass(holders=list)] == ["refreshed:human"]
    assert writer_borrows, "the outcome refresh is an actual write: it borrows the shared writer"
    assert so.get_outcome(CHAT, db=hermes_state_registry.acquire(foreign)) is not None
    writer_borrows.clear()
    assert [r["result"] for r in _pass(holders=list)] == ["skipped:refreshed"]
    assert writer_borrows == []


def test_claim_stamp_supersede_and_give_up_write_through_notice_writer(foreign, writer_borrows):
    row = _notice(foreign)
    with ng._profile_store("other") as store:
        [cand] = ng.candidates(store, time.time())
        writer_borrows.clear()
        assert ng._claim(store, cand, time.time()) is True
        assert writer_borrows
        ng._stamp(store, row, "started")
        # compare-and-swap: the stale raw_meta no longer matches, so neither supersede nor give-up overwrites it
        assert ng._supersede(store, cand, time.time()) == "skipped:claimed"
        assert ng._give_up(store, cand) == "error"
        meta = store._read_one("SELECT display_metadata FROM messages WHERE id = ?", (row,))["display_metadata"]
        assert '"started"' in meta
        assert writer_borrows


def test_supersede_and_give_up_stamp_a_fresh_candidate(foreign):
    first, second = _notice(foreign, "one"), None
    with ng._profile_store("other") as store:
        [cand] = ng.candidates(store, time.time())
        assert ng._supersede(store, cand, time.time()) == "superseded"
        assert '"superseded"' in store._read_one(
            "SELECT display_metadata FROM messages WHERE id = ?", (first,))["display_metadata"]
    second = _notice(foreign, "two")
    with ng._profile_store("other") as store:
        [cand] = ng.candidates(store, time.time())
        assert cand["msg_id"] == second
        assert ng._give_up(store, cand) == "error"
        assert '"error"' in store._read_one(
            "SELECT display_metadata FROM messages WHERE id = ?", (second,))["display_metadata"]


def test_dispatch_claims_and_closeout_stamps_through_the_writer(foreign, writer_borrows):
    row = _notice(foreign)
    jobs = []

    @contextlib.contextmanager
    def opener(chat_id):  # the close-out worker's own (writable) session store
        db = hermes_state_registry.acquire(foreign)
        try:
            yield db, chat_id
        finally:
            hermes_state_registry.release(db)

    out = _pass(session_db=opener, wake=lambda sid, mid: "started", spawn=jobs.append)
    assert [r["result"] for r in out] == ["dispatched"]
    jobs[0]()
    with kept.read_only(foreign) as reader:
        meta = reader._read_one("SELECT display_metadata FROM messages WHERE id = ?", (row,))["display_metadata"]
    assert '"started"' in meta


def test_launch_profile_store_is_still_the_shared_writable_handle(tmp_path, monkeypatch):
    launch = SessionDB(tmp_path / "state.db")
    monkeypatch.setattr(server, "_profile_home", lambda profile: None)
    monkeypatch.setattr(server, "_get_db", lambda: launch)
    try:
        for profile in (None, "default"):
            with ng._profile_store(profile) as db:
                assert db is launch and not isinstance(db, NoticeSessionStore)
    finally:
        launch.close()


def test_plain_handles_are_their_own_writer_and_reader():
    class Fake:
        pass

    fake = Fake()
    with notice_writer(fake) as w:
        assert w is fake
    assert reader_of(fake) is fake


def _killed_child(path):
    db = hermes_state_registry.acquire(path)
    try:
        db.create_session("parent", source="desktop")
        db.append_message("parent", role="user", content="audit the repos")
        db.append_message("parent", role="assistant", content="Delegated.")
        db.create_session("child", source="subagent", parent_session_id="parent")
        db.append_message("child", role="user", content="audit repo A")
        db._conn.execute("UPDATE sessions SET end_reason = 'startup_orphan_reap', ended_at = ? WHERE id = 'child'",
                         (time.time() - 60,))
        db._conn.commit()
    finally:
        hermes_state_registry.release(db)


def test_reap_retry_reads_kept_and_writes_notice_and_refresh_through_the_writer(foreign, writer_borrows):
    _killed_child(foreign)
    rn._reset_for_tests()
    writer_borrows.clear()
    with ng._profile_store("other") as store:
        assert rn.retry_pending_killed_notices(store) == ["parent"]
        assert writer_borrows, "the notice row and the parent's refresh are writes"
        notices = [m for m in store.get_messages("parent") if m.get("display_kind") == "kanban_notice"]
        assert len(notices) == 1
        assert (so.get_outcome("parent", db=store)["reason"]) == "subagent_killed"
        writer_borrows.clear()
        assert rn.retry_pending_killed_notices(store) == []  # delivered: a read-only pass, no writer
    assert writer_borrows == []


def test_reap_retry_all_walks_foreign_stores_without_a_writer_when_nothing_is_owed(foreign, monkeypatch):
    monkeypatch.setattr(hermes_state_registry, "acquire", lambda *a, **k: pytest.fail("writer borrowed"))
    assert rn.retry_pending_killed_notices_all() == []
