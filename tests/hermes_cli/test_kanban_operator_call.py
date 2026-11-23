"""Scoped Board-operator call: a worker comments on / moves / unblocks ONE named foreign card. [daveey]

Two board files stand in for two gateways: ``worker-5`` runs the caller card, ``hub`` created the target card. A
third file is the Board replica both read; :func:`_board` copies every card, run and event into it (as the Board
feed does), stamps Board uids and remembers which gateway wrote each event (the Board's ``origin_gateway``).
"""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move as km
from hermes_cli import kanban_operator_call as kox
from hermes_cli import kanban_supervisor_requests as ksr
from hermes_cli.kanban_db_connect import connect


class Fleet:
    def __init__(self, tmp_path: Path):
        self.w = connect(tmp_path / "worker5.db")
        self.h = connect(tmp_path / "hub.db")
        self.rep = connect(tmp_path / "replica.db")
        self.gw = {id(self.w): "worker-5", id(self.h): "hub"}
        self.origin: dict[str, str] = {}
        self.canonical: dict[str, dict] = {}
        self.evidence_down = False
        for c in (self.w, self.h, self.rep):
            for table, col in (("tasks", "created_on"), ("task_events", "uid"), ("task_runs", "uid")):
                if col not in {r[1] for r in c.execute(f"PRAGMA table_info({table})")}:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            c.commit()

    def name(self, conn):
        return self.gw.get(id(conn))

    def card(self, conn, title, **kw) -> str:
        tid = kb.create_task(conn, title=title, assignee="default", created_by="default", **kw)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (self.name(conn), tid))
        return tid

    def board(self):
        """One Board round: every live row reaches the replica; Board uids and origin gateways are stamped."""
        for src in (self.w, self.h):
            gw = self.name(src)
            with kb.write_txn(src):
                for (eid,) in src.execute("SELECT id FROM task_events WHERE uid IS NULL").fetchall():
                    src.execute("UPDATE task_events SET uid = ? WHERE id = ?", (str(uuid.uuid4()), eid))
            own = [r[0] for r in src.execute("SELECT id FROM tasks WHERE created_on = ?", (gw,))]
            with kb.write_txn(self.rep):
                for tid in own:
                    row = src.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
                    cols = row.keys()
                    # Upsert, not REPLACE: the board factory refuses REPLACE on tasks (PR4045).
                    self.rep.execute(f"INSERT INTO tasks ({','.join(cols)}) VALUES "
                                     f"({','.join('?' * len(cols))}) ON CONFLICT(id) DO UPDATE SET "
                                     + ','.join(f"{c}=excluded.{c}" for c in cols if c != "id"), tuple(row))
                    for r in src.execute("SELECT * FROM task_runs WHERE task_id = ?", (tid,)).fetchall():
                        rc = [k for k in r.keys() if k != "id"]
                        if not self.rep.execute("SELECT 1 FROM task_runs WHERE uid = ?", (r["uid"],)).fetchone():
                            self.rep.execute(f"INSERT INTO task_runs ({','.join(rc)}) VALUES "
                                             f"({','.join('?' * len(rc))})", tuple(r[k] for k in rc))
                        else:
                            self.rep.execute("UPDATE task_runs SET ended_at = ?, status = ? WHERE uid = ?",
                                             (r["ended_at"], r["status"], r["uid"]))
                for r in src.execute("SELECT task_id, kind, payload, created_at, uid FROM task_events").fetchall():
                    if self.rep.execute("SELECT 1 FROM task_events WHERE uid = ?", (r["uid"],)).fetchone():
                        continue
                    self.rep.execute("INSERT INTO task_events (task_id, kind, payload, created_at, uid) "
                                     "VALUES (?,?,?,?,?)", tuple(r))
                    self.origin[r["uid"]] = gw
                    self.canonical[r["uid"]] = dict(r)
        # The feed: events on each gateway's own cards that another gateway wrote (none here) would come back.

    def run_uid(self, conn, tid) -> str:
        uid = str(uuid.uuid4())
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET uid = ? WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
                         (uid, tid))
            # The Board's lease event for that run (holder = the gateway it leased the card to).
            kb._append_event(conn, tid, "claimed", {"holder": self.name(conn), "run_uid": uid,
                                                     "expires": int(time.time() * 1000) + 60000})
        return uid


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    f = Fleet(tmp_path)

    @contextmanager
    def replica(_conn):
        yield f.rep

    def evidence(_board, uid):
        if f.evidence_down:
            raise OSError("board unreachable")
        row = f.canonical.get(uid)
        return {"table": "task_events", "uid": uid, "state": "live" if row else "never_existed",
                "row": {**row, "origin_gateway": f.origin.get(uid)} if row else None}

    monkeypatch.setattr(kox, "_own_gateway", f.name)
    monkeypatch.setattr(kox, "_replica", replica)
    monkeypatch.setattr(kox, "_synced", lambda conn: True)
    monkeypatch.setattr(kox, "_evidence", evidence)
    def outbox_rows(c):
        # Production assigns event UIDs in the append transaction, before start logs it.
        for (eid,) in c.execute("SELECT id FROM task_events WHERE uid IS NULL").fetchall():
            c.execute("UPDATE task_events SET uid=? WHERE id=?", (str(uuid.uuid4()), eid))
        return c.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    monkeypatch.setattr(kox, "_outbox_rows", outbox_rows)
    monkeypatch.setattr(kox, "_board_slug", lambda conn: "default")
    kox._MARK.clear()
    kox._PERSISTED.clear()
    caller = f.card(f.w, "needs to talk to a hub card")
    kb.claim_task(f.w, caller)
    run_id = kb._current_run_id(f.w, caller)
    f.run_uid(f.w, caller)
    target = f.card(f.h, "hub-owned card")
    f.caller, f.run_id, f.target = caller, run_id, target
    f.board()
    yield f
    for c in (f.w, f.h, f.rep):
        c.close()


