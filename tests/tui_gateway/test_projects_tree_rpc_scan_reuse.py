"""The ``projects.tree`` RPC reads its store once per commit (t_01a20286f9f3f262). [daveey]

On the hub the Desktop's own-profile tree RPC re-read the 2,000-row session page and re-aggregated
``distinct_session_cwds`` over every session on each request, though the store had not committed:
the tui-rpc pool ran ~15k voluntary context switches/s in that loop. Both reads are now kept until
the SessionDB's write generation moves; annotation and the tree build still run per request.
"""
from __future__ import annotations

import gc
import sqlite3

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
        _create_session(home, "s_two", repo)  # another connection's commit
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


def test_reuse_opens_no_connection_and_ends_with_the_store(launch, monkeypatch):
    """Reuse rides the SessionDB's own writer: nothing here opens or retains SQLite (profile deletion's
    custody drains only the stores it knows), a closed store is never reused, and the kept rows go with it."""
    from hermes_state import SessionDB
    from tui_gateway import project_tree_scan

    home, _repo = launch
    db = SessionDB(db_path=home / "state.db")
    scans: list[int] = []

    def scan():
        scans.append(1)
        return [{"id": "row", "_lineage_ids": ["a"]}]

    project_tree_scan.read(db, ("k",), scan)
    opened: list[object] = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: opened.append(a) or real_connect(*a, **k))
    kept = project_tree_scan.read(db, ("k",), scan)
    kept[0]["_lineage_ids"].append("caller")
    assert project_tree_scan.read(db, ("k",), scan) == [{"id": "row", "_lineage_ids": ["a"]}]
    assert len(scans) == 1 and opened == []
    db.close()
    project_tree_scan.read(db, ("k",), scan)
    assert len(scans) == 2  # closed: equal to nothing
    del db
    gc.collect()
    assert len(project_tree_scan._KEPT) == 0
