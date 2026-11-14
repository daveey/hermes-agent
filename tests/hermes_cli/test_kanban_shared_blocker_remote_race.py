"""A remote claim absent from the home snapshot still predates recovery."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from hermes_cli import kanban_timed_park as park
from tests.hermes_cli import test_kanban_shared_blocker_check_transport as transport
from tests.hermes_cli import test_kanban_shared_blockers_copies as copies
from tests.hermes_cli.test_kanban_wake_check_home import BUILD, _probe

gws = copies.gws
GH = 'github-rest-quota'


@pytest.mark.parametrize('before', [True, False])
def test_unsynced_same_second_claim_order(gws, tmp_path, monkeypatch, before):
    home, worker = gws
    monkeypatch.setattr(copies, 'BID', GH)
    carrier = transport._park_with_remapped_run(home, worker, tmp_path / 'carrier')
    home.enter()
    tid = kb.create_task(home.conn, title='remote in flight', assignee='default', runs_on=['linux'])
    home.round()
    worker.round()
    cfg = {'kanban': {'shared_blocker_checks': {GH: 'exit 0'}}}
    second = int(time.time()) + 10
    clock = [second + 0.1]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    monkeypatch.setattr(time, 'time_ns', lambda: int(clock[0] * 1_000_000_000))

    def check():
        clock[0] = second + 0.5
        home.enter()
        assert checks.run_due_checks(home.conn, cfg=cfg) == [GH]
        receipt = checks._latest_results(home.conn)[GH]
        assert receipt['active_runs'] == []
        assert home.conn.execute('SELECT 1 FROM task_runs WHERE task_id=?', (tid,)).fetchone() is None

    if not before:
        check()
    clock[0] = second + (0.1 if before else 0.9)
    worker.enter()
    worker.tick()
    assert kb.get_task(worker.conn, tid).status == 'running'
    if before:
        check()
    worker.enter()
    clock[0] = second + 2.0
    with kb.write_txn(worker.conn):
        assert park.park(worker.conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z quota',
                         requested_kind='time', source_status='running',
                         wake_check=_probe(tmp_path / 'remote'), check_every=300,
                         awaiting=BUILD, blocker_id=GH)
    worker.round()
    home.round()
    worker.round()
    runs = [g.conn.execute('SELECT id, uid, started_at FROM task_runs WHERE task_id=?', (tid,)).fetchone()
            for g in (home, worker)]
    assert runs[0]['uid'] == runs[1]['uid']
    assert runs[0]['id'] != runs[1]['id']
    assert int(runs[0]['started_at']) == second
    for gateway in (home, worker):
        assert blockers.blocker_states(gateway.conn)[GH].is_open is (not before)
    worker.enter()
    assert blockers.release_cleared_blockers(worker.conn) == []
    home.enter()
    released = blockers.release_cleared_blockers(home.conn)
    assert set(released) == ({carrier, tid} if before else set())
