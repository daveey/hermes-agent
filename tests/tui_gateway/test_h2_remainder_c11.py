"""H2 remainder (C11): ``card_change_pass`` skips an idle board without connecting to it, refreshes a chat once per
pass, and the other sub-pass keys are not moved by writes none of them reads. [daveey]"""
from __future__ import annotations

import contextlib
import json
import time

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_session_activity as ksa
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku
from tui_gateway import kanban_unloaded_gates as gates
from tui_gateway import notice_fanout as nf
from tui_gateway import notice_guarantee as ng

CHAT = "20261115_120000_c11chat"


@pytest.fixture
def db(tmp_path, monkeypatch):
    so._reset_hooks_for_tests()
    nf._reset_for_tests()
    ksa._reset_cache_for_tests()
    gates.reset_for_tests()
    monkeypatch.setattr(ku, "_board_slugs", lambda _kb: ["default"])
    handle = SessionDB(tmp_path / "state.db")
    handle.create_session(CHAT, "desktop")
    handle.append_message(CHAT, "user", "start the cards")
    yield handle
    handle.close()
    gates.reset_for_tests()
    nf._reset_for_tests()
    so._reset_hooks_for_tests()


def _opener(db, opened=None):
    @contextlib.contextmanager
    def opener(chat_id):
        if opened is not None:
            opened.append(chat_id)
        yield db, db.get_compression_tip(chat_id) or chat_id
    return opener


def _card(title="card"):
    with contextlib.closing(kbc.connect()) as conn:
        return kb.create_task(conn, title=title, assignee="worker", session_id=CHAT)


def _refreshes(monkeypatch):
    seen = []
    real = so.refresh_outcomes_quietly
    monkeypatch.setattr(so, "refresh_outcomes_quietly", lambda sids, trigger, **k: seen.append(list(sids)) or real(sids, trigger, **k))
    return seen


# ------------------------------------------------------------------------------ card_change_pass
def test_unchanged_board_is_skipped_without_connecting(db, monkeypatch):
    _card()
    assert nf.card_change_pass(session_db=_opener(db)) == []  # seeds the cursor
    real = kbc.connect
    boards = []
    monkeypatch.setattr(kbc, "connect", lambda *a, **k: boards.append(k.get("board")) or real(*a, **k))
    assert nf.card_change_pass(session_db=_opener(db)) == []
    assert nf.card_change_pass(session_db=_opener(db)) == []
    assert boards == []  # no per-pass board connect when the cursor equals the board's newest event


def test_bookkeeping_events_keep_the_board_idle_and_advance_the_cursor(db, monkeypatch):
    """A comment/heartbeat moves the newest event id but is no STATUS kind: ``_changed_tasks`` would return no card,
    so the pass must not connect to the board (t_01a1dfa94f2251cd: the hub's default board never went idle)."""
    tid = _card()
    nf.card_change_pass(session_db=_opener(db))
    ident = str(kb.kanban_db_path("default").resolve())
    with contextlib.closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, "someone", "a note")
        top = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
    from tui_gateway import notice_fanout_state as state
    assert state.get_cursor(ident) < top
    real, boards = kbc.connect, []
    monkeypatch.setattr(kbc, "connect", lambda *a, **k: boards.append(k.get("board")) or real(*a, **k))
    assert nf.card_change_pass(session_db=_opener(db)) == []
    assert boards == [] and state.get_cursor(ident) == top


def test_a_status_event_behind_bookkeeping_events_still_runs_the_pass(db, monkeypatch):
    tid = _card()
    nf.card_change_pass(session_db=_opener(db))
    with contextlib.closing(kbc.connect()) as conn:
        kb.add_comment(conn, tid, "someone", "a note")
        assert kb.complete_task(conn, tid, summary="done")
        kb.add_comment(conn, tid, "someone", "after")
    ksa._reset_cache_for_tests()
    changed = nf.card_change_pass(session_db=_opener(db))
    assert [(c["task_id"], c["status"]) for c in changed] == [(tid, "done")]


