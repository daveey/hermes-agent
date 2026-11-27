"""The all-profiles project tree reads each store's rows once per commit (t_01a21ab4eb964866). [daveey]

On the hub 12 of 13 profile stores are idle between the Desktop's tree refreshes, yet each response re-ranked and
re-projected every store's 2,000-row page. The rows are a pure projection of the store (plus its config, in the
key), so they are kept until that store commits; the activity annotation and the tree build still run on every
response.
"""
from __future__ import annotations

import pytest

from tests.hermes_cli import test_profiles_sidebar_scope as sidebar

profiles_on_disk = sidebar.profiles_on_disk
_seed_session = sidebar._seed_session


@pytest.fixture(autouse=True)
def _cold(monkeypatch):
    from hermes_cli.web_routers import profiles

    monkeypatch.setattr(profiles, "_SIDEBAR_CACHE_TTL_SECONDS", 0.0)
    kept = getattr(profiles, "_PROJECT_TREE_SCAN_CACHE", None)
    if kept is not None:
        kept.clear()
    yield
    if kept is not None:
        kept.clear()


@pytest.fixture
def hooks():
    """``hooks["after"](name)`` runs after a store's page is read; ``hooks["before"](name)`` before (may raise)."""
    return {"before": lambda name: None, "after": lambda name: None}


@pytest.fixture
def scans(monkeypatch, profiles_on_disk, hooks):
    """Profile names whose session page the tree read from their store, in order."""
    from pathlib import Path

    import hermes_state

    real = hermes_state.SessionDB.list_sessions_rich
    names = {str(Path(home) / "state.db"): name for name, home in profiles_on_disk.items()}
    seen: list[str] = []

    def counting(self, *args, **kwargs):
        name = names.get(str(Path(self.db_path)))
        if name is None or kwargs.get("limit") != 2000:  # only the tree's page, not other readers
            return real(self, *args, **kwargs)
        hooks["before"](name)
        seen.append(name)
        rows = real(self, *args, **kwargs)
        hooks["after"](name)
        return rows

    monkeypatch.setattr(hermes_state.SessionDB, "list_sessions_rich", counting)
    return seen


def _tree():
    from hermes_cli.web_routers import profiles

    return profiles.get_profiles_projects_tree()


def _session_ids(payload) -> set[str]:
    return {m["id"] for p in payload["projects"] for m in p["sessionMembership"]}


def _stable(value):
    """``value`` without the per-response annotation clock (``kanban_observed_at`` is when it was stamped)."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k != "kanban_observed_at"}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def test_an_idle_store_is_not_read_again_and_its_commit_is_seen_by_the_next_response(profiles_on_disk, scans):
    for name, home in profiles_on_disk.items():
        _seed_session(home, f"{name}-chat", source="cli")
    first = _tree()
    assert _session_ids(first) == {"default-chat", "worker-chat"}
    assert sorted(scans) == ["default", "worker"]

    scans.clear()
    assert _stable(_tree()) == _stable(first), "an unchanged store answers the same tree"
    assert scans == [], "no store committed, so none is read again"

    _seed_session(profiles_on_disk["worker"], "worker-new", source="cli")
    # Notification loss would serve the old rows until some later commit.
    assert _session_ids(_tree()) == {"default-chat", "worker-chat", "worker-new"}
    assert scans == ["worker"], "only the store that committed is read again"


def test_activity_is_stamped_on_every_response_and_never_leaks_into_the_kept_rows(profiles_on_disk, scans, monkeypatch):
    from tui_gateway import server as gateway_server

    _seed_session(profiles_on_disk["default"], "chat", source="cli")
    live: set[str] = {"chat"}

    def annotate(rows):
        for row in rows:
            if row["id"] in live:
                row["turn_live"] = True
        return rows

    monkeypatch.setattr(gateway_server, "_annotate_tree_activity", annotate)

    def stamped(payload):
        rows = [s for p in payload["projects"] for s in p.get("previewSessions") or []]
        return {s["id"]: s.get("turn_live", False) for s in rows}

    assert stamped(_tree()) == {"chat": True}
    live.clear()  # the turn ended without a state.db commit the tree's rows read
    assert stamped(_tree()) == {"chat": False}, "a stamp from an earlier response must not survive in the kept rows"
    assert scans == ["default"]


def test_a_config_change_without_a_commit_is_seen(profiles_on_disk, scans):
    home = profiles_on_disk["worker"]
    _seed_session(home, "worker-chat", source="cli")
    _seed_session(home, "worker-bot", source="telegram")
    assert {"worker-chat", "worker-bot"} <= _session_ids(_tree())

    (home / "config.yaml").write_text("sessions:\n  sidebar_exclude_sources: [telegram]\n", encoding="utf-8")
    ids = _session_ids(_tree())
    assert "worker-chat" in ids and "worker-bot" not in ids, "the store's sidebar policy is part of the key"


def test_a_commit_during_the_read_is_not_kept(profiles_on_disk, scans, hooks):
    home = profiles_on_disk["worker"]
    _seed_session(home, "worker-chat", source="cli")
    raced: list[bool] = []

    def race(name):
        if name == "worker" and not raced:
            raced.append(True)
            _seed_session(home, "worker-raced", source="cli")  # commits after the page was read

    hooks["after"] = race
    assert "worker-raced" not in _session_ids(_tree())
    assert "worker-raced" in _session_ids(_tree()), "a read that raced a commit must not be served again"


def test_a_failed_read_is_reported_and_retried(profiles_on_disk, scans, hooks):
    for name, home in profiles_on_disk.items():
        _seed_session(home, f"{name}-chat", source="cli")
    fail = [True]

    def flaky(name):
        if fail[0] and name == "worker":
            raise RuntimeError("worker store is unreadable")

    hooks["before"] = flaky
    payload = _tree()
    assert [e["profile"] for e in payload["errors"]] == ["worker"]
    assert _session_ids(payload) == {"default-chat"}
    fail[0] = False
    payload = _tree()
    assert payload["errors"] == []
    assert _session_ids(payload) == {"default-chat", "worker-chat"}, "a failure is never kept"
