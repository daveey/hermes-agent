"""Actual dispatch/read seams, isolated four-card harness; no model calls."""
from dataclasses import asdict

from hermes_cli import kanban_capacity_evidence as evidence
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_queue_position as queue
from tests.hermes_cli import test_kanban_capacity_harness as harness

capacity_board = harness.capacity_board


def test_dispatch_publishes_pressure_without_changing_decision(capacity_board, all_assignees_spawnable, monkeypatch):
    conn, ids, child = capacity_board
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda sample=None: "critical")
    seen = []
    result = dispatch.dispatch_once(conn, spawn_fn=lambda *a: seen.append(a),
                                    max_in_progress=4, reconcile_orphans=False, board="default")
    assert seen == [] and result.memory_pressure == "critical"
    out = evidence.read(conn, kb.get_task(conn, ids[0]), board="default")
    assert out["freshness"] == "current"
    assert out["reasons"][0]["code"] == "capacity.memory_pressure"
    assert kb.get_task(conn, child).status == "todo"


def test_drain_is_explicit_not_inferred_from_zero_budget(capacity_board, all_assignees_spawnable, monkeypatch):
    conn, ids, _ = capacity_board
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda sample=None: "ok")
    dispatch.dispatch_once(conn, max_spawn=0, reconcile_orphans=False, board="default",
                           observation_update_drain=True)
    out = evidence.read(conn, kb.get_task(conn, ids[0]), board="default")
    assert [r["code"] for r in out["reasons"]] == ["capacity.update_drain"]
    dispatch.dispatch_once(conn, max_spawn=0, reconcile_orphans=False, board="default")
    assert evidence.read(conn, kb.get_task(conn, ids[0]), board="default")["reasons"] == []


def test_observer_exception_cannot_change_dispatch_return(capacity_board, monkeypatch):
    conn, _, _ = capacity_board
    expected = dispatch.DispatchResult(memory_pressure="critical")
    monkeypatch.setattr(dispatch, "_dispatch_once_locked", lambda *a, **kw: expected)
    monkeypatch.setattr(evidence, "record", lambda *a, **kw: (_ for _ in ()).throw(ValueError("fixture")))
    result = dispatch.dispatch_once(conn, board="default")
    assert result is expected and asdict(result) == asdict(expected)


def test_existing_queue_read_adds_optional_fields(capacity_board, monkeypatch):
    conn, ids, _ = capacity_board
    task = kb.get_task(conn, ids[0])
    monkeypatch.setattr(queue, "_running_and_limit", lambda *a: (0, 4))
    evidence.record(conn, dispatch.DispatchResult(memory_pressure="critical"), board="default", machine="m")
    fields = queue.detail_fields(conn, task, "default")
    place = fields["queue"]
    assert fields["capacity_evidence"]["freshness"] == "current"
    assert fields["execution_observations"][0]["state"] == "unknown"
    assert "capacity.memory_pressure" in fields["capacity_note"]
    assert "capacity_evidence" not in place
    assert place["limit"] == 4 and place["running"] == 0


def test_tool_cli_rest_read_actual_persisted_evidence(capacity_board, monkeypatch, capsys):
    import json
    from argparse import Namespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import kanban as cli
    from tools.kanban_show import show
    from tests.plugins.test_kanban_foreign_card_api import _load_plugin_router

    from hermes_cli import kanban_db_sync, kanban_origin

    conn, ids, child = capacity_board
    # Hermetic gateway identity: never inherit the host's hub.db / hermes-mail name (a CI runner has none).
    monkeypatch.setattr(kanban_db_sync, "sync_config", lambda: {"gateway": "fixture-gw"})
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("", set()))
    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: "")
    monkeypatch.setattr(queue, "_running_and_limit", lambda *a: (0, 4))
    evidence.record(conn, dispatch.DispatchResult(memory_pressure="critical"), board="default", machine="m")
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    with TestClient(app) as client:
        for tid, reason in ((ids[0], "capacity.memory_pressure"), (child, "capacity.dependency")):
            tool = json.loads(show({"task_id": tid}))
            assert cli._cmd_show(Namespace(task_id=tid, json=True, board=None)) == 0
            command = json.loads(capsys.readouterr().out)
            response = client.get(f"/api/plugins/kanban/tasks/{tid}")
            assert response.status_code == 200
            for payload in (tool, command, response.json()):
                assert reason in [r["code"] for r in payload["capacity_evidence"]["reasons"]]
                assert payload["capacity_evidence"]["observed_at_ms"] == tool["capacity_evidence"]["observed_at_ms"]
                work = [value for value in payload["execution_observations"] if value["source"] == "kanban.card"]
                assert len(work) == 1 and work[0]["state"] == ("waiting" if tid == child else "queued")
                assert work[0]["identity"]["work_id"] == tid
                assert work[0]["last_progress"] is None
                if tid == child:
                    assert "queue" not in payload
                    assert payload["capacity_evidence"]["eligible_backlog"] is False
    assert cli._cmd_show(Namespace(task_id=child, json=False, board=None)) == 0
    assert "capacity.dependency" in capsys.readouterr().out


def test_provider_phase_visible_through_tool_cli_and_rest(capacity_board, monkeypatch, capsys):
    import json
    from argparse import Namespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import kanban as cli
    from tools.kanban_show import show
    from tests.plugins.test_kanban_foreign_card_api import _load_plugin_router

    conn, ids, _ = capacity_board
    monkeypatch.setattr(queue, "_running_and_limit", lambda *a: (0, 4))
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    with TestClient(app) as client:
        for tid, started in zip(ids[:2], (False, True), strict=True):
            kb.claim_task(conn, tid)
            task = kb.get_task(conn, tid)
            if started:
                with kb.write_txn(conn):
                    kb._append_event(conn, tid, "worker_bound", {
                        "pid": 222, "started_at": "fixture|birth", "claim_lock": task.claim_lock,
                    }, run_id=task.current_run_id)
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "rate_limited", {"exit_code": 75}, run_id=task.current_run_id)
            evidence.record(conn, dispatch.DispatchResult(rate_limited=[tid]), board="default", machine="m")
            tool = json.loads(show({"task_id": tid}))
            assert cli._cmd_show(Namespace(task_id=tid, json=True, board=None)) == 0
            command = json.loads(capsys.readouterr().out)
            response = client.get(f"/api/plugins/kanban/tasks/{tid}")
            assert response.status_code == 200
            phase = "post_start" if started else "post_claim"
            for payload in (tool, command, response.json()):
                reason = payload["capacity_evidence"]["reasons"][0]
                assert reason["code"] == "capacity.provider_limit" and reason["phase"] == phase
            assert cli._cmd_show(Namespace(task_id=tid, json=False, board=None)) == 0
            assert phase in capsys.readouterr().out


def test_unmapped_restriction_remains_explicit(capacity_board):
    conn, ids, _ = capacity_board
    evidence.record(conn, dispatch.DispatchResult(at_capacity="max_in_progress"), board="default", machine="m")
    out = queue.detail_fields(conn, kb.get_task(conn, ids[0]))
    assert "at_capacity" in out["capacity_note"]
    assert "no restriction observed" not in out["capacity_note"]
