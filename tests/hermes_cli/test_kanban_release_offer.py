"""A Release button that works for human-only releases (``kanban_release_offer``). [daveey]

Real SQLite boards. Only the Board boundary is stubbed: the authenticated allowance read (``board_state``) and the
canonical renewal that retires the allowance; ``kanban_release._board_allowance`` and every fence stay real.
"""
from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_release
from hermes_cli import kanban_release_offer as offers
from hermes_cli import kanban_spend_budget as budget
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_stranded_park as stranded
from tests.hermes_cli import test_kanban_stranded_park as park

kanban_home = park.kanban_home
own_gateway = park.own_gateway
CHAT = "20261128_040840_673ada"
MAC = "david-mac"


@pytest.fixture
def board_allowance(monkeypatch):
    """The Board holds the card's ten-lease reviewer allowance, used up; a human renewal retires it."""
    from hermes_cli import kanban_allowance_observation as observation
    from hermes_cli import kanban_scope_renewal_cli as renewal_cli
    from hermes_cli import kanban_spend_allowance as allowance
    state = {"seen": {"state": "active", "remaining": 0, "used": 10, "runs": 10}, "reads": 0, "renewed": []}

    def read(conn, task_id, **kw):
        assert not conn.in_transaction
        state["reads"] += 1
        return state["seen"]

    def arm():
        """After the stop is recorded: local budget accounting before it is the ordinary cumulative one."""
        monkeypatch.setattr(allowance, "history", lambda conn, task_id: True)
        monkeypatch.setattr(allowance, "state", lambda conn, task_id, data=None: allowance.BOARD)
        monkeypatch.setattr(observation, "board_state", read)
        monkeypatch.setattr(renewal_cli, "renew", lambda conn, task, **kw: state["renewed"].append((task, kw)))
    state["arm"] = arm
    return state


def _held(conn, allowance_state=None):
    tid = park.stopped(conn)
    assert budget.hold(conn, tid) == "triage"
    if allowance_state is not None:
        allowance_state["arm"]()
    return tid


def _stopped_with_live_chat(conn, allowance_state=None):
    """A spend stop whose owner chat (on David's Mac, so live to this home) is linked to the card."""
    tid = _held(conn, allowance_state)
    park._link(conn, tid, MAC, "default", CHAT)
    return tid


def _ask(conn, tid):
    return json.loads(park._row(conn, tid)[2])


def _answer(conn, tid, choice, op=None, generation=None):
    request = {"ownerGateway": "owner", "ownerProfile": "default", "board": "default", "task": tid,
               "generation": generation or ca.snapshot(conn, tid)["generation"], "choice": choice,
               "operationId": op or f"desktop-op-{choice}"}
    return ca.apply(conn, request, owner_gateway="owner", owner_profile="default", board="default",
                    verify_source=lambda req: {"gateway": "origin", "recorded": True, "answer": req})


def _released(conn, tid):
    return conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=? AND body LIKE 'Released to finish%'",
                        (tid,)).fetchone()[0]


def test_live_owner_that_cannot_release_gets_the_stamped_offer_once(kanban_home, own_gateway, board_allowance):
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn, board_allowance)
        assert stranded.live_owner(conn, tid, own=park.OWN, liveness=stranded.Liveness(now=park._later(0), idle=60))
        # At once: no strand age is waited for, and a live owner chat does not suppress it.
        assert park._sweep(conn, now=park._later(0.1), cfg={}) == [tid]
        ask = _ask(conn, tid)
        assert ask["choices"] == [{"label": "Release", "value": "release"}, {"label": "Don't", "value": "dont"}]
        assert ask[kanban_release.STAMP]["task"] == tid and kanban_release.offered(conn, tid, ask) == tid
        assert f"@task:default@{park.OWN}/{tid}" in ask["action"] and "Release" in ask["action"]
        assert "used up" in ask["why_human"] and "Cost: up to" in ask["scope"]
        from hermes_cli.kanban_block_policy import user_ask_lint_error
        assert user_ask_lint_error(ask) is None
        (filing,) = park._events(conn, tid, stranded.EVENT)
        assert filing["human_only"] == "allowance_exhausted" and filing["ask_sha256"] == kanban_release.ask_digest(ask)
        assert f"{MAC}/default/{CHAT}" in filing["chats"]
        subs = {r[0] for r in conn.execute("SELECT chat_id FROM kanban_notify_subs WHERE task_id=?", (tid,))}
        assert f"{MAC}/default/{CHAT}" in subs
        # Once per park episode, however often the sweep runs.
        assert park._sweep(conn, now=park._later(3), cfg={}) == []
        assert len(park._events(conn, tid, stranded.EVENT)) == 1


