"""The origin learns from the owner's own courier answer whether the owner wrote its chat's ``created`` link. [daveey]

PR #2079 review: the origin's ``chat_link: owner_written`` note was decided from the chat id's shape, untrue for an
owner on older core or with its Board sync off, and too cautious for a ``local-<sha256>`` chat an ancestor proves.
``await_task_id(result_out=...)`` hands the owner's result to ``owner_chat_link``, which reads only the owner's
``origin_chat_link`` field.
"""
import json

from hermes_cli import kanban_placement as kp

RID = "placement:macbook:1866c148153415f35121c325"
WRITTEN = {"ok": False, "created": True, "code": "origin_bind_pending", "task_id": "t_01a1d42acdce8e23",
           "status": "ready", "origin_chat_link": "owner_written"}


def _outcomes(result):
    return json.dumps({"id": RID, "state": "queued", "result": None, "remote_state": "waiting",
                       "remote_result": result})


def test_the_owners_answer_from_hub_outcomes_reaches_the_caller(monkeypatch):
    monkeypatch.setattr(kp, "remote_task_id", lambda rid: None)
    monkeypatch.setattr(kp, "hub_has_outcomes", lambda hub_cli: True)
    monkeypatch.setattr(kp, "hub_runner", lambda argv: (0, _outcomes(WRITTEN), ""))
    seen: dict = {}
    assert kp.await_task_id(RID, hub_cli="hermes-hub", budget=5, result_out=seen) == ("t_01a1d42acdce8e23", "ready")
    assert kp.owner_chat_link(seen["result"]) == {"chat_link": "owner_written"}


def test_the_owners_answer_from_the_done_replica_row_reaches_the_caller(monkeypatch):
    done = {**WRITTEN, "ok": True, "code": "origin_bound", "handoff_complete": True}
    monkeypatch.setattr(kp, "_sent_row", lambda rid: ("done", json.dumps(done)))
    seen: dict = {}
    assert kp.await_task_id(RID, hub_cli="hermes-hub", budget=0, result_out=seen) == ("t_01a1d42acdce8e23", "ready")
    assert kp.owner_chat_link(seen["result"]) == {"chat_link": "owner_written"}


def test_an_owner_answer_without_the_field_claims_nothing():
    older = {k: v for k, v in WRITTEN.items() if k != "origin_chat_link"}  # core before _link_origin
    assert kp.owner_chat_link(older) == {}
    assert kp.owner_chat_link(None) == {}  # no answer yet (a queued request)
    assert kp.owner_chat_link({"origin_chat_link": "yes"}) == {}


def test_await_task_id_without_result_out_is_unchanged(monkeypatch):
    monkeypatch.setattr(kp, "_sent_row", lambda rid: ("done", json.dumps(WRITTEN)))
    assert kp.await_task_id(RID, hub_cli="hermes-hub", budget=0) == ("t_01a1d42acdce8e23", "ready")
