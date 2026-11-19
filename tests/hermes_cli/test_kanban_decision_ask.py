"""A park on the card's own orchestrator wakes that chat. [daveey]

Replay of t_01a1d774f7486668 (hub, 2026-11-18 22:25Z): the worker parked for a week with
``awaiting={kind:'external', ref:'Owning orchestrator of comments 216055 and 3389 (hub)'}``. Neither the
comment nor the ``scheduled`` event is a notify kind, so the dormant orchestrator chat never saw its three
decisions. Now the park is recorded as ``kind='chat'`` and posts ``decision_requested``, a wake kind for the
gateway and a notify kind for ``tui`` chats (which the unloaded sweeper turns into a turn in a dormant chat).
"""
from __future__ import annotations

import argparse
import contextlib
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
        assert cmd.startswith(f"hermes kanban wait-comment {tid} --after-id ") and cmd.endswith(" --skip-system"), cmd
        after = int(cmd.split("--after-id ", 1)[1].split()[0])
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))

    def probe() -> int:
        return kcli.kanban_command(parser.parse_args(["kanban", "wait-comment", tid, "--after-id", str(after),
                                                      "--skip-system"]))

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


def test_named_profile_worker_sees_the_root_store_chat(kanban_home, monkeypatch) -> None:
    """Adversarial review of #3647: a worker for profile 'coder' runs with HERMES_HOME=<root>/profiles/coder; the
    Desktop orchestrator chat lives in <root>/state.db, which the sweeper reads, so it must count."""
    import hermes_constants
    chat = _session()  # in the root (launch) store
    profile_home = kanban_home / "profiles" / "coder"
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text("{}\n")
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid, chat)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None, raising=False)
        assert hermes_constants.get_hermes_home() == profile_home
        assert kaw.tui_chat_reachable(chat)
        assert kaw.has_chat_subscriber(conn, tid)


@pytest.mark.parametrize("op", ["schedule", "block"])
def test_park_progress_note_does_not_fire_the_chat_probe(kanban_home, monkeypatch, capsys, op) -> None:
    """Review of #3647: a reason announcing finished work makes the park write a 'kanban-park' progress comment
    after the probe's --after-id was taken; that kernel note must not count as the orchestrator's answer."""
    from hermes_cli import kanban as kcli

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        args = {"reason": LIVE_REASON + " Everything else is done.", "clock_only": True, "awaiting": LIVE_AWAITING}
        if op == "block":
            args["kind"] = "time"
        out = _tool(op, args)
        assert out.get("ok") is not False and "error" not in out, out
        assert any(c.author == "kanban-park" for c in kb.list_comments(conn, tid))
        argv = kwc.get_state(conn, tid)["cmd"].split()[1:]
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    assert kcli.kanban_command(parser.parse_args(argv)) != 0
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "kanban-dispatcher", "Woken: probe stale", system=True)
        kb.add_comment(conn, tid, "dispatcher", "Host rebooted; card requeued", system=True)
        kb.add_comment(conn, tid, "placement", "Placed on worker-1", system=True)
    assert kcli.kanban_command(parser.parse_args(argv)) != 0
    with kbc.connect_closing() as conn:
        kb.add_comment(conn, tid, "default", "Decisions: B, authorize, Phase A alone.")
    assert kcli.kanban_command(parser.parse_args(argv)) == 0
    capsys.readouterr()


def test_foreign_card_parks_without_a_local_subscriber(kanban_home, monkeypatch) -> None:
    """Review of #3647: a pool worker parking a hub card has no copy of the hub's subscriptions or chat stores.
    It parks and posts the ask; the owner, which holds the subscriptions, delivers it after sync."""
    from tests.hermes_cli.test_kanban_release_imported_comments import enable_sync
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    enable_sync(tid)  # this gateway is "owner"
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on = 'hub' WHERE id = ?", (tid,))
        _as_worker(monkeypatch, conn, tid)
        assert not kaw.has_chat_subscriber(conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "clock_only": True, "awaiting": LIVE_AWAITING})
        assert out.get("ok") is not False and "error" not in out, out
        assert _park_payload(conn, tid)["awaiting"]["kind"] == "chat" and len(_asks(conn, tid)) == 1


