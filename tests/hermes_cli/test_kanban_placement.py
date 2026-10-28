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
ALIVE = lambda target: None
NOWHERE = lambda need: {"need": need, "local": "absent", "holders": [], "stale": []}


def decide(args, **kw):
    kw.setdefault("cfg", ROUTE)
    kw.setdefault("self_gw", "macbook")
    kw.setdefault("live", ALIVE)
    kw.setdefault("locate", NOWHERE)
    kw.setdefault("carries", False)  # a hermes-hub client without create-task --option (< 1.8.0)
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


@pytest.mark.parametrize("extra", [
    {"model": "claude-opus-5-5", "provider": "anthropic"},
    {"goal_mode": True, "goal_max_turns": 5},
    {"triage": True},
    {"completion_contract": "daveey/hermes"},
    {"skills": ["x"], "max_runtime_seconds": 3600, "priority": 2},
])
def test_options_a_new_client_carries_go_to_the_target(extra):
    assert decide({"title": "Fix the flaky test", **extra}, carries=True)["where"] == "hub"


@pytest.mark.parametrize("extra,why", [
    ({"initial_status": "blocked"}, "initial_status"),
    ({"human_request": {"action": "x"}}, "human_request"),
    ({"tenant": "acme"}, "tenant"),
    ({"goal_mode": True, "tenant": "acme"}, "tenant"),
])
def test_options_no_client_carries_stay_local(extra, why):
    d = decide({"title": "Fix the flaky test", **extra}, carries=True)
    assert d["where"] == "local" and why in d["why"], d


def test_decide_probes_the_client_only_when_a_legacy_option_is_used(monkeypatch):
    seen = []
    monkeypatch.setattr(kp, "hub_carries_options", lambda cli: seen.append(cli) or True)
    assert decide({"title": "Fix the flaky test"}, carries=None)["where"] == "hub"
    assert seen == []
    assert decide({"title": "Fix the flaky test", "goal_mode": True}, carries=None)["where"] == "hub"
    assert len(seen) == 1


def test_carried_options_are_json_typed_and_skip_defaults():
    got = kp.carried_options({
        "title": "t", "skills": ["x", ""], "goal_mode": "true", "triage": False, "priority": 0,
        "max_runtime_seconds": "900", "goal_max_turns": 3, "model": "m", "provider": "p",
        "completion_contract": "local-only", "idempotency_key": "k", "requires": "ssh:mini",
        "initial_status": "blocked"})
    assert got == {"skills": ["x"], "goal_mode": True, "max_runtime_seconds": 900, "goal_max_turns": 3,
                   "model": "m", "provider": "p", "requires": ["ssh:mini"]}
    assert kp.carried_options({"title": "t"}) == {}
    with pytest.raises(ValueError):
        kp.carried_options({"max_runtime_seconds": "2h"})


