"""A schedule's actionability names what it waits FOR; the wake clock is only the next check. [daveey]"""
import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wait_info as kwi
from hermes_cli.kanban_actionability_cli import report
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_db_connect import connect

T0 = 1_800_000_000
WHY = 'Event watch on hub-hosted original owners; backstop at nightly mini rollout 10:30Z'


def _row(conn, tid, now):
    return next(r for r in report(conn, now=now)['rows'] if r['task_id'] == tid)


def _park(conn, monkeypatch, awaiting, wake='2027-01-15T08:01:40Z'):
    monkeypatch.setattr('time.time', lambda: T0)
    tid = kb.create_task(conn, title='parked', assignee='default')
    assert kb.schedule_task(conn, tid, reason=f'SCHEDULED_UNTIL={wake} awaiting natural hub update then live '
                            'measurements', awaiting=awaiting, clock_only=True)
    return tid


def test_clock_park_with_why_names_prerequisite_not_only_timer(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY})
        row = _row(conn, tid, T0 + 10)
        assert row['instruction'].startswith(f'Waiting on: {WHY}. Next check: Recorded clock reaches ')
        assert row['blocker']['prerequisite'] == WHY
        assert 'Recorded clock reaches' in row['blocker']['trigger']
        assert row['blocker']['prerequisite'] != row['blocker']['trigger']
        assert describe(conn, tid, now=T0 + 10)['blocker']['prerequisite'] == WHY


def test_wake_into_todo_clears_the_old_reason(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = _park(conn, monkeypatch, {'kind': 'clock', 'why': WHY})
        parent = kb.create_task(conn, title='late prerequisite', assignee='default')
        kb.link_tasks(conn, parent, tid)
        assert kc.wake_due_continuations(conn, now=5_000_000_000) == [tid]
        assert kb.get_task(conn, tid).status == 'todo'
        row = _row(conn, tid, T0 + 500)
        assert WHY not in row['instruction'] and WHY not in str(row['blocker'])


def test_wait_label_carries_the_full_reason(monkeypatch):
    park = {'awaiting': {'kind': 'clock', 'why': WHY}, 'wake_at': T0 + 600}
    wait = kwi.describe('scheduled', park=park, since=T0, now=T0 + 10)
    assert wait['waiting_on'].endswith(f'({WHY})')
    assert kwi.describe('scheduled', park={'awaiting': {'kind': 'clock'}, 'wake_at': T0 + 600},
                        since=T0, now=T0 + 10)['waiting_on'].endswith('UTC')
