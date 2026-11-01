"""Owner-wake grants cross local boards without unpinning ordinary workers."""
import json

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
