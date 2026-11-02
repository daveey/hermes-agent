"""A Desktop poll whose kanban inputs did not change reuses the previous read instead of refreshing. [daveey]

David-MBP profile (t_01a199a0a5d38255, 60 s, Desktop open, 1-2 agents): the session-list poll (every ~3 s) started a
``kanban-annotation-refresh`` thread on every poll (20/min, 1.72% of a core) because the counts cache expired after
``_CACHE_TTL_S`` (3 s) even when no board content had moved, and the attention cache re-ran every done check after its
30 s TTL. A 3-minute read of the live boards saw a content change in 3 of 60 polls. Now a poll whose board content
revisions (and the attention inputs) are unchanged revalidates the cached read inline: no thread, no recompute, a new
observation date. A board write still shows on the very next poll (read-your-writes); time-derived states (stalled,
remote stale) are bounded by ``_COUNTS_MAX_AGE_S`` like the remote-links projection (``_LINKS_MAX_AGE_S``), and a board
without the content-revision counter keeps the old 3 s bound.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_activity_revision as revision
from hermes_cli import kanban_session_activity as ksa


@pytest.fixture
def clock(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setattr(ksa._annotation_copy, "published", lambda scope, parts: None)
    monkeypatch.setattr(ksa._annotation_copy, "seed", lambda *a, **k: None)
    now = {"mono": 1000.0, "wall": 5000.0}
    monkeypatch.setattr(ksa.time, "monotonic", lambda: now["mono"])
    monkeypatch.setattr(ksa.time, "time", lambda: now["wall"])
    ksa._reset_cache_for_tests()
    yield now
    ksa._reset_cache_for_tests()


def _advance(now, seconds):
    now["mono"] += seconds
    now["wall"] += seconds


def _board(path: Path, rows, *, counted: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, session_id TEXT)")
    if counted:
        revision.ensure_revision(conn, revision.LOCAL_TABLES)
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?)", rows)
    return conn


@pytest.fixture
def spies(monkeypatch):
    calls = {"recompute": 0, "threads": 0}
    recompute, bounded = ksa._recompute_all_counts, ksa._bounded.refresh_bounded

    def counting_recompute(*a, **k):
        calls["recompute"] += 1
        return recompute(*a, **k)

    def counting_bounded(*a, **k):
        calls["threads"] += 1
        return bounded(*a, **k)

    monkeypatch.setattr(ksa, "_recompute_all_counts", counting_recompute)
    monkeypatch.setattr(ksa._bounded, "refresh_bounded", counting_bounded)
    return calls


def _poll(sid="s1"):
    return ksa.annotate_kanban_activity([{"id": sid}])[0]


def test_unchanged_polls_past_the_ttl_reuse_one_read_without_a_refresh_thread(clock, spies, tmp_path):
    conn = _board(tmp_path / "kanban.db", [("t1", "running", "s1")])
    try:
        assert _poll()["kanban_active"] == 1
        dates = []
        for _ in range(4):  # a Desktop poll every 3 s: past _CACHE_TTL_S each time
            _advance(clock, ksa._CACHE_TTL_S + 0.5)
            row = _poll()
            assert row["kanban_active"] == 1 and "kanban_unknown" not in row
            dates.append(row["kanban_observed_at"])
        assert spies["recompute"] == 1
        assert spies["threads"] == 0
        # Each revalidation is a new observation: the Desktop's freshness checks see a current date.
        assert dates == sorted(dates) and len(set(dates)) == 4
        assert dates[-1] >= clock["wall"] - ksa._CACHE_TTL_S
    finally:
        conn.close()


def test_a_board_write_shows_on_the_next_poll(clock, spies, tmp_path):
    conn = _board(tmp_path / "kanban.db", [("t1", "running", "s1")])
    try:
        assert _poll()["kanban_active"] == 1
        _advance(clock, ksa._CACHE_TTL_S + 0.5)
        assert _poll()["kanban_active"] == 1
        conn.execute("INSERT INTO tasks VALUES ('t2', 'ready', 's1')")
        _advance(clock, 0.1)  # well inside the TTL: the content revision moved, so the cache is not used
        assert _poll()["kanban_active"] == 2
        conn.execute("UPDATE tasks SET status = 'archived'")
        _advance(clock, ksa._CACHE_TTL_S + 0.5)
        assert "kanban_active" not in _poll()
    finally:
        conn.close()


def test_time_derived_states_are_re_read_after_the_max_age(clock, spies, tmp_path):
    conn = _board(tmp_path / "kanban.db", [("t1", "running", "s1")])
    try:
        _poll()
        assert spies["recompute"] == 1
        _advance(clock, ksa._COUNTS_MAX_AGE_S + 0.5)
        _poll()
        assert spies["recompute"] == 2
    finally:
        conn.close()


def test_a_board_without_the_revision_counter_keeps_the_ttl_bound(clock, spies, tmp_path):
    conn = _board(tmp_path / "kanban.db", [("t1", "running", "s1")], counted=False)
    try:
        _poll()
        _advance(clock, ksa._CACHE_TTL_S + 0.5)
        _poll()
        # A stat key can miss a same-size commit inside one mtime tick: only the TTL bounds it, as before.
        assert spies["recompute"] == 2
    finally:
        conn.close()


def test_a_settled_stat_keyed_board_revalidates_until_it_is_written(clock, spies, tmp_path):
    """``boards/_archived`` on David-MBP has no revision counter (no current writer opened it); its stat is days old.
    A write after a settled stat lands in a later mtime tick, so the stat key is proof enough."""
    path = tmp_path / "kanban.db"
    conn = _board(path, [("t1", "running", "s1")], counted=False)
    try:
        old = int((clock["wall"] - 3600) * 1e9)
        os.utime(path, ns=(old, old))
        assert _poll()["kanban_active"] == 1
        _advance(clock, ksa._CACHE_TTL_S + 0.5)
        assert _poll()["kanban_active"] == 1
        assert spies == {"recompute": 1, "threads": 0}
        conn.execute("INSERT INTO tasks VALUES ('t2', 'ready', 's1')")
        _advance(clock, 0.1)
        assert _poll()["kanban_active"] == 2
    finally:
        conn.close()


def _done_board(path: Path) -> sqlite3.Connection:
    conn = _board(path, [])
    conn.execute("ALTER TABLE tasks ADD COLUMN title TEXT")
    conn.execute("ALTER TABLE tasks ADD COLUMN completed_at REAL")
    conn.execute("ALTER TABLE tasks ADD COLUMN result TEXT")
    revision.drop_triggers(conn)
    revision.ensure_revision(conn, revision.LOCAL_TABLES)
    return conn


def test_attention_checks_rerun_only_when_board_content_moved(clock, monkeypatch, tmp_path):
    conn = _done_board(tmp_path / "kanban.db")
    conn.execute("INSERT INTO tasks VALUES ('t1', 'done', 's1', 'ship it', ?, 'done')", (clock["wall"] - 60,))
    checked = []
    dispose = ksa.register_done_check("test", lambda card: checked.append(card["id"]) or True)
    try:
        assert _poll()["kanban_attention"] == 1
        assert checked == ["t1"]
        for _ in range(3):  # past the 30 s attention TTL, nothing written
            _advance(clock, ksa._ATTENTION_TTL_S + 1)
            assert _poll()["kanban_attention"] == 1
        assert checked == ["t1"]
        conn.execute("INSERT INTO tasks VALUES ('t2', 'done', 's1', 'and this', ?, 'done')", (clock["wall"] - 5,))
        _advance(clock, ksa._ATTENTION_TTL_S + 1)
        assert _poll()["kanban_attention"] == 2
        assert sorted(checked) == ["t1", "t1", "t2"]  # verdicts past their TTL re-run once the content moved
        _advance(clock, ksa._ATTENTION_MAX_AGE_S + 1)  # the 24 h window is time-derived: bounded by the max age
        _poll()
        assert len(checked) == 5
    finally:
        dispose()
        conn.close()

