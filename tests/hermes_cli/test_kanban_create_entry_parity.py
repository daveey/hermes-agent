"""Every way an agent can create a Kanban card links it to the calling chat. [daveey]

The chat's task strip and sidebar badge list exactly ``session_tasks(<lineage ids>)``.
A card that misses the chat there is invisible while the chat says it is running.
Each entry path got the link separately (tool; CLI from the terminal in 0067/0068;
CLI from execute_code in 0111) and the execute_code path shipped broken while its
doc claimed it worked. This test drives all of them with the env each tool really
builds, so a new or regressed path fails here instead of in a user's chat.
Add a case here whenever a new way to create cards appears.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pytest

import gateway.session_context as sc
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
SID = "20261016_185414_c283c2"
_SESSION_ENV = ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_SESSION_SOURCE",
                "HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_RPC_SOCKET",
                "HERMES_KERNEL_SENTINEL", "HERMES_KANBAN_TASK")


@pytest.fixture
def chat(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in _SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    ksa._reset_cache_for_tests()
    kb.init_db()
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=SID, source="desktop")
    finally:
        db.close()
    tokens = sc.set_session_vars(session_id=SID, session_key=SID, source="desktop")
    yield home
    sc.clear_session_vars(tokens)


def _cli_create(title: str) -> str:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", title])) == 0
    with kbc.connect_closing() as conn:
        return next(t for t in kb.list_tasks(conn) if t.title == title).id


def _run_cli_in(env: dict, title: str, monkeypatch) -> str:
    """Run the CLI as the child process would: os.environ = the env the tool built.
    The CLI is a separate process, so the parent's ContextVars must not leak in."""
    for var in _SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    for name, value in env.items():
        if name.startswith("HERMES_SESSION_") or name in ("HERMES_RPC_SOCKET", "HERMES_KERNEL_SENTINEL"):
            monkeypatch.setenv(name, value)
    tokens = [var.set(sc._UNSET) for var in sc._VAR_MAP.values()]
    engaged = sc._session_context_engaged
    sc._session_context_engaged = False
    try:
        return _cli_create(title)
    finally:
        sc._session_context_engaged = engaged
        for var, tok in zip(sc._VAR_MAP.values(), tokens):
            var.reset(tok)


def _via_tool(title: str, monkeypatch) -> str:
    from tools.kanban_tools import _handle_create
    out = json.loads(_handle_create({"title": title, "assignee": "default"}))
    return out["task_id"]


def _via_terminal(title: str, monkeypatch) -> str:
    from tools.environments.local import _inject_session_context_env
    env: dict = {}
    _inject_session_context_env(env)
    return _run_cli_in(env, title, monkeypatch)


def _via_execute_code(title: str, monkeypatch) -> str:
    from tools.code_execution_env import _build_child_env
    env = _build_child_env(rpc_endpoint="sock", rpc_token="tok", tmpdir="/tmp/hermes-test",
                           child_python=sys.executable)
    env.setdefault("HERMES_RPC_SOCKET", "sock")
    return _run_cli_in(env, title, monkeypatch)


