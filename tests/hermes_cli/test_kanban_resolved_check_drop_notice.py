"""A dropped resolved_check (resolved_check_broken / _lost) notifies each card origin exactly once.

The notice must say automatic detection stopped until the ask is re-filed, and must not repeat the filed ask's
own "I'll notice automatically" promise as if it were still true.
"""
import asyncio
import json
import sqlite3
from contextlib import closing

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_cli.kanban_block_notice import block_notice
from tests.gateway import test_kanban_changes_requested_notifier as helpers
from tests.tui_gateway.test_kanban_remote_notices import CHAT, _card, _hub as _base_hub
from tui_gateway import kanban_remote_notices as krn
from tui_gateway import server

PROMISE = "I will detect this automatically once the DNS record exists and resume the card on my own."
DROPPED_AT = 1_800_000_100


def _request(dropped=True, **extra):
    req = {"action": f"Create the DNS record. {PROMISE}", "why_human": "Only you hold the registrar login",
           "interaction": "external", "scope": "example.com TXT record", "evidence": "dig exited 1",
           "resolved_check": "dig +short TXT x.example.com | grep -q ok", "resolved_check_every": 900}
    if dropped:
        req.update(resolved_check_dropped_at=DROPPED_AT, resolved_check_drop_reason="resolved_check_broken")
    req.update(extra)
    return req


def _drop(conn, tid, event="resolved_check_broken", request=None, seen=True, produce=True):
    """Exercise the real atomic drop producer before collecting its origin notices."""
    from hermes_cli import kanban_ask_resolved as kar
    from hermes_cli import kanban_wake_check as kwc
    request = request or _request(dropped=False)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?", (tid,))
        kar.store_request(conn, tid, request)
        kb._append_event(conn, tid, 'blocked', {})
        if seen:  # original block delivered before the probe failed; seen=False models an offline origin
            conn.execute('UPDATE kanban_notify_subs SET last_event_id=(SELECT MAX(id) FROM task_events WHERE task_id=?) WHERE task_id=?', (tid, tid))
    if not produce:
        return
    if event == kar.BROKEN_EVENT:
        state = kwc.get_state(conn, tid)
        for at in (DROPPED_AT - 21600, DROPPED_AT - 100, DROPPED_AT):
            kar.apply_result(conn, tid, state['cmd'], 255, 'connection closed',
                             generation=state['generation'], now=at)
    else:
        for at in (DROPPED_AT - 100, DROPPED_AT):
            with kb.write_txn(conn):
                conn.execute('DELETE FROM task_wake_checks WHERE task_id=?', (tid,))
            kar.reconcile(conn, now=at)


def _assert_drop_wording(text):
    low = text.lower()
    assert "automatic detection stopped" in low
    assert "re-filed" in low
    assert "historical" in low  # the filed prose is labelled, not left as a live promise
    # The stale promise only survives inside the explicitly labelled historical quote.
    assert low.index("historical") < low.index("detect this automatically")
    assert "Create the DNS record" in text  # the human step itself is not altered or dropped


# -- shared formatter ----------------------------------------------------------------------------

@pytest.mark.parametrize("event", ["resolved_check_broken", "resolved_check_lost"])
def test_block_notice_marks_stale_promise_historical(event):
    task = {"status": "blocked", "block_kind": "needs_input",
            "human_request": json.dumps(_request(resolved_check_drop_reason=event))}
    body = block_notice(task, "t_1", "default", drop_event=event)
    _assert_drop_wording(body)
    assert "blocked — actor: user" in body and "Open task: @task:default/t_1" in body


def test_block_notice_for_dropped_card_without_event_name_still_warns():
    body = block_notice({"status": "blocked", "human_request": _request()}, "t_1", "default")
    _assert_drop_wording(body)


def test_undropped_ask_renders_unchanged_and_refiled_ask_is_not_called_stopped():
    plain = block_notice({"status": "blocked", "human_request": _request(dropped=False)}, "t_1", "default")
    assert "stopped" not in plain.lower() and "historical" not in plain.lower()
    refiled = block_notice({"status": "blocked", "human_request": _request(dropped=False)}, "t_1", "default",
                           drop_event="resolved_check_lost")
    assert "re-filed since" in refiled.lower() and "stopped" in refiled.lower()
    assert "historical" not in refiled.lower()
    resolved = block_notice({"status": "ready", "human_request": _request()}, "t_1", "default",
                            drop_event="resolved_check_lost")
    assert resolved.startswith("Earlier block resolved")


