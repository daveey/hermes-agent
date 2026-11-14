"""A notice poller defers a published-index miss to a live holder instead of rebuilding the index. [daveey]

Card J (t_01a1d862f6e2fc9e): in P1 run11 ~23 of a Desktop serve's 202 kanban polls missed the host copy between a
replica commit and the holder's publish (1 s poll + ~0.2 s build) and each rebuilt the whole remote index locally:
5.65 s of the poller's 8.97 s. A miss also stayed memoized for the replica stamp, so the copy the holder then
published for that same stamp was never read. Bounds: one extra poll (DEFER_MAX_S) per missing state while a
holder is live and beating; any doubt about the holder, a profile home or explicit paths compute as before.
"""
import json
import os
import time

import pytest

from tests.tui_gateway.test_kanban_remote_notices import CHAT, _card, _hub
from tui_gateway import kanban_remote_index as kri
from tui_gateway import kanban_remote_notices as krn
from tui_gateway.kanban_replica_observer import fingerprint

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _fresh_memos():
    krn._reset_for_tests()
    yield
    krn._reset_for_tests()


def _done(tid, at=NOW - 60):
    return _card(tid, CHAT, status="done", at=at)


def _add(replica, card, seq):
    import sqlite3
    with sqlite3.connect(replica) as conn:
        conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)", (card["key"], json.dumps(card["task"]), seq))


@pytest.fixture
def own(tmp_path, monkeypatch):
    """The host's own replica and root-home ledger: the paths ``collect`` uses when given none."""
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    state.mkdir()
    (home / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    replica = _hub(state, [_done("t_seed")])
    ledger = home / "kanban" / "remote-notices.db"
    ledger.parent.mkdir()
    assert krn.hub_db_path() == replica and krn.ledger_path() == ledger
    return replica, ledger


def _lease(held=True, pid=None, age=0.0, interval=5.0, raw=None):
    from hermes_cli.host_loop_lease import lease_dir
    path = lease_dir() / f"{kri.LEASE}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {"name": kri.LEASE, "held": held, "pid": os.getpid() if pid is None else pid, "start": None,
            "role": "gateway", "priority": 2, "interval": interval, "heartbeat_at": time.time() - age}
    path.write_text(raw if raw is not None else json.dumps(meta))


def _count_computes(monkeypatch):
    calls, real = [], kri.compute
    monkeypatch.setattr(kri, "compute", lambda *a, **k: calls.append(a) or real(*a, **k))
    return calls


def _refs(items):
    return {item["ref"] for item in items}


def _poll():
    return krn.collect(CHAT, "default", now=NOW)


def test_a_miss_is_not_replayed_once_the_copy_for_that_stamp_is_published(tmp_path):
    replica = _hub(tmp_path, [_done("t_seed")])
    ledger = tmp_path / "kanban" / "remote-notices.db"
    ledger.parent.mkdir()
    stamp = fingerprint(replica)
    assert kri.lookup(replica, ledger, stamp, "default", (CHAT,)) is None  # nothing published yet
    assert kri.refresh(replica, ledger, now=NOW)  # the holder publishes the same replica state
    assert fingerprint(replica) == stamp
    hit = kri.lookup(replica, ledger, stamp, "default", (CHAT,))
    assert hit is not None and [c["key"] for c in hit[1][CHAT]] == ["default/default/t_seed"]


def test_live_holder_defers_the_miss_and_the_next_poll_reads_its_copy(own, monkeypatch):
    replica, ledger = own
    _lease()
    assert kri.refresh(replica, ledger, now=NOW)
    assert _refs(_poll()) == {"hub/default/default/t_seed"}
    _add(replica, _done("t_new"), 50)  # replica commit; the holder has not republished yet
    computes = _count_computes(monkeypatch)
    assert _poll() == [] and not computes  # deferred: nothing read, claimed or rebuilt
    assert kri.stats["deferred"] == 1
    assert kri.refresh(replica, ledger, now=NOW)  # the holder's next pass (it computes; the reader must not)
    computes.clear()
    assert _refs(_poll()) == {"hub/default/default/t_new"} and not computes


def test_deferral_is_bounded_by_one_poll_while_the_holder_never_publishes(own, monkeypatch):
    replica, ledger = own
    _lease()
    assert kri.refresh(replica, ledger, now=NOW)
    _poll()
    clock = [100.0]
    monkeypatch.setattr(kri, "_clock", lambda: clock[0])
    _add(replica, _done("t_new"), 50)
    computes = _count_computes(monkeypatch)
    assert _poll() == []
    clock[0] += kri.DEFER_MAX_S - 0.01
    _add(replica, _done("t_newer"), 51)  # replica churn: a new stamp must not restart the bound
    assert _poll() == [] and not computes
    clock[0] += 0.01
    assert _refs(_poll()) == {"hub/default/default/t_new", "hub/default/default/t_newer"} and len(computes) == 1


