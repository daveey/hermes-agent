"""A park on the card's own orchestrator wakes that chat. [daveey]

Replay of t_01a1d774f7486668 (hub, 2026-11-18 22:25Z): the worker parked for a week with
``awaiting={kind:'external', ref:'Owning orchestrator of comments 216055 and 3389 (hub)'}``. Neither the
comment nor the ``scheduled`` event is a notify kind, so the dormant orchestrator chat never saw its three
decisions. Now the park is recorded as ``kind='chat'`` and posts ``decision_requested``, a wake kind for the
gateway and a notify kind for ``tui`` chats (which the unloaded sweeper turns into a turn in a dormant chat).
"""
from __future__ import annotations

import argparse
import json
import time

import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli.test_kanban_awaiting import _as_worker, _park_payload, _running, _tool
from tests.hermes_cli.test_kanban_awaiting import kanban_home as _kanban_home

kanban_home = _kanban_home  # Re-export the shared fixture without shadowing an imported name.

LIVE_REASON = ("SCHEDULED_UNTIL=2099-11-25T22:30:00Z — Await the owning orchestrator's three decisions on "
               "accepted brief v2. Review comment 3392 accepted the revision and released nothing else. A hub "
               "reply wakes for interpretation only, not automatic implementation, publication, or window approval.")
LIVE_AWAITING = {"kind": "external", "ref": "Owning orchestrator of comments 216055 and 3389 (hub)",
                 "why": "Review 3392 left premise A/B/C, owner-journal authorization, and phase release to that "
                        "orchestrator. No hub comment after 3414. Agent decision, not a David ask."}
LIVE_PROBE = "hermes kanban wait-comment {tid} --after-id 3414 --from-gateway hub"


ORCHESTRATOR = "20261114_223808_7fed0a"


def _session(chat_id: str = ORCHESTRATOR, *, source: str = "desktop", archived: bool = False,
             end_reason: str | None = None) -> str:
    """A real session row in the launch store, as the sweeper finds the chat."""
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB
    db = SessionDB(get_hermes_home() / "state.db")
    try:
        if db.get_session(chat_id) is None:
            db.create_session(chat_id, source=source)
        db._conn.execute("UPDATE sessions SET archived = ?, end_reason = ? WHERE id = ?",
                         (1 if archived else 0, end_reason, chat_id))
        db._conn.commit()
    finally:
        db.close()
    return chat_id


def _subscribe(conn, tid, chat_id: str | None = None) -> None:
    chat_id = chat_id or _session()
    # Written directly: add_notify_sub refuses headless-looking ids, and the worker-transcript case must be
    # refused by the chat-wait check, not by a missing row.
    with kb.write_txn(conn):
        conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, notifier_profile, "
                     "delivery_mode, delivery_metadata, created_at, last_event_id) VALUES (?,?,?,?,?,?,?,?,?)",
                     (tid, "tui", chat_id, "", "default", "notify", json.dumps({"reason": "shepherd"}),
                      int(time.time()), 0))


def _asks(conn, tid) -> list:
    return [e for e in kb.list_events(conn, tid) if e.kind == "decision_requested"]


def _assert_chat_park(conn, tid, *, reclassified=True) -> None:
    task = kb.get_task(conn, tid)
    assert task.status == "scheduled", task.status
    rec = _park_payload(conn, tid)["awaiting"]
    assert rec["kind"] == "chat", rec
    if reclassified:
        assert rec["reclassified_from"] == "external" and rec["ref"].startswith("Owning orchestrator")
    asks = _asks(conn, tid)
    assert len(asks) == 1, [e.kind for e in kb.list_events(conn, tid)]
    assert "premise A/B/C" in (asks[0].payload.get("why") or asks[0].payload.get("reason"))


@pytest.mark.parametrize("op", ["schedule", "block"])
def test_live_external_orchestrator_park_becomes_a_chat_wait_that_asks(kanban_home, monkeypatch, op) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        args = {"reason": LIVE_REASON, "wake_check": LIVE_PROBE.format(tid=tid), "awaiting": LIVE_AWAITING}
        if op == "block":
            args["kind"] = "time"
        out = _tool(op, args)
        assert out.get("ok") is not False and "error" not in out, out
        _assert_chat_park(conn, tid)