# -- gateway (telegram etc.) path ------------------------------------------------------------------

@pytest.mark.parametrize("event", ["resolved_check_broken", "resolved_check_lost"])
def test_gateway_notifier_sends_and_wakes_origin_exactly_once(tmp_path, monkeypatch, event):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "drop.db"))
    kb.init_db()
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1", delivery_mode="notify+wake")
        _drop(conn, tid, event)
    adapter = helpers.RecordingAdapter()
    runner = helpers._runner(adapter)
    for _ in range(3):
        runner._running = True
        asyncio.run(helpers._run_one_tick(monkeypatch, runner))
    texts = [s["text"] for s in adapter.sent if "automatic detection stopped" in s["text"].lower()]
    assert len(texts) == 1, adapter.sent
    _assert_drop_wording(texts[0])
    assert len(adapter.handled) == 1
    _assert_drop_wording(adapter.handled[0].text)


# -- local TUI path ----------------------------------------------------------------------------------

@pytest.mark.parametrize("event", ["resolved_check_broken", "resolved_check_lost"])
def test_tui_origin_gets_one_notice_per_drop_event(event):
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="drop-chat")
        _drop(conn, tid, event)
        claims = []
        texts = server._collect_kanban_notifications({"session_key": "drop-chat"}, claims)
        assert len(texts) == 1 and len(claims) == 1
        _assert_drop_wording(texts[0])
        claim = claims[0]
        assert kbn.ack_notify_claim(conn, task_id=tid, platform="tui", chat_id="drop-chat", thread_id="",
                                    upto=claim["new"], from_cursor=claim["old"])
        assert server._collect_kanban_notifications({"session_key": "drop-chat"}) == []


@pytest.mark.parametrize("event", ["resolved_check_broken", "resolved_check_lost"])
@pytest.mark.parametrize("platform", ["tui", "telegram"])
@pytest.mark.parametrize("consumed", [True, False])
def test_legacy_drop_is_notified_once_after_backfill(tmp_path, monkeypatch, event, platform, consumed):
    from hermes_cli import kanban_ask_resolved as kar
    from tests.hermes_cli.test_kanban_ask_backfill import _legacy

    if platform == "telegram":
        monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "legacy.db"))
    kb.init_db()
    chat = "drop-chat" if platform == "tui" else "chat-1"
    adapter = helpers.RecordingAdapter()
    runner = helpers._runner(adapter)
    texts = []
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat, delivery_mode="notify+wake")
        _drop(conn, tid, produce=False)
        _legacy(conn, tid, event, at=DROPPED_AT)
        original = max(kb.list_events(conn, tid), key=lambda e: e.id)
        # Consumed: the old notifier read the event before drop notices existed. Unconsumed: the origin was offline
        # and sees the historical drop and the backfill re-emission in one unseen batch.
        with kb.write_txn(conn):
            conn.execute("UPDATE kanban_notify_subs SET last_event_id=? WHERE task_id=?",
                         (original.id if consumed else original.id - 1, tid))
        for _ in range(3):
            kar.reconcile(conn, now=DROPPED_AT + 100)
            if platform == "tui":
                claims = []
                texts.extend(server._collect_kanban_notifications({"session_key": chat}, claims))
                for claim in claims:
                    assert kbn.ack_notify_claim(conn, task_id=tid, platform=platform, chat_id=chat, thread_id="",
                                                upto=claim["new"], from_cursor=claim["old"])
            else:
                runner._running = True
                asyncio.run(helpers._run_one_tick(monkeypatch, runner))
        if platform == "telegram":
            texts = [s["text"] for s in adapter.sent]
            assert len(adapter.handled) == 1
        assert len(texts) == 1, texts
        _assert_drop_wording(texts[0])
        drops = sorted((e for e in kb.list_events(conn, tid) if e.kind == event), key=lambda e: e.id)
        assert len(drops) == 2
        assert drops[-1].payload == {"backfilled": True, "origin_event_id": original.id}
        request = kar._request(kb.get_task(conn, tid).human_request)
        assert request["resolved_check_dropped_at"] == DROPPED_AT
        assert request["resolved_check_drop_reason"] == event


# -- remote (hub replica) path -----------------------------------------------------------------------

# -- offline origin: unseen original block followed by a drop ------------------------------------------

def _stops(texts):
    return [x for x in texts if "automatic detection stopped" in str(x).lower()]


