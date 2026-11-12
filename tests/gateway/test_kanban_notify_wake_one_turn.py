"""One Kanban event starts one model turn in a chat holding both a ``tui`` row and a waking ``local`` row for the
card (t_01a1ce9070eee14c). [daveey]

Chat 20261026_235722_bef6c6 got t_01a1ce83e793e24f's completion twice: the ``local`` notify+wake row woke the chat
(message 872084, 22:54:07 PST) and the ``tui`` notify row's notice started a second turn (872153, 22:59:50 PST).
Real SQLite board and session store, real collector/delivery/poller/sweeper; only the LOCAL adapter is a recorder
(its ``handle_message`` is the wake turn) and a legacy poller batch counts as one turn when it has text.
"""

import asyncio
import contextlib
import time

import pytest

from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import kanban_unloaded as ku
from tui_gateway.server import _collect_kanban_notifications

CHAT = "20261026_235722_bef6c6"
WAIT_PAYLOAD = {"parent": "t_parent", "parent_title": "the card this chat waits on", "ready_minutes": 30}


class LocalAdapter:
    """The gateway's LOCAL adapter: ``send`` is a no-op ack (session_local.py), a wake is a turn."""

    def __init__(self):
        self.sent, self.turns = [], []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append(text)

    async def handle_message(self, event):
        self.turns.append(event.text)
        event._gateway_accepted = True


@pytest.fixture
def board(tmp_path, monkeypatch):
    kb.init_db()
    adapter = LocalAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.LOCAL: adapter}
    runner._profile_adapters = {}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._kanban_notifier_profile = "default"
    runner.config = None
    return runner, adapter


def _card(*, local_mode="notify+wake", tui=True):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="review exact head", assignee="default")
        if local_mode:
            kbn.add_notify_sub(conn, task_id=tid, platform="local", chat_id=CHAT, chat_type="dm",
                               notifier_profile="default", delivery_mode=local_mode)
        if tui:
            kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=CHAT, chat_type="dm",
                               notifier_profile="default", delivery_mode="notify")
        return tid
    finally:
        conn.close()


def _event(tid, kind):
    conn = kbc.connect()
    try:
        if kind == "completed":
            assert kb.complete_task(conn, tid, summary="I approve PR #1250 at its exact head.", force=True)
        else:
            with kb.write_txn(conn):
                kb._append_event(conn, tid, kind, dict(WAIT_PAYLOAD))
    finally:
        conn.close()


def _cursors(tid):
    conn = kbc.connect()
    try:
        last = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id = ?", (tid,)).fetchone()[0]
        rows = conn.execute("SELECT platform, last_event_id, inflight_from FROM kanban_notify_subs WHERE task_id = ?",
                            (tid,)).fetchall()
        return last, {r[0]: (r[1], r[2]) for r in rows}
    finally:
        conn.close()


def _wake_leg(runner, fail_counts=None):
    async def tick():
        deliveries = await asyncio.to_thread(_notifier_collect, runner, kb, notifier_profile="default",
                                             gc_due=False, gc_retention_days=30)
        for d in deliveries:
            await _KanbanNotification(runner, d, platform_cls=Platform,
                                      sub_fail_counts={} if fail_counts is None else fail_counts).deliver()
    asyncio.run(tick())


def _notice_leg():
    """The legacy Desktop poller's collection: a batch with text becomes one notice turn."""
    return _collect_kanban_notifications({"session_key": CHAT})


NOTICE = {"completed": "✔ [default] @default Kanban {tid} done",
          "chat_wait_unclaimed": "⏳ [default] @default Kanban {tid}: t_parent (the card this chat waits on)"}


@pytest.mark.parametrize("kind", ["completed", "chat_wait_unclaimed"])
@pytest.mark.parametrize("wake_first", [True, False], ids=["wake-first", "notice-first"])
def test_notify_wake_event_starts_one_turn_in_either_order_and_both_legs_settle(board, kind, wake_first):
    runner, adapter = board
    tid = _card()
    _event(tid, kind)

    texts = []
    for leg in (("wake", "notice") if wake_first else ("notice", "wake")):
        if leg == "wake":
            _wake_leg(runner)
        else:
            texts += _notice_leg()

    turns = len(adapter.turns) + (1 if texts else 0)
    assert turns == 1, (adapter.turns, texts)
    assert texts == []
    [turn] = adapter.turns
    assert f"[kanban] Task {tid}" in turn  # follow-through's wake-leg proof stays intact
    assert NOTICE[kind].format(tid=tid) in turn  # the notice text is not dropped: it rides the wake turn
    last, subs = _cursors(tid)
    assert subs["local"] == (last, None) and subs["tui"] == (last, None)  # both legs settled
    # A later poll of either leg starts nothing.
    _wake_leg(runner)
    assert _notice_leg() == [] and len(adapter.turns) == 1


