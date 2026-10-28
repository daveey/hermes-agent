"""Default Kanban placement: a MacBook card with no local-only need runs on hermes-hub. [daveey]

On 2026-10-27 the MacBook (16 cores) ran 11-14 default-board workers at once and its load average
peaked at 430. Of 455 MacBook cards sampled over the week, ~45% touched nothing MacBook-only, yet
every one ran there. ``kanban.placement.route`` sends such cards to the hub through the origin-bound
``hermes-hub create-task`` path, so they still show in the chat that asked for them.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_capability_registry as reg
from hermes_cli import kanban_placement as kp

ROUTE = {"route": {"macbook": "hub"}, "creator_session": {"hub": "20261023_052109_5b3dee"}}
ALIVE = lambda target: None  # noqa: E731
NOWHERE = lambda need: {"need": need, "local": "absent", "holders": [], "stale": []}  # noqa: E731


def decide(args, **kw):
    kw.setdefault("cfg", ROUTE)
    kw.setdefault("self_gw", "macbook")
    kw.setdefault("live", ALIVE)
    kw.setdefault("locate", NOWHERE)
    return kp.decide(args, **kw)


# --- the placement rule -------------------------------------------------------------------------

def test_card_with_no_local_need_goes_to_the_target():
    d = decide({"title": "daveey/hermes CI: ruff check fails with 11 errors on main",
                "body": "Fix the lint errors on main and push.", "assignee": "default"})
    assert d["where"] == "hub", d


def test_no_route_is_upstream_behaviour():
    assert decide({"title": "anything"}, cfg={})["where"] == "local"
    # A route for another gateway does not apply here.
    assert decide({"title": "anything"}, self_gw="mini")["where"] == "local"


@pytest.mark.parametrize("extra,why", [
    ({"parents": ["t_1"]}, "parents"),
    ({"workspace_kind": "worktree", "workspace_path": "/Users/me/code/x"}, "worktree"),
    ({"workspace_kind": "dir", "workspace_path": "/tmp/x"}, "dir"),
    ({"board": "links"}, "board"),
    ({"assignee": "links"}, "assignee"),
    ({"model": "claude-opus-5-5", "provider": "anthropic"}, "model"),
    ({"goal_mode": True}, "goal_mode"),
    ({"triage": True}, "triage"),
    ({"initial_status": "blocked"}, "initial_status"),
    ({"project_id": "p_1"}, "project_id"),
    ({"completion_contract": "daveey/hermes"}, "completion_contract"),
    ({"requires": ["terminal"]}, "profile tools"),
])
def test_cards_hub_create_cannot_express_stay_local(extra, why):
    d = decide({"title": "Fix the flaky test", **extra})
    assert d["where"] == "local" and why in d["why"], d


@pytest.mark.parametrize("text", [
    "Sign in to X in the agent Chrome and buy credits",
    "Read the token from the login Keychain",
    "TCC prompt for Hermes Python.app keeps coming back",
    "Reload the launchd job com.daveey.link-inbox",
    "Hermes Desktop sidebar shows red on idle sessions",
    "MacBook overloaded: find the drivers",
    "Restart Baby Bear on the mini",
    "aws s3 ls --profile softmax-org",
    "Export the album from Apple Photos",
])
def test_local_markers_keep_mac_work_on_the_mac(text):
    d = decide({"title": text})
    assert d["where"] == "local" and d["why"].startswith("local marker"), d


def test_local_skill_pins_the_card():
    d = decide({"title": "Weekly digest", "skills": ["apple-notes"]})
    assert d["where"] == "local" and "skill apple-notes" in d["why"], d


def test_configured_markers_replace_and_extend_the_builtin_list():
    cfg = {**ROUTE, "local_markers": [r"\bfoo\b"]}
    assert decide({"title": "Fix Chrome"}, cfg=cfg)["where"] == "hub"
    assert decide({"title": "Fix foo"}, cfg=cfg)["where"] == "local"
    cfg = {**ROUTE, "extra_local_markers": [r"\bshoo\b"]}
    assert decide({"title": "shoo app store build"}, cfg=cfg)["where"] == "local"


def test_caller_pin_wins_both_ways():
    assert decide({"title": "CI fix", "placement": "local"})["where"] == "local"
    # An explicit pin to the target skips the marker heuristic (the caller knows).
    assert decide({"title": "Chrome-free MacBook docs", "placement": "hub"})["where"] == "hub"
    # ...but never the structural limits.
    assert decide({"title": "x", "placement": "hub", "parents": ["t_1"]})["where"] == "local"
    # A gateway with no route is not a destination.
    assert decide({"title": "x", "placement": "mini"})["where"] == "local"


def test_courier_creation_is_never_re_placed():
    assert decide({"title": "x"}, origin_intent={"request_id": "r"})["where"] == "local"


def test_declared_need_held_here_but_not_on_target_stays_local():
    def only_here(need):
        return {"need": need, "local": "fresh", "holders": [], "stale": []}
    d = decide({"title": "Grant the scoped IAM change", "requires": ["aws:767406518141:admin"]}, locate=only_here)
    assert d["where"] == "local" and "aws:767406518141:admin" in d["why"], d


def test_declared_need_the_target_holds_goes_there():
    def on_hub(need):
        return {"need": need, "local": "absent", "holders": [("hub", time.time())], "stale": []}
    assert decide({"title": "Run the runner drill", "requires": ["sudo:nopasswd"]}, locate=on_hub)["where"] == "hub"


def test_target_not_live_falls_back_to_local():
    d = decide({"title": "CI fix"}, live=lambda t: "hub's capability record is stale or missing")
    assert d["where"] == "local" and "not live" in d["why"], d


def test_decide_never_raises():
    def boom(_):
        raise RuntimeError("replica gone")
    d = decide({"title": "x", "requires": ["ssh:hub"]}, locate=boom)
    assert d["where"] == "local" and "failed" in d["why"]


# --- liveness -----------------------------------------------------------------------------------

def test_target_live_needs_a_recent_sync_and_a_fresh_target_record():
    now = 1_000_000.0
    fresh = {"hub": {"refreshed_at": now - 60}}
    assert kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote=fresh) is None
    assert "sync" in kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 3600}, remote=fresh)
    stale = {"hub": {"refreshed_at": now - 7200}}
    assert "stale" in kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote=stale)
    assert "missing" in kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote={})


# --- kanban_create end to end -------------------------------------------------------------------

HUB_SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,
  updated_at INTEGER, PRIMARY KEY (gw, coll, key));
CREATE TABLE sent (id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, created_at REAL,
  done_at REAL, posted INTEGER DEFAULT 0);
"""


