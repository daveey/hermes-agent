"""``ready_since`` must equal the diagnostic's ready floor without loading a task's whole history.

Measured on the hub (dispatch.maintenance -> parent_unclaimed.safe_sweep -> ready_since): every pass
materialised ALL task events/runs per parent. This pins the old semantics (the oracle below is the
previous implementation, verbatim) and forbids full-history reads. It does not claim to fix dispatcher
starvation by itself.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_parent_unclaimed as pu


def _oracle(conn, task_id, created_at=None):
    from hermes_cli.kanban_diagnostics import _last_ready_entry_ts
    runs = conn.execute("SELECT * FROM task_runs WHERE task_id = ?", (task_id,)).fetchall()
    since = _last_ready_entry_ts(kb.list_events(conn, task_id), [dict(r) for r in runs])
    return int(since or created_at or 0)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def _ev(c, task, kind, ts, payload=None, run=None):
    c.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
              (task, run, kind, json.dumps(payload) if payload is not None else None, ts))


def _run(c, task, *, started=1, ended=None, status="done", outcome="completed", metadata=None):
    return c.execute(
        "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, metadata) VALUES (?,?,?,?,?,?)",
        (task, status, outcome, started, ended, json.dumps(metadata) if metadata is not None else None),
    ).lastrowid


def _unused_grant(c, task, ts):
    r = _run(c, task, started=ts, ended=ts, status="released", outcome="released",
             metadata={"unused_grant": True})
    _ev(c, task, "claimed", ts, {"holder": "hub"}, r)
    _ev(c, task, "released", ts, {"unused_grant": True}, r)
    return r


def _noise(c, task, n):
    c.executemany("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  [(task, None, "heartbeat", '{"x":1}', 5 + i % 7) for i in range(n)])
    c.executemany("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                  [(f"other{i % 50}", None, "claimed", None, 99999) for i in range(n)])


def _scenarios():
    def empty(c, t): pass
    def created_only(c, t): _ev(c, t, "created", 100)
    def latest_kind(c, t): _ev(c, t, "created", 100); _ev(c, t, "promoted", 500); _ev(c, t, "claimed", 300)
    def run_end_wins(c, t): _ev(c, t, "created", 100); _run(c, t, ended=900)
    def open_run_ignored(c, t): _ev(c, t, "created", 100); _run(c, t, ended=None, status="running", outcome=None)
    def continued_ready(c, t): _ev(c, t, "created", 100); _ev(c, t, "continued", 700, {"status": "ready"})
    def continued_other(c, t): _ev(c, t, "created", 100); _ev(c, t, "continued", 700, {"status": "blocked"})
    def continued_badjson(c, t):
        c.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                  (t, "continued", "{not json", 800))
    def unused_excluded(c, t): _ev(c, t, "created", 100); _unused_grant(c, t, 1000)
    def unused_plus_real(c, t):
        _ev(c, t, "created", 100); _unused_grant(c, t, 1000)
        r = _run(c, t, ended=400); _ev(c, t, "spawned", 350, None, r)
    def unused_with_evidence(c, t):
        r = _unused_grant(c, t, 1000); _ev(c, t, "heartbeat", 1001, None, r)
    def unused_continued_on_run(c, t):
        r = _unused_grant(c, t, 1000); _ev(c, t, "continued", 1002, {"status": "ready"}, r)
    def iso_run_end(c, t):
        c.execute("INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at) VALUES (?,?,?,?,?)",
                  (t, "done", "completed", 1, "2026-05-10T15:00:00Z"))
    def released_but_used(c, t):
        r = _run(c, t, ended=600, status="released", outcome="released", metadata={"unused_grant": True, "x": 1})
        _ev(c, t, "claimed", 590, {"holder": "hub"}, r)
    return [v for k, v in list(locals().items()) if callable(v)]


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda f: f.__name__)
@pytest.mark.parametrize("created_at", [None, 42])
def test_matches_previous_semantics_with_large_irrelevant_history(conn, scenario, created_at):
    task = "t_target"
    scenario(conn, task)
    _noise(conn, task, 400)
    conn.commit()
    assert pu.ready_since(conn, task, created_at) == _oracle(conn, task, created_at)


@pytest.mark.parametrize("kind", ["continued", "shared_blocker_cleared"])
@pytest.mark.parametrize("status", [None, "ready", "review", "blocked"])
def test_conditional_registry_matches_oracle_without_full_history(conn, kind, status):
    task = "t_conditional"
    _ev(conn, task, "created", 100)
    _ev(conn, task, kind, 700, {"status": status})
    _noise(conn, task, 40)
    conn.commit()
    assert pu.ready_since(conn, task) == _oracle(conn, task)
    assert pu.ready_since(conn, task) == (700 if status == "ready" else 100)


def test_does_not_materialise_full_history(conn, monkeypatch):
    task = "t_target"
    _ev(conn, task, "created", 100)
    _unused_grant(conn, task, 1000)
    _run(conn, task, ended=400)
    _noise(conn, task, 3000)
    conn.commit()
    expected = _oracle(conn, task)

    def boom(*a, **k):
        raise AssertionError("full event history loaded")
    monkeypatch.setattr(kb, "list_events", boom)
    seen = []
    conn.set_trace_callback(seen.append)
    assert pu.ready_since(conn, task) == expected
    conn.set_trace_callback(None)
    # no unfiltered whole-history reads of either table
    for sql in seen:
        low = " ".join(sql.lower().split())
        assert "select * from task_runs" not in low or "outcome" in low, sql
        assert "from task_events where task_id" not in low or "kind in" in low or "run_id" in low, sql
