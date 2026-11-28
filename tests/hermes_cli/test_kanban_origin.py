"""Cards an orchestrator creates for a chat on ANOTHER gateway stay bound to that chat. [daveey]

Scenario: a chat on 'macbook' hands a build to an orchestrator session on 'hub' (``hermes chat
--origin-session @session:default@macbook/<id>``). Every card the orchestrator creates through the
supported paths (the kanban_create tool, ``hermes kanban create``) gets a platform='hub' subscription
for that origin, which hermes-hub publishes as a rel:shepherd link the origin gateway reads. The card's
own session_id (the orchestrator) is not rewritten. A missing kanban tool or an unusable origin is a
typed setup failure, not a silent fallback.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_origin as ko

ORIGIN = "@session:default@macbook/20261022_204322_aaaaaa"


@pytest.fixture
def hub_gateway(tmp_path, monkeypatch):
    """This process is gateway 'hub'; hermes-hub knows 'macbook' and 'mini'."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    conn = sqlite3.connect(state / "hub.db")
    conn.executescript("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);"
                       "CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,"
                       " updated_at INTEGER, PRIMARY KEY (gw, coll, key));")
    conn.execute("INSERT INTO meta VALUES ('gateway', 'hub')")
    conn.executemany("INSERT INTO replica VALUES (?, 'gateway', 'info', 1, 1, '{}', 0)", [("macbook",), ("mini",)])
    conn.commit()
    conn.close()
    for var in ("HERMES_KANBAN_TASK", "HERMES_SESSION_ID", ko.ENV):
        monkeypatch.delenv(var, raising=False)
    # Courier creation requires genuine renewal authority, which exists only on a synced board.
    (tmp_path / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
    kb.init_db()
    return tmp_path


def _hub_subs(task_id):
    with kbc.connect_closing() as conn:
        return [s["chat_id"] for s in kn.list_notify_subs(conn, task_id) if s["platform"] == "hub"]


def test_parse_origin_forms_and_rejects_junk():
    assert ko.parse_origin(ORIGIN).chat_id == "macbook/default/20261022_204322_aaaaaa"
    assert ko.parse_origin("macbook/work/s1") == ko.Origin("macbook", "work", "s1")
    assert ko.parse_origin("macbook:s1") == ko.Origin("macbook", "default", "s1")
    # headless ``local-<hash>`` ids parse too (they own nothing, which is creator resolution's call, not parsing's)
    assert ko.parse_origin("@session:default/local-" + "ab" * 32).session == "local-" + "ab" * 32
    local = ko.parse_origin("@session:default/20261122_233430_28d354")
    assert (local.profile, local.session) == ("default", "20261122_233430_28d354")
    for bad in ("", "s1", "@session:a@b", "a/b/c/d", "mac book:s1"):
        with pytest.raises(ko.OriginError) as e:
            ko.parse_origin(bad)
        assert e.value.code == "origin_ref_invalid"


def test_tool_create_binds_origin_without_rewriting_the_creator(hub_gateway, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv(ko.ENV, ORIGIN)
    res = json.loads(kt._handle_create({"title": "P0", "assignee": "default", "idempotency_key": "k-p0"}))
    assert res["ok"], res
    tid = res["task_id"]
    assert _hub_subs(tid) == ["macbook/default/20261022_204322_aaaaaa"]
    assert res["origin_binding"]["readback"] == "pending"
    assert res["origin_binding"]["verify_visible"] == (
        f"hermes -p default kanban chat-tasks 20261022_204322_aaaaaa --expect hub/default/{tid}")
    assert res["origin_binding"]["verify_on_origin"] == f"hermes-hub links 20261022_204322_aaaaaa --expect hub/default/{tid}"
    # idempotent: a retried create returns the same card and does not duplicate the binding
    again = json.loads(kt._handle_create({"title": "P0", "assignee": "default", "idempotency_key": "k-p0"}))
    assert again["task_id"] == tid and _hub_subs(tid) == ["macbook/default/20261022_204322_aaaaaa"]
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id != "20261022_204322_aaaaaa"  # creator identity kept
        child = kb.create_task(conn, title="P1", parents=(tid,))
    assert _hub_subs(child) == ["macbook/default/20261022_204322_aaaaaa"]  # descendants inherit it


def test_explicit_origin_argument_and_same_gateway_origin(hub_gateway):
    from tools import kanban_tools as kt

    res = json.loads(kt._handle_create({"title": "x", "assignee": "default", "origin_session": "mini/default/s9"}))
    assert res["ok"] and _hub_subs(res["task_id"]) == ["mini/default/s9"]
    same = json.loads(kt._handle_create({"title": "y", "assignee": "default", "origin_session": "hub:s_local"}))
    assert same["ok"] and _hub_subs(same["task_id"]) == [] and "origin_binding" not in same
    with kbc.connect_closing() as conn:
        assert [(s["platform"], s["chat_id"]) for s in kn.list_notify_subs(conn, same["task_id"])] == [
            ("tui", "s_local")]


def test_unknown_origin_gateway_fails_before_any_card_exists(hub_gateway, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv(ko.ENV, "@session:default@hub-ec2/s1")  # a Desktop connection label, not a hub gateway
    res = json.loads(kt._handle_create({"title": "orphan", "assignee": "default"}))
    assert not res.get("ok") and "origin_gateway_unknown" in json.dumps(res)
    with kbc.connect_closing() as conn:
        assert [t.title for t in kb.list_tasks(conn)] == []


def test_cli_create_binds_origin_too(hub_gateway, monkeypatch, capsys):
    from hermes_cli import kanban as kc

    monkeypatch.setenv(ko.ENV, ORIGIN)
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", "via cli", "--json"])) == 0
    out = json.loads(capsys.readouterr().out)
    assert _hub_subs(out["id"]) == ["macbook/default/20261022_204322_aaaaaa"]
    assert out["origin_binding"]["readback"] == "pending"


def test_origin_makes_the_kanban_tools_visible_and_preflight_is_typed(hub_gateway, monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: False)
    assert kt._check_kanban_mode() is False
    monkeypatch.setenv(ko.ENV, ORIGIN)
    assert kt._check_kanban_mode() is True and kt._check_kanban_orchestrator_mode() is True
    ko.preflight(["terminal", "kanban_create"])
    with pytest.raises(ko.OriginError) as e:
        ko.preflight(["terminal"])
    assert e.value.code == "kanban_tool_unavailable"
    monkeypatch.setenv(ko.ENV, "nonsense")
    with pytest.raises(ko.OriginError) as e:
        ko.preflight(["kanban_create"])
    assert e.value.code == "origin_ref_invalid"


def test_get_tool_definitions_registers_kanban_for_an_origin_session(hub_gateway, monkeypatch):
    """The real registration path: a session whose platform toolsets lack 'kanban' still gets kanban_create
    when it was started with an origin (as dispatcher workers do), so it never needs a private handler."""
    import model_tools

    monkeypatch.setenv(ko.ENV, ORIGIN)
    model_tools.get_tool_definitions(enabled_toolsets=["terminal"], quiet_mode=True)
    # kanban_create may sit behind tool_search, but it is registered (pre-assembly selection)
    assert "kanban_create" in model_tools._last_resolved_tool_names


@pytest.mark.parametrize("ref", ["@session:default/20261122_233430_28d354", "@session:default/20261127_202849_4c4281"])
def test_this_gateway_session_link_is_accepted_as_origin(hub_gateway, ref):
    from tools import kanban_tools as kt

    res = json.loads(kt._handle_create({"title": "x", "assignee": "default", "origin_session": ref}))
    assert res.get("ok"), res
    assert _hub_subs(res["task_id"]) == [] and "origin_binding" not in res


@pytest.mark.parametrize("profile", ["default", "work"])
def test_hubless_cross_profile_origin_refuses_before_creation(hub_gateway, monkeypatch, tmp_path, profile):
    from hermes_cli import profiles
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "absent-hub"))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "absent-mail.json"))
    monkeypatch.setattr(profiles, "current_profile_name", lambda default=None: profile)
    other = "work" if profile == "default" else "default"
    ref = f"@session:{other}/20261122_233430_28d354"
    with pytest.raises(ko.OriginError) as error:
        ko.resolve(ref)
    assert error.value.code == "hub_not_installed"
    res = json.loads(kt._handle_create({"title": "unreachable cross-profile", "origin_session": ref}))
    assert not res.get("ok") and "hub_not_installed" in json.dumps(res), res
    with kbc.connect_closing() as conn:
        assert kb.list_tasks(conn) == []


