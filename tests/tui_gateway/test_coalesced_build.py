"""Overlapping identical builds share one that began after each caller asked. [daveey]"""
from __future__ import annotations

import threading

import pytest

from tui_gateway.coalesced_build import Coalescer


def test_single_caller_runs_its_own_build_and_leaves_nothing_behind():
    c = Coalescer()
    assert c.run("k", lambda: 7) == 7
    assert c.run("k", lambda: 8) == 8  # not a cache: a later call builds again
    assert c._states == {}


def test_callers_arriving_during_a_build_share_the_next_one_not_the_running_one():
    c = Coalescer()
    first_started, release_first = threading.Event(), threading.Event()
    builds, results, lock = [], {}, threading.Lock()

    def build():
        with lock:
            builds.append(len(builds) + 1)
            n = builds[-1]
        if n == 1:
            first_started.set()
            assert release_first.wait(10)
        return n

    def call(name):
        results[name] = c.run("k", build)

    leader = threading.Thread(target=call, args=("leader",))
    leader.start()
    assert first_started.wait(10)
    followers = [threading.Thread(target=call, args=(f"f{i}",)) for i in range(6)]
    for t in followers:
        t.start()
    for _ in range(200):  # every follower is parked behind the running build
        with c._cond:
            if c._states["k"].waiters == 7:
                break
        threading.Event().wait(0.01)
    else:
        pytest.fail("followers never queued")
    release_first.set()
    for t in (leader, *followers):
        t.join(10)
        assert not t.is_alive()
    assert results["leader"] == 1
    assert {results[f"f{i}"] for i in range(6)} == {2}  # one shared build, begun after they asked
    assert builds == [1, 2]
    assert c._states == {}


def test_keys_do_not_wait_on_each_other():
    c = Coalescer()
    gate, started = threading.Event(), threading.Event()
    out = {}

    def slow():
        started.set()
        assert gate.wait(10)
        return "a"

    t = threading.Thread(target=lambda: out.setdefault("a", c.run("a", slow)))
    t.start()
    assert started.wait(10)
    assert c.run("b", lambda: "b") == "b"  # returned while "a" is still building
    gate.set()
    t.join(10)
    assert out == {"a": "a"}


def test_failed_build_reaches_only_its_caller_and_waiters_build_for_themselves():
    c = Coalescer()
    started, release = threading.Event(), threading.Event()
    calls, results, errors = [], {}, {}

    def build():
        calls.append(1)
        if len(calls) == 1:
            started.set()
            assert release.wait(10)
            raise RuntimeError("store unavailable")
        return "ok"

    def call(name):
        try:
            results[name] = c.run("k", build)
        except RuntimeError as exc:
            errors[name] = str(exc)

    leader = threading.Thread(target=call, args=("leader",))
    leader.start()
    assert started.wait(10)
    follower = threading.Thread(target=call, args=("follower",))
    follower.start()
    for _ in range(200):
        with c._cond:
            if c._states["k"].waiters == 2:
                break
        threading.Event().wait(0.01)
    release.set()
    leader.join(10)
    follower.join(10)
    assert errors == {"leader": "store unavailable"}
    assert results == {"follower": "ok"}
    assert c._states == {}
