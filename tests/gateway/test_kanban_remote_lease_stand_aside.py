"""The Desktop backend's remote sweep stands aside while the canonical gateway's turn holds a chat (t_01a1cff0541bf30c
G3). [daveey]

Live on the MacBook after PR #1486 (Nov 13, 02:24-03:51 PST): the sweep (every 15-25 s) reached busy gateway-owned
chat f07c32 before the gateway's owner pass did, claimed each remote notice, failed the passive row on the turn
lease, released it and logged "deferred: a turn is running" up to 6 times in a row per notice. While a live process
holds an unexpired lease the sweep can deliver nothing (row and wake are both refused), so it now leaves such a chat
alone without a claim; an expired lease, a dead holder or a holder that is not the gateway keep today's path.
"""
import contextlib
import logging
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_state_pidns import holder_namespace_token
from tui_gateway import kanban_remote_fifo as fifo_mod
from tui_gateway import kanban_remote_notices as krn
from tests.gateway import test_kanban_remote_owner_delivery as owner_tests
from tests.gateway.test_kanban_remote_owner_delivery import (
    CHAT, _dead_pid, _done, _key, _ledger, _publish, _rows, _sweep, _sweep_writes_row)

env = owner_tests.env  # the shared hub/board/state.db fixture

TID = "t_01a1d46fe87dff26"
KEY = _key(TID, gw="worker-3")


@pytest.fixture
def live_pids():
    procs = []

    def spawn():
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        procs.append(proc)
        return proc.pid
    yield spawn
    for proc in procs:
        proc.kill()
        proc.wait()


def _holder(pid):
    return f"pid={pid}{holder_namespace_token()}:turn={CHAT}:x:platform=local"


def _gateway(monkeypatch, pid):
    monkeypatch.setattr(fifo_mod, "verified_gateway_pids", lambda profile: (pid,), raising=False)


def _messages(caplog, text):
    return [r.getMessage() for r in caplog.records if text in r.getMessage()]


def _card():
    return _done(TID, gw="worker-3")


def test_sweep_leaves_a_gateway_leased_busy_chat_alone_no_claim_no_deferred(env, live_pids, monkeypatch, caplog):
    hub, _runner, _authority, _fifo, db = env
    gw = live_pids()
    _gateway(monkeypatch, gw)
    assert db.try_acquire_session_turn_lease(CHAT, _holder(gw))
    _publish(hub, [_card()])
    wakes = []
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            _sweep(wakes)
    assert wakes == [] and _rows(KEY) == []
    assert _ledger(TID, gw="worker-3") is None  # no claim taken, so none can race the gateway's owner pass
    assert _messages(caplog, "deferred") == []
    assert len(_messages(caplog, f"left to gateway pid {gw}")) == 1  # once per episode, not per pass
    db.release_session_turn_lease(CHAT, _holder(gw))


def test_gateway_lease_with_no_owner_pass_delivers_exactly_once_after_release(env, live_pids, monkeypatch):
    """notify_in_gateway=false (or a non-launch profile): nobody admits during the turn; the sweep takes over once
    the lease is released, and delivers once."""
    hub, _runner, _authority, _fifo, db = env
    gw = live_pids()
    _gateway(monkeypatch, gw)
    assert db.try_acquire_session_turn_lease(CHAT, _holder(gw))
    _publish(hub, [_card()])
    for _ in range(3):
        _sweep_writes_row(db)
    assert _rows(KEY) == [] and _ledger(TID, gw="worker-3") is None
    db.release_session_turn_lease(CHAT, _holder(gw))
    _sweep_writes_row(db)
    _sweep_writes_row(db)
    assert len(_rows(KEY)) == 1
    assert _ledger(TID, gw="worker-3")["delivered"] != 0


def test_a_dead_gateway_holder_keeps_the_sweeps_path(env, monkeypatch, caplog):
    hub, _runner, _authority, _fifo, db = env
    dead = _dead_pid()  # reaped: provably dead
    _gateway(monkeypatch, dead)
    assert db.try_acquire_session_turn_lease(CHAT, _holder(dead))
    _publish(hub, [_card()])
    with caplog.at_level(logging.INFO):
        _sweep([])
    assert len(_rows(KEY)) == 1  # the passive row reclaims the dead holder's lease
    assert _messages(caplog, "left to gateway pid") == []


def test_an_expired_lease_of_the_live_gateway_keeps_the_sweeps_path(env, live_pids, monkeypatch, caplog):
    """The passive-row guard treats an expired lease as stale even while its pid lives (a Mac that slept through
    the gateway's lease refresh), so the sweep must not stand aside then."""
    hub, _runner, _authority, fifo, db = env
    gw = live_pids()
    _gateway(monkeypatch, gw)
    assert db.try_acquire_session_turn_lease(CHAT, _holder(gw))
    fifo._write("UPDATE session_turn_leases SET expires_at = ?", (time.time() - 1,))
    _publish(hub, [_card()])
    with caplog.at_level(logging.INFO):
        _sweep([])
    assert len(_rows(KEY)) == 1
    assert _messages(caplog, "left to gateway pid") == []


def test_a_live_holder_that_is_not_the_gateway_keeps_todays_path(env, live_pids, monkeypatch, caplog):
    hub, _runner, _authority, _fifo, db = env
    gw, other = live_pids(), live_pids()
    _gateway(monkeypatch, gw)
    assert db.try_acquire_session_turn_lease(CHAT, _holder(other))
    _publish(hub, [_card()])
    with caplog.at_level(logging.INFO):
        _sweep([])
    assert _rows(KEY) == []
    assert _messages(caplog, "left to gateway pid") == []
    assert len(_messages(caplog, "deferred: a turn is running")) == 1  # unchanged: claimed, refused, released
    db.release_session_turn_lease(CHAT, _holder(other))