def _submit(f, op, **kw):
    return ksr.submit(f.w, env_task=f.caller, env_run=str(f.run_id), ops=[op], **kw)


def _round(f):
    """Caller's drain forwards, Board carries, home executes, Board carries back, caller's drain finishes."""
    ksr.drain(f.w)
    f.board()
    ksr.drain(f.h)
    f.board()
    return ksr.drain(f.w)


def _log(conn, side=None):
    rows = kox.list_calls(conn)
    return [r for r in rows if side is None or r["side"] == side]


def test_worker_comments_on_foreign_card_through_operator_call(fleet):
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "PR #12 is merged"})
    assert out["status"] == "pending" and out["ops"][0]["verdict"] == "supervisor", out
    _round(f)
    comments = f.h.execute("SELECT author, body, origin_gateway FROM task_comments WHERE task_id = ?",
                           (f.target,)).fetchall()
    assert [tuple(c) for c in comments] == [(f"worker-5/{f.caller}", "PR #12 is merged", "worker-5")]
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done", req
    assert req["result"][0]["outcome"] == "applied"
    # The worker never wrote the hub's card; the hub's own executor did, and said so on the card.
    result = f.h.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                         (f.target, kox.RESULT_KIND)).fetchone()
    assert json.loads(result[0])["executor_gateway"] == "hub"


def test_worker_moves_foreign_card_through_operator_call(fleet, monkeypatch):
    f = fleet
    plan = {"runs_on": ["machine:worker-1"], "previous": [], "matches": ["worker-1"], "move_no": 1,
            "forced": False, "me": "hub", "host": {}}
    monkeypatch.setattr(km, "check", lambda *a, **k: plan)
    from hermes_cli import kanban_worker_repin as repin
    monkeypatch.setattr(repin, "applies", lambda conn, tid: False)
    from hermes_cli import kanban_worker_node as wn
    set_to: dict = {}
    monkeypatch.setattr(wn, "set_runs_on", lambda conn, tid, labels: set_to.__setitem__(tid, labels))
    out = _submit(f, {"op": "operator", "action": "move", "task_id": f.target, "runs_on": ["machine:worker-1"],
                      "reason": "needs the GPU box"})
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done", (req["error"], req["result"])
    moved = f.h.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                        (f.target, km.MOVED_EVENT)).fetchone()
    payload = json.loads(moved[0])
    assert set_to == {f.target: ["machine:worker-1"]}
    assert payload["runs_on"] == ["machine:worker-1"]
    assert payload["operator_caller"]["task_id"] == f.caller
    assert payload["operator_request_uid"]


