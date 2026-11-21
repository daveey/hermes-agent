"""Refusal history is diagnostic evidence, never an input to provider routing."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_ratio_routing import _budget, _route, hub as hub  # noqa: PLC0414 -- pytest fixture re-export
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- pytest fixture dependency


@pytest.mark.parametrize("lane", ["ready", "review"])
def test_prior_refusal_does_not_change_route(hub, lane):
    _budget(hub["clock"])
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ordinary work", assignee="b")
        before = _route(conn, tid, lane)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "gave_up", {
                "reason_code": "provider_refusal", "kind": "provider_refusal",
                "worker_output": "Provider said: This content was flagged for possible cybersecurity risk.",
                "provider": before[1], "model": before[0],
            })
        assert _route(conn, tid, lane) == before
        task = kb.get_task(conn, tid)
        assert task.model_override is None
        assert task.provider_override is None
