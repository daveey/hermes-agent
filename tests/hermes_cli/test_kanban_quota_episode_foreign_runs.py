"""Episode walls stored under another gateway's run ids are read through this replica's run ids. [daveey]

t_01a1d060012b9cd1: a stored ``quota_episode`` payload keeps the recording gateway's run ids (worker-2's
2392) while the event row and ``task_runs`` use this replica's (the hub's 173967). ``_outstanding`` compared
the payload's ``episode_id`` against local event rows, so walls of earlier (released) episodes kept holding;
``_unhinted`` looked the payload's ``run_id`` up in local ``task_runs``, found nothing and anchored the
backoff at the current time on every evaluation, so it never expired. Both now use
``kanban_quota_episode_release._local_run`` (PR #1541).
"""
import argparse
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_quota_episode_release as release
from hermes_cli.kanban_quota_episode import episode, hold_until
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- fixture re-export
from tests.hermes_cli.test_kanban_quota_episode_live_release import NOW, DAYS, _entry, _reap, pool as pool  # noqa: PLC0414 -- fixture re-export

COOLDOWN = 300  # the board fixture's HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS


def _advance_local_ids(conn):
    """Earlier runs on this replica: its run ids run ahead of the recording worker's."""
    for i in range(4):
        kb.claim_task(conn, kb.create_task(conn, title=f'other {i}', assignee='a'), claimer=kb._claimer_id())


def _as_recorded_elsewhere(conn, tid, foreign, **changes):
    """Rewrite the latest stored episode as a synced copy naming the recording gateway's run id *foreign*."""
    row = conn.execute("SELECT id, payload, run_id FROM task_events WHERE task_id=? AND kind='quota_episode' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    payload = {**json.loads(row['payload']), 'run_id': foreign, 'episode_id': foreign, **changes}
    for key in [k for k, v in changes.items() if v is None]:
        payload.pop(key)
    conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row['id']))
    conn.commit()
    assert row['run_id'] > foreign  # this replica's id for the same run
    return row['run_id']


def test_outstanding_ignores_a_released_episode_recorded_under_foreign_run_ids(board, pool, capsys):
    """(a) The earlier episode's longer wall must not hold the card once a newer local run started a new one."""
    entries, _ = pool
    entries.append(_entry(1, exhausted_until=NOW + 5 * DAYS))  # the live pool never disproves either hint
    with kbc.connect() as conn:
        _advance_local_ids(conn)
        tid = kb.create_task(conn, title='two episodes', assignee='a')
        _reap(conn, tid)
        _as_recorded_elsewhere(conn, tid, 2, until=NOW + 5 * DAYS)
        assert hold_until(conn, tid, NOW + 60) == NOW + 5 * DAYS
    assert release.cmd(argparse.Namespace(task_id=tid, reason='stale hint')) == 0
    capsys.readouterr()
    with kbc.connect() as conn:
        assert episode(conn, tid) is None
        _reap(conn, tid)  # a newer local run hits a 3-day wall: a new episode
        _as_recorded_elsewhere(conn, tid, 3)
        assert episode(conn, tid)['episode_id'] == 3
        assert hold_until(conn, tid, NOW + 60) == NOW + 3 * DAYS  # never the released 5-day wall


def test_unhinted_backoff_expires_across_repeated_evaluations(board, pool, monkeypatch):
    """(b) A disproved hint stored without ``backoff_until`` keeps the backoff of its run's end, not of now."""
    entries, _ = pool
    entries += [_entry(1, exhausted_until=NOW + 3 * DAYS), _entry(2)]  # one account has room: hint disproved
    clock = [NOW]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    with kbc.connect() as conn:
        _advance_local_ids(conn)
        tid = kb.create_task(conn, title='pre-backoff_until episode', assignee='a')
        _reap(conn, tid)
        _as_recorded_elsewhere(conn, tid, 2, backoff_until=None)
        for clock[0] in (NOW + 60, NOW + 120, NOW + COOLDOWN - 1):
            assert hold_until(conn, tid, clock[0]) == NOW + COOLDOWN
        for clock[0] in (NOW + COOLDOWN + 1, NOW + 2 * COOLDOWN, NOW + 3 * COOLDOWN):
            assert hold_until(conn, tid, clock[0]) is None
