"""Passive listeners must retain recovery when execution custody is unhealthy."""
import time

import pytest

from hermes_cli import config
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_passive_intake as passive_helpers
from tests.hermes_cli import test_kanban_wake_check as helpers
from tests.hermes_cli import test_kanban_wake_check_home as custody

kanban_home = helpers.kanban_home
listener = passive_helpers.listener
gws = custody.gws


def test_passive_local_probe_loss_restores_once_then_wakes(listener):
    conn, tid, _ = listener
    custody._lose(conn, tid)
    assert wc.restore_missing_checks(conn) == {"restored": [tid], "woken": []}
    assert wc.get_state(conn, tid)["cmd"] == "exit 1"
    assert wc.restore_missing_checks(conn) == {"restored": [], "woken": []}
    custody._lose(conn, tid)
    assert wc.restore_missing_checks(conn) == {"restored": [], "woken": [tid]}
    assert kb.get_task(conn, tid).status == "ready"
    assert helpers._events(conn, tid, wc.LOST_EVENT)[-1].payload["reason"] == "lost_again"
    assert wc.restore_missing_checks(conn) == {"restored": [], "woken": []}
    assert len(helpers._events(conn, tid, wc.LOST_EVENT)) == 1


def _passive_relay(gws, tmp_path, monkeypatch):
    home, worker = gws
    policy = {}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"passive_intake": policy}})
    flag = tmp_path / "intake"
    tid = custody._relayed_park(home, worker, flag)
    policy[tid] = custody._probe(flag)
    home.round()
    home.enter()
    return home, worker, tid, policy


def test_passive_remote_silence_still_wakes_once(gws, tmp_path, monkeypatch):
    home, _worker, tid, _policy = _passive_relay(gws, tmp_path, monkeypatch)
    park = kc.park_state(home.conn, tid)
    deadline = park["parked_at"] + wc.ALIVE_EVERY + 600 + wc.SILENT_GRACE
    assert wc.restore_missing_checks(home.conn, now=deadline) == {"restored": [], "woken": []}
    assert wc.get_state(home.conn, tid) is None
    assert wc.restore_missing_checks(home.conn, now=deadline + 1) == {"restored": [], "woken": [tid]}
    assert helpers._events(home.conn, tid, wc.LOST_EVENT)[-1].payload["reason"] == "remote_probe_silent"
    assert wc.restore_missing_checks(home.conn, now=deadline + 2) == {"restored": [], "woken": []}
    assert len(helpers._events(home.conn, tid, wc.LOST_EVENT)) == 1


def test_passive_remote_heartbeat_extends_silence_deadline(gws, tmp_path, monkeypatch):
    home, worker, tid, _policy = _passive_relay(gws, tmp_path, monkeypatch)
    parked_at = kc.park_state(home.conn, tid)["parked_at"]
    heartbeat_at = parked_at + wc.ALIVE_EVERY + 1
    worker.enter()
    # Exercise the actual heartbeat producer, then transport its event home.
    monkeypatch.setattr(wc.time, "time", lambda: heartbeat_at)
    assert wc.run_due_wake_checks(worker.conn, now=heartbeat_at, wait=True)["submitted"] == [tid]
    worker.round()
    home.round()
    home.enter()
    assert helpers._events(home.conn, tid, wc.ALIVE_EVENT)
    old_deadline = parked_at + wc.ALIVE_EVERY + 600 + wc.SILENT_GRACE + 1
    assert wc.restore_missing_checks(home.conn, now=old_deadline) == {"restored": [], "woken": []}
    deadline = heartbeat_at + wc.ALIVE_EVERY + 600 + wc.SILENT_GRACE + 1
    assert wc.restore_missing_checks(home.conn, now=deadline) == {"restored": [], "woken": [tid]}


@pytest.mark.parametrize("passive_enabled", [False, True])
def test_unobserved_local_executor_retains_overdue_timer_recovery(listener, passive_enabled):
    conn, tid, policy = listener
    if not passive_enabled:
        policy.clear()
    # A registration is not liveness: the executor never produces even one result.
    state = wc.get_state(conn, tid)
    assert state["last_at"] is None
    now = int(time.time()) + 49 * 3600
    assert wc.restore_missing_checks(conn, now=now) == {"restored": [], "woken": []}
    woken, _deadline = kc._wake_parks(conn, now)
    assert woken == [tid], "a never-observed executor must not gain an indefinite timer exemption"


@pytest.mark.parametrize("passive_enabled", [False, True])
def test_split_custody_cannot_hide_remote_silence_and_timer(gws, tmp_path, monkeypatch, passive_enabled):
    home, worker, tid, policy = _passive_relay(gws, tmp_path, monkeypatch)
    if not passive_enabled:
        policy.clear()
    # A stale local execution row coexists with the park's designated remote owner.
    # This is not proof that either executor is running.
    with kb.write_txn(home.conn):
        wc.set_check(home.conn, tid, custody._probe(tmp_path / "intake"), 300)
    assert wc.get_state(home.conn, tid) is not None
    assert wc.get_state(worker.conn, tid) is not None
    park = kc.park_state(home.conn, tid)
    now = int(park["payload"]["wake_at"]) + 1
    recovered = wc.restore_missing_checks(home.conn, now=now)["woken"]
    woken, _deadline = kc._wake_parks(home.conn, now)
    assert tid in recovered + woken, "split custody must not disable both silence detection and timer recovery"
