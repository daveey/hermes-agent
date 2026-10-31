"""reconcile_notify supervisor requests: worker request -> non-worker executor -> owner-read receipt. [daveey]

Every test runs on an isolated board, session stores and hub.db under ``tmp_path``. The hermes-hub CLI
is replaced at its argv boundary (``_run_hub``, or a fake ``HERMES_HUB_BIN`` script for the cross-process
test); owner answers are written into the hub.db ``sent`` table the way the courier's ``fetch_outcomes``
writes them.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_notify_reconcile_exec as nrx
from hermes_cli import kanban_supervisor_requests as ksr
from hermes_cli.kanban_session_activity import _unseen_on

REPO = Path(__file__).resolve().parents[2]
CHAT = "20261026_235722_bef6c6"
OWNER = "macbook"
SENT = ("CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, "
        "created_at REAL, done_at REAL, posted INTEGER DEFAULT 0)")


@pytest.fixture
def world(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    hub = tmp_path / "hub-state"
    hub.mkdir()
    mail = tmp_path / "mail.json"
    mail.write_text(json.dumps({"gateway": "hub"}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    monkeypatch.setenv("HERMES_HUB_STATE", str(hub))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(mail))
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    db = kb.kanban_db_path()
    assert str(db).startswith(str(tmp_path)), db  # never the live board
    (home / "state.db").touch()
    sqlite3.connect(home / "state.db").execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)").connection.commit()
    conn = kbc.connect()
    try:
        requester = kb.create_task(conn, title="verify unseen", assignee="default", created_by="default")
        tasks, keys = [], []
        for name in ("a", "b"):
            tid = kb.create_task(conn, title=name, assignee="default")
            notify.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT, notifier_profile="default",
                                  delivery_metadata={"reason": "send_refused"})
            notify.add_notify_sub(conn, task_id=tid, platform="hub", chat_id=f"{OWNER}/default/{CHAT}")
            with kb.write_txn(conn):
                kb._append_event(conn, tid, "completed", {"summary": name})
            eid = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=?", (tid,)).fetchone()[0]
            tasks.append(tid)
            keys.append(f"remote:hub:default/default/{tid}:{eid}")
        kb.claim_task(conn, requester)
        run_id = kb._current_run_id(conn, requester)
    finally:
        conn.close()
    calls = []

    def fake_hub(argv):
        calls.append(argv)
        return _queue_like_courier(hub / "hub.db", argv)
    monkeypatch.setattr(nrx, "_run_hub", fake_hub)
    monkeypatch.setenv("HERMES_KANBAN_TASK", requester)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    return {"requester": requester, "run_id": run_id, "tasks": tasks, "keys": keys, "hub_db": hub / "hub.db",
            "home": home, "db": str(db), "tmp": tmp_path, "calls": calls}


def _queue_like_courier(path: Path, argv: list[str]) -> tuple[int, str, str]:
    """``hermes-hub request <gw> <coll> <key> <action> --arg K=V --id ID``: the courier's local queue row."""
    gw, coll, key, action = argv[1:5]
    args = dict(a.split("=", 1) for a in argv[argv.index("--arg") + 1:argv.index("--arg") + 2])
    rid = argv[argv.index("--id") + 1]
    body = {"id": rid, "to_gw": gw, "coll": coll, "key": key, "action": action, "args": args}
    c = sqlite3.connect(path)
    c.execute(SENT)
    c.execute("INSERT INTO sent (id, body, state, created_at) VALUES (?, ?, 'local', ?) ON CONFLICT(id) DO NOTHING",
              (rid, json.dumps(body), time.time()))
    stored = json.loads(c.execute("SELECT body FROM sent WHERE id=?", (rid,)).fetchone()[0])
    c.commit()
    c.close()
    if stored != body:
        return 2, "", "request_id_conflict: request id already used for a different envelope"
    return 0, rid + "\n", ""


