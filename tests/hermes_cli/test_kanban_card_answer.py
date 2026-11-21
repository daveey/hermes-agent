"""Owner-side exact ask transitions, with a real disposable core board. [daveey]"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_awaiting as helpers

kanban_home = helpers.kanban_home


def setup_answer(conn, *, choices=True):
    tid = helpers._human_blocked(conn)
    if choices:
        with kb.write_txn(conn):
            hr = json.loads(conn.execute("SELECT human_request FROM tasks WHERE id=?", (tid,)).fetchone()[0])
            hr["choices"] = [{"label": "Apply plan A", "value": "plan-a"}, {"label": "Apply plan B", "value": "plan-b"}]
            conn.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps(hr), tid))
    return {"ownerGateway": "owner", "ownerProfile": "default", "board": "default", "task": tid,
            "generation": ca.snapshot(conn, tid)["generation"],
            "choice": "plan-a" if choices else "unblock", "operationId": "desktop-operation-one"}


def verified(request):
    return {"gateway": "origin", "recorded": True, "answer": request}


def apply(conn, request, verify=verified):
    return ca.apply(conn, request, owner_gateway="owner", owner_profile="default", board="default",
                    verify_source=verify)


def comments(conn, tid):
    return [tuple(r) for r in conn.execute("SELECT author,body FROM task_comments WHERE task_id=?", (tid,))]


def test_atomic_answer_and_resume_with_readback(kanban_home):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        before = comments(conn, req["task"])
        receipt = apply(conn, req)
        task = kb.get_task(conn, req["task"])
        assert task.status == "ready" and task.human_request is None
        assert comments(conn, req["task"])[len(before):] == [(
            "user-steps", 'Choice recorded in Desktop (remote): {"label":"Apply plan A","value":"plan-a"}\nOperation: desktop-operation-one')]
        event = [e for e in kb.list_events(conn, req["task"]) if e.kind == "unblocked"][-1]
        assert event.payload[ca.FIELD] == receipt
        assert receipt["operationId"] == req["operationId"] and receipt["choice"] == "plan-a"


def test_exact_replay_after_commit_without_origin(kanban_home):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        first = apply(conn, req)
        before = (comments(conn, req["task"]), len(kb.list_events(conn, req["task"])))
        def offline(_):
            pytest.fail("committed operation must not require origin online")
        assert apply(conn, req, offline) == first
        assert before == (comments(conn, req["task"]), len(kb.list_events(conn, req["task"])))
        with pytest.raises(ca.AnswerRefused, match="operation_conflict"):
            apply(conn, dict(req, choice="plan-b"))


@pytest.mark.parametrize("change", ["ask", "cycle"])
def test_stale_generation_never_comments_or_clears_hold(kanban_home, change):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        with kb.write_txn(conn):
            if change == "ask":
                conn.execute("UPDATE tasks SET human_request=json_set(human_request,'$.action','A new ask') WHERE id=?", (req["task"],))
            else:
                kb._append_event(conn, req["task"], "blocked", {"reason": "same ask, new block"})
        before = (comments(conn, req["task"]), len(kb.list_events(conn, req["task"])))
        with pytest.raises(ca.AnswerRefused, match="stale_ask"):
            apply(conn, req)
        assert kb.get_task(conn, req["task"]).status == "blocked"
        assert kb.get_task(conn, req["task"]).human_request
        assert before == (comments(conn, req["task"]), len(kb.list_events(conn, req["task"])))


@pytest.mark.parametrize("proof", [None, True, {}, {"recorded": False}, {"recorded": True, "answer": {}}])
def test_bare_gateway_or_caller_claim_does_not_clear_human_hold(kanban_home, proof):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        before = comments(conn, req["task"])
        with pytest.raises(ca.AnswerRefused, match="unverified_user_decision"):
            apply(conn, req, lambda _: proof)
        assert kb.get_task(conn, req["task"]).status == "blocked"
        assert kb.get_task(conn, req["task"]).human_request
        assert comments(conn, req["task"]) == before


def test_fence_rechecks_after_source_read(kanban_home):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        def source(request):
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET human_request=json_set(human_request,'$.action','new question') WHERE id=?", (req["task"],))
            return verified(request)
        with pytest.raises(ca.AnswerRefused, match="stale_ask"):
            apply(conn, req, source)
        assert not any("Choice recorded" in body for _, body in comments(conn, req["task"]))


def test_transition_refusal_rolls_back_choice_comment(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        before = comments(conn, req["task"])
        monkeypatch.setattr(kb, "_landing_status_after_parents", lambda *_: (_ for _ in ()).throw(RuntimeError("commit failed")))
        with pytest.raises(RuntimeError, match="commit failed"):
            apply(conn, req)
        assert comments(conn, req["task"]) == before
        assert kb.get_task(conn, req["task"]).status == "blocked"
        assert not any(e.kind == "unblocked" for e in kb.list_events(conn, req["task"]))


def test_archive_default_choice_uses_archive_transition(kanban_home):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn, choices=False)
        req["choice"] = "archive"
        receipt = apply(conn, req)
        assert kb.get_task(conn, req["task"]).status == "archived"
        assert apply(conn, req) == receipt


def test_under_lock_fence_rechecks_after_preflight(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        original = kb.unblock_task
        def raced(c, task, **kwargs):
            with kb.write_txn(c):
                c.execute("UPDATE tasks SET human_request=json_set(human_request,'$.action','new under-lock ask') WHERE id=?", (task,))
            return original(c, task, **kwargs)
        monkeypatch.setattr(kb, "unblock_task", raced)
        with pytest.raises(ca.AnswerRefused, match="ask_changed_or_held"):
            apply(conn, req)
        assert kb.get_task(conn, req["task"]).status == "blocked"
        assert not any("Choice recorded" in body for _, body in comments(conn, req["task"]))


def test_terminal_hold_is_not_cleared_by_exact_answer(kanban_home):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, req["task"], "completed", {"summary": "terminal history precedes stale blocked row"})
        req["generation"] = ca.snapshot(conn, req["task"])["generation"]
        before = comments(conn, req["task"])
        with pytest.raises(ca.AnswerRefused, match="ask_changed_or_held"):
            apply(conn, req)
        assert comments(conn, req["task"]) == before
        assert kb.get_task(conn, req["task"]).human_request
        assert not any(e.kind == "unblocked" for e in kb.list_events(conn, req["task"]))


@pytest.mark.parametrize("field", ["ownerGateway", "ownerProfile", "board"])
def test_wrong_namespace_is_refused_before_source_read(kanban_home, field):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        def not_called(_):
            pytest.fail("wrong owner namespace must not request authority")
        with pytest.raises(ca.AnswerRefused, match="invalid_answer"):
            apply(conn, dict(req, **{field: "other"}), not_called)
        assert kb.get_task(conn, req["task"]).status == "blocked"


def test_unknown_choice_refused(kanban_home):
    with kbc.connect_closing() as conn:
        req = setup_answer(conn)
        with pytest.raises(ca.AnswerRefused, match="unknown_choice"):
            apply(conn, dict(req, choice="invented"))
        assert kb.get_task(conn, req["task"]).status == "blocked"
