"""Sidebar poll cost: the per-board read memo and the one-snapshot annotation scope give the same rows with fewer
reads. [daveey]"""

import sqlite3
import time

import pytest

from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli.test_kanban_wait_info import H, _board


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    ksa._reset_cache_for_tests()
    yield tmp_path
    ksa._reset_cache_for_tests()


def _boards(root, now):
    _board(root / "kanban.db", [
        {"id": "t_probe", "status": "scheduled", "session": "s_a",
         "events": [("scheduled", {"reason": "grant", "wake_check": "python3 probe.py"}, int(now - 3 * H))]},
        {"id": "t_run", "status": "running", "session": "s_a"},
        {"id": "t_block", "status": "blocked", "kind": "transient", "session": "s_b"},
    ])
    _board(root / "kanban" / "boards" / "other" / "kanban.db", [
        {"id": "t_other", "status": "todo", "session": "s_b"},
        {"id": "t_done", "status": "done", "session": "s_b"},
    ])


def _snapshot():
    """Everything the counts publish, read fresh (cache dropped, board memo kept)."""
    with ksa._cache_lock:
        ksa._cache = None
    active, blocked, unseen, stale, waiting = ksa._all_counts(include_waiting=True)
    return (active, blocked, unseen, stale, waiting, ksa.parked_task_counts(), ksa.stalled_task_counts())


def _count_board_reads(monkeypatch):
    reads = []
    real = ksa._read_board

    def traced(path, **kw):
        reads.append(path.parent.name)
        return real(path, **kw)

    monkeypatch.setattr(ksa, "_read_board", traced)
    return reads


def test_unchanged_boards_are_not_read_again_and_give_the_same_counts(kanban_root, monkeypatch):
    _boards(kanban_root, time.time())
    reads = _count_board_reads(monkeypatch)
    first = _snapshot()
    assert sorted(reads) == ["other", kanban_root.name]
    assert first[0] == {"s_a": 2, "s_b": 2} and first[6] == {"s_a": 1}
    reads.clear()
    again = _snapshot()
    assert reads == [] and again == first
    # The memo's answer is the fresh answer: drop it and read every board again.
    ksa._board_memo.clear()
    assert _snapshot() == first and len(reads) == 2


def test_a_write_rereads_only_the_board_that_moved(kanban_root, monkeypatch):
    _boards(kanban_root, time.time())
    reads = _count_board_reads(monkeypatch)
    _snapshot()
    reads.clear()
    with sqlite3.connect(kanban_root / "kanban" / "boards" / "other" / "kanban.db") as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id='t_other'")
    counts = _snapshot()
    assert reads == ["other"]
    assert counts[0] == {"s_a": 2, "s_b": 1}
    ksa._board_memo.clear()
    assert _snapshot() == counts


def test_a_memoized_board_still_turns_stalled_when_the_clock_passes_the_cutoff(kanban_root, monkeypatch):
    now = time.time()
    _board(kanban_root / "kanban.db", [
        {"id": "t_probe", "status": "scheduled", "session": "s_a",
         "events": [("scheduled", {"reason": "grant", "wake_check": "python3 probe.py"}, int(now - H))]},
    ])
    reads = _count_board_reads(monkeypatch)
    assert _snapshot()[6] == {}
    monkeypatch.setattr(ksa.time, "time", lambda: now + 2 * H)
    assert _snapshot()[6] == {"s_a": 1}
    assert len(reads) == 1  # the rows were read once; only the verdict moved


def test_a_failed_display_read_is_not_memoized(kanban_root, monkeypatch):
    _boards(kanban_root, time.time())
    reads = _count_board_reads(monkeypatch)
    real = ksa._unseen_on
    fail = [True]

    def flaky(conn):
        if fail[0]:
            raise sqlite3.OperationalError("database is locked")
        return real(conn)

    monkeypatch.setattr(ksa, "_unseen_on", flaky)
    _snapshot()
    fail[0] = False
    reads.clear()
    _snapshot()
    assert sorted(reads) == ["other", kanban_root.name]  # retried, not served from a failed read


