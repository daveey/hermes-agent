"""Regression contract: observing a program is not owning its dependency graph."""
import json
import sqlite3

from hermes_cli import kanban_session_activity as activity


def replica(tmp_path, monkeypatch, records):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    path = tmp_path / "hub.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
            "INSERT INTO meta VALUES ('gateway', 'macbook');"
            "INSERT INTO meta VALUES ('last_ok', '100');"
            "CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, data TEXT, updated_at INTEGER, version INTEGER);"
        )
        for key, card in records:
            conn.execute("INSERT INTO replica VALUES ('hub', 'tasks', ?, ?, 100000, 1)",
                         (key, json.dumps(card)))
    return path


def observation(task_id, status="running", **extra):
    return {"id": task_id, "status": status, "created_at": 1, "created_by": "u", "sessions": [
        {"gw": "macbook", "profile": "default", "session": "panel", "rel": "shepherd"}
    ], **extra}


def test_shepherd_does_not_claim_dependency_descendants(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [
        ("default/runners/root", observation("root", children=["child"])),
        ("default/runners/child", {"id": "child", "status": "todo", "children": ["grandchild"]}),
        ("default/runners/grandchild", {"id": "grandchild", "status": "running"}),
    ])
    rows = activity._read_remote_links(path, now=100)
    assert [r["id"] for r in rows] == ["root"]


def test_blank_profile_duplicate_counts_only_qualified_card(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [
        ("runners/root", observation("root")),
        ("default/runners/root", observation("root")),
    ])
    rows = activity._read_remote_links(path, now=100)
    assert [r["key"] for r in rows] == ["hub/default/runners/root"]


def test_qualified_terminal_observation_supersedes_unknown_duplicate(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [
        ("runners/root", observation("root", status="unknown")),
        ("default/runners/root", observation("root", status="done")),
    ])
    rows = activity._read_remote_links(path, now=100)
    # One canonical card; the unqualified duplicate is never counted. Without owner
    # attestation the qualified terminal stays unverified (upstream release rule).
    assert len(rows) == 1 and rows[0]["owner_profile"] == "default"


def test_qualified_profiles_are_distinct_cards(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [
        ("default/runners/root", observation("root")),
        ("build/runners/root", observation("root")),
    ])
    rows = activity._read_remote_links(path, now=100)
    assert {r["key"] for r in rows} == {"hub/default/runners/root", "hub/build/runners/root"}


def test_created_child_counts_through_inherited_link(tmp_path, monkeypatch):
    root = observation("root", children=["child"])
    root["sessions"][0]["rel"] = "create-task"
    child = observation("child", children=["unrelated"])
    path = replica(tmp_path, monkeypatch, [
        ("default/runners/root", root),
        ("default/runners/child", child),
        ("default/runners/unrelated", {"id": "unrelated", "status": "todo"}),
    ])
    assert {r["id"] for r in activity._read_remote_links(path, now=100)} == {"root", "child"}


def test_successful_read_migrates_inferred_cache_only(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [("default/runners/root", observation("root"))])
    root = activity._read_remote_links(path, now=100)[0]
    descendant = {**root, "id": "child", "key": "hub/default/runners/child", "via": "descendant"}
    missing_direct = {**root, "id": "missing", "key": "hub/default/runners/missing"}
    monkeypatch.setattr(activity, "_origin_scope", lambda: ("test",))
    monkeypatch.setattr(activity, "_remote_scope", ("test",))
    monkeypatch.setattr(activity, "_remote_last", [root, descendant, missing_direct])
    monkeypatch.setattr(activity, "_snapshot_saved", None)
    monkeypatch.setattr(activity, "_hub_db_path", lambda: path)
    monkeypatch.setattr(activity, "_remote_snapshot", lambda cards=None: cards)
    rows = activity._legacy_remote_links()
    assert {r["key"] for r in rows} == {root["key"], missing_direct["key"]}


def test_terminal_observation_replaces_active_snapshot(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [("default/runners/root", observation("root"))])
    old = activity._read_remote_links(path, now=100)[0]
    monkeypatch.setattr(activity, "_origin_scope", lambda: ("test",))
    monkeypatch.setattr(activity, "_remote_scope", ("test",))
    monkeypatch.setattr(activity, "_remote_last", [old])
    monkeypatch.setattr(activity, "_snapshot_saved", None)
    monkeypatch.setattr(activity, "_hub_db_path", lambda: path)
    monkeypatch.setattr(activity, "_remote_snapshot", lambda cards=None: cards)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE replica SET data=?", (json.dumps(observation("root", "done")),))
    rows = activity._legacy_remote_links()
    assert len(rows) == 1 and rows[0]["reported_status"] == "done"
    assert activity._legacy_remote_links() == rows


def test_cached_proven_alias_is_dropped_once_folded(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [("runners/root", observation("root"))])
    old = activity._read_remote_links(path, now=100)[0]
    monkeypatch.setattr(activity, "_origin_scope", lambda: ("test",))
    monkeypatch.setattr(activity, "_remote_scope", ("test",))
    monkeypatch.setattr(activity, "_remote_last", [old])
    monkeypatch.setattr(activity, "_snapshot_saved", None)
    monkeypatch.setattr(activity, "_hub_db_path", lambda: path)
    monkeypatch.setattr(activity, "_remote_snapshot", lambda cards=None: cards)
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub', 'tasks', 'default/runners/root', ?, 100000, 1)",
                     (json.dumps(observation("root", "running")),))
    assert [r["key"] for r in activity._legacy_remote_links()] == ["hub/default/runners/root"]


def test_unproven_alias_stays_unknown_not_released(tmp_path, monkeypatch):
    path = replica(tmp_path, monkeypatch, [
        ("runners/root", observation("root", created_at=2)),
        ("default/runners/root", observation("root")),
    ])
    rows = activity._read_remote_links(path, now=100)
    assert {r["key"] for r in rows} == {"hub/runners/root", "hub/default/runners/root"}


def test_failed_read_does_not_migrate_cache(tmp_path, monkeypatch):
    old = {"session": "panel", "id": "child", "key": "hub/default/runners/child", "via": "descendant"}
    monkeypatch.setattr(activity, "_origin_scope", lambda: ("test",))
    monkeypatch.setattr(activity, "_remote_scope", ("test",))
    monkeypatch.setattr(activity, "_remote_last", [old])
    monkeypatch.setattr(activity, "_hub_db_path", lambda: None)
    assert activity._legacy_remote_links() == [{**old, "stale": True}]
