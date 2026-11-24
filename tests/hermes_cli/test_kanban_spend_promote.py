"""Spend-stop promotion through the operator courier: the stop's named decision card sets a finite window. [daveey]

The cumulative spend budget stops a card in triage and names one supervisor card as its decision owner. That card's
worker may not use the board CLI, and an unblock is not a decision. ``spend_promote`` lets exactly that caller, with
a recorded decision comment, promote the card with a budget of exactly ``runs`` more runs, in one home transaction.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_operator_call as kox
from hermes_cli import kanban_spend_budget as ksb
from hermes_cli import kanban_spend_decision as ksd
from hermes_cli import kanban_spend_promote as ksp
from hermes_cli import kanban_supervisor_requests as ksr
from tests.hermes_cli import test_kanban_operator_call as base

_log, _round, _submit = base._log, base._round, base._submit
fleet = base.fleet  # the two-gateway fixture, shared


def _stop(f, target=None, *, producer_task=None, at=None):
    """The home's spend budget stopped ``target`` (15 runs) and named ``producer_task`` its decision card."""
    target = target or f.target
    at = int(time.time()) - 1000 if at is None else at
    with kb.write_txn(f.h):
        for n in range(15):  # the spent window: runs before the stop never count again
            f.h.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                        "VALUES (?, 'default', 'done', 'released', ?, ?)", (target, at - 5000 + n, at - 4000 + n))
        f.h.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (target,))
        kb._append_event(f.h, target, ksd.EVENT, {"limit": "runs", "runs": 15, "reason": "Spend budget: 15 runs"})
        eid = f.h.execute("SELECT MAX(id) FROM task_events WHERE task_id = ? AND kind = ?",
                          (target, ksd.EVENT)).fetchone()[0]
        f.h.execute("UPDATE task_events SET created_at = ? WHERE id = ?", (at, eid))
    f.board()  # stamps the stop's Board uid: the episode
    episode = f.h.execute("SELECT uid FROM task_events WHERE id = ?", (eid,)).fetchone()[0]
    with kb.write_txn(f.h):
        kb._append_event(f.h, target, ksd.NOTICE, {
            "episode": episode, "producer": {"kind": "supervisor", "task_id": producer_task or f.caller}})
    f.board()
    return episode


def _decision_comment(f, target=None) -> int:
    target = target or f.target
    _submit(f, {"op": "operator", "action": "comment", "task_id": target, "text": "Decision: re-scope, 4 runs."})
    _round(f)
    row = f.h.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
                      (target, kox.COMMENT_MARK)).fetchone()
    return int(json.loads(row[0])["comment_id"])


def _op(f, episode, cid, **kw):
    return {"op": "operator", "action": "spend_promote", "task_id": f.target, "runs": 4, "episode": episode,
            "decision_comment_id": cid, "reason": "re-scoped to bounded landing", **kw}


def _events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _add_runs(conn, tid, n, start):
    with kb.write_txn(conn):
        for i in range(n):
            conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) "
                         "VALUES (?, 'default', 'done', 'released', ?, ?)", (tid, start + i, start + i + 100))