def test_cli_schedule_records_the_same_chat_wait(kanban_home, capsys) -> None:
    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
    rc = kcli.kanban_command(parser.parse_args([
        "kanban", "schedule", tid, LIVE_REASON, "--wake-check", LIVE_PROBE.format(tid=tid),
        "--awaiting", json.dumps(LIVE_AWAITING)]))
    assert rc == 0, capsys.readouterr().err
    with kbc.connect_closing() as conn:
        _assert_chat_park(conn, tid)


@pytest.mark.parametrize("op", ["schedule", "block"])
def test_chat_wait_with_no_subscribed_chat_is_refused(kanban_home, monkeypatch, op) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        args = {"reason": LIVE_REASON, "wake_check": LIVE_PROBE.format(tid=tid), "awaiting": LIVE_AWAITING}
        if op == "block":
            args["kind"] = "time"
        out = _tool(op, args)
        assert out.get("code") == kaw.NO_CHAT_SUBSCRIBER or kaw.NO_CHAT_SUBSCRIBER in out.get("error", ""), out
        assert kb.get_task(conn, tid).status == "running" and not _asks(conn, tid)
        assert kwc.get_state(conn, tid) is None


def test_declared_chat_wait_parks_and_asks(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the release go",
                                 "wake_check": LIVE_PROBE.format(tid=tid),
                                 "awaiting": {"kind": "chat", "ref": "owning orchestrator",
                                              "why": "premise A/B/C for the replay?"}})
        assert out.get("ok") is not False and "error" not in out, out
        _assert_chat_park(conn, tid, reclassified=False)


def test_inferred_orchestrator_wait_is_a_chat_wait(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "clock_only": True})
        assert out.get("ok") is not False and "error" not in out, out
        rec = _park_payload(conn, tid)["awaiting"]
        assert rec["kind"] == "chat" and rec["inferred"] is True
        assert len(_asks(conn, tid)) == 1


@pytest.mark.parametrize("awaiting,reason", [
    ({"kind": "external", "ref": "AWS support case 17722033541", "why": "quota raise requested, case open"},
     "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for AWS support to raise the quota"),
    ({"kind": "external", "ref": "vendor ticket 4411", "why": "vendor fix scheduled"},
     "Per the orchestrator's decision (comment 3392), waiting for vendor ticket 4411 to close."),
])
def test_vendor_waits_and_past_citations_are_unchanged(kanban_home, monkeypatch, awaiting, reason) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": reason, "clock_only": True, "awaiting": awaiting})
        assert out.get("ok") is not False and "error" not in out, out
        assert _park_payload(conn, tid)["awaiting"]["kind"] == "external"
        assert not _asks(conn, tid)


def test_decision_requested_reaches_live_and_dormant_chats() -> None:
    """Gateway: a wake kind with a formatter. TUI: a notify kind the live poller and the unloaded sweeper claim."""
    from gateway import kanban_watchers_notifier as gn
    from gateway.kanban_wake_fold import _PREMISE_HOLDS
    from tui_gateway import session_notifications as sn

    assert "decision_requested" in gn.TERMINAL_KINDS and "decision_requested" in gn._WAKE_KINDS
    assert "decision_requested" in gn._EVENT_FORMATTERS
    assert "decision_requested" in sn._KANBAN_NOTIFY_KINDS and "decision_requested" in sn._KANBAN_EVENT_FORMATTERS
    assert _PREMISE_HOLDS["decision_requested"] == frozenset({"scheduled"})
    from hermes_cli.kanban_decision_ask import notice_text
    text = notice_text({"why": "premise A/B/C?"})
    assert "premise A/B/C?" in text and "kanban_comment" in text
    _icon, render = sn._KANBAN_EVENT_FORMATTERS["decision_requested"]
    assert "premise A/B/C?" in render(None, {"why": "premise A/B/C?"}, "t")