@pytest.fixture
def mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hermes_cli import kanban_db as kb
    from hermes_state import SessionDB

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    mail = tmp_path / "mail.json"
    mail.write_text(json.dumps({"gateway": "macbook"}))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(mail))
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    hub = sqlite3.connect(state / "hub.db")
    hub.executescript(HUB_SCHEMA)
    hub.execute("INSERT INTO meta VALUES ('gateway', 'macbook')")
    hub.execute("INSERT INTO meta VALUES ('last_ok', ?)", (str(time.time()),))
    rec = {"schema": 1, "gateway": "hub", "refreshed_at": time.time(),
           "capabilities": [{"name": "sudo:nopasswd", "ok": True, "checked_at": time.time()}]}
    hub.execute("INSERT INTO replica VALUES ('hub', 'capabilities', 'info', 1, 1, ?, ?)",
                (json.dumps(rec), int(time.time())))
    hub.commit()
    sdb = SessionDB(db_path=home / "state.db")
    sdb.create_session("20261027_183000_abc123", source="desktop")
    sdb.close()
    calls: list[list[str]] = []
    result = {"rc": 0}

    def fake_hub(argv):
        calls.append(argv)
        rid = argv[argv.index("--id") + 1]
        if result["rc"] in (0, 3):
            hub.execute("INSERT OR IGNORE INTO sent (id, body, state, created_at) VALUES (?, ?, 'sent', ?)",
                        (rid, json.dumps({"id": rid, "to_gw": argv[2]}), time.time()))
            if result.get("apply"):
                hub.execute("UPDATE sent SET state='done', result=? WHERE id=?",
                            (json.dumps({"ok": True, "task_id": "t_hub0001"}), rid))
            hub.commit()
        return result["rc"], rid, "" if result["rc"] == 0 else "boom"

    monkeypatch.setattr(kp, "hub_runner", fake_hub)
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 0})
    kb.init_db()
    yield {"hub": hub, "calls": calls, "result": result}
    hub.close()


