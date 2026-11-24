"""Creation may inherit owners, never promote passive observers. [daveey]"""
import json

import pytest

from tests.hermes_cli import test_kanban_create_scope_authority as helpers
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_chat_links import link_uid
from hermes_cli.kanban_create_authority import CreateAuthorityRefused, has_authority

home = helpers.home


@pytest.mark.parametrize("kind", ["shepherd", "touch", "created"])
@pytest.mark.parametrize("surface", ["db", "tool"])
@pytest.mark.parametrize("ancestry", [False, True])
def test_observer_parent_cannot_supply_creation_authority(home, monkeypatch, kind, surface, ancestry):
    session = "20261124_060000_observer"
    helpers._seed_session(home, session)
    with kbc.connect_closing() as conn:
        helpers._links_schema(conn)
        parent = kb.create_task(conn, title="source", assignee="default")
        conn.execute(
            "INSERT INTO task_chat_links "
            "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
            "VALUES (?,?,?,?,?,?,0,1)",
            (link_uid(parent, "", None, session, kind), parent, "", None, session, kind))
        conn.commit()
        assert has_authority(conn, parent, None, durable=True) is (kind == "created")
        if ancestry:
            source = parent
            parent = kb.create_task(conn, title="unowned intermediate", assignee="default")
            kb._link(conn, source, parent)
            conn.commit()
        before = list(conn.iterdump())
        if surface == "db":
            if kind == "created":
                child = kb.create_task(conn, title="child", assignee="default", creator_task_id=parent,
                                       require_scope_authority=True)
            else:
                with pytest.raises(CreateAuthorityRefused):
                    kb.create_task(conn, title="child", assignee="default", creator_task_id=parent,
                                   require_scope_authority=True)
        else:
            from tools import kanban_tools as kt
            monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
            monkeypatch.setenv("HERMES_SESSION_ID", "unregistered-worker")
            out = json.loads(kt._handle_create(
                {"title": "child", "assignee": "default", "placement": "local", "requires": []}))
            assert bool(out.get("ok")) is (kind == "created"), out
            child = out.get("task_id")
        if kind != "created":
            assert list(conn.iterdump()) == before
        else:
            assert has_authority(conn, child, None, durable=True)