def test_notify_only_local_row_leaves_the_tui_notice_turn(board):
    runner, adapter = board
    tid = _card(local_mode="notify")
    _event(tid, "completed")
    _wake_leg(runner)
    texts = _notice_leg()
    assert adapter.turns == []
    assert len(texts) == 1 and NOTICE["completed"].format(tid=tid) in texts[0]


def test_tui_only_row_still_gets_its_notice_turn(board):
    runner, adapter = board
    tid = _card(local_mode=None)
    _event(tid, "completed")
    _wake_leg(runner)
    assert adapter.turns == [] and len(_notice_leg()) == 1


def test_kinds_the_wake_leg_does_not_wake_on_stay_with_the_tui_row(board):
    runner, adapter = board
    tid = _card()
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "status", {"status": "ready"})
    finally:
        conn.close()
    _wake_leg(runner)
    texts = _notice_leg()
    assert adapter.turns == [] and len(texts) == 1 and "→ ready" in texts[0]


def test_wake_without_a_tui_sibling_keeps_its_text(board):
    runner, adapter = board
    tid = _card(tui=False)
    _event(tid, "completed")
    _wake_leg(runner)
    [turn] = adapter.turns
    assert f"[kanban] Task {tid} completed." in turn and f"Kanban {tid} done" not in turn


def test_unloaded_sweeper_leaves_a_covered_event_to_the_wake_turn(board, tmp_path, monkeypatch):
    from gateway import kanban_wake_sibling
    monkeypatch.setattr(kanban_wake_sibling, "DEFER_MAX_S", 7200.0)  # the sweeper runs an hour on; the wake is due
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    db = SessionDB(tmp_path / "state.db")
    db.create_session(CHAT, "desktop")
    db.append_message(CHAT, "user", "start the cards")

    @contextlib.contextmanager
    def opener(chat_id):
        yield (db, chat_id) if db.get_session(chat_id) else (None, None)

    runner, adapter = board
    tid = _card()
    _event(tid, "completed")
    assert ku.sweep_once(set(), now=time.time() + 3600, session_db=opener) == []
    assert [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"] == []
    last, subs = _cursors(tid)
    assert subs["tui"][0] < last and subs["tui"][1] is None  # left unclaimed for the wake, not acked past it
    _wake_leg(runner)
    assert len(adapter.turns) == 1 and NOTICE["completed"].format(tid=tid) in adapter.turns[0]
    last, subs = _cursors(tid)
    assert subs["tui"] == (last, None)  # the wake settled it
    assert ku.sweep_once(set(), now=time.time() + 3600, session_db=opener) == []


def test_archived_card_after_the_wake_leaves_no_second_notice_turn(board):
    """t_01a1cfb6db097701 (2026-11-12): the wake delivered at 04:40:25, the card was archived at 04:41:22 (its local
    row went with it), and the tui row's later claim found no sibling and started a second turn at 04:59:20."""
    from hermes_cli.kanban_db_archive import archive_task

    runner, adapter = board
    tid = _card()
    _event(tid, "completed")
    _wake_leg(runner)
    conn = kbc.connect()
    try:
        archive_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ? AND platform = 'local'", (tid,))
    finally:
        conn.close()
    assert _notice_leg() == []
    assert len(adapter.turns) == 1


def test_a_stuck_tui_claim_rewound_after_the_wake_and_archive_starts_no_second_turn(board):
    """The live shape (t_01a1cfb6db097701): the tui row was claimed before the wake (04:25:06), the wake delivered
    (04:40:25), the card was archived (04:41:22), and the stuck claim was rewound for redelivery (04:46:05)."""
    from hermes_cli.kanban_db_archive import archive_task

    runner, adapter = board
    tid = _card()
    _event(tid, "completed")
    conn = kbc.connect()
    try:
        old, cur, events = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=CHAT,
                                                           thread_id="", durable=True)
        assert events and cur > old
    finally:
        conn.close()
    _wake_leg(runner)
    conn = kbc.connect()
    try:
        archive_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ? AND platform = 'local'", (tid,))
            row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ? AND platform = 'tui'",
                               (tid,)).fetchone()
            assert kbn._rewind_orphan(conn, kbn._sub_key(tid, "tui", CHAT, ""), row, why="stopped serving the chat")
    finally:
        conn.close()
    assert _notice_leg() == []
    assert len(adapter.turns) == 1


