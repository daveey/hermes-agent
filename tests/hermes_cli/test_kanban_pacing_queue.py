"""Recorded pacing is a queue reason, not a slot wait. [daveey]"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_queue_position as queue
from tests.hermes_cli.test_kanban_pace_reroute import board as board  # noqa: PLC0414 -- pytest fixture


@pytest.mark.parametrize('placement', [None, 'worker-first-v1'])
@pytest.mark.parametrize('reason', ['llm_paced', 'llm_pacing'])
def test_pacing_queue_note_precedes_worker_placement(board, placement, reason):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='held card', assignee='coach')
        if placement:
            conn.execute('UPDATE tasks SET placement_policy=? WHERE id=?', (placement, tid))
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'dispatch_skipped', {
                'reason': reason, 'detail': 'xai-oauth held', 'until': 1795474351.914,
                'providers': [{'provider': 'xai-oauth', 'state': 'held', 'limit': 0}]})
        task = kb.get_task(conn, tid)
        fields = queue.fields(queue.read(conn, task))
        assert fields['queue']['lane'] == 'llm_pacing'
        assert 'LLM quota pacing (xai-oauth held pool) until 2026-11-23 22:52Z' in fields['queue_note']
        assert 'worker' not in fields['queue_note'] and 'slot' not in fields['queue_note']
        with kb.write_txn(conn):
            kb._append_event(conn, tid, 'dispatch_skipped', {'reason': 'per_profile_capped', 'detail': 'coach'})
        assert queue.read(conn, task)['lane'] != 'llm_pacing'
