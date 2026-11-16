"""Standing listeners exempt only healthy quiet waiting, never probe failures."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as wc
from hermes_cli import kanban_continuation as kc
from tests.hermes_cli import test_kanban_wake_check as helpers

kanban_home = helpers.kanban_home
AWAITING = {"kind": "external", "ref": "inbox senders", "why": "new messages arrive here"}


@pytest.fixture
def listener(kanban_home, monkeypatch):
    from hermes_cli import config
    policy = {}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"passive_intake": policy}})
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        policy[tid] = "exit 1"
        assert kb.schedule_task(conn, tid, reason="waiting", wake_check="exit 1", awaiting=AWAITING)
        yield conn, tid, policy


def apply(listener, now, rc=1, out=""):
    conn, tid, _ = listener
    state = wc.get_state(conn, tid)
    return wc.apply_result(conn, tid, state["cmd"], rc, out, generation=state["generation"], now=now)


def test_quiet_episode_and_expired_timer(listener):
    conn, tid, _ = listener
    now = int(time.time())
    park = kc.park_state(conn, tid)
    assert apply(listener, now) is None
    assert apply(listener, now + 49 * 3600) is None
    state = wc.get_state(conn, tid)
    assert state["episode_first_at"] == now
    assert state["episode_checks"] == 2
    assert kc._wake_parks(conn, now + 49 * 3600) == ([], None)
    assert kc.park_state(conn, tid) == park


@pytest.mark.parametrize("change", ["inferred", "kind", "ref", "why", "blocker", "command", "untimed"])
def test_changed_park_fails_closed(listener, change):
    from hermes_cli import kanban_passive_intake as passive
    conn, tid, _ = listener
    park = kc.park_state(conn, tid)
    assert passive.current(conn, tid, "exit 1", park=park)
    payload = park["payload"]
    if change == "blocker":
        payload["blocker_id"] = "quota"
    elif change == "command":
        payload["wake_check"] = "exit 1 "
    elif change == "untimed":
        park["timed"] = False
    else:
        payload["awaiting"][change] = {"inferred": True, "kind": "ci", "ref": [], "why": ""}[change]
    assert not passive.current(conn, tid, "exit 1", park=park)


def test_repeated_fire_still_escalates(listener, monkeypatch):
    from hermes_cli import kanban_awaiting as kaw
    conn, tid, _ = listener
    monkeypatch.setattr(kaw, "prior_same_premise", lambda *a, **kw: kaw.REPARK_LIMIT)
    monkeypatch.setattr(kaw, "probe_fire_reparks", lambda *a, **kw: 0)
    assert kaw.gate(conn, tid, awaiting=AWAITING, reason="waiting", wake_check="exit 1")
    with pytest.raises(kaw.RepeatedPark):
        kaw.gate(conn, tid, awaiting=AWAITING, reason="waiting", wake_check="exit 1", blocker_id="quota")
    monkeypatch.setattr(kaw, "probe_fire_reparks", lambda *a, **kw: kaw.PROBE_FIRE_REPARK_LIMIT)
    with pytest.raises(kaw.RepeatedPark):
        kaw.gate(conn, tid, awaiting=AWAITING, reason="waiting", wake_check="exit 1")


def test_removal_invalidates_quiet_sweep_memo(listener, monkeypatch):
    from hermes_cli import kanban_capability_registry as registry
    conn, tid, policy = listener
    monkeypatch.setattr(wc, "run_due_wake_checks", lambda *a, **kw: None)
    monkeypatch.setattr(registry, "maybe_refresh_async", lambda: None)
    now = int(time.time()) + 49 * 3600
    assert kc.wake_due_continuations(conn, now=now) == []
    assert kc.wake_due_continuations(conn, now=now) == []
    policy.clear()
    assert kc.wake_due_continuations(conn, now=now) == [tid]


def test_async_policy_snapshot_and_cadence(listener, monkeypatch):
    from contextvars import ContextVar
    from threading import Event
    from hermes_cli import config

    conn, tid, policy = listener
    scope = ContextVar("passive_test_policy", default={})
    token = scope.set(policy)
    monkeypatch.setattr(config, "load_config_readonly",
                        lambda: {"kanban": {"passive_intake": scope.get()}})
    entered, release = Event(), Event()

    def probe(*args):
        entered.set()
        assert release.wait(5)
        return 1, ""

    monkeypatch.setattr(wc, "_run_probe", probe)
    now = int(time.time())
    apply(listener, now)
    later = now + 49 * 3600
    try:
        assert wc.run_due_wake_checks(conn, now=later)["submitted"] == [tid]
        assert entered.wait(5)
        policy.clear()  # Already submitted workers retain an independent scoped snapshot.
        release.set()
        wc._shutdown_for_tests()
        state = wc.get_state(conn, tid)
        assert state is not None and state["last_rc"] == 1
        assert kb.get_task(conn, tid).status == "scheduled"
        assert state["next_at"] == later + state["every"]
        assert wc.run_due_wake_checks(conn, now=later + 1, wait=True)["submitted"] == []
        assert wc.run_due_wake_checks(conn, now=state["next_at"], wait=True)["submitted"] == [tid]
        assert helpers._events(conn, tid, "wake_check_stale")
    finally:
        release.set()
        wc._shutdown_for_tests()
        scope.reset(token)


def test_metadata_blocker_disables_repark_exemption(listener, monkeypatch):
    from hermes_cli import kanban_awaiting as kaw
    from hermes_cli import kanban_timed_park as ktp

    conn, tid, _ = listener
    monkeypatch.setattr(kaw, "prior_same_premise", lambda *a, **kw: kaw.REPARK_LIMIT)
    monkeypatch.setattr(kaw, "probe_fire_reparks", lambda *a, **kw: 0)
    escalated = []
    monkeypatch.setattr(kaw, "escalate", lambda *a, **kw: escalated.append(kw["record"]))
    with kb.write_txn(conn):
        assert not ktp.park(conn, tid, reason="waiting", requested_kind="time",
                            wake_check="exit 1", awaiting=AWAITING,
                            run_metadata={"blocker_id": "quota"})
    assert len(escalated) == 1


def test_old_generation_cannot_mutate_episode(listener):
    conn, tid, _ = listener
    now = int(time.time())
    apply(listener, now)
    state = wc.get_state(conn, tid)
    assert wc.apply_result(conn, tid, "exit 1", 0, "", generation="old", now=now) is None
    assert wc.get_state(conn, tid) == state


def test_removal_reactivates_stale(listener):
    _conn, _tid, policy = listener
    now = int(time.time())
    apply(listener, now)
    assert apply(listener, now + 49 * 3600) is None
    policy.clear()
    assert apply(listener, now + 50 * 3600) == "wake_check_stale"


@pytest.mark.parametrize("rc,out", [(0, ""), (2, "bad"), (1, "Traceback (most recent call last):")])
def test_success_and_errors_remain_active(listener, rc, out):
    for _ in range(3):
        result = apply(listener, int(time.time()), rc, out)
        if result:
            break
    assert result == ("condition_met" if rc == 0 else "wake_check_broken")


@pytest.mark.parametrize("policy", [None, [], True, {"other": "exit 1"}, {"SELF": 1}, {"SELF": "exit 1 "}])
def test_malformed_or_changed_config_fails_closed(listener, monkeypatch, policy):
    from hermes_cli import config
    _conn, tid, _ = listener
    if isinstance(policy, dict) and "SELF" in policy:
        policy = {tid: policy["SELF"]}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"passive_intake": policy}})
    now = int(time.time())
    apply(listener, now)
    assert apply(listener, now + 49 * 3600) == "wake_check_stale"