# Review of PR #1465 at 67d5fb1cfc: silencing the tui row because a waking local row EXISTS lost the event when the
# tui leg ran first and the wake never landed. These pass on the merge-base and must keep passing.

class FailingAdapter(LocalAdapter):
    async def handle_message(self, event):
        raise RuntimeError("local FIFO refused the wake")


def test_notice_first_then_a_wake_that_fails_until_dropped_still_delivers_the_notice(board):
    from gateway.kanban_watchers_notifier import MAX_SEND_FAILURES

    runner, _ = board
    failing = FailingAdapter()
    runner.adapters = {Platform.LOCAL: failing}
    tid = _card()
    _event(tid, "completed")
    texts = _notice_leg()  # the tui leg first: the wake still owes the event, so nothing is acked here
    counts: dict = {}
    for _ in range(MAX_SEND_FAILURES + 1):
        _wake_leg(runner, counts)
    _last, subs = _cursors(tid)
    assert "local" not in subs  # dropped after MAX_SEND_FAILURES
    texts += _notice_leg()
    assert len(texts) == 1 and NOTICE["completed"].format(tid=tid) in texts[0]


def test_notice_first_with_no_local_adapter_delivers_the_notice_once_the_wait_ends(board, monkeypatch):
    from gateway import kanban_wake_sibling

    runner, _ = board
    runner.adapters = {}
    tid = _card()
    _event(tid, "completed")
    _wake_leg(runner)
    assert _notice_leg() == []  # within the window: left to the wake
    monkeypatch.setattr(kanban_wake_sibling, "DEFER_MAX_S", -1.0)  # the wake never landed in time
    _wake_leg(runner)
    texts = _notice_leg()
    assert len(texts) == 1 and NOTICE["completed"].format(tid=tid) in texts[0]


def test_unloaded_sweeper_delivers_a_covered_event_the_wake_never_delivered(board, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    db = SessionDB(tmp_path / "state.db")
    db.create_session(CHAT, "desktop")
    db.append_message(CHAT, "user", "start the cards")

    @contextlib.contextmanager
    def opener(chat_id):
        yield (db, chat_id) if db.get_session(chat_id) else (None, None)

    board[0].adapters = {}
    tid = _card()
    _event(tid, "completed")
    ku.sweep_once(set(), now=time.time() + 3600, session_db=opener)  # an hour on, past DEFER_MAX_S
    rows = [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"]
    assert len(rows) == 1 and NOTICE["completed"].format(tid=tid) in (rows[0].get("content") or "")


def test_archived_card_after_a_local_failure_wake_leaves_no_second_notice_turn(board):
    """Review of PR #1465 at 196018f8a7: a local failure appends a provisional canonical ``crashed`` event in the same
    transaction as ``failure_finalized``. The tui row shows nothing for the provisional event, so it must not stop the
    wake from settling the tui row; otherwise archiving the card after the wake delivers failure_finalized again."""
    from hermes_cli.kanban_db_archive import archive_task

    runner, adapter = board
    tid = _card()
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "crashed", {"error": "worker exited 1", "failure_notice": "canonical"})
            kb._append_event(conn, tid, "failure_finalized", {"error": "worker exited 1", "failures": 1,
                                                             "trigger_outcome": "crashed", "tripped": False,
                                                             "run_uid": "local:1", "final_status": "ready"})
    finally:
        conn.close()
    _wake_leg(runner)
    assert len(adapter.turns) == 1
    last, subs = _cursors(tid)
    assert subs["tui"] == (last, None)  # the wake settled the tui row past the provisional event
    conn = kbc.connect()
    try:
        archive_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ? AND platform = 'local'", (tid,))
    finally:
        conn.close()
    assert _notice_leg() == []
    assert len(adapter.turns) == 1


# Review of PR #1465 at 8be3211785: an uncovered notice (status, quota, re-pin) ahead of the completion kept the wake
# from settling the tui row, so after an archive the tui batch delivered the completion a second time.

def _status(tid, status="ready"):
    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "status", {"status": status})
    finally:
        conn.close()