def test_hub_carries_options_reads_create_task_help_and_caches(monkeypatch):
    calls = []
    out = {"text": "usage: ... [--id ID] [--option KEY=JSON]"}

    def runner(argv):
        calls.append(argv)
        return 0, out["text"], ""
    monkeypatch.setattr(kp, "hub_runner", runner)
    monkeypatch.setattr(kp, "_help_cache", {})
    assert kp.hub_carries_options("/x/hermes-hub") is True
    assert calls == [["/x/hermes-hub", "create-task", "--help"]]
    out["text"] = "usage: ... [--id ID]"
    assert kp.hub_carries_options("/x/hermes-hub") is True and len(calls) == 1  # cached
    assert kp.hub_carries_options("/y/hermes-hub") is False
    monkeypatch.setattr(kp, "hub_runner", lambda argv: (127, "", "not found"))
    assert kp.hub_carries_options("/z/hermes-hub") is False


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
    # ...but never the structural limits: a pinned gateway that cannot be honoured is refused, not local.
    assert decide({"title": "x", "placement": "hub", "parents": ["t_1"]})["where"] == "refuse"
    # A gateway with no route is not a destination, and never silently becomes a local card.
    assert decide({"title": "x", "placement": "mini"})["where"] == "refuse"
    assert decide({"title": "x", "placement": "mini"}, cfg={})["where"] == "refuse"
    # Own gateway name and 'here' still mean local, with or without a route.
    assert decide({"title": "x", "placement": "macbook"})["where"] == "local"
    assert decide({"title": "x", "placement": "macbook"}, cfg={})["where"] == "local"
    assert decide({"title": "x", "placement": "local"}, cfg={})["where"] == "local"


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
    result = {"rc": 0, "help": "usage: hermes-hub create-task [--id ID]"}  # pre-1.8.0 client by default

    def apply(rid=None):
        rows = [rid] if rid else [r[0] for r in hub.execute("SELECT id FROM sent WHERE state != 'done'")]
        for r in rows:
            hub.execute("UPDATE sent SET state='done', result=? WHERE id=?",
                        (json.dumps({"ok": True, "task_id": "t_hub0001", "status": "ready"}), r))
        hub.commit()

    def fake_hub(argv):
        if argv[1:] == ["create-task", "--help"]:
            return 0, result["help"], ""
        if argv[1:] == ["--help"]:
            return 0, result.get("top_help", "{sync,status,requests,create-task}"), ""
        if argv[1] in ("sync", "outcomes"):
            refreshes.append(argv[1:])
            if result.get("apply_on") == argv[1]:
                apply()
            if result.get("apply_on") == "remote_waiting" and argv[1] == "outcomes":
                # hermes-hub 1.9.1: the owner made the card; the request waits on our binding acceptance.
                rid = argv[argv.index("--id") + 1]
                return 0, json.dumps({"id": rid, "state": "queued", "result": None, "remote_state": "waiting",
                                      "remote_result": {"ok": True, "task_id": "t_hub0002", "status": "ready",
                                                        "handoff_complete": False}}), ""
            return 0, "", ""
        calls.append(argv)
        rid = argv[argv.index("--id") + 1]
        if result["rc"] in (0, 3):
            hub.execute("INSERT OR IGNORE INTO sent (id, body, state, created_at) VALUES (?, ?, 'sent', ?)",
                        (rid, json.dumps({"id": rid, "to_gw": argv[2]}), time.time()))
            hub.commit()
            if result.get("apply"):
                apply(rid)
        return result["rc"], rid, "" if result["rc"] == 0 else "boom"

    def fake_spawn(argv):
        spawned.append(argv[1:])
        if result.get("apply_on") == "spawn":
            apply()

    refreshes: list[list[str]] = []
    spawned: list[list[str]] = []
    monkeypatch.setattr(kp, "hub_runner", fake_hub)
    monkeypatch.setattr(kp, "hub_spawn", fake_spawn, raising=False)
    monkeypatch.setattr(kp, "_help_cache", {})
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 0})
    kb.init_db()
    yield {"hub": hub, "calls": calls, "result": result, "refreshes": refreshes, "spawned": spawned}
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


NEW_HELP = "usage: hermes-hub create-task [--id ID] [--option KEY=JSON]"


def _options(argv) -> dict:
    out = {}
    for i, a in enumerate(argv):
        if a == "--option":
            k, _, v = argv[i + 1].partition("=")
            out[k] = json.loads(v)
    return out


def test_new_client_carries_skills_goal_mode_and_pins_as_fields(mac):
    """MacBook drill shape: skills=['x'] + goal_mode=true lands on the hub with both fields set."""
    from tools import kanban_tools as kt
    mac["result"]["help"] = NEW_HELP
    out = json.loads(kt._handle_create({
        "title": "Regenerate the plugin index", "body": "Do it.", "skills": ["x"], "goal_mode": True,
        "goal_max_turns": 6, "model": "claude-opus-5-5", "provider": "anthropic", "max_runtime_seconds": 1800,
        "priority": 3, "session_id": "20261027_183000_abc123"}))
    assert out["ok"] and out["placed"]["gateway"] == "hub", out
    assert _local_count() == 0
    argv = mac["calls"][0]
    assert _options(argv) == {"skills": ["x"], "goal_mode": True, "goal_max_turns": 6, "model": "claude-opus-5-5",
                              "provider": "anthropic", "max_runtime_seconds": 1800, "priority": 3}
    body = argv[argv.index("--body") + 1]
    assert "Skills to load first" not in body and "Runtime budget" not in body and "needs=[" in body


