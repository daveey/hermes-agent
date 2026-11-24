"""A paced feed source coalesces a burst of wakes into one trailing pass (``ChangeFeed`` min_interval). [daveey]

Hub 2026-11-24: clock-only kanban.db bindings (board and project-tree view owners) ran a feed pass on every
write to the file, about 4/s on the default board for each of nine boards: a guarded connection, two change
reads and a re-projection of every view on the clock each time, ~35-40% of a core in the dashboard backend.
"""
import threading
import time
from types import SimpleNamespace

from hermes_cli.fact_content_owner import CLOCK_ONLY_INTERVAL, ContentBinding, ContentOwner
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_feed import ChangeFeed
from hermes_cli.fact_process import _Adapter
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore
from tests.hermes_cli import test_fact_task_source as tasks
from tests.waiting import wait_until

home, board = tasks.home, tasks.board


class Paced:
    def __init__(self, min_interval):
        self.min_interval = min_interval
        self.starts = []
        self.wake = None

    def watch(self, wake):
        self.wake = wake
        return self

    def close(self):
        pass

    def capture(self, cursor, budget):
        self.starts.append(time.monotonic())
        return SimpleNamespace(after=len(self.starts))


def _feed(adapter):
    feed = ChangeFeed(lambda *a: None, lambda *a: None)
    feed.register(adapter)
    assert wait_until(lambda: len(adapter.starts) == 1, timeout=5)  # registration's own pass
    return feed


def test_a_burst_of_wakes_becomes_one_pass_after_the_interval():
    source = Paced(0.3)
    feed = _feed(source)
    try:
        for _ in range(10):
            source.wake()
        assert wait_until(lambda: len(source.starts) == 2, timeout=5)
        assert source.starts[1] - source.starts[0] >= 0.3 - 0.01  # paced from last_start, stamped just before
        # health: allow HX013 -- the quiet interval after the trailing pass IS the behaviour under test
        threading.Event().wait(0.5)
        assert len(source.starts) == 2  # the whole burst cost one pass
    finally:
        feed.close()


def test_an_unpaced_source_still_runs_on_the_leading_edge():
    source = Paced(0)
    feed = _feed(source)
    try:
        woke = time.monotonic()
        source.wake()
        assert wait_until(lambda: len(source.starts) == 2, timeout=5)
        assert source.starts[1] - woke < 0.3
    finally:
        feed.close()


def test_a_quiet_paced_source_runs_at_once_once_its_interval_has_passed():
    source = Paced(1.0)
    feed = _feed(source)
    try:
        # health: allow HX013 -- an idle period longer than the interval IS the precondition under test
        threading.Event().wait(1.1)
        woke = time.monotonic()
        source.wake()
        assert wait_until(lambda: len(source.starts) == 2, timeout=5)
        assert source.starts[1] - woke < 0.5  # at once, not a full interval after the wake
    finally:
        feed.close()


def test_the_process_wrapper_forwards_pacing():
    assert _Adapter(Paced(2.5), owner=None).min_interval == 2.5
    assert _Adapter(SimpleNamespace(), owner=None).min_interval == 0


def test_only_clock_only_content_bindings_are_paced(board):
    path = next(r[2] for r in board.execute("PRAGMA database_list") if r[1] == "main")
    scope = Scope("install", "profile")
    clock, rows = SourceId(scope, "tasks.content", "clock"), SourceId(scope, "tasks.content", "rows")
    store = FactStore()
    owner = ContentOwner({scope: ReducerEngine(store, scope)},
                         [ContentBinding(clock, path, clock_only=True), ContentBinding(rows, path)])
    try:
        paced = {adapter.source: getattr(adapter, "min_interval", 0) for adapter in owner._publishers
                 if getattr(adapter, "source", None) in (clock, rows) and hasattr(adapter, "path")}
        assert paced == {clock: CLOCK_ONLY_INTERVAL, rows: 0}
        states = [owner.publisher(source).state_key for source in (clock, rows)]
        assert wait_until(lambda: all(store.snapshot(scope).get(key) is not None for key in states), timeout=10)
    finally:
        owner.close()  # after both first passes: no "source owner closed" capture error in the log
