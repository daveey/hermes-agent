"""Quoted names/evidence are not execution requirements on either creation surface."""
import json
import re
import shlex

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as placement


@pytest.mark.parametrize("frontdoor", ["cli", "tool"])
@pytest.mark.parametrize("title,body", [
    ("Repair `macOS CI gate`", "Fix the workflow"),
    ("Fix `babybear`", "Review the repository"),
    ("Fix tests", "Name: `on the hub`"),
    ("Fix tests", "Evidence:\n```\nkeychain on the hub\n```"),
    ("Fix tests", "<!-- placement:evidence -->keychain on the hub<!-- /placement:evidence -->"),
])
def test_quoted_names_enroll_on_both_frontdoors(tmp_path, monkeypatch, frontdoor, title, body):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(placement, "config", lambda: {"worker_pools": {"hub": ["worker-1"]}})
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: "hub")
    kb.init_db()
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(home, "20261123_120000_quoted_owner"))
    if frontdoor == "cli":
        from hermes_cli import kanban as cli
        result = cli.run_slash(f"create {shlex.quote(title)} --body {shlex.quote(body)}")
        tid = re.search(r"(t_[0-9a-f]+)", result).group(1)
    else:
        from tools import kanban_tools as tool
        result = json.loads(tool._handle_create({"title": title, "body": body, "requires": []}))
        assert result.get("ok"), result
        tid = result["task_id"]
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).placement_policy == "worker-first-v1"
        created = next(e for e in kb.list_events(conn, tid) if e.kind == "created")
        assert created.payload["portable_reason"] is None


@pytest.mark.parametrize("body", [
    "Use keychain for `macOS CI gate`", "Read `name` on the hub", "Use `keychain", "Use ``keychain`",
    "<!-- placement:evidence -->keychain", "Use keychain <!-- /placement:evidence -->",
])
def test_real_work_and_incomplete_quotes_still_pin(body):
    assert placement.portable_reason({"title": "Fix tests", "body": body}, {}) is not None


@pytest.mark.parametrize("route", ["pool", "legacy"])
def test_backticked_names_are_not_markers_in_routed_creation(route):
    cfg = {"worker_pools": {"origin": ["worker-1"]}} if route == "pool" else {"route": {"origin": "remote"}}
    result = placement.decide({"title": "Fix `macOS CI gate`", "body": "Fix `keychain` job"},
                              cfg=cfg, self_gw="origin", live=lambda _: None)
    assert result.get("placement_policy") == "worker-first-v1" if route == "pool" else result["where"] == "remote"