def test_decision_card_promotes_with_an_exact_four_run_window(fleet):
    f = fleet
    with kb.write_txn(f.h):  # the card's own cache-read override survives the new run budget
        kb._append_event(f.h, f.target, ksb.OVERRIDE_EVENT, {"runs": None, "cache_read_tokens": 5_000_000,
                                                             "by": "user", "reason": "big context"})
    episode = _stop(f)
    cid = _decision_comment(f)
    out = _submit(f, _op(f, episode, cid))
    assert out["ops"][0]["verdict"] == "supervisor", out
    _round(f)
    req = ksr.get_request(f.w, out["request_id"])
    assert req["status"] == "done", (req["error"], req["result"])
    assert kb.get_task(f.h, f.target).status == "ready"
    budget = _events(f.h, f.target, ksb.OVERRIDE_EVENT)[-1]
    assert (budget["runs"], budget["cache_read_tokens"], budget["episode"]) == (4, 5_000_000, episode)
    # The decision card's worker is an agent; the cache-read limit it carried over was never attributed.
    assert (budget["writer_kind"], budget["cache_read_writer_kind"]) == ("agent", "unknown")
    promoted = _events(f.h, f.target, "promoted_manual")
    assert len(promoted) == 1
    p = promoted[0]
    assert p["spend_budget"] == {"runs": 4, "episode": episode, "decision_comment_id": cid}
    assert p["from_status"] == "triage" and p["operator_caller"]["task_id"] == f.caller
    assert p["operator_request_uid"] == budget["operator_request_uid"]
    assert not ksd.pending(f.h, f.target)
    assert ksb.card_limits(f.h, f.target, ksb.Limits(15, 30_000_000)) == ksb.Limits(4, 5_000_000)
    promoted_at = f.h.execute("SELECT created_at FROM task_events WHERE task_id = ? AND kind = 'promoted_manual'",
                              (f.target,)).fetchone()[0]
    # The new window opens at the promotion: the 15 spent runs are gone, exactly 4 more are allowed.
    assert ksb.spend(f.h, f.target, as_of=promoted_at + 1).since == promoted_at
    lim = ksb.Limits(15, 0)
    _add_runs(f.h, f.target, 3, promoted_at)
    assert ksb.hold(f.h, f.target, now=promoted_at + 500, lim=lim) is None
    _add_runs(f.h, f.target, 1, promoted_at + 10)
    assert ksb.hold(f.h, f.target, now=promoted_at + 500, lim=lim) == "triage"


def test_plain_manual_promote_does_not_reset_the_spend_window(fleet):
    f = fleet
    stop_at = int(time.time()) - 1000
    _stop(f, at=stop_at)
    with kb.write_txn(f.h):
        kb._append_event(f.h, f.target, "promoted_manual", {"actor": "user", "reason": "x"})
    assert ksb.spend(f.h, f.target).since == stop_at


def test_unauthorized_caller_is_refused(fleet):
    f = fleet
    episode = _stop(f, producer_task="t_someone_else")
    cid = _decision_comment(f)
    out = _submit(f, _op(f, episode, cid), dry_run=True)
    assert out["ops"][0]["code"] == "spend_scope" and "not " + f.caller in out["ops"][0]["reason"], out
    # A chat-owned stop names no supervisor: nobody may use this path.
    with kb.write_txn(f.h):
        kb._append_event(f.h, f.target, ksd.NOTICE, {"episode": episode, "producer": {"kind": "chats"}})
    f.board()
    assert ksp.scope_refusal(f.h, f.target, f.caller, episode, cid)


def test_home_rechecks_scope_and_refuses_a_superseded_episode(fleet):
    f = fleet
    episode = _stop(f)
    cid = _decision_comment(f)
    _submit(f, _op(f, episode, cid))
    ksr.drain(f.w)  # forwarded; meanwhile the home stopped the card again (a new episode)
    with kb.write_txn(f.h):
        kb._append_event(f.h, f.target, ksd.EVENT, {"limit": "runs"})
    f.board()
    kox.apply_incoming(f.h)
    row = next(r for r in _log(f.h, "execute") if r["action"] == "spend_promote")
    assert row["outcome"] == "refused" and "not" in row["detail"] and "current spend stop" in row["detail"]
    assert kb.get_task(f.h, f.target).status == "triage"
    assert not _events(f.h, f.target, "promoted_manual") and not _events(f.h, f.target, ksb.OVERRIDE_EVENT)