def test_every_call_logged_with_card_and_caller(fleet):
    f = fleet
    _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hello"})
    with pytest.raises(ksr.SupervisorRequestError):
        _submit(f, {"op": "operator", "action": "archive", "task_id": f.target})
    _round(f)
    req_rows = _log(f.w, "request")
    assert {r["outcome"] for r in req_rows} == {"done", "refused"}
    for r in req_rows:
        assert r["target_task_id"] == f.target
        assert (r["caller_gateway"], r["caller_task_id"], r["caller_run_id"]) == ("worker-5", f.caller, f.run_id)
        assert r["caller_run_uid"]
    refused = next(r for r in req_rows if r["outcome"] == "refused")
    assert refused["action"] == "archive" and "operator_action_refused" in refused["detail"]
    exe = _log(f.h, "execute")
    assert len(exe) == 1
    assert (exe[0]["target_task_id"], exe[0]["caller_gateway"], exe[0]["caller_task_id"], exe[0]["outcome"],
            exe[0]["executor_gateway"]) == (f.target, "worker-5", f.caller, "applied", "hub")
    # The refusal is fleet-visible on the caller's card too.
    assert f.w.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = ?",
                       (f.caller, kox.REFUSED_KIND)).fetchone()


@pytest.mark.parametrize("action", ["complete", "archive", "link", "unlink", "block", "edit"])
def test_owner_only_ops_are_refused(fleet, action):
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        _submit(fleet, {"op": "operator", "action": action, "task_id": fleet.target})
    assert exc.value.code == "operator_action_refused"


def test_label_publish_needs_machine_labels_and_proof(fleet):
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        _submit(fleet, {"op": "operator", "action": "label_publish", "task_id": fleet.target})
    assert exc.value.code == "invalid_ops"


def test_only_one_named_card_per_call(fleet):
    f = fleet
    other = f.card(f.h, "another hub card")
    f.board()
    op = {"op": "operator", "action": "comment", "task_id": f.target, "text": "x"}
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        ksr.submit(f.w, env_task=f.caller, env_run=str(f.run_id),
                   ops=[op, {**op, "task_id": other}])
    assert "exactly one card" in str(exc.value)
    with pytest.raises(ksr.SupervisorRequestError) as exc:  # a second card smuggled in the args
        _submit(f, {**op, "child_id": other})
    assert exc.value.code == "invalid_ops"
    assert all(r["outcome"] == "refused" for r in _log(f.w))


def test_home_executes_only_the_card_the_request_names(fleet):
    """A forged request event naming a card on the caller's own card is refused at the home: caller unproven."""
    f = fleet
    other = f.card(f.h, "unrelated hub card")
    with kb.write_txn(f.h):  # someone on the hub writes a request event pretending to be worker-5's caller
        kb._append_event(f.h, other, kox.REQUEST_KIND, {
            "request_uid": "forged", "board": "default", "action": "comment", "target_task_id": other,
            "args": {"text": "spoofed"}, "caller": {"gateway": "worker-5", "task_id": f.caller, "run_id": f.run_id,
                                                     "run_uid": "x"}, "requested_at": int(time.time())})
    f.board()
    kox.apply_incoming(f.h)
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id = ?", (other,)).fetchone()
    row = next(r for r in _log(f.h, "execute") if r["request_uid"] == "forged")
    assert row["outcome"] == "refused" and "caller_unproven" in row["detail"]


def test_origin_gateway_mismatch_is_refused(fleet):
    f = fleet
    _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    f.board()
    for uid in list(f.origin):
        f.origin[uid] = "worker-9"  # the Board says another gateway wrote the request event
    ksr.drain(f.h)
    assert not f.h.execute("SELECT 1 FROM task_comments WHERE task_id = ?", (f.target,)).fetchone()
    assert "caller_unproven" in _log(f.h, "execute")[0]["detail"]
    f.board()
    out = ksr.drain(f.w)
    assert any(s == "rejected" for _rid, s in out["processed"]), out