def test_owned_card_still_needs_a_subscriber(kanban_home, monkeypatch) -> None:
    from tests.hermes_cli.test_kanban_release_imported_comments import enable_sync
    with kbc.connect_closing() as conn:
        tid = _running(conn)
    enable_sync(tid)  # created_on = this gateway
    with kbc.connect_closing() as conn:
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "clock_only": True, "awaiting": LIVE_AWAITING})
        assert kaw.NO_CHAT_SUBSCRIBER in (out.get("code") or "") + out.get("error", ""), out


def _imported_ask(tid) -> None:
    """A pool worker's park reaching the owner by sync: its decision_requested event."""
    from hermes_cli import kanban_db_sync as sync
    payload = {"why": "premise A, B or C for the replay?", "reason": LIVE_REASON}
    change = sync.RemoteChange(1, "task_events", "remote-ask-1", "upsert",
                               {"task_id": tid, "kind": "decision_requested", "created_at": int(time.time()),
                                "payload": json.dumps(payload)}, 1, "", "worker-a1")
    with kbc.connect_closing() as conn:
        assert sync.apply_remote_changes(conn, [change], new_cursor=1, observers=True).applied == 1


def _owned_parked(subscribed: bool) -> str:
    from tests.hermes_cli.test_kanban_release_imported_comments import enable_sync
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        if subscribed:
            _subscribe(conn, tid)
    enable_sync(tid)  # this gateway ("owner") owns the card
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (tid,))
    return tid


def test_owner_with_no_wakeable_chat_turns_an_imported_ask_into_a_visible_one(kanban_home) -> None:
    """Adversarial review of #3647: a foreign-card park skips the subscriber check, so the owner must act."""
    tid = _owned_parked(subscribed=False)
    _imported_ask(tid)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", task.status
        assert kwc.get_state(conn, tid) is None
        assert [e.kind for e in kb.list_events(conn, tid) if e.kind == "decision_unanswerable"]
        request = json.loads(task.human_request) if isinstance(task.human_request, str) else task.human_request
        assert request["interaction"] == "reply" and "premise A, B or C" in request["action"]


def test_owner_with_a_wakeable_chat_leaves_the_card_parked(kanban_home) -> None:
    tid = _owned_parked(subscribed=True)
    _imported_ask(tid)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"


class _ChatStore:
    """The session store the sweeper opens for a reachable, live (not archived) orchestrator chat."""

    def get_compression_tip(self, chat_id):
        return chat_id

    def _read_all(self, sql, params=()):
        return [{"archived": 0, "end_reason": None}] if "archived" in sql and "end_reason" in sql else []

    def kanban_notice_keys_delivered(self, tip, keys):
        return set()

    def __getattr__(self, name):
        return lambda *a, **k: None


def _sweep(chat, now, *, busy_slots=False):
    from tui_gateway import kanban_unloaded as ku
    wakes, rows = [], []

    class Store(_ChatStore):
        def append_kanban_notice(self, tip, text, metadata):
            rows.append(text)
            return True

    @contextlib.contextmanager
    def session_db(chat_id):
        yield Store(), chat_id

    def wake(tip, text, meta):
        wakes.append(text)
        return "started"

    if busy_slots:
        taken = 0
        while ku._wake_slots.acquire(blocking=False):
            taken += 1
    try:
        ku.sweep_once(set(), grace_s=0, now=now, session_db=session_db, wake=wake, holders=list,
                      spawn=lambda job: job())
    finally:
        if busy_slots:
            for _ in range(taken):
                ku._wake_slots.release()
    return wakes, rows


def _parked_ask(monkeypatch):
    chat = _session()
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid, chat)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "clock_only": True, "awaiting": LIVE_AWAITING})
        assert kb.get_task(conn, tid).status == "scheduled", out
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    return chat, tid


