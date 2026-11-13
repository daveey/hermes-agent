"""Host change feed: one probe thread wakes subscribed loops (design t_01a1d563324d7e8e §6, card P). [daveey]"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from hermes_cli import host_change_feed as hcf
from hermes_cli import loop_accounting as la


class _Source:
    """A mutable set of counters the feed probes."""

    def __init__(self, **keys: object) -> None:
        self.keys: dict[str, object] = dict(keys)
        self.fail = False

    def __call__(self) -> dict[str, object]:
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        return dict(self.keys)


@pytest.fixture(autouse=True)
def _fresh():
    hcf.reset()
    la.reset()
    yield
    hcf.reset()


def _feed(src: _Source) -> hcf.HostChangeFeed:
    feed = hcf.HostChangeFeed(src, autostart=False)
    feed.poll_once()  # baseline
    return feed


def test_subscriber_is_woken_when_its_counter_moves():
    src = _Source(**{"board:/b/kanban.db": 1, "replica": 7, "state": 3})
    feed = _feed(src)
    sub = feed.subscribe("sync", ["board:*", "replica"])
    woke: list[frozenset[str]] = []
    waiter = threading.Thread(target=lambda: woke.append(sub.wait(30.0)))
    waiter.start()
    started = time.monotonic()
    src.keys["board:/b/kanban.db"] = 2
    assert feed.poll_once() == {"board:/b/kanban.db"}
    waiter.join(10.0)
    assert not waiter.is_alive()
    assert woke == [frozenset({"board:/b/kanban.db"})]
    assert time.monotonic() - started < 10.0  # woken by the change, not by its 30 s due time


def test_subscriber_is_woken_on_its_due_time_with_no_change():
    src = _Source(**{"board:/b/kanban.db": 1, "replica": 7, "state": 3})
    feed = _feed(src)
    sub = feed.subscribe("notifier", ["board:*"])
    started = time.monotonic()
    assert feed.poll_once() == frozenset()
    assert sub.wait(0.2) == frozenset()  # the due time passed: an empty wake, the loop runs its timed work
    assert time.monotonic() - started >= 0.2


def test_only_subscribers_of_a_moved_topic_wake():
    src = _Source(**{"board:/b/kanban.db": 1, "replica": 7, "state": 3})
    feed = _feed(src)
    boards = feed.subscribe("boards", ["board:*"])
    state = feed.subscribe("sessions", ["state"])
    src.keys["state"] = 4
    feed.poll_once()
    assert state.wait(5.0) == {"state"}
    assert boards.wait(0.0) == frozenset()


def test_change_while_busy_is_returned_by_the_next_wait():
    src = _Source(replica=1)
    feed = _feed(src)
    sub = feed.subscribe("remote_index", ["replica"])
    src.keys["replica"] = 2
    feed.poll_once()
    src.keys["replica"] = 3
    feed.poll_once()
    assert sub.wait(0.0) == {"replica"}  # coalesced, delivered at once without waiting
    assert sub.wait(0.0) == frozenset()


def test_first_probe_is_only_a_baseline():
    src = _Source(replica=1)
    feed = hcf.HostChangeFeed(src, autostart=False)
    sub = feed.subscribe("x", ["replica"])
    assert feed.poll_once() == frozenset()
    assert sub.wait(0.0) == frozenset()


def test_unreadable_counter_wakes_on_every_probe():
    src = _Source(replica=1, state=1)
    feed = _feed(src)
    sub = feed.subscribe("x", ["replica"])
    src.keys["replica"] = hcf.UNKNOWN
    assert "replica" in feed.poll_once()
    assert "replica" in feed.poll_once()  # still unknown: never proof that nothing moved
    assert sub.wait(0.0) == {"replica"}
    src.fail = True
    assert feed.poll_once() == {"replica", "state"}  # the probe itself failed: every topic is unknown


def test_appearing_and_disappearing_topics_move():
    src = _Source(**{"board:/a/kanban.db": 1})
    feed = _feed(src)
    sub = feed.subscribe("x", ["board:*"])
    src.keys["board:/b/kanban.db"] = 1
    del src.keys["board:/a/kanban.db"]
    assert feed.poll_once() == {"board:/a/kanban.db", "board:/b/kanban.db"}
    assert sub.wait(0.0) == {"board:/a/kanban.db", "board:/b/kanban.db"}


def test_wait_returns_on_stop_and_close():
    feed = _feed(_Source(replica=1))
    sub = feed.subscribe("x", ["replica"])
    stop = threading.Event()
    stop.set()
    assert sub.wait(30.0, stop=stop) == frozenset()
    sub.close()
    assert sub.wait(30.0) == frozenset()
    feed.poll_once()
    assert sub not in feed._subs


def test_async_wait_woken_by_change_and_by_due_time():
    src = _Source(replica=1)
    feed = _feed(src)
    sub = feed.subscribe("notifier", ["replica"])

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        assert await sub.wait_async(0.2) == frozenset()  # due time, no change
        assert loop.time() - started >= 0.2

        def bump():
            src.keys["replica"] = 2
            feed.poll_once()

        loop.call_later(0.05, lambda: threading.Thread(target=bump).start())
        started = loop.time()
        assert await sub.wait_async(30.0) == {"replica"}
        assert loop.time() - started < 10.0
        stopping = loop.time()
        assert await sub.wait_async(30.0, running=lambda: False) == frozenset()
        assert loop.time() - stopping < 5.0

    asyncio.run(scenario())


def test_probe_thread_wakes_subscriber_and_reports_passes():
    src = _Source(state=1)
    feed = hcf.HostChangeFeed(src, interval=0.01)
    sub = feed.subscribe("sessions", ["state"])  # autostart
    try:
        from tests.waiting import wait_until

        wait_until(lambda: feed.probes >= 2, timeout=10.0)
        src.keys["state"] = 2
        assert sub.wait(10.0) == {"state"}
        counters = la.counters()["change_feed.probe"]
        assert counters["changes"] >= 1 and counters["skips"] >= 1
    finally:
        feed.stop()


def test_replica_key_moves_with_the_cursor_only(tmp_path):
    hub = tmp_path / "hub.db"
    assert hcf.replica_key(hub) is None
    with sqlite3.connect(hub) as conn:
        conn.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [("cursor", "10"), ("pulled_at", "1")])
    first = hcf.replica_key(hub)
    with sqlite3.connect(hub) as conn:
        conn.execute("UPDATE meta SET v = '2' WHERE k = 'pulled_at'")  # hub bookkeeping
    assert hcf.replica_key(hub) == first
    with sqlite3.connect(hub) as conn:
        conn.execute("UPDATE meta SET v = '11' WHERE k = 'cursor'")
    assert hcf.replica_key(hub) != first


def test_replica_key_unreadable_is_unknown(tmp_path):
    hub = tmp_path / "hub.db"
    hub.write_bytes(b"not a database" * 100)
    assert hcf.replica_key(hub) is hcf.UNKNOWN


def test_state_key_moves_on_another_connections_commit(tmp_path):
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT)")
    first = hcf.state_key(db)
    assert hcf.state_key(db) == first
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO sessions VALUES ('s1')")
    assert hcf.state_key(db) != first


def test_default_probe_covers_boards_replica_and_state(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_activity as ksa

    board = tmp_path / "kanban.db"
    sqlite3.connect(board).close()
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [board])
    monkeypatch.setattr(ksa, "_board_content_key", lambda path: ("revision", str(path), 5))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub"))
    keys = hcf.default_probe()
    assert keys["board:" + str(board)] == ("revision", str(board), 5)
    assert keys[hcf.REPLICA] is None  # no hub replica on this host
    assert hcf.STATE in keys


def test_process_feed_never_autostarts_under_pytest():
    feed = hcf.feed()
    feed.subscribe("x", ["state"])
    assert feed._thread is None
    assert hcf.feed() is feed