def _archive_and_drop_local(tid, rewind_tui=False):
    from hermes_cli.kanban_db_archive import archive_task
    conn = kbc.connect()
    try:
        archive_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id = ? AND platform = 'local'", (tid,))
            if rewind_tui:
                row = conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id = ? AND platform = 'tui'",
                                   (tid,)).fetchone()
                assert kbn._rewind_orphan(conn, kbn._sub_key(tid, "tui", CHAT, ""), row, why="stopped serving the chat")
    finally:
        conn.close()


def test_uncovered_notice_before_the_completion_then_archive_leaves_no_second_completion(board):
    runner, adapter = board
    tid = _card()
    _status(tid)
    _event(tid, "completed")
    _wake_leg(runner)
    [turn] = adapter.turns
    assert "→ ready" in turn and NOTICE["completed"].format(tid=tid) in turn  # the wake carries both lines
    last, subs = _cursors(tid)
    assert subs["tui"] == (last, None)  # settled past the status notice it carried
    _archive_and_drop_local(tid)
    texts = _notice_leg()
    assert not any(NOTICE["completed"].format(tid=tid) in t for t in texts), texts
    assert not any("→ ready" in t for t in texts), texts  # shown once, in the wake turn
    assert len(adapter.turns) == 1


def test_a_stuck_tui_claim_with_an_uncovered_notice_ahead_rewound_after_wake_and_archive_starts_no_second_turn(board):
    runner, adapter = board
    tid = _card()
    _status(tid)
    _event(tid, "completed")
    conn = kbc.connect()
    try:
        old, cur, events = kbn.claim_unseen_events_for_sub(conn, task_id=tid, platform="tui", chat_id=CHAT,
                                                           thread_id="", durable=True)
        assert len(events) >= 2 and cur > old
    finally:
        conn.close()
    _wake_leg(runner)
    _archive_and_drop_local(tid, rewind_tui=True)
    texts = _notice_leg()
    assert not any(NOTICE["completed"].format(tid=tid) in t for t in texts), texts
    assert len(adapter.turns) == 1


def test_an_uncovered_notice_the_tui_row_already_delivered_is_not_repeated_in_the_wake(board):
    runner, adapter = board
    tid = _card()
    _status(tid)
    texts = _notice_leg()  # no covered event yet: the tui row shows the status itself
    assert len(texts) == 1 and "→ ready" in texts[0]
    _event(tid, "completed")
    _wake_leg(runner)
    [turn] = adapter.turns
    assert "→ ready" not in turn and NOTICE["completed"].format(tid=tid) in turn
    assert _notice_leg() == [] and len(adapter.turns) == 1


def test_a_covered_event_landing_between_the_defer_check_and_the_claim_goes_only_to_the_wake(board, monkeypatch):
    """Nit on PR #1465 at 8be3211785: tui_defer and the claim are separate statements. Model the race by letting
    tui_defer miss the completion; the poller must hand the claim back rather than render it."""
    from gateway import kanban_wake_sibling
    runner, adapter = board
    tid = _card()
    _event(tid, "completed")
    monkeypatch.setattr(kanban_wake_sibling, "tui_defer", lambda *a, **k: False)
    assert _notice_leg() == []
    last, subs = _cursors(tid)
    assert subs["tui"][0] < last and subs["tui"][1] is None  # handed back, not acked or held
    _wake_leg(runner)
    assert len(adapter.turns) == 1 and NOTICE["completed"].format(tid=tid) in adapter.turns[0]
    last, subs = _cursors(tid)
    assert subs["tui"] == (last, None)
    assert _notice_leg() == [] and len(adapter.turns) == 1


def _wake_cards(runner):
    """One wake-leg pass that captures each wake's text and folding card instead of turning it."""
    out = []

    async def tick():
        deliveries = await asyncio.to_thread(_notifier_collect, runner, kb, notifier_profile="default",
                                             gc_due=False, gc_retention_days=30)
        for d in deliveries:
            n = _KanbanNotification(runner, d, platform_cls=Platform, sub_fail_counts={})
            await n.deliver()
            out.append((n.synth, n.wake_card))
    asyncio.run(tick())
    return out