@pytest.mark.parametrize("busy_slots", [False, True])
def test_an_ask_that_cannot_wake_now_is_retried_never_written_passively(kanban_home, monkeypatch,
                                                                       busy_slots) -> None:
    """Adversarial review of #3647: with the chat's hourly wake budget spent (or every wake slot busy), the sweeper
    wrote the ask as a passive row and acked it; no turn read it and the card stayed parked."""
    from tui_gateway import kanban_unloaded as ku
    chat, _tid = _parked_ask(monkeypatch)
    now = time.time() + 10
    ku._reset_wake_budget_for_tests()
    if not busy_slots:
        for i in range(ku.WAKES_PER_HOUR):  # the orchestrator's other cards woke it this hour
            ku._charge_wake(chat, now - 60 * (i + 1))
    wakes, rows = _sweep(chat, now, busy_slots=busy_slots)
    assert not wakes and not rows  # handed back, not written passively
    later = now + (60 if busy_slots else 3600)  # a slot frees / the budget's hour slides
    wakes, rows = _sweep(chat, later)
    assert len(wakes) == 1 and "parked until you decide" in wakes[0] and not rows, (wakes, rows)
    ku._reset_wake_budget_for_tests()


def test_an_ask_undelivered_for_two_hours_goes_to_david(kanban_home, monkeypatch) -> None:
    from tui_gateway import kanban_unloaded as ku
    chat, tid = _parked_ask(monkeypatch)
    now = time.time() + ku.ASK_OVERDUE_S + 60
    ku._reset_wake_budget_for_tests()
    for i in range(ku.WAKES_PER_HOUR):
        ku._charge_wake(chat, now - 60 * (i + 1))
    _sweep(chat, now)
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task.status == "blocked", task.status
        request = json.loads(task.human_request) if isinstance(task.human_request, str) else task.human_request
        assert "premise A/B/C" in request["action"] or "Owning orchestrator" in request["action"], request
    ku._reset_wake_budget_for_tests()


def test_reworded_chat_reask_still_escalates(kanban_home, monkeypatch) -> None:
    """Adversarial review of #3647: the premise hashed the question, so a reworded re-ask never escalated."""
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        for n in range(2):
            _as_worker(monkeypatch, conn, tid)
            out = _tool("schedule", {"reason": f"waiting for the owning orchestrator decision, round {n}",
                                     "clock_only": True, "awaiting": {"kind": "chat", "ref": "owning orchestrator",
                                                                      "why": f"premise A or B? (asked {n} times)"}})
            assert out.get("status") == "scheduled", out
            assert kb.unblock_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            assert kb.claim_task(conn, tid, claimer="worker") is not None
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "still need the orchestrator's answer", "clock_only": True,
                                 "awaiting": {"kind": "chat", "ref": "owning orchestrator",
                                              "why": "which premise, A or B, please?"}})
        assert out.get("code") == "repark_escalated" and out.get("status") == "todo", out


def test_declared_chat_wait_needs_no_probe_or_clock_flag(kanban_home, monkeypatch) -> None:
    """Adversarial review of #3647: the long-park guard refused a chat wait before its comment probe existed."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the release go",
                                 "awaiting": {"kind": "chat", "ref": "owning orchestrator", "why": "premise A or B?"}})
        assert out.get("status") == "scheduled", out
        assert "wait-comment" in kwc.get_state(conn, tid)["cmd"]


def test_separately_answered_questions_do_not_escalate(kanban_home, monkeypatch) -> None:
    """Review of #3647: with one premise per orchestrator, three questions each answered escalated on the third."""
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        for n in range(3):
            _as_worker(monkeypatch, conn, tid)
            out = _tool("schedule", {"reason": f"waiting for the owning orchestrator decision {n}", "clock_only": True,
                                     "awaiting": {"kind": "chat", "ref": "owning orchestrator", "why": f"question {n}?"}})
            assert out.get("status") == "scheduled", (n, out)
            _orchestrator_comment(monkeypatch, conn, tid, f"Answer {n}: go with A.")
            assert kb.unblock_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            assert kb.claim_task(conn, tid, claimer="worker") is not None