def _answer(w, *, state="done", delivered=None, session_exists=True, result=None, mutate=None):
    """The owner's outcome, written into ``sent`` the way fetch_outcomes does."""
    c = sqlite3.connect(w["hub_db"])
    rows = c.execute("SELECT id, body FROM sent").fetchall()
    assert rows, "no owner read was queued"
    for rid, raw in rows:
        body = json.loads(raw)
        keys = body["args"]["keys"].split(",")
        res = result if result is not None else {
            "code": "notice_receipts", "profile": body["key"].split("/")[0], "session": body["key"].split("/")[1],
            "keys": keys, "session_exists": session_exists,
            "delivered": keys if delivered is None else [k for k in keys if k in delivered]}
        if mutate:
            mutate(body, res)
        c.execute("UPDATE sent SET state=?, result=?, body=? WHERE id=?",
                  (state, json.dumps(res), json.dumps(body), rid))
    c.commit()
    c.close()


def _as_executor(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)


def _ops(w, tasks=None):
    return [{"op": "reconcile_notify", "task_id": t, "chat_id": CHAT} for t in (tasks or w["tasks"])]


def _tool(args):
    from tools import kanban_tools as kt
    return json.loads(kt._handle_supervisor_request(args))


def _tui_rows(conn, w):
    return conn.execute("SELECT task_id FROM kanban_notify_subs WHERE platform='tui' AND chat_id=?",
                        (CHAT,)).fetchall()


def _file_and_start(w, monkeypatch, ops=None):
    out = _tool({"ops": ops or _ops(w)})
    assert out["ok"] and out["status"] == "pending", out
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        first = ksr.drain(conn)
    assert first["processed"] == [(out["request_id"], nrx.PHASE)]
    return out["request_id"]


def _finish(w):
    with kbc.connect() as conn:
        return ksr.drain(conn)["processed"], ksr.get_request(conn, ksr.list_requests(conn)[0]["id"])


# --- request -> apply ------------------------------------------------------------------------------

def test_owner_receipt_retires_the_rows_and_reports_on_the_requester(world, monkeypatch):
    w = world
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {CHAT: 2}
    rid = _file_and_start(w, monkeypatch)
    # One owner read for the chat, carrying both exact keys; nothing removed yet.
    assert len(w["calls"]) == 1
    argv = w["calls"][0]
    assert argv[:5] == ["request", OWNER, "sessions", f"default/{CHAT}", "notice-receipts"]
    assert argv[argv.index("--arg") + 1] == "keys=" + ",".join(sorted(w["keys"]))
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {CHAT: 2}
        assert ksr.drain(conn)["processed"] == []  # owner has not answered: still waiting, no attempt spent
        assert ksr.get_request(conn, rid)["attempts"] == 1
    _answer(w)
    processed, req = _finish(w)
    assert processed == [(rid, "done")]
    assert [r["outcome"] for r in req["result"]] == ["reconciled", "reconciled"]
    with kbc.connect() as conn:
        assert _tui_rows(conn, w) == []
        assert _unseen_on(conn) == {}
        hub_rows = conn.execute("SELECT COUNT(*) FROM kanban_notify_subs WHERE platform='hub'").fetchone()[0]
        assert hub_rows == 2  # the real route is untouched
        for tid in w["tasks"]:
            (payload,) = [e.payload for e in kb.list_events(conn, tid) if e.kind == "notify_reconciled"]
            assert payload["owner"] == OWNER
        kinds = [e.kind for e in kb.list_events(conn, w["requester"]) if e.kind.startswith("supervisor_request_")]
        assert kinds == ["supervisor_request_queued", "supervisor_request_accepted", "supervisor_request_done"]
        comment = kb.list_comments(conn, w["requester"])[-1]
        assert comment.author == ksr.COMMENT_AUTHOR
        assert f"reconcile_notify {w['tasks'][0]} tui:{CHAT}: reconciled" in comment.body
        assert kb.get_task(conn, w["requester"]).status == "running"  # never releases the requester


