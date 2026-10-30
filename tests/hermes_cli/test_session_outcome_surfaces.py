"""The stored chat outcome on its surfaces: the sidebar list rows (GET /api/sessions), POST mute and
GET outcome over REST (own store and another profile's), and the ``hermes sessions`` CLI
(mute / unmute / outcome / outcome-audit)."""

from __future__ import annotations

import argparse
import json

import pytest

import hermes_state_outcome as so
from hermes_state import SessionDB

SUMMARY = "Rebased the branch, fixed the two failing tests and pushed; CI is green on the new head."


@pytest.fixture(autouse=True)
def _hooks():
    so._reset_hooks_for_tests()
    yield
    so._reset_hooks_for_tests()


@pytest.fixture
def homes(monkeypatch, _isolate_hermes_home):
    from hermes_cli import profiles
    from hermes_constants import get_hermes_home

    default_home = get_hermes_home()
    worker = default_home / "profiles" / "worker_beta"
    for home in (default_home, worker):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: default_home / "profiles")
    return {"default": default_home, "worker_beta": worker}


def seed(home, sid="chat", reply=SUMMARY, source="desktop"):
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(sid, source=source)
        db.append_message(sid, role="user", content="fix it")
        db.append_message(sid, role="assistant", content=reply)
    finally:
        db.close()


@pytest.fixture
def client(monkeypatch, homes):
    starlette = pytest.importorskip("starlette.testclient")
    import hermes_state
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", homes["default"] / "state.db")
    c = starlette.TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def test_sidebar_rows_carry_outcome_and_muted(client, homes):
    seed(homes["default"])
    rows = client.get("/api/sessions?order=recent&limit=20").json()["sessions"]
    row = next(r for r in rows if r["id"] == "chat")
    assert row["outcome"] is None and row["muted"] is False
    assert client.get("/api/sessions/chat/outcome?refresh=1").json()["outcome"]["kind"] == "done"
    row = next(r for r in client.get("/api/sessions?order=recent").json()["sessions"] if r["id"] == "chat")
    assert row["outcome"]["kind"] == "done" and row["outcome"]["summary"] == SUMMARY


def test_post_mute_and_get_outcome(client, homes):
    seed(homes["default"])
    resp = client.post("/api/sessions/chat/mute", json={"muted": True})
    assert resp.status_code == 200
    assert resp.json()["muted"] is True and resp.json()["outcome"]["kind"] == "muted"
    assert client.get("/api/sessions/chat/outcome").json() == {
        "session_id": "chat", "outcome": resp.json()["outcome"]}
    row = next(r for r in client.get("/api/sessions").json()["sessions"] if r["id"] == "chat")
    assert row["muted"] is True and row["outcome"]["kind"] == "muted"
    assert client.post("/api/sessions/chat/mute", json={"muted": False}).json()["outcome"]["kind"] == "done"
    assert client.post("/api/sessions/nope/mute", json={"muted": True}).status_code == 404
    assert client.get("/api/sessions/nope/outcome").status_code == 404


def test_mute_and_outcome_resolve_the_profile_like_other_session_writes(client, homes):
    seed(homes["worker_beta"], sid="worker-chat")
    resp = client.post("/api/sessions/worker-chat/mute", json={"muted": True, "profile": "worker_beta"})
    assert resp.status_code == 200 and resp.json()["outcome"]["kind"] == "muted"
    db = SessionDB(db_path=homes["worker_beta"] / "state.db")
    try:
        assert db.get_session("worker-chat")["muted"] == 1
    finally:
        db.close()
    got = client.get("/api/sessions/worker-chat/outcome?profile=worker_beta").json()
    assert got["outcome"]["kind"] == "muted"
    # The session lives only in the profile's store, so the default store does not know it.
    assert client.post("/api/sessions/worker-chat/mute", json={"muted": False}).status_code == 404
    assert client.post("/api/sessions/worker-chat/mute",
                       json={"muted": False, "profile": "ghost"}).status_code == 404


# ── CLI ──────────────────────────────────────────────────────────────────


def run_cli(*argv):
    from hermes_cli.subcommands.sessions import build_sessions_parser, cmd_sessions_lazy

    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=cmd_sessions_lazy)
    args = parser.parse_args(["sessions", *argv])
    return args.func(args)


def test_cli_mute_unmute_and_outcome(homes, capsys):
    seed(homes["default"])
    assert run_cli("outcome", "chat", "--json") is None
    assert json.loads(capsys.readouterr().out)["outcome"] is None
    run_cli("outcome", "chat", "--refresh", "--json")
    assert json.loads(capsys.readouterr().out)["outcome"]["kind"] == "done"
    run_cli("mute", "chat")
    assert "Muted session 'chat'" in capsys.readouterr().out
    run_cli("outcome", "chat")
    assert capsys.readouterr().out.strip() == "chat  muted"
    run_cli("unmute", "chat")
    run_cli("outcome", "chat")
    assert capsys.readouterr().out.strip().endswith(f"done: {SUMMARY}")
    assert run_cli("mute", "missing") == 1