def test_unanswered_chat_wait_goes_to_david_after_two_hours(kanban_home, monkeypatch) -> None:
    """Adversarial review of #3647: a wake that was refused, failed or never started acked the ask passively.
    The board backstop: the card's comment probe says "not yet" for 2 h -> needs_input for David."""
    from hermes_cli import kanban_decision_ask as kda
    _chat, tid = _parked_ask(monkeypatch)
    with kbc.connect_closing() as conn:
        assert not kda.escalate_if_overdue(conn, tid, now=int(time.time()) + 60)  # not yet overdue
        assert kda.escalate_if_overdue(conn, tid, now=int(time.time()) + kda.ASK_OVERDUE_S + 60)
        assert kb.get_task(conn, tid).status == "blocked"


@pytest.mark.parametrize("answered", [False, True])
def test_a_chat_wait_on_its_own_named_probe_has_the_deadline_until_answered(kanban_home, monkeypatch,
                                                                             answered) -> None:
    """Every chat wait has the deadline (adversarial review of #3647); one that named a CI probe and has its answer
    is waiting on CI, not on the orchestrator (review of #3647)."""
    from hermes_cli import kanban_decision_ask as kda
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "awaiting": LIVE_AWAITING,
                                 "wake_check": "gh run view 40443505012 --json status | grep -q completed"})
        assert out.get("status") == "scheduled", out
        if answered:
            _orchestrator_comment(monkeypatch, conn, tid, "Go with premise A; then wait for the CI run.")
        result = kda.escalate_if_overdue(conn, tid, now=int(time.time()) + kda.ASK_OVERDUE_S + 60)
        assert result is (not answered)
        assert kb.get_task(conn, tid).status == ("scheduled" if answered else "blocked")


@pytest.mark.parametrize("cmd,expected", [
    ("hermes kanban wait-comment t_1 --after-id 3 --skip-system", True),
    ("nice hermes kanban wait-comment t_1 --after-id 3", True),
    ("HERMES_HOME=/root/.hermes hermes kanban wait-comment t_1", True),
    ("/usr/local/bin/hermes kanban wait-comment t_1 --contains APPROVED", True),
    ("hermes kanban wait-check task t_1", False),
    ("gh run view 404 --json status | grep -q completed", False),
    ("", False),
])
def test_comment_probe_is_recognised_however_spelled(cmd, expected) -> None:
    from hermes_cli.kanban_decision_ask import is_comment_probe
    assert is_comment_probe(cmd) is expected


def test_answered_since_uses_times_not_local_ids(kanban_home, monkeypatch) -> None:
    """Review of #3647: on the owner a pool worker's --after-id is in another file's id space; an old comment with
    a high local id must not read as an answer, and a comment after the park must."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        park_at = int(time.time())
        old = kb.add_comment(conn, tid, "default", "an old note from before the park")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at = ? WHERE id = ?", (park_at - 600, old))
        assert not kaw._answered_since(conn, tid, park_at)
        same = kb.add_comment(conn, tid, "operator", "a note in the park's own second")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at = ? WHERE id = ?", (park_at, same))
        assert not kaw._answered_since(conn, tid, park_at)  # nobody could answer an ask in the second it was made
        _orchestrator_comment(monkeypatch, conn, tid, "Go with premise A.")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at = ? WHERE id = (SELECT MAX(id) FROM task_comments)",
                         (park_at + 5,))
        assert kaw._answered_since(conn, tid, park_at)


def test_wake_check_runner_calls_the_backstop(kanban_home, monkeypatch) -> None:
    """The backstop runs on the probe's own path: a "not yet" from the chat probe past the deadline escalates."""
    _chat, tid = _parked_ask(monkeypatch)
    calls = []
    from hermes_cli import kanban_decision_ask as kda
    monkeypatch.setattr(kda, "escalate_if_overdue", lambda conn, task_id, now=None: calls.append(task_id) or False)
    with kbc.connect_closing() as conn:
        state = kwc.get_state(conn, tid)
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(kwc, "_run_probe", lambda key, cmd, cwd, env: (1, "not yet: no comment"))
    kwc._probe_and_apply(db_path, tid, state["cmd"], str(kanban_home), {}, None, state["generation"])
    assert calls == [tid]


