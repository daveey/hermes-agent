"""Public enrolled service with canonical transport doubles, not live ingress."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_containment_remote as remote
from hermes_cli import kanban_containment_service as service
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_canonical import DeliveryUnknown
from tests.hermes_cli import test_kanban_containment_service as public
from tests.hermes_cli import test_kanban_containment_canonical as wire

board = public.board


class Transport:
    def __init__(self, conn):
        self.ident = SimpleNamespace(gateway="worker")
        self.posts = []
        self.events = {}
        self.rows = {}
        self.drop_reply = False
        self.closed = 0
        for row in conn.execute("SELECT * FROM tasks"):
            data = dict(row)
            data.update(uid=data["id"], current_run_uid=None, version=100, origin_gateway="home")
            self.rows[("tasks", data["id"])] = data
        for row in conn.execute("SELECT * FROM task_runs"):
            self.rows[("task_runs", row["uid"])] = {**dict(row), "version": 100, "origin_gateway": "worker"}

    def containment_context(self, slug, task, producer=None):
        def coords(tid):
            return {"gateway": "home", "board": slug, "task_id": tid}
        data = {"version": 1, "board": slug, "target": coords(task), "producer_baseline": None,
                "status": self.rows[("tasks", task)]["status"], "held": False}
        if producer is not None:
            data["producer"] = coords(producer)
        return data

    def ops(self, slug, operations):
        self.posts.append(copy.deepcopy(operations))
        op = operations[0]
        if op["event_uid"] not in self.events:
            if op["op"] == "contain":
                stop, episode = op["payload"], op["event_uid"]
            else:
                episode = op["payload"]["episode"]
                import json
                stop = json.loads(self.events[episode]["row"]["payload"])
            proof = wire.evidence(op, stop, episode)
            proof["row"].update(task_id=op["task"], created_at=1)
            self.events[op["event_uid"]] = proof
            row = self.rows[("tasks", op["task"])]
            row.update(status="triage" if op["op"] == "contain" else "ready", current_run_uid=None,
                       claim_lock=None, claim_expires=None, worker_pid=None, worker_started_at=None,
                       containment_episode=episode if op["op"] == "contain" else None)
            if op["actor_task"] == op["task"]:
                self.rows[("task_runs", op["actor_run_uid"])].update(ended_at=2, outcome="contained", status="contained", claim_lock=None)
        if self.drop_reply:
            raise TimeoutError("fixture accepted request before losing reply")
        return {"results": [{"client_op_id": op["client_op_id"], "status": "applied"}]}

    def row_evidence(self, slug, table, uid):
        if table == "task_events":
            return copy.deepcopy(self.events[uid])
        return {"board": slug, "table": table, "uid": uid, "state": "live", "row": copy.deepcopy(self.rows[(table, uid)])}

    def close(self):
        self.closed += 1


def setup(conn, monkeypatch):
    target, authority, session = public.arguments(conn, monkeypatch)
    monkeypatch.setattr(sync, "sync_enabled", lambda: True)
    monkeypatch.setattr(sync, "board_selected", lambda path: True)
    sync.install_or_remove(conn, conn.execute("PRAGMA database_list").fetchone()[2])
    sync.set_identity(conn, "fixture", "worker")
    sync.set_mode(conn, "primary")
    transport = Transport(conn)
    monkeypatch.setattr(remote, "enrolled", lambda c: (transport, "fixture"))
    return target, authority, session, transport


def adopt(conn, monkeypatch, tid, session):
    task = kb.get_task(conn, tid)
    for key, val in {"HERMES_KANBAN_TASK": tid, "HERMES_KANBAN_RUN_ID": str(task.current_run_id),
                     "HERMES_KANBAN_CLAIM_LOCK": task.claim_lock}.items():
        monkeypatch.setenv(key, val)
    kb._append_event(conn, tid, "owner_admitted", {"session_id": session}, run_id=task.current_run_id)


def test_public_synced_self_stop_then_authority_resume_preserves_edges(board, monkeypatch):
    target, authority, session, hub = setup(board, monkeypatch)
    links = [tuple(r) for r in board.execute("SELECT parent_id,child_id FROM task_links")]
    with scoped_current_session_id(session):
        stopped = service.contain(board, authority=authority, summary="Fixture handoff")
    assert stopped["terminal_for_run"] is True
    assert stopped["local_projected"] is True
    assert kb.get_task(board, target).status == "triage"
    assert kb.get_task(board, target).current_run_id is None
    adopt(board, monkeypatch, authority, session)
    with scoped_current_session_id(session):
        resumed = service.resume(board, task_id=target, episode=stopped["episode"])
    assert resumed["status"] == "ready"
    assert kb.get_task(board, target).status == "ready"
    assert [tuple(r) for r in board.execute("SELECT parent_id,child_id FROM task_links")] == links
    assert all(r[0] == "todo" for r in board.execute("SELECT status FROM tasks WHERE id IN (SELECT child_id FROM task_links WHERE parent_id=?)", (target,)))
    assert hub.closed == 2


def test_resume_succeeds_when_hub_evidence_head_advances(board, monkeypatch):
    target, authority, session, hub = setup(board, monkeypatch)
    head = [1000]
    original = hub.row_evidence

    def moving(slug, table, uid):
        data = original(slug, table, uid)
        head[0] += 7
        data["head"] = head[0]
        return data
    hub.row_evidence = moving
    with scoped_current_session_id(session):
        stopped = service.contain(board, authority=authority, summary="Fixture handoff")
    adopt(board, monkeypatch, authority, session)
    with scoped_current_session_id(session):
        resumed = service.resume(board, task_id=target, episode=stopped["episode"])
    assert resumed["status"] == "ready"
    assert head[0] > 1007


def test_unknown_transport_reuses_committed_request_and_rejects_changed_policy(board, monkeypatch):
    target, authority, session, hub = setup(board, monkeypatch)
    hub.drop_reply = True
    with scoped_current_session_id(session), pytest.raises(DeliveryUnknown):
        service.contain(board, authority=authority, summary="Fixture")
    assert kb.get_task(board, target).status == "running"
    import sqlite3
    from hermes_cli.kanban_containment_connections import held
    assert held(board, target)
    with pytest.raises(sqlite3.IntegrityError, match="containment_pending"):
        board.execute("UPDATE tasks SET status='done' WHERE id=?", (target,))
    with scoped_current_session_id(session), pytest.raises(ContainmentRefused, match="frozen"):
        service.contain(board, authority=target, summary="Fixture")
    hub.drop_reply = False
    with scoped_current_session_id(session):
        result = service.contain(board, authority=authority, summary="Fixture")
    assert hub.posts[0] == hub.posts[1]
    assert board.execute("SELECT COUNT(*) FROM containment_delivery").fetchone()[0] == 1
    assert result["terminal_for_run"] is True


def test_live_authority_recovers_original_uncertain_stop_after_actor_expiry(board, monkeypatch):
    target, authority, session, hub = setup(board, monkeypatch)
    hub.drop_reply = True
    with scoped_current_session_id(session), pytest.raises(DeliveryUnknown):
        service.contain(board, authority=authority, summary="Fixture")
    original = copy.deepcopy(hub.posts[0])
    board.execute("UPDATE task_runs SET claim_expires=0 WHERE task_id=?", (target,))
    adopt(board, monkeypatch, authority, session)
    hub.drop_reply = False
    with scoped_current_session_id(session):
        result = service.resume(board, task_id=target, episode=original[0]["event_uid"])
    assert result["status"] == "ready"
    assert hub.posts[1] == original
    assert hub.posts[2][0]["actor_task"] == authority
    assert kb.get_task(board, target).status == "ready"


def test_known_rejection_allows_a_new_request_without_replaying_refused_bytes(board, monkeypatch):
    target, authority, session, hub = setup(board, monkeypatch)
    def reject(slug, operations):
        hub.posts.append(operations)
        return {"results": [{"client_op_id": operations[0]["client_op_id"], "status": "rejected", "reason": "fixture"}]}
    monkeypatch.setattr(hub, "ops", reject)
    for _ in range(2):
        with scoped_current_session_id(session), pytest.raises(ContainmentRefused, match="refused"):
            service.contain(board, authority=authority, summary="Fixture")
    from hermes_cli.kanban_containment_connections import held
    assert not held(board, target)
    assert len(hub.posts) == 2
    assert hub.posts[0][0]["client_op_id"] != hub.posts[1][0]["client_op_id"]
    board.execute("UPDATE tasks SET status='ready' WHERE id=?", (target,))


def test_confirmed_request_never_answers_new_contain_or_release(board, monkeypatch):
    from hermes_cli.kanban_containment_connections import held

    relay, authority, session, hub = setup(board, monkeypatch)
    child = board.execute("SELECT child_id FROM task_links WHERE parent_id=?", (relay,)).fetchone()[0]
    episodes = []
    for _ in range(2):
        adopt(board, monkeypatch, relay, session)
        before = len(hub.posts)
        with scoped_current_session_id(session):
            stop = service.contain(board, task_id=child, authority=authority, summary="Fixture")
        episodes.append(stop["episode"])
        assert len(hub.posts) == before + 1
        assert held(board, child)
        assert kb.get_task(board, child).status == "triage"
        adopt(board, monkeypatch, authority, session)
        with scoped_current_session_id(session):
            released = service.resume(board, task_id=child, episode=stop["episode"])
        assert released["status"] == "ready"
        assert not held(board, child)
        assert kb.get_task(board, child).status == "ready"
    assert episodes[0] != episodes[1]
    assert len(hub.posts) == 4


def test_recover_stop_does_not_run_for_a_live_non_authority(board, monkeypatch):
    from hermes_cli.kanban_containment_actor import Actor

    target, authority, session, hub = setup(board, monkeypatch)
    hub.drop_reply = True
    with scoped_current_session_id(session), pytest.raises(DeliveryUnknown):
        service.contain(board, authority=authority, summary="Fixture")
    original = copy.deepcopy(hub.posts[0])
    assert original[0]["op"] == "contain"
    assert original[0]["actor_task"] == target
    assert original[0]["payload"]["policy"]["authority"]["task_id"] == authority
    hub.drop_reply = False
    remote._recover_stop(
        board, hub, "fixture",
        Actor(original[0]["actor_task"], original[0]["actor_run_uid"]),
        target, original[0]["event_uid"],
    )
    assert hub.posts == [original]
    assert kb.get_task(board, target).status == "running"



def test_cli_and_tool_unknown_receipts_expose_same_recovery_identity(board, monkeypatch, capsys):
    import argparse
    import json
    from contextlib import contextmanager
    from hermes_cli import kanban_containment_cli as cli
    from hermes_cli import kanban_db_connect as kbc
    from tools import kanban_tools as kt
    from tools import kanban_containment_tools as tools
    target, authority, session, hub = setup(board, monkeypatch)
    hub.drop_reply = True
    @contextmanager
    def opened(*args, **kwargs):
        yield kb, board
    @contextmanager
    def connection(*args, **kwargs):
        yield board
    monkeypatch.setattr(kt, "_board", opened)
    monkeypatch.setattr(kbc, "connect_closing", connection)
    with scoped_current_session_id(session):
        out = json.loads(tools.handle_contain({"task_id": target, "authority": authority, "summary": "Fixture"}))
        rc = cli.command(argparse.Namespace(kanban_action="contain", task_id=target,
                         authority=authority, producer=None, summary="Fixture"))
    printed = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["ok"] is False
    assert printed == out
    assert out["code"] == "containment_delivery_unknown"
    assert out["request_uid"] == hub.posts[0][0]["client_op_id"]
    assert out["episode"] == hub.posts[0][0]["event_uid"]
    assert hub.posts[0] == hub.posts[1]


def test_capability_absence_never_falls_back_to_local(board, monkeypatch):
    target, authority, session, hub = setup(board, monkeypatch)
    monkeypatch.setattr(hub, "containment_context", lambda *a: {"version": True})
    with scoped_current_session_id(session), pytest.raises(ContainmentRefused, match="capability"):
        service.contain(board, authority=authority, summary="Fixture")
    assert not hub.posts
    assert kb.get_task(board, target).status == "running"
