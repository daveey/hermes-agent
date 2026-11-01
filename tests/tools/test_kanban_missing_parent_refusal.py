"""Both tool entry paths retain the worker claim when a parent cannot resolve."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.tools.test_kanban_tools import worker_env
from tools import kanban_tools as kt
from tools.registry import registry

__all__ = ["kt", "worker_env"]


@pytest.mark.parametrize("entry", ["kanban_link", "kanban_block"])
def test_missing_parent_tool_refusal(worker_env, entry):
    with kbc.connect() as conn:
        if entry == "kanban_block":
            with kbc.write_txn(conn):
                conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                             ("t_foreign", worker_env))
        before = kb.get_task(conn, worker_env)
    args = ({"parent_id": "t_foreign", "child_id": worker_env} if entry == "kanban_link"
            else {"task_id": worker_env, "kind": "dependency", "reason": "Wait for remote owner"})
    tool = registry.get_entry(entry)
    assert tool is not None
    result = json.loads(tool.handler(args))
    assert result.get("error"), result
    assert "awaiting=" in result["error"] and "gateway" in result["error"]
    with kbc.connect() as conn:
        assert kb.get_task(conn, worker_env) == before
        assert kb.parent_ids(conn, worker_env) == (["t_foreign"] if entry == "kanban_block" else [])
