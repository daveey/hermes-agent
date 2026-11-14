"""Counterfactual replay of real incident run rows; NOT natural recovery proof.

Source: read-only hub kanban.db and sync-shadow/default.db, 00:39–01:10 UTC.
Only wall metadata is retained. Commands are modeled rc1 before the reported
00:46:42 reset, rc0 afterward; this is not an observed historical quota sample.
Card lanes are modeled as blocked on assertion, with all cards local to isolate
recovery logic from transport. Tick cadence is 120s, plus row-arrival ticks.
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blockers as blockers
from hermes_cli import kanban_shared_blocker_checks as checks
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home
DATA = Path(__file__).parents[1] / 'fixtures/shared_blockers/2026-11-14.json'
RESET = int(datetime.fromisoformat('2026-11-14T00:46:42+00:00').timestamp())
START = int(datetime.fromisoformat('2026-11-14T00:39:00+00:00').timestamp())
END = int(datetime.fromisoformat('2026-11-14T01:10:00+00:00').timestamp())


@pytest.mark.parametrize('source', ['hub', 'shadow'])
def test_incident_run_replay(kanban_home, monkeypatch, source):
    rows = json.loads(DATA.read_text())[source]['runs']
    clock = [START]
    monkeypatch.setattr(blockers.time, 'time', lambda: clock[0])
    monkeypatch.setattr(checks, '_run_check', lambda _: (int(clock[0] < RESET), 'modeled reset'))
    cfg = {'kanban': {'shared_blocker_checks': {'github-rest-quota': 'modeled'}}}
    mapping, releases, assertions = {}, [], 0
    with kbc.connect_closing() as conn:
        for tick in sorted(set(range(START, END + 121, 120)) | {r['ended_at'] for r in rows}):
            clock[0] = tick
            for row in (r for r in rows if r['ended_at'] == tick):
                old = row['task_id']
                if old not in mapping:
                    mapping[old] = kb.create_task(conn, title='historical replay', assignee='default')
                tid = mapping[old]
                effect, bid = blockers.run_effect(row['outcome'], json.dumps(row['metadata']))
                with kb.write_txn(conn):
                    conn.execute('INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome,metadata) '
                                 'VALUES (?,?,?,?,?,?)', (tid, row['outcome'], row['started_at'],
                                 tick, row['outcome'], json.dumps(row['metadata'])))
                    if effect == 'assert':
                        assertions += 1
                        assert bid == 'github-rest-quota'
                        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (tid,))
            checks.run_due_checks(conn, cfg=cfg)
            released = blockers.release_cleared_blockers(conn, now=tick)
            if released:
                releases.append((tick, len(released)))
            if tick >= RESET + 120:
                assert all(not state.is_open for state in blockers.blocker_states(conn, now=tick).values())
        assert assertions > 0 and releases
        first_recovery = next(tick for tick, _ in releases if tick >= RESET)
        assert first_recovery <= RESET + 120
        print(json.dumps({'source': source, 'runs': len(rows), 'assertions': assertions,
                          'reset': RESET, 'first_recovery': first_recovery, 'release_events': releases}))