def test_no_receipt_keeps_the_row(world, monkeypatch):
    w = world
    _file_and_start(w, monkeypatch)
    _answer(w, delivered=[w["keys"][0]])
    _processed, req = _finish(w)
    assert [r["outcome"] for r in req["result"]] == ["reconciled", "missing_receipt"]
    with kbc.connect() as conn:
        assert [r[0] for r in _tui_rows(conn, w)] == [w["tasks"][1]]
        assert _unseen_on(conn) == {CHAT: 1}


def test_unknown_owner_session_keeps_the_rows(world, monkeypatch):
    w = world
    _file_and_start(w, monkeypatch)
    _answer(w, session_exists=False)
    _processed, req = _finish(w)
    assert {r["outcome"] for r in req["result"]} == {"unknown_owner"}
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {CHAT: 2}


@pytest.mark.parametrize("hub_chats", [[], [f"{OWNER}/default/{CHAT}", f"mini/default/{CHAT}"]])
def test_no_single_remote_route_keeps_the_row_without_asking(world, monkeypatch, hub_chats):
    w = world
    with kbc.connect() as conn:
        with kb.write_txn(conn):
            conn.execute("DELETE FROM kanban_notify_subs WHERE platform='hub'")
        for chat in hub_chats:
            notify.add_notify_sub(conn, task_id=w["tasks"][0], platform="hub", chat_id=chat)
    out = _tool({"ops": _ops(w, w["tasks"][:1])})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "done")]
        assert ksr.get_request(conn, out["request_id"])["result"][0]["outcome"] in ("unknown_owner", "missing_remote_route")
        assert _unseen_on(conn) == {CHAT: 2}
    assert w["calls"] == []


def test_unreadable_local_store_keeps_the_row(world, monkeypatch):
    w = world
    _file_and_start(w, monkeypatch)
    _answer(w)
    (w["home"] / "state.db").unlink()
    (w["home"] / "state.db").mkdir()  # exists, cannot be opened as a database
    _processed, req = _finish(w)
    assert {r["outcome"] for r in req["result"]} == {"evidence_unavailable"}
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {CHAT: 2}


def test_a_local_chat_keeps_the_row(world, monkeypatch):
    w = world
    prof = w["home"] / "profiles" / "work"
    prof.mkdir(parents=True)
    c = sqlite3.connect(prof / "state.db")
    c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    c.execute("INSERT INTO sessions VALUES (?)", (CHAT,))
    c.commit()
    c.close()
    _file_and_start(w, monkeypatch)
    _answer(w)
    _processed, req = _finish(w)
    assert {r["outcome"] for r in req["result"]} == {"local_owner"}


@pytest.mark.parametrize("what", ["envelope", "echo_keys", "echo_session", "extra_delivered"])
def test_an_answer_that_does_not_match_the_read_keeps_the_rows(world, monkeypatch, what):
    w = world
    _file_and_start(w, monkeypatch)

    def mutate(body, res):
        if what == "envelope":
            body["to_gw"] = "mini"
        elif what == "echo_keys":
            res["keys"] = res["keys"][:1]
        elif what == "echo_session":
            res["session"] = "other"
        else:
            res["delivered"] = res["delivered"] + ["remote:hub:default/default/t_x:1"]
    _answer(w, mutate=mutate)
    _processed, req = _finish(w)
    assert {r["outcome"] for r in req["result"]} == {"evidence_unavailable"}
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {CHAT: 2}


@pytest.mark.parametrize("state", ["rejected", "expired", "failed", "quarantined"])
def test_a_failed_owner_read_finishes_at_once_and_keeps_the_rows(world, monkeypatch, state):
    w = world
    rid = _file_and_start(w, monkeypatch)
    _answer(w, state=state, result={"code": "owner_reader_unavailable", "error": "boom"})
    processed, req = _finish(w)
    assert processed == [(rid, "done")]
    assert {r["outcome"] for r in req["result"]} == {"evidence_unavailable"}
    assert "owner_reader_unavailable" in req["result"][0]["detail"]
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {CHAT: 2}


