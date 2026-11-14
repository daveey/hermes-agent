"""kanban_show's compact worker payload: diagnostics out, repeated bookkeeping events collapsed. [daveey]

The first tool result of every worker run is this read; the full shape stays one ``verbose=true`` away
and is unchanged for interactive and orchestrator callers."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.tools import test_kanban_tools as helpers
from tools import kanban_show
from tools import kanban_tools as kt

worker_env = helpers.worker_env
BODY = "Fix the widget so it stops dropping events.\n\nDone when the widget works and has a regression test."


@pytest.fixture
def card(worker_env):
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (BODY, worker_env))
        run_id = kb._current_run_id(conn, worker_env)
        with kb.write_txn(conn):
            for _ in range(6):
                kb._append_event(conn, worker_env, "heartbeat", {"note": None}, run_id=run_id)
            kb._append_event(conn, worker_env, "commented", {"author": "x", "_internal": {"id": "i"}}, run_id=run_id)
            for note in ("a", "b"):
                kb._append_event(conn, worker_env, "heartbeat", {"note": note}, run_id=run_id)
    return worker_env


def _kinds(out):
    return [e["kind"] for e in out["events"]]


def test_worker_default_is_compact(card):
    full = json.loads(kt._handle_show({"verbose": True}))
    out = json.loads(kt._handle_show({}))
    assert {"capacity_evidence", "execution_observations"} <= full.keys()
    assert not {"capacity_evidence", "execution_observations"} & out.keys()
    assert out["capacity_note"] == full["capacity_note"]
    assert "verbose=true" in out["compact"] and "compact" not in full
    # Heartbeat runs collapse to their newest row; the commented event between them survives.
    assert _kinds(full).count("heartbeat") == 8
    assert _kinds(out)[-3:] == ["heartbeat", "commented", "heartbeat"]
    first, last = out["events"][-3], out["events"][-1]
    assert first["repeated"] == 6 and last["repeated"] == 2 and last["payload"] == {"note": "b"}
    assert last["first_at"] <= last["created_at"]
    # Internal stamps never reach the worker; the full payload keeps them.
    assert any(k.startswith("_") for e in full["events"] for k in (e["payload"] or {}))
    assert not any(k.startswith("_") for e in out["events"] for k in (e["payload"] or {}))
    # The body is read once, from worker_context.
    assert full["task"]["body"] == BODY and BODY in out["worker_context"]
    assert out["task"]["body"] == "(in worker_context)"
    assert len(json.dumps(out)) < len(json.dumps(full))


@pytest.mark.parametrize("verbose", [True, "true", 1])
def test_verbose_restores_the_full_payload(card, verbose):
    out = json.loads(kt._handle_show({"verbose": verbose}))
    assert {"capacity_evidence", "execution_observations"} <= out.keys() and "compact" not in out
    assert out["task"]["body"] == BODY and _kinds(out).count("heartbeat") == 8


def test_non_worker_callers_keep_the_full_payload(card, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    out = json.loads(kt._handle_show({"task_id": card}))
    assert {"capacity_evidence", "execution_observations"} <= out.keys() and "compact" not in out
    assert out["task"]["body"] == BODY and _kinds(out).count("heartbeat") == 8


def test_a_body_worker_context_truncates_stays_in_task(card):
    long_body = "x" * (20 * 1024)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (long_body, card))
    out = json.loads(kt._handle_show({}))
    assert long_body not in out["worker_context"]
    assert out["task"]["body"] == long_body


def test_texts_worker_context_carries_are_pointed_and_others_kept(card):
    long_comment = "Please also cover the gizmo path; the widget fix alone leaves gizmo users broken."
    huge_comment = "y" * (3 * 1024)  # over the 2 KB per-comment context cap: rendered truncated
    with kbc.connect() as conn:
        kb.add_comment(conn, card, "operator", long_comment)
        kb.add_comment(conn, card, "operator", huge_comment)
        kb.add_comment(conn, card, "operator", "short")
    full = json.loads(kt._handle_show({"verbose": True}))
    out = json.loads(kt._handle_show({}))
    bodies = [c["body"] for c in out["comments"]]
    assert bodies == ["(in worker_context)", huge_comment, "short"]
    assert [c["body"] for c in full["comments"]] == [long_comment, huge_comment, "short"]
    assert [(c["author"], c["created_at"]) for c in out["comments"]] == [
        (c["author"], c["created_at"]) for c in full["comments"]]


def test_run_texts_point_only_when_rendered_whole():
    meta = {"pr": "https://github.com/o/r/pull/1", "tests_run": 12, "notes": "z" * 40}
    summary = "Implemented the widget fix and added regression tests for the gizmo path."
    context = f"### Attempt 1\n{summary}\n_metadata_: `{json.dumps(meta, sort_keys=True)}`\n"
    other = {"id": 2, "summary": "s" * 80, "error": None, "metadata": {"k": "v" * 80}}
    out = kanban_show.compact({"worker_context": context, "task": {"body": None}, "comments": [], "events": [],
                               "runs": [{"id": 1, "summary": summary, "error": None, "metadata": meta}, other]})
    assert out["runs"][0] == {"id": 1, "summary": "(in worker_context)", "error": None,
                              "metadata": "(in worker_context)"}
    assert out["runs"][1] == other  # not in the context: kept whole


def test_collapse_keeps_distinct_runs_kinds_and_rows():
    rows = [
        {"kind": "heartbeat", "payload": None, "created_at": 1, "run_id": 1},
        {"kind": "heartbeat", "payload": None, "created_at": 2, "run_id": 2},  # another run: kept
        {"kind": "claimed", "payload": {"a": 1, "_x": 2}, "created_at": 3, "run_id": 2},
        {"kind": "claimed", "payload": {"a": 2}, "created_at": 4, "run_id": 2},  # not a repeated kind: kept
        {"kind": "respawn_guarded", "payload": {"reason": "r"}, "created_at": 5, "run_id": None},
        {"kind": "respawn_guarded", "payload": {"reason": "r"}, "created_at": 6, "run_id": None},
        {"kind": "respawn_guarded", "payload": {"reason": "r"}, "created_at": 7, "run_id": None},
    ]
    out = kanban_show.collapse_events(rows)
    assert [(e["kind"], e.get("repeated")) for e in out] == [
        ("heartbeat", None), ("heartbeat", None), ("claimed", None), ("claimed", None), ("respawn_guarded", 3)]
    assert out[2]["payload"] == {"a": 1} and out[-1]["first_at"] == 5 and out[-1]["created_at"] == 7
    assert rows[2]["payload"] == {"a": 1, "_x": 2}  # input rows are not mutated