def _via_origin_courier(title: str, monkeypatch) -> str:
    import io
    from hermes_constants import get_hermes_home
    from tools import kanban_tools
    home = get_hermes_home()
    cfg = home / 'mail.json'
    cfg.write_text(json.dumps({'gateway': 'owner'}))
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(cfg))
    monkeypatch.setenv('HERMES_HUB_STATE', str(home / 'hub'))
    monkeypatch.setattr(kanban_tools, '_profile_has_kanban_toolset', lambda: True)
    request = {'id': 'request-parity-001', 'from': 'gw:origin', 'to_gw': 'owner',
               'coll': 'tasks', 'key': 'default/default', 'action': 'create',
               'args': {'origin': {'gateway': 'origin', 'profile': 'default', 'session': 'original'},
                        'create': {'title': title, 'assignee': 'default', 'session_id': SID}}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    root = argparse.ArgumentParser(prog='hermes')
    kc.build_parser(root.add_subparsers())
    assert kc.kanban_command(root.parse_args(['kanban', 'origin-create', '--request-file', '-'])) == 0
    with kbc.connect_closing() as conn:
        return next(t for t in kb.list_tasks(conn) if t.title == title).id


PATHS = {"origin courier CLI": _via_origin_courier,
         "kanban_create tool": _via_tool, "terminal: hermes kanban create": _via_terminal,
         "execute_code: hermes kanban create": _via_execute_code}


@pytest.mark.parametrize("path", list(PATHS))
def test_card_shows_in_the_creating_chats_task_list(chat, monkeypatch, capsys, path):
    task_id = PATHS[path](f"card via {path}", monkeypatch)
    ksa._reset_cache_for_tests()
    listed = {t["id"] for t in ksa.session_tasks([SID])}
    assert task_id in listed, f"{path}: card {task_id} is not in chat {SID}'s task list"
    assert "not linked to any chat" not in capsys.readouterr().err


def test_agent_cli_create_without_a_known_chat_is_refused(chat, monkeypatch, capsys):
    # e.g. execute_code on another host / another profile: agent env, but no known session. The card would have no
    # scope authority, so it is refused instead of created orphaned with a "not linked to any chat" warning (req. D).
    for var in _SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HERMES_RPC_SOCKET", "sock")
    monkeypatch.setenv("HERMES_SESSION_ID", "not_a_real_session")
    tokens = [var.set(sc._UNSET) for var in sc._VAR_MAP.values()]
    engaged = sc._session_context_engaged
    sc._session_context_engaged = False
    try:
        root = argparse.ArgumentParser(prog="hermes")
        kc.build_parser(root.add_subparsers())
        assert kc._cmd_create(root.parse_args(["kanban", "create", "orphan"])) == 2
    finally:
        sc._session_context_engaged = engaged
        for var, tok in zip(sc._VAR_MAP.values(), tokens):
            var.reset(tok)
    assert "scope_authority_missing" in capsys.readouterr().err
    with kbc.connect_closing() as conn:
        assert not [t for t in kb.list_tasks(conn) if t.title == "orphan"]


def test_plain_user_shell_create_does_not_warn(chat, monkeypatch, capsys):
    _run_cli_in({}, "from my own shell", monkeypatch)
    assert "not linked to any chat" not in capsys.readouterr().err


def test_suggested_notify_subscribe_links_the_orphan(chat, monkeypatch, capsys):
    # An orphan can still exist (internal/legacy creation); the CLI no longer makes one for an agent (req. D).
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="orphan to fix", assignee="default")
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "notify-subscribe", task_id, "--platform", "tui", "--chat-id", SID])
    assert kc._cmd_notify_subscribe(args) == 0
    ksa._reset_cache_for_tests()
    assert task_id in {t["id"] for t in ksa.session_tasks([SID])}


@pytest.fixture
def restricted_creator(chat, monkeypatch):
    profile = chat / "profiles" / "restricted"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("platform_toolsets:\n  cli: [web]\nterminal:\n  backend: local\n")
    from tools import kanban_tools
    monkeypatch.setattr(kanban_tools, "_persisted_identity", lambda: "restricted")
    monkeypatch.setattr(kc, "_profile_author", lambda: "restricted")
    monkeypatch.setattr(kc, "_agent_invoked", lambda: True)
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))


@pytest.mark.parametrize("surface", ["registry", "cli"])
def test_create_requires_and_default_creator_parity(restricted_creator, surface, monkeypatch):
    if surface == "registry":
        from tools.registry import registry
        schema = registry.get_schema("kanban_create")
        items = schema["parameters"]["properties"]["requires"]["items"]
        # Profile tool names plus namespaced gateway capability needs (cross-gateway routing).
        assert re.fullmatch(items["pattern"], "kanban") and re.fullmatch(items["pattern"], "ssh:mini")
        assert not re.fullmatch(items["pattern"], "bogus")
        good = json.loads(registry.dispatch("kanban_create", {"title": "Allowed", "requires": []}))
        assert good.get("task_id"), good
        bad = json.loads(registry.dispatch("kanban_create", {"title": "Denied", "requires": ["terminal"]}))
        assert bad["code"] == "assignee_capability_mismatch", bad
    else:
        monkeypatch.setenv("HERMES_SESSION_ID", SID)  # a registered chat: an agent create needs scope authority
        root = argparse.ArgumentParser()
        kc.build_parser(root.add_subparsers())
        assert kc._cmd_create(root.parse_args(["kanban", "create", "Allowed"])) == 0
        assert kc._cmd_create(root.parse_args(["kanban", "create", "Denied", "--requires", "terminal"])) == 2
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
        assert len(tasks) == 1
        assert tasks[0].assignee == "restricted"
        assert tasks[0].requires == []