def test_stale_caller_run_is_refused_at_the_home(fleet):
    f = fleet
    episode = _stop(f)
    cid = _decision_comment(f)
    _submit(f, _op(f, episode, cid))
    ksr.drain(f.w)
    f.board()
    uid = f.w.execute("SELECT uid FROM task_runs WHERE id = ?", (f.run_id,)).fetchone()[0]
    with kb.write_txn(f.rep):  # the Board says that run had ended long before the call
        f.rep.execute("UPDATE task_runs SET started_at = 1, ended_at = 2 WHERE uid = ?", (uid,))
    kox.apply_incoming(f.h)
    row = next(r for r in _log(f.h, "execute") if r["action"] == "spend_promote")
    assert row["outcome"] == "refused" and "caller_unproven" in row["detail"]
    assert kb.get_task(f.h, f.target).status == "triage"


def test_foreign_home_routing(fleet):
    f = fleet
    local = f.card(f.w, "a worker-5 card")
    f.board()
    out = _submit(f, {**_op(f, "e", 1), "task_id": local}, dry_run=True)
    assert out["ops"][0]["code"] == "not_foreign", out
    episode = _stop(f)
    cid = _decision_comment(f)
    _submit(f, _op(f, episode, cid))
    ksr.drain(f.w)
    f.board()
    # A gateway that does not own the target never executes it, even though it sees the request.
    real = kox._own_gateway
    kox._own_gateway = lambda conn: "worker-9" if conn is f.h else real(conn)
    try:
        assert kox.apply_incoming(f.h) == []
    finally:
        kox._own_gateway = real
    assert kb.get_task(f.h, f.target).status == "triage"
    kox._MARK.clear()
    with kb.write_txn(f.h):
        f.h.execute("DELETE FROM operator_call_scan")
    assert [o for _u, o in kox.apply_incoming(f.h)] == ["applied"]


def test_duplicate_request_applies_once(fleet):
    f = fleet
    episode = _stop(f)
    cid = _decision_comment(f)
    _submit(f, _op(f, episode, cid))
    ksr.drain(f.w)
    f.board()
    kox.apply_incoming(f.h)
    with kb.write_txn(f.h):  # crash after applying: log and watermark lost, the card's marker answers
        f.h.execute("DELETE FROM operator_calls")
        f.h.execute("DELETE FROM operator_call_scan")
    kox._MARK.clear()
    kox.apply_incoming(f.h)
    assert len(_events(f.h, f.target, "promoted_manual")) == 1
    assert len(_events(f.h, f.target, ksb.OVERRIDE_EVENT)) == 1
    assert _log(f.h, "execute")[0]["outcome"] == "already_applied"
    # A second, new request for the same decision finds no pending stop.
    f.board()
    out = _submit(f, _op(f, episode, cid), dry_run=True)
    assert out["ops"][0]["code"] == "spend_scope", out


def test_human_hold_is_refused_at_request_and_home(fleet):
    f = fleet
    episode = _stop(f)
    cid = _decision_comment(f)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET human_request = ? WHERE id = ?", (json.dumps({"action": "pick"}), f.target))
    f.board()
    out = _submit(f, _op(f, episode, cid), dry_run=True)
    assert out["ops"][0]["code"] == "spend_scope" and "human" in out["ops"][0]["reason"], out
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET human_request = NULL WHERE id = ?", (f.target,))
    f.board()
    _submit(f, _op(f, episode, cid))
    ksr.drain(f.w)
    f.board()
    with kb.write_txn(f.h):  # the human hold arrives after the caller's preflight
        f.h.execute("UPDATE tasks SET block_kind = 'needs_input' WHERE id = ?", (f.target,))
    kox.apply_incoming(f.h)
    row = next(r for r in _log(f.h, "execute") if r["action"] == "spend_promote")
    assert row["outcome"] == "refused" and "human" in row["detail"]
    assert kb.get_task(f.h, f.target).status == "triage"


def test_unblock_never_masquerades_as_a_promotion(fleet):
    f = fleet
    _stop(f)
    out = _submit(f, {"op": "operator", "action": "unblock", "task_id": f.target, "reason": "go"}, dry_run=True)
    assert out["ops"][0]["code"] == "not_blocked", out
    assert not _events(f.h, f.target, "promoted_manual")
    assert ksd.pending(f.h, f.target)