def test_local_card_and_own_card_are_refused(fleet):
    f = fleet
    local = f.card(f.w, "a worker-5 card")
    f.board()
    for tid, code in ((local, "not_foreign"), (f.caller, "own_card")):
        out = _submit(f, {"op": "operator", "action": "comment", "task_id": tid, "text": "x"}, dry_run=True)
        assert out["ops"][0]["code"] == code, out


def test_unblock_refuses_human_gate_and_hand_hold(fleet):
    f = fleet
    asked = f.card(f.h, "waits on David")
    kb.block_task(f.h, asked, reason="pick A or B", kind="needs_input")
    held = f.card(f.h, "held by hand")
    kb.block_task(f.h, held, reason="hold", actor="user", kind="transient")
    desk = f.card(f.h, "held from the Desktop")
    kb.block_task(f.h, desk, reason="hold", kind="transient")  # Desktop/dashboard blocks carry no actor
    chat = f.card(f.h, "held by a chat agent")
    kb.block_task(f.h, chat, reason="hold", actor="agent (chat local-abc)", kind="transient")
    agent = f.card(f.h, "agent block")
    kb.block_task(f.h, agent, reason="waiting on CI", actor=f"agent (task {agent})", kind="transient")
    f.board()
    out = _submit(f, {"op": "operator", "action": "unblock", "task_id": asked, "reason": "decided"}, dry_run=True)
    assert out["ops"][0]["code"] == "human_gate", out
    # Holds pass the caller's preflight (the replica has no actor) but the home refuses them: only a block the
    # card's own agent run placed may be released by an operator call.
    for tid in (held, desk, chat):
        _submit(f, {"op": "operator", "action": "unblock", "task_id": tid, "reason": "go"})
        _round(f)
        assert kb.get_task(f.h, tid).status == "blocked", tid
        row = next(r for r in _log(f.h, "execute") if r["target_task_id"] == tid)
        assert row["outcome"] == "refused" and "not blocked by its own agent run" in row["detail"]
    out = _submit(f, {"op": "operator", "action": "unblock", "task_id": agent, "reason": "CI is green"})
    _round(f)
    assert kb.get_task(f.h, agent).status != "blocked"
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"


def test_unblock_of_a_terminal_held_stale_copy_names_the_completion_and_reopen(fleet):
    """#2271: the home's row still reads blocked (its own agent's block) but the card's ``completed`` event reached
    the file after it, so ``unblock_task`` refuses (an unblock is a reopen). The refusal must say why and how to
    revive it, not the bare "(state changed)" fallback."""
    f = fleet
    stale = f.card(f.h, "completed elsewhere, stale blocked copy here")
    kb.block_task(f.h, stale, reason="waiting on CI", actor=f"agent (task {stale})", kind="transient")
    with kb.write_txn(f.h):  # the completion's event reached the hub's file, the row did not
        kb._append_event(f.h, stale, "completed", {"result_len": 12})
    assert kb.get_task(f.h, stale).status == "blocked"
    f.board()
    _submit(f, {"op": "operator", "action": "unblock", "task_id": stale, "reason": "CI is green"})
    _round(f)
    assert kb.get_task(f.h, stale).status == "blocked"
    row = next(r for r in _log(f.h, "execute") if r["target_task_id"] == stale)
    assert row["outcome"] == "refused", row
    assert "its completed event holds it done" in row["detail"], row["detail"]
    assert f"hermes kanban reopen {stale} --reason" in row["detail"], row["detail"]
    assert "state changed" not in row["detail"]


def test_running_target_is_not_moved(fleet):
    f = fleet
    kb.claim_task(f.h, f.target)
    f.board()
    out = _submit(f, {"op": "operator", "action": "move", "task_id": f.target, "runs_on": ["machine:worker-1"],
                      "reason": "x"}, dry_run=True)
    assert out["ops"][0]["code"] == "running"


