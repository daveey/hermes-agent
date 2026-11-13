"""Pending folds preserve the completion-note decision and its display card."""
import asyncio

import pytest

from tests.gateway.test_fold_pending_admissions import _completed, _harness, _idle
from tests.waiting import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize('viewed', [False, True])
async def test_requested_stop_behind_kanban_keeps_unviewed_note(tmp_path, monkeypatch, viewed):
    from gateway import session_completion_notes
    gate = asyncio.Event()

    async def behave(n, event):
        if n == 1:
            await gate.wait()
        return _completed()

    h = await _harness(tmp_path, monkeypatch, behave)
    monkeypatch.setattr(session_completion_notes, '_refresh_outcome_after_note', lambda *args: None)
    head, result = '[kanban] Task t_next blocked.', 'Delegation stopped on request'
    try:
        await h.notice('[kanban] Task t_first completed.', 'kanban-wake')
        await async_wait_until(lambda: len(h.seen) == 1, timeout=10, what='first turn')
        await h.notice(head, 'kanban-wake')
        await h.notice(result, 'async_delegation', {
            'completion_requested_stop': ['cancelled'], 'display_kind': 'async_delegation_complete',
            'display_text': 'Stopped delegation',
        })
        if not viewed:
            h.live.subscribers.clear()
        gate.set()
        await _idle(h, 2)
        assert [e.text for e in h.seen][1:] == [f'{head}\n\n{result}' if viewed else head]
        row = h.rows()[result]
        assert row['outcome'] == ('steered' if viewed else 'completed')
        notes = [m for m in h.db.get_messages(h.authority.physical_target(h.ref))
                 if m.get('display_kind') == 'async_delegation_complete']
        if viewed:
            assert notes == []
        else:
            assert len(notes) == 1
            assert notes[0]['content'] == result
            assert notes[0]['display_metadata']['display_text'] == 'Stopped delegation'
            assert notes[0]['display_metadata']['completion_note'] == [row['request_id']]
    finally:
        await h.close()


@pytest.mark.parametrize(('producer', 'viewed', 'flags', 'closed', 'wakes', 'note'), [
    ('async_delegation', False, {}, False, 0, True),
    ('async_delegation', True, {}, False, 0, False),
    ('completion', False, {'resumes_parent': True}, False, 99, False),
    ('completion', False, {'resumes_parent': True}, True, 0, True),
    ('async_delegation', False, {'wakes_parent': True}, False, 2, False),
    ('async_delegation', False, {'wakes_parent': True}, False, 3, True),
    ('async_delegation', False, {'wakes_parent': True}, True, 0, True),
    ('cron', True, {}, False, 0, True),
    ('cron', False, {}, False, 0, True),
    ('kanban-wake', False, {}, False, 0, False),
])
def test_note_decision_scan_is_read_only(monkeypatch, producer, viewed, flags, closed, wakes, note):
    import json
    from types import SimpleNamespace
    from gateway import session_completion_notes as notes
    from gateway import session_fold_pending as folds

    live = SimpleNamespace(subscribers={'viewer': object()} if viewed else {}, unattended_wakes=wakes)
    authority = SimpleNamespace(sessions={'s': live}, db=object())
    ref = SimpleNamespace(session_id='s')
    identity = json.dumps([producer, 'result'])
    row = {'principal_id': 'automation:test', 'request_id': identity, 'admission_id': 'result', 'status': 'queued', 'payload': {
        'text': 'result', 'local_automation_v1': {'identity': identity, **flags}}}
    head = {'admission_id': 'head', 'payload': {
        'text': 'head', 'local_automation_v1': {'identity': '["kanban-wake","head"]'}}}
    monkeypatch.setattr(notes, '_closed_by_user', lambda *args: closed)
    monkeypatch.setattr(folds, 'list_session_admissions', lambda *args, **kwargs: [row])
    for _ in range(2):
        assert notes.completion_is_note(authority, ref, row) is note
        assert folds.candidates(authority, ref, head) == (
            [row] if not note and producer in folds.FOLDABLE_PRODUCERS else [])
        assert live.unattended_wakes == wakes
    assert notes.completion_is_note(authority, ref, row, consume=True) is note
    spends = bool(flags.get('wakes_parent')) and not note and not viewed
    assert live.unattended_wakes == wakes + int(spends)
    authority.sessions.clear()
    assert notes.completion_is_note(authority, ref, row) is False
