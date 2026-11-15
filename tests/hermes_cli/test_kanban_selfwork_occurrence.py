"""Submission identity follows daily occurrences, not delivery retries."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_selfwork_intake as intake
from hermes_cli import kanban_selfwork_outbound as outbound
from tests.hermes_cli import test_kanban_selfwork_outbound as support
from tools import kanban_tools as kt

sender = support.sender
local = support.local
board = support.board


@pytest.mark.parametrize('key', [None, 'explicit-occurrence'])
def test_daily_submission_and_explicit_retry_identity(sender, local, monkeypatch, key):
    args = {'title': 'Daily finding', 'self_work': support.entry.support.declaration()}
    if key:
        args['idempotency_key'] = key
    identities = []
    receipts = []
    for day in (15, 16):
        clock = datetime(2026, 11, day, tzinfo=UTC)
        monkeypatch.setattr(outbound, 'datetime', SimpleNamespace(now=lambda tz: clock))
        _, body, request = outbound.prepare(args)
        identities.append(request)
        receipts.append(intake.admit(local, request_id=request, authority='home', local_gateway='home',
                                      options={'title': body['create']['title'], 'self_work': args['self_work']},
                                      now=int(clock.timestamp())))
        assert outbound.prepare(args)[2] == request
    if key:
        assert identities[0] == identities[1]
        assert receipts[1]['replayed'] is True
    else:
        assert identities[0] != identities[1]
        assert receipts[0]['task_id'] != receipts[1]['task_id']
        assert len(kb.list_tasks(local)) == 2


def test_cli_key_only_context_keeps_origin(sender, monkeypatch):
    monkeypatch.setenv('HERMES_SESSION_KEY', 'persisted-chat')
    monkeypatch.setattr(kt, '_persisted_session_id', lambda value: 'session-123' if value == 'persisted-chat' else None)
    captured = []
    def submit(args):
        captured.append(outbound.prepare(args)[1])
        return {'queued': True}
    monkeypatch.setattr(outbound, 'submit', submit)
    args = SimpleNamespace(title='Finding', parent=[], self_work=support.entry.support.declaration())
    outbound.cli_submit(args, body='Evidence', workspace_kind='scratch', workspace_path=None,
                        max_runtime=None, branch_name=None)
    assert captured[0]['origin'] == {'gateway': 'origin', 'profile': 'default', 'session': 'session-123'}