def test_old_client_keeps_a_goal_card_local_and_skills_in_the_body(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "goal_mode": True}))
    assert out["ok"] and "placed" not in out and mac["calls"] == [] and _local_count() == 1
    out = json.loads(kt._handle_create({"title": "Regenerate the docs index", "skills": ["x"],
                                        "max_runtime_seconds": 600}))
    argv = mac["calls"][0]
    assert out["placed"]["gateway"] == "hub" and "--option" not in argv
    body = argv[argv.index("--body") + 1]
    assert "Skills to load first: x." in body and "Runtime budget asked by the creator: 600 s." in body


def test_kanban_cli_create_takes_reasoning_effort(mac):
    """hermes-hub maps a plain create's reasoning_effort onto `hermes kanban create --reasoning-effort`."""
    import argparse
    from hermes_cli import kanban as kcli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    root = argparse.ArgumentParser()
    kcli.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", "plain card", "--reasoning-effort", "high", "--json"])
    assert kcli._cmd_create(args) == 0
    with kbc.connect_closing() as conn:
        assert [t.reasoning_effort for t in kb.list_tasks(conn)] == ["high"]


def test_hub_task_id_is_returned_once_the_hub_applies_it(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 2})
    mac["result"]["apply"] = True
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "session_id": "20261027_183000_abc123"}))
    assert out["task_id"] == "t_hub0001" and out["placed"]["task_id"] == "t_hub0001", out


def test_task_id_comes_back_once_the_outcome_fetch_sees_the_hub_apply_it(mac, monkeypatch):
    """10-27 drill: the request was applied on the hub, but its result reached the MacBook only on a
    later sync, so kanban_create returned task_id null and the chat created a second card."""
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 5})
    mac["result"].update(top_help="{sync,requests,create-task,outcomes}", apply_on="outcomes")
    out = json.loads(kt._handle_create({"title": "Placement drill", "session_id": "20261027_183000_abc123"}))
    assert out["ok"] and out["task_id"] == "t_hub0001" and out["status"] == "ready", out
    assert out["placed"]["task_id"] == "t_hub0001" and "IS created" not in out["note"]
    assert mac["refreshes"][0][0] == "outcomes" and mac["spawned"] == []


def test_task_id_comes_back_while_the_origin_binding_is_still_waiting(mac, monkeypatch):
    """Live shape: the hub made the card within seconds, but the request stays 'waiting' on the hub
    until the MacBook's next full sync accepts the binding (~1-4 min on 10-27)."""
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 5})
    mac["result"].update(top_help="{sync,requests,create-task,outcomes}", apply_on="remote_waiting")
    out = json.loads(kt._handle_create({"title": "Placement drill", "session_id": "20261027_183000_abc123"}))
    assert out["task_id"] == "t_hub0002" and out["status"] == "ready", out
    rid = out["placed"]["request_id"]
    assert mac["refreshes"][0] == ["outcomes", "--id", rid, "--wait", mac["refreshes"][0][-1]]


def test_old_client_gets_one_detached_sync_and_the_task_id(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 5})
    mac["result"]["apply_on"] = "spawn"
    out = json.loads(kt._handle_create({"title": "Placement drill", "session_id": "20261027_183000_abc123"}))
    assert out["task_id"] == "t_hub0001", out
    assert mac["spawned"] == [["sync", "--quiet"]] and mac["refreshes"] == []


def test_still_queued_after_the_wait_says_the_card_is_created_and_not_to_call_again(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 1})
    out = json.loads(kt._handle_create({"title": "Placement drill", "session_id": "20261027_183000_abc123"}))
    assert out["ok"] is True and out["task_id"] is None and out["status"] == "queued", out
    note = out["note"]
    assert "The card IS created" in note and out["placed"]["request_id"] in note
    assert "Do NOT call kanban_create again" in note and "duplicate" in note
    assert len(mac["spawned"]) == 1  # one bounded sync, never one per poll


