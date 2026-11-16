"""Boundary tests for the fail-closed passive-listener policy and custody clock."""
import json
import time
from typing import Any

import pytest

from hermes_cli import config
from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_passive_intake as passive
from hermes_cli import kanban_wake_check as wc
from tests.hermes_cli import test_kanban_passive_intake as helpers

kanban_home = helpers.kanban_home
listener = helpers.listener
TID = "t_abc123"
CMD = "exit 1"
AWAITING = helpers.AWAITING


@pytest.mark.parametrize("cfg", [None, [], True, {"kanban": []}, {"kanban": True},
    {"kanban": {"passive_intake": []}},
    *[{"kanban": {"passive_intake": {TID: CMD, key: value}}}
      for key, value in [(3, CMD), ("t_ABC", CMD), ("t_", CMD), ("t_abc\n", CMD),
                         ("t_def", None), ("t_def", "  ")]]])
def test_snapshot_rejects_whole_malformed_mapping(monkeypatch, cfg):
    monkeypatch.setattr(config, "load_config_readonly", lambda: cfg)
    assert passive.snapshot() == {}


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_snapshot_read_failure(monkeypatch, error):
    def fail():
        raise error("unavailable")
    monkeypatch.setattr(config, "load_config_readonly", fail)
    assert passive.snapshot() == {}


def test_snapshot_copy_preserves_exact_command(monkeypatch):
    value = {TID: CMD + " "}
    monkeypatch.setattr(config, "load_config_readonly", lambda: {"kanban": {"passive_intake": value}})
    result = passive.snapshot()
    assert result == value and result is not value
    assert not passive.eligible(TID, CMD, AWAITING, policy=result)


@pytest.mark.parametrize("change", ["policy", "task", "command", "blank", "nonstring", "blocker",
    "awaiting", "kind", "inferred", "ref_type", "ref_blank", "why_type", "why_blank"])
def test_eligibility_each_clause(change):
    tid, cmd, blocker, policy = TID, CMD, None, {TID: CMD}
    awaiting: Any = dict(AWAITING)
    if change == "policy": policy = []
    if change == "task": tid = "t_def"
    if change == "command": cmd += " "
    if change == "blank": cmd = "  "; policy = {TID: cmd}
    if change == "nonstring": cmd = 1; policy = {TID: cmd}
    if change == "blocker": blocker = ""
    if change == "awaiting": awaiting = []
    if change == "kind": awaiting["kind"] = "ci"
    if change == "inferred": awaiting["inferred"] = True
    if change.endswith("_type"): awaiting[change.split("_")[0]] = 1
    if change.endswith("_blank"): awaiting[change.split("_")[0]] = "  "
    assert not passive.eligible(tid, cmd, awaiting, blocker, policy=policy)


@pytest.mark.parametrize("producer", [None, ("task", "t_def"), ("probe", CMD)])
def test_producer_classification(monkeypatch, producer):
    from hermes_cli import kanban_wait_info
    monkeypatch.setattr(kanban_wait_info, "producer_from_wake_check", lambda cmd: producer)
    assert passive.eligible(TID, CMD, AWAITING, policy={TID: CMD}) is bool(producer and producer[0] == "probe")


@pytest.fixture
def clock(monkeypatch):
    park = {"timed": True, "event_id": 10, "parked_at": 100,
            "payload": {"wake_check": CMD, "awaiting": dict(AWAITING), "check_every": 300}}
    state = {"cmd": CMD, "last_rc": 1, "fails": 0, "last_at": 100}
    monkeypatch.setattr(wc, "own_gateway", lambda conn: "home")
    monkeypatch.setattr(wc, "get_state", lambda conn, tid: state)
    return park, state


@pytest.mark.parametrize("change", ["untimed", "policy", "missing", "command", "rc0", "rc2", "fails", "old", "zero", "none"])
def test_local_custody_each_clause(clock, monkeypatch, change):
    park, state = clock
    policy = {TID: CMD}
    if change == "untimed": park["timed"] = False
    if change == "policy": policy = {}
    if change == "missing": monkeypatch.setattr(wc, "get_state", lambda *a: None)
    if change == "command": state["cmd"] += " "
    if change.startswith("rc"): state["last_rc"] = int(change[-1])
    if change == "fails": state["fails"] = 1
    if change == "old": state["last_at"] = 99
    if change == "zero": state["last_at"] = 0; park["parked_at"] = 0
    if change == "none": state["last_at"] = None
    assert passive.quiet_until(None, TID, park, 100, policy=policy) is None


@pytest.mark.parametrize("host,own", [(None, "home"), ("home", "home"), ("remote", None)])
def test_local_custody_boundary(clock, monkeypatch, host, own):
    park, _state = clock
    park["payload"]["probe_host"] = host
    monkeypatch.setattr(wc, "own_gateway", lambda conn: own)
    deadline = 100 + wc.ALIVE_EVERY + 600 + wc.SILENT_GRACE + 1
    assert passive.quiet_until(None, TID, park, deadline - 1, policy={TID: CMD}) == deadline
    assert passive.quiet_until(None, TID, park, deadline, policy={TID: CMD}) is None


@pytest.mark.parametrize("change", ["valid", "host", "command", "before", "old_event", "kind", "task", "absent"])
def test_remote_heartbeat_identity_and_freshness(clock, listener, change):
    conn, tid, _ = listener
    park, _state = clock
    park["payload"]["probe_host"] = "remote"
    # Real isolated DB query: enforce the task, event-id and kind filters too.
    conn.execute("DELETE FROM task_events")
    note = {"host": "wrong" if change == "host" else "remote",
            "wake_check": CMD + " " if change == "command" else CMD}
    if change != "absent":
        conn.execute("INSERT INTO task_events (id, task_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                     (9 if change == "old_event" else 11, "t_other" if change == "task" else tid,
                      "other" if change == "kind" else wc.ALIVE_EVENT, json.dumps(note),
                      99 if change == "before" else 100))
    deadline = 100 + wc.ALIVE_EVERY + 600 + wc.SILENT_GRACE + 1
    assert passive.quiet_until(conn, tid, park, 100, policy={tid: CMD}) == (deadline if change == "valid" else None)


def test_custody_deadline_invalidates_sweep_memo(listener, monkeypatch):
    from hermes_cli import kanban_capability_registry as registry
    conn, tid, _ = listener
    monkeypatch.setattr(wc, "run_due_wake_checks", lambda *a, **kw: None)
    monkeypatch.setattr(registry, "maybe_refresh_async", lambda: None)
    now = int(time.time()) + 49 * 3600
    helpers.apply(listener, now)
    state = wc.get_state(conn, tid)
    assert state is not None
    deadline = now + wc.ALIVE_EVERY + 2 * state["every"] + wc.SILENT_GRACE + 1
    assert kc.wake_due_continuations(conn, now=now) == []
    assert kc.wake_due_continuations(conn, now=deadline - 1) == []
    assert kc.wake_due_continuations(conn, now=deadline) == [tid]
