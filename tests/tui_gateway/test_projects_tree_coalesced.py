"""Overlapping identical ``projects.tree`` RPCs share one legacy build. [daveey]"""
from __future__ import annotations

import contextlib
import threading

import pytest

from tui_gateway import coalesced_build, server


@pytest.fixture
def legacy_tree(monkeypatch):
    from hermes_cli import projects_fact_routes

    monkeypatch.setattr(projects_fact_routes, "read", lambda *a, **k: None)
    monkeypatch.setattr(projects_fact_routes, "legacy", lambda profile: contextlib.nullcontext())
    monkeypatch.setattr(server, "_profile_db", lambda params=None, **k: contextlib.nullcontext(object()))
    builds, lock = [], threading.Lock()
    first_started, release_first = threading.Event(), threading.Event()

    def build(db, params, **kwargs):
        with lock:
            builds.append(kwargs)
            n = len(builds)
        if n == 1:
            first_started.set()
            assert release_first.wait(10)
        return {"projects": [{"id": f"build-{n}"}], "scoped_session_ids": [f"s{n}"]}, f"active-{n}"

    monkeypatch.setattr(server, "_stamped_project_tree", build)
    return builds, first_started, release_first


def _tree(params=None):
    resp = server._methods["projects.tree"](1, params or {})
    assert "error" not in resp, resp.get("error")
    return resp["result"]


def test_overlapping_requests_share_one_build_begun_after_they_asked(legacy_tree):
    builds, first_started, release_first = legacy_tree
    results = {}
    leader = threading.Thread(target=lambda: results.setdefault("leader", _tree()))
    leader.start()
    assert first_started.wait(10)
    followers = [threading.Thread(target=lambda i=i: results.setdefault(i, _tree())) for i in range(5)]
    for t in followers:
        t.start()
    for _ in range(300):
        with coalesced_build.project_tree._cond:
            if sum(s.waiters for s in coalesced_build.project_tree._states.values()) == 6:
                break
        threading.Event().wait(0.01)
    else:
        pytest.fail("followers never queued behind the running build")
    release_first.set()
    for t in (leader, *followers):
        t.join(10)
        assert not t.is_alive()
    assert len(builds) == 2
    assert results["leader"] == {"projects": [{"id": "build-1"}], "active_id": "active-1",
                                 "scoped_session_ids": ["s1"]}
    assert all(results[i]["active_id"] == "active-2" for i in range(5))
    assert coalesced_build.project_tree._states == {}


def test_a_different_selector_builds_on_its_own(legacy_tree):
    builds, first_started, release_first = legacy_tree
    results = {}
    slow = threading.Thread(target=lambda: results.setdefault("slow", _tree()))
    slow.start()
    assert first_started.wait(10)
    other = _tree({"preview_limit": 7})  # returns while the default-selector build is still running
    assert other["active_id"] == "active-2"
    assert [b["preview_limit"] for b in builds] == [3, 7]
    release_first.set()
    slow.join(10)
    assert results["slow"]["active_id"] == "active-1"


def test_callers_get_their_own_result_dict(legacy_tree):
    _, _, release_first = legacy_tree
    release_first.set()
    one = _tree()
    one["active_id"] = "changed by a caller"
    assert _tree()["active_id"] == "active-2"
