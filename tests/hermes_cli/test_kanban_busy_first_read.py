"""A chat's first session-tasks read waits out a brief busy board, within one budget per request. [daveey]

The Desktop session panel showed "Task status unavailable" whenever ``/api/session-kanban-tasks`` hit a board whose
lifecycle lock was held past one read's 500 ms and the chat had no memoized list yet. The retry budget is shared by
the whole request (several busy boards never stack past the Desktop timeout), and callers that pass no deadline
(the links projection under ``_links_lock``) still fail fast. Fake clock: no real waiting.
"""

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_generation as gen
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_session_activity_busy as busy


class _Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(busy.time, "monotonic", c.monotonic)
    monkeypatch.setattr(busy.time, "sleep", c.sleep)
    busy.reset()
    yield c
    busy.reset()


def _reader(fail_times: dict[str, int], calls: list[str], cost: float = 0.0, clock=None):
    """``read`` that raises busy for the first ``fail_times[path]`` calls on each path, each costing ``cost`` s."""
    def read(path, ids, statuses):
        calls.append(str(path))
        if clock is not None:
            clock.now += cost
        if fail_times.get(str(path), 0) > 0:
            fail_times[str(path)] -= 1
            raise gen.BoardLifecycleBusy()
        return [{"id": f"t-{Path(path).name}", "board": "default"}]
    return read


def test_busy_first_read_retries_until_the_board_frees(clock):
    calls: list[str] = []
    read = _reader({"a": 3}, calls, cost=0.5, clock=clock)
    out = busy.session_tasks(Path("a"), ["s1"], ("running",), read, deadline=clock.now + 3.0)
    assert [t["id"] for t in out] == ["t-a"] and calls == ["a"] * 4


def test_no_deadline_never_retries(clock):
    calls: list[str] = []
    with pytest.raises(sqlite3.OperationalError):
        busy.session_tasks(Path("a"), ["s1"], ("running",), _reader({"a": 1}, calls), deadline=None)
    assert calls == ["a"] and clock.sleeps == []


def test_non_busy_error_is_not_retried(clock):
    calls: list[str] = []

    def read(path, ids, statuses):
        calls.append(str(path))
        raise sqlite3.OperationalError("no such table: tasks")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        busy.session_tasks(Path("a"), ["s1"], ("running",), read, deadline=clock.now + 3.0)
    assert calls == ["a"] and clock.sleeps == []


def test_an_instant_busy_error_pauses_instead_of_spinning(clock):
    calls: list[str] = []
    start = clock.now
    with pytest.raises(sqlite3.OperationalError):
        busy.session_tasks(Path("a"), ["s1"], ("running",), _reader({"a": 10**6}, calls), deadline=start + 3.0)
    assert clock.sleeps and all(0 < s <= busy.FIRST_READ_RETRY_PAUSE_S for s in clock.sleeps)
    assert len(calls) <= int(3.0 / busy.FIRST_READ_RETRY_PAUSE_S) + 2
    assert clock.now - start <= 3.0 + 1e-9


def test_several_busy_boards_share_one_budget(clock):
    """Two boards that never free up: the second gets only what the first left, so the total stays bounded."""
    calls: list[str] = []
    read = _reader({"a": 10**6, "b": 10**6}, calls, cost=0.5, clock=clock)
    start = clock.now
    deadline = start + 3.0
    for p in ("a", "b"):
        with pytest.raises(sqlite3.OperationalError):
            busy.session_tasks(Path(p), ["s1"], ("running",), read, deadline=deadline)
    assert calls.count("b") == 1  # the budget was spent on "a": "b" fails after one read
    assert clock.now - start <= 3.0 + 0.5 + 0.5 + 1e-9  # budget + the last in-flight read + b's single read


def test_request_shares_one_deadline_across_boards(tmp_path, monkeypatch):
    seen: list[float | None] = []
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [tmp_path / "a", tmp_path / "b", tmp_path / "c"])
    monkeypatch.setattr(ksa, "_remote_cards", lambda *a, **k: [])
    monkeypatch.setattr(ksa._busy, "session_tasks",
                        lambda path, ids, statuses, read, *, deadline=None: seen.append(deadline) or [])
    ksa.session_tasks(["s1"])
    assert len(seen) == 3 and seen[0] is not None and len(set(seen)) == 1


def test_links_projection_never_waits_on_a_busy_board(tmp_path, monkeypatch):
    seen: list[float | None] = []
    db = tmp_path / "kanban.db"
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [db])

    def fake(path, ids, statuses, read, *, deadline=None):
        seen.append(deadline)
        raise gen.BoardLifecycleBusy()

    monkeypatch.setattr(ksa._busy, "session_tasks", fake)
    rows = [{"gateway": "g", "profile": "p", "board": "default", "id": "t1", "key": "g/default/t1",
             "session": "s1", "origin_session": "s1"}]
    with pytest.raises(sqlite3.OperationalError):
        ksa._without_local_open_duplicates(rows, gateway="g", profile="p")
    assert seen == [None]
