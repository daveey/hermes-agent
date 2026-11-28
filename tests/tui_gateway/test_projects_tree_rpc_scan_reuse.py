"""The ``projects.tree`` RPC reads its store once per commit (t_01a20286f9f3f262). [daveey]

On the hub the Desktop's own-profile tree RPC re-read the 2,000-row session page and re-aggregated
``distinct_session_cwds`` over every session on each request, though the store had not committed:
the tui-rpc pool ran ~15k voluntary context switches/s in that loop. Both reads are now kept until
the store's ``PRAGMA data_version`` moves; annotation and the tree build still run per request.
"""
from __future__ import annotations

import pytest

from tests.tui_gateway.test_projects_rpc import (  # fixtures, used by name
    _bind_profiles, _call, _create_project, _create_session, _fast_git_probe, _profile_dir,
    _serving_launch_profile, _workspace_temp_policy)


@pytest.fixture(autouse=True)
def _cold():
    from tui_gateway import project_tree_scan

    project_tree_scan.clear()
    yield
    project_tree_scan.clear()


@pytest.fixture
def reads(monkeypatch):
    """Store reads the tree RPC made, by kind, in order."""
    import hermes_state

    seen: list[str] = []
    page, cwds = hermes_state.SessionDB.list_sessions_rich, hermes_state.SessionDB.distinct_session_cwds

    def counting_page(self, *args, **kwargs):
        if kwargs.get("limit") == 2000:  # only the tree's page
            seen.append("page")
        return page(self, *args, **kwargs)

    def counting_cwds(self, *args, **kwargs):
        seen.append("cwds")
        return cwds(self, *args, **kwargs)

    monkeypatch.setattr(hermes_state.SessionDB, "list_sessions_rich", counting_page)
    monkeypatch.setattr(hermes_state.SessionDB, "distinct_session_cwds", counting_cwds)
    return seen


@pytest.fixture
def launch(monkeypatch, tmp_path):
    home = _profile_dir(tmp_path, "launch")
    repo = tmp_path / "repos" / "scan-reuse"
    repo.mkdir(parents=True)
    _bind_profiles(monkeypatch, tmp_path, {"default": home})
    _create_project(home, "Reuse", repo, use=True)
    _create_session(home, "s_one", repo)
    return home, repo


def _members(tree) -> list[str]:
    return sorted(tree["scoped_session_ids"])


def _stable(value):
    """``value`` without the per-response annotation clock (``kanban_observed_at`` is when it was stamped)."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k != "kanban_observed_at"}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def test_an_unchanged_store_is_read_once(launch, reads):
    home, _repo = launch
    with _serving_launch_profile(home):
        first = _call("projects.tree")
        second = _call("projects.tree")
    assert reads == ["page", "cwds"]
    assert _stable(first) == _stable(second)
    assert _members(second) == ["s_one"]


def test_a_commit_rereads_and_shows_the_new_session(launch, reads):
    home, repo = launch
    with _serving_launch_profile(home):
        assert _members(_call("projects.tree")) == ["s_one"]
        _create_session(home, "s_two", repo)
        after = _call("projects.tree")
        again = _call("projects.tree")
    assert reads == ["page", "cwds", "page", "cwds"]
    assert _members(after) == _members(again) == ["s_one", "s_two"]


def test_a_caller_mutating_its_answer_does_not_reach_the_kept_copy(launch, reads):
    home, _repo = launch
    with _serving_launch_profile(home):
        first = _call("projects.tree", {"preview_limit": 5})
        for project in first["projects"]:
            for row in project.get("previewSessions") or []:
                row["title"] = "edited by a caller"
        second = _call("projects.tree", {"preview_limit": 5})
    assert reads.count("page") == 1
    rows = [row for project in second["projects"] for row in project.get("previewSessions") or []]
    assert rows and all(row.get("title") != "edited by a caller" for row in rows)


def test_capture_worker_fast_exit_closes_the_rpc_tree_observer(launch, reads):
    """The capture worker's early exit verifies every retained observer is closed; this one too."""
    from hermes_cli import sessions_capture_worker
    from tui_gateway import project_tree_scan

    home, _repo = launch
    with _serving_launch_profile(home):
        _call("projects.tree")
    assert project_tree_scan._CACHE._profiles  # the observer is retained between requests
    assert project_tree_scan._LOCK.acquire(blocking=False)
    try:  # a read in flight: no early exit, and its observer is left alone
        assert sessions_capture_worker._cleaned() is False
        assert project_tree_scan._CACHE._profiles
    finally:
        project_tree_scan._LOCK.release()
    sessions_capture_worker._cleaned()
    assert not project_tree_scan._CACHE._profiles