def _plain_block(conn, tid):
    _drop(conn, tid, seen=False, request=_request(dropped=False), produce=False)  # an ordinary unseen block


@pytest.mark.parametrize("event", ["resolved_check_broken", "resolved_check_lost"])
def test_tui_offline_origin_unseen_block_plus_drop_warns_once(event):
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="off-chat")
        _drop(conn, tid, event, seen=False)
        claims = []
        texts = server._collect_kanban_notifications({"session_key": "off-chat"}, claims)
        assert len(texts) == 1 and len(_stops(texts)) == 1, texts
        _assert_drop_wording(texts[0])
        claim = claims[0]  # the whole range (block + drop) is acked; nothing replays
        assert kbn.ack_notify_claim(conn, task_id=tid, platform="tui", chat_id="off-chat", thread_id="",
                                    upto=claim["new"], from_cursor=claim["old"])
        assert server._collect_kanban_notifications({"session_key": "off-chat"}) == []


def test_tui_ordinary_block_alone_still_notifies():
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="plain-chat")
        _plain_block(conn, tid)
        texts = server._collect_kanban_notifications({"session_key": "plain-chat"})
        assert len(texts) == 1 and not _stops(texts), texts


def test_tui_drop_alone_still_notifies():
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="dalone-chat")
        _drop(conn, tid, seen=True)
        texts = server._collect_kanban_notifications({"session_key": "dalone-chat"})
        assert len(texts) == 1 and len(_stops(texts)) == 1


@pytest.mark.parametrize("event", ["resolved_check_broken", "resolved_check_lost"])
def test_gateway_offline_origin_unseen_block_plus_drop_sends_one_stop_warning(tmp_path, monkeypatch, event):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "off.db"))
    kb.init_db()
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1", delivery_mode="notify+wake")
        _drop(conn, tid, event, seen=False)
    adapter = helpers.RecordingAdapter()
    runner = helpers._runner(adapter)
    for _ in range(3):
        runner._running = True
        asyncio.run(helpers._run_one_tick(monkeypatch, runner))
    assert len(_stops([s["text"] for s in adapter.sent])) == 1, adapter.sent
    assert len(adapter.handled) == 1 and len(_stops([adapter.handled[0].text])) == 1


def test_gateway_ordinary_block_alone_still_sends(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "plain.db"))
    kb.init_db()
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="DNS", assignee="worker")
        kbn.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1", delivery_mode="notify+wake")
        _plain_block(conn, tid)
    adapter = helpers.RecordingAdapter()
    runner = helpers._runner(adapter)
    runner._running = True
    asyncio.run(helpers._run_one_tick(monkeypatch, runner))
    assert len(adapter.sent) == 1 and not _stops([s["text"] for s in adapter.sent])


def test_remote_origin_gets_one_extra_notice_when_the_replica_shows_the_drop(tmp_path):
    t0 = 1_800_000_000.0
    replica = _base_hub(tmp_path, [])
    with sqlite3.connect(replica) as conn:
        conn.executemany("INSERT INTO meta VALUES (?,?)", [("cursor", "100"), ("head", "100")])
    ledger = tmp_path / "ledger.db"
    krn._reset_for_tests()

    def put(card, seq):
        with sqlite3.connect(replica) as conn:
            conn.execute("DELETE FROM replica WHERE key=?", (card["key"],))
            conn.execute("INSERT INTO replica VALUES ('hub','tasks',?,?,?)", (card["key"], json.dumps(card["task"]), seq))
        krn._reset_for_tests()

    def poll(now):
        got = krn.collect(CHAT, "default", hub_db=replica, ledger=ledger, now=now)
        for claim in got:
            assert krn.ack(claim, ledger=ledger)
        return got

    assert poll(t0 - 60) == []
    card = _card("t_drop", CHAT, status="blocked", at=t0)
    card["task"]["human_request"] = _request(dropped=False)
    put(card, 1)
    first = poll(t0 + 5)
    assert len(first) == 1 and "automatic detection stopped" not in first[0]["text"].lower()
    assert poll(t0 + 6) == []
    card["task"]["human_request"] = _request(resolved_check_dropped_at=t0 + 60)
    put(card, 2)
    second = poll(t0 + 65)
    assert len(second) == 1, second
    _assert_drop_wording(second[0]["text"])
    assert second[0]["state"] != first[0]["state"]
    assert poll(t0 + 66) == []
    krn._reset_for_tests()
    assert poll(t0 + 67) == []
