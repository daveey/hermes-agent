"""Minimal lifecycle fixtures must supply class routing before expecting a launch."""
from tests.hermes_cli import test_kanban_shared_blockers as fixtures
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_db_dispatch import dispatch_once

kanban_home = fixtures.kanban_home


def test_minimal_fixture_refusal_reason(kanban_home):
    (kanban_home / 'config.yaml').write_text('model_routing:\n  enabled: false\n')
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title='ordinary lifecycle work', assignee='default')
        seen = []
        outcome = dispatch_once(conn, spawn_fn=lambda task, *a: seen.append(task.id))
        assert seen == []
        assert outcome.skipped_ineligible == [(tid, 'model_unroutable:routing_disabled')], repr(outcome)