def test_annotations_in_one_scope_share_one_read(kanban_root, monkeypatch):
    _boards(kanban_root, time.time())
    calls = {"counts": 0, "extra": 0, "attention": 0}
    real_counts, real_attention = ksa._all_counts, ksa.attention_task_counts

    def counts(**kw):
        calls["counts"] += 1
        with ksa._cache_lock:
            ksa._cache = None  # as if a board moved between the slices: every unscoped call recomputes
        return real_counts(**kw)

    def extra():
        calls["extra"] += 1
        return {"s_c": {"t_pending"}}

    def attention():
        calls["attention"] += 1
        return real_attention()

    monkeypatch.setattr(ksa, "_all_counts", counts)
    monkeypatch.setattr(ksa, "_extra_waiting", extra)
    monkeypatch.setattr(ksa, "attention_task_counts", attention)

    def slices():
        return [ksa.annotate_kanban_activity([{"id": sid}]) for sid in ("s_a", "s_b", "s_c")]

    def strip(rows):
        return [{k: v for k, v in r.items() if k != "kanban_observed_at"} for rs in rows for r in rs]

    unscoped = slices()
    assert calls == {"counts": 3, "extra": 3, "attention": 3}
    for k in calls:
        calls[k] = 0
    with ksa.one_annotation_snapshot():
        scoped = slices()
        with ksa.one_annotation_snapshot():  # nested scopes keep the outer snapshot
            ksa.annotate_kanban_activity([{"id": "s_a"}])
    assert calls == {"counts": 1, "extra": 1, "attention": 1}
    assert strip(scoped) == strip(unscoped)
    assert scoped[2][0]["kanban_waiting_on"] == ["t_pending"]
    ksa.annotate_kanban_activity([{"id": "s_a"}])  # outside the scope: reads again
    assert calls["counts"] == 2


def test_a_failed_read_in_a_scope_is_retried_by_the_next_annotation(kanban_root, monkeypatch):
    _boards(kanban_root, time.time())
    real = ksa._all_counts
    fail = [True]

    def counts(**kw):
        if fail[0]:
            fail[0] = False
            raise sqlite3.OperationalError("locked")
        return real(**kw)

    monkeypatch.setattr(ksa, "_all_counts", counts)
    with ksa.one_annotation_snapshot():
        first = ksa.annotate_kanban_activity([{"id": "s_a"}])[0]
        second = ksa.annotate_kanban_activity([{"id": "s_a"}])[0]
    assert first["kanban_unknown"] is True
    assert "kanban_unknown" not in second and second["kanban_active"] == 2


def test_remote_links_and_origin_rows_share_one_projection(kanban_root, monkeypatch):
    released = {"key": "gw/p/b/t_released", "via": "origin", "unfinished": False, "session": "s_a"}
    open_row = {"key": "gw/p/b/t_open", "via": "origin", "unfinished": True, "session": "s_a"}
    projected = []

    def project_remote_links():
        projected.append(True)
        sink = getattr(ksa._origin_rows_sink, "rows", None)
        if sink is not None:
            sink.extend([released, open_row])
        return [open_row]

    monkeypatch.setattr(ksa, "_project_remote_links", project_remote_links)
    monkeypatch.setattr(ksa, "_links_fingerprint", lambda: ())
    cards = ksa._remote_links()
    cards2, rows = ksa.remote_links_and_origin_rows()
    assert cards == cards2 == [open_row]
    assert rows == [released, open_row]
    assert len(projected) == 1


def test_session_tips_resolve_the_same_on_one_shared_connection(tmp_path):
    """The remote projection resolves every receipt alias's compression tip on one state.db connection; each lookup
    answers as a fresh open does, and a failed lookup leaves the connection usable for the next."""
    from hermes_cli.kanban_origin import _ro
    from hermes_cli.kanban_origin_adapter import _resolve_session
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("root", source="cli")
    db.end_session("root", "compression")
    db.create_session("tip", source="cli", parent_session_id="root")
    db.create_session("plain", source="cli")
    db.close()
    path = tmp_path / "state.db"
    conn = _ro(path)
    try:
        for sid in ("root", "plain", "tip", "missing", "root"):
            try:
                alone = _resolve_session(path, sid)
            except ValueError as exc:
                alone = repr(exc)
            try:
                shared = _resolve_session(path, sid, conn)
            except ValueError as exc:
                shared = repr(exc)
            assert shared == alone
            assert not conn.in_transaction
        assert _resolve_session(path, "root", conn) == "tip"
    finally:
        conn.close()


def test_one_scope_projects_the_remote_links_once(kanban_root, monkeypatch):
    """Inside one annotation scope the counts and a plugin's waiting pass share one remote projection, even when a
    store moved between them (board WALs move every few seconds)."""
    projected = []
    fingerprint = [0]

    def project_remote_links():
        projected.append(True)
        return [{"key": f"k{len(projected)}"}]

    monkeypatch.setattr(ksa, "_project_remote_links", project_remote_links)
    monkeypatch.setattr(ksa, "_links_fingerprint", lambda: (fingerprint[0],))
    with ksa.one_annotation_snapshot():
        first = ksa._remote_links()
        fingerprint[0] += 1
        assert ksa._remote_links() == first and ksa.remote_links_and_origin_rows()[0] == first
    assert len(projected) == 1
    assert ksa._remote_links() != first and len(projected) == 2  # outside the scope: the moved store reprojects
