"""Durable CI/deploy display clocks, independent of wake deadlines. [daveey]"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_wait_episode as episode


@pytest.fixture
def conn():
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE task_events(id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT, created_at REAL)")
        yield db


def event(conn, kind, at, awaiting=None, **extra):
    payload = {**extra, **({"awaiting": awaiting} if awaiting else {})}
    conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES ('t_fixture', ?, ?, ?)",
                 (kind, json.dumps(payload), at))


@pytest.mark.parametrize("kind", ["ci", "git-ci", "deploy"])
def test_reload_and_same_wait_reschedule_keep_first_stamp(conn, kind):
    a = {"kind": kind, "ref": "https://example.com/pull/123", "gateway": "owner"}
    event(conn, "scheduled", 100, a, wake_at=200)
    event(conn, "continuation_woken", 200)
    event(conn, "claimed", 201)
    event(conn, "continued", 210, {**a, "why": "checks still pending"}, wake_at=900)
    assert episode.read(conn, "t_fixture") == {"kind": "ci" if kind == "git-ci" else kind, "since": 100.0}
    # A fresh read has no process-local/render state.
    assert episode.read(conn, "t_fixture")["since"] == 100


@pytest.mark.parametrize("change", ["ref", "gateway", "kind"])
def test_exact_producer_change_resets(conn, change):
    a = {"kind": "ci", "ref": "PR #123", "gateway": "owner"}
    event(conn, "scheduled", 100, a)
    event(conn, "continued", 200, {**a, change: {"ref": "PR #124", "gateway": "other", "kind": "deploy"}[change]})
    assert episode.read(conn, "t_fixture")["since"] == 200


@pytest.mark.parametrize("boundary", ["completed", "archived", "reopened", "blocked"])
def test_terminal_or_other_wait_ends_episode(conn, boundary):
    a = {"kind": "deploy", "ref": "commit-123"}
    event(conn, "scheduled", 100, a)
    event(conn, boundary, 150)
    event(conn, "scheduled", 200, a)
    assert episode.read(conn, "t_fixture")["since"] == 200


def test_different_intervening_wait_resets_even_if_original_returns(conn):
    a = {"kind": "ci", "ref": "PR #123"}
    event(conn, "scheduled", 100, a)
    event(conn, "scheduled", 150, {"kind": "clock", "why": "cooldown"})
    event(conn, "continued", 200, a)
    assert episode.read(conn, "t_fixture")["since"] == 200


def test_no_timestamp_or_history_is_unknown(conn):
    assert episode.read(conn, "t_fixture") is None
    event(conn, "scheduled", None, {"kind": "ci", "ref": "PR #123"})
    assert episode.read(conn, "t_fixture") == {"kind": "ci", "since": None}


def test_truncated_history_does_not_invent_start(conn):
    for at in range(1, episode.HISTORY_LIMIT + 2):
        event(conn, "scheduled", at, {"kind": "deploy", "ref": "same"})
    assert episode.read(conn, "t_fixture") == {"kind": "deploy", "since": None}


def test_legacy_git_ci_probe_has_exact_identity(conn):
    event(conn, "scheduled", 100, wake_check="git-ci --pr 123")
    event(conn, "scheduled", 200, wake_check="git-ci --pr 123", wake_at=999)
    assert episode.read(conn, "t_fixture") == {"kind": "ci", "since": 100.0}
    event(conn, "scheduled", 300, wake_check="git-ci --pr 124")
    assert episode.read(conn, "t_fixture")["since"] == 300


@pytest.mark.parametrize("label", ["CI: PR #123", "git-ci PR #123", "deploy: PR #123"])
def test_legacy_external_named_producer(conn, label):
    event(conn, "scheduled", 100, {"kind": "external", "ref": label})
    assert episode.read(conn, "t_fixture") == {"kind": "deploy" if label.startswith("deploy") else "ci", "since": 100.0}


@pytest.mark.parametrize("payload", [None, "bad json", "[]", '{"awaiting": {"kind": "ci"}}',
                                    '{"awaiting": {"kind": "ci", "ref": "PR #1", "inferred": true}}',
                                    '{"awaiting": {"kind": "external", "ref": "other"}}'])
def test_unidentified_wait_is_unknown(conn, payload):
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('t_fixture','scheduled',?,100)",
                 (payload,))
    assert episode.read(conn, "t_fixture") is None


@pytest.mark.parametrize("stamp", [None, -1, 0, "bad", float("inf")])
def test_invalid_start_is_unknown(conn, stamp):
    event(conn, "scheduled", stamp, {"kind": "ci", "ref": "PR #1"})
    assert episode.read(conn, "t_fixture") == {"kind": "ci", "since": None}


def test_missing_table_and_terminal_wait(conn):
    event(conn, "scheduled", 100, {"kind": "ci", "ref": "PR #1"})
    event(conn, "completed", 200)
    assert episode.read(conn, "t_fixture") is None
    conn.execute("DROP TABLE task_events")
    assert episode.read(conn, "t_fixture") is None


def test_manual_status_exit_ends_the_episode(conn):
    a = {"kind": "ci", "ref": "PR #1"}
    event(conn, "scheduled", 100, a)
    conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES ('t_fixture','status',?,150)",
                 (json.dumps({"from": "scheduled", "to": "todo"}),))
    event(conn, "scheduled", 200, a)
    assert episode.read(conn, "t_fixture") == {"kind": "ci", "since": 200.0}


def test_millisecond_stamp_is_normalized(conn):
    event(conn, "scheduled", 1795250000000, {"kind": "deploy", "ref": "sha"})
    assert episode.read(conn, "t_fixture") == {"kind": "deploy", "since": 1795250000}


def test_dashboard_task_detail_exposes_real_scheduled_episode(tmp_path, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("wait_episode_api_fixture", path)
    api = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, api)
    spec.loader.exec_module(api)
    a = {"kind": "ci", "ref": "https://example.com/pull/123"}
    with kbc.connect_closing() as db:
        tid = kb.create_task(db, title="CI wait fixture", assignee="alice")
        assert kb.schedule_task(db, tid, reason="CI pending", awaiting=a, wake_check=f"test -f {home}/missing")
        first = episode.read(db, tid)
        view = api._task_detail(db, kb.get_task(db, tid), None, None)
        assert view["task"]["wait_episode"] == first
        assert first["kind"] == "ci" and first["since"] > 0
        # A new reader sees the same persisted start, not time since mount.
    with kbc.connect_closing() as db:
        assert api._task_detail(db, kb.get_task(db, tid), None, None)["task"]["wait_episode"] == first