@pytest.mark.parametrize("text,ref,expected", [
    (LIVE_REASON, None, True),
    ("", "Owning orchestrator of comments 216055 and 3389 (hub)", True),
    ("Parked pending the orchestrator's go-ahead on phase B.", None, True),
    ("Waiting on the orchestrator to decide between A and B.", None, True),
    ("Need the creator chat's answer on the scope.", None, True),
    ("Awaiting the origin session's call on the window.", None, True),
    # citations, negations and product names are not waits on the card's orchestrator (review of #3647)
    ("Per the orchestrator's decision, waiting for CI run 40443505012.", None, False),
    ("Orchestrator decision recorded; waiting for the release build.", None, False),
    ("Waiting for vendor ticket 4411 per the orchestrator's decision in comment 3392.", None, False),
    ("This card needs no orchestrator decision; waiting for CI run 40443505012.", None, False),
    ("Waiting for the Airflow orchestrator release 2.9 from the vendor.", None, False),
    ("Blocked on the k8s orchestrator call timing out; vendor ticket 4411.", None, False),
])
def test_orchestrator_wait_matcher(text, ref, expected) -> None:
    assert kaw.orchestrator_wait(text, ref) is expected


WHY_DAVID = "David's approval of the $400 GPU spend is pending"
WHY_WALL = "needs the IAM permission grant on s3://prod-bucket"
DAVID_REASON = "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for David's approval of the $400 GPU spend"


@pytest.mark.parametrize("reason,awaiting,code", [
    (DAVID_REASON, {"kind": "chat", "ref": "owning orchestrator", "why": WHY_DAVID}, kaw.HUMAN_WAIT),
    (DAVID_REASON, {"kind": "external", "ref": "Owning orchestrator", "why": WHY_DAVID}, kaw.HUMAN_WAIT),
    (DAVID_REASON + "; awaiting the owning orchestrator's go-ahead",
     {"kind": "external", "ref": "AWS case 17722033541", "why": WHY_DAVID}, kaw.HUMAN_WAIT),
    ("SCHEDULED_UNTIL=2099-01-01T00:00:00Z " + WHY_WALL, {"kind": "chat", "why": WHY_WALL}, kaw.AGENT_ACTION_WAIT),
    ("SCHEDULED_UNTIL=2099-01-01T00:00:00Z " + WHY_WALL,
     {"kind": "external", "ref": "Owning orchestrator", "why": WHY_WALL}, kaw.AGENT_ACTION_WAIT),
])
def test_chat_wait_never_launders_a_david_ask_or_an_agent_wall(kanban_home, monkeypatch, reason, awaiting,
                                                                 code) -> None:
    """Adversarial review of #3647: each row parked as a chat wait before the wall check in _check_chat."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": reason, "clock_only": True, "awaiting": awaiting})
        assert out.get("code") == code or code in out.get("error", ""), out
        assert kb.get_task(conn, tid).status == "running" and not _asks(conn, tid)


@pytest.mark.parametrize("platform,mode,parks", [
    ("tui", None, True),
    ("telegram", "notify+wake", True),
    ("telegram", None, False),  # a passive ping, maybe to a person: no orchestrator is woken
    ("hub", None, False),       # the hub relay carries only cards that turned terminal
    ("hub", "notify+wake", False),  # legacy hub rows were backfilled to notify+wake: still only terminal cards
])
def test_only_a_subscription_the_notice_can_wake_counts(kanban_home, monkeypatch, platform, mode, parks) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        chat = _session() if platform == "tui" else "chat-1"
        kbn.add_notify_sub(conn, task_id=tid, platform=platform,
                           chat_id="macbook/default/20261026_235722_bef6c6" if platform == "hub" else chat,
                           notifier_profile="default", delivery_mode=mode,
                           delivery_metadata={"reason": "shepherd"})
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "clock_only": True, "awaiting": LIVE_AWAITING})
        if parks:
            assert out.get("ok") is not False and "error" not in out, out
            assert len(_asks(conn, tid)) == 1
        else:
            assert kaw.NO_CHAT_SUBSCRIBER in (out.get("code") or "") + out.get("error", ""), out
            assert kb.get_task(conn, tid).status == "running"


def test_continue_on_the_orchestrator_asks_too(kanban_home, monkeypatch) -> None:
    """Review of #3647: kanban_continue recorded the chat wait but posted no decision_requested."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("continue", {"checkpoint": LIVE_REASON, "resume_after_seconds": 86400,
                                 "wake_check": LIVE_PROBE.format(tid=tid), "awaiting": LIVE_AWAITING})
        assert out.get("ok") is not False and "error" not in out, out
        assert _park_payload(conn, tid)["awaiting"]["kind"] == "chat"
        assert len(_asks(conn, tid)) == 1
        assert "wait-comment" in (kwc.get_state(conn, tid) or {}).get("cmd", "")


