"""``quota-release`` closes an episode whose stored payload carries another gateway's run ids. [daveey]

t_01a1d052f773722d: a stored ``quota_episode`` payload keeps the run ids of the gateway that recorded it
(worker-2's run 2392) while the event row and ``task_runs`` use this replica's (the hub's 173967). The
release copied the payload's id, so ``episode()`` compared 173967 <= 2392, found the runs unreleased and
kept the 4 released cards held on the hub and every worker replica.
"""
import argparse
import json

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_quota_episode_release as release
from hermes_cli import kanban_queue_position as queue
from hermes_cli.kanban_quota_episode import episode
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing  # noqa: PLC0414 -- fixture re-export
from tests.hermes_cli.test_kanban_quota_episode_live_release import NOW, DAYS, _entry, _reap, pool as pool  # noqa: PLC0414 -- fixture re-export

FOREIGN_RUN = 2  # the recording worker's run id, lower than this replica's


def _as_recorded_elsewhere(conn, tid):
    """Rewrite the stored episode as a synced copy: the payload names the recording gateway's run ids."""
    row = conn.execute("SELECT id, payload, run_id FROM task_events WHERE task_id=? AND kind='quota_episode' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    payload = json.loads(row['payload'])
    payload.update(run_id=FOREIGN_RUN, episode_id=FOREIGN_RUN)
    conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), row['id']))
    conn.commit()
    assert row['run_id'] > FOREIGN_RUN  # this replica's id for the same run
    return row['run_id']


def test_release_of_an_episode_recorded_on_another_gateway_ends_the_hold(board, pool, capsys):
    entries, _ = pool
    entries.append(_entry(1, exhausted_until=NOW + 3 * DAYS))  # the live pool still says walled
    with kbc.connect() as conn:
        for i in range(4):  # earlier runs on this replica: its ids run ahead of the recording worker's
            kb.claim_task(conn, kb.create_task(conn, title=f'other {i}', assignee='a'), claimer=kb._claimer_id())
        tid = kb.create_task(conn, title='released on the home', assignee='a')
        _reap(conn, tid)
        local = _as_recorded_elsewhere(conn, tid)
        assert episode(conn, tid)['run_id'] == FOREIGN_RUN
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
    assert release.cmd(argparse.Namespace(task_id=tid, reason='stale outage hint')) == 0
    assert 'Released' in capsys.readouterr().out
    with kbc.connect() as conn:
        assert release.released_run(conn, tid) == local
        assert episode(conn, tid) is None
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert 'quota hold released (stale outage hint, via operator)' in queue.describe(
            queue.position(conn, kb.get_task(conn, tid)))


def test_a_computed_episode_keeps_its_own_run_id(board, pool, capsys):
    """No stored payload names the released run: its id is this replica's already."""
    entries, _ = pool
    entries.append(_entry(1, exhausted_until=NOW + 3 * DAYS))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='computed', assignee='a')
        state = _reap(conn, tid)
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='quota_episode'", (tid,))
        conn.commit()
    assert release.cmd(argparse.Namespace(task_id=tid, reason='x')) == 0
    with kbc.connect() as conn:
        assert release.released_run(conn, tid) == state['run_id']
        assert episode(conn, tid) is None
