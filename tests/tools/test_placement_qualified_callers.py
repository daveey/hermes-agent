"""Placement discovery and caller fences retain a qualified foreign origin."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_forward as kpf
from hermes_cli import kanban_placement_provenance as provenance
from tests.tools import test_kanban_cross_board_creator_chats as helpers
from tools import kanban_tools as tools

home = helpers.home
AUTHORITY = {"creator": {"gateway": "hub", "profile": "owner", "board": "default", "task_id": "t_creator"},
             "run_uid": "run-portable"}


def creator(monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="creator", assignee="default", session_id=helpers.WORKER)
        helpers._link(conn, tid, "macbook", "chat-profile", helpers.FOREIGN_CHAT)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_SESSION_ID", helpers.WORKER)
    monkeypatch.setenv("HERMES_SESSION_KEY", helpers.WORKER)
    return tid


def test_immediate_preserves_full_origin_and_creator(home, monkeypatch):
    tid = creator(monkeypatch)
    sent, authorities = [], []
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "hub", "why": "route"})
    def authority(conn, task_id, run_id, *, self_gw):
        authorities.append(task_id)
        return AUTHORITY
    monkeypatch.setattr(provenance, "forwarding_authority", authority)
    monkeypatch.setattr(kp, "create_remote", lambda args, **kw: sent.append(kw) or {
        "ok": True, "request_id": "forward:test", "queued_only": True})
    out = json.loads(tools._handle_create({"title": "child", "requires": []}))
    assert out["ok"] is True
    assert authorities == [tid]
    assert sent[0]["origin_gateway"] == "macbook"
    assert sent[0]["origin_profile"] == "chat-profile"
    assert sent[0]["origin_session"] == helpers.FOREIGN_CHAT
    assert sent[0]["creator"] == AUTHORITY["creator"]
    assert sent[0]["run_uid"] == AUTHORITY["run_uid"]


@pytest.mark.parametrize("exception", [False, True])
def test_qualified_transport_failure_never_creates_local(home, monkeypatch, exception):
    creator(monkeypatch)
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "hub", "why": "route"})
    monkeypatch.setattr(provenance, "forwarding_authority", lambda *a, **k: AUTHORITY)
    def send(*a, **k):
        if exception:
            raise OSError("uncertain send")
        return {"ok": False, "error": "refused"}
    monkeypatch.setattr(kp, "create_remote", send)
    out = json.loads(tools._handle_create({"title": "must not exist", "requires": []}))
    assert out["created"] is False
    with kbc.connect_closing() as conn:
        assert not conn.execute("SELECT 1 FROM tasks WHERE title='must not exist'").fetchone()


def test_foreign_deferred_create_refuses_before_creating_placeholder(home, monkeypatch):
    creator(monkeypatch)
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "local", "defer": "hub", "why": "parents"})
    with kbc.connect_closing() as conn:
        before = [tuple(row) for row in conn.execute("SELECT id FROM tasks ORDER BY id")]
    out = json.loads(tools._handle_create({"title": "deferred child", "requires": []}))
    assert out.get("created") is False, out
    assert out["code"] == "placement_unavailable"
    assert "foreign-origin deferred placement is unsupported" in out["error"]
    assert "task_id" not in out
    with kbc.connect_closing() as conn:
        assert [tuple(row) for row in conn.execute("SELECT id FROM tasks ORDER BY id")] == before


def test_foreign_origin_without_deferral_still_creates_local_card(home, monkeypatch):
    creator(monkeypatch)
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "local", "why": "local marker"})
    out = json.loads(tools._handle_create({"title": "local foreign child", "requires": []}))
    assert out["ok"] is True, out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, out["task_id"]) is not None
        assert kpf.state(conn, out["task_id"]) is None


@pytest.mark.parametrize("owner", ["local", "none"])
def test_supported_deferred_create_still_records_placeholder(home, monkeypatch, owner):
    tid = creator(monkeypatch)
    with kbc.connect_closing() as conn:
        conn.execute("DELETE FROM task_chat_links WHERE task_id=?", (tid,))
        conn.commit()
        if owner == "local":
            helpers._link(conn, tid, "here", "default", helpers.CHAT)
    monkeypatch.setattr(kp, "decide", lambda *a, **k: {"where": "local", "defer": "hub", "why": "parents"})
    out = json.loads(tools._handle_create({"title": "supported deferred child", "requires": []}))
    assert out["ok"] is True, out
    with kbc.connect_closing() as conn:
        st = kpf.state(conn, out["task_id"])
        assert st["target"] == "hub"
        assert st["origin_session"] == (helpers.CHAT if owner == "local" else None)
        assert not st.get("origin_gateway")