def test_cli_block_time_records_the_chat_wait(kanban_home, capsys) -> None:
    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
    rc = kcli.kanban_command(parser.parse_args([
        "kanban", "block", tid, LIVE_REASON, "--kind", "time", "--wake-check", LIVE_PROBE.format(tid=tid),
        "--awaiting", json.dumps(LIVE_AWAITING)]))
    assert rc == 0, capsys.readouterr().err
    with kbc.connect_closing() as conn:
        _assert_chat_park(conn, tid)


@pytest.mark.parametrize("op", ["schedule", "block"])
def test_clock_only_chat_wait_wakes_on_the_answering_comment(kanban_home, monkeypatch, capsys, op) -> None:
    """Review of #3647: a chat wait with no probe slept to its clock. Its auto probe ignores the park's own
    comment and fires on the next one."""
    from hermes_cli import kanban as kcli

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        args = {"reason": LIVE_REASON, "clock_only": True, "awaiting": LIVE_AWAITING}
        if op == "block":
            args["kind"] = "time"
        out = _tool(op, args)
        assert out.get("ok") is not False and "error" not in out, out
        cmd = (kwc.get_state(conn, tid) or {}).get("cmd", "")
        assert cmd.startswith(f"hermes kanban wait-comment {tid} --after-id "), cmd
        after = int(cmd.rsplit(" ", 1)[1])
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))

    def probe() -> int:
        return kcli.kanban_command(parser.parse_args(["kanban", "wait-comment", tid, "--after-id", str(after)]))

    assert probe() != 0  # the park's own comment and notice do not count as the answer
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "default", "Decisions: B, authorize, Phase A alone.")
    assert probe() == 0
    capsys.readouterr()


@pytest.mark.parametrize("shape", ["archived", "moved", "worker_transcript", "kanban_source", "missing"])
def test_unreachable_tui_chat_does_not_count(kanban_home, monkeypatch, shape) -> None:
    """Adversarial review of #3647: the sweeper drops a notice to an archived/moved chat, a worker transcript or
    a chat no store holds, so such a tui row cannot carry the ask."""
    chat = {"archived": lambda: _session("20261101_010101_aaaaaa", archived=True),
            "moved": lambda: _session("20261101_010101_bbbbbb", end_reason="moved_to_gateway:hub"),
            "worker_transcript": lambda: _session("local-" + "ab" * 32, source="local"),
            "kanban_source": lambda: _session("20261101_010101_cccccc", source="kanban"),
            "missing": lambda: "20261101_010101_dddddd"}[shape]()
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid, chat)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "clock_only": True, "awaiting": LIVE_AWAITING})
        assert kaw.NO_CHAT_SUBSCRIBER in (out.get("code") or "") + out.get("error", ""), out
        assert kb.get_task(conn, tid).status == "running" and not _asks(conn, tid)


def test_orphan_reaped_desktop_chat_still_counts(kanban_home, monkeypatch) -> None:
    """The live incident's orchestrator: a Desktop chat ended by ws_orphan_reap, which the sweeper resumes."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        chat = _session("20261114_010101_eeeeee", source="local", end_reason="ws_orphan_reap")
        from hermes_constants import get_hermes_home
        import sqlite3
        with sqlite3.connect(get_hermes_home() / "state.db") as sdb:
            sdb.execute("UPDATE sessions SET created_source = 'cli' WHERE id = ?", (chat,))
        _subscribe(conn, tid, chat)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "clock_only": True, "awaiting": LIVE_AWAITING})
        assert out.get("ok") is not False and "error" not in out, out
        assert len(_asks(conn, tid)) == 1
