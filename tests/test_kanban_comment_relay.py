"""A comment-relay card is denied hub admission with the worker route, and no model is asked. [daveey]

The four cards are the hub-pin sweep's ``foreign-comment-relay`` evidence (titles verbatim, bodies trimmed to the
lines that made them relays). On the parent the judge's model is asked and, here, admits every one of them.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import importlib
import json
from types import SimpleNamespace

import pytest

RELAYS = [
    ("Post two correction comments on hub-owned cards (ship never needs hub custody; real owner of "
     "kanban-04bdba70ac)",
     "This card only posts comments. It needs the hub because both target cards are hub-owned, and this card's "
     "creator, hermes-worker-3, is refused with `foreign_card` and holds only replica copies. Post each comment "
     "verbatim with kanban_comment, then complete.\n\n1. On **t_01a1cdfe0d81e0f0** ...\n2. On "
     "**t_01a1cce501c36450** ..."),
    ("Post PR #1251 landing note on runners/t_01a1cdc7df9e82dd",
     "Pool worker t_01a1ce87aadbea4d is pinned to board default and cannot write to the runners board: "
     "kanban_comment returns board_pinned, and cross_task is a read-only replica. This card must run on the hub "
     "because the hub can write to the runners board.\n\nAction: post this comment verbatim on task "
     "t_01a1cdc7df9e82dd, board `runners`."),
    ("Post five upkeep script-result comments on hub-owned target cards",
     "Hub-local write needed: the five target cards below were created on hub. Worker-1's replica refuses "
     "comments (`foreign_card` / `read_only_transport`), so only the hub can post them.\n\nFor each section, call "
     "`kanban_comment(task_id=<heading id>, body=<section text verbatim>)`.\n\n## t_01a1cb8c76be80bb\n..."),
    ("Post final chat-closeout fleet evidence to hub-owned gate card",
     "Owner-only comment write required: worker-1 kanban_comment rejects foreign_card and cross_task rejects "
     "read_only_transport. Post the following final result to hub-owned t_01a1ca65e8da5217 and read it back."),
]


@pytest.fixture
def judge(tmp_path, monkeypatch):
    """The real admit() with a model that admits everything (the old outcome for these cards)."""
    gate = importlib.import_module("hermes_cli.kanban_hub_admission")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_WORKER_TASK", raising=False)
    skill = tmp_path / "skills/devops/hub-admission-test/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: hub-admission-test\n---\nJudge the card.")
    from agent import auxiliary_client as aux
    from agent import model_routing_managed as managed
    monkeypatch.setattr(managed, "managed_config", lambda: {"managed": True})
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        card = json.loads(kwargs["messages"][1]["content"])
        kwargs["route_info"].update(provider="anthropic", model="claude-sonnet-5-5", billing_group="subscription")
        reply = {"admit": True, "need": "hub_live_process", "evidence": card["title"], "why": "admits all"}
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(reply)))])

    monkeypatch.setattr(aux, "call_llm", fake)
    return gate, calls


@pytest.mark.parametrize("title,body", RELAYS)
def test_comment_relay_is_denied_without_a_model_call(judge, title, body):
    gate, calls = judge
    verdict = gate.admit({"title": title, "body": body}, hub_reason="only the hub can post them")
    assert verdict.admit is False
    assert verdict.need is None
    assert verdict.why.startswith("comment relay:")
    assert "kanban_supervisor_request" in verdict.why and "hub-comment" in verdict.why
    assert calls == []


@pytest.mark.parametrize("title,body", [
    ("Add tests for the comment parser", "Cover t_01a1cdfe0d81e0f0's comment edge cases and push."),
    ("Post the billing note on t_01a1cdfe0d81e0f0",
     "Read it in the hub's signed-in Chrome first, then post the comment verbatim on the hub-owned card."),
    ("Fix kanban_comment foreign_card refusal text", "The replica refuses comments on t_01a1cdfe0d81e0f0."),
    # Allowlist gateway_key_cli: the hub gateway's own key for a hermes-hub subcommand, then a comment.
    ("Write repair results onto t_01a1cf4778d6d1b1",
     "Run `hermes-hub replica repair` with the hub gateway's own key (only the hub holds it), then kanban_comment "
     "the receipt on t_01a1cf4778d6d1b1."),
    # Allowlist hub_live_process: in-process hub gateway state, then a comment.
    ("Post live dispatcher evidence on t_01a1cf4778d6d1b1",
     "Dump the in-process dispatcher queue of the hub gateway (only the hub has it) and post it as a comment "
     "verbatim on t_01a1cf4778d6d1b1."),
    # No explicit target card for the comment: not a relay.
    ("Add replica notes to sync docs",
     "Document how the replica handles comments; see t_01a1cf4778d6d1b1 for background."),
])
def test_other_cards_still_go_to_the_judge(judge, title, body):
    gate, calls = judge
    assert gate.admit({"title": title, "body": body}).admit is True
    assert len(calls) == 1


@pytest.mark.parametrize("title,body", RELAYS)
def test_worker_machine_hub_pin_for_a_relay_is_refused_with_the_route(judge, monkeypatch, title, body):
    """kanban_create on a worker with ``runs_on=['machine:hub']`` (how all four were filed) is refused."""
    from hermes_cli import kanban_hub_admission_placement as hap
    _, calls = judge
    cfg = {"worker_pools": {"hub": ["worker-1"]}}
    args = {"title": title, "body": body, "runs_on": ["machine:hub"], "requires": ["kanban"]}
    out = hap.apply(args, {"where": "local", "why": "explicit runs_on"}, "worker-1", cfg)
    assert out["where"] == "refuse"
    assert out["why"].startswith("comment relay:")
    assert out["hub_admission"]["admit"] is False
    assert calls == []


def test_move_back_to_the_hub_for_a_relay_is_refused(judge, tmp_path):
    """worker-3 moved t_01a1cf3e8deb41b4 back to machine:hub ('Only the hub can post them'): gate_move refuses."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_hub_admission_placement as hap
    from hermes_cli.kanban_db_connect import connect_closing
    _, calls = judge
    title, body = RELAYS[2]
    with connect_closing(tmp_path / "kanban.db") as conn:
        tid = kb.create_task(conn, title=title, body=body, assignee="default")
        out = hap.gate_move(conn, tid, ["machine:hub"], cfg={})
        assert out is not None and out["reason_code"] == "hub_admission_denied"
        assert "comment relay:" in out["error"]
        latest = hap.latest_verdict(conn, tid) or {}
        assert latest["why"].startswith("comment relay:")
    assert calls == []