@pytest.mark.parametrize("live_chat", [False, True])
def test_young_supervisor_owned_stop_is_not_offered(kanban_home, own_gateway, board_allowance, live_chat):
    with kbc.connect_closing() as conn:
        tid = _held(conn)
        supervisor = park._legacy_wrapper(conn, tid)
        if live_chat:
            park._link(conn, tid, MAC, "default", CHAT)
        board_allowance["arm"]()
        assert stranded.open_decision_card(conn, tid) == supervisor
        before = park._row(conn, tid)
        assert tid not in park._sweep(conn, now=park._later(0.1), cfg={})
        assert park._row(conn, tid) == before
        assert not park._events(conn, tid, stranded.EVENT)
        assert board_allowance["reads"] == 0


def test_live_owner_that_can_release_is_left_alone(kanban_home, own_gateway, board_allowance):
    board_allowance["seen"] = {"state": "active", "remaining": 4}
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn, board_allowance)
        assert park._sweep(conn, now=park._later(3), cfg={}) == []
        assert park._row(conn, tid)[2] is None and not park._events(conn, tid, stranded.EVENT)


def test_young_card_without_allowance_history_costs_no_board_read(kanban_home, own_gateway, monkeypatch):
    from hermes_cli import kanban_allowance_observation as observation
    monkeypatch.setattr(observation, "board_state", lambda *a, **kw: pytest.fail("no Board read for this card"))
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn)
        assert park._sweep(conn, now=park._later(0.1), cfg={}) == []
        assert park._row(conn, tid)[2] is None


def test_release_answer_releases_as_the_human_and_renews_runs(kanban_home, own_gateway, board_allowance):
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn, board_allowance)
        assert park._sweep(conn, now=park._later(0.1), cfg={}) == [tid]
        generation = ca.snapshot(conn, tid)["generation"]
        receipt = _answer(conn, tid, "release")
        assert receipt["choice"] == "release" and receipt["operationId"] == "desktop-op-release"
        assert kb.get_task(conn, tid).status in ("ready", "todo") and not decision.pending(conn, tid)
        assert board_allowance["renewed"] and board_allowance["renewed"][0][1]["allowance_only"] is True
        record = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                         (tid, kanban_release.RELEASED)).fetchone()[0])
        assert record["authority_kind"] == "human" and record["actor_kind"] == "human"
        assert record["runs"] == kanban_release.default_runs() and record["budget_runs"] >= record["runs"]
        written = [json.loads(r[0]) for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='commented'", (tid,))]
        assert any(p.get("writer_kind") == "human" for p in written)
        assert _answer(conn, tid, "release", generation=generation) == receipt  # redelivered: same receipt
        assert _released(conn, tid) == 1


def test_release_answer_needs_no_seen_after_an_unread_refusal(kanban_home, own_gateway, board_allowance):
    """David's repro: his "release" comment is unread and a read-first refusal already bounced a release
    (``release_unread_shown``); the recorded answer still releases, with no ``--seen`` step for him."""
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn, board_allowance)
        said = kb.add_comment(conn, tid, "david", "release", writer_kind="human")
        agent = ("agent", "20261128_agent", "4242:HERMES_SESSION_ID")
        with pytest.raises(kanban_release._Refusal):
            kanban_release.read_first(conn, tid, decision.pending_episode(conn, tid), agent, None, None)
        shown = park._events(conn, tid, kanban_release.SHOWN)
        assert shown and said in shown[-1]["comment_ids"]
        assert park._sweep(conn, now=park._later(0.1), cfg={}) == [tid]
        assert _answer(conn, tid, "release")["choice"] == "release"
        assert kb.get_task(conn, tid).status in ("ready", "todo") and _released(conn, tid) == 1


def test_dont_answer_leaves_the_card_untouched(kanban_home, own_gateway, board_allowance):
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn, board_allowance)
        assert park._sweep(conn, now=park._later(0.1), cfg={}) == [tid]
        ask = _ask(conn, tid)
        receipt = _answer(conn, tid, "dont")
        assert receipt["choice"] == "dont"
        assert park._row(conn, tid)[0] == "triage" and _ask(conn, tid) == ask and decision.pending(conn, tid)
        assert _released(conn, tid) == 0 and not board_allowance["renewed"]
        assert _answer(conn, tid, "dont") == receipt


def test_agent_release_is_still_refused(kanban_home, own_gateway, board_allowance):
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn, board_allowance)
        assert park._sweep(conn, now=park._later(0.1), cfg={}) == [tid]
        agent = ("agent", "20261128_agent", "4242:HERMES_SESSION_ID")
        out = kanban_release.release(conn, tid, writer=agent, env={"HERMES_SESSION_ID": agent[1]},
                                     ancestry=[{"HERMES_SESSION_ID": agent[1]}])
        assert out["outcome"] == "refused"
        assert park._row(conn, tid)[0] == "triage" and _released(conn, tid) == 0


