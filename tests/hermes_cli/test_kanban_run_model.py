"""Which model a listed card's newest run uses, for the Desktop running row (``kanban_run_model``). [daveey]"""

import json
import sqlite3

from hermes_cli.kanban_run_model import add_run_models


def _conn(events, runs=()):
    """A board with ``task_events`` and ``task_runs``; every event's run plus ``runs`` (task, run id) exist."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, "
                 "payload TEXT)")
    conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT)")
    conn.executemany("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?, ?, ?, ?)",
                     [(t, r, k, json.dumps(p) if isinstance(p, dict) else p) for t, r, k, p in events])
    pairs = {(r, t) for t, r, _, _ in events if r is not None} | {(r, t) for t, r in runs}
    conn.executemany("INSERT INTO task_runs (id, task_id) VALUES (?, ?)", sorted(pairs))
    return conn


def _stamp(events, ids=("t1",), runs=()):
    found = {i: {"id": i} for i in ids}
    add_run_models(_conn(events, runs), found)
    return {i: c.get("run_model") for i, c in found.items()}


def test_route_class_and_pin():
    assert _stamp([("t1", 1, "run_route", {"model": "class:deep"})]) == {"t1": "class:deep"}
    assert _stamp([("t1", 1, "run_route", {"model": "claude-opus-5-5"})]) == {"t1": "claude-opus-5-5"}


def test_ended_run_shows_the_models_stamp_writes_into_its_terminal_event():
    # Production shape: kanban_run_models.stamp puts session_models on the terminal event; no attestation event.
    for kind in ("completed", "worker_result", "scheduled", "continued", "blocked"):
        events = [("t1", 1, "run_route", {"model": "class:deep"}),
                  ("t1", 1, kind, {"summary": "x", "session_models": ["claude-opus-5-5"]})]
        assert _stamp(events) == {"t1": "claude-opus-5-5"}, kind


def test_backfilled_attestation_still_counts():
    events = [("t1", 1, "run_route", {"model": "class:standard"}),
              ("t1", 1, "run_models_attested", {"session_models": ["gpt-6-astra", "gpt-6-astra", "grok-4.6"]})]
    assert _stamp(events) == {"t1": "gpt-6-astra, grok-4.6"}


def test_newest_run_wins_and_concrete_beats_a_later_route():
    events = [
        ("t1", 1, "run_route", {"model": "class:standard"}),
        ("t1", 1, "completed", {"session_models": ["gpt-6-astra"]}),
        ("t1", 2, "run_route", {"model": "class:deep"}),
    ]
    assert _stamp(events) == {"t1": "class:deep"}  # run 2 is live: its route, not run 1's models
    events.append(("t1", 2, "worker_result", {"session_models": ["claude-opus-5-5"]}))
    events.append(("t1", 2, "run_route", {"model": "class:standard"}))
    assert _stamp(events) == {"t1": "claude-opus-5-5"}


def test_reroute_within_a_run_takes_the_newest_route():
    assert _stamp([("t1", 3, "run_route", {"model": "claude-fable-5-2"}),
                   ("t1", 3, "run_route", {"model": "gpt-6.1-sol"})]) == {"t1": "gpt-6.1-sol"}


def test_newest_run_with_unknown_route_stamps_nothing_not_an_older_runs_model():
    older = [("t1", 1, "run_route", {"model": "class:standard"}),
             ("t1", 1, "completed", {"session_models": ["gpt-6-astra"]})]
    # record_run_route writes model None when the route is unknown.
    assert _stamp([*older, ("t1", 2, "run_route", {"model": None, "source": "unknown"})]) == {"t1": None}
    # Claimed, no route event yet: run 2 exists only in task_runs.
    assert _stamp(older, runs=[("t1", 2)]) == {"t1": None}
    assert _stamp([("t1", 1, "run_route", {"model": "class:deep"})], runs=[("t1", 2)]) == {"t1": None}


def test_nothing_usable_stamps_nothing():
    assert _stamp([("t1", 1, "run_route", "{bad"), ("t1", 1, "run_route", {"model": ""}),
                   ("t1", 1, "completed", {"session_models": "not-a-list"}),
                   ("t1", None, "run_route", {"model": "x"}), ("t2", 2, "run_route", {"model": "y"})],
                  ids=("t1",)) == {"t1": None}


def test_cards_are_independent():
    events = [("t1", 1, "run_route", {"model": "class:deep"}), ("t2", 2, "run_route", {"model": "class:standard"}),
              ("t2", 2, "completed", {"session_models": ["grok-4.7"]})]
    assert _stamp(events, ids=("t1", "t2")) == {"t1": "class:deep", "t2": "grok-4.7"}


def test_old_board_without_events_table():
    conn = sqlite3.connect(":memory:")
    found = {"t1": {"id": "t1"}}
    add_run_models(conn, found)
    assert "run_model" not in found["t1"]