def test_replay_never_applies_twice(fleet):
    f = fleet
    _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "once"})
    ksr.drain(f.w)
    f.board()
    kox.apply_incoming(f.h)
    kox._MARK.clear()  # a restart, and a lost watermark: everything is rescanned
    with kb.write_txn(f.h):
        f.h.execute("DELETE FROM operator_call_scan")
    kox.apply_incoming(f.h)
    with kb.write_txn(f.h):  # even the log row lost (crash after the comment): the card's marker answers
        f.h.execute("DELETE FROM operator_calls")
        f.h.execute("DELETE FROM operator_call_scan")
    kox._MARK.clear()
    kox.apply_incoming(f.h)
    assert f.h.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (f.target,)).fetchone()[0] == 1
    assert _log(f.h, "execute")[0]["outcome"] == "already_applied"


def test_unreachable_board_defers_instead_of_refusing(fleet):
    f = fleet
    _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "later"})
    ksr.drain(f.w)
    f.board()
    f.evidence_down = True
    assert kox.apply_incoming(f.h) == []
    f.evidence_down = False
    assert kox.apply_incoming(f.h) and f.h.execute("SELECT 1 FROM task_comments WHERE task_id = ?",
                                                   (f.target,)).fetchone()


def test_parked_caller_is_woken_on_the_result(fleet):
    from hermes_cli.kanban_db_schedule import schedule_task
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    assert schedule_task(f.w, f.caller, reason=f"waiting on supervisor request #{out['request_id']}",
                         clock_only=True, awaiting={"kind": "clock", "why": "the supervisor request result"})
    f.board()
    ksr.drain(f.h)
    f.board()
    ksr.drain(f.w)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"
    assert kb.get_task(f.w, f.caller).status != "scheduled"
    assert f.w.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'supervisor_request_wake'",
                       (f.caller,)).fetchone()


def test_evicted_caller_copy_still_finishes(fleet):
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    f.board()
    ksr.drain(f.h)
    f.board()
    with kb.write_txn(f.w):  # a worker node evicted its copy of the caller card meanwhile
        f.w.execute("DELETE FROM task_events WHERE task_id = ?", (f.caller,))
        f.w.execute("DELETE FROM task_runs WHERE task_id = ?", (f.caller,))
        f.w.execute("DELETE FROM tasks WHERE id = ?", (f.caller,))
    ksr.drain(f.w)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done" and req["error"] == "requester_gone"


def _complete_caller(f):
    assert kb.complete_task(f.w, f.caller, summary="filed the comment", expected_run_id=f.run_id)
    assert kb.get_task(f.w, f.caller).status == "done"


def test_operator_comment_lands_after_requester_completes(fleet):
    """A comment is naturally a worker's last act: completing the card must not void the filed request."""
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "handoff: PR #7 merged"})
    _complete_caller(f)
    _round(f)
    comments = f.h.execute("SELECT body FROM task_comments WHERE task_id = ?", (f.target,)).fetchall()
    assert [c[0] for c in comments] == ["handoff: PR #7 merged"]
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done" and req["error"] is None, (req["status"], req["error"], req["result"])
    assert req["result"][0]["outcome"] == "applied"
    kinds = [e.kind for e in kb.list_events(f.w, f.caller)]
    assert "supervisor_request_done" in kinds
    assert kb.get_task(f.w, f.caller).status == "done"  # the receipt neither wakes nor reopens the card
    _round(f)  # a later round neither repeats the comment nor reopens the request
    assert f.h.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (f.target,)).fetchone()[0] == 1


def test_late_forward_after_completion_is_proven_at_submit_time(fleet):
    """The executor ran long after the caller's run ended: the home proves the run live when it was submitted."""
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "late"})
    _complete_caller(f)
    back = kox.SKEW_S * 5
    with kb.write_txn(f.w):  # submit and completion happened well before this dispatcher tick
        f.w.execute("UPDATE supervisor_requests SET created_at = created_at - ? WHERE id = ?",
                    (back, out["request_id"]))
        f.w.execute("UPDATE task_runs SET started_at = started_at - ?, ended_at = ended_at - ? WHERE id = ?",
                    (back + 10, back - 5, f.run_id))
    uid = f.w.execute("SELECT uid FROM task_runs WHERE id = ?", (f.run_id,)).fetchone()[0]
    with kb.write_txn(f.rep):  # the Board already holds the run row; its start moves back with it
        f.rep.execute("UPDATE task_runs SET started_at = started_at - ? WHERE uid = ?", (back + 10, uid))
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done", (req["error"], req["result"])
    assert f.h.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (f.target,)).fetchone()[0] == 1