def _pid_lookups(monkeypatch, *, profile, default):
    """The two verified-pid lookups at their boundary: the profile home's gateway and the default root's."""
    from gateway import status
    import hermes_cli.gateway_multiplex_served as served
    monkeypatch.setattr(status, "live_gateway_pid_for_home", profile)
    monkeypatch.setattr(served, "live_default_gateway_pid", default)


@pytest.mark.parametrize("holds", ["profile", "default"])
def test_a_lease_held_by_either_verified_gateway_stands_the_sweep_aside(env, live_pids, monkeypatch, caplog, holds):
    """PR #2208 review nit 1: a profile gateway and a multiplexed default gateway can both be live. Only the
    profile's was matched, so a chat whose turn the default gateway ran fell back to claim/defer every pass."""
    hub, _runner, _authority, _fifo, db = env
    profile_gw, default_gw = live_pids(), live_pids()
    _pid_lookups(monkeypatch, profile=lambda home: profile_gw, default=lambda: default_gw)
    holder = profile_gw if holds == "profile" else default_gw
    assert db.try_acquire_session_turn_lease(CHAT, _holder(holder))
    _publish(hub, [_card()])
    wakes = []
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            _sweep(wakes)
    assert wakes == [] and _rows(KEY) == []
    assert _ledger(TID, gw="worker-3") is None
    assert _messages(caplog, "deferred") == []
    assert len(_messages(caplog, f"left to gateway pid {holder}")) == 1
    db.release_session_turn_lease(CHAT, _holder(holder))


def test_a_lease_on_the_compression_root_stands_aside_for_a_link_on_the_tip_then_delivers_once(
        env, live_pids, monkeypatch, caplog):
    """PR #2208 review nit 3: the turn lease sits on the lineage root while the link names the compression tip.
    The sweep must still see the gateway's lease (no claim, no wake), and deliver exactly once after release."""
    hub, _runner, _authority, _fifo, db = env
    db.end_session(CHAT, "compression")
    db.create_session(owner_tests.TIP, "desktop", parent_session_id=CHAT)
    gw = live_pids()
    _gateway(monkeypatch, gw)
    assert db.try_acquire_session_turn_lease(CHAT, _holder(gw))  # keyed on the root
    _publish(hub, [_done(TID, gw="worker-3", chat=owner_tests.TIP)])
    wakes = []
    with caplog.at_level(logging.INFO):
        for _ in range(3):
            _sweep(wakes)
    assert wakes == [] and owner_tests._lineage_rows(KEY) == []
    assert _tip_ledger() == []  # no claim under the tip (nor the root)
    assert _messages(caplog, "deferred") == []
    assert len(_messages(caplog, f"left to gateway pid {gw}")) == 1
    db.release_session_turn_lease(CHAT, _holder(gw))
    _sweep_writes_row(db)
    _sweep_writes_row(db)
    assert len(owner_tests._lineage_rows(KEY)) == 1
    assert _tip_ledger() == [(owner_tests.TIP, krn.DELIVERED)]


def _tip_ledger():
    with contextlib.closing(sqlite3.connect(krn.ledger_path())) as conn:
        return conn.execute("SELECT session, delivered FROM notices WHERE ref=?",
                            (f"worker-3/default/default/{TID}",)).fetchall()


def test_verified_gateway_pids_lists_the_profile_and_the_multiplexed_default_gateway(monkeypatch):
    _pid_lookups(monkeypatch, profile=lambda home: None, default=lambda: 4242)
    assert fifo_mod.verified_gateway_pids("household") == (4242,)
    _pid_lookups(monkeypatch, profile=lambda home: 77, default=lambda: 4242)
    assert fifo_mod.verified_gateway_pids("household") == (77, 4242)
    _pid_lookups(monkeypatch, profile=lambda home: 4242, default=lambda: 4242)
    assert fifo_mod.verified_gateway_pids("default") == (4242,)  # one gateway, listed once
    _pid_lookups(monkeypatch, profile=lambda home: (_ for _ in ()).throw(OSError("x")), default=lambda: 4242)
    assert fifo_mod.verified_gateway_pids("household") == (4242,)  # a failed lookup drops only its candidate
    _pid_lookups(monkeypatch, profile=lambda home: 77, default=lambda: (_ for _ in ()).throw(OSError("x")))
    assert fifo_mod.verified_gateway_pids("household") == (77,)
    _pid_lookups(monkeypatch, profile=lambda home: None, default=lambda: None)
    assert fifo_mod.verified_gateway_pids("household") == ()


def test_no_verified_gateway_means_no_stand_aside(env, live_pids, monkeypatch, caplog):
    hub, _runner, _authority, _fifo, db = env
    holder = live_pids()
    _pid_lookups(monkeypatch, profile=lambda home: None, default=lambda: None)
    assert db.try_acquire_session_turn_lease(CHAT, _holder(holder))
    _publish(hub, [_card()])
    with caplog.at_level(logging.INFO):
        _sweep([])
    assert _messages(caplog, "left to gateway pid") == []
    assert len(_messages(caplog, "deferred: a turn is running")) == 1
    db.release_session_turn_lease(CHAT, _holder(holder))
