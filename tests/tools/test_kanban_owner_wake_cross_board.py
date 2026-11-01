"""Owner-wake grants cross local boards without unpinning ordinary workers."""
import json
import os
import subprocess
import sys


import pytest

from tests.tools.test_kanban_owner_wake_requeue import (
    _as_worker, _card, _line, _supervisor,
)
from tests.tools.test_kanban_owner_wake_requeue import board

pytestmark = pytest.mark.usefixtures(board.__name__)


@pytest.mark.parametrize("creator", ["owner-wake", "default"])
@pytest.mark.parametrize("status", ["triage", "blocked", "scheduled", "running"])
def test_cross_board_surface_and_unblock(monkeypatch, creator, status):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    kb.create_board("ops")
    own = kbc.connect()
    target_conn = kbc.connect(board="ops")
    target = _card(target_conn, kb, "target", status)
    other = _card(target_conn, kb, "unlisted", "triage")
    sup = _supervisor(own, kb, [_line(f"ops/{target}")], created_by=creator)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    _as_worker(monkeypatch, own, kb, sup)
    assert kb.kanban_db_path(board="ops") == kb.kanban_db_path()
    assert kt._check_kanban_unblock() is (creator == "owner-wake")
    shown = json.loads(kt._handle_show({"task_id": target, "board": "ops"}))
    assert (shown.get("task", {}).get("id") == target) is (creator == "owner-wake")
    for args in ({"task_id": other, "board": "ops"}, {"task_id": target, "board": "default"}):
        assert not json.loads(kt._handle_unblock(args)).get("ok")
    result = json.loads(kt._handle_unblock({"task_id": target, "board": "ops"}))
    allowed = creator == "owner-wake" and status in ("triage", "blocked")
    assert bool(result.get("ok")) is allowed, result
    assert kb.get_task(target_conn, target).status == ("ready" if allowed else status)
    assert kb.get_task(target_conn, other).status == "triage"
    own.close()
    target_conn.close()


def test_cross_board_delegate_cannot_use_supervisor_grant(monkeypatch):
    from agent.delegation_context import delegated_child_context
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    kb.create_board("ops")
    own = kbc.connect()
    other = kbc.connect(board="ops")
    target = _card(other, kb, "target", "triage")
    sup = _supervisor(own, kb, [_line(f"ops/{target}")])
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
    _as_worker(monkeypatch, own, kb, sup)
    with delegated_child_context():
        assert not kt._check_kanban_unblock()
        assert not json.loads(kt._handle_unblock({"task_id": target, "board": "ops"})).get("ok")
        assert "task" not in json.loads(kt._handle_show({"task_id": target, "board": "ops"}))
    assert kb.get_task(other, target).status == "triage"
    own.close()
    other.close()


@pytest.mark.parametrize("board_arg", [None, "ops"])
@pytest.mark.parametrize("stale", [False, True])
def test_omitted_board_and_stale_run(monkeypatch, board_arg, stale):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    kb.create_board("ops")
    with kbc.connect_closing() as own, kbc.connect_closing(board="ops") as target_conn:
        target = _card(target_conn, kb, "target", "triage")
        sup = _supervisor(own, kb, [_line(f"ops/{target}")])
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        _as_worker(monkeypatch, own, kb, sup)
        if stale:
            monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "999999")
        args = {"task_id": target, **({"board": board_arg} if board_arg else {})}
        out = json.loads(kt._handle_unblock(args))
        assert bool(out.get("ok")) is not stale, out
        assert kb.get_task(target_conn, target).status == ("triage" if stale else "ready")