def test_owner_silent_past_the_deadline_fails_and_keeps_the_rows(world, monkeypatch):
    w = world
    rid = _file_and_start(w, monkeypatch)
    with kbc.connect() as conn:
        assert nrx.poll(conn, "x:1:dispatcher", now=int(time.time()) + nrx.OWNER_DEADLINE_S - 60) == []
        out = ksr.drain(conn, now=int(time.time()) + nrx.OWNER_DEADLINE_S + 3600)
        assert out["processed"] == [(rid, "failed")]
        req = ksr.get_request(conn, rid)
        assert "did not answer" in req["error"]
        assert _unseen_on(conn) == {CHAT: 2}


def test_new_completion_after_the_read_is_not_retired_on_old_evidence(world, monkeypatch):
    w = world
    _file_and_start(w, monkeypatch)
    _answer(w)
    with kbc.connect() as conn, kb.write_txn(conn):
        kb._append_event(conn, w["tasks"][0], "completed", {"summary": "again"})
    _processed, req = _finish(w)
    assert [r["outcome"] for r in req["result"]] == ["evidence_unavailable", "reconciled"]
    with kbc.connect() as conn:
        assert [r[0] for r in _tui_rows(conn, w)] == [w["tasks"][0]]


def test_executor_crash_and_restarts_reuse_the_read_and_spend_no_attempts(world, monkeypatch):
    w = world
    rid = _file_and_start(w, monkeypatch)
    with kbc.connect() as conn:
        with kb.write_txn(conn):  # the accepting executor died; its lease is long gone
            conn.execute("UPDATE supervisor_requests SET executor='gone-host:1:dispatcher', lease_expires=1")
        for _ in range(ksr.MAX_ATTEMPTS + 1):
            assert ksr.drain(conn, now=int(time.time()) + 3600)["processed"] == []
        req = ksr.get_request(conn, rid)
        assert req["status"] == "accepted" and req["attempts"] == 1
    assert len({c[c.index("--id") + 1] for c in w["calls"]}) == 1  # one owner read, never a second id
    _answer(w)
    processed, req = _finish(w)
    assert processed == [(rid, "done")] and {r["outcome"] for r in req["result"]} == {"reconciled"}


def test_a_read_the_hub_cli_could_not_queue_is_queued_again_with_the_same_id(world, monkeypatch):
    w = world
    queued = []
    monkeypatch.setattr(nrx, "_run_hub", lambda argv: queued.append(argv) or (1, "", "hub.db locked"))
    rid = _file_and_start(w, monkeypatch)
    monkeypatch.setattr(nrx, "_run_hub", lambda argv: queued.append(argv) or _queue_like_courier(w["hub_db"], argv))
    with kbc.connect() as conn:
        assert ksr.drain(conn)["processed"] == []
    assert len(queued) >= 2 and all(q == queued[0] for q in queued)  # retried each drain, same id + envelope
    c = sqlite3.connect(w["hub_db"])
    assert c.execute("SELECT COUNT(*) FROM sent").fetchone()[0] == 1
    _answer(w)
    processed, _req = _finish(w)
    assert processed == [(rid, "done")]


def test_a_preseeded_read_id_conflict_fails_and_keeps_the_rows(world, monkeypatch):
    w = world
    monkeypatch.setattr(nrx, "_run_hub", lambda argv: (2, "", "request_id_conflict: request id already used"))
    out = _tool({"ops": _ops(w)})
    _as_executor(monkeypatch)
    with kbc.connect() as conn:
        assert ksr.drain(conn)["processed"] == [(out["request_id"], "failed")]
        assert _unseen_on(conn) == {CHAT: 2}