def _local_count() -> int:
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def test_chat_card_is_created_on_the_hub_bound_to_its_chat(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({
        "title": "daveey/hermes CI: ruff check fails on main", "body": "Fix it and push.",
        "skills": ["github-pr-workflow"], "session_id": "20261027_183000_abc123"}))
    assert out["ok"], out
    assert out["placed"]["gateway"] == "hub" and out["placed"]["origin_session"] == "20261027_183000_abc123"
    assert _local_count() == 0
    argv = mac["calls"][0]
    assert argv[1:3] == ["create-task", "hub"]
    assert argv[argv.index("--origin-session") + 1] == "20261027_183000_abc123"
    assert argv[argv.index("--creator-session") + 1] == "20261023_052109_5b3dee"
    assert argv[argv.index("--assignee") + 1] == "default"
    body = argv[argv.index("--body") + 1]
    assert body.startswith("Fix it and push.")
    assert "Skills to load first: github-pr-workflow." in body and "needs=[" in body


def test_hub_task_id_is_returned_once_the_hub_applies_it(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 2})
    mac["result"]["apply"] = True
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "session_id": "20261027_183000_abc123"}))
    assert out["task_id"] == "t_hub0001" and out["placed"]["task_id"] == "t_hub0001", out


def test_unpersisted_session_creates_an_unbound_hub_card(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "session_id": "nope"}))
    argv = mac["calls"][0]
    assert out["placed"]["origin_session"] is None
    assert argv[argv.index("--origin-session") + 1] == "" and "--creator-session" not in argv


def test_mac_card_stays_local_and_never_calls_the_hub(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Fix the Hermes Desktop sidebar stripe"}))
    assert out["ok"] and out["task_id"] and "placed" not in out
    assert mac["calls"] == [] and _local_count() == 1


def test_hub_refusal_falls_back_to_a_local_card(mac):
    from tools import kanban_tools as kt
    mac["result"]["rc"] = 2
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index"}))
    assert out["ok"] and out["task_id"] and "placed" not in out
    assert len(mac["calls"]) == 1 and _local_count() == 1


def test_offline_hub_is_durably_queued_not_duplicated_locally(mac):
    from tools import kanban_tools as kt
    mac["result"]["rc"] = 3
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index"}))
    assert out["placed"]["queued_only"] is True and _local_count() == 0


def test_retry_reuses_the_request_id():
    a = kp.request_id("macbook", {"title": "t", "body": "b"}, "s1")
    assert a == kp.request_id("macbook", {"title": "t", "body": "b"}, "s1")
    assert a != kp.request_id("macbook", {"title": "t", "body": "b"}, "s2")
    assert kp.request_id("macbook", {"title": "t", "idempotency_key": "k"}, "s1") == \
        kp.request_id("macbook", {"title": "other", "idempotency_key": "k"}, "s1")
    assert len(a) <= 128 and reg.is_gateway_need("x:y")  # ids fit hermes-hub's alphabet/length


def test_hub_cli_resolves_without_the_login_shell_path(tmp_path, monkeypatch):
    """Gateway/worker processes have no ~/.local/bin on PATH: a bare 'hermes-hub' raised ENOENT."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert reg.hub_cli_path() == "hermes-hub"  # nothing installed: unchanged (the caller's error names it)
    exe = tmp_path / ".local" / "bin" / "hermes-hub"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert reg.hub_cli_path() == str(exe)
    assert reg.hub_cli_path(str(tmp_path / "custom")) == str(tmp_path / "custom")  # configured wins
    from hermes_cli import kanban_capability_routing as kcr
    monkeypatch.setattr(reg, "_config", lambda: {})
    assert kcr._hub_cli() == str(exe)


def test_remote_body_names_the_capability_to_route_back_with():
    body = kp.remote_body({"body": "Do it."}, self_gw="macbook", target="hub")
    assert "needs=['host:macbook']" in body and reg.is_gateway_need("host:macbook")