def test_rest_create_preserves_and_enforces_requires(restricted_creator):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        denied = client.post("/tasks", json={"title": "Denied", "assignee": "restricted", "requires": ["terminal"]})
        assert denied.status_code == 400, denied.text
        allowed = client.post("/tasks", json={"title": "Draft", "requires": ["terminal"]})
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["task"]["requires"] == ["terminal"]
        tid = allowed.json()["task"]["id"]
        rejected = client.patch(f"/tasks/{tid}", json={"assignee": "restricted"})
        assert rejected.status_code == 400, rejected.text
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, tid).assignee is None


def test_decomposition_root_assignment_is_validated_atomically(restricted_creator):
    from hermes_cli.kanban_capabilities import CapabilityMismatchError
    from hermes_cli.kanban_db_graph import decompose_triage_task
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Draft", triage=True, requires=["terminal"])
        with pytest.raises(CapabilityMismatchError):
            decompose_triage_task(conn, tid, root_assignee="restricted", children=[{"title": "Safe", "assignee": "restricted"}])
        assert len(kb.list_tasks(conn)) == 1
        assert kb.get_task(conn, tid).status == "triage"


def test_single_task_promotion_checks_assignment(restricted_creator):
    from hermes_cli.kanban_capabilities import CapabilityMismatchError
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Draft", triage=True, requires=["terminal"])
        with pytest.raises(CapabilityMismatchError):
            kb.specify_triage_task(conn, tid, title="Specified", assignee="restricted")
        task = kb.get_task(conn, tid)
        assert (task.title, task.status, task.assignee) == ("Draft", "triage", None)


def test_single_decomposer_returns_rejected_outcome(restricted_creator, monkeypatch):
    from hermes_cli import kanban_decompose as decompose
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Draft", triage=True, requires=["terminal"])
    routing = decompose._Routing("restricted", "restricted", True, [], {"restricted"})
    monkeypatch.setattr(decompose, "_load_routing", lambda **kw: routing)
    monkeypatch.setattr(decompose, "_call_aux", lambda *a, **kw: (
        json.dumps({"fanout": False, "title": "Specified", "assignee": "restricted"}), ""))
    outcome = decompose.decompose_task(tid)
    assert not outcome.ok
    assert "lacks required capabilities" in outcome.reason
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"