def test_decision_comment_must_be_the_callers_after_the_stop(fleet):
    f = fleet
    cid = _decision_comment(f)  # recorded BEFORE the stop
    episode = _stop(f, at=int(time.time()) + 5)
    assert "decision comment" in ksp.scope_refusal(f.h, f.target, f.caller, episode, cid)
    later = _decision_comment(f)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE task_events SET created_at = ? WHERE kind = ? AND json_extract(payload, "
                    "'$.comment_id') = ?", (int(time.time()) + 10, kox.COMMENT_MARK, later))
    assert ksp.scope_refusal(f.h, f.target, f.caller, episode, later) is None
    assert "decision comment" in ksp.scope_refusal(f.h, f.target, f.caller, episode, later + 999)
    assert "decision card" in ksp.scope_refusal(f.h, f.target, f.target, episode, later)


@pytest.mark.parametrize("bad", [{"runs": 0}, {"runs": 21}, {"runs": True}, {"runs": "x"}, {"episode": ""},
                                 {"decision_comment_id": None}, {"decision_comment_id": 0}, {"reason": " "},
                                 {"exempt": True}, {"task_id": ""}, {"runs": "²"}])
def test_invalid_spend_promote_ops_are_refused(fleet, bad):
    with pytest.raises(ksr.SupervisorRequestError) as exc:
        _submit(fleet, {**_op(fleet, "e", 1), **bad})
    assert exc.value.code == "invalid_ops"


def test_tool_schema_exposes_spend_promote():
    from tools import kanban_tools_schemas as schemas
    text = json.dumps({k: getattr(schemas, k) for k in dir(schemas) if not k.startswith("_")}, default=str)
    assert "spend_promote" in text and "decision_comment_id" in text


def _mark(f, cid, *, caller=None, at=None):
    """An operator comment marker on the target, as the home writes it for ``caller``'s comment ``cid``."""
    with kb.write_txn(f.h):
        kb._append_event(f.h, f.target, kox.COMMENT_MARK, {"comment_id": cid, "operator_request_uid": f"m{cid}",
                                                          "operator_caller": {"task_id": caller or f.caller}})
        if at is not None:
            f.h.execute("UPDATE task_events SET created_at = ? WHERE id = (SELECT MAX(id) FROM task_events)", (at,))


def _direct(f, episode, cid, uid="req-direct", **args):
    return {"target_task_id": f.target, "request_uid": uid, "caller": {"gateway": f.name(f.w), "task_id": f.caller},
            "args": {"runs": 4, "episode": episode, "decision_comment_id": cid, "reason": "re-scoped", **args}}


def _untouched(f):
    assert kb.get_task(f.h, f.target).status == "triage"
    assert not _events(f.h, f.target, "promoted_manual") and not _events(f.h, f.target, ksb.OVERRIDE_EVENT)


def test_target_must_still_be_triaged_and_pending(fleet):
    f = fleet
    episode = _stop(f)
    _mark(f, 701)
    assert ksp.scope_refusal(f.h, f.target, f.caller, episode, 701) is None
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (f.target,))
    assert "not held in triage" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 701)
    with kb.write_txn(f.h):  # promoted by someone else, then triaged for another reason: no pending decision
        kb._append_event(f.h, f.target, "promoted_manual", {"actor": "user", "reason": "x"})
        f.h.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (f.target,))
    assert "no pending spend-budget decision" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 701)