def test_a_beating_holder_that_never_publishes_does_not_defer_again_after_the_bound(own, monkeypatch):
    """A holder beats after a failed build too: once a chat's bound expired, its misses compute until a hit."""
    replica, ledger = own
    _lease()
    assert kri.refresh(replica, ledger, now=NOW)
    _poll()
    clock = [100.0]
    monkeypatch.setattr(kri, "_clock", lambda: clock[0])
    monkeypatch.setattr(kri, "publish", lambda *a, **k: False)  # no copy is ever published
    _add(replica, _done("t_new"), 50)
    computes = _count_computes(monkeypatch)
    assert _poll() == []  # deferred
    clock[0] += kri.DEFER_MAX_S
    assert _refs(_poll()) == {"hub/default/default/t_new"} and len(computes) == 1  # bound reached: computed
    for n in range(3):  # still no holder publish: every later missing state computes at once
        clock[0] += 5.0
        _add(replica, _done(f"t_more{n}"), 60 + n)
        assert f"hub/default/default/t_more{n}" in _refs(_poll()) and len(computes) == 2 + n
    assert kri.stats["deferred"] == 1


def test_a_wall_clock_heartbeat_from_the_future_is_not_a_beat(own, monkeypatch):
    replica, ledger = own
    _lease(age=-60.0)  # heartbeat_at a minute ahead: the wall clock stepped back
    assert kri.refresh(replica, ledger, now=NOW)
    _poll()
    _add(replica, _done("t_new"), 50)
    computes = _count_computes(monkeypatch)
    assert _refs(_poll()) == {"hub/default/default/t_new"} and len(computes) == 1
    assert kri.stats["deferred"] == 0


def test_a_hit_restarts_the_bound_for_the_next_missing_state(own, monkeypatch):
    replica, ledger = own
    _lease()
    assert kri.refresh(replica, ledger, now=NOW)
    _poll()
    clock = [100.0]
    monkeypatch.setattr(kri, "_clock", lambda: clock[0])
    _add(replica, _done("t_new"), 50)
    assert _poll() == []  # deferred at t=100
    assert kri.refresh(replica, ledger, now=NOW)
    clock[0] += 1.0
    assert _refs(_poll()) == {"hub/default/default/t_new"}  # hit: the chat is no longer deferring
    clock[0] += kri.DEFER_MAX_S + 1.0  # past the old first miss's bound
    _add(replica, _done("t_later"), 51)
    computes = _count_computes(monkeypatch)
    assert _poll() == [] and not computes  # a fresh deferral, not a compute charged to the old miss
    assert kri.stats["deferred"] == 2


def test_a_replica_with_no_change_key_never_defers(own, monkeypatch):
    replica, ledger = own
    _lease()
    assert kri.refresh(replica, ledger, now=NOW)
    _poll()
    _add(replica, _done("t_new"), 50)
    monkeypatch.setattr(kri, "current_key", lambda *a: None)  # e.g. a replica without seq: nothing is published
    computes = _count_computes(monkeypatch)
    assert "hub/default/default/t_new" in _refs(_poll()) and len(computes) == 1
    assert kri.stats["deferred"] == 0


@pytest.mark.parametrize("holder", ["absent", "released", "dead_pid", "stale_beat", "corrupt"])
def test_no_live_beating_holder_computes_the_miss_at_once(own, monkeypatch, holder):
    replica, ledger = own
    if holder == "released":
        _lease(held=False)
    elif holder == "dead_pid":
        _lease(pid=2 ** 22 + 12345)
    elif holder == "stale_beat":
        _lease(age=3 * 5.0 + 1, interval=5.0)
    elif holder == "corrupt":
        _lease(raw="{not json")
    assert kri.refresh(replica, ledger, now=NOW)
    _poll()
    _add(replica, _done("t_new"), 50)
    computes = _count_computes(monkeypatch)
    assert _refs(_poll()) == {"hub/default/default/t_new"} and len(computes) == 1
    assert kri.stats["deferred"] == 0


def test_a_profile_home_never_defers_to_the_root_holder(own, tmp_path, monkeypatch):
    _replica, _root_ledger = own
    _lease()  # the root's holder publishes only the root ledger's copy
    profile = tmp_path / "home" / "profiles" / "alpha"
    (profile / "kanban").mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    assert krn.ledger_path() == profile / "kanban" / "remote-notices.db"
    computes = _count_computes(monkeypatch)
    assert _refs(_poll()) == {"hub/default/default/t_seed"} and len(computes) == 1
    assert kri.stats["deferred"] == 0


def test_explicit_paths_never_defer(own, monkeypatch):
    replica, ledger = own
    _lease()
    assert kri.refresh(replica, ledger, now=NOW)
    _add(replica, _done("t_new"), 50)
    computes = _count_computes(monkeypatch)
    got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=NOW)
    assert "hub/default/default/t_new" in _refs(got) and len(computes) == 1
    assert kri.stats["deferred"] == 0