def test_two_owner_reads_settle_independently_and_report_what_each_did(world, monkeypatch):
    w = world
    with kbc.connect() as conn:  # second task's chat is owned by another gateway
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET chat_id='chat_two' WHERE task_id=? AND platform='tui'",
                         (w["tasks"][1],))
            conn.execute("UPDATE kanban_notify_subs SET chat_id='mini/default/chat_two' WHERE task_id=? "
                         "AND platform='hub'", (w["tasks"][1],))
    ops = [{"op": "reconcile_notify", "task_id": w["tasks"][0], "chat_id": CHAT},
           {"op": "reconcile_notify", "task_id": w["tasks"][1], "chat_id": "chat_two"}]
    rid = _file_and_start(w, monkeypatch, ops)
    assert sorted(c[1] for c in w["calls"]) == ["macbook", "mini"]
    c = sqlite3.connect(w["hub_db"])
    (mac_id,) = [r for (r, b) in c.execute("SELECT id, body FROM sent") if json.loads(b)["to_gw"] == OWNER]
    c.close()
    _answer(w)  # both answered, but hide mini's answer for now
    c = sqlite3.connect(w["hub_db"])
    c.execute("UPDATE sent SET state='sent' WHERE id<>?", (mac_id,))
    c.commit()
    with kbc.connect() as conn:
        assert ksr.drain(conn)["processed"] == []
        assert ksr.drain(conn)["processed"] == []  # a later poll must not relabel the retired row "absent"
    c.execute("UPDATE sent SET state='done' WHERE id<>?", (mac_id,))
    c.commit()
    c.close()
    processed, req = _finish(w)
    assert processed == [(rid, "done")]
    assert [r["outcome"] for r in req["result"]] == ["reconciled", "reconciled"]
    assert [r["owner"] for r in req["result"]] == ["macbook", "mini"]


def test_a_transient_hub_db_error_waits_instead_of_failing(world, monkeypatch):
    w = world
    rid = _file_and_start(w, monkeypatch)
    _answer(w)
    real = nrx._sent
    monkeypatch.setattr(nrx, "_sent", lambda i: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    with kbc.connect() as conn:
        assert ksr.drain(conn)["processed"] == []
        assert ksr.get_request(conn, rid)["status"] == "accepted"
    monkeypatch.setattr(nrx, "_sent", real)
    processed, req = _finish(w)
    assert processed == [(rid, "done")] and {r["outcome"] for r in req["result"]} == {"reconciled"}


# --- request shape -------------------------------------------------------------------------------

@pytest.mark.parametrize("op", [
    {"op": "reconcile_notify", "task_id": "t", "chat_id": CHAT, "delivered": ["k"]},
    {"op": "reconcile_notify", "task_id": "t", "chat_id": CHAT, "owner": "macbook"},
    {"op": "reconcile_notify", "task_id": "t", "chat_id": "a/b"},
    {"op": "reconcile_notify", "chat_id": CHAT},
])
def test_submit_refuses_evidence_fields_and_bad_rows(world, op):
    out = _tool({"ops": [op]})
    assert not out.get("ok") and out["code"] == "invalid_ops", out


def test_submit_refuses_mixed_ops(world):
    out = _tool({"ops": _ops(world)[:1] + [{"op": "unlink", "parent_id": "a", "child_id": "b"}]})
    assert out["code"] == "invalid_ops" and "only" in out["error"]


def test_preflight_verdicts(world):
    w = world
    with kbc.connect() as conn:
        notify.add_notify_sub(conn, task_id=w["requester"], platform="tui", chat_id=CHAT,
                              delivery_metadata={"reason": "origin"})
    out = _tool({"dry_run": True, "ops": _ops(w, w["tasks"][:1]) + [
        {"op": "reconcile_notify", "task_id": w["requester"], "chat_id": CHAT},
        {"op": "reconcile_notify", "task_id": w["tasks"][1], "chat_id": "other"},
        {"op": "reconcile_notify", "task_id": "t_missing", "chat_id": CHAT}]})
    assert [v["verdict"] for v in out["ops"]] == ["supervisor", "refused", "noop", "refused"]
    out = _tool({"ops": [{"op": "reconcile_notify", "task_id": w["tasks"][1], "chat_id": "other"}]})
    assert out["code"] == "no_supervisor_needed"


# --- the fence stays -----------------------------------------------------------------------------

def test_a_worker_cannot_run_the_executor_or_the_mutation(world, monkeypatch):
    from hermes_cli.kanban_notify_reconcile import reconcile_refused_sub
    w = world
    rid = _tool({"ops": _ops(w)})["request_id"]
    with kbc.connect() as conn:  # HERMES_KANBAN_TASK is set: a dispatcher worker
        with pytest.raises(ksr.SupervisorRequestError, match="worker_context"):
            ksr.drain(conn)
        with pytest.raises(ksr.SupervisorRequestError, match="worker_context"):
            nrx.poll(conn, "x")
        assert ksr.dispatcher_drain(conn) == []
        assert ksr.get_request(conn, rid)["status"] == "pending"
    # Every Kanban worker subprocess inherits the write fence for its board root: the library mutation
    # itself is refused there, with every reader saying "deliver".
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", str(w["home"]))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_OWNER", raising=False)
    with kbc.connect() as conn:
        with pytest.raises(PermissionError):
            reconcile_refused_sub(conn, task_id=w["tasks"][0], chat_id=CHAT, source_gateway="hub",
                                  source_profile="default", board="default",
                                  local_session_exists=lambda p, s: False, owner=lambda p, s: (OWNER, p),
                                  delivered_keys=lambda g, p, s, keys: set(keys))
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT")
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {CHAT: 2}