def test_a_queued_wake_folded_into_a_later_wake_for_the_card_keeps_the_notice_lines_it_carried(board):
    """Review of PR #1465 at b0fdf842f2 (probe P1): wake A carries an uncovered ``→ ready`` line and the tui row is
    settled past it at admission; if A is still queued when B (the completion) folds into it, ``_merge`` replaced
    A's card with B's and the line was gone for good."""
    import json
    from types import SimpleNamespace

    from gateway.kanban_wake_fold import card_metadata, fold_into
    runner, _adapter = board
    tid = _card()
    _status(tid)
    _event(tid, "blocked")
    [(text_a, card_a)] = _wake_cards(runner)
    assert "→ ready" in text_a and "→ ready" in "\n".join(card_a["notices"])
    last, subs = _cursors(tid)
    assert subs["tui"] == (last, None)  # settled at admission
    _event(tid, "completed")
    [(text_b, card_b)] = _wake_cards(runner)
    assert "→ ready" not in text_b
    waiting = {"request_id": json.dumps(["kanban-wake", "a"]),
               "payload": {"text": text_a, "local_automation_v1": {"kanban_cards": [card_a]}}}
    event = SimpleNamespace(text=text_b, metadata={"kanban_cards": [card_b]})
    fold_into(event, waiting, json.dumps(["kanban-wake", "b"]))
    assert "→ ready" in event.text and NOTICE["completed"].format(tid=tid) in event.text
    assert event.text.index("→ ready") < event.text.index(NOTICE["completed"].format(tid=tid))
    [blocked] = [line for line in card_a["notices"] if "→ ready" not in line]
    assert blocked in event.text  # the covered blocked notice A carried survives too
    assert card_metadata(event.metadata)["kanban_cards"][0]["notices"]  # what a third wake folds from
    _archive_and_drop_local(tid)
    assert _notice_leg() == []  # the tui row owes nothing: the folded turn is the one place it shows


def test_unloaded_sweeper_hands_back_a_claim_that_raced_a_covered_event(board, tmp_path, monkeypatch):
    """Nit on PR #1465 at b0fdf842f2 (probe P2): the sweeper's tui_defer and claim are separate statements too."""
    from gateway import kanban_wake_sibling
    monkeypatch.setattr(kanban_wake_sibling, "DEFER_MAX_S", 7200.0)
    monkeypatch.setenv("HERMES_KANBAN_UNLOADED_WAKE", "0")
    db = SessionDB(tmp_path / "state.db")
    db.create_session(CHAT, "desktop")
    db.append_message(CHAT, "user", "start the cards")

    @contextlib.contextmanager
    def opener(chat_id):
        yield (db, chat_id) if db.get_session(chat_id) else (None, None)

    runner, adapter = board
    tid = _card()
    _event(tid, "completed")
    monkeypatch.setattr(kanban_wake_sibling, "tui_defer", lambda *a, **k: False)  # the completion lands after it
    assert ku.sweep_once(set(), now=time.time() + 3600, session_db=opener) == []
    assert [m for m in db.get_messages(CHAT) if m.get("display_kind") == "kanban_notice"] == []
    last, subs = _cursors(tid)
    assert subs["tui"][0] < last and subs["tui"][1] is None  # handed back, not acked or held
    _wake_leg(runner)
    assert len(adapter.turns) == 1 and NOTICE["completed"].format(tid=tid) in adapter.turns[0]
    assert _cursors(tid)[1]["tui"] == (last, None)


@pytest.mark.parametrize("carrier_first", [True, False], ids=["notices-in-earlier-wake", "notices-in-later-wake"])
def test_two_wakes_for_one_card_fold_in_either_order_keeping_every_notice_once(carrier_first):
    from gateway.kanban_wake_fold import _merge
    head = "[kanban] Task t_x {}.\nTitle: x"
    plain = {"task_id": "t_x", "board": "b", "kinds": ["blocked"], "text": head.format("blocked") + "\n⏸ blocked"}
    carrier = {"task_id": "t_x", "board": "b", "kinds": ["completed"], "notices": ["🔄 t_x → ready", "✔ t_x done"],
               "text": head.format("completed") + "\n🔄 t_x → ready\n✔ t_x done"}
    plain = {**plain, "notices": ["⏸ blocked"]}
    first, second = (carrier, plain) if carrier_first else (plain, carrier)
    [card] = _merge([first], [second])
    assert card["text"].startswith(second["text"].split("\n")[0])  # the latest premise heads the card
    for line in ("🔄 t_x → ready", "✔ t_x done", "⏸ blocked"):
        assert card["text"].count(line) == 1, card["text"]
    assert sorted(card["notices"]) == sorted(["🔄 t_x → ready", "✔ t_x done", "⏸ blocked"])
    [again] = _merge([card], [dict(second)])  # a third wake re-folding the same card adds nothing twice
    assert again["text"] == card["text"]
