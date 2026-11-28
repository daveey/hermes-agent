"""Opt-in synchronous /board CPU fixture. Touch .board-cost-benchmark in the repository,
then run this file with scripts/run_tests.sh; board-cost-result.json contains all samples.
No live store access. Remove the marker after measuring.
"""
import json
import statistics
import time
from contextlib import closing
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.plugins import test_kanban_board_incremental as helpers

board = helpers.board


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not (ROOT / '.board-cost-benchmark').exists(), reason='opt-in CPU benchmark')
def test_board_cost_fixture(board):
    with closing(kbc.connect()) as conn:
        assert str(kb.kanban_db_path()).startswith(str(Path.home()))
        ids = [kb.create_task(conn, title=f'cost-{i}', assignee='worker') for i in range(300)]
        payload = json.dumps({'reason': 'waiting for worker capacity', 'detail': 'x' * 300})
        conn.executemany('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)',
                         [(ids[0], 'dispatch_skipped', payload, 5)] * 9000)
        conn.commit()
        helpers._read(board)
        samples = []
        for i in range(35):
            conn.execute('INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?,?,?,?)',
                         (ids[0], 'dispatch_skipped', payload, 6 + i))
            conn.commit()  # writer cost excluded; every read sees a real commit without notifications
            start = time.thread_time_ns()
            body = helpers._read(board)
            elapsed = time.thread_time_ns() - start
            assert sum(len(col['tasks']) for col in body['columns']) == 300
            if i >= 5:
                samples.append(elapsed / 1e6)
    (ROOT / 'board-cost-result.json').write_text(json.dumps({
        'cards': 300, 'hot_events': 9000, 'samples': samples,
        'median_ms': statistics.median(samples), 'mean_ms': statistics.mean(samples)}, indent=2))