@pytest.mark.parametrize("surface", ["tool", "cli", "rest"])
def test_every_create_path_refuses_a_pin_the_managed_registry_rejects(chat, monkeypatch, surface):
    """Concrete selectors are refused before insert, even if registry validation once handled them."""
    from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
    (chat / "config.yaml").write_text("model:\n  default: claude-sonnet-5-5\n  provider: anthropic\n" + _managed_yaml())
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))
    if surface == "tool":
        from tools.registry import registry
        bad = json.loads(registry.dispatch("kanban_create", {"title": "Bad", "assignee": "default",
                                                             "model": "claude-sonnet-4-5"}))
        assert "Kanban is model-neutral" in bad.get("error", ""), bad
    elif surface == "cli":
        root = argparse.ArgumentParser()
        kc.build_parser(root.add_subparsers())
        assert kc._cmd_create(root.parse_args(["kanban", "create", "Bad", "--assignee", "default",
                                               "--model", "claude-sonnet-4-5"])) == 2
    else:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from plugins.kanban.dashboard.plugin_api import router
        app = FastAPI()
        app.include_router(router)
        with TestClient(app) as client:
            bad = client.post("/tasks", json={"title": "Bad", "assignee": "default",
                                              "model_override": "claude-sonnet-4-5"})
            assert bad.status_code == 400 and "Kanban is model-neutral" in bad.text, bad.text
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_placed_tool_create_is_overlap_checked_before_it_leaves(chat, tmp_path, monkeypatch):
    """[daveey] A card the placement route sends to another gateway is checked against that gateway's open cards
    (hermes-hub replica) first: a duplicate never reaches the courier; distinct work still does."""
    import sqlite3

    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_placement as kp
    from tools.registry import registry
    hub_db = tmp_path / "hub-state" / "hub.db"
    hub_db.parent.mkdir(parents=True)
    db = sqlite3.connect(hub_db)
    db.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, data TEXT)")
    db.execute("INSERT INTO replica VALUES ('hub', 'tasks', 'default/research-x/t_live', ?)", (json.dumps(
        {"id": "t_live", "board": "research-x", "gateway": "hub", "status": "running",
         "title": "Stage B1: tuning matrix on rented GPUs", "sessions": []}),))
    db.commit()
    db.close()
    monkeypatch.setenv("HERMES_HUB_STATE", str(hub_db.parent))
    monkeypatch.setattr(reg, "self_gateway", lambda: "macbook")
    monkeypatch.setattr(kp, "decide", lambda view, **kw: {"where": "hub", "why": "routed"})
    sent = []
    monkeypatch.setattr(kp, "create_remote", lambda view, **kw: sent.append(view["title"]) or
                        {"ok": True, "request_id": "r1", "queued_only": True})
    dup = json.loads(registry.dispatch("kanban_create", {
        "title": "Stage B: run it", "body": "Execute Stage B for Metta-AI/research-x.", "assignee": "default",
        "requires": []}))
    assert dup["code"] == "create_overlap" and [m["id"] for m in dup["matches"]] == ["t_live"], dup
    ok = json.loads(registry.dispatch("kanban_create", {"title": "Rotate the Telnyx key", "assignee": "default",
                                                        "requires": []}))
    assert ok["status"] == "queued" and ok["placed"]["gateway"] == "hub", ok
    assert sent == ["Rotate the Telnyx key"]


_LIVE_TITLE = "Rotate the Telnyx webhook signing key"
_DUP_TITLE = "Rotate Telnyx webhook signing key now"


def _open_owner(key: str | None = None) -> str:
    """An open card another chat created on this gateway (the existing owner)."""
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title=_LIVE_TITLE, assignee="default", session_id="20261027_000000_other0",
                              idempotency_key=key)


def _cli(argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(kc, "_check_dispatcher_presence", lambda **kw: (True, ""))
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    return kc._cmd_create(root.parse_args(["kanban", "create", *argv]))


def _rest(payload: dict):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from plugins.kanban.dashboard.plugin_api import router
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        return client.post("/tasks", json=payload)


def _titles() -> list[str]:
    with kbc.connect_closing() as conn:
        return sorted(t.title for t in kb.list_tasks(conn))


def _override_events(task_id: str) -> list[dict]:
    with kbc.connect_closing() as conn:
        rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'create_overlap'",
                            (task_id,)).fetchall()
    return [json.loads(r[0]) for r in rows]


def test_cli_create_refuses_a_duplicate_owner(chat, monkeypatch, capsys):
    """[daveey] `hermes kanban create` runs the same create_overlap check as the kanban_create tool."""
    live = _open_owner()
    assert _cli([_DUP_TITLE, "--assignee", "default", "--json"], monkeypatch) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "create_overlap" and [m["id"] for m in out["matches"]] == [live], out
    assert _titles() == [_LIVE_TITLE]
    assert _cli([_DUP_TITLE, "--assignee", "default"], monkeypatch) == 2
    err = capsys.readouterr().err
    assert "create_overlap" in err and live in err and f"--distinct-from {live}" in err, err
    assert _titles() == [_LIVE_TITLE]