# --- across processes: worker tool -> separate CLI executor -> real argv boundary -----------------

FAKE_HUB = '''#!{py}
import json, sqlite3, sys, time
sys.path.insert(0, {tests!r})
from test_kanban_notify_reconcile_exec import _queue_like_courier
rc, out, err = _queue_like_courier(__import__("pathlib").Path({db!r}), sys.argv[1:])
sys.stdout.write(out); sys.stderr.write(err); sys.exit(rc)
'''


def _cli_drain(w, hub_bin):
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
    env.update(HERMES_HOME=str(w["home"]), HOME=str(w["tmp"]), HERMES_KANBAN_DB=w["db"], PYTHONPATH=str(REPO),
               HERMES_HUB_BIN=str(hub_bin))
    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "supervisor-requests", "--drain", "--json"],
                          cwd=str(REPO), env=env, capture_output=True, text=True, timeout=180, check=False)


def test_end_to_end_through_a_separate_executor_process_and_the_hub_cli(world, monkeypatch):
    from tools.registry import registry
    import tools.kanban_tools  # registers the tool
    w = world
    hub_bin = w["tmp"] / "hermes-hub"
    hub_bin.write_text(FAKE_HUB.format(py=sys.executable, tests=str(Path(__file__).parent), db=str(w["hub_db"])))
    hub_bin.chmod(0o755)
    out = json.loads(registry.dispatch("kanban_supervisor_request", {"ops": _ops(w)}))
    assert out["ok"] and out["status"] == "pending", out
    first = _cli_drain(w, hub_bin)
    assert first.returncode == 0, first.stderr[-2000:]
    assert json.loads(first.stdout[first.stdout.index("{"):])["drained"]["processed"] == [[out["request_id"], nrx.PHASE]]
    _answer(w)
    second = _cli_drain(w, hub_bin)
    assert second.returncode == 0, second.stderr[-2000:]
    report = json.loads(second.stdout[second.stdout.index("{"):])
    assert report["drained"]["processed"] == [[out["request_id"], "done"]]
    with kbc.connect() as conn:
        assert _unseen_on(conn) == {}
        assert ksr.get_request(conn, out["request_id"])["status"] == "done"
