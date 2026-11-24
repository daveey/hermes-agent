"""Explicit replica acquisition owns config, ledger, fallback and freshness inputs."""
from __future__ import annotations

import sqlite3

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_session_activity as activity
from tests.hermes_cli.test_kanban_chat_links import ME, OTHER, _ledger_row, _replica


@pytest.fixture(autouse=True)
def isolated_links(monkeypatch):
    links._reset_for_tests()

    def forbidden(*args, **kwargs):
        pytest.fail("explicit acquisition read ambient home")

    monkeypatch.setattr(sync, "sync_config", forbidden)
    monkeypatch.setattr(activity, "_remote_links", forbidden)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", forbidden)
    monkeypatch.setattr("hermes_cli.kanban_sync_client.board_paths", forbidden)
    yield
    links._reset_for_tests()


def test_empty_explicit_scope_does_not_reuse_ambient_profile_index():
    # Seed a foreign owner's projection; empty input must replace, not reuse it.
    links._ledger_index(cards=[{**_ledger_row("t_foreign", "s1"), "profile": "work"}])
    assert links.replica_cards(["s1"], ME, replicas=[], profile="default", origin_cards=[]) == []


@pytest.mark.parametrize("origin", [None, []])
def test_explicit_scope_without_ledger_is_empty(tmp_path, origin):
    assert links.replica_cards(["s1"], ME, replicas=[("default", tmp_path / "missing.db")],
                               profile="default", origin_cards=origin) == []


def test_cold_unreadable_replica_uses_only_supplied_ledger(tmp_path):
    row = _ledger_row("t_scoped", "s1")
    unreadable = []
    got = links.replica_cards(["s1"], ME, replicas=[("default", tmp_path / "missing.db")],
                              profile="default", origin_cards=[row], unreadable_out=unreadable)
    assert unreadable == ["default"]
    assert [(c["id"], c["stale"], c["source"]) for c in got] == [("t_scoped", True, "ledger-fallback")]


@pytest.mark.parametrize("threshold, stale", [(None, False), (10, True), (100, False)])
def test_populated_replica_uses_explicit_freshness_and_owner(tmp_path, threshold, stale):
    rep = tmp_path / "scope.db"
    _replica(rep, [("t_scoped", "SCOPED", "running", OTHER)], [("t_scoped", ME, "s1", "created", 0)])
    with sqlite3.connect(rep) as conn:
        conn.execute("CREATE TABLE kanban_sync_state (pulled_at REAL)")
        conn.execute("INSERT INTO kanban_sync_state VALUES (950)")
        conn.execute("UPDATE task_chat_links SET chat_profile = 'research'")
    row = {**_ledger_row("t_scoped", "s1"), "profile": "owner", "origin_profile": "research"}
    got = links.replica_cards(["s1"], ME, replicas=[("default", rep)], profile="research",
                              origin_cards=[row], now=1000, stale_after_s=threshold)
    assert len(got) == 1
    assert (got[0]["title"], got[0]["profile"], got[0]["origin_profile"], got[0]["stale"]) == (
        "SCOPED", "owner", "research", stale)
    assert got[0]["synced_at"] == 950


def test_populated_explicit_replica_graph_never_discovers_second_shadow(tmp_path, monkeypatch):
    rep = tmp_path / 'selected.db'
    _replica(rep, [('t_scoped', 'SCOPED', 'running', OTHER)], [('t_scoped', ME, 's1', 'created', 0)])

    def forbidden(*args, **kwargs):
        pytest.fail('selected replica graph discovered a process-home shadow')

    monkeypatch.setattr('hermes_cli.kanban_sync_client.shadow_path', forbidden)
    got = links.replica_cards(['s1'], ME, replicas=[('default', rep)], profile='default',
                              origin_cards=[_ledger_row('t_scoped', 's1')])
    assert len(got) == 1 and got[0]['title'] == 'SCOPED'


def test_omitted_profile_preserves_legacy_policy_and_ledger(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "sync_config", lambda: calls.append("config") or {})
    monkeypatch.setattr(activity, "_remote_links", lambda: calls.append("ledger") or [])
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: calls.append("profile") or "default")
    assert links.replica_cards(["s1"], ME, replicas=[]) == []
    assert calls == ["profile", "config", "ledger"]