def test_cli_create_override_is_accepted_and_recorded(chat, monkeypatch, capsys):
    live = _open_owner()
    # distinct_from without a reason is still refused
    assert _cli([_DUP_TITLE, "--assignee", "default", "--distinct-from", live], monkeypatch) == 2
    capsys.readouterr()
    assert _cli([_DUP_TITLE, "--assignee", "default", "--json", "--distinct-from", live,
                 "--distinct-reason", "different key: the staging one"], monkeypatch) == 0
    new = json.loads(capsys.readouterr().out)["id"]
    assert _titles() == sorted([_LIVE_TITLE, _DUP_TITLE])
    [ev] = _override_events(new)
    assert ev["outcome"] == "override" and ev["reason"] == "different key: the staging one", ev


def test_cli_create_with_idempotency_key_skips_its_own_family(chat, monkeypatch, capsys):
    """Automated creators (coach / self-heal crons) dedupe by --idempotency-key: a retry returns the existing
    card, and an earlier open card from the same key family (text before the first ':') never blocks a new one."""
    live = _open_owner("coach-fix:telnyx-a")
    assert _cli([_LIVE_TITLE, "--assignee", "default", "--json", "--idempotency-key", "coach-fix:telnyx-a"],
                monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["id"] == live
    assert _cli([_DUP_TITLE, "--assignee", "default", "--json", "--idempotency-key", "coach-fix:telnyx-b"],
                monkeypatch) == 0
    capsys.readouterr()
    assert _cli(["Rotate Telnyx webhook signing key today", "--assignee", "default", "--json",
                 "--idempotency-key", "other-watch:1"], monkeypatch) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "create_overlap"


def test_cli_create_never_matches_its_parent(chat, monkeypatch, capsys):
    live = _open_owner()
    assert _cli([_DUP_TITLE, "--assignee", "default", "--parent", live], monkeypatch) == 0


def test_rest_create_refuses_a_duplicate_owner(chat):
    """[daveey] The dashboard/REST POST /tasks runs the same create_overlap check as the kanban_create tool."""
    live = _open_owner()
    r = _rest({"title": _DUP_TITLE, "assignee": "default"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "create_overlap" and [m["id"] for m in detail["matches"]] == [live], detail
    assert _titles() == [_LIVE_TITLE]


@pytest.mark.parametrize("surface", ["tool", "cli", "rest"])
@pytest.mark.parametrize("historical", [False, True])
def test_mirror_singleton_refused_on_every_create_path(chat, monkeypatch, capsys, surface, historical):
    from tests.tools.test_kanban_create_overlap_singleton import MISSED, NEW, OPEN_TITLE
    if historical:
        NEW = MISSED[0]
    with kbc.connect_closing() as conn:
        live = kb.create_task(conn, title=OPEN_TITLE, assignee="default", session_id="other-chat",
                              idempotency_key="core-mirror:a")
    payload = {"title": NEW, "assignee": "default", "requires": [], "idempotency_key": "core-mirror:b"}
    if surface == "tool":
        from tools import kanban_tools  # registers the tool for standalone selection
        from tools.registry import registry
        out = json.loads(registry.dispatch("kanban_create", payload))
    elif surface == "cli":
        assert _cli([NEW, "--assignee", "default", "--json", "--idempotency-key", "core-mirror:b"],
                    monkeypatch) == 2
        out = json.loads(capsys.readouterr().out)
    else:
        response = _rest(payload)
        assert response.status_code == 409, response.text
        out = response.json()["detail"]
    assert out["code"] == "create_overlap" and [m["id"] for m in out["matches"]] == [live], out
    assert _titles() == [OPEN_TITLE]


def test_rest_create_override_is_accepted_and_recorded(chat):
    live = _open_owner()
    r = _rest({"title": _DUP_TITLE, "assignee": "default", "distinct_from": [live]})
    assert r.status_code == 409, r.text
    r = _rest({"title": _DUP_TITLE, "assignee": "default", "distinct_from": [live],
               "distinct_reason": "the staging key, not prod"})
    assert r.status_code == 200, r.text
    new = r.json()["task"]["id"]
    assert _titles() == sorted([_LIVE_TITLE, _DUP_TITLE])
    [ev] = _override_events(new)
    assert ev["outcome"] == "override" and ev["reason"] == "the staging key, not prod", ev
