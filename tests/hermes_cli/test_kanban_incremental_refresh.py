"""Churn on one board must not re-read the others (t_01a1cee98aaa8db1). [daveey]

On hermes-hub the main board's content revision bumped ~39 times a minute, 18 of them ``heartbeat`` events, and the
annotation and remote-links refreshers re-read every board's slice (counts, the attention pass's done cards, the
links projection) on every bump. Proven by read counters, never timing:

- a heartbeat (``heartbeat_worker``: run/task stamps plus a ``heartbeat`` event) no longer moves a board's content
  key, yet the card-details strip still shows it at once (that read is not keyed);
- the attention pass re-reads only the boards whose key moved;
- the links projection is kept across commits to a board none of its rows read, and rebuilt at once on a commit to
  a board it read, a new board, or the hub replica/ledger;
- a writer of the previous version (unconditional event trigger) leaves readers on a valid, superset key.
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_links_narrow as narrow
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_db_dispatch import heartbeat_worker
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    paths = [home / "kanban.db", home / "kanban" / "boards" / "b" / "kanban.db"]
    paths[1].parent.mkdir(parents=True)
    conns = [kbc.connect(p) for p in paths]
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: list(paths))
    monkeypatch.setattr(ksa, "_replica_paths", list)
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    yield paths, conns
    ksa._reset_cache_for_tests()
    for conn in conns:
        conn.close()


def _running(conn, session: str = "s_chat") -> str:
    tid = kb.create_task(conn, title="work", assignee="worker", session_id=session)
    assert kb.claim_task(conn, tid) is not None
    return tid


def _key(path):
    return ksa._board_content_key(path)


def test_a_heartbeat_moves_no_content_key_and_the_strip_still_shows_it(boards):
    paths, (a, _b) = boards
    tid = _running(a)
    # Card details take the event at MAX(created_at) (whole seconds): date the earlier ones back so the heartbeat is
    # the latest (an event UPDATE bumps, so this precedes ``before``).
    a.execute("UPDATE task_events SET created_at = created_at - 60")
    a.commit()
    before = _key(paths[0])
    assert rev.is_revision(before)
    assert heartbeat_worker(a, tid, note="still going")
    assert a.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                     (tid,)).fetchone()[0] == "heartbeat"
    assert _key(paths[0]) == before
    # The card-details read is not keyed on the revision: the very next listing shows the heartbeat.
    (card,) = ksa.session_tasks(["s_chat"])
    assert card["activity_kind"] == "heartbeat"
    # Every other event kind still bumps, as does any delete of an event row.
    kb._append_event(a, tid, "commented")
    a.commit()
    assert _key(paths[0]) != before
    before = _key(paths[0])
    a.execute("DELETE FROM task_events WHERE kind='heartbeat'")
    a.commit()
    assert _key(paths[0]) != before


def test_heartbeat_churn_does_not_reread_counts(boards, monkeypatch):
    _paths, (a, b) = boards
    tid = _running(a)
    kb.create_task(b, title="other", assignee="worker", session_id="s_other")
    reads: list = []
    real = ksa._read_board
    monkeypatch.setattr(ksa, "_read_board", lambda path, **kw: reads.append(str(path)) or real(path, **kw))
    assert ksa.active_task_counts() == {"s_chat": 1, "s_other": 1}
    assert len(reads) == 2
    for _ in range(5):
        assert heartbeat_worker(a, tid)
        with ksa._cache_lock:
            ksa._cache = None  # even a recompute past the TTL serves both boards from their memo rows
        assert ksa.active_task_counts() == {"s_chat": 1, "s_other": 1}
    assert len(reads) == 2


def test_attention_pass_rereads_only_the_board_that_moved(boards, monkeypatch):
    paths, (a, b) = boards
    for conn, sid in ((a, "s_a"), (b, "s_b")):
        tid = kb.create_task(conn, title="x (split to follow-up)", assignee="w", session_id=sid)
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, result="ok")
    reads: list = []
    real = ksa._read_recent_done
    monkeypatch.setattr(ksa, "_read_recent_done", lambda *args: reads.append(str(args[0])) or real(*args))
    dispose = ksa.register_done_check("t", lambda card: "split to follow-up" in (card.get("title") or ""))
    try:
        assert ksa.attention_task_counts() == {"s_a": 1, "s_b": 1}
        assert sorted(reads) == sorted(map(str, paths))
        reads.clear()
        # Unrelated churn on board A: a commit that moves A's key (not B's).
        other = kb.create_task(a, title="new done (split to follow-up)", assignee="w", session_id="s_a")
        kb.claim_task(a, other)
        kb.complete_task(a, other, result="ok")
        ksa._attention.clear()
        assert ksa.attention_task_counts() == {"s_a": 2, "s_b": 1}  # read-your-writes for the board that moved
        assert reads == [str(paths[0])]  # board B served from its memo row, not re-read
    finally:
        dispose()


@pytest.fixture
def projection(boards, monkeypatch):
    """A projector that declares the core read pattern: local boards only for this origin's rows' own board."""
    paths, conns = boards
    rows = [{"gateway": "gw", "profile": "default", "origin_gateway": "gw", "origin_profile": "default",
             "board": ksa._board_slug(paths[0]), "id": "t1"}]
    calls: list = []

    def project():
        calls.append(True)
        sink = getattr(ksa._origin_rows_sink, "rows", None)
        if sink is not None:
            sink.extend(rows)
        return [dict(r, session="s") for r in rows]

    setattr(project, narrow.DECLARES_BOARD_READS, True)
    monkeypatch.setattr(ksa, "_project_remote_links", project)
    monkeypatch.setattr(ksa, "_origin_scope", lambda: ("gw", "default"))
    ksa._reset_cache_for_tests()
    return paths, conns, calls, rows


def test_links_projection_survives_commits_to_boards_it_never_read(projection):
    _paths, (a, b), calls, _rows = projection
    first = ksa._remote_links()
    assert len(calls) == 1
    for i in range(5):  # unrelated churn on board B: every commit moves B's content key
        kb.create_task(b, title=f"b{i}", assignee="w", session_id="s_b")
        assert ksa._remote_links() == first
    assert len(calls) == 1
    kb.create_task(a, title="a", assignee="w", session_id="s_a")  # the board a row read: rebuilt at once
    ksa._remote_links()
    assert len(calls) == 2


def test_links_projection_rebuilds_for_a_new_board_and_unknown_rows(projection, monkeypatch, tmp_path):
    paths, (_a, b), calls, rows = projection
    ksa._remote_links()
    extra = tmp_path / "extra.db"
    kbc.connect(extra).close()
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [*paths, extra])
    ksa._remote_links()
    assert len(calls) == 2  # a board the entry never knew always differs
    rows[0] = {"unexpected": "shape"}  # rows with no readable identity: exact fingerprint only
    ksa._reset_cache_for_tests()
    ksa._remote_links()
    kb.create_task(b, title="b", assignee="w", session_id="s_b")
    ksa._remote_links()
    assert len(calls) == 4


def test_an_undeclared_projector_keeps_the_exact_fingerprint(boards, monkeypatch):
    _paths, (_a, b) = boards
    calls: list = []
    monkeypatch.setattr(ksa, "_project_remote_links", lambda: calls.append(True) or [])
    ksa._reset_cache_for_tests()
    ksa._remote_links()
    kb.create_task(b, title="b", assignee="w", session_id="s_b")
    ksa._remote_links()
    assert len(calls) == 2


def test_previous_version_writer_trigger_keeps_readers_on_a_valid_superset_key(boards):
    paths, (a, _b) = boards
    tid = _running(a)
    # An older writer's init: the unconditional event-insert trigger.
    a.execute(f"DROP TRIGGER {rev.PREFIX}task_events_insert")
    a.execute(f"CREATE TRIGGER {rev.PREFIX}task_events_insert AFTER INSERT ON task_events BEGIN {rev._BUMP} END")
    a.commit()
    before = _key(paths[0])
    assert rev.is_revision(before)  # still a covered revision, not the stat fallback
    heartbeat_worker(a, tid)
    assert _key(paths[0]) != before  # heartbeats bump again: more re-reads, never a stale answer
    rev.ensure_revision(a, rev.LOCAL_TABLES)  # this version's init repairs it (and bumps)
    before = _key(paths[0])
    heartbeat_worker(a, tid)
    assert _key(paths[0]) == before


def test_recent_done_memo_drops_rows_past_a_later_cutoff(boards):
    from hermes_cli import kanban_session_activity_recent_done as rd

    calls: list = []
    now = time.time()
    rows = [("s", {"id": "old", "completed_at": now - 100}), ("s", {"id": "new", "completed_at": now - 10})]

    def read(path, since):
        calls.append(since)
        return [(s, dict(c)) for s, c in rows if c["completed_at"] >= since]

    path = boards[0][0]
    key = lambda p: ("revision", 1)
    assert [c["id"] for _, c in rd.memoized(path, now - 200, read, key)] == ["old", "new"]
    assert [c["id"] for _, c in rd.memoized(path, now - 50, read, key)] == ["new"]
    assert len(calls) == 1
    rd.memoized(path, now - 300, read, key)  # an earlier cut-off than the memo row's: read
    assert len(calls) == 2
    rd.memoized(path, now - 300, read, lambda p: ("revision", 2))  # a moved key: read
    assert len(calls) == 3


def test_recent_done_memo_key_failure_reads_without_caching(boards):
    """A board whose memo key can't be taken (unopenable, vanished) is still read, and never cached."""
    import sqlite3

    from hermes_cli import kanban_session_activity_recent_done as rd

    calls: list = []

    def read(path, since):
        calls.append(since)
        return [("s", {"id": "x", "completed_at": since + 1})]

    def broken(_path):
        raise sqlite3.OperationalError("unable to open database file")

    path = boards[0][0]
    assert [c["id"] for _, c in rd.memoized(path, 1.0, read, broken)] == ["x"]
    assert [c["id"] for _, c in rd.memoized(path, 1.0, read, broken)] == ["x"]
    assert len(calls) == 2


def test_links_narrow_counts_a_shared_slug_as_read_on_every_board():
    entry = (0.0, ("scope",), (("p1", 1), ("p2", 1), ("p3", 1)), [],
             [{"gateway": "g", "profile": "d", "origin_gateway": "g", "origin_profile": "d", "board": "default"}])
    narrow.reset()

    def project():
        return None

    setattr(project, narrow.DECLARES_BOARD_READS, True)
    slugs = {"p1": "default", "p2": "default", "p3": "other"}
    narrow.remember(entry, project, ["p1", "p2", "p3"], lambda p: slugs[p])
    assert narrow.matches(entry, (("p1", 1), ("p2", 1), ("p3", 2)))  # only the unread board moved
    assert not narrow.matches(entry, (("p1", 1), ("p2", 2), ("p3", 1)))  # the second "default" board moved
    narrow.reset()
