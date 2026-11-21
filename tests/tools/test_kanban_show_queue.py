"""Every card read a chat uses says where a ``ready`` card waits, so a queued card never reads as stuck. [daveey]

Defect kanban-7d720d4ab8 (2026-11-02): rollout card t_01a19400313cfbda sat ready 37 min while the hub was at its
7-worker cap with higher-priority cards ahead. The chat read ``kanban_show`` (only ``queue_reason``, null for a card
waiting on a hub slot), called it "still queued despite having no remaining dependencies", and ran the card's work
in a background subagent outside the dispatcher. b1f608189c0 had added the queue position to the dashboard REST read
only. Separately t_01a1a521638d0a78 (runs_on ["hermes-hub"], a label no machine carries) sat ready for days with
nothing on the card saying why.

Pinned here: the ``kanban_show`` tool and ``hermes kanban show`` (JSON and text) carry the same ``queue`` as REST for
every ready card (no 30-minute wait), a runs_on no known machine satisfies reads ``unplaceable`` with the missing
labels, and the dispatcher records that once as a ``skipped_unplaceable`` event.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_worker_node as kwn


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 7)
    kb.create_board("other")  # 7 workers on another board, really counted through its file (read-only)
    with kbc.connect_closing(board="other") as other:
        for i in range(7):
            busy = kb.create_task(other, title=f"busy {i}", assignee="default")
            other.execute("UPDATE tasks SET status='running' WHERE id=?", (busy,))
        other.commit()
    yield h
    kwn.set_registry_fetch(None)


def _card(title, *, priority=0, created_at=None, runs_on=None):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, assignee="default", priority=priority)
        if created_at is not None:
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (created_at, tid))
        if runs_on is not None:
            if "runs_on" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
                conn.execute("ALTER TABLE tasks ADD COLUMN runs_on TEXT")  # a synced board's column
            conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", (json.dumps(runs_on), tid))
        conn.commit()
    return tid


def _registry(monkeypatch, machines, me="hub"):
    """A synced board whose Board registry lists ``machines`` ({name: labels})."""
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True})
    kwn.set_registry_fetch(lambda: {"me": me, "machines": [{"name": n, "labels": lb} for n, lb in machines.items()]})


def _tool(tid):
    from tools import kanban_show
    return json.loads(kanban_show.show({"task_id": tid}))


def _cli(capsys, *argv):
    from hermes_cli import kanban as kcli
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    capsys.readouterr()
    rc = kcli.kanban_command(parser.parse_args(["kanban", *argv]))
    return rc, capsys.readouterr().out


def _rest(tid):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_show_queue_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app).get(f"/api/plugins/kanban/tasks/{tid}").json()


LOCAL_BEHIND_ONE = {"lane": "local", "ahead": 1, "running": 7, "limit": 7, "approximate": True, "board": "default"}


def _placed(queue):
    """The queue place without its observation time (it varies by the second)."""
    assert isinstance(queue.pop("observed_at"), int)
    return queue


def test_tool_shows_queue_for_a_fresh_ready_card_at_the_cap(home):
    _card("p5 ahead", priority=5, created_at=200)
    tid = _card("p0 mine", priority=0, created_at=100)
    out = _tool(tid)
    assert _placed(out.get("queue")) == LOCAL_BEHIND_ONE
    note = out.get("queue_note") or ""
    assert "1 ahead" in note and "7/7" in note
    assert "not stuck" in note and "outside the dispatcher" in note


def test_cli_json_and_text_show_the_queue(home, capsys):
    _card("p5 ahead", priority=5, created_at=200)
    tid = _card("p0 mine", priority=0, created_at=100)
    rc, out = _cli(capsys, "show", tid, "--json")
    assert rc == 0 and _placed(json.loads(out).get("queue")) == LOCAL_BEHIND_ONE
    rc, out = _cli(capsys, "show", tid)
    assert rc == 0
    line = next((ln for ln in out.splitlines() if ln.strip().startswith("queue:")), "")
    assert "1 ahead" in line and "7/7" in line and "not stuck" in line


def test_non_ready_card_has_no_queue(home, capsys):
    tid = _card("done")
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        conn.commit()
    assert "queue" not in _tool(tid)
    rc, out = _cli(capsys, "show", tid, "--json")
    assert rc == 0 and "queue" not in json.loads(out)


def test_runs_on_no_machine_carries_reads_unplaceable(home, monkeypatch, capsys):
    tid = _card("PR831 land", runs_on=["hermes-hub"])
    _registry(monkeypatch, {"hub": ["linux"], "worker-1": ["linux"]})
    want = {"lane": "unplaceable", "runs_on": ["hermes-hub"], "missing": ["hermes-hub"],
            "machines": ["hub", "worker-1"], "task_id": tid}
    out = _tool(tid)
    assert out.get("queue") == want
    assert "no machine" in (out.get("queue_note") or "") and "hermes-hub" in out["queue_note"]
    rc, text = _cli(capsys, "show", tid)
    assert rc == 0 and "unplaceable" in text and "hermes-hub" in text


def test_runs_on_another_known_machine_reads_that_machine(home, monkeypatch):
    tid = _card("for worker-1", runs_on=["machine:worker-1"])
    _registry(monkeypatch, {"hub": ["linux"], "worker-1": ["linux"]})
    assert _tool(tid).get("queue") == {"lane": "machine", "runs_on": ["machine:worker-1"], "machines": ["worker-1"]}


def test_runs_on_this_machine_satisfies_stays_local(home, monkeypatch):
    tid = _card("for hub", runs_on=["linux"])
    _registry(monkeypatch, {"hub": ["linux"]})
    assert _tool(tid).get("queue", {}).get("lane") == "local"


def test_unknown_registry_never_claims_unplaceable(home, monkeypatch):
    tid = _card("PR831 land", runs_on=["hermes-hub"])
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True})

    def boom():
        raise OSError("board unreachable")
    kwn.set_registry_fetch(boom)
    assert _tool(tid).get("queue", {}).get("lane") == "local"


@pytest.mark.parametrize("unplaceable", [False, True])
def test_rest_tool_and_cli_agree_on_queue(home, monkeypatch, capsys, unplaceable):
    _card("p5 ahead", priority=5, created_at=200)
    tid = _card("p0 mine", priority=0, created_at=100, runs_on=["hermes-hub"] if unplaceable else None)
    if unplaceable:
        _registry(monkeypatch, {"hub": ["linux"]})
    rest = _rest(tid).get("queue")
    tool = _tool(tid).get("queue")
    rc, out = _cli(capsys, "show", tid, "--json")
    assert rc == 0 and rest is not None

    def place(queue):
        """The place each read reports, without its wall-clock observation second (three reads can straddle one)."""
        return {k: v for k, v in queue.items() if k != "observed_at"}
    assert place(rest) == place(tool) == place(json.loads(out).get("queue"))


def test_dispatcher_records_unplaceable_once(home, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli.kanban_db_dispatch_preclaim import eligible_rows
    from hermes_cli.kanban_db_dispatch_queries import lane_rows
    tid = _card("PR831 land", runs_on=["hermes-hub"])
    _registry(monkeypatch, {"hub": ["linux"]})
    monkeypatch.setattr(kwn, "dispatch_refusals", lambda conn, status: {tid: "labels_mismatch"})
    with kbc.connect_closing() as conn:
        for _ in range(3):
            res = kbd.DispatchResult()
            res.scheduling_admitted = True
            assert eligible_rows(conn, lane_rows(conn, "ready"), "ready", res) == []
            assert res.skipped_ineligible == [(tid, "labels_mismatch")]
        events = [e for e in kb.list_events(conn, tid) if e.kind == "skipped_unplaceable"]
    assert len(events) == 1
    assert events[0].payload["missing"] == ["hermes-hub"]
