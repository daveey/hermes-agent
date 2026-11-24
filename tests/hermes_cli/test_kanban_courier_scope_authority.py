"""Requirement D on courier origin creation: scope authority is enforced AFTER the valid origin is
subscribed/linked, inside the same transaction, and an authority-less courier card rolls back entirely.

Authority is never invented: the bound origin must be a created, unreleased, provable chat link (the renewal
reader decides), not a foreign hub subscription alone, a placeholder, or a chat on the owner's own gateway.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_state import SessionDB
from tools import kanban_tools  # registers kanban_create
from tools.registry import registry

CHAT = "20261027_134353_5ab65c"


def _owner(tmp_path, monkeypatch, synced):
    if synced:
        (tmp_path / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n")
    for name in ("HERMES_KANBAN_DB", "HERMES_KANBAN_TASK", "HERMES_ORIGIN_SESSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_SESSION_KEY", "20261026_170127_f07c32")
    monkeypatch.setenv("HERMES_SESSION_ID", "20261026_170127_f07c32")
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("kanban-placement-macbook", "cli")
    db.close()
    kb.init_db()
    return {"request_id": "placement:macbook:0123456789abcdef",
            "owner": {"gateway": "hub", "profile": "default", "board": "default"},
            "origin": {"gateway": "macbook", "profile": "default", "session": CHAT}}


@pytest.fixture
def synced(tmp_path, monkeypatch):
    return _owner(tmp_path, monkeypatch, True)


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    return _owner(tmp_path, monkeypatch, False)


ARGS = {"title": "Build", "assignee": "default", "session_id": "kanban-placement-macbook"}
TABLES = ("tasks", "origin_creation_intents", "kanban_notify_subs", "task_events")


def _counts():
    with kbc.connect_closing() as conn:
        out = {}
        for table in TABLES:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='task_chat_links'").fetchone():
            out["task_chat_links"] = conn.execute("SELECT count(*) FROM task_chat_links").fetchone()[0]
        return out


def _create(intent, **extra):
    return json.loads(str(registry.dispatch("kanban_create", {**ARGS, **extra}, origin_intent=intent)))


def _assert_refused(res):
    assert res.get("created") is not True, res
    assert "scope authority" in json.dumps(res), res


def test_courier_create_with_provable_origin_link_is_created_and_renewable(synced):
    res = _create(synced)
    assert res.get("created") is True, res
    with kbc.connect_closing() as conn:
        from hermes_cli.kanban_create_authority import has_authority
        assert has_authority(conn, res["task_id"], None)
        assert conn.execute("SELECT count(*) FROM task_chat_links WHERE task_id=? AND kind='created' AND released=0",
                            (res["task_id"],)).fetchone()[0] == 1


def test_foreign_hub_subscription_alone_is_not_authority_and_rolls_back_everything(legacy):
    before = _counts()
    _assert_refused(_create(legacy))
    assert _counts() == before  # card, intent, subscription and events all rolled back
    # An exact retry is refused again rather than adopting a half-written card.
    _assert_refused(_create(legacy))
    assert _counts() == before


@pytest.mark.parametrize("origin", [
    {"gateway": "macbook", "profile": "default", "session": "kanban-placement-macbook"},  # placeholder
    {"gateway": "macbook", "profile": "default", "session": "local-" + "9c" * 32},  # unproven receipt-shaped hash
])
def test_origin_without_provable_created_link_is_refused_even_on_synced_owner(synced, origin):
    before = _counts()
    _assert_refused(_create({**synced, "origin": origin}))
    assert _counts() == before


def test_created_by_is_not_a_courier_argument_so_it_cannot_supply_authority(legacy):
    before = _counts()
    res = _create(legacy, created_by="claude-code-chat")
    assert res.get("created") is not True and "created_by" in json.dumps(res)
    assert _counts() == before


def test_enforcement_runs_after_subscribe_and_link_not_in_create_task(synced, monkeypatch):
    """create_task must not be asked for the early check (courier anchors after create), and the authority check
    must see the subscription and link already written in this transaction."""
    from hermes_cli import kanban_create_authority as auth
    seen = {}
    real_create, real_require = kb.create_task, auth.require

    def create_spy(conn, **kw):
        seen["early"] = kw.get("require_scope_authority")
        return real_create(conn, **kw)

    def require_spy(conn, task_id, creator):
        seen["subs"] = conn.execute("SELECT count(*) FROM kanban_notify_subs WHERE task_id=?", (task_id,)).fetchone()[0]
        seen["links"] = conn.execute("SELECT count(*) FROM task_chat_links WHERE task_id=?", (task_id,)).fetchone()[0]
        return real_require(conn, task_id, creator)

    monkeypatch.setattr(kb, "create_task", create_spy)
    monkeypatch.setattr(auth, "require", require_spy)
    assert _create(synced).get("created") is True
    assert not seen["early"]
    assert seen["subs"] == 1 and seen["links"] == 1


def test_authority_flag_is_never_part_of_the_stored_envelope(synced):
    res = _create(synced)
    with kbc.connect_closing() as conn:
        stored = conn.execute("SELECT intent_json FROM origin_creation_intents WHERE task_id=?",
                              (res["task_id"],)).fetchone()[0]
    assert "require_scope_authority" not in stored
    assert _create(synced)["task_id"] == res["task_id"]  # exact retry still adopts the same card
