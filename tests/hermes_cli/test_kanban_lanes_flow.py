"""Lanes flow JSON: step/reason classification, daily caps, snapshot shape. [daveey]"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_lanes as lanes
from hermes_cli import kanban_lanes_flow as flow
from tests.hermes_cli import test_kanban_lanes as base
from tests.hermes_cli.test_kanban_lanes_plan import cli

conn = base.conn
configured = base.configured
person = base.person


def _card(**fields):
    card = {"status": "ready", "block_kind": None, "human_request": None, "parents_open": False,
            "events": [], "awaiting": None, "pr": None, "created_by": "user", "session_id": None,
            "project_id": None, "gate_reason": None}
    card.update(fields)
    return card


@pytest.mark.parametrize("status,extra,step", [
    ("triage", {}, "intake"),
    ("blocked", {}, "waiting"),
    ("scheduled", {}, "waiting"),
    ("todo", {"parents_open": True}, "waiting"),
    ("ready", {}, "ready"),
    ("running", {}, "running"),
    ("review", {}, "review"),
    ("done", {}, "done"),
    ("ready", {"pr": {"checks": "pending"}}, "ci"),
    ("todo", {"pr": {"approved": True, "checks": "green", "merged": False}}, "merge"),
    ("todo", {"pr": {"merged": True, "deployed": False}}, "deploy"),
    ("todo", {"pr": {"merged": True, "deployed": True, "verified": False}}, "test"),
    ("todo", {"pr": {"url": "https://github.com/o/r/pull/1"}}, "landing_unclassified"),
    ("mystery", {}, "landing_unclassified"),
])
def test_each_step_and_unclassified(status, extra, step):
    assert flow.classify_step(_card(status=status, **extra)) == step


def test_review_requested_event_is_review_without_guessing_ci():
    assert flow.classify_step(_card(status="todo", events=[{"kind": "review_requested"}])) == "review"


@pytest.mark.parametrize("fields,reason", [
    ({"human_request": {"action": "pick", "why_human": "Only you know", "interaction": "reply", "scope": "This card", "evidence": "Choice needed"}, "status": "blocked", "block_kind": "needs_input"}, "you"),
    ({"parents_open": True, "status": "todo"}, "dep"),
    ({"status": "triage", "block_kind": lanes.GATE_KIND, "held": True}, "gate"),
    ({"status": "triage", "block_kind": lanes.GATE_KIND, "held": True,
      "gate_reason": "limit:cards_per_day"}, "limit:cards_per_day"),
    ({"status": "triage", "block_kind": lanes.GATE_KIND, "held": True,
      "gate_reason": "limit:usd_per_day"}, "limit:usd_per_day"),
    ({"status": "scheduled"}, "time"),
    ({"status": "ready", "events": [{"kind": "dispatch_skipped", "payload": {"reason": "llm_paced"}}]}, "quota"),
    ({"status": "ready"}, "slot"),
    ({"status": "triage"}, "triage"),
    ({"status": "running"}, "run"),
    ({"status": "review"}, "review"),
])
def test_each_reason(fields, reason):
    assert flow.classify_reason(_card(**fields), flow.classify_step(_card(**fields))) == reason


def test_source_kinds():
    assert flow.classify_source(_card(created_by="user"))["kind"] == "user"
    assert flow.classify_source(_card(created_by="self-heal"))["kind"] == "automation"
    assert flow.classify_source(_card(created_by="self-heal"))["id"] == "self-heal"
    assert flow.classify_source(_card(session_id="sess-1", created_by="user"))["kind"] == "session"
    assert flow.classify_source(_card(session_id="sess-1"))["id"] == "sess-1"
    assert flow.classify_source(_card(project_id="Baby Bear"))["project"] == "Baby Bear"


def _babybear_limits(configured, **lane):
    mapping = json.loads(json.dumps(base.LANES))
    mapping["babybear"].update(lane)
    configured(mapping)


def test_cards_per_day_under_over_unset(conn, configured):
    _babybear_limits(configured, cards_per_day=2)
    a = base._ready_in(conn, "babybear", "a")
    b = base._ready_in(conn, "babybear", "b")
    c = base._ready_in(conn, "babybear", "c")
    assert base._task(conn, a)["status"] == "ready"
    assert base._task(conn, b)["status"] == "ready"
    row = base._task(conn, c)
    assert (row["status"], row["block_kind"]) == ("triage", lanes.GATE_KIND)
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                           (c, lanes.GATED)).fetchone()[0]
    assert json.loads(payload)["reason"] == "limit:cards_per_day"
    configured(base.LANES)  # unset
    d = base._ready_in(conn, "babybear", "d")
    assert base._task(conn, d)["status"] == "ready"


def test_usd_per_day_under_over_unset(conn, configured):
    _babybear_limits(configured, usd_per_day=10)
    cheap = base._ready_in(conn, "babybear", "cheap")
    now = int(time.time())
    conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at) VALUES (?, 'done', ?, ?)",
                 (cheap, now - 10, now - 1))
    run_id = conn.execute("SELECT MAX(id) FROM task_runs").fetchone()[0]
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                 (cheap, run_id, "worker_result", json.dumps({"usage": {"cost_usd": 10.0}}), now - 1))
    conn.commit()
    over = base._ready_in(conn, "babybear", "over")
    row = base._task(conn, over)
    assert (row["status"], row["block_kind"]) == ("triage", lanes.GATE_KIND)
    payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                           (over, lanes.GATED)).fetchone()[0]
    assert json.loads(payload)["reason"] == "limit:usd_per_day"
    configured(base.LANES)
    free = base._ready_in(conn, "babybear", "free")
    assert base._task(conn, free)["status"] == "ready"


def test_daily_limits_reset_at_local_midnight(conn, configured, monkeypatch):
    start = lanes.day_start()
    clock = [start + 22 * 3600]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    _babybear_limits(configured, cards_per_day=1)
    first = base._ready_in(conn, "babybear", "before-midnight")
    blocked = base._ready_in(conn, "babybear", "still-today")
    assert base._task(conn, first)["status"] == "ready"
    assert base._task(conn, blocked)["status"] == "triage"
    clock[0] = start + 24 * 3600 + 60
    nxt = base._ready_in(conn, "babybear", "after-midnight")
    assert base._task(conn, nxt)["status"] == "ready"


def test_flow_json_shape_and_window(conn, configured, capsys):
    configured()
    tid = base._ready_in(conn, "babybear", "shape card")
    assert cli("lanes", "flow", "--json", "--window", "6h") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["window"] == "6h"
    assert set(data) >= {"window", "cards", "lanes"}
    card = next(c for c in data["cards"] if c["id"] == tid)
    assert set(card) == {"id", "title", "lane", "step", "reason", "source", "age_h", "pr"}
    assert card["lane"] == "babybear" and card["step"] == "ready" and card["reason"] == "slot"
    assert card["pr"] is None and isinstance(card["age_h"], (int, float))
    assert set(card["source"]) == {"kind", "id", "project"}
    assert card["source"]["kind"] in ("session", "automation", "user")
    lane = data["lanes"]["babybear"]
    assert set(lane) >= {"done", "run_hours", "cost_usd", "limits", "usage"}
    assert set(lane["limits"]) == {"cards_per_day", "usd_per_day", "max_workers"}
    assert set(lane["usage"]) == {"cards_today", "usd_today"}
    assert lane["limits"]["cards_per_day"] is None and lane["limits"]["usd_per_day"] is None


def test_set_persists_daily_limits(conn, configured):
    configured()
    from hermes_cli import kanban_lanes_cli
    kanban_lanes_cli.set_lane("babybear", cards_per_day=7, usd_per_day=12.5)
    lane = lanes.policy().lanes["babybear"]
    assert (lane.cards_per_day, lane.usd_per_day) == (7, 12.5)


def test_unknown_window_is_rejected():
    with pytest.raises(SystemExit):
        cli("lanes", "flow", "--json", "--window", "2h")


def _legacy_spent(conn, pol, lane, now):
    """The per-lane scan spent_today used before one aggregate served every lane."""
    total = 0.0
    for tenant, cost in conn.execute(
            "SELECT tenant, SUM(cost) FROM (SELECT t.tenant AS tenant, "
            "MAX(COALESCE(json_extract(e.payload, '$.usage.cost_usd'), 0)) AS cost FROM task_events e "
            "JOIN tasks t ON t.id = e.task_id WHERE e.kind = 'worker_result' AND e.created_at >= ? "
            "AND json_extract(e.payload, '$.usage') IS NOT NULL GROUP BY e.task_id, e.run_id) GROUP BY tenant",
            (lanes.day_start(now),)):
        if pol.lane_of(tenant) == lane:
            total += float(cost or 0)
    return round(total, 2)


def test_spend_comes_from_one_scan_and_equals_the_per_lane_scan(conn, configured):
    configured()
    now = int(time.time())
    pol = lanes.policy()
    for lane, cost in (("babybear", 1.25), ("babybear", 2.5), ("other", 0.126), ("stranger", 7.0)):
        tid = base._ready_in(conn, lane, f"spend {lane} {cost}")
        conn.execute("INSERT INTO task_runs (task_id, status, started_at, ended_at) VALUES (?, 'done', ?, ?)",
                     (tid, now - 10, now - 1))
        run_id = conn.execute("SELECT MAX(id) FROM task_runs").fetchone()[0]
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                     (tid, run_id, "worker_result", json.dumps({"usage": {"cost_usd": cost}}), now - 1))
    conn.commit()
    scans = []
    conn.set_trace_callback(lambda sql: scans.append(sql) if "worker_result" in sql else None)
    spent = lanes.spent_by_lane(conn, pol, now=now)
    assert len(scans) == 1
    assert set(spent) >= set(pol.lanes)
    for name in pol.lanes:
        assert spent[name] == _legacy_spent(conn, pol, name, now) == lanes.spent_today(conn, pol, name, now=now)
    assert spent["babybear"] == 3.75 and spent[pol.lane_of("stranger")] >= 7.0
    scans.clear()
    snap = flow.snapshot(conn, window="6h", now=now, pol=pol, reader=type("R", (), {
        "prefetch": lambda self, urls: None, "get": lambda self, url: {}})())
    assert len([s for s in scans if "GROUP BY e.task_id, e.run_id" in s and "created_at >= " in s]) == 1
    assert snap["lanes"]["babybear"]["usage"]["usd_today"] == 3.75


def test_evidence_projection_equals_plain_projection_with_pr_urls(conn, configured, monkeypatch):
    configured()
    now = int(time.time())
    pol = lanes.policy()
    published = base._ready_in(conn, "babybear", "published")
    waiting = base._ready_in(conn, "babybear", "waiting on ci")
    plain = base._ready_in(conn, "babybear", "plain")
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'review_requested', ?, ?)",
                 (published, json.dumps({"metadata": {"pr": "https://github.com/example/repo/pull/7"}}), now))
    conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (waiting,))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'scheduled', ?, ?)",
                 (waiting, json.dumps({"awaiting": {"kind": "ci", "ref": "https://github.com/example/repo/pull/8"}}), now))
    conn.commit()
    reader = type("R", (), {"prefetch": lambda self, urls: None, "get": lambda self, url: {}})()
    from hermes_cli.kanban_lanes_flow_evidence import Evidence
    calls = []
    original = flow._pr_url

    def counted(task, events):
        calls.append(task["id"])
        return original(task, events)

    monkeypatch.setattr(flow, "_pr_url", counted)
    with_evidence = flow.snapshot(conn, window="6h", now=now, pol=pol, reader=reader, evidence=Evidence(conn))
    assert sorted(calls) == sorted([published, waiting, plain])
    plain_cards = [flow._card_from_row(conn, row, now, reader, pol) for row in
                   conn.execute(f"SELECT * FROM tasks WHERE status IN {flow._OPEN} ORDER BY created_at, id")]
    by_id = {c["id"]: c["pr"] for c in with_evidence["cards"]}
    assert by_id[published] == "https://github.com/example/repo/pull/7"
    assert by_id[waiting] == "https://github.com/example/repo/pull/8"
    assert by_id == {c["id"]: (c["pr"] or {}).get("url") for c in plain_cards}