def test_home_offline_fails_after_deadline(fleet):
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    later = int(time.time()) + kox.DEADLINE_S + 60
    ksr.drain(f.w, now=later)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "failed" and "did not answer" in (req["error"] or "")


def test_reconcile_poller_leaves_operator_calls_alone(fleet):
    from hermes_cli import kanban_notify_reconcile_exec as nrx
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    assert nrx.awaiting(f.w) == []
    assert [r[0] for r in kox.awaiting(f.w)] == [out["request_id"]]


def test_unsynced_board_refuses_not_synced(fleet, monkeypatch):
    f = fleet
    monkeypatch.setattr(kox, "_outbox_rows", lambda c: None)
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "rejected" and "not_synced" in req["error"]
    assert not f.w.execute("SELECT 1 FROM task_events WHERE kind = ?", (kox.REQUEST_KIND,)).fetchone()


def test_worker_context_cannot_execute(fleet, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", fleet.caller)
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        ksr.drain(fleet.h)
    assert exc.value.code == "worker_context"


def test_forged_result_neither_finishes_nor_hides_the_real_answer(fleet):
    """request_uid is public: a result another gateway wrote (or one naming the wrong executor) is ignored."""
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    f.board()
    uid = json.loads(f.w.execute("SELECT payload FROM task_events WHERE kind = ?",
                                 (kox.REQUEST_KIND,)).fetchone()[0])["request_uid"]
    forged = {"request_uid": uid, "action": "comment", "outcome": "applied", "executor_gateway": "hub"}
    wrong_exec = {**forged, "executor_gateway": "worker-9"}
    with kb.write_txn(f.rep):  # worker-9 writes both onto the hub card's replica copy
        for p in (forged, wrong_exec):
            euid = str(uuid.uuid4())
            f.rep.execute("INSERT INTO task_events (task_id, kind, payload, created_at, uid) VALUES (?,?,?,?,?)",
                          (f.target, kox.RESULT_KIND, json.dumps(p), int(time.time()), euid))
            f.origin[euid] = "worker-9"
    ksr.drain(f.w)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "accepted"  # still waiting on the real home
    ksr.drain(f.h)
    f.board()
    ksr.drain(f.w)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done" and req["result"][0]["home"] == "hub"
    assert f.h.execute("SELECT COUNT(*) FROM task_comments WHERE task_id = ?", (f.target,)).fetchone()[0] == 1


def test_result_waits_while_the_board_cannot_vouch_for_it(fleet):
    f = fleet
    out = _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"})
    ksr.drain(f.w)
    f.board()
    ksr.drain(f.h)
    f.board()
    f.evidence_down = True
    ksr.drain(f.w)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "accepted"
    f.evidence_down = False
    ksr.drain(f.w)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"


def test_repin_move_still_records_operator_provenance(fleet, monkeypatch):
    """A worker-first card moves through the Board repin, whose event has no operator fields: a marker does."""
    f = fleet
    from hermes_cli import kanban_worker_repin as repin
    monkeypatch.setattr(repin, "applies", lambda conn, tid: True)
    calls = []

    def fake_repin(conn, tid, labels, reason, *, hub=None):
        calls.append(tid)
        return {"ok": True, "runs_on": labels, "repinned": True}
    monkeypatch.setattr(repin, "repin_to_origin", fake_repin)
    out = _submit(f, {"op": "operator", "action": "move", "task_id": f.target, "runs_on": ["machine:hub"],
                      "reason": "back to the origin"})
    _round(f)
    assert ksr.get_request(f.w, out["request_id"])["status"] == "done"
    mark = f.h.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ?",
                       (f.target, kox.MOVE_MARK)).fetchone()
    payload = json.loads(mark[0])
    assert payload["operator_caller"]["task_id"] == f.caller and payload["repinned"] is True
    kox._MARK.clear()  # a replay is answered from the marker, not moved again
    with kb.write_txn(f.h):
        f.h.execute("DELETE FROM operator_calls")
        f.h.execute("DELETE FROM operator_call_scan")
    kox.apply_incoming(f.h)
    assert calls == [f.target]