def test_not_yet_answers_still_end_in_escalation(kanban_home, monkeypatch) -> None:
    """Adversarial review of #3647: "not yet" replies reset the streak; the daily cap ends the loop."""
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        for n in range(kaw.CHAT_PARK_CAP):
            _as_worker(monkeypatch, conn, tid)
            out = _tool("schedule", {"reason": "waiting for the owning orchestrator decision", "clock_only": True,
                                     "awaiting": {"kind": "chat", "ref": "owning orchestrator", "why": f"ready? {n}"}})
            assert out.get("status") == "scheduled", (n, out)
            _orchestrator_comment(monkeypatch, conn, tid, "Not yet - still waiting on David. Hold.")
            assert kb.unblock_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            assert kb.claim_task(conn, tid, claimer="worker") is not None
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "waiting for the owning orchestrator decision", "clock_only": True,
                                 "awaiting": {"kind": "chat", "ref": "owning orchestrator", "why": "ready now?"}})
        assert out.get("code") == "repark_escalated", out


def test_the_workers_own_progress_note_is_not_an_answer(kanban_home, monkeypatch) -> None:
    """Review of #3647: a worker that comments in each run before re-asking never escalated."""
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        for n in range(2):
            _as_worker(monkeypatch, conn, tid)
            out = _tool("schedule", {"reason": f"waiting for the owning orchestrator decision {n}", "clock_only": True,
                                     "awaiting": {"kind": "chat", "ref": "owning orchestrator", "why": f"q {n}?"}})
            assert out.get("status") == "scheduled", (n, out)
            assert kb.unblock_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            assert kb.claim_task(conn, tid, claimer="worker") is not None
            _as_worker(monkeypatch, conn, tid)
            assert _tool("comment", {"task_id": tid, "body": f"Progress: prepared the next prefix ({n})."}).get("ok")
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "waiting for the owning orchestrator decision", "clock_only": True,
                                 "awaiting": {"kind": "chat", "ref": "owning orchestrator", "why": "q again?"}})
        assert out.get("code") == "repark_escalated", out


def _orchestrator_comment(monkeypatch, conn, tid, body) -> None:
    """The orchestrator answers from its own chat, not from inside this card's run."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    cid = kb.add_comment(conn, tid, "default", body)
    with kb.write_txn(conn):  # an answer comes after the ask was delivered, never in the park's own second
        conn.execute("UPDATE task_comments SET created_at = ? WHERE id = ?", (int(time.time()) + 5, cid))


def test_a_filtered_probe_that_misses_the_answer_still_escalates(kanban_home, monkeypatch) -> None:
    """Adversarial review of #3647: a worker-supplied probe (--contains APPROVED) ignored the answer "Go with A";
    the 2 h guard counted that answer, so it never escalated and the card slept to its deadline."""
    from hermes_cli import kanban_decision_ask as kda
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _subscribe(conn, tid)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": LIVE_REASON, "awaiting": LIVE_AWAITING,
                                 "wake_check": f"hermes kanban wait-comment {tid} --after-id 0 --contains APPROVED"})
        assert out.get("status") == "scheduled", out
        _orchestrator_comment(monkeypatch, conn, tid, "Go with premise A.")
        assert kda.escalate_if_overdue(conn, tid, now=int(time.time()) + kda.ASK_OVERDUE_S + 60)
        assert kb.get_task(conn, tid).status == "blocked"