def test_producer_receipt_must_name_this_supervisor_for_this_episode(fleet):
    f = fleet
    episode = _stop(f, producer_task="t_nobody")
    _mark(f, 702)
    with kb.write_txn(f.h):  # strip the receipt: none for this episode
        f.h.execute("DELETE FROM task_events WHERE task_id = ? AND kind = ?", (f.target, ksd.NOTICE))
    assert "no decision producer receipt" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 702)
    with kb.write_txn(f.h):  # a receipt naming the caller, but for another episode
        kb._append_event(f.h, f.target, ksd.NOTICE, {"episode": "other",
                                                     "producer": {"kind": "supervisor", "task_id": f.caller}})
    assert "no decision producer receipt" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 702)
    with kb.write_txn(f.h):  # this episode, the caller's id, but a chat owns the decision
        kb._append_event(f.h, f.target, ksd.NOTICE, {"episode": episode,
                                                     "producer": {"kind": "chats", "task_id": f.caller}})
    assert "names its decision owner" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 702)
    with kb.write_txn(f.h):
        kb._append_event(f.h, f.target, ksd.NOTICE, {"episode": episode,
                                                     "producer": {"kind": "supervisor", "task_id": f.caller}})
    assert ksp.scope_refusal(f.h, f.target, f.caller, episode, 702) is None


def test_decision_comment_must_be_the_callers_own_and_strictly_after_the_stop(fleet):
    f = fleet
    at = int(time.time()) - 1000
    _mark(f, 703, at=at)  # same second as the stop, but written before it
    episode = _stop(f, at=at)
    assert "decision comment" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 703)
    _mark(f, 704, caller="t_other_supervisor")
    assert "decision comment" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 704)
    _mark(f, 705)
    assert ksp.scope_refusal(f.h, f.target, f.caller, episode, 705) is None


def test_human_comment_after_the_decision_needs_a_new_decision(fleet):
    f = fleet
    episode = _stop(f)
    _mark(f, 706)
    with kb.write_txn(f.h):
        kb._append_event(f.h, f.target, "commented", {"writer_kind": "human"})
    assert "a human commented" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 706)
    _mark(f, 707)
    assert ksp.scope_refusal(f.h, f.target, f.caller, episode, 707) is None
    with kb.write_txn(f.h):
        kb._append_event(f.h, f.target, "commented", {"relayed_human": True})
    assert "a human commented" in ksp.scope_refusal(f.h, f.target, f.caller, episode, 707)


def test_home_apply_bounds_runs_and_reason(fleet):
    f = fleet
    episode = _stop(f)
    _mark(f, 708)
    for bad in ({"runs": 0}, {"runs": ksp.MAX_RUNS + 1}, {"runs": "²"}, {"reason": " "}):
        assert ksp.apply(f.h, _direct(f, episode, 708, **bad), {})[0] == "refused", bad
    _untouched(f)
    extra = {"operator_request_uid": "req-direct"}
    assert ksp.apply(f.h, _direct(f, episode, 708, runs="4"), extra)[0] == "applied"
    assert _events(f.h, f.target, ksb.OVERRIDE_EVENT)[-1]["runs"] == 4
    # A replay after the card finished still answers already_applied, not a terminal refusal.
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (f.target,))
    assert kox._apply(f.h, {**_direct(f, episode, 708), "action": "spend_promote"}, "x")[0] == "already_applied"


def test_open_parent_is_refused_at_home(fleet):
    f = fleet
    episode = _stop(f)
    _mark(f, 709)
    parent = f.card(f.h, "an open parent")
    with kb.write_txn(f.h):
        f.h.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, f.target))
    outcome, why = ksp.apply(f.h, _direct(f, episode, 709), {"operator_request_uid": "req-direct"})
    assert outcome == "refused" and parent in why
    _untouched(f)
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    assert ksp.apply(f.h, _direct(f, episode, 709), {"operator_request_uid": "req-direct"})[0] == "applied"


def test_promotion_is_all_or_nothing(fleet, monkeypatch):
    f = fleet
    episode = _stop(f)
    _mark(f, 710)
    monkeypatch.setattr(ksp, "scope_refusal", lambda *a: None)  # scope passed, then the card left triage
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (f.target,))
    with pytest.raises(RuntimeError):
        ksp.apply(f.h, _direct(f, episode, 710), {"operator_request_uid": "req-direct"})
    assert not _events(f.h, f.target, "promoted_manual") and not _events(f.h, f.target, ksb.OVERRIDE_EVENT)
