"""Registered stop-only public API and closed operator wire contracts."""
import json
import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as courier
from tests.hermes_cli import test_kanban_protect_stopped_local as support

board = support.board
fixture = support.fixture


def test_registered_stop_tool_uses_actual_unsynced_kernel(board, monkeypatch):
    from tools import kanban_tools
    from tools.registry import registry
    import toolsets
    from agent.transports import hermes_tools_mcp_server as native
    tool = next((item for item in kanban_tools._TOOLS if item[0] == 'kanban_protect_stopped'), None)
    assert registry is not None and tool is not None
    assert 'kanban_protect_stopped' in toolsets.TOOLSETS['kanban']['tools']
    assert 'kanban_protect_stopped' in native.EXPOSED_TOOLS
    actor, rid, session, target, _, args = fixture(board, monkeypatch)
    from contextlib import contextmanager
    @contextmanager
    def local(_):
        yield 'default', board
    monkeypatch.setattr(kanban_tools, '_board', local)
    with scoped_current_session_id(session):
        result = json.loads(tool[2](args))
    assert result['ok'] is True and result['terminal_for_run'] is False
    assert result['execution_authority'] is False
    assert kb.get_task(board, target).status == 'triage'
    assert kb.get_task(board, actor).current_run_id == rid


def test_protect_operator_wire_has_closed_stop_only_fields(board, monkeypatch):
    _, _, _, _, _, args = fixture(board, monkeypatch)
    raw = {'op': 'operator', 'action': 'protect_stopped', **args, 'expected_updated_at':1234567890}
    assert courier.normalize_op(0, raw) == raw
    import pytest
    with pytest.raises(ValueError):
        courier.normalize_op(0, {**raw, 'release': True})
    with pytest.raises(ValueError, match='integer target clock'):
        courier.normalize_op(0, {**raw, 'expected_updated_at': None})


def _pending(board, monkeypatch, outcome_row=True):
    _, _, _, target, _, _ = fixture(board, monkeypatch)
    courier.log(board, request_uid='req-1', side='execute', outcome='accepted_pending_canonical',
                op={'action': 'protect_stopped', 'task_id': target}, caller={'gateway': 'g', 'task_id': 'a'}, executor_gateway='g')
    return target, {'request_uid': 'req-1', 'target_task_id': target, 'caller': {'gateway': 'g', 'task_id': 'a'}}


def test_refused_protection_is_not_logged_as_confirmed_receipt(board, monkeypatch):
    from hermes_cli import kanban_protect_operator as op
    _, payload = _pending(board, monkeypatch)
    op._finish(board, 'g', payload, {'ok': False, 'canonical_refusal': {'reason': 'x'}}, outcome='refused')
    row = board.execute("SELECT outcome, detail FROM operator_calls WHERE request_uid='req-1'").fetchone()
    assert row[0] == 'refused' and 'confirmed' not in row[1] and 'no stop receipt' in row[1]
    event = board.execute('SELECT payload FROM task_events WHERE kind=?', (courier.RESULT_KIND,)).fetchone()
    detail = json.loads(event[0])['detail']
    assert 'canonical refusal recorded; no stop receipt' in detail
    assert 'x' in detail and 'no execution authority' in detail


def test_applied_protection_logs_confirmed_receipt(board, monkeypatch):
    from hermes_cli import kanban_protect_operator as op
    _, payload = _pending(board, monkeypatch)
    op._finish(board, 'g', payload, {'ok': True})
    assert 'confirmed' in board.execute("SELECT detail FROM operator_calls WHERE request_uid='req-1'").fetchone()[0]


@pytest.mark.parametrize('outcome, expected', [
    ('applied', 'exact canonical stop receipt and held replica confirmed'),
    ('recorded', 'historical stop receipt confirmed; current hold not confirmed after separate renewal'),
])
def test_protection_result_exposes_outcome_specific_detail(board, monkeypatch, outcome, expected):
    from hermes_cli import kanban_protect_operator as op
    _, payload = _pending(board, monkeypatch)
    op._finish(board, 'g', payload, {'ok': True}, outcome=outcome)
    event = board.execute('SELECT payload FROM task_events WHERE kind=?', (courier.RESULT_KIND,)).fetchone()
    detail = json.loads(event[0])['detail']
    assert detail == expected + '; Administrative protection only; no execution authority'
    assert detail == board.execute("SELECT detail FROM operator_calls WHERE request_uid='req-1'").fetchone()[0]


def test_unresolvable_pending_row_backs_off_instead_of_requesting_every_tick(board, monkeypatch):
    from hermes_cli import kanban_protect_operator as op
    from hermes_cli import kanban_protect_evidence as evidence
    from hermes_cli import kanban_sync_client as client
    _pending(board, monkeypatch)
    board.execute("CREATE TABLE IF NOT EXISTS kanban_protect_commands (request_uid TEXT, board TEXT, home TEXT, request_json TEXT, "
                  "request_event_uid TEXT, delivery_state TEXT, canonical_receipt_json TEXT, request_sha256 TEXT)")
    board.execute("CREATE TABLE IF NOT EXISTS kanban_protect_scan (board TEXT PRIMARY KEY, last_id INTEGER)")
    board.execute("INSERT INTO kanban_protect_commands VALUES ('req-1','b','g','{}','e','pending',NULL,NULL)")
    calls = []
    monkeypatch.setattr(client, 'load_identity', lambda: object())
    monkeypatch.setattr(client, 'Hub', lambda identity: object())
    def unavailable(*a, **k):
        calls.append(1)
        raise OSError('capability route not deployed')
    monkeypatch.setattr(evidence, 'frozen', unavailable)
    op._BACKOFF.clear()
    for _ in range(5):
        assert op.tick(board, 'g', 'b') == []
    assert len(calls) == 1
