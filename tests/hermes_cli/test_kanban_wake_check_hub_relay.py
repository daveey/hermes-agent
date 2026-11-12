"""A copy of a hub card re-parked on a worker node with a probe that reads the board relays that probe to the hub.
[daveey]

t_01a1cd7f37cfb8a7 (a hub card) was re-parked on worker-2 with ``sqlite3.connect(Path.home()/".hermes/kanban.db")``
watching for hub-created selfheal FIX cards. ``park_fields`` stamped ``probe_host=worker-2``, so worker-2 ran the
probe against its OWN board, where hub-created cards never appear: rc 1 ("not yet") forever, with
``wake_check_alive host: worker-2`` events saying all was well. The park now names the hub as ``probe_host``: the
worker registers no probe and skips the dry run (it would read the wrong board), and the hub registers the probe
from the synced park event and runs it against the hub's board. A probe that reads something local to the worker
(its workspace script), a probe already routed remotely, the worker's own card and a non-hub card stay unchanged.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_continuation as kcont
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_timed_park as ktp
from hermes_cli import kanban_wake_check as kwc
from hermes_cli import kanban_wake_check_hub_relay as relay
from tests.hermes_cli import test_kanban_wake_check_home as home_tests
from tests.hermes_cli.test_kanban_worker_node import HUB, WORKER

gws = home_tests.gws
BUILD = home_tests.BUILD
RESTORED = home_tests.RESTORED
UNTIL = "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the hub's FIX card"

# The board probe: exits 0 once a card titled "selfheal FIX" exists on the board of the host that runs it.
BOARD_PROBE = ("/usr/bin/env python3 -S -c 'import sqlite3,pathlib,sys; "
               "c=sqlite3.connect(\"file:\"+str(pathlib.Path(__import__(\"os\").environ[\"HERMES_HOME\"])/"
               "\"kanban.db\")+\"?mode=ro\",uri=True); "
               "sys.exit(0 if c.execute(\"select 1 from tasks where title=?\",(\"selfheal FIX\",)).fetchone() else 1)'")
# The probe from t_01a1cd7f37cfb8a7, verbatim.
EVIDENCE_PROBE = ("/usr/bin/python3 -S -c 'import sqlite3,pathlib,sys; c=sqlite3.connect(\"file:\"+str(pathlib.Path."
                  "home()/\".hermes/kanban.db\")+\"?mode=ro\",uri=True); rows=c.execute(\"select id from tasks where "
                  "idempotency_key like ? and created_at >= strftime(?,?)\",(\"selfheal%:fix\",\"%s\",\"2026-11-12 "
                  "00:34:00\")).fetchall(); print(rows); sys.exit(0 if rows else 1)'")


@pytest.mark.parametrize("cmd", [
    EVIDENCE_PROBE,
    BOARD_PROBE,
    "test \"$(sqlite3 ~/.hermes/kanban.db \"select count(*) from tasks where id='t_7e2ee45c' and status='done'\")\" = 1",
    "sqlite3 -readonly /home/ubuntu/.hermes/kanban/boards/bb/kanban.db 'select 1' | grep -q 1",
    "python3 -c 'import os,sqlite3; sqlite3.connect(os.environ[\"HERMES_KANBAN_DB\"])'",
])
def test_a_probe_that_reads_a_board_on_the_host_that_runs_it(cmd):
    assert relay.reads_home_board(cmd)


@pytest.mark.parametrize("cmd", [
    # a worker-local fact: its own workspace / wake-checks script (default and named boards)
    "bash /home/ubuntu/.hermes/kanban/workspaces/t_01a1c8609d476871/train-ci-probe.sh",
    "/home/ubuntu/.hermes/kanban/boards/hermes/workspaces/t_01a1d01a9830c8cb/wake_probe.sh",
    "/home/ubuntu/.hermes/kanban/wake-checks/t_01a1cf843e21f1b3-pr1131-head-moved.sh",
    "python3 \"$HERMES_KANBAN_WORKSPACE/probe.py\" ~/.hermes/kanban.db",
    "python3 /home/ubuntu/.hermes/kanban/workspaces/t_x/probe.py ~/.hermes/kanban.db",
    # already routed to where the data lives
    "ssh -o BatchMode=yes hermes-hub \"sqlite3 -readonly /home/ubuntu/.hermes/kanban.db 'select 1'\"",
    "Q='select 1'; if [ \"$(hostname)\" = hermes-hub ]; then sqlite3 -readonly ~/.hermes/kanban.db \"$Q\"; "
    "else ssh hermes-hub sqlite kanban \"$Q\"; fi | grep -q 1",
    "hub-admin 'sqlite3 -readonly ~/.hermes/kanban.db \"select 1\"' | grep -q 1",
    "/usr/bin/ssh hub-admin test -f /home/ubuntu/.hermes/kanban.db",
    # no board read at all
    "hermes kanban wait-check task t_01a1cd7f37cfb8a7 --gateway hub --until done,blocked",
    "git -C ~/.hermes/hermes-agent merge-base --is-ancestor abc123 HEAD",
    "test -f /home/ubuntu/.hermes/kanban/evidence/x/summary.json",
    "token-broker exec --scope user/mac-mini-ssh -- bash /tmp/kanban.dbx",
    "",
    None,
])
def test_a_probe_with_no_unrouted_board_read_stays_local(cmd):
    assert not relay.reads_home_board(cmd)


def test_only_a_hub_home_takes_the_relay(gws, monkeypatch):
    """A Mac card the hub ran with a hub ``kanban.db`` probe meant the hub's board (t_b2390590 waited on hub cards);
    relaying it to the Mac would make it silent. Only a hub home takes it."""
    from hermes_cli import kanban_capability_routing as kcr

    _hub, worker = gws
    monkeypatch.setattr(kcr, "card_home", lambda conn, tid: "macbook")
    assert relay.relay_host(worker.conn, "t_x", EVIDENCE_PROBE) is None
    monkeypatch.setattr(kcr, "card_home", lambda conn, tid: "hermes-hub")
    assert relay.relay_host(worker.conn, "t_x", EVIDENCE_PROBE) == "hermes-hub"
    # No board or no card (a caller without them): no relay, even when every card here is a hub card.
    assert relay.relay_host(None, "t_x", EVIDENCE_PROBE) is None
    assert relay.relay_host(worker.conn, None, EVIDENCE_PROBE) is None
    monkeypatch.setattr(kcr, "card_home", lambda conn, tid: None)   # the parker's own card
    assert relay.relay_host(worker.conn, "t_x", EVIDENCE_PROBE) is None


# --- the park paths ----------------------------------------------------------------------------------------------

def _running_copy(hub, worker, title="hub card run on the worker"):
    hub.enter()
    tid = kb.create_task(hub.conn, title=title, assignee="default", runs_on=["linux"])
    hub.round()
    worker.round()
    worker.tick()
    assert home_tests._status(worker.conn, tid) == "running"
    return tid, kb.get_task(worker.conn, tid).current_run_id


def _park_block(conn, tid, run_id, cmd):
    with kb.write_txn(conn):
        assert ktp.park(conn, tid, reason=UNTIL, requested_kind="time", source_status="running",
                        expected_run_id=run_id, wake_check=cmd, check_every=300, awaiting=BUILD)


def _park_schedule(conn, tid, run_id, cmd):
    assert kb.schedule_task(conn, tid, reason=UNTIL, expected_run_id=run_id, wake_check=cmd, check_every=300,
                            awaiting=BUILD)


def _park_continue(conn, tid, run_id, cmd):
    res = kcont.continue_task(conn, tid, checkpoint="waiting for the hub's FIX card", resume_after_seconds=86400,
                              expected_run_id=run_id, wake_check=cmd, check_every=300, awaiting=BUILD)
    assert res["ok"], res


PARKS = {"block": _park_block, "schedule": _park_schedule, "continue": _park_continue}


def _last_park(conn, tid):
    kinds = kcont.SCHEDULED_ENTRY_EVENTS
    return [e for e in kb.list_events(conn, tid) if e.kind in kinds][-1].payload


@pytest.mark.parametrize("op", sorted(PARKS))
def test_a_worker_reparking_a_hub_card_on_a_board_probe_relays_it_to_the_hub(gws, op):
    hub, worker = gws
    tid, run_id = _running_copy(hub, worker)
    worker.enter()
    PARKS[op](worker.conn, tid, run_id, BOARD_PROBE)
    assert home_tests._status(worker.conn, tid) == "scheduled"
    assert _last_park(worker.conn, tid)["probe_host"] == HUB
    assert kwc.get_state(worker.conn, tid) is None, "the worker registered a probe that reads its own board"

    # A FIX card the probe waits for exists only on the hub's board; the worker's board never gets it.
    hub.enter()
    kb.create_task(hub.conn, title="selfheal FIX", assignee="default")
    worker.round()   # push the park
    hub.round()      # the hub receives it
    assert home_tests._status(hub.conn, tid) == "scheduled"
    assert _last_park(hub.conn, tid)["probe_host"] == HUB
    assert kwc.restore_missing_checks(hub.conn)["restored"] == [tid]
    assert kwc.get_state(hub.conn, tid)["cmd"] == BOARD_PROBE
    assert home_tests._events(hub.conn, tid, RESTORED)
    kwc.run_due_wake_checks(hub.conn, wait=True)
    assert home_tests._status(hub.conn, tid) == "ready", "the hub's probe did not see the hub's FIX card"


@pytest.mark.parametrize("op", sorted(PARKS))
def test_a_worker_local_probe_on_a_hub_card_stays_on_the_worker(gws, tmp_path, op):
    hub, worker = gws
    tid, run_id = _running_copy(hub, worker)
    script = tmp_path / ".hermes" / "kanban" / "workspaces" / tid / "probe.sh"
    cmd = f"bash {script} ~/.hermes/kanban.db"
    worker.enter()
    PARKS[op](worker.conn, tid, run_id, cmd)
    assert _last_park(worker.conn, tid)["probe_host"] == WORKER
    assert kwc.get_state(worker.conn, tid)["cmd"] == cmd


@pytest.mark.parametrize("op", sorted(PARKS))
def test_the_workers_own_card_keeps_its_board_probe(gws, op):
    _hub, worker = gws
    worker.enter()
    tid = kb.create_task(worker.conn, title="the worker's own card", assignee="default")
    worker.tick()
    assert home_tests._status(worker.conn, tid) == "running"
    PARKS[op](worker.conn, tid, kb.get_task(worker.conn, tid).current_run_id, BOARD_PROBE)
    assert _last_park(worker.conn, tid)["probe_host"] == WORKER
    assert kwc.get_state(worker.conn, tid)["cmd"] == BOARD_PROBE


# --- the park-time dry run --------------------------------------------------------------------------------------

ALREADY_TRUE_ON_THE_WORKER = "echo ~/.hermes/kanban.db; exit 0"


def test_a_relayed_probe_skips_the_dry_run_on_the_worker(gws, monkeypatch):
    """The dry run would read the worker's board: an answer about the wrong board refuses nothing."""
    from tools import kanban_tools_park_args as tpa

    hub, worker = gws
    monkeypatch.delenv("HERMES_KANBAN_WAKE_DRYRUN", raising=False)
    tid, _ = _running_copy(hub, worker)
    own = kb.create_task(worker.conn, title="own", assignee="default")
    args = {"wake_check": ALREADY_TRUE_ON_THE_WORKER}
    assert tpa._wake_check_args(args, conn=worker.conn, tid=tid)[0] == ALREADY_TRUE_ON_THE_WORKER
    with pytest.raises(Exception, match=kwc.WAKE_CHECK_ALREADY_TRUE):
        tpa._wake_check_args(args, conn=worker.conn, tid=own)
    with pytest.raises(Exception, match=kwc.WAKE_CHECK_ALREADY_TRUE):
        tpa._wake_check_args(args)


def test_the_cli_skips_the_dry_run_only_when_every_card_relays(gws, monkeypatch):
    import argparse

    from hermes_cli import kanban_cli_park as kcp

    hub, worker = gws
    monkeypatch.delenv("HERMES_KANBAN_WAKE_DRYRUN", raising=False)
    tid, _ = _running_copy(hub, worker)
    own = kb.create_task(worker.conn, title="own", assignee="default")
    args = argparse.Namespace(wake_check=ALREADY_TRUE_ON_THE_WORKER, check_every=None, clock_only=False)
    assert kcp._cli_wake_check(args, conn=worker.conn, ids=[tid])[0] == ALREADY_TRUE_ON_THE_WORKER
    for ids in ([tid, own], [own], []):
        with pytest.raises(ValueError, match=kwc.WAKE_CHECK_ALREADY_TRUE):
            kcp._cli_wake_check(args, conn=worker.conn, ids=ids)
    with pytest.raises(ValueError, match=kwc.WAKE_CHECK_ALREADY_TRUE):
        kcp._cli_wake_check(args)