def test_outcome_change_is_picked_up_within_one_pass(db, monkeypatch):
    tid = _card()
    nf.card_change_pass(session_db=_opener(db))
    nf.card_change_pass(session_db=_opener(db))  # idle pass: skipped
    refreshed = _refreshes(monkeypatch)
    with contextlib.closing(kbc.connect()) as conn:
        assert kb.complete_task(conn, tid, summary="done")
    ksa._reset_cache_for_tests()
    changed = nf.card_change_pass(session_db=_opener(db))
    assert [(c["task_id"], c["status"]) for c in changed] == [(tid, "done")]
    assert refreshed == [[CHAT]]
    assert nf.card_change_pass(session_db=_opener(db)) == []  # and settles again


def test_a_chat_linked_to_many_changed_cards_is_opened_and_refreshed_once(db, monkeypatch):
    tids = [_card(f"card {i}") for i in range(4)]
    nf.card_change_pass(session_db=_opener(db))
    with contextlib.closing(kbc.connect()) as conn:
        for tid in tids:
            kb.complete_task(conn, tid, summary="done")
    ksa._reset_cache_for_tests()
    refreshed, opened = _refreshes(monkeypatch), []
    changed = nf.card_change_pass(session_db=_opener(db, opened))
    assert sorted(c["task_id"] for c in changed) == sorted(tids)  # every card is still reported
    assert refreshed == [[CHAT]] and opened == [CHAT]


def test_unreadable_board_is_not_idle(db, monkeypatch):
    _card()
    nf.card_change_pass(session_db=_opener(db))
    from hermes_cli import kanban_reader_retention as retention

    @contextlib.contextmanager
    def broken(path):
        raise OSError("unreadable")
        yield
    monkeypatch.setattr(retention, "reader", broken)
    assert nf._board_idle(str(kb.kanban_db_path("default").resolve())) is False


# ------------------------------------------------------------------------------- sub-pass keys
def _subscribe(chat="chat", done=True):
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="notice card", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=chat)
        if done:
            kb.complete_task(conn, tid, summary="ready")
    return tid


def test_notice_for_a_live_chat_is_not_pending_until_the_chat_unloads(db, monkeypatch):
    """A chat unloading with a pending notice: the sweep skips live chats, so the notice waits on the live set."""
    monkeypatch.setattr(gates, "_profile_paths", lambda every_store=True: [db.db_path])
    _subscribe(CHAT)
    assert gates.probe("notices", live={CHAT}).pending is False
    assert gates.probe("notices", live=set()).pending is True
    ran = []
    for live in ({CHAT}, {CHAT}, set(), set()):
        gates.run("notices", lambda: ran.append(1) or [], live=live)
    assert len(ran) == 3  # first live pass, the unload, and the unloaded notice stays hot (never stamped)


def test_inflight_claim_on_a_live_chat_stays_pending(db):
    tid = _subscribe(CHAT)
    with contextlib.closing(kbc.connect()) as conn:
        conn.execute("UPDATE kanban_notify_subs SET inflight_from=1, inflight_pid=1, inflight_start=1, inflight_at=1 "
                     "WHERE task_id=?", (tid,))
        conn.commit()
    assert gates.probe("notices", live={CHAT}).pending is True


def test_board_writes_do_not_move_the_notices_key(db):
    _subscribe("chat", done=False)
    first = gates.probe("notices", live=set())
    assert not first.pending
    _subscribe("other", done=False)  # bookkeeping on the board: not an input
    assert gates.probe("notices", live=set()).key == first.key


def _guarantee_store(db, closeout, ts, last_active=None):
    msg = db.append_kanban_notice(CHAT, "done", {"notice_id": "n1"})
    meta = {} if closeout is None else {"closeout": closeout}
    db._write_rowcount("UPDATE messages SET timestamp=?, display_metadata=? WHERE id=?", (ts, json.dumps(meta), msg))
    return msg