def test_cli_outcome_audit_lists_exactly_the_sidebar_rows(homes, capsys):
    (homes["default"] / "config.yaml").write_text("sessions:\n  interactive_sources: [desktop]\n", encoding="utf-8")
    seed(homes["default"], "done-chat")
    seed(homes["default"], "open-chat", reply="Should I also bump the version?")
    seed(homes["default"], "bot-chat", source="telegram")
    run_cli("outcome-audit", "--json", "--refresh")
    rows = {r["id"]: r for r in json.loads(capsys.readouterr().out)}
    assert set(rows) == {"done-chat", "open-chat"}
    assert (rows["done-chat"]["kind"], rows["open-chat"]["reason"]) == ("done", "no_summary")
    assert set(rows["done-chat"]) == {"id", "title", "kind", "reason", "age_h", "on", "items"}
    from hermes_cli.sessions_cmd_outcome import sidebar_rows

    db = SessionDB(db_path=homes["default"] / "state.db")
    try:
        assert [r["id"] for r in sidebar_rows(db)] == list(rows)
    finally:
        db.close()
    run_cli("outcome-audit")
    assert "no_summary" in capsys.readouterr().out


# ── refresh loads plugin hooks ───────────────────────────────────────────

PROBE_PLUGIN = '''
def register(ctx):
    import hermes_state_outcome as so

    def provider(sid):
        return {"decision_items": [{"id": "us_probe", "question": "Ship it?",
                                    "choices": [{"label": "Yes", "value": "yes"}, {"label": "No", "value": "no"}],
                                    "state": "open"}]}

    so.register_outcome_provider("probe", provider)
'''


@pytest.fixture
def probe_plugin(homes):
    from hermes_cli import plugins

    pdir = homes["default"] / "plugins" / "outcome-probe"
    pdir.mkdir(parents=True)
    (pdir / "plugin.yaml").write_text("name: outcome-probe\nversion: 0.1.0\ndescription: probe\n", encoding="utf-8")
    (pdir / "__init__.py").write_text(PROBE_PLUGIN, encoding="utf-8")
    (homes["default"] / "config.yaml").write_text("plugins:\n  enabled: [outcome-probe]\n", encoding="utf-8")
    plugins._reset_plugin_managers_for_tests()
    yield
    plugins._reset_plugin_managers_for_tests()


def test_cli_refresh_loads_plugin_providers(homes, probe_plugin, capsys):
    """A CLI re-derive must see plugin decisions; without loading plugins it stored ``done`` over them."""
    seed(homes["default"])
    run_cli("outcome", "chat", "--refresh", "--json")
    outcome = json.loads(capsys.readouterr().out)["outcome"]
    assert outcome["kind"] == "decision"
    assert outcome["items"][0]["id"] == "us_probe"
    so._reset_hooks_for_tests()
    from hermes_cli import plugins

    plugins._reset_plugin_managers_for_tests()
    run_cli("outcome-audit", "--json", "--refresh")
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "decision"


def test_web_refresh_loads_plugin_providers(client, homes, probe_plugin):
    seed(homes["default"])
    assert client.get("/api/sessions/chat/outcome?refresh=1").json()["outcome"]["kind"] == "decision"


# ── tree rows match list rows ────────────────────────────────────────────

# Every row field the Desktop's sessionStatusStripe reads (via session-row-stripe.tsx).
_STRIPE_FIELDS = ("outcome", "muted", "message_count", "kanban_active", "kanban_blocked", "kanban_held", "kanban_parked",
                  "kanban_stalled", "kanban_waiting_on", "kanban_attention", "goal_active", "cron_active", "background_active",
                  "turn_live", "review_wait")


def _tree_rows(node):
    """Every session row anywhere in a projects-tree payload (rows are the dicts with a ``message_count``)."""
    if isinstance(node, list):
        for item in node:
            yield from _tree_rows(item)
    elif isinstance(node, dict):
        if "message_count" in node and "id" in node:
            yield node
        for value in node.values():
            yield from _tree_rows(value)


def test_project_tree_rows_carry_the_list_rows_outcome_and_mute(client, homes):
    """[daveey] A tree preview and a list row of one chat paint the same stripe: the tree used to drop
    ``outcome``/``muted``, so an ``unresolved`` chat flapped green (tree) <-> amber (list) on every refresh."""
    seed(homes["default"], "open-chat", reply="Should I also bump the version?")
    seed(homes["default"], "done-chat")
    seed(homes["default"], "muted-chat")
    for sid in ("open-chat", "done-chat"):
        client.get(f"/api/sessions/{sid}/outcome?refresh=1")
    client.post("/api/sessions/muted-chat/mute", json={"muted": True})

    listed = {r["id"]: r for r in client.get("/api/profiles/sessions?profile=all&limit=50").json()["sessions"]}
    tree = client.get("/api/profiles/projects/tree", params={"preview_limit": 50}).json()
    treed = {r["id"]: r for r in _tree_rows(tree)}
    assert {"open-chat", "done-chat", "muted-chat"} <= set(treed) & set(listed)
    assert listed["open-chat"]["outcome"]["kind"] == "unresolved"
    assert (listed["done-chat"]["outcome"]["kind"], listed["muted-chat"]["muted"]) == ("done", True)
    for sid in ("open-chat", "done-chat", "muted-chat"):
        for field in _STRIPE_FIELDS:
            assert treed[sid].get(field) == listed[sid].get(field), (sid, field)