def test_ambiguous_id_requires_board(monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    with kbc.connect_closing() as own:
        target = _card(own, kb, "target", "triage")
        sup = _supervisor(own, kb, [_line(f"default/{target}"), _line(f"ops/{target}")])
        _as_worker(monkeypatch, own, kb, sup)
        out = json.loads(kt._handle_unblock({"task_id": target}))
        assert "ambiguous" in out.get("error", ""), out
        assert kb.get_task(own, target).status == "triage"
        assert json.loads(kt._handle_unblock({"task_id": target, "board": "default"})).get("ok")


@pytest.mark.parametrize("creator", ["owner-wake", "default"])
def test_fresh_worker_registry_describe_and_dispatch(monkeypatch, creator):
    from pathlib import Path
    Path(os.environ["HERMES_HOME"], "config.yaml").write_text(
        "tools:\n  tool_search:\n    defer: [kanban_unblock]\n")
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc

    kb.create_board("ops")
    with kbc.connect_closing() as own, kbc.connect_closing(board="ops") as target_conn:
        target = _card(target_conn, kb, "target", "triage")
        sup = _supervisor(own, kb, [_line(f"ops/{target}")], created_by=creator)
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        _as_worker(monkeypatch, own, kb, sup)
        script = '''
import json, sys
from model_tools import get_tool_definitions
from tools.tool_search import dispatch_tool_describe, resolve_underlying_call
from tools.registry import registry
defs = get_tool_definitions(enabled_toolsets=["kanban"], skip_tool_search_assembly=True)
names = {d["function"]["name"] for d in defs}
allowed = sys.argv[2] == "owner-wake"
assert ("kanban_unblock" in names) is allowed, names
described = json.loads(dispatch_tool_describe({"names": ["kanban_unblock"]}, current_tool_defs=defs))
assert ("kanban_unblock" in described["tools"]) is allowed, described
args = {"task_id": sys.argv[1], "board": "ops"}
name, parsed, error = resolve_underlying_call(
    {"calls": [{"name": "kanban_unblock", "arguments": args}]}, session_tool_names=names)
if allowed:
    assert name == "kanban_unblock" and error is None, error
    shown = json.loads(registry.dispatch("kanban_show", args))
    assert shown["task"]["id"] == sys.argv[1], shown
    result = json.loads(registry.dispatch(name, parsed))
    assert result["ok"] and result["status"] == "ready", result
else:
    assert error and name is None, (name, error)
print("fresh registry/describe/dispatch verified", allowed)
'''
        result = subprocess.run([sys.executable, "-c", script, target, creator],
                                env=os.environ.copy(), text=True, capture_output=True, timeout=90, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert kb.get_task(target_conn, target).status == ("ready" if creator == "owner-wake" else "triage")


@pytest.mark.parametrize("change", ["board", "body", "run", "target_status"])
def test_revocation_at_transition_boundary(monkeypatch, change):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt

    kb.create_board("ops")
    with kbc.connect_closing() as own, kbc.connect_closing(board="ops") as other:
        target = _card(other, kb, "target", "triage")
        sup = _supervisor(own, kb, [_line(f"ops/{target}")])
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        _as_worker(monkeypatch, own, kb, sup)
        original = kt._board
        from contextlib import contextmanager

        calls = 0

        @contextmanager
        def changed_board(board_arg):
            nonlocal calls
            calls += 1
            if calls == 2:
                if change == "target_status":
                    other.execute("UPDATE tasks SET status='running' WHERE id=?", (target,))
                    other.commit()
                elif change == "run":
                    own.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (sup,))
                    own.commit()
                else:
                    body = kb.get_task(own, sup).body
                    own.execute("UPDATE tasks SET body=? WHERE id=?",
                                (body.replace("ops/", "default/") if change == "board" else "revoked", sup))
                    own.commit()
            with original(board_arg) as result:
                yield result

        # Revoke before the handler enters its target transaction, not after it
        # already accepted the grant. The second _board is the source recheck.
        if change == "target_status":
            # This variant must change before the target acquires its write lock.
            real_unblock = kb.unblock_task

            def change_target(conn, tid, **kw):
                other.execute("UPDATE tasks SET status='running' WHERE id=?", (target,))
                other.commit()
                return real_unblock(conn, tid, **kw)

            monkeypatch.setattr(kb, "unblock_task", change_target)
        else:
            monkeypatch.setattr(kt, "_board", changed_board)
        result = json.loads(kt._handle_unblock({"task_id": target}))
        assert not result.get("ok"), result
        assert kb.get_task(other, target).status == ("running" if change == "target_status" else "triage")
