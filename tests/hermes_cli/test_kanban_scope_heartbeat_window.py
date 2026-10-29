"""Scope-heartbeat freshness follows the owner's published collector period, in every reader. [daveey]

A loaded hub refreshed each scope heartbeat every ~440 s while every reader used a fixed 450 s window, so terminal
remote cards flapped between released and "never released" (session-idle-check, 2026-10-29). The owner now
publishes ``scope_heartbeat_period_s`` and ``kanban_scope_heartbeat`` derives each owner's window from it.
"""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_scope_heartbeat as hb
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_wait_check as wait
from tests.hermes_cli import test_kanban_remote_terminal_release as release
from tests.hermes_cli.test_kanban_remote_terminal_release import NOW, card, counted, observed
from tests.hermes_cli.test_kanban_wait_replica import TID, pin_replica_clock, put
from tests.hermes_cli.test_kanban_wait_replica import replica as replica  # noqa: PLC0414 -- pytest fixture
from tests.hermes_cli.test_kanban_origin_ledger import world as world  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_scope_heartbeat import project
from tests.hermes_cli.test_kanban_origin_scope_heartbeat import published as published  # noqa: PLC0414 -- fixture


# ---------------------------------------------------------------- the window rule


@pytest.mark.parametrize('info,window', [
    ({}, 450.0),                                         # an older owner publishes no period: the floor
    ({'scope_heartbeat_period_s': 60}, 450.0),           # a fast collector never shrinks the window below the floor
    ({'scope_heartbeat_period_s': 440}, 1100.0),         # the loaded hub of 2026-10-29
    ({'scope_heartbeat_period_s': 100_000}, 1800.0),     # an offline owner's last word still expires
    ({'scope_heartbeat_period_s': True}, 450.0),         # malformed periods prove nothing
    ({'scope_heartbeat_period_s': -5}, 450.0),
    ({'scope_heartbeat_period_s': float('nan')}, 450.0),
    ({'scope_heartbeat_period_s': '440'}, 450.0),
    (None, 450.0),
])
def test_window_derives_from_published_period(info, window):
    assert hb.scope_window(info) == window


@pytest.mark.parametrize('period', [1, 30, 60, 120, 180, 240, 300, 440, 600, 720])
def test_window_covers_two_and_a_half_collector_periods(period):
    """The invariant the fixed constant broke: up to the cap, a heartbeat stays fresh for >= 2.5 periods, so one
    late cycle cannot expire it."""
    assert hb.window_for_period(period) >= 2.5 * period


def test_floor_still_covers_the_nominal_collector_period():
    # hermes-hub's watch runs a full sync every 60 s; with a typical board read it republishes every ~180 s.
    assert hb.SCOPE_HEARTBEAT_FLOOR_S >= 2.5 * 180
    assert activity.SCOPE_HEARTBEAT_FRESH_S == hb.SCOPE_HEARTBEAT_FLOOR_S


# ---------------------------------------------------------------- remote-terminal release (session sidebar counts)


def _scopes(beat):
    return [{'profile': 'default', 'board': 'default', 'state': 'ok', 'observed_at': beat}]


def _with_period(path, period):
    with sqlite3.connect(path) as db:
        info = json.loads(db.execute("SELECT data FROM replica WHERE coll='gateway'").fetchone()[0])
        info['scope_heartbeat_period_s'] = period
        db.execute("UPDATE replica SET data=? WHERE coll='gateway'", (json.dumps(info),))
    return path


@pytest.fixture(autouse=True)
def _origin_profile(monkeypatch):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')


def test_slow_owner_heartbeat_past_the_floor_still_releases_a_done_card(tmp_path):
    """The 2026-10-29 flap: the hub's heartbeat was 441 s apart, so for part of each cycle it was > 450 s old."""
    path = release.replica(tmp_path, [('default/default/t_1', observed(card('done')))], beat=NOW - 500)
    assert len(counted(activity._read_remote_links(path, now=NOW))) == 1  # no period published: floor, not released
    _with_period(path, 440)
    activity._remote_read_memo = None
    rows = activity._read_remote_links(path, now=NOW)
    assert counted(rows) == []
    assert [r['released'] for r in rows] == [True]


def test_slow_owner_heartbeat_still_expires_past_its_own_window(tmp_path):
    path = _with_period(release.replica(tmp_path, [('default/default/t_1', observed(card('done')))], beat=NOW - 1200), 440)
    assert len(counted(activity._read_remote_links(path, now=NOW))) == 1


def test_projection_horizon_uses_each_owners_window(tmp_path):
    path = _with_period(release.replica(tmp_path, [], beat=NOW - 100), 440)
    with sqlite3.connect(path) as db:
        info_rows = list(db.execute("SELECT gw, data FROM replica WHERE coll='gateway'"))
    assert hb.scope_heartbeats(info_rows) == {('owner', 'default', 'default'): (NOW - 100, 1100.0)}
    horizon: list = []
    activity._project_replica(path, NOW, 'origin', [], info_rows, NOW, None, horizon_out=horizon)
    assert NOW - 100 + 1100.0 in horizon


# ---------------------------------------------------------------- origin ledger (owner liveness)


def _ledger_info(world, binding, *, observed_at, period=None):
    _, replica_path, _ = world
    with sqlite3.connect(replica_path) as db:
        info = json.loads(db.execute("SELECT data FROM replica WHERE coll='gateway' AND key='info'").fetchone()[0])
        info['task_scopes'] = [{'profile': binding['owner']['profile'], 'board': binding['owner']['board'],
                                'state': 'ok', 'observed_at': observed_at}]
        if period is not None:
            info['scope_heartbeat_period_s'] = period
        db.execute("UPDATE replica SET data=? WHERE coll='gateway' AND key='info'", (json.dumps(info),))


def test_ledger_keeps_a_slow_owner_live_within_its_window(world, published):
    _, binding = published
    _ledger_info(world, binding, observed_at=500)  # 501 s old at now=1001
    assert project(world)['status'] == 'unknown'
    _ledger_info(world, binding, observed_at=500, period=440)
    row = project(world)
    assert row['status'] == 'ready' and not row['stale']


# ---------------------------------------------------------------- wait probes (kanban_wait_replica)


def _wait_scope(db, age, period=None):
    info = {"task_scopes": [{"profile": "default", "board": "default", "state": "ok", "observed_at": time.time() - age}]}
    if period is not None:
        info["scope_heartbeat_period_s"] = period
    db.execute("UPDATE replica SET data=? WHERE coll='gateway'", (json.dumps(info),))
    db.commit()


def test_wait_probe_does_not_call_a_slow_owners_fresh_heartbeat_stale(replica, monkeypatch):
    put(replica, "done")
    pin_replica_clock(monkeypatch)
    _wait_scope(replica, 700)
    assert wait.check_remote("other", "task", TID)[0] == 3  # past the 600 s probe limit: unknown, not yet
    _wait_scope(replica, 700, period=440)
    assert wait.check_remote("other", "task", TID)[0] == 0  # inside that owner's 1100 s window: met
    _wait_scope(replica, 1237, period=440)
    code, text = wait.check_remote("other", "task", TID)
    assert code == 3 and "limit=1100s" in text
