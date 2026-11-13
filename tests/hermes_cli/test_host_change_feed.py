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
    src = _Source(**{"board:/b/kanban.db": 1, "replica": 7})
    feed = _feed(src)
    boards = feed.subscribe("boards", ["board:*"])
    replica = feed.subscribe("remote", ["replica"])
    src.keys["replica"] = 8
    feed.poll_once()
    assert replica.wait(5.0) == {"replica"}
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
    src = _Source(replica=1, other=1)
    feed = _feed(src)
    sub = feed.subscribe("x", ["replica"])
    src.keys["replica"] = hcf.UNKNOWN
    assert "replica" in feed.poll_once()
    assert "replica" in feed.poll_once()  # still unknown: never proof that nothing moved
    assert sub.wait(0.0) == {"replica"}
    src.fail = True
    assert feed.poll_once() == {"replica", "other"}  # the probe itself failed: every topic is unknown


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
    src = _Source(replica=1)
    feed = hcf.HostChangeFeed(src, interval=0.01)
    sub = feed.subscribe("remote", ["replica"])  # autostart
    try:
        from tests.waiting import wait_until

        wait_until(lambda: feed.probes >= 2, timeout=10.0)
        src.keys["replica"] = 2
        assert sub.wait(10.0) == {"replica"}
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
    assert not any(t == "state" or t.startswith("state:") for t in keys)  # state.db is per subscriber


def test_process_feed_never_autostarts_under_pytest():
    feed = hcf.feed()
    feed.subscribe("x", ["replica"])
    assert feed._thread is None
    assert hcf.feed() is feed


# ---- state.db is resolved per subscriber profile (PR #2633 review nit 1) -------------------------------------


def _state_db(home):
    home.mkdir(parents=True, exist_ok=True)
    db = home / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT)")
    return db


@pytest.fixture
def per_profile_state(monkeypatch):
    """Production resolution of state.db: the hermetic conftest re-points ``DEFAULT_DB_PATH`` (which wins over
    the profile home); restore the import default so ``_default_db_path`` follows the context's home."""
    import hermes_state

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)


def _subscribe_as(feed: hcf.HostChangeFeed, home, name: str) -> hcf.Subscription:
    """Subscribe to the bare ``state`` topic from a context bound to ``home`` (another profile's loop)."""
    import contextvars

    import hermes_scope

    def run() -> hcf.Subscription:
        token = hermes_scope.push_state(home=str(home))
        try:
            return feed.subscribe(name, [hcf.STATE])
        finally:
            token.reset()

    return contextvars.Context().run(run)


@pytest.mark.usefixtures("per_profile_state")
def test_state_topic_resolves_in_each_subscribers_profile(tmp_path):
    a_db, b_db = _state_db(tmp_path / "a"), _state_db(tmp_path / "b")
    feed = hcf.HostChangeFeed(_Source(), autostart=False)
    feed.poll_once()  # baseline
    a = _subscribe_as(feed, tmp_path / "a", "sessions-a")
    b = _subscribe_as(feed, tmp_path / "b", "sessions-b")
    assert a.topics == (hcf.state_topic(a_db),)
    assert b.topics == (hcf.state_topic(b_db),)
    assert feed.poll_once() == frozenset()  # subscribing records each db's baseline: no spurious wake

    with sqlite3.connect(b_db) as conn:
        conn.execute("INSERT INTO sessions VALUES ('b1')")
    assert feed.poll_once() == {hcf.state_topic(b_db)}
    assert b.wait(5.0) == {hcf.state_topic(b_db)}
    assert a.wait(0.0) == frozenset()  # profile A's loop never wakes for profile B's state.db

    with sqlite3.connect(a_db) as conn:
        conn.execute("INSERT INTO sessions VALUES ('a1')")
    assert feed.poll_once() == {hcf.state_topic(a_db)}
    assert a.wait(5.0) == {hcf.state_topic(a_db)}
    assert b.wait(0.0) == frozenset()


@pytest.mark.usefixtures("per_profile_state")
def test_state_db_is_probed_only_while_subscribed(tmp_path):
    db = _state_db(tmp_path / "a")
    feed = _feed(_Source(replica=1))
    sub = _subscribe_as(feed, tmp_path / "a", "sessions")
    assert hcf.state_topic(db) in feed.keys()
    sub.close()
    feed.poll_once()
    assert hcf.state_topic(db) not in feed.keys()


@pytest.mark.usefixtures("per_profile_state")
def test_unreadable_state_db_wakes_its_subscriber(tmp_path):
    db = _state_db(tmp_path / "a")
    reads = {"fail": False}

    def state_probe(path):
        if reads["fail"]:
            raise sqlite3.OperationalError("disk I/O error")
        return hcf.state_key(path)

    feed = hcf.HostChangeFeed(_Source(), autostart=False, state_probe=state_probe)
    feed.poll_once()
    sub = _subscribe_as(feed, tmp_path / "a", "sessions")
    reads["fail"] = True
    assert feed.poll_once() == {hcf.state_topic(db)}
    assert sub.wait(0.0) == {hcf.state_topic(db)}


def test_probe_thread_is_detached_from_the_first_subscribers_scope(tmp_path):
    import hermes_scope
    from hermes_cli.local_work import ACTIVE_SCOPE, Registry
    from hermes_cli.local_work_threads import TrackedThread
    from tests.waiting import wait_until

    seen: list[tuple[object, object]] = []

    def probe() -> dict[str, object]:
        seen.append((ACTIVE_SCOPE.get(), hermes_scope.current_state().home))
        return {}

    registry = Registry(homes=[str(tmp_path)])
    feed = hcf.HostChangeFeed(probe, interval=0.01)
    token = hermes_scope.push_state(home=str(tmp_path / "profile-a"))
    try:
        with registry.scope("turn", home=str(tmp_path), source="first-subscriber"):
            feed.subscribe("x", ["replica"])  # autostart inside an active work scope
            assert not isinstance(feed._thread, TrackedThread)
        wait_until(lambda: len(seen) >= 1, timeout=10.0)
        assert registry.snapshot().active == 0  # the probe thread is not that scope's work
        assert feed._thread.is_alive()
        assert seen[0] == (None, None)  # no captured work scope, no captured profile home
    finally:
        token.reset()
        feed.stop()
