"""Policy at outbound transports and pending-route previews."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_model_tier as fixtures
from tests.hermes_cli.test_kanban_model_neutral_legacy import legacy

board = fixtures.board


def test_selfwork_submit_rejects_before_transport(monkeypatch):
    from hermes_cli import kanban_selfwork_outbound as outbound
    monkeypatch.setattr(outbound, 'prepare', lambda args: pytest.fail('transport preparation reached'))
    with pytest.raises(ValueError, match='model-neutral'):
        outbound.submit({'title': 'work', 'model': 'grok-4.6', 'provider': 'xai-oauth'})


def test_forwarding_legacy_pin_does_not_emit_new_restriction(board):
    from hermes_cli.kanban_placement_forward import remote_args
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='work', assignee='default')
        task = legacy(conn, tid)
        args = remote_args(conn, task, self_gw='example')
        assert args['model'] is None
        assert args['provider'] is None
        # A historical mandate cannot become an active directive at a new ingress.
        from hermes_cli.kanban_model_neutral import validate_text
        validate_text(args['title'], args['body'])
        assert kb.get_task(conn, tid).model_override == 'grok-4.6'


def test_pending_route_preview_never_falls_back_to_legacy_pin(board, monkeypatch):
    from hermes_cli import kanban_shared_blockers as blockers, kanban_model_tier as tier
    monkeypatch.setattr(tier, 'resolve_dispatch_tier', lambda *a, **k: (None, None, 'off'))
    monkeypatch.setattr(tier, 'class_unroutable_reason', lambda model: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='work', assignee='default')
        legacy(conn, tid)
        assert blockers.task_route(conn, tid)[:2] == ('class:standard', None)