def test_old_notice_without_closeout_beyond_the_lookback_is_not_pending(db, monkeypatch):
    old = time.time() - ng.LOOKBACK_S - 3600
    _guarantee_store(db, None, old)
    monkeypatch.setattr(gates, "_profile_paths", lambda every_store=True: [db.db_path])
    monkeypatch.setattr("hermes_state_common._sql_session_last_active", lambda alias: str(old))
    probed = gates.probe("guarantee_pass")
    assert not probed.pending  # `candidates()` never offers it; one such row kept the hub pass hot for days


def test_recent_notice_without_closeout_is_pending(db, monkeypatch):
    _guarantee_store(db, None, time.time() - 1000)
    monkeypatch.setattr(gates, "_profile_paths", lambda every_store=True: [db.db_path])
    assert gates.probe("guarantee_pass").pending


@pytest.mark.parametrize("busy", [False, True])
def test_deferral_retried_after_its_due_time_with_no_other_change(db, monkeypatch, busy):
    """Busy-chat deferral / wake-slot backoff: nothing is written between passes, yet the retry must still run."""
    ts, retry = 1000.0, 5000.0
    closeout = {"state": "deferred", "retry_at": retry, **({"busy_attempts": 1} if busy else {})}
    _guarantee_store(db, closeout, ts)
    monkeypatch.setattr(gates, "_profile_paths", lambda every_store=True: [db.db_path])
    clock, ran = [4000.0], []
    monkeypatch.setattr(gates.time, "time", lambda: clock[0])

    def run():
        gates.run("guarantee_pass", lambda: ran.append(clock[0]) or [], live=set())

    run()
    run()
    clock[0] = retry - 1
    run()
    clock[0] = retry + 1
    run()
    # a busy-chat deferral is never skipped; a timed backoff is skipped only until it expires
    assert ran == ([4000.0, 4000.0, retry - 1, retry + 1] if busy else [4000.0, retry + 1])


def test_unrelated_state_commits_do_not_move_guarantee_delegation_or_killed_keys(db, monkeypatch):
    import sqlite3
    _guarantee_store(db, {"state": "refused"}, time.time() - 1000)
    monkeypatch.setattr(gates, "_profile_paths", lambda every_store=True: [db.db_path])
    kinds = ("guarantee_pass", "delegation", "killed_notices")
    before = {k: gates.probe(k) for k in kinds}
    assert not any(p.pending for p in before.values())
    other = sqlite3.connect(db.db_path)
    try:
        other.execute("INSERT INTO sessions(id, source, started_at) VALUES ('unrelated', 'desktop', 1.0)")
        other.commit()
    finally:
        other.close()
    for kind in kinds:
        after = gates.probe(kind)
        assert not after.pending and after.key == before[kind].key, kind


def test_replay_benchmark_idle_passes_do_not_connect_and_cost_less(db, monkeypatch):
    """Replay of the production cadence (one pass per 30 s) over a quiet board: the cursor skip must open no board
    connection, and a 200-pass replay must cost a small fraction of the connect-per-pass baseline."""
    for i in range(20):
        _card(f"card {i}")
    nf.card_change_pass(session_db=_opener(db))
    real = kbc.connect
    connects = []
    monkeypatch.setattr(kbc, "connect", lambda *a, **k: connects.append(1) or real(*a, **k))
    t0 = time.process_time()
    for _ in range(200):
        nf.card_change_pass(session_db=_opener(db))
    skipped = time.process_time() - t0
    assert connects == []
    monkeypatch.setattr(nf, "_board_idle", lambda ident: False)  # the pre-change behaviour: connect every pass
    t0 = time.process_time()
    for _ in range(200):
        nf.card_change_pass(session_db=_opener(db))
    baseline = time.process_time() - t0
    assert len(connects) == 200
    print(f"replay 200 idle passes: skip={skipped:.3f}s connect-per-pass={baseline:.3f}s ratio={skipped / baseline:.3f}")
    assert skipped < baseline * 0.5
