"""Attention counts stay cached per origin scope when one process alternates profiles. [daveey]

``/api/profiles/projects/tree`` annotates every profile in one request. With a single-slot attention cache each
profile evicted the previous one, so every tree poll re-ran every done check for every profile (48 board reads,
2-2.5 s on the hub) and the default profile's next ``/api/sessions/<id>`` re-ran them again.
"""

import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_session_activity as ksa
from hermes_cli import profiles


def _make_board(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT NOT NULL, session_id TEXT,
                            completed_at INTEGER, result TEXT);
        CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, summary TEXT);
    """)
    conn.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """Two origin profiles on one shared kanban root, as on the hub."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    current = ["default"]
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: current[0])
    now = int(time.time())
    _make_board(tmp_path / "kanban.db", [("t1", "x (split to follow-up)", "done", "s1", now - 60, None),
                                         ("t2", "clean", "done", "s2", now - 60, None)])
    calls: list[str] = []

    def check(card):
        calls.append(card["id"])
        return "split to follow-up" in (card.get("title") or "")

    ksa._reset_cache_for_tests()
    dispose = ksa.register_done_check("test", check)
    yield current, calls
    dispose()
    ksa._reset_cache_for_tests()


def test_alternating_scopes_keep_their_attention_cache(two_profiles, monkeypatch):
    current, _calls = two_profiles
    reads = []
    real = ksa._read_recent_done
    monkeypatch.setattr(ksa, "_read_recent_done", lambda *a: reads.append(a[0]) or real(*a))
    for name in ("default", "work", "default", "work", "default"):
        current[0] = name
        assert ksa.attention_task_counts() == {"s1": 1}
    # One board read per scope, not one per call: the second visit of each scope is a cache hit.
    assert len(reads) == 2


def test_done_check_verdict_is_shared_across_scopes_on_the_same_boards(two_profiles):
    current, calls = two_profiles
    ksa.attention_task_counts()
    current[0] = "work"
    ksa.attention_task_counts()
    assert sorted(calls) == ["t1", "t2"]  # each card checked once, not once per profile


def test_scopes_seeing_different_boards_do_not_share_verdicts(two_profiles):
    current, calls = two_profiles
    ksa.attention_task_counts()
    from hermes_cli import kanban_db

    _make_board(kanban_db.boards_root() / "extra" / "kanban.db", [])
    current[0] = "work"
    ksa.attention_task_counts()
    assert sorted(calls) == ["t1", "t1", "t2", "t2"]  # the board set is part of the verdict's input


def test_projects_tree_poll_then_session_detail_do_not_rerun_checks(two_profiles):
    """The production pattern: a tree request annotates each profile in turn, then the default profile's
    ``/api/sessions/<id>`` annotates its row. Neither re-runs the done checks within the TTL."""
    current, calls = two_profiles
    for _poll in range(3):
        for name in ("default", "work", "coach"):  # one tree request
            current[0] = name
            ksa.annotate_kanban_activity([{"id": "s1"}])
        current[0] = "default"  # the session detail request
        row = {"id": "s1"}
        ksa.annotate_kanban_activity([row])
        assert row["kanban_attention"] == 1
    assert sorted(calls) == ["t1", "t2"]


def test_card_change_reruns_its_check(two_profiles, tmp_path, monkeypatch):
    current, calls = two_profiles
    ksa.attention_task_counts()
    with sqlite3.connect(tmp_path / "kanban.db") as db:
        db.execute("UPDATE tasks SET title='clean now', completed_at=completed_at+1 WHERE id='t1'")
    ksa._attention.counts.clear()  # past the scope TTL
    current[0] = "work"
    assert ksa.attention_task_counts() == {}
    assert sorted(calls) == ["t1", "t1", "t2"]


def test_registering_a_check_clears_cached_attention(two_profiles):
    assert ksa.attention_task_counts() == {"s1": 1}
    dispose = ksa.register_done_check("everything", lambda card: True)
    try:
        assert ksa.attention_task_counts() == {"s1": 1, "s2": 1}
    finally:
        dispose()
    assert ksa.attention_task_counts() == {"s1": 1}


def test_scope_cache_is_bounded(two_profiles, monkeypatch):
    current, _calls = two_profiles
    monkeypatch.setattr(ksa._attention, "max_scopes", 3)
    for i in range(10):
        current[0] = f"p{i}"
        ksa.attention_task_counts()
    assert len(ksa._attention.counts) == 3
