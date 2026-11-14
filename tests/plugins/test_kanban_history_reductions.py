"""Retained event reductions must not retain live diagnostic decisions."""
import sqlite3

import pytest

from hermes_cli import kanban_diagnostics as kd
from plugins.kanban.dashboard import board_read as br


def history(events):
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE task_events (id INTEGER, task_id TEXT, kind TEXT, payload TEXT, created_at)")
        conn.executemany("INSERT INTO task_events VALUES (?, 't', ?, ?, ?)",
                         [(i, *event) for i, event in enumerate(events)])
        return br._rows_by_task(conn, "task_events", ["t"])["t"]


def test_unchanged_projection_does_not_rescan_historical_events(monkeypatch):
    events = history([("heartbeat", "{}", 100)] * 10000)
    inputs = (({"id": "t", "status": "done"},), {"t": events}, {"t": ()}, {}, {})
    br.diagnostics_from_inputs(inputs)
    calls = []
    real = kd._event_ts
    def spy(ev):
        calls.append(ev)
        return real(ev)
    monkeypatch.setattr(kd, "_event_ts", spy)
    kinds = []
    real_kind = kd._event_kind
    def kind_spy(ev):
        kinds.append(ev)
        return real_kind(ev)
    monkeypatch.setattr(kd, "_event_kind", kind_spy)
    br.diagnostics_from_inputs(inputs)
    assert calls == [], "unchanged event history must reuse pure reductions"
    assert kinds == [], "unchanged history must reuse both active hallucination subsets"


@pytest.mark.parametrize("status", ["done", "running", "ready", "blocked", "triage", "todo", "scheduled"])
def test_reductions_preserve_clock_config_replica_and_arrival_order(monkeypatch, status):
    from hermes_cli import kanban_db as kb
    unresolved = ["t_phantom"]
    monkeypatch.setattr(kb, "unresolved_after_replica", lambda ids: [v for v in ids if v in unresolved])
    events = history([
        ("completion_blocked_hallucination", '{"phantom_cards":["old"]}', 900),
        ("edited", "{}", 1),  # reset is arrival-ordered, not timestamp-ordered
        ("completion_blocked_hallucination", '{"phantom_cards":["new","new"]}', 200),
        ("suspected_hallucinated_references", '{"phantom_refs":["t_phantom"]}', 201),
        ("unblocked", "{}", 220), ("blocked", "{}", 220),
        ("heartbeat", "{}", 1),
        ("unblocked", "{}", 210), ("blocked", "{}", 210),
        ("unblocked", "{}", 230), ("blocked", "{}", 230),
    ])
    task = {"id": "t", "status": status, "created_at": 1}
    for now in (231, 400, 220, 231):  # forward and backward time, no DB write
        for window in (30, 10, 300):
            config = {"block_cycle_window_seconds": window, "block_cycle_threshold": 1}
            expected = kd.compute_task_diagnostics(task, list(events), [], now=now, config=config)
            assert kd.compute_task_diagnostics(task, events, [], now=now, config=config) == expected
        unresolved.clear()  # live replica resolution must not be retained


def test_ready_hold_reuses_history_but_not_deadline(monkeypatch):
    events = history([('heartbeat', '{}', 1)] * 10000 + [
        ('dispatch_skipped', '{"reason":"pinned_quota_wall","until":500}', 100)])
    task = {'id': 't', 'status': 'ready'}
    assert kd._ready_hold_reason(task, events, {}, 499) == 'pinned_quota_wall'
    real = kd._event_ts
    calls = []
    def spy(ev):
        calls.append(ev)
        return real(ev)
    monkeypatch.setattr(kd, '_event_ts', spy)
    assert kd._ready_hold_reason(task, events, {}, 500) is None
    assert kd._ready_hold_reason(task, events, {}, 499) == 'pinned_quota_wall'
    assert not calls


def test_scheduled_projection_reuses_latest_parking_event(monkeypatch):
    from hermes_cli import kanban_continuation as kc
    events = history([("heartbeat", "{}", 100)] * 10000 + [
        (next(iter(kc.SCHEDULED_ENTRY_EVENTS)), '{"wake_at": 100}', 101)])
    task = {"id": "t", "status": "scheduled", "created_at": 1}
    original = kd.compute_task_diagnostics(task, list(events), [], now=1000)
    assert kd.compute_task_diagnostics(task, events, [], now=1000) == original
    real = kd._event_kind
    calls = []
    def spy(ev):
        calls.append(ev)
        return real(ev)
    monkeypatch.setattr(kd, "_event_kind", spy)
    kd._latest_parking_event(events)
    assert calls == [], "parking rules must not rescan the full history"
    assert kd.compute_task_diagnostics(task, events, [], now=1000) == original


def test_projection_rechecks_live_inputs_and_isolates_returned_values(monkeypatch):
    from hermes_cli import config as hc, kanban_db as kb
    clock = [110]
    settings = {"block_cycle_threshold": 1, "block_cycle_window_seconds": 30}
    config = {"kanban": {"diagnostics": settings}}
    replica = ["t_phantom"]
    monkeypatch.setattr(hc, "load_config", lambda: config)
    monkeypatch.setattr(kd.time, "time", lambda: clock[0])
    monkeypatch.setattr(kb, "unresolved_after_replica", lambda ids: list(replica))
    events = history([("unblocked", "{}", 100), ("blocked", "{}", 101),
                      ("suspected_hallucinated_references", '{"phantom_refs":["t_phantom"]}', 102)])
    inputs = (({"id": "t", "status": "done"},), {"t": events}, {"t": ()}, {}, {})
    original = br.diagnostics_from_inputs(inputs)
    assert {d["kind"] for d in original["t"]} == {"prose_phantom_refs", "block_unblock_cycling"}
    original["t"][0]["data"].clear()
    assert all(d["data"] for d in br.diagnostics_from_inputs(inputs)["t"])
    settings["block_cycle_threshold"] = 2
    assert [d["kind"] for d in br.diagnostics_from_inputs(inputs)["t"]] == ["prose_phantom_refs"]
    settings["block_cycle_threshold"] = 1
    clock[0] = 200
    assert [d["kind"] for d in br.diagnostics_from_inputs(inputs)["t"]] == ["prose_phantom_refs"]
    replica.clear()
    assert br.diagnostics_from_inputs(inputs) == {}
    clock[0] = 110
    assert [d["kind"] for d in br.diagnostics_from_inputs(inputs)["t"]] == ["block_unblock_cycling"]
    with pytest.raises(AttributeError):
        events.cycle_inputs = ()
    with pytest.raises(AttributeError):
        del events.active_refs


@pytest.mark.parametrize("bad", ["invalid-time", None, 0])
def test_bad_timestamp_keeps_per_rule_exception_isolation(bad):
    events = history([("heartbeat", "{}", bad), ("unblocked", "{}", 100), ("blocked", "{}", 101),
                      ("completion_blocked_hallucination", '{"phantom_cards":["x"]}', 102)])
    task = {"id": "t", "status": "done"}
    for _ in range(2):
        expected = kd.compute_task_diagnostics(task, list(events), [], now=110, config={"block_cycle_threshold": 1})
        actual = kd.compute_task_diagnostics(task, events, [], now=110, config={"block_cycle_threshold": 1})
        assert actual == expected
        assert any(d.kind == "hallucinated_cards" for d in actual)
