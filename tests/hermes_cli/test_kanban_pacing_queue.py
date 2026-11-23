"""Recorded pacing is a queue reason, not a slot wait. [daveey]"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_queue_position as queue
from tests.hermes_cli.test_kanban_pace_reroute import board as board  # noqa: PLC0414 -- pytest fixture


# A hold three days out: still active, and never today (today's holds print without a date). A fixed epoch went
# red at its own UTC date and would drop the hold once it passed. [daveey]
_UNTIL = time.time() + 3 * 86400
_UNTIL_TEXT = time.strftime('%Y-%m-%d %H:%MZ', time.gmtime(int(_UNTIL)))


@pytest.mark.parametrize('placement', [None, 'worker-first-v1'])
@pytest.mark.parametrize('reason', ['llm_paced', 'llm_pacing'])
def test_pacing_queue_note_precedes_worker_placement(board, placement, reason, monkeypatch):
    import time
    from types import SimpleNamespace

    from hermes_cli import kanban_dispatch_skip as skips

    # Exercise the dated (non-today) view regardless of the machine's UTC date.
    monkeypatch.setattr(skips, 'time', SimpleNamespace(
        time=lambda: 0, gmtime=time.gmtime, strftime=time.strftime))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='held card', assignee='coach')
        if placement:
            conn.execute('UPDATE tasks SET placement_policy=? WHERE id=?', (placement, tid))
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'dispatch_skipped', {
                'reason': reason, 'detail': 'xai-oauth held', 'until': _UNTIL,
                'providers': [{'provider': 'xai-oauth', 'state': 'held', 'limit': 0}]})
        task = kb.get_task(conn, tid)
        fields = queue.fields(queue.read(conn, task))
        assert fields['queue']['lane'] == 'llm_pacing'
        assert f'LLM quota pacing (xai-oauth held pool) until {_UNTIL_TEXT}' in fields['queue_note']
        assert 'worker' not in fields['queue_note'] and 'slot' not in fields['queue_note']
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'dispatch_skipped', {'reason': 'per_profile_capped', 'detail': 'coach'})
        assert queue.read(conn, task)['lane'] != 'llm_pacing'