def test_identical_retry_returns_the_same_request_and_sends_no_second_card(mac):
    from tools import kanban_tools as kt
    args = {"title": "Placement drill", "body": "Run hostname.", "session_id": "20261027_183000_abc123"}
    first = json.loads(kt._handle_create(dict(args)))
    again = json.loads(kt._handle_create(dict(args)))
    assert len(mac["calls"]) == 1, mac["calls"]  # create-task ran once
    assert again["placed"]["request_id"] == first["placed"]["request_id"]
    assert again["placed"]["retry"] is True and first["placed"]["retry"] is False
    assert "already made" in again["note"] and _local_count() == 0
    mac["result"]["apply"] = False
    mac["hub"].execute("UPDATE sent SET state='done', result=? WHERE id=?",
                       (json.dumps({"ok": True, "task_id": "t_hub0001"}), first["placed"]["request_id"]))
    mac["hub"].commit()
    assert json.loads(kt._handle_create(dict(args)))["task_id"] == "t_hub0001"
    assert len(mac["calls"]) == 1


def test_remote_task_id_reads_a_plain_create_output(mac):
    """An unbound create's result is the owner's truncated `kanban create --json` text."""
    mac["hub"].execute("INSERT INTO sent (id, body, state, result) VALUES ('placement:macbook:x1', '{}', 'done', ?)",
                       (json.dumps({"output": '{\n  "id": "t_01a180ef6421a3f0",\n  "title": "Shrink\u2026'}),))
    mac["hub"].commit()
    assert kp.remote_task_id("placement:macbook:x1") == "t_01a180ef6421a3f0"


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


@pytest.mark.parametrize("pin", ["mini", "macbook-typo", "hub-typo"])
def test_pin_to_an_unrouted_gateway_is_refused_not_created_locally(mac, pin):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Run the MacBook-only drill", "placement": pin}))
    assert not out.get("ok"), out
    assert out["code"] == "placement_unavailable" and out["created"] is False and out["placement"] == pin
    assert "placement='local'" in out["error"]
    assert mac["calls"] == [] and _local_count() == 0


def test_pin_to_a_gateway_when_no_route_is_configured_is_refused(mac, monkeypatch):
    # The reported case: a hub worker (no route on the hub) pinned placement='macbook'.
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", dict)
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    out = json.loads(kt._handle_create({"title": "MacBook-only drill", "placement": "macbook"}))
    assert not out.get("ok") and out["code"] == "placement_unavailable", out
    assert _local_count() == 0


def test_pin_to_the_routed_target_that_the_hub_refuses_is_an_error_not_local(mac):
    from tools import kanban_tools as kt
    mac["result"]["rc"] = 2
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hub"}))
    assert not out.get("ok") and out["code"] == "placement_unavailable", out
    assert _local_count() == 0


@pytest.mark.parametrize("pin", ["local", "here", "macbook"])
def test_pin_local_or_own_gateway_still_creates_locally(mac, pin):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": pin}))
    assert out["ok"] and out["task_id"] and "placed" not in out
    assert mac["calls"] == [] and _local_count() == 1


def test_own_gateway_pin_creates_locally_with_no_route_configured(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", dict)
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    out = json.loads(kt._handle_create({"title": "Hub drill", "placement": "hub"}))
    assert out["ok"] and out["task_id"] and _local_count() == 1


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
    # Options join the key only when present, so a card without them keeps its pre-1.8.0 id.
    assert kp.request_id("macbook", {"title": "t", "body": "b"}, "s1", {}) == a
    assert kp.request_id("macbook", {"title": "t", "body": "b"}, "s1", {"goal_mode": True}) != a


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
    monkeypatch.setattr(reg, "_config", dict)
    assert kcr._hub_cli() == str(exe)


def test_remote_body_names_the_capability_to_route_back_with():
    body = kp.remote_body({"body": "Do it."}, self_gw="macbook", target="hub")
    assert "needs=['host:macbook']" in body and reg.is_gateway_need("host:macbook")


def test_pin_with_an_origin_session_is_refused_not_created_locally(mac):
    # An origin-bound card is created on this gateway by design; a pin elsewhere cannot be honoured.
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "MacBook-only drill", "placement": "mini",
                                        "origin_session": "hub:abc123"}))
    assert not out.get("ok") and out["code"] == "placement_unavailable", out
    assert _local_count() == 0


