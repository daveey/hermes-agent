"""Default Kanban placement: a MacBook card with no local-only need runs on hermes-hub. [daveey]

On 2026-10-27 the MacBook (16 cores) ran 11-14 default-board workers at once and its load average
peaked at 430. Of 455 MacBook cards sampled over the week, ~45% touched nothing MacBook-only, yet
every one ran there. ``kanban.placement.route`` sends such cards to the hub through the origin-bound
``hermes-hub create-task`` path, so they still show in the chat that asked for them.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_capability_registry as reg
from hermes_cli import kanban_placement as kp
from tests.hermes_cli._placement_helpers import ALIVE, NOWHERE, ROUTE, SILENT, decide


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
    ({"requires": ["terminal"]}, "not carried by this hermes-hub client"),
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
    ({"max_retries": 5}, "max_retries"),  # hermes-hub create-task has no retry breaker option [daveey]
    # 11-11: placement="hub" + runs_on=[machine:worker-1] ran on the hub's own queue, labels dropped. [daveey]
    ({"runs_on": ["machine:worker-1"]}, "runs_on"),
    ({"runs_on": []}, "runs_on"),  # an explicit "any machine" is a placement too
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
                   "model": "m", "provider": "p", "requires": ["ssh:mini"], "idempotency_key": "k"}
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
    # Parents no longer keep a hub-pinned card from the hub: it is deferred there (not refused).
    assert decide({"title": "x", "placement": "hub", "parents": ["t_1"]}).get("defer") == "hub"
    # A gateway with no route is not a destination, and never silently becomes a local card.
    assert decide({"title": "x", "placement": "mini"}, live=lambda t: f"{t} record missing")["where"] == "refuse"
    assert decide({"title": "x", "placement": "mini"}, cfg={}, live=lambda t: f"{t} record missing")["where"] == "refuse"
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


# --- requires and assignees (an honest `requires` list no longer pins a card here) ---------------

HONEST = ["terminal", "file", "git", "kanban"]
HUB_PROFILES = ["default", "alpha", "bb-tuner", "coach", "ci-sweeper"]
HUB_TOOLS = {"default": {"terminal", "file", "git", "kanban"}, "coach": {"terminal", "file", "git"},
             "alpha": {"terminal", "file", "git"}}
ADVERTISED = lambda target: {"profiles": HUB_PROFILES, "tools": HUB_TOOLS}


def test_honest_requires_with_the_default_assignee_goes_to_the_target():
    d = decide({"title": "Fix the flaky test", "requires": HONEST}, carries=True)
    assert d["where"] == "hub", d
    # The same when the target publishes its profile tools and its default profile has them all.
    assert decide({"title": "Fix the flaky test", "requires": HONEST}, carries=True,
                  info=ADVERTISED)["where"] == "hub"


def test_host_repo_and_unknown_requires_stay_local():
    for req in (["terminal", "host_repo"], ["teleport"]):
        d = decide({"title": "Fix the flaky test", "requires": req}, carries=True, info=ADVERTISED)
        assert d["where"] == "local" and "carried_requires" in d["why"], d


def test_carried_requires_is_configurable():
    cfg = {**ROUTE, "carried_requires": ["terminal"]}
    assert decide({"title": "x", "requires": ["terminal"]}, carries=True, cfg=cfg)["where"] == "hub"
    d = decide({"title": "x", "requires": ["terminal", "file"]}, carries=True, cfg=cfg)
    assert d["where"] == "local" and "file" in d["why"], d


def test_requires_a_target_profile_lacks_stays_local():
    d = decide({"title": "x", "assignee": "coach", "requires": ["terminal", "kanban"]}, carries=True,
               info=ADVERTISED)
    assert d["where"] == "local" and "kanban" in d["why"] and "coach" in d["why"], d
    # ci-sweeper publishes no tools at all: lacks every one.
    d = decide({"title": "x", "assignee": "ci-sweeper", "requires": ["terminal"]}, carries=True,
               info=ADVERTISED)
    assert d["where"] == "local" and "lacks" in d["why"], d


def test_assignee_the_target_has_a_profile_for_goes_there():
    d = decide({"title": "Coach plugin build", "assignee": "coach", "requires": ["terminal", "file", "git"]},
               carries=True, info=ADVERTISED)
    assert d["where"] == "hub", d


def test_unknown_assignee_stays_local():
    d = decide({"title": "x", "assignee": "links"}, info=ADVERTISED)
    assert d["where"] == "local" and "no profile on hub" in d["why"], d
    # A target that advertises no profile list keeps the old rule: only `assignees` travel.
    d = decide({"title": "x", "assignee": "coach"})
    assert d["where"] == "local" and "not placed remotely" in d["why"], d


def test_target_info_reads_the_gateway_record_and_fresh_profile_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path))
    now = time.time()
    db = sqlite3.connect(tmp_path / "hub.db")
    db.executescript(HUB_SCHEMA)
    db.execute("INSERT INTO replica VALUES ('hub','gateway','info',1,1,?,0)",
               (json.dumps({"gateway": "hub", "profiles": ["default", "coach"]}),))
    db.commit()
    db.close()
    caps = [{"name": "profile-tools:default:terminal", "ok": True, "checked_at": now - 10},
            {"name": "profile-tools:coach:file", "ok": True, "checked_at": now - 99999},  # stale
            {"name": "ssh:mini", "ok": True, "checked_at": now}]
    got = kp.target_info("hub", now=now, remote={"hub": {"schema": 1, "capabilities": caps}})
    assert got == {"profiles": ["default", "coach"], "tools": {"default": {"terminal"}}}
    # No profile-tools entry at all: tools unknown (None), not "lacks everything".
    got = kp.target_info("mini", now=now, remote={"mini": {"schema": 1, "capabilities": caps[2:]}})
    assert got == {"profiles": None, "tools": None}


def test_registry_publishes_each_profiles_tools(monkeypatch):
    from hermes_cli import kanban_capabilities as kc
    from hermes_cli import profiles as pr
    monkeypatch.setattr(pr, "list_profile_names", lambda: ["default", "coach", "broken"])

    def caps(name):
        if name == "broken":
            raise ValueError("bad config")
        return {"default": {"terminal", "file", "git", "kanban", "host_repo"},
                "coach": {"terminal", "git"}}[name]
    monkeypatch.setattr(kc, "_profile_capabilities", caps)
    names = [n for n, ok in reg.profile_tool_entries() if ok]
    assert names == ["profile-tools:default:terminal", "profile-tools:default:file",
                     "profile-tools:default:git", "profile-tools:default:kanban",
                     "profile-tools:coach:terminal", "profile-tools:coach:git"]
    assert all(reg.is_gateway_need(n) for n in names)


# --- marker narrowing ------------------------------------------------------------------------------

def test_macos_ci_runner_labels_do_not_pin_the_card():
    d = decide({"title": "Move hermes-state CI to GitHub-hosted runners",
                "body": "runs-on: macos-latest; see .github/workflows/hermes-state-macos.yml"})
    assert d["where"] == "hub", d
    d = decide({"title": "test_gateway_start_refused.py: teardown ERRORs on macOS.", "body": ""})
    assert d["where"] == "local" and "macos" in d["why"], d
    d = decide({"title": "Teardown fails", "body": "Only on macOS, never on Linux."})
    assert d["where"] == "local" and "macos" in d["why"], d


# --- tool-generated card text: a required-check name is not device context ----------------------
# 11-12/13: every review card `hermes-core-wt ship --request-review` filed on a pool worker stayed on that
# worker (t_01a1d1c2ca18df39, t_01a1d2434cf3676c, t_01a1d1fe567955ba): its body quotes the required checks
# ("CI at filing: green: macOS state required (v1): success; ..."), and MACOS_MARKER read the check name
# as Mac work. On the hub the same text sent the card to the admission judge. [daveey]

SHIP_REVIEW = json.loads((Path(__file__).parent / "fixtures" /
                          "ship_review_card_t_01a1d2434cf3676c.json").read_text())
SHIP_REVIEW_ARGS = {"title": SHIP_REVIEW["title"], "body": SHIP_REVIEW["body"], "assignee": "default",
                    "priority": 60000, "requires": ["terminal", "git"]}
POOL = "pool:worker"


def test_ship_review_fixture_quotes_the_required_check_name():
    assert "CI at filing: green: macOS state required (v1): success" in SHIP_REVIEW["body"]


def test_ship_review_card_on_a_pool_worker_joins_the_pool_queue():
    d = kp.decide(dict(SHIP_REVIEW_ARGS), cfg={"worker_queue": {"worker-2": POOL}}, self_gw="worker-2",
                  live=ALIVE, locate=NOWHERE, info=SILENT, carries=True, machine_ok=lambda name: True)
    assert POOL in (d.get("runs_on") or []), d
    assert "local marker" not in d["why"] and not any(v.startswith("machine:") for v in d["runs_on"]), d
    assert kp.portable_reason(dict(SHIP_REVIEW_ARGS), {"worker_queue": {"worker-2": POOL}}) is None


def test_ship_review_card_on_the_hub_is_no_marker_hit_and_never_consults_admission(monkeypatch):
    from hermes_cli import kanban_hub_admission as judge
    asked = []
    monkeypatch.setattr(judge, "admit", lambda fields, **kw: asked.append(fields) or judge.Verdict())
    cfg = {"worker_queue": {"hub": POOL}}
    d = kp.decide(dict(SHIP_REVIEW_ARGS), cfg=cfg, self_gw="hub", live=ALIVE, locate=NOWHERE, info=SILENT,
                  carries=True, machine_ok=lambda name: True)
    assert asked == [] and POOL in (d.get("runs_on") or []), d
    assert "marker" not in d["why"], d


@pytest.mark.parametrize("text", [
    "macOS state required (v1)", "MACOS STATE REQUIRED (v2)", "mac os state required", "macOS state required",
])
def test_the_required_check_name_is_not_a_macos_marker(text):
    assert not re.search(kp.MACOS_MARKER, text, re.IGNORECASE)


@pytest.mark.parametrize("text", [
    "macOS state is broken after the update", "macOS required for this repro", "Only on macOS state",
    "macOS stateful tests fail", "macOS",
])
def test_macos_followed_by_other_words_still_matches(text):
    assert re.search(kp.MACOS_MARKER, text, re.IGNORECASE)


# --- liveness -----------------------------------------------------------------------------------

def test_target_live_needs_a_recent_sync_and_a_fresh_target_record():
    now = 1_000_000.0
    fresh = {"hub": {"refreshed_at": now - 60}}
    assert kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote=fresh) is None
    assert "sync" in kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 3600}, remote=fresh)
    stale = {"hub": {"refreshed_at": now - 7200}}
    assert "stale" in kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote=stale)
    assert "missing" in kp.target_live("hub", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote={})


def test_target_live_names_the_known_gateways_for_a_name_it_has_no_record_of():
    # The Desktop calls the hub 'hub-ec2' and the mini 'mac-mini'. "stale or missing" read as an outage, so
    # callers dropped the pin instead of fixing the name (22 refusals, 10-28..11-02). [daveey]
    now = 1_000_000.0
    fresh = {"hub": {"refreshed_at": now - 60}, "mini": {"refreshed_at": now - 60}, "macbook": {"refreshed_at": now}}
    why = kp.target_live("hub-ec2", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote=fresh) or ""
    assert why.startswith("no gateway is named 'hub-ec2'") and "hub, macbook, mini" in why, why
    assert "stale" not in why and "kanban.placement.aliases" in why, why
    # An empty registry can't tell a wrong name from an unreadable registry: the old reason stays.
    empty = kp.target_live("hub-ec2", now=now, live_seconds=600, meta={"last_ok": now - 30}, remote={}) or ""
    assert "missing" in empty and not empty.startswith("no gateway"), empty


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


def test_max_retries_card_stays_local_with_its_breaker(mac):
    """hermes-hub create-task cannot carry max_retries: the card is created here, breaker intact. [daveey]"""
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "max_retries": 5}))
    assert out["ok"] and out["task_id"] and "placed" not in out, out
    assert mac["calls"] == [] and _local_count() == 1
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT max_retries FROM tasks WHERE id=?", (out["task_id"],)).fetchone()[0] == 5


def test_max_retries_card_pinned_to_the_hub_is_refused_not_created(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hub",
                                        "max_retries": 5}))
    assert not out.get("ok") and out["code"] == "placement_unavailable" and "max_retries" in out["error"], out
    assert mac["calls"] == [] and _local_count() == 0


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


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("pin", [None, "auto"])
def test_origin_bound_portable_tool_card_enrolls(mac, monkeypatch, worker, pin):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    if worker:
        with kbc.connect_closing() as conn:
            parent = kb.create_task(conn, title="Coordinator", assignee="default")
            claim = kb.claim_task(conn, parent)
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claim.current_run_id))
        monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {"macbook": ["worker-1"]}})
    out = json.loads(kt._handle_create({"title": "Analyze public data", "requires": [],
                                        "origin_session": "hub:abc123", "placement": pin}))
    assert out.get("ok"), out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, out["task_id"]).placement_policy == "worker-first-v1"
        if worker:
            created = next(e for e in kb.list_events(conn, out["task_id"]) if e.kind == "created")
            assert created.payload["creator_task_id"] == parent
    assert out["placement"]["placement_policy"] == "worker-first-v1"
    assert "worker pool" in out["runs_where"]
    assert not mac["calls"]


@pytest.mark.parametrize("surface", ["cli", "tool", "origin-tool"])
def test_current_nondefault_board_does_not_enroll(mac, monkeypatch, surface):
    from tools import kanban_tools as kt
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb.create_board("other")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other")
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {"macbook": ["worker-1"]}})
    if surface == "cli":
        output = cli.run_slash("--board other create 'Analyze public data' --assignee default")
        assert "t_" in output, output
    else:
        args = {"title": "Analyze public data", "requires": []}
        if surface == "origin-tool":
            args["origin_session"] = "hub:abc123"
        out = json.loads(kt._handle_create(args))
        assert out.get("ok"), out
    with kbc.connect_closing(board="other") as conn:
        rows = conn.execute("SELECT placement_policy FROM tasks").fetchall()
        assert len(rows) == 1 and rows[0][0] is None, output if surface == "cli" else out


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


# --- marker tuning: a passing MacBook mention is not a local-only need [daveey] ---

_SIDEBAR_TITLE = (
    "Fix sidebar count vs task strip split under kanban.sync.links_reader=replica (Upkeep 14 vs 9)")
_SIDEBAR_BODY = (Path(__file__).parent / "fixtures" / "t_01a180aae388e142_body.txt").read_text()
_MARKER_REMEDY = ("if the work doesn't need this machine, archive and re-create without naming the machine in the title "
                  "or body (placement='auto' overrides only a context skill such as babybear-*)")


def test_marker_cases_a_through_e():
    """The five cases the marker tune is pinned to, verbatim."""
    # (a) the work itself is a live verify on the MacBook.
    a = decide({"title": "Verify 57094b7a60 live on the MacBook"})
    assert a["where"] == "local" and a["why"].startswith("local marker"), a
    assert "macbook" in a["why"].lower()
    # (b) a hermes_cli fix that only names the MacBook as a verification step.
    b = decide({"title": "Fix the hermes_cli session-activity projection",
                "body": "Ship the hermes_cli fix, then verify live on the hub and the MacBook."})
    assert b["where"] == "hub", b
    # (c) Desktop DOM, CDP readback, or Chrome still needs this machine.
    for text in ("Needs a Desktop DOM readback of the composer",
                 "Needs a CDP readback of the sidebar",
                 "Sign in with Chrome and read the cookie"):
        c = decide({"title": "Read the live UI", "body": text})
        assert c["where"] == "local" and c["why"].startswith("local marker"), (text, c)
    # (d) t_01a180aae388e142 title and body, verbatim.
    d = decide({"title": _SIDEBAR_TITLE, "body": _SIDEBAR_BODY})
    assert d["where"] == "hub", d
    # (e) a local-only skill still pins, even with no marker in the text.
    e = decide({"title": "Weekly digest", "skills": ["apple-notes"]})
    assert e["where"] == "local" and "skill apple-notes" in e["why"], e


def test_local_marker_decision_is_in_the_create_result(mac):
    """Auto-placement that stays local because of a text marker says so, with the remedy."""
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({
        "title": "Verify 57094b7a60 live on the MacBook",
        "body": "Read the live sidebar on this machine.",
    }))
    assert out["ok"] and out.get("task_id") and _local_count() == 1, out
    assert out["placement"] == {
        "where": "local",
        "why": "local marker: mentions 'macbook'",
    }, out
    assert out["remedy"] == _MARKER_REMEDY
    # A card that goes to the hub, and one kept local for a structural reason, do not get that note.
    hub = json.loads(kt._handle_create({"title": "Fix the flaky lint", "body": "Push it."}))
    assert hub.get("placed", {}).get("gateway") == "hub" and "placement" not in hub, hub
    structural = json.loads(kt._handle_create({"title": "Keep this one here", "goal_mode": True}))
    assert structural["ok"] and "placed" not in structural and "remedy" not in structural, structural


# --- every local decision carries its reason (41 of 148 MacBook cards had none) [daveey] ---------

_NO_NEED_NOTE = ("placement='local' was pinned, but nothing in the card names a machine-only need; without the "
                 "pin it would run on hub. If the work doesn't need this machine, archive this card and create "
                 "it again without the pin.")


def test_local_pin_returns_its_why_and_the_no_need_note(mac, caplog):
    """A placement='local' pin on a card with no local need: created here, says why, names the target."""
    from tools import kanban_tools as kt
    with caplog.at_level("INFO", logger="tools.kanban_tools_placement"):
        out = json.loads(kt._handle_create({"title": "Verify green main CI", "body": "Check the run.",
                                            "placement": "local"}))
    assert out["ok"] and out.get("task_id") and _local_count() == 1 and not mac["calls"], out
    assert out["placement"] == {"where": "local", "why": "placement pinned local by the caller"}, out
    assert out["note"] == _NO_NEED_NOTE, out
    assert "remedy" not in out
    assert "kanban placement: local (placement pinned local by the caller)" in caplog.text


def test_local_pin_with_a_local_marker_gets_no_note(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Reload the launchd agent", "body": "kickstart it.",
                                        "placement": "local"}))
    assert out["ok"] and _local_count() == 1, out
    assert out["placement"] == {"where": "local", "why": "placement pinned local by the caller"}, out
    assert "note" not in out and "remedy" not in out, out


def test_local_pin_with_a_structural_local_need_gets_no_note(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Fix the flaky lint", "goal_mode": True,
                                        "placement": "local"}))
    assert out["ok"], out
    assert out["placement"]["why"] == "placement pinned local by the caller" and "note" not in out, out


def test_plain_local_decision_returns_its_why(mac, caplog):
    """No pin, kept local for a structural reason (not a marker): the result still says why."""
    from tools import kanban_tools as kt
    with caplog.at_level("INFO", logger="tools.kanban_tools_placement"):
        out = json.loads(kt._handle_create({"title": "Keep this one here", "goal_mode": True}))
    assert out["ok"] and "placed" not in out, out
    assert out["placement"]["where"] == "local" and out["placement"]["why"], out
    assert set(out["placement"]) == {"where", "why"} and "note" not in out, out
    assert f"kanban placement: local ({out['placement']['why']})" in caplog.text


def test_needs_reason_is_returned(mac, monkeypatch):
    """A gateway need the target does not hold keeps the card here; the result names it."""
    from tools import kanban_tools as kt
    monkeypatch.setattr(reg, "locate", lambda need, now=None: {"holders": []})
    monkeypatch.setattr("hermes_cli.kanban_capability_routing.preflight", lambda *a, **k: None)
    out = json.loads(kt._handle_create({"title": "Reboot the box", "requires": ["ssh:nowhere"]}))
    assert out["ok"], out
    assert out["placement"]["where"] == "local" and out["placement"]["why"].startswith("needs ssh:nowhere"), out



# --- deferred placement: cards with parents run on the target once the parents finish ---------

def test_child_with_parents_and_no_local_need_is_deferred_to_the_target():
    d = decide({"title": "Fix the flaky lint", "body": "Push it.", "parents": ["t_1"]})
    assert d["where"] == "local" and d["defer"] == "hub" and "parents" in d["why"], d


@pytest.mark.parametrize("extra,why", [
    ({"title": "Read the Chrome tab"}, "local marker"),
    ({"skills": ["apple-notes"]}, "skill apple-notes"),
    ({"workspace_kind": "dir", "workspace_path": "/tmp/x"}, "dir"),
    ({"board": "links"}, "board"),
    ({"initial_status": "blocked"}, "initial_status"),
])
def test_child_with_parents_and_a_local_need_stays_local_and_is_not_deferred(extra, why):
    d = decide({"title": "Fix the flaky lint", "parents": ["t_1"], **extra})
    assert d["where"] == "local" and not d.get("defer") and why in d["why"], d


def test_defer_parents_off_keeps_children_local():
    d = decide({"title": "Fix the flaky lint", "parents": ["t_1"]}, cfg={**ROUTE, "defer_parents": False})
    assert d["where"] == "local" and not d.get("defer") and "parents" in d["why"], d


def test_deferral_does_not_need_the_target_live_now():
    d = decide({"title": "Fix the flaky lint", "parents": ["t_1"]}, live=lambda t: "down")
    assert d.get("defer") == "hub", d


def test_pin_to_the_target_with_parents_is_deferred_not_refused():
    d = decide({"title": "Fix the flaky lint", "parents": ["t_1"], "placement": "hub"})
    assert d["where"] == "local" and d.get("defer") == "hub", d


HUB_TID = "t_01a1ffff00000001"


def _done_parent(kb, conn, title="Parent work"):
    pid = kb.create_task(conn, title=title, assignee="default")
    kb.complete_task(conn, pid, summary=f"{title}: did the thing", force=True)
    return pid


def _forwarder(conn, tid, mac, monkeypatch, *, check=(1, "x"), await_tid=HUB_TID):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement_forward as kpf
    from hermes_cli import kanban_wait_check as kwait

    monkeypatch.setattr(kwait, "check_remote", lambda gw, kind, tid_, **kw: (1, f"{tid_} ready"))
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    f = kpf.Forwarder(conn, tid, claimed.current_run_id)
    f.live = lambda t: None
    f.await_id = lambda rid, **kw: (await_tid, "ready")
    f.check = lambda gw, rtid: check
    return f


def test_kanban_create_with_parents_creates_a_local_deferred_card(mac):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Orchestrate", assignee="default")
    out = json.loads(kt._handle_create({"title": "Fix the flaky lint", "body": "Push it.", "parents": [parent],
                                        "session_id": "20261027_183000_abc123"}))
    assert out["ok"] and out["placement"]["defer"] == "hub" and "placed" not in out, out
    assert mac["calls"] == []  # nothing sent to the hub yet
    with kbc.connect_closing() as conn:
        st = kpf.state(conn, out["task_id"])
        assert st["target"] == "hub" and st["origin_session"] == "20261027_183000_abc123"
        assert kb.get_task(conn, out["task_id"]).status == "todo"
        task = kb.get_task(conn, out["task_id"])
        assert kpf.forward_argv(task, None, ["hermes", "chat"])[1:5] == ["-P", "-m", kpf.MODULE, "--"]
        task.skills = ["sdlc-review"]  # the review lane always runs a real reviewer
        assert kpf.forward_argv(task, None, ["hermes", "chat"]) is None
    marked = json.loads(kt._handle_create({"title": "Read the Chrome tab", "parents": [parent]}))
    with kbc.connect_closing() as conn:
        assert kpf.state(conn, marked["task_id"]) is None
        assert kpf.forward_argv(kb.get_task(conn, marked["task_id"]), None, ["hermes"]) is None


def test_forwarder_sends_the_card_with_parent_handoffs_and_parks_until_the_hub_card_is_done(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Orchestrate", assignee="default")
    created = json.loads(kt._handle_create({"title": "Fix the flaky lint", "body": "Push it.", "parents": [parent],
                                          "max_runtime_seconds": 900,
                                          "session_id": "20261027_183000_abc123"}))
    assert created.get("task_id"), created
    child = created["task_id"]
    with kbc.connect_closing() as conn:
        grandchild = kb.create_task(conn, title="Verify the lint fix live", assignee="default", parents=(child,))
        kb.complete_task(conn, parent, summary="fanned out; lint fix is the child", force=True)
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        assert kb.get_task(conn, grandchild).status == "todo"
        f = _forwarder(conn, child, mac, monkeypatch)
        assert f.run() == "parked"
        argv = mac["calls"][0]
        assert argv[1:3] == ["create-task", "hub"]
        assert argv[argv.index("--origin-session") + 1] == "20261027_183000_abc123"
        body = argv[argv.index("--body") + 1]
        assert body.startswith("Push it.") and "fanned out; lint fix is the child" in body and child in body
        assert "Runtime budget asked by the creator: 900 s." in body  # options travel with the card
        t = kb.get_task(conn, child)
        assert t.status == "scheduled"
        ev = [e for e in kb.list_events(conn, child) if e.kind == "scheduled"][-1]
        assert f"wait-check task {HUB_TID} --gateway hub --until done,archived" in json.dumps(ev.payload)
        assert kpf.state(conn, child)["remote_task_id"] == HUB_TID
        # Woken while the hub card still runs: park again, the grandchild keeps waiting.
        kb.unblock_task(conn, child)
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, check=(1, HUB_TID + " running"))
        assert f.run() == "parked" and len(mac["calls"]) == 1  # never a second card
        assert kb.get_task(conn, grandchild).status == "todo"
        # The hub card is done: the local card completes and releases the grandchild.
        kb.unblock_task(conn, child)
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, check=(0, HUB_TID + " done"))
        assert f.run() == "completed"
        assert kb.get_task(conn, child).status == "done"
        assert HUB_TID in (kb.latest_summary(conn, child) or "")
        kb.recompute_ready(conn)
        assert kb.get_task(conn, grandchild).status == "ready"
        assert len(mac["calls"]) == 1


def test_forwarder_completes_when_the_hub_card_finishes_between_settle_and_park(mac, monkeypatch):
    """confirm_remote refuses a done producer (producer_done); the forwarder settles it instead of crashing."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_wait_check as kwait
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        assert _forwarder(conn, child, mac, monkeypatch).run() == "parked"
        kb.unblock_task(conn, child)
        kb.recompute_ready(conn)
        reads = iter([(1, HUB_TID + " running"), (0, HUB_TID + " done")])
        f = _forwarder(conn, child, mac, monkeypatch, check=None)
        f.check = lambda gw, rtid: next(reads)
        monkeypatch.setattr(kwait, "check_remote", lambda gw, kind, tid_, **kw: (0, f"{tid_} done"))
        assert f.run() == "completed"
        assert kb.get_task(conn, child).status == "done"


