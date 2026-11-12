"""The incident shape on the Board: the creator parks a card another machine claimed before its lease mirror
arrived (t_01a1d01a0a065d6b). [daveey]

On 2026-11-12 the MacBook's copy of t_01a1d017f26dc827 already had worker-2's open run and the Board's ``claimed``
event (holder worker-2) but no mirrored lease, so ``remote_live_lease`` did not fire. The park closed that run;
the close synced to worker-2, its worker exited without a terminal call and was booked a protocol violation.
Now the Board claim event alone is enough to refuse.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_remote_archive import _running_on_worker, _tool
from tests.hermes_cli.test_kanban_worker_node import WORKER

pytest_plugins = ["tests.hermes_cli.test_kanban_remote_archive"]  # gws, svc, kills


def _drop_lease_mirror(conn, tid):
    """The feed applied the run and the claim event; the lease mirror has not arrived."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET lease_holder = NULL, lease_expires = NULL WHERE id = ?", (tid,))


def test_creator_park_before_the_lease_mirror_arrives_is_refused_naming_the_claimer(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    _drop_lease_mirror(hub.conn, tid)
    run_id = kb._current_run_id(hub.conn, tid)
    claim = hub.conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
                             "AND json_extract(payload, '$.holder') IS NOT NULL", (tid, run_id)).fetchone()
    assert claim is not None and json.loads(claim[0])["holder"] == WORKER  # the fixture carries the incident's event
    out = _tool("_handle_schedule", {"task_id": tid, "reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z soak",
                                     "clock_only": True})
    assert out.get("code") == "live_claim_park" and out.get("holder") == WORKER, out
    assert out.get("claim_via") == "board_claim", out
    assert kb.get_task(hub.conn, tid).status == "running"
    assert hub.conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] is None
    hub.round()
    worker.round()
    worker.enter()
    wrun = kb._current_run_id(worker.conn, tid)
    assert worker.conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (wrun,)).fetchone()[0] is None


def test_an_expired_board_claim_does_not_hold_off_an_outside_park(gws, svc, kills):
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    _drop_lease_mirror(hub.conn, tid)
    from tools.kanban_tools_live_park import live_claim
    payload = json.loads(hub.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claimed' "
        "AND json_extract(payload, '$.holder') IS NOT NULL ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
    assert live_claim(hub.conn, tid, now=payload["expires"] / 1000 + 1) is None
    assert live_claim(hub.conn, tid)["holder"] == WORKER


def _claim_event(conn, tid):
    return conn.execute("SELECT id, payload FROM task_events WHERE task_id = ? AND run_id = ? AND kind = 'claimed' "
                        "AND json_extract(payload, '$.holder') IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (tid, kb._current_run_id(conn, tid))).fetchone()


def test_a_board_claim_naming_this_gateway_does_not_refuse(gws, svc, kills):
    """``holder == own``: the Board ``claimed`` event names this gateway while ``claim_lock`` is NULL (e.g. a local
    reclaim raced the feed). Our own claim with no live local worker protects no run, so the park is not refused."""
    from hermes_cli import kanban_db_sync as sync
    from tools.kanban_tools_live_park import live_claim
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    _drop_lease_mirror(hub.conn, tid)
    assert live_claim(hub.conn, tid)["via"] == "board_claim"  # the foreign claim is seen, so the gate is reached
    own = str(sync._state(hub.conn)["gateway_id"])
    event_id, payload = _claim_event(hub.conn, tid)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE task_events SET payload = ? WHERE id = ?",
                         (json.dumps({**json.loads(payload), "holder": own}), event_id))
    assert kb.get_task(hub.conn, tid).status == "running"
    assert live_claim(hub.conn, tid) is None


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_a_non_primary_board_does_not_consult_claim_events(gws, svc, kills, monkeypatch, mode):
    """Only a primary board's feed carries authoritative ``claimed`` events; any other mode never refuses on them."""
    from hermes_cli import kanban_db_sync as sync
    from tools.kanban_tools_live_park import live_claim
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    _drop_lease_mirror(hub.conn, tid)
    assert live_claim(hub.conn, tid)["via"] == "board_claim"
    monkeypatch.setattr(sync, "board_mode", lambda conn: mode)
    assert live_claim(hub.conn, tid) is None


def test_cli_creator_park_before_the_lease_mirror_arrives_is_refused(gws, svc, kills, capsys):
    """The CLI path of the incident shape: ``hermes kanban schedule <id>`` on the creator's replica."""
    import argparse

    from hermes_cli import kanban as kcli
    hub, worker = gws
    tid = _running_on_worker(hub, worker, svc)
    hub.enter()
    _drop_lease_mirror(hub.conn, tid)
    run_id = kb._current_run_id(hub.conn, tid)
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    rc = kcli.kanban_command(parser.parse_args(["kanban", "schedule", tid,
                                                "SCHEDULED_UNTIL=2099-01-01T00:00:00Z soak", "--clock-only"]))
    assert rc != 0
    assert WORKER in capsys.readouterr().err
    assert kb.get_task(hub.conn, tid).status == "running"
    assert hub.conn.execute("SELECT ended_at FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] is None
