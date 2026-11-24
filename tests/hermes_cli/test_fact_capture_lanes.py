"""A capture lane is its own worker: the project tree never waits behind the board views (fact_capture_queue). [daveey]

Hub 2026-11-24, board facts on with a 300 s tree input validity: the tree's captures queued behind the board views'
on the one shared worker, its inputs still expired, and GET /api/profiles/projects/tree answered 503
dependency_unavailable (169 Desktop errors in 12 min).
"""
import threading
import time

from hermes_cli.fact_board_inputs import BoardInputSource, CapturedInput
from hermes_cli.fact_capture import CaptureBudget
from hermes_cli.fact_capture_queue import submit
from hermes_cli.fact_contract import Scope, SourceId
from hermes_cli.fact_reducers import ReducerEngine
from hermes_cli.fact_store import FactStore
from hermes_cli.fact_tree_startup import TreeRuntime


def test_a_blocked_board_lane_does_not_hold_up_the_tree_lane():
    release, ran = threading.Event(), threading.Event()
    names = []
    board = submit(lambda: release.wait(5))  # a slow board capture holds the board lane
    try:
        tree = submit(lambda: (names.append(threading.current_thread().name), ran.set()), lane='tree')
        assert ran.wait(5), 'tree capture waited behind the board lane'
        tree.join(5)
        assert names == ['fact-tree-capture']
        assert board.is_alive()
    finally:
        release.set()
        board.join(5)


def test_captures_in_one_lane_stay_serialized():
    release, ran = threading.Event(), threading.Event()
    order = []
    first = submit(lambda: (order.append('first'), release.wait(5)), lane='tree')
    second = submit(lambda: (order.append('second'), ran.set()), lane='tree')
    try:
        assert not second.done.wait(0.2)  # health: allow HX013 -- the lane must NOT run it while first holds it
        assert order == ['first']
    finally:
        release.set()
    assert ran.wait(5)  # join() cancels a job that has not started yet: wait for it to run first
    first.join(5)
    second.join(5)
    assert order == ['first', 'second']


def test_a_lane_whose_worker_died_runs_the_next_capture(monkeypatch):
    """A callback escaping with a non-Exception ends its worker; the lane must not stay registered and dead."""
    monkeypatch.setattr(threading, 'excepthook', lambda args: None)  # the dying worker's SystemExit is expected
    dying = submit(lambda: (_ for _ in ()).throw(SystemExit('stop')), lane='tree')
    assert dying.done.wait(5)
    ran = threading.Event()
    after = submit(ran.set, lane='tree')
    assert ran.wait(5), 'the lane stayed registered to a dead worker'
    after.join(5)


def test_the_board_lane_keeps_its_worker_name():
    names, ran = [], threading.Event()
    job = submit(lambda: (names.append(threading.current_thread().name), ran.set()))
    assert ran.wait(5)  # join() cancels a job that has not started yet: wait for it to run first
    job.join(5)
    assert names == ['fact-board-capture']


def test_a_tree_input_source_captures_on_the_tree_lane_while_the_board_lane_is_busy():
    release = threading.Event()
    board = submit(lambda: release.wait(10))
    scope = Scope('installation', 'default', 'tree')
    engine = ReducerEngine(FactStore(), scope)
    names, woke = [], threading.Event()

    def capture(view, selector, budget):
        names.append(threading.current_thread().name)
        return CapturedInput({'rows': []}, (), time.time())

    source = BoardInputSource(engine, SourceId(scope, 'board.input', 'native'), capture, capture_lane='tree')
    source.watch(woke.set)
    try:
        source.want('tree', {'n': 1})
        woke.clear()
        source.capture(None, CaptureBudget(max_seconds=5))
        assert woke.wait(5), 'tree input capture waited behind the board lane'
        assert names == ['fact-tree-capture']
    finally:
        release.set()
        board.join(5)
        source.close()


def test_the_tree_runtime_uses_its_own_lane_and_the_board_the_default():
    from hermes_cli.fact_view_owner import ViewOwner
    assert TreeRuntime(targets=()).owner.source._lane == 'tree'
    board = ViewOwner(Scope('installation', 'default', 'board'), lambda *a: None, lambda *a: None, ('board',))
    assert board.source._lane == 'board'