def test_stale_stamp_after_a_new_stop_is_a_plain_unblock(kanban_home, own_gateway, board_allowance):
    with kbc.connect_closing() as conn:
        tid = _stopped_with_live_chat(conn, board_allowance)
        assert park._sweep(conn, now=park._later(0.1), cfg={}) == [tid]
        stamp = _ask(conn, tid)[kanban_release.STAMP]
        with kb.write_txn(conn):  # released by hand, then stopped again: the stamp names the earlier stop
            kb._append_event(conn, tid, "promoted_manual", {"actor": "someone", "reason": "one more"})
            kb._append_event(conn, tid, decision.EVENT, {"reason": "Spend budget: again"})
        assert kanban_release.offer(conn, tid)["episode"] != stamp["episode"]
        with pytest.raises(ca.AnswerRefused, match="ask_changed_or_held|spend_decision_pending"):
            _answer(conn, tid, "release")
        assert park._row(conn, tid)[0] == "triage" and _released(conn, tid) == 0


def _cli(monkeypatch, capsys, *args):
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    capsys.readouterr()
    rc = kanban.kanban_command(parser.parse_args(["kanban", "offer-release", *args, "--json"]))
    return rc, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_offer_release_files_for_the_requesting_chat(kanban_home, own_gateway, board_allowance, monkeypatch,
                                                         capsys):
    with kbc.connect_closing() as conn:
        tid = _held(conn, board_allowance)
    rc, out = _cli(monkeypatch, capsys, tid, "--for-chat", f"{MAC}/default/{CHAT}")
    assert rc == 0 and out["outcome"] == "offered" and out["target"] == tid, out
    rc, again = _cli(monkeypatch, capsys, tid, "--for-chat", f"{MAC}/default/{CHAT}")
    assert rc == 0 and again["outcome"] == "already_offered"
    with kbc.connect_closing() as conn:
        assert kanban_release.offered(conn, tid, _ask(conn, tid)) == tid
        links = {(r[0], r[1], r[2]) for r in conn.execute(
            "SELECT chat_gateway, chat_profile, chat_session FROM task_chat_links WHERE task_id=?", (tid,))}
        assert (MAC, "default", CHAT) in links
        assert len(park._events(conn, tid, stranded.EVENT)) == 1
        _answer(conn, tid, "release")
        assert kb.get_task(conn, tid).status in ("ready", "todo")


def test_cli_offer_release_on_a_safety_held_card_refuses(kanban_home, own_gateway, board_allowance, monkeypatch,
                                                         capsys):
    with kbc.connect_closing() as conn:
        tid = _held(conn, board_allowance)
        stop = decision.pending_episode(conn, tid)
        kb._append_event(conn, tid, "decision_hold_restricted",
                         {"cause": "safety_refusal", "episode": stop.get("uid") or str(stop["id"])})
        conn.commit()
    rc, out = _cli(monkeypatch, capsys, tid)
    assert rc == 1 and out["refusal"] == "safety_restricted", out
    with kbc.connect_closing() as conn:
        assert park._row(conn, tid)[2] is None and not park._events(conn, tid, stranded.EVENT)


def test_offer_on_a_contained_card_refuses(kanban_home, own_gateway, board_allowance, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = _held(conn, board_allowance)
        monkeypatch.setattr("hermes_cli.kanban_containment_guard.pending", lambda c, task, **kw: True)
        with pytest.raises(offers.OfferRefused) as refused:
            offers.offer_release(conn, tid)
        assert refused.value.code == "containment_hold" and park._row(conn, tid)[2] is None


def test_tool_entry_files_the_offer_and_releases_nothing(kanban_home, own_gateway, board_allowance, monkeypatch):
    from tools import kanban_offer_release_tools as tool
    with kbc.connect_closing() as conn:
        tid = _held(conn, board_allowance)
    monkeypatch.setenv("HERMES_SESSION_ID", CHAT)
    out = json.loads(tool.handle({"task_id": tid}))
    assert out["ok"] and out["outcome"] == "offered" and out["released"] is False, out
    with kbc.connect_closing() as conn:
        assert park._row(conn, tid)[0] == "triage" and decision.pending(conn, tid) and _released(conn, tid) == 0
        links = {r[0] for r in conn.execute("SELECT chat_session FROM task_chat_links WHERE task_id=?", (tid,))}
        assert CHAT in links
    refused = json.loads(tool.handle({"task_id": "t_0000000000000000"}))
    assert refused["ok"] is False and refused["refusal"] == "no_such_task"
