"""Which model a listed card's newest run uses, for the Desktop running row (``kanban_run_model``). [daveey]"""

import json
import sqlite3

from hermes_cli.kanban_run_model import add_run_models


def _conn(events):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, run_id INTEGER, kind TEXT, "
                 "payload TEXT)")
    conn.executemany("INSERT INTO task_events (task_id, run_id, kind, payload) VALUES (?, ?, ?, ?)",
                     [(t, r, k, json.dumps(p) if isinstance(p, dict) else p) for t, r, k, p in events])
    return conn


def _stamp(events, ids=("t1",)):
    found = {i: {"id": i} for i in ids}
    add_run_models(_conn(events), found)
    return {i: c.get("run_model") for i, c in found.items()}


def test_route_class_and_pin():
    assert _stamp([("t1", 1, "run_route", {"model": "class:deep"})]) == {"t1": "class:deep"}
    assert _stamp([("t1", 1, "run_route", {"model": "claude-opus-5-5"})]) == {"t1": "claude-opus-5-5"}


def test_newest_run_wins_and_attestation_beats_route():
    events = [
        ("t1", 1, "run_route", {"model": "class:standard"}),
        ("t1", 1, "run_models_attested", {"session_models": ["gpt-6-astra"]}),
        ("t1", 2, "run_route", {"model": "class:deep"}),
    ]
    assert _stamp(events) == {"t1": "class:deep"}  # run 2 is live: its route, not run 1's attestation
    events.append(("t1", 2, "run_models_attested", {"session_models": ["claude-opus-5-5", "claude-opus-5-5"]}))
    events.append(("t1", 2, "run_route", {"model": "class:standard"}))  # a later route never beats attestation
    assert _stamp(events) == {"t1": "claude-opus-5-5"}


def test_reroute_within_a_run_takes_the_newest_route():
    assert _stamp([("t1", 3, "run_route", {"model": "claude-fable-5-2"}),
                   ("t1", 3, "run_route", {"model": "gpt-6.1-sol"})]) == {"t1": "gpt-6.1-sol"}


def test_nothing_usable_stamps_nothing():
    assert _stamp([("t1", 1, "run_route", "{bad"), ("t1", 1, "run_route", {"model": ""}),
                   ("t1", None, "run_route", {"model": "x"}), ("t2", 1, "run_route", {"model": "y"})],
                  ids=("t1",)) == {"t1": None}


def test_old_board_without_events_table():
    conn = sqlite3.connect(":memory:")
    found = {"t1": {"id": "t1"}}
    add_run_models(conn, found)
    assert "run_model" not in found["t1"]
