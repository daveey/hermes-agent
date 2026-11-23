"""Backend boot builds the all-profiles project tree before the Desktop's reconnect burst.

After the board fix (b4f69efc705), the first ``GET /api/profiles/projects/tree`` after the 22:25Z 2026-11-01 hub
restart still took 4.02 s: the first build in a new process pays the lazy gateway import, one git probe per session
cwd and every profile's cold annotation scope (~3 s uncontended, ~0.15-0.4 s warm), and it paid them inside the burst.
(t_01a19864fc42f5b2) [daveey]
"""

from __future__ import annotations

import threading
from collections import OrderedDict

import pytest

from hermes_cli import kanban_annotation_prewarm as boot
from hermes_cli import projects_tree_prewarm as tree_prewarm
from hermes_cli.web_routers import profiles as profiles_routes


def _clear_tree_cache():
    """The route's single-flight cache lives in the decorator's closure; start every test with it empty."""
    route = profiles_routes.get_profiles_projects_tree
    while route is not None:
        for cell in route.__closure__ or ():
            value = cell.cell_contents
            if isinstance(value, OrderedDict):
                value.clear()
        route = getattr(route, "__wrapped__", None)


@pytest.fixture
def counted_scans(monkeypatch):
    _clear_tree_cache()
    scans = []

    def fake_trees(preview_limit=3, session_limit=2000, errors=None, *, log_label="", profile=None):
        scans.append((preview_limit, session_limit, profile))
        return [("default", {"projects": [], "scoped_session_ids": ["s1"]})]

    monkeypatch.setattr(profiles_routes, "profile_project_trees", fake_trees)
    yield scans
    _clear_tree_cache()


def test_prewarm_builds_the_tree_through_the_route(counted_scans):
    assert tree_prewarm.prewarm_project_tree() is True
    assert counted_scans == [(3, 2000, None)]


def test_prewarm_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("state.db unreadable")

    monkeypatch.setattr(profiles_routes, "get_profiles_projects_tree", boom)
    assert tree_prewarm.prewarm_project_tree() is False


def test_boot_thread_warms_the_annotation_then_the_tree(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    order = []
    done = threading.Event()
    monkeypatch.setattr(boot, "prewarm_annotation", lambda: order.append("annotation") or True)
    monkeypatch.setattr(tree_prewarm, "prewarm_project_tree", lambda: order.append("tree") or done.set() or True)
    thread = boot.start_annotation_prewarm()
    assert thread is not None and thread.daemon
    thread.join(5)
    assert done.is_set()
    assert order == ["annotation", "tree"]


def test_first_tree_poll_after_the_prewarm_does_not_scan_again(counted_scans, monkeypatch, _isolate_hermes_home):
    """The point of the prewarm: the Desktop's first tree poll reads the warm result, it does not run a cold scan."""
    from starlette.testclient import TestClient

    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    monkeypatch.setattr(profiles_routes, "_SIDEBAR_CACHE_TTL_SECONDS", 60.0)
    assert tree_prewarm.prewarm_project_tree() is True
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    resp = client.get("/api/profiles/projects/tree")
    assert resp.status_code == 200
    assert resp.json()["scoped_session_ids"] == ["s1"]
    assert counted_scans == [(3, 2000, None)]