def test_rebuilt_replica_is_rescanned_even_at_a_reused_position(fleet):
    """A watermark is tied to the event uid at it, so a rebuilt replica (ids restart) is rescanned."""
    f = fleet
    kox.apply_incoming(f.h)  # watermark at the current top of the replica
    last = f.h.execute("SELECT last_id FROM operator_call_scan").fetchone()[0]
    _submit(f, {"op": "operator", "action": "comment", "task_id": f.target, "text": "after rebuild"})
    ksr.drain(f.w)
    f.board()
    with kb.write_txn(f.rep):  # rebuild: ids restart, every event gets a new row; the request now sits at id 1
        rows = f.rep.execute("SELECT task_id, kind, payload, created_at, uid FROM task_events "
                             "ORDER BY (kind = ?) DESC, id", (kox.REQUEST_KIND,)).fetchall()
        f.rep.execute("DELETE FROM task_events")
        f.rep.execute("DELETE FROM sqlite_sequence WHERE name = 'task_events'")
        for r in rows:
            f.rep.execute("INSERT INTO task_events (task_id, kind, payload, created_at, uid) VALUES (?,?,?,?,?)",
                          (r[0], r[1], r[2], r[3], r[4] + "-rebuilt"))
            f.origin[r[4] + "-rebuilt"] = f.origin.get(r[4])
            f.canonical[r[4] + "-rebuilt"] = {**f.canonical[r[4]], "uid": r[4] + "-rebuilt"}
    top = f.rep.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
    assert top >= last  # the old watermark position still exists, holding a different event
    kox.apply_incoming(f.h)
    assert f.h.execute("SELECT 1 FROM task_comments WHERE task_id = ?", (f.target,)).fetchone()


def test_operator_path_never_touches_an_admin_key(fleet, tmp_path, monkeypatch):
    """Behaviour: with an admin key on disk, a full comment, move and unblock round never opens it or shells out
    to a Board admin command, on either side."""
    import builtins
    import io
    import os
    import subprocess
    f = fleet
    key = tmp_path / ".config" / "hermes-mail" / "admin.key"
    key.parent.mkdir(parents=True)
    key.write_text("secret")
    opened, spawned = [], []
    real_open, real_os_open, real_popen = builtins.open, os.open, subprocess.Popen

    def spy_open(file, *a, **k):
        opened.append(str(file))
        return real_open(file, *a, **k)

    def spy_os_open(path, *a, **k):
        opened.append(str(path))
        return real_os_open(path, *a, **k)

    class SpyPopen(real_popen):
        def __init__(self, args, *a, **k):
            spawned.append(args)
            super().__init__(args, *a, **k)
    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(subprocess, "Popen", SpyPopen)
    plan = {"runs_on": ["machine:worker-1"], "previous": [], "matches": ["worker-1"], "move_no": 1,
            "forced": False, "me": "hub", "host": {}}
    monkeypatch.setattr(km, "check", lambda *a, **k: plan)
    from hermes_cli import kanban_worker_node as wn
    from hermes_cli import kanban_worker_repin as repin
    monkeypatch.setattr(repin, "applies", lambda conn, tid: False)
    monkeypatch.setattr(wn, "set_runs_on", lambda conn, tid, labels: None)
    blocked = f.card(f.h, "agent block")
    kb.block_task(f.h, blocked, reason="waiting", actor=f"agent (task {blocked})", kind="transient")
    f.board()
    ids = [_submit(f, op)["request_id"] for op in (
        {"op": "operator", "action": "comment", "task_id": f.target, "text": "hi"},
        {"op": "operator", "action": "move", "task_id": f.target, "runs_on": ["machine:worker-1"], "reason": "gpu"},
        {"op": "operator", "action": "unblock", "task_id": blocked, "reason": "green"})]
    _round(f)
    assert [ksr.get_request(f.w, i)["status"] for i in ids] == ["done", "done", "done"]
    assert not [p for p in opened if "admin" in p], opened
    assert not [a for a in spawned if "admin" in json.dumps(a, default=str)], spawned