def test_this_gateway_cross_profile_origin_matches_qualified(hub_gateway, monkeypatch):
    from hermes_cli import profiles
    from tools import kanban_tools as kt

    monkeypatch.setattr(profiles, "current_profile_name", lambda default=None: "default")
    ref = "@session:work/20261122_233430_28d354"
    assert ko.resolve(ref) == ko.resolve("hub/work/20261122_233430_28d354")
    res = json.loads(kt._handle_create({"title": "cross-profile", "origin_session": ref}))
    assert res.get("ok"), res
    assert _hub_subs(res["task_id"]) == ["hub/work/20261122_233430_28d354"]


@pytest.mark.parametrize("profile", ["default", "work"])
def test_this_gateway_origin_without_hub_keeps_local_ownership(hub_gateway, monkeypatch, tmp_path, profile):
    from hermes_cli import profiles
    from tools import kanban_tools as kt

    monkeypatch.setattr(profiles, "current_profile_name", lambda default=None: profile)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: profile)
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "absent-hub"))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "absent-mail.json"))
    assert ko.gateway_names()[0] == ""
    ref = f"@session:{profile}/20261122_233430_28d354"
    res = json.loads(kt._handle_create({"title": "local without hub", "assignee": "default", "origin_session": ref}))
    assert res.get("ok"), res
    assert "origin_binding" not in res
    with kbc.connect_closing() as conn:
        assert [(s["platform"], s["chat_id"]) for s in kn.list_notify_subs(conn, res["task_id"])] == [
            ("tui", "20261122_233430_28d354")]