def test_forwarder_falls_back_to_a_local_worker_when_the_hub_refuses(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    mac["result"]["rc"] = 1
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch)
        assert f.run() == "fallback"
        assert kpf.state(conn, child) is None
        assert kb.get_task(conn, child).status == "running"  # the exec'd worker owns this claim
        assert kpf.forward_argv(kb.get_task(conn, child), None, ["hermes"]) is None


def test_forwarder_retries_a_target_that_is_not_live_then_runs_it_here(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        actions = []
        for _ in range(kpf.MAX_NOT_LIVE):
            f = _forwarder(conn, child, mac, monkeypatch)
            f.live = lambda t: "sync stale"
            actions.append(f.run())
            if kb.get_task(conn, child).status == "scheduled":
                kb.unblock_task(conn, child)
                kb.recompute_ready(conn)
        assert actions == ["requeued"] * (kpf.MAX_NOT_LIVE - 1) + ["fallback"], actions
        assert mac["calls"] == [] and kpf.state(conn, child) is None


def test_forwarder_waits_for_a_queued_request_without_sending_it_twice(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
        assert f.run() == "requeued" and kb.get_task(conn, child).status == "scheduled"
        # The system clock park declares its wake reason (a bare clock is refused no_wake_reason).
        rec = [e for e in kb.list_events(conn, child) if e.kind == "scheduled"][-1].payload["awaiting"]
        assert rec["kind"] == "clock" and rec["why"].startswith("deferred placement retry: "), rec
        kb.unblock_task(conn, child)
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=HUB_TID)
        assert f.run() == "parked"
        assert len(mac["calls"]) == 1


def test_hub_card_archived_blocks_the_local_card_for_a_decision(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        assert _forwarder(conn, child, mac, monkeypatch).run() == "parked"
        kb.unblock_task(conn, child)
        kb.recompute_ready(conn)
        assert _forwarder(conn, child, mac, monkeypatch, check=(0, HUB_TID + " archived")).run() == "blocked"
        assert kb.get_task(conn, child).status == "blocked"
        assert kpf.state(conn, child) is None  # an unblock runs it here


def test_main_execs_the_worker_when_forwarding_falls_back(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    mac["result"]["rc"] = 1
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)
    monkeypatch.setattr(kpf.Forwarder, "__init__", _patched_init(kpf.Forwarder.__init__))
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    execs = []
    monkeypatch.setattr(kpf.os, "execvp", lambda f, argv: execs.append(argv))
    kpf.main(["--", "hermes", "-p", "default", "chat"])
    assert execs == [["hermes", "-p", "default", "chat"]]


def _patched_init(orig):
    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.live = lambda t: None
    return init


def test_dispatcher_spawns_the_forwarder_for_a_deferred_card(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kdd
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    plain = json.loads(kt._handle_create({"title": "Read the Chrome tab", "parents": [parent]}))["task_id"]
    spawned = {}

    class P:
        pid = 4242

    def popen(cmd, **kw):
        spawned[kw["env"]["HERMES_KANBAN_TASK"]] = cmd
        return P()

    monkeypatch.setattr(kdd.subprocess, "Popen", popen)
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        for tid in (child, plain):
            claimed = kb.claim_task(conn, tid)
            kdd._default_spawn(claimed, str(Path(os.environ["HERMES_HOME"])))
    assert "hermes_cli.kanban_placement_forward" in spawned[child], spawned[child]
    tail = spawned[child][spawned[child].index("--") + 1:]
    # Fork: the worker the forwarder execs on fallback is upstream's owner client
    # (``-m hermes_cli.kanban_worker_client``), no longer ``hermes ... chat -q``.
    assert "hermes_cli.kanban_worker_client" in tail  # the worker it execs on fallback
    assert "hermes_cli.kanban_placement_forward" not in spawned[plain]


# --- gateway aliases: 'hermes-hub' is the hub's host name, 'hub' its gateway name ------------------
# Coach signature (6 sessions, 10-24..10-28): placement='hermes-hub' was refused as "not a routed
# gateway (only 'hub')", so cron runs and chats created no card at all. [daveey]

@pytest.mark.parametrize("pin", ["hermes-hub", "Hermes-Hub", " hermes-hub "])
def test_host_style_alias_of_the_routed_target_is_placed_there(pin):
    d = decide({"title": "CI fix", "placement": pin})
    assert d["where"] == "hub", d


def test_host_style_alias_of_own_gateway_means_local():
    # On the hub (no route there), placement='hermes-hub' is this gateway: create locally.
    assert decide({"title": "x", "placement": "hermes-hub"}, cfg={}, self_gw="hub")["where"] == "local"
    assert kp.explicit_foreign_pin({"placement": "hermes-hub"}, "hub", {}) == ""


def test_configured_alias_maps_any_name_and_unknown_names_still_refuse():
    cfg = {**ROUTE, "aliases": {"ec2-hub": "hub"}}
    assert decide({"title": "x", "placement": "ec2-hub"}, cfg=cfg)["where"] == "hub"
    # Only a known gateway behind the 'hermes-' prefix resolves; anything else is refused as before.
    d = decide({"title": "x", "placement": "hermes-mini"}, live=lambda t: f"{t} record missing")
    assert d["where"] == "refuse" and d["pin"] == "hermes-mini", d
    assert decide({"title": "x", "placement": "hermes"}, live=lambda t: f"{t} record missing")["where"] == "refuse"


def test_hermes_hub_pin_through_the_tool_creates_on_the_hub_not_refused(mac):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hermes-hub"}))
    assert out.get("ok"), out
    assert out["placed"]["gateway"] == "hub" and mac["calls"], out
    assert _local_count() == 0


def test_hermes_hub_pin_on_the_hub_itself_creates_locally(mac, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", dict)
    monkeypatch.setattr(reg, "self_gateway", lambda: "hub")
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hermes-hub"}))
    assert out.get("ok") and out["task_id"] and "placed" not in out, out
    assert _local_count() == 1


def test_desktop_gateway_name_through_the_tool_is_refused_with_the_names_to_use(mac):
    # cross_session/cross_task and @task refs name the hub 'hub-ec2'. With no alias for it, the refusal must
    # say which names exist, not "hub-ec2 not live: ... stale or missing". [daveey]
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hub-ec2"}))
    assert out.get("code") == "placement_unavailable" and out.get("created") is False, out
    assert "no gateway is named 'hub-ec2': hermes-hub knows hub" in out["error"], out
    assert "not live" not in out["error"] and "stale" not in out["error"], out
    assert _local_count() == 0 and not mac["calls"]


def test_desktop_gateway_name_with_a_configured_alias_creates_on_the_hub(mac, monkeypatch):
    # The shipped policy maps the Desktop's names: kanban.placement.aliases {hub-ec2: hub, mac-mini: mini}. [daveey]
    from tools import kanban_tools as kt
    monkeypatch.setattr(kp, "config", lambda: {**ROUTE, "wait_seconds": 0, "aliases": {"hub-ec2": "hub"}})
    out = json.loads(kt._handle_create({"title": "Regenerate the plugin index", "placement": "hub-ec2"}))
    assert out.get("ok"), out
    assert out["placed"]["gateway"] == "hub" and mac["calls"], out
    assert _local_count() == 0


# --- heavy work goes to a worker node (kanban.placement.heavy_route) ------------------------------------

HEAVY = {**ROUTE, "heavy_route": {"macbook": "worker-1"}}
PICKS_UP = lambda name: True


@pytest.mark.parametrize("args", [
    {"title": "Kanban blockers: fix the re-wake loop + regression test"},
    {"title": "Model gate bench-v2 scores Opus and Astra the same"},
    {"title": "CI red on main", "body": "Reproduce with scripts/run_tests.sh tests/hermes_cli/ -j 4"},
    {"title": "Coach report_only fix", "body": "run pytest on plugins/coach before pushing"},
])
def test_heavy_work_runs_on_the_worker_through_the_board_lease(args):
    d = decide(args, cfg=HEAVY, machine_ok=PICKS_UP)
    assert d["where"] == "local" and d["runs_on"] == ["machine:worker-1"], d
    assert d["why"].startswith("heavy work") and "worker-1" in d["why"]


def test_heavy_work_with_parents_still_runs_on_the_worker():
    d = decide({"title": "Run the benchmark", "parents": ["t_1"]}, cfg=HEAVY, machine_ok=PICKS_UP)
    assert d["runs_on"] == ["machine:worker-1"] and "defer" not in d, d


def test_light_work_keeps_the_gateway_route():
    d = decide({"title": "daveey/hermes CI: summarise the failing jobs"}, cfg=HEAVY, machine_ok=PICKS_UP)
    assert d["where"] == "hub" and "runs_on" not in d, d


def test_mac_only_heavy_work_stays_local_without_runs_on():
    d = decide({"title": "Fix flaky macOS test in the pytest suite"}, cfg=HEAVY, machine_ok=PICKS_UP)
    assert d["where"] == "local" and "runs_on" not in d and d["why"].startswith("local marker"), d


def test_heavy_work_that_names_the_target_host_keeps_the_route():
    d = decide({"title": "Run the regression tests on hermes-hub"}, cfg=HEAVY, machine_ok=PICKS_UP)
    assert d["where"] == "hub" and "runs_on" not in d, d


def test_heavy_work_falls_back_to_the_route_when_the_worker_does_not_pick_up():
    d = decide({"title": "Run the benchmark"}, cfg=HEAVY, machine_ok=lambda name: False)
    assert d["where"] == "hub" and "runs_on" not in d, d


def test_heavy_route_is_off_unless_configured_and_an_explicit_pin_wins():
    assert decide({"title": "Run the benchmark"}, machine_ok=PICKS_UP)["where"] == "hub"
    assert decide({"title": "Run the benchmark"}, cfg=HEAVY, self_gw="mini", machine_ok=PICKS_UP)["where"] == "local"
    d = decide({"title": "Run the benchmark", "placement": "hub"}, cfg=HEAVY, machine_ok=PICKS_UP)
    assert d["where"] == "hub" and "runs_on" not in d, d
    d = decide({"title": "Run the benchmark", "placement": "local"}, cfg=HEAVY, machine_ok=PICKS_UP)
    assert d["where"] == "local" and "runs_on" not in d, d


def test_configured_heavy_markers_replace_the_builtin_list():
    cfg = {**HEAVY, "heavy_markers": [r"\bcrunch\b"]}
    assert decide({"title": "Run the benchmark"}, cfg=cfg, machine_ok=PICKS_UP)["where"] == "hub"
    assert decide({"title": "crunch the logs"}, cfg=cfg, machine_ok=PICKS_UP)["runs_on"] == ["machine:worker-1"]


@pytest.mark.parametrize("entry,ok", [
    ({"state": "active", "labels": ["linux"]}, True),           # the Board publishes no pickup field (10-28)
    ({"state": "active", "pickup": "explicit"}, True),
    ({"state": "active", "pickup": "off"}, False),
    ({"state": "active", "pickup": False}, False),
    ({"state": "active", "hold": True}, False),
    ({"state": "revoked"}, False),
    (None, False),
])
def test_heavy_route_machine_check_reads_the_board_registry(monkeypatch, entry, ok):
    from hermes_cli import kanban_worker_node as wn
    # Seen just now: a named machine is held to the same FRESH_S window (test_kanban_placement_freshness).
    machines = {"worker-1": {**entry, "last_seen": time.time() * 1000}} if entry else {}
    monkeypatch.setattr(wn, "registry", lambda: {"me": "macbook", "machines": machines})
    assert kp._machine_picks_up("worker-1") is ok


# PR #2001 review nit 4 (t_01a1d416d71f0738): coordination attestation counts only members seen within FRESH_S, but
# label_machines ignored last_seen, so a stale active/unheld entry holding the whole set still let a card queue.
@pytest.mark.parametrize("age_s,listed", [(0, True), (60, True), (None, False), (301, False), (3600, False),
                                          (-120, False), ("nan", False)])
def test_label_machines_counts_only_fresh_members(monkeypatch, age_s, listed):
    from hermes_cli import kanban_placement_coordination as coord
    from hermes_cli import kanban_worker_node as wn
    entry = {"state": "active", "labels": ["pool:worker", "capability:hub-write"]}
    if age_s == "nan":
        entry["last_seen"] = float("nan")
    elif age_s is not None:
        entry["last_seen"] = (time.time() - age_s) * 1000
    assert coord.FRESH_S == 300
    monkeypatch.setattr(wn, "registry", lambda: {"me": "hub", "machines": {"worker-1": entry}})
    want = ["worker-1"] if listed else []
    assert kp.label_machines("pool:worker") == want
    assert kp.label_machines(("pool:worker", "capability:hub-write")) == want
    assert kp._machine_picks_up(("pool:worker", "capability:hub-write")) is listed


# Review nit 3: any non-string label collection is a joint label set, judged in one place.
@pytest.mark.parametrize("shape", [tuple, list, set, frozenset])
def test_machine_picks_up_takes_any_label_collection(monkeypatch, shape):
    from hermes_cli import kanban_worker_node as wn
    seen = int(time.time() * 1000)
    machines = {"worker-1": {"state": "active", "last_seen": seen, "labels": ["pool:worker"]},
                "worker-5": {"state": "active", "last_seen": seen, "labels": ["capability:hub-write"]}}
    monkeypatch.setattr(wn, "registry", lambda: {"me": "hub", "machines": machines})
    assert kp._machine_picks_up(shape(["pool:worker"])) is True
    assert kp._machine_picks_up(shape(["pool:worker", "capability:hub-write"])) is False
    machines["worker-1"]["labels"].append("capability:hub-write")
    assert kp._machine_picks_up(shape(["pool:worker", "capability:hub-write"])) is True


@pytest.mark.parametrize("title", ["Analyze public data", "Run the full test suite"])
def test_tool_creates_worker_policy_atomically_and_reports_queue(mac, monkeypatch, title):
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db_connect as kbc
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {"macbook": []}})
    out = json.loads(kt._handle_create({"title": title, "requires": []}))
    assert out["ok"], out
    with kbc.connect_closing() as conn:
        row = conn.execute("SELECT placement_policy FROM tasks WHERE id=?", (out["task_id"],)).fetchone()
        assert row[0] == "worker-first-v1"
    assert out["placement"]["placement_policy"] == "worker-first-v1"
    assert out["placement"]["why"] == "waiting_for_worker_capacity"
    assert out["runs_where"] == "worker pool (waiting_for_worker_capacity; no local fallback)"
    assert not mac["calls"]


# --- the hub (no route entry) sends portable heavy work to the worker ---------------------------------------
# Gap: heavy_route was only read after a route target existed, so the hub (routeless) kept every heavy card
# on itself and worker-1's explicit pickup never saw one.

HUB_HEAVY = {"route": {"macbook": "hub"}, "heavy_route": {"hub": "worker-1"}}


def _hub(args, **kw):
    kw.setdefault("cfg", HUB_HEAVY)
    kw.setdefault("self_gw", "hub")
    kw.setdefault("machine_ok", PICKS_UP)
    return decide(args, **kw)


@pytest.mark.parametrize("args", [
    {"title": "Run the regression tests for the parser"},
    {"title": "CI red", "body": "Reproduce with scripts/run_tests.sh tests/hermes_cli/ -j 4"},
    {"title": "Benchmark the tokenizer", "requires": ["terminal", "file", "git"]},
    {"title": "Run the benchmark", "parents": ["t_1"]},
])
def test_hub_origin_portable_heavy_work_runs_on_the_worker(args):
    d = _hub(args)
    assert d["where"] == "local" and d["runs_on"] == ["machine:worker-1"] and "defer" not in d, d
    assert d["why"].startswith("heavy work")


@pytest.mark.parametrize("args", [
    {"title": "Light summary of CI failures"},                                  # not heavy
    {"title": "Run the regression tests on hermes-hub"},                        # names the hub host
    {"title": "pytest the hub", "body": "read ~/.hermes/kanban.db while it runs"},   # host state
    {"title": "Run pytest", "body": "then edit the cron jobs"},                 # cron control plane
    {"title": "Run pytest", "body": "use token-broker exec to fetch a key"},    # credentials broker
    {"title": "Run pytest", "workspace_kind": "dir", "workspace_path": "/home/ubuntu/code/x"},
    {"title": "Run pytest", "workspace_kind": "worktree", "workspace_path": "/home/ubuntu/code/x"},
    {"title": "Run pytest", "board": "links"},
    {"title": "Run pytest", "requires": ["host_repo"]},                          # checkout on this host
    {"title": "Run pytest", "requires": ["aws:acct:ec2-describe"]},             # gateway capability
    {"title": "Run pytest", "initial_status": "blocked"},
    {"title": "Run pytest", "project": "hermes"},
    {"title": "Run pytest", "assignee": "alpha"},
    {"title": "Fix flaky macOS test in the pytest suite"},                      # Mac-only
    {"title": "Run pytest", "skills": ["macos-performance-triage"]},
])
def test_hub_origin_host_specific_or_light_work_stays_on_the_hub(args):
    d = _hub(args)
    assert d["where"] == "local" and "runs_on" not in d, d


def test_hub_origin_heavy_work_stays_when_the_worker_does_not_pick_up_or_is_not_configured():
    assert "runs_on" not in _hub({"title": "Run pytest"}, machine_ok=lambda name: False)
    assert "runs_on" not in _hub({"title": "Run pytest"}, cfg={"route": {"macbook": "hub"}})
    assert "runs_on" not in _hub({"title": "Run pytest"}, cfg={"heavy_route": {"macbook": "worker-1"}})


def test_hub_origin_explicit_pins_and_courier_cards_win_over_heavy_routing():
    assert "runs_on" not in _hub({"title": "Run pytest", "placement": "local"})
    assert "runs_on" not in _hub({"title": "Run pytest", "placement": "macbook"})
    assert "runs_on" not in _hub({"title": "Run pytest"}, origin_intent={"x": 1})


@pytest.mark.parametrize("body", [
    "Run pytest in ~/code/hermes-mods",
    "cd /home/alice/code/project then pytest",
])
def test_hub_origin_heavy_repo_work_goes_to_the_worker(body):
    """A repo path is not a host need (the worker provisions its own checkout); an existing ~/code/_wt
    worktree is, below."""
    assert _hub({"title": "Maintenance", "body": body})["runs_on"] == ["machine:worker-1"]


@pytest.mark.parametrize("body", [
    "cd /home/ubuntu/code/_wt/foo then pytest",
    "Run pytest in ~/.hermes/kanban/workspaces/t_x",
    "ssh ubuntu@mini then pytest",
    "Run pytest then restart the gateway",
    "Run pytest before restarting gateway",
    "Run pytest then hermes config set kanban.x y",
    "Run pytest then hermes config edit",
    "Run pytest then hermes config unset kanban.x",
])
def test_hub_origin_textual_host_operations_stay_local(body):
    d = _hub({"title": "Maintenance", "body": body})
    assert d["where"] == "local" and "runs_on" not in d, d


@pytest.mark.parametrize("kind", ["dir", "worktree"])
def test_hub_origin_workspace_kind_alone_stays_local(kind):
    d = _hub({"title": "Run pytest", "workspace_kind": kind})
    assert d["where"] == "local" and "runs_on" not in d, d


def test_hub_origin_gateway_need_even_if_carried_stays_local():
    need = "aws:acct:ec2-describe"
    cfg = {**HUB_HEAVY, "carried_requires": ["terminal", need]}
    assert _hub({"title": "Run pytest", "requires": ["terminal"]}, cfg=cfg)["runs_on"]
    d = _hub({"title": "Run pytest", "requires": [need]}, cfg=cfg)
    assert d["where"] == "local" and "runs_on" not in d, d


def test_hub_origin_extra_home_markers_extend_the_host_specific_list():
    cfg = {**HUB_HEAVY, "extra_home_markers": [r"\bsecret-lab\b"]}
    assert "runs_on" not in _hub({"title": "Run pytest in secret-lab"}, cfg=cfg)
    assert _hub({"title": "Run pytest in the open"}, cfg=cfg)["runs_on"] == ["machine:worker-1"]


# --- explicit pin to a live gateway that is not this gateway's route target ---------------------------
# From the hub (no route entry) placement='macbook' was refused "no placement route for this gateway", so
# kanban_create could not reach the MacBook at all. [daveey]

HUB_CFG = {"route": {"macbook": "hub"}}
NOT_LIVE = lambda target: f"{target}'s capability record is stale or missing"


def test_pin_to_a_live_gateway_without_a_route_entry_goes_there():
    d = decide({"title": "scratch", "placement": "macbook"}, cfg=HUB_CFG, self_gw="hub")
    assert d["where"] == "macbook", d


def test_pin_to_a_non_routed_gateway_goes_there_even_with_a_route_of_its_own():
    d = decide({"title": "scratch", "placement": "mini"})  # self macbook routes to hub; mini is another
    assert d["where"] == "mini", d


def test_pin_without_any_route_config_goes_to_a_live_gateway():
    assert decide({"title": "x", "placement": "macbook"}, cfg={}, self_gw="hub")["where"] == "macbook"


def test_pin_to_an_unknown_or_not_live_gateway_is_still_refused():
    d = decide({"title": "x", "placement": "nowhere"}, cfg=HUB_CFG, self_gw="hub", live=NOT_LIVE)
    assert d["where"] == "refuse" and d["pin"] == "nowhere" and "not live" in d["why"], d


@pytest.mark.parametrize("extra", [
    {"workspace_kind": "dir", "workspace_path": "/tmp/x"},
    {"board": "links"},
    {"assignee": "links"},
])
def test_foreign_pin_keeps_the_structural_refusals(extra):
    d = decide({"title": "x", "placement": "macbook", **extra}, cfg=HUB_CFG, self_gw="hub")
    assert d["where"] == "refuse" and d["pin"] == "macbook", d


def test_foreign_pin_with_parents_is_deferred_to_the_pinned_gateway():
    d = decide({"title": "x", "placement": "macbook", "parents": ["t_1"]}, cfg=HUB_CFG, self_gw="hub")
    assert d["where"] == "local" and d["defer"] == "macbook", d


def test_foreign_pin_for_an_origin_bound_card_is_not_forwarded():
    d = decide({"title": "x", "placement": "macbook"}, cfg=HUB_CFG, self_gw="hub", origin_intent="chat")
    assert d["where"] == "local", d


def _forwarded_card(mac, monkeypatch, conn):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    parent = kb.create_task(conn, title="Orchestrate", assignee="default")
    created = json.loads(kt._handle_create({"title": "Fix the flaky lint", "body": "Push it.", "parents": [parent],
                                            "session_id": "20261027_183000_abc123"}))
    kb.complete_task(conn, parent, summary="fanned out", force=True)
    kb.recompute_ready(conn)
    child = created["task_id"]
    assert _forwarder(conn, child, mac, monkeypatch).run() == "parked"
    mac["calls"].clear()
    return child


@pytest.mark.parametrize("rc", [0, 3])
def test_manual_completion_archives_forwarded_copy_online_or_queued(mac, monkeypatch, rc):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        mac["result"]["rc"] = rc
        assert kb.complete_task(conn, child, summary="Finished manually", force=True)
        assert kb.get_task(conn, child).status == "done"
        assert len(mac["calls"]) == 1
        argv = mac["calls"][0]
        assert argv[1:6] == ["request", "hub", "tasks", f"default/default/{HUB_TID}", "archive"]
        assert kpf.archive_forward_view(conn, child)["status"] == "pending"
        rid = argv[argv.index("--id") + 1]
        assert mac["hub"].execute("SELECT state FROM sent WHERE id=?", (rid,)).fetchone()[0] == "sent"
        mac["hub"].execute("UPDATE sent SET state='done' WHERE id=?", (rid,))
        mac["hub"].commit()
        assert kpf.archive_forward_view(conn, child)["status"] == "archived"
        assert not kb.complete_task(conn, child, summary="Again", force=True)
        assert len(mac["calls"]) == 1


@pytest.mark.parametrize("status", ["done", "archived"])
def test_manual_completion_preserves_terminal_remote_copy(mac, monkeypatch, status):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_wait_replica as replica
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        monkeypatch.setattr(replica, "check", lambda *a, **kw: (0, f"{HUB_TID} {status}"))
        assert kb.complete_task(conn, child, summary="Finished manually", force=True)
        assert mac["calls"] == []


@pytest.mark.parametrize("observation", ["offline", "stale", "exception", "wrong-id"])
def test_completion_queues_archive_when_observation_is_unknown(mac, monkeypatch, observation):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_wait_replica as replica
    from hermes_cli import kanban_wait_check as kwait
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        def observe(*a, **kw):
            if observation == "exception":
                raise OSError("unavailable")
            return {"offline": (2, "unavailable"), "stale": (3, f"{HUB_TID} done (stale)"),
                    "wrong-id": (0, "t_other done")}[observation]
        monkeypatch.setattr(replica, "check", observe)
        monkeypatch.setattr(kwait, "check_remote", lambda *a, **kw: pytest.fail("must not SSH on completion"))
        mac["result"]["rc"] = 3
        assert kb.complete_task(conn, child, summary="Manual override", force=True)
        assert len(mac["calls"]) == 1


def test_settle_completion_never_forwards_archive(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        f = _forwarder(conn, child, mac, monkeypatch, check=(0, f"{HUB_TID} done"))
        assert f.run() == "completed"
        assert kb.get_task(conn, child).status == "done"
        assert mac["calls"] == []
        assert kpf.archive_forward_view(conn, child) is None


def test_refused_and_plain_completion_do_not_forward(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        assert not kb.complete_task(conn, child, summary="Still scheduled")  # parked: only --force closes it
        plain = kb.create_task(conn, title="plain", assignee="default")
        kb.recompute_ready(conn)
        assert kb.complete_task(conn, plain, summary="Local only", force=True)
        assert mac["calls"] == []


def _cli(*argv: str) -> int:
    import argparse
    from hermes_cli import kanban as kc
    top = argparse.ArgumentParser()
    kc.build_parser(top.add_subparsers(dest="cmd"))
    return kc.kanban_command(top.parse_args(["kanban", *argv]))


@pytest.mark.parametrize("unblock_first", [False, True])
def test_cli_force_complete_of_parked_forwarded_card_archives_its_copy(mac, monkeypatch, capsys, unblock_first):
    """The real deferred-placement lifecycle (claim -> placement_forwarded with remote_task_id -> scheduled),
    then the operator's `hermes kanban complete --force`, from where the card sits (scheduled) or after an
    unblock (ready)."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        assert kb.get_task(conn, child).status == "scheduled"
        assert kpf.state(conn, child)["remote_task_id"] == HUB_TID
        if unblock_first:
            assert kb.unblock_task(conn, child)
    assert _cli("complete", child, "--force", "--summary", "Finished by hand") == 0, capsys.readouterr()
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "done"
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (child,))]
        assert "placement_archive_forwarded" in kinds, kinds
        assert kpf.archive_forward_view(conn, child)["status"] == "pending"
    assert [a[1:6] for a in _archive_requests(mac)] == [["request", "hub", "tasks", f"default/default/{HUB_TID}",
                                                          "archive"]]


def test_archiving_a_forwarded_card_archives_the_remote_copy(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
    out = json.loads(kt._handle_archive({"task_id": child}))
    assert out["ok"] and out["status"] == "archived", out
    argv = mac["calls"][0]
    assert argv[1:6] == ["request", "hub", "tasks", f"default/default/{HUB_TID}", "archive"], argv
    assert out["placed_copy"] == {"gateway": "hub", "task_id": HUB_TID, "status": "pending"}, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "archived"
        assert [e["status"] for e in kpf._archive_events(conn, child)] == ["pending"]
    # The hub client applied it: the view now says archived, and a second archive sends nothing new.
    rid = argv[argv.index("--id") + 1]
    mac["hub"].execute("UPDATE sent SET state='done' WHERE id=?", (rid,))
    mac["hub"].commit()
    with kbc.connect_closing() as conn:
        assert kpf.archive_forward_view(conn, child)["status"] == "archived"
        assert kpf.forward_archive(conn, child)["status"] == "archived"
    assert len(mac["calls"]) == 1


def test_archive_forward_is_queued_when_the_target_is_offline(mac, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
    mac["result"]["rc"] = 3  # hub unreachable: the client queues the request durably
    out = json.loads(kt._handle_archive({"task_id": child}))
    assert out["ok"] and out["placed_copy"]["status"] == "pending", out
    rid = mac["calls"][0][mac["calls"][0].index("--id") + 1]
    assert mac["hub"].execute("SELECT state FROM sent WHERE id=?", (rid,)).fetchone()[0] == "sent"
    with kbc.connect_closing() as conn:
        assert kpf.archive_forward_view(conn, child)["status"] == "pending"
    mac["hub"].execute("UPDATE sent SET state='done' WHERE id=?", (rid,))  # reconnected, target applied it
    mac["hub"].commit()
    with kbc.connect_closing() as conn:
        assert kpf.archive_forward_view(conn, child)["status"] == "archived"


def test_archive_forward_failure_is_reported_and_retried_and_plain_cards_are_untouched(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        plain = kb.create_task(conn, title="plain", assignee="default")
    mac["result"]["rc"] = 1
    out = json.loads(kt._handle_archive({"task_id": child}))
    assert out["ok"] and out["placed_copy"]["status"] == "failed" and out["placed_copy"]["error"], out
    mac["result"]["rc"] = 0
    with kbc.connect_closing() as conn:
        assert kpf.forward_archive(conn, child)["status"] == "pending"  # a failed forward is sent again
    mac["calls"].clear()
    assert "placed_copy" not in json.loads(kt._handle_archive({"task_id": plain}))
    assert mac["calls"] == []


def _unresolved_forwarded_card(mac, monkeypatch, conn):
    """A deferred card whose create request was sent but the target has not made the card yet."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    parent = kb.create_task(conn, title="Orchestrate", assignee="default")
    created = json.loads(kt._handle_create({"title": "Fix the flaky lint", "body": "Push it.", "parents": [parent],
                                            "session_id": "20261027_183000_abc123"}))
    kb.complete_task(conn, parent, summary="fanned out", force=True)
    kb.recompute_ready(conn)
    child = created["task_id"]
    assert _forwarder(conn, child, mac, monkeypatch, await_tid=None).run() == "requeued"
    st = kpf.state(conn, child)
    assert st["request_id"] and not st.get("remote_task_id"), st
    mac["calls"].clear()
    return child, st["request_id"]


def _archive_requests(mac):
    return [a for a in mac["calls"] if a[1] == "request" and "archive" in a]


def test_archive_waits_briefly_for_the_remote_id_and_sends_the_archive(mac, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        child, _rid = _unresolved_forwarded_card(mac, monkeypatch, conn)
    # The target makes the card while the archive waits for it (``hermes-hub outcomes``).
    mac["result"].update(top_help="{sync,requests,create-task,outcomes}", apply_on="outcomes")
    out = json.loads(kt._handle_archive({"task_id": child}))
    assert out["placed_copy"] == {"gateway": "hub", "task_id": "t_hub0001", "status": "pending"}, out
    assert _archive_requests(mac)[0][1:6] == ["request", "hub", "tasks", "default/default/t_hub0001", "archive"]


def test_unresolved_archive_is_sent_by_the_dispatcher_sweep_once_the_target_makes_the_card(mac, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_owner_archive_relay as oar
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    monkeypatch.setattr(kpf, "ARCHIVE_AWAIT_BUDGET", 0.0)
    with kbc.connect_closing() as conn:
        child, rid = _unresolved_forwarded_card(mac, monkeypatch, conn)
    out = json.loads(kt._handle_archive({"task_id": child}))
    assert out["ok"] and out["placed_copy"]["status"] == "unresolved", out
    assert _archive_requests(mac) == []
    # Tick while the target still has no card: nothing is sent and nothing is recorded.
    with kbc.connect_closing() as conn:
        assert oar.apply_remote_archives(conn) == []
        assert [e["status"] for e in kpf._archive_events(conn, child)] == ["unresolved"]
    assert _archive_requests(mac) == []
    # The target makes the card; its result reaches the replica. The next tick sends the archive.
    mac["hub"].execute("UPDATE sent SET state='done', result=? WHERE id=?",
                       (json.dumps({"ok": True, "task_id": "t_hub0001", "status": "ready"}), rid))
    mac["hub"].commit()
    with kbc.connect_closing() as conn:
        oar.apply_remote_archives(conn)
        assert [e["status"] for e in kpf._archive_events(conn, child)] == ["unresolved", "pending"]
        assert kpf.archive_forward_view(conn, child) == {"gateway": "hub", "task_id": "t_hub0001",
                                                         "status": "pending"}
    sent = _archive_requests(mac)
    assert len(sent) == 1 and sent[0][1:6] == ["request", "hub", "tasks", "default/default/t_hub0001", "archive"]
    # Later ticks send nothing more.
    with kbc.connect_closing() as conn:
        oar.apply_remote_archives(conn)
    assert len(_archive_requests(mac)) == 1


def test_request_only_completion_never_waits_and_sweep_cancels_late_copy(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child, rid = _unresolved_forwarded_card(mac, monkeypatch, conn)
        assert kb.unblock_task(conn, child)
        monkeypatch.setattr(kp, "await_task_id", lambda *a, **kw: pytest.fail("completion must not wait"))
        assert kb.complete_task(conn, child, summary="Manual override", force=True)
        assert kb.get_task(conn, child).status == "done"
        assert kpf.archive_forward_view(conn, child)["status"] == "unresolved"
        assert kpf.sweep_unresolved_archives(conn) == []
        mac["hub"].execute("UPDATE sent SET state='done', result=? WHERE id=?",
                           (json.dumps({"ok": True, "task_id": "t_hub0001", "status": "ready"}), rid))
        mac["hub"].commit()
        assert kpf.sweep_unresolved_archives(conn) == [child]
        assert kpf.archive_forward_view(conn, child)["status"] == "pending"
        assert kpf.sweep_unresolved_archives(conn) == []
    assert len(_archive_requests(mac)) == 1


def test_unresolved_archive_sweep_gives_up_after_seven_days_with_a_comment(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    monkeypatch.setattr(kpf, "ARCHIVE_AWAIT_BUDGET", 0.0)
    with kbc.connect_closing() as conn:
        child, _rid = _unresolved_forwarded_card(mac, monkeypatch, conn)
    kt._handle_archive({"task_id": child})
    with kbc.connect_closing() as conn:
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + 6 * 86400) == []
        assert [e["status"] for e in kpf._archive_events(conn, child)] == ["unresolved"]
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + 7 * 86400 + 60) == []
        assert [e["status"] for e in kpf._archive_events(conn, child)] == ["unresolved", "expired"]
        assert any("gave up" in c.body for c in kb.list_comments(conn, child))
        assert kpf.archive_forward_view(conn, child)["status"] == "expired"
        kpf.sweep_unresolved_archives(conn, now=time.time() + 30 * 86400)  # not swept again
        assert len(kpf._archive_events(conn, child)) == 2
    assert _archive_requests(mac) == []


def test_just_forwarded_card_the_replica_has_not_synced_parks_on_the_card_not_the_clock(mac, monkeypatch):
    """confirm_remote says ``not found in <gw> replica`` right after a forward: retry, then a card-wait park."""
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    calls = []

    def unsynced(awaiting):
        calls.append(awaiting["ref"])
        raise kaw.AwaitingError(kaw.INVALID_AWAITING, f"cannot confirm producer card {awaiting['ref']}: not found")

    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Orchestrate", assignee="default")
        created = json.loads(kt._handle_create({"title": "Fix the flaky lint", "body": "Push it.", "parents": [parent],
                                                "session_id": "20261027_183000_abc123"}))
        kb.complete_task(conn, parent, summary="fanned out", force=True)
        kb.recompute_ready(conn)
        child = created["task_id"]
        f = _forwarder(conn, child, mac, monkeypatch)
        monkeypatch.setattr(kaw, "confirm_remote", unsynced)
        f.sleep = lambda s: None
        assert f.run() == "parked"
        assert len(calls) == 3  # first try + 2 retries
        task = kb.get_task(conn, child)
        assert task.status == "scheduled"
        aw, _ = kaw._latest_park(conn, child)
        assert aw["kind"] == "card" and aw["ref"] == HUB_TID and aw["gateway"] == "hub", aw


def test_archived_producer_is_not_parked_on(mac, monkeypatch):
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    def archived(awaiting):
        raise kaw.AwaitingError(kaw.AGENT_ACTION_WAIT, "the producer card is not live")

    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Orchestrate", assignee="default")
        created = json.loads(kt._handle_create({"title": "Fix the flaky lint", "body": "Push it.", "parents": [parent],
                                                "session_id": "20261027_183000_abc123"}))
        kb.complete_task(conn, parent, summary="fanned out", force=True)
        kb.recompute_ready(conn)
        f = _forwarder(conn, created["task_id"], mac, monkeypatch)
        monkeypatch.setattr(kaw, "confirm_remote", archived)
        with pytest.raises(kaw.AwaitingError):
            f.run()


@pytest.mark.parametrize("loop", ["queued", "refused_no_fallback", "step_failed"])
def test_placement_requeue_loops_escalate_on_the_third_requeue(mac, monkeypatch, loop):
    """The forwarder's requeue loops park on a declared clock (``_park_clock``). Declared clocks are exempt from the
    repeat-park escalation for agents, but these system loops pass ``count_repeats``: the third requeue goes to an
    owner card instead of re-queueing every 10 min forever. The per-attempt detail in the why (an exception text)
    does not split the premise. [daveey]"""
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    if loop == "refused_no_fallback":  # a qualified origin's card: no local fallback, so _cancel re-parks
        real_state = kpf.state
        monkeypatch.setattr(kpf, "state", lambda c, t: {**(real_state(c, t) or {}), "origin_gateway": "elsewhere"})
    actions = []
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        for n in range(3):
            f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
            if loop == "queued":
                actions.append(f.run())
            elif loop == "refused_no_fallback":
                actions.append(f._cancel(f"hub refused the card: attempt {n}"))
            else:
                actions.append(f._park_clock(f"forward step failed (RuntimeError: timeout {n}); retrying"))
            if n < 2:
                assert kb.get_task(conn, child).status == "scheduled", (n, actions)
                kb.unblock_task(conn, child)
                kb.recompute_ready(conn)
        kinds = [e.kind for e in kb.list_events(conn, child)]
        assert kinds.count("scheduled") == 2 and kaw.ESCALATED_EVENT in kinds, (actions, kinds)
        assert kb.get_task(conn, child).status == "todo"


# A hub refusal or exception text quoted in a requeue park reads like an agent wall ("permission denied", "waiting
# for David"); the wall check refused the park, main()'s retry raised again and ran a qualified-origin card here.
WALL_TEXT = "refused: permission denied, needs access granted; waiting for David to approve the profile"


@pytest.mark.parametrize("loop", ["not_live", "refused_no_fallback", "step_failed"])
def test_placement_requeue_parks_whatever_the_quoted_text_says(mac, monkeypatch, loop):
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    assert kaw.text_wall(WALL_TEXT)  # the text an agent park would be refused for
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    if loop == "refused_no_fallback":
        real_state = kpf.state
        monkeypatch.setattr(kpf, "state", lambda c, t: {**(real_state(c, t) or {}), "origin_gateway": "elsewhere"})
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        f = _forwarder(conn, child, mac, monkeypatch, await_tid=None)
        if loop == "not_live":
            f.live = lambda t: WALL_TEXT
            action = f.run()
        elif loop == "refused_no_fallback":
            action = f._cancel(f"hub refused the card: {WALL_TEXT}")
        else:
            action = f._park_clock(f"forward step failed (RuntimeError: {WALL_TEXT}); retrying")
        assert action == "requeued", action
        assert kb.get_task(conn, child).status == "scheduled"


@pytest.mark.parametrize("shape", ["qualified_origin", "already_sent"])
def test_main_never_runs_a_qualified_or_sent_card_locally_when_the_requeue_fails(mac, monkeypatch, shape):
    """main(): the forward step raises and the requeue raises too. A card with no local fallback (qualified origin)
    or already sent is held (blocked, agent-owned), never exec'd as a local worker. [daveey]"""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    real_state = kpf.state
    extra = {"origin_gateway": "elsewhere"} if shape == "qualified_origin" else {"request_id": "rq-1"}
    monkeypatch.setattr(kpf, "state", lambda c, t: {**(real_state(c, t) or {}), **extra})
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    def boom(self, *a, **kw):
        raise RuntimeError(f"hub said: {WALL_TEXT}")
    monkeypatch.setattr(kpf.Forwarder, "run", boom)
    monkeypatch.setattr(kpf.Forwarder, "_park_clock", boom)
    monkeypatch.setattr(kpf.os, "execvp", lambda *a: pytest.fail("ran the card locally"))
    assert kpf.main(["--", "hermes", "worker"]) == 0
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, child)
        assert task.status in ("blocked", "triage") and task.block_kind == "transient", (task.status, task.block_kind)
        blocked = [e for e in kb.list_events(conn, child) if e.kind == "blocked"]
        assert blocked and "must not run here" in blocked[-1].payload["reason"]


def test_main_still_falls_back_for_an_unsent_local_card_when_cancel_fails(mac, monkeypatch):
    """Control: a card never sent and with a local fallback still runs here when the forward and its cancel fail."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from tools import kanban_tools as kt
    with kbc.connect_closing() as conn:
        parent = _done_parent(kb, conn)
    child = json.loads(kt._handle_create({"title": "Fix the flaky lint", "parents": [parent]}))["task_id"]
    with kbc.connect_closing() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, child)
    monkeypatch.setenv("HERMES_KANBAN_TASK", child)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    def boom(self, *a, **kw):
        raise RuntimeError("transport down")
    monkeypatch.setattr(kpf.Forwarder, "run", boom)
    monkeypatch.setattr(kpf.Forwarder, "_cancel", boom)
    ran = []
    monkeypatch.setattr(kpf.os, "execvp", lambda *a: ran.append(a))
    assert kpf.main(["--", "hermes", "worker"]) == 1 and ran