def test_pin_with_a_structural_limit_is_refused_through_the_tool(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "x", "placement": "hub", "workspace_kind": "dir",
                                        "workspace_path": "/tmp/x"}))
    assert not out.get("ok") and out["code"] == "placement_unavailable", out
    assert _local_count() == 0 and mac["calls"] == []


def test_pin_to_a_target_that_is_not_live_is_refused_through_the_tool(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "target_live", lambda *a, **k: "hub's capability record is stale or missing")
    out = json.loads(kt._handle_create({"title": "x", "placement": "hub"}))
    assert not out.get("ok") and out["code"] == "placement_unavailable", out
    assert _local_count() == 0


def test_pin_is_case_insensitive_for_the_own_gateway(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "x", "placement": "MacBook"}))
    assert out["ok"] and out["task_id"] and _local_count() == 1


def test_exception_while_placing_a_pinned_card_is_refused(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "create_remote", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = json.loads(kt._handle_create({"title": "x", "placement": "hub"}))
    assert not out.get("ok") and out["code"] == "placement_unavailable", out
    assert _local_count() == 0


def _identity_raises():
    raise RuntimeError("identity lookup failed")


def test_decide_refuses_explicit_pin_when_identity_lookup_raises(monkeypatch):
    """A raise before pin checking must not turn an explicit pin into a local card."""
    monkeypatch.setattr(reg, "self_gateway", _identity_raises)
    refused = kp.decide({"title": "x", "placement": "mini"}, cfg={})
    assert refused["where"] == "refuse" and refused["pin"] == "mini" and "failed" in refused["why"], refused
    # The fixture's own name is not "known" when the lookup itself raised.
    assert kp.decide({"title": "x", "placement": "MacBook"}, cfg={})["where"] == "refuse"
    for pin in (None, "local", "here", "auto", "LOCAL", " Here "):
        args = {"title": "x"} if pin is None else {"title": "x", "placement": pin}
        assert kp.decide(args, cfg={})["where"] == "local", pin
    # Courier creation is local by contract; the tool layer refuses an origin-bound pin itself.
    courier = kp.decide({"title": "x", "placement": "mini"}, cfg={}, origin_intent={"request_id": "r"})
    assert courier["where"] == "local", courier


def test_decide_keeps_a_known_own_gateway_local_when_a_later_step_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("pin check failed")
    monkeypatch.setattr(kp, "explicit_foreign_pin", boom)
    own = kp.decide({"title": "x", "placement": "Hub"}, self_gw="hub", cfg={})
    assert own["where"] == "local", own
    foreign = kp.decide({"title": "x", "placement": "mini"}, self_gw="hub", cfg={})
    assert foreign["where"] == "refuse" and foreign["pin"] == "mini", foreign


@pytest.mark.parametrize("pin", ["mini", "macbook", "MacBook"])
def test_identity_lookup_failure_refuses_explicit_pin_and_creates_no_row(mac, monkeypatch, pin):
    """Tool-level: self_gateway raising must not write a local row for an explicit pin."""
    from tools import kanban_tools as kt
    monkeypatch.setattr(reg, "self_gateway", _identity_raises)
    out = json.loads(kt._handle_create({"title": "MacBook-only drill", "placement": pin}))
    assert not out.get("ok"), out
    assert out["code"] == "placement_unavailable" and out["created"] is False
    assert out["placement"] == pin.strip().lower()
    assert "placement='local'" in out["error"]
    assert mac["calls"] == [] and _local_count() == 0


@pytest.mark.parametrize("pin", ["local", "here", "LOCAL", " Here "])
def test_identity_lookup_failure_still_creates_local_and_here(mac, monkeypatch, pin):
    from tools import kanban_tools as kt
    monkeypatch.setattr(reg, "self_gateway", _identity_raises)
    out = json.loads(kt._handle_create({"title": "stay here", "placement": pin}))
    assert out["ok"] and out["task_id"] and "placed" not in out, out
    assert mac["calls"] == [] and _local_count() == 1


def test_identity_lookup_failure_without_a_pin_still_creates_locally(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(reg, "self_gateway", _identity_raises)
    out = json.loads(kt._handle_create({"title": "unpinned card"}))
    assert out["ok"] and out["task_id"] and "placed" not in out, out
    assert _local_count() == 1
