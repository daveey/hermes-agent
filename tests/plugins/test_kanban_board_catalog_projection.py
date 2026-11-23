"""Canonical catalog projections consume captured values, never readers."""
from copy import deepcopy

from plugins.kanban.dashboard import board_catalog_projection as projection


def test_catalog_exact_shapes_and_caller_isolation(monkeypatch):
    import builtins
    import socket
    import sqlite3
    import subprocess
    import time

    projects = [{"id": "p", "slug": "project", "name": "Project", "primary_path": None,
                 "icon": None, "color": "blue", "archived": False}]
    boards = [{"slug": "a", "project_id": "p", "extra": {"preserved": [1]}},
              {"slug": "b", "project_id": "missing"}, {"slug": "c"}]
    original = deepcopy(boards)

    def denied(*args, **kwargs):
        raise AssertionError("projection performed I/O or read the clock")

    for target, attribute in ((builtins, "open"), (socket, "socket"), (sqlite3, "connect"),
                              (subprocess, "Popen"), (time, "time"), (time, "monotonic")):
        monkeypatch.setattr(target, attribute, denied)
    assert projection.projects(projects) == {"projects": [
        {"id": "p", "slug": "project", "name": "Project", "primary_path": "", "icon": "", "color": "blue"}]}
    result = projection.boards(boards, "b", counts={"a": {"ready": 2, "archived": 9}, "b": {}, "c": {}},
                               workspace_kinds={"a": "worktree", "b": "dir", "c": "scratch"},
                               project_names={"p": "Project"})
    assert result == {"current": "b", "boards": [
        {**original[0], "is_current": False, "counts": {"ready": 2, "archived": 9}, "total": 2,
         "default_workspace_kind": "worktree", "project_name": "Project"},
        {**original[1], "is_current": True, "counts": {}, "total": 0,
         "default_workspace_kind": "dir", "project_name": None},
        {**original[2], "is_current": False, "counts": {}, "total": 0,
         "default_workspace_kind": "scratch", "project_id": None, "project_name": None}]}
    result["boards"][0]["extra"]["preserved"].append(2)
    assert boards == original


def test_unknown_capture_is_not_silently_empty():
    import pytest

    with pytest.raises(KeyError):
        projection.boards([{"slug": "missing"}], "missing", counts={},
                          workspace_kinds={"missing": "scratch"}, project_names={})
    assert projection.boards([], "default", counts={}, workspace_kinds={}, project_names={}) == {
        "boards": [], "current": "default"}
    assert projection.projects([]) == {"projects": []}
